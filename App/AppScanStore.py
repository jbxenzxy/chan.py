# -*- coding: utf-8 -*-
"""
App/AppScanStore.py —— 批量扫描结果共享存储（SQLite，跨进程）
=========================================================================
设计文档 V10 方案 5.10 / 8.10：

  - ProcessPoolExecutor 的 worker 是独立进程，API 进程的内存字典对
    worker 不可见 → 扫描结果（task_id → 结果）用 SQLite 跨进程共享：
    worker 写入、API 进程读取供前端轮询。零额外依赖。
  - SQLite 多进程并发写入：开启 WAL 模式 + 写入 timeout（30 秒）+
    重试机制（database is locked 时退避重试）。冲突频繁时的兜底方案
    （每 worker 独立文件、最后合并）作为文档化备选，本版不启用。

分层缓存边界（5.10）：
  - 进程内字典：K 线 / 股票名 / PE / 实时行情快照（交互路径与 SSE 都在
    API 进程内，无需跨进程）。
  - SQLite：扫描结果（task_id → 结果，供前端轮询）→ 本文件。

单一事实源（交叉评审 W2 修复）：
  - completed 不落列，由 scan_results 行数 COUNT 派生（单事务单源，
    无漂移）；结果表主键为 (task_id, seq)，写入用 INSERT OR IGNORE
    （重复 seq 幂等，崩溃兜底由收割线程补写错误行，completed 单调收敛）。
  - 增量读取（交叉评审 W1 修复）：get_results(task_id, since) 按
    seq >= since 游标返回，前端按 row.seq + 1 推进，避免全量回传 O(n²)。

线程 / 进程安全：
  - 每次操作独立连接（WAL + busy_timeout），天然线程安全（check_same_thread=False）；
  - 多进程各自打开连接，WAL 允许一写多读并发，写冲突由 timeout + 重试兜底。
  - 惰性单例（交叉评审 W8 修复）：get_scan_store() 首次调用才建库，
    import 期零副作用；DB 路径支持 SCAN_TASK_DB 环境变量覆盖（测试隔离）。
"""
import json
import os
import sqlite3
import threading
import time
import uuid

# 写冲突重试参数（设计 8.10：timeout 30s + 重试机制）
_WRITE_TIMEOUT_S = 30.0
_LOCKED_RETRY = 3
_LOCKED_BACKOFF = 0.05

_SCHEMA = """
CREATE TABLE IF NOT EXISTS scan_tasks (
    task_id     TEXT PRIMARY KEY,
    status      TEXT NOT NULL DEFAULT 'pending',   -- pending/running/done/aborted/error
    total       INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL,
    error       TEXT,
    params_json TEXT,
    abort_requested INTEGER NOT NULL DEFAULT 0   -- 中止请求旗（worker 每票前检查）
);
CREATE TABLE IF NOT EXISTS scan_results (
    task_id     TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    code        TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'ok',   -- ok/error/aborted（供 skip_log 汇总）
    result_json TEXT NOT NULL,
    PRIMARY KEY (task_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_scan_results_task ON scan_results(task_id, seq);
"""


def _default_db_path():
    """DB 路径解析：SCAN_TASK_DB 环境变量优先（测试隔离/多实例），
    否则落在 vipdoc_dir 下。"""
    env = os.environ.get("SCAN_TASK_DB")
    if env:
        return env
    from App.AppConfig import app_config
    return os.path.join(app_config.vipdoc_dir, "scan_tasks.db")


class ScanStore:
    """扫描任务 + 结果 的 SQLite 存储（跨进程共享）"""

    def __init__(self, db_path=None, timeout=_WRITE_TIMEOUT_S):
        if db_path is None:
            db_path = _default_db_path()
        self.db_path = db_path
        self.timeout = timeout
        self._init_lock = threading.Lock()
        self._init_db()

    # ── 连接管理 ─────────────────────────────────────────────────────
    def _connect(self):
        """打开连接：WAL 模式 + busy_timeout（毫秒）。每次操作独立连接。"""
        conn = sqlite3.connect(self.db_path, timeout=self.timeout,
                               check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=%d" % int(self.timeout * 1000))
        return conn

    def _init_db(self):
        """建表（幂等）。多进程并发初始化由 WAL + 幂等 DDL 兜底。

        旧版 schema（含 done 列 / 结果主键 (task_id, code)）为阶段 7 初版
        遗留，扫描数据为瞬态，检测到旧 schema 时重建两表（W2 修复）。
        注意：必须在 executescript(_SCHEMA) 之前探测旧 schema——旧表
        scan_results 无 seq 列，若先执行 _SCHEMA 的 CREATE INDEX 会抛
        "no such column: seq"。
        """
        parent = os.path.dirname(self.db_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with self._init_lock:
            conn = self._connect()
            try:
                # 先探测旧 schema（含 done 列）→ 直接重建两表
                old_cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(scan_tasks)").fetchall()]
                if "done" in old_cols:
                    conn.execute("DROP TABLE IF EXISTS scan_results")
                    conn.execute("DROP TABLE IF EXISTS scan_tasks")
                conn.executescript(_SCHEMA)
                cols = [r[1] for r in conn.execute(
                    "PRAGMA table_info(scan_tasks)").fetchall()]
                # 增量迁移：缺 abort_requested 列 → ALTER TABLE ADD COLUMN
                # （阶段 7 初版无此列，中止语义升级为「请求旗 + 收割后终态」）
                if "abort_requested" not in cols:
                    conn.execute(
                        "ALTER TABLE scan_tasks ADD COLUMN "
                        "abort_requested INTEGER NOT NULL DEFAULT 0")
                conn.commit()
            finally:
                conn.close()

    def _execute(self, sql, params=()):
        """带重试的写执行：database is locked → 退避重试（设计 8.10）。"""
        last_err = None
        for attempt in range(_LOCKED_RETRY + 1):
            conn = self._connect()
            try:
                cur = conn.execute(sql, params)
                conn.commit()
                return cur
            except sqlite3.OperationalError as exc:
                last_err = exc
                if "locked" not in str(exc).lower():
                    raise
                time.sleep(_LOCKED_BACKOFF * (attempt + 1))
            finally:
                conn.close()
        raise last_err

    def _query_one(self, sql, params=()):
        conn = self._connect()
        try:
            cur = conn.execute(sql, params)
            row = cur.fetchone()
            return row
        finally:
            conn.close()

    def _query_all(self, sql, params=()):
        conn = self._connect()
        try:
            cur = conn.execute(sql, params)
            return cur.fetchall()
        finally:
            conn.close()

    # ── 任务生命周期 ─────────────────────────────────────────────────
    def create_task(self, task_id=None, total=0, params=None):
        """创建任务（status=pending）。

        task_id 缺省时内部自动生成（时间戳前缀 + uuid 片段，评审「优势 5」
        采纳：生成逻辑收敛到存储层，调用方零心智负担；格式保持 W13 修复的
        可读可排序语义）。返回实际 task_id。
        """
        if not task_id:
            task_id = (time.strftime("%Y%m%d%H%M%S") + "-"
                       + uuid.uuid4().hex[:8])
        now = time.time()
        self._execute(
            "INSERT INTO scan_tasks (task_id, status, total, created_at, updated_at, error, params_json) "
            "VALUES (?, 'pending', ?, ?, ?, NULL, ?)",
            (task_id, int(total), now, now,
             json.dumps(params or {}, ensure_ascii=False)))
        return task_id

    def set_status(self, task_id, status, error=None):
        """更新任务状态（running/done/aborted/error）"""
        self._execute(
            "UPDATE scan_tasks SET status=?, error=?, updated_at=? WHERE task_id=?",
            (status, error, time.time(), task_id))

    def is_aborted(self, task_id):
        """任务是否已请求中止（worker 每票前检查）。

        中止语义（交叉评审「中止后 completed 收敛」）：abort 只置
        abort_requested 请求旗，不立即改 status——worker 继续把 queued
        票快速落库 aborted 行，收割线程等全部 future 完成后才把 status
        置为 aborted 终态。故 is_aborted 查请求旗而非 status。
        """
        row = self._query_one(
            "SELECT abort_requested FROM scan_tasks WHERE task_id=?",
            (task_id,))
        return bool(row) and row[0] == 1

    def request_abort(self, task_id):
        """置中止请求旗（abort 入口调用；不改变 status，终态由收割线程定）。"""
        self._execute(
            "UPDATE scan_tasks SET abort_requested=1, updated_at=? "
            "WHERE task_id=?",
            (time.time(), task_id))

    def abort_all_running(self):
        """中止所有 pending/running 任务（旧接口 scan_abort 兜底调用）。

        ProcessPool worker 是独立进程，看不到主进程 _scan_aborted 标志；
        旧接口 /api/scan_abort 无 task_id 参数，无法精确中止单个任务，
        故兜底中止全部进行中的批量任务（实际使用中同时只有一个）。
        只置 abort_requested 请求旗，终态由各任务收割线程收敛。
        返回中止条数。
        """
        rows = self._query_all(
            "SELECT task_id FROM scan_tasks "
            "WHERE status IN ('pending','running')")
        for (task_id,) in rows:
            self.request_abort(task_id)
        return len(rows)

    # ── 结果写入（worker 进程调用）───────────────────────────────────
    def put_result(self, task_id, seq, code, status, result):
        """写入单只结果（单事务，INSERT OR IGNORE 幂等）。

        - 主键 (task_id, seq)：股票清单重复 code 时各占独立 seq，互不覆盖；
        - INSERT OR IGNORE：收割线程崩溃兜底补写错误行时不覆盖 worker
          已写行；completed 由行数派生单调收敛 total（W2 修复）。
        """
        self._execute(
            "INSERT OR IGNORE INTO scan_results "
            "(task_id, seq, code, status, result_json) VALUES (?, ?, ?, ?, ?)",
            (task_id, int(seq), code, status,
             json.dumps(result, ensure_ascii=False)))
        self._execute(
            "UPDATE scan_tasks SET updated_at=? WHERE task_id=?",
            (time.time(), task_id))

    # ── 读取（API 进程调用，供前端轮询）──────────────────────────────
    def get_task(self, task_id):
        row = self._query_one(
            "SELECT task_id, status, total, created_at, updated_at, error, "
            "params_json, abort_requested "
            "FROM scan_tasks WHERE task_id=?", (task_id,))
        if not row:
            return None
        completed = self._query_one(
            "SELECT COUNT(*) FROM scan_results WHERE task_id=?", (task_id,))[0]
        return {
            "task_id": row[0],
            "status": row[1],
            "total": row[2],
            "completed": completed,
            "created_at": row[3],
            "updated_at": row[4],
            "error": row[5],
            "params": json.loads(row[6]) if row[6] else {},
            "abort_requested": bool(row[7]),
        }

    def get_results(self, task_id, since=0):
        """增量读取：返回 seq >= since 的结果行（含首行，>= 语义勿改 >）。

        since 语义为「下次期望的 seq」（从 0 起），调用方以 row.seq + 1
        推进，保证第 0 行不被 > 漏读（交叉评审 W1 实测教训）。
        """
        rows = self._query_all(
            "SELECT seq, code, status, result_json FROM scan_results "
            "WHERE task_id=? AND seq >= ? ORDER BY seq ASC",
            (task_id, int(since)))
        out = []
        for seq, code, status, result_json in rows:
            try:
                result = json.loads(result_json)
            except (TypeError, ValueError):
                result = {"code": code, "error": "结果反序列化失败"}
            if isinstance(result, dict):
                result.setdefault("code", code)
            out.append({"seq": seq, "code": code, "status": status,
                        "data": result})
        return out

    def get_status(self, task_id, since=0):
        """前端轮询视图：{task_id, status, total, completed, results, error}"""
        task = self.get_task(task_id)
        if task is None:
            return None
        task["results"] = self.get_results(task_id, since=since)
        return task

    def iter_error_rows(self, task_id):
        """错误 / 中止行（供收割线程合并进 _scan_skip_log 汇总打印）。

        返回 [{code, status, data}]；中止行由调用方按 data.aborted 排除。
        """
        rows = self._query_all(
            "SELECT code, status, result_json FROM scan_results "
            "WHERE task_id=? AND status != 'ok' ORDER BY seq ASC",
            (task_id,))
        out = []
        for code, status, result_json in rows:
            try:
                data = json.loads(result_json)
            except (TypeError, ValueError):
                data = {}
            out.append({"code": code, "status": status, "data": data})
        return out

    # ── 维护 ─────────────────────────────────────────────────────────
    def cleanup_old(self, keep_seconds=7 * 86400):
        """清理过期任务（含结果）。返回清理条数。"""
        cutoff = time.time() - keep_seconds
        rows = self._query_all(
            "SELECT task_id FROM scan_tasks WHERE created_at < ?", (cutoff,))
        ids = [r[0] for r in rows]
        for tid in ids:
            self._execute("DELETE FROM scan_results WHERE task_id=?", (tid,))
            self._execute("DELETE FROM scan_tasks WHERE task_id=?", (tid,))
        return len(ids)


# 惰性单例（交叉评审 W8 修复）：首次 get_scan_store() 才建库，import 期零副作用
_scan_store = None
_scan_store_lock = threading.Lock()


def get_scan_store():
    """获取扫描任务库单例（API 进程与 ProcessPool worker 共用）。"""
    global _scan_store
    if _scan_store is None:
        with _scan_store_lock:
            if _scan_store is None:
                _scan_store = ScanStore()
    return _scan_store
