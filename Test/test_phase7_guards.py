# -*- coding: utf-8 -*-
"""
阶段 7：批量扫描异步化（ProcessPool 先行）—— 成果防护守护用例
=====================================================================
守护阶段 7 的结构性成果（设计文档 V10 方案 5.4 / 5.5 / 5.7 / 5.10 / 8.10），
并锁定交叉评审 W1-W15 全部接纳项（防止后续改动回潮）：

  ① 锁分类登记（W9）：LOCK_POLICY 新增 SCAN_ASYNC 类别且登记完备
     （ScannerService.submit_batch_scan / _worker_scan_one）；批量路径
     不持 _ENGINE_LOCK（进程隔离），交互路径 SERIAL 不受波及。
  ② 路由收敛与漏斗：FrontAPI 登记三条新路由（POST /api/scan/submit、
     GET /api/scan/status?since=、GET /api/scan/cancel），全部经
     run_in_threadpool(orch.scanner.*) 漏斗；FrontAPI 不直连 ScanStore
     （分层方向 FrontAPI → AppOrch → AppData/ScanStore 单向）。
  ③ 装配契约（W3/W4/W10/W11 + 合并项）：ProcessPoolExecutor 以 spawn
     上下文创建（跨平台安全，防 fork 锁继承）；initializer 屏蔽 SIGINT；
     受限环境降级 ThreadPoolExecutor（同一 worker 函数）；worker 数钳制
     [1,16]；atexit 注册显式关池；入口 __main__ 守卫在位；
     scan_pool_workers 配置面在位。
  ④ 跨进程存储契约（W1/W2/W8）：ScanStore 关键方法齐备；WAL +
     busy_timeout + locked 重试；completed 由结果行数 COUNT 派生（单一
     事实源，不落列）；put_result INSERT OR IGNORE（主键 (task_id, seq)，
     collector 兜底幂等）；get_results(task_id, since) 增量游标（>= 含首行）；
     惰性单例 get_scan_store() + SCAN_TASK_DB 环境变量覆盖（import 零副作用）。
  ⑤ 终止语义：abort 置 abort_requested 请求旗（worker 每票前检查 is_aborted）；
     queued 作业不取消（无 fut.cancel），worker 快速落库 aborted 行，收割线程
     等全部 future 完成后才置 aborted 终态（中止后 completed 收敛 total）；
     collector 合并错误明细时跳过中止行（口径与旧路径一致）；
     任务级 error 终态（worker 崩溃 ≠ 静默 done）。
  ⑥ 前端轮询契约（W1/W5/W15）：_asyncScanAll 提交 POST /api/scan/submit →
     轮询 GET /api/scan/status；since 按 row.seq + 1 推进（增量含首行）；
     终态 done/aborted/error 三态停轮询；连续 3 次失败熔断（退避重试）；
     保留旧回调形状 onData(单票)/onDone(err, interrupted)（渲染零漂移）；
     批量扫描三处调用点全部走异步径，旧 /api/scan_one 并发循环清零；
     index.html 缓存版本 v>=8（阶段 7 前端改动的缓存击穿）。
  ⑦ 功能冒烟（存储层）：ScanStore CRUD + since 增量语义 + 同 seq 幂等 +
     completed 派生收敛 + 错误行汇总 + 历史清理；空清单报错；不存在任务
     报错；SCAN_TASK_DB 环境变量隔离生效。
  ⑧ 端到端冒烟（真实 ProcessPool）：提交 N 只虚拟票（ZZxxxx 快速失败落库）
     → 轮询至终态 → completed == total、结果行 seq 连续 0..N-1、engine 标记
     ∈ {process_pool, thread_fallback}（验证 spawn worker + 跨进程 SQLite
     回流链路真实可用；受限环境降级线程池同判定）。

合并自对方阶段 7 交付并已由本守护锁定的成果：
  · worker SIGINT 屏蔽（③：Windows Ctrl+C 进程组传播实测问题）；
  · scan_pool_workers <= 0 按 CPU 自适应（③：仍钳制 [1,16]）；
  · collector 任务级 error 终态（⑤：worker 崩溃≠静默 done）；
  · index.html app.js?v=8 缓存击穿（⑥）；
  · Test/smoke_phase7.py 独立冒烟工具（③：工具面存在且 spawn 安全）。

运行：python Test/test_phase7_guards.py          # 校验（run_all 组件 14）
      python Test/test_phase7_guards.py --update  # 兼容参数（无冻结基线可更新，等价校验）
"""
import argparse
import os
import re
import sys
import tempfile
import time

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

# 行为级冒烟（⑦⑧）的环境隔离：必须在任何 App 模块导入前注入。
# SCAN_TASK_DB / SCAN_CONCURRENCY 为阶段 7 新增配置（AppConfig 读取环境变量）。
# ⚠ 一律 setdefault：spawn worker 会重导入本模块（__mp_main__），
# 直接赋值会用各自新生成的临时路径覆盖继承值 → worker 写入孤立库。
_SMOKE_TMPDIR = tempfile.mkdtemp(prefix="p7-guard-")
os.environ.setdefault("SCAN_TASK_DB", os.path.join(_SMOKE_TMPDIR, "guard_tasks.db"))
os.environ.setdefault("SCAN_CONCURRENCY", "2")

APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")
FRONTAPI = os.path.join(REPO_ROOT, "FrontAPI.py")
SCAN_STORE = os.path.join(REPO_ROOT, "App", "ScanStore.py")
SCAN_POOL = os.path.join(REPO_ROOT, "App", "ScanPool.py")
APP_ORCH = os.path.join(REPO_ROOT, "App", "AppOrch.py")
APP_CONFIG = os.path.join(REPO_ROOT, "App", "AppConfig.py")
ENV_EXAMPLE = os.path.join(REPO_ROOT, ".env.example")


def read(rel):
    with open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


def fn_body(src, header):
    """截取源码中 header（装饰器/定义行）之后到下一个同级标记前的文本。"""
    i = src.find(header)
    if i < 0:
        return None
    j = src.find("@router.", i + len(header))
    return src[i: j if j > 0 else len(src)]


# ═══════════════════════════════════════════════════════════════════════
# ① 锁分类登记（W9：SCAN_ASYNC）
# ═══════════════════════════════════════════════════════════════════════
def test_lock_policy(failures):
    import inspect
    from App import AppOrch as orch

    bad = []
    required = {"ScannerService.submit_batch_scan", "ScannerService.scan_one"}
    missing = required - set(orch.LOCK_POLICY)
    if missing:
        bad.append(f"LOCK_POLICY 缺少阶段 7 登记: {sorted(missing)}")
    # submit_batch_scan 必须为 SCAN_ASYNC（API 进程零持锁，引擎调用在 worker）
    cat = orch.LOCK_POLICY.get("ScannerService.submit_batch_scan", ("", ""))[0]
    if cat != "SCAN_ASYNC":
        bad.append(f"submit_batch_scan 类别应为 SCAN_ASYNC，实际 {cat!r}")
    # scan_one 保持 SCAN（同步旧径，worker 内每 worker 独立 _scan_lock）
    cat1 = orch.LOCK_POLICY.get("ScannerService.scan_one", ("", ""))[0]
    if cat1 != "SCAN":
        bad.append(f"scan_one 类别应为 SCAN，实际 {cat1!r}")

    # 批量提交路径：派发 worker 且不持 _ENGINE_LOCK / _scan_lock（进程隔离）
    src_pool = read("App/ScanPool.py")
    if "pool.submit(_worker_scan_one" not in src_pool:
        bad.append("ScanPool 未派发 _worker_scan_one")
    if "with _ENGINE_LOCK:" in src_pool:
        bad.append("ScanPool 持 _ENGINE_LOCK（与 SCAN_ASYNC 进程隔离语义矛盾）")

    # worker 入口：不持 _ENGINE_LOCK（引擎调用经 scan_one 内部 _scan_lock 自理）
    src_worker = inspect.getsource(orch.scanner.__class__)  # 类级兜底
    if "with _ENGINE_LOCK:" in read("App/ScanPool.py"):
        bad.append("ScanPool 持 _ENGINE_LOCK（应交由 scan_one 内部锁）")

    # 交互路径 SERIAL 不动（阶段 3 守护①的回归锚点）
    if "with _ENGINE_LOCK:" not in inspect.getsource(orch.call_analysis):
        bad.append("call_analysis 丢失 _ENGINE_LOCK（交互路径被阶段 7 改动波及）")

    if bad:
        failures.extend(f"锁分类: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ① 锁分类: {b}")
    else:
        n_async = sum(1 for c, _ in orch.LOCK_POLICY.values() if c == "SCAN_ASYNC")
        print(f"[PASS] ① 锁分类登记: SCAN_ASYNC {n_async} 项在位，批量路径"
              f"零引擎锁（进程隔离），交互路径 SERIAL 未受波及")


# ═══════════════════════════════════════════════════════════════════════
# ② 路由收敛与漏斗
# ═══════════════════════════════════════════════════════════════════════
def test_routes_funnel(failures):
    src = read("FrontAPI.py")
    bad = []
    expect = [
        ('@router.post("/api/scan/submit")', "orch.scanner.submit_batch_scan"),
        ('@router.get("/api/scan/status")', "orch.scanner.get_batch_scan_status"),
        ('@router.get("/api/scan/cancel")', "orch.scanner.abort_batch_scan"),
    ]
    for header, call in expect:
        body = fn_body(src, header)
        if body is None:
            bad.append(f"缺少路由 {header}")
            continue
        # 归一化空白：run_in_threadpool(\n    orch.scanner.*) 跨行也匹配
        body_flat = re.sub(r"\s+", "", body)
        if "run_in_threadpool(" + call not in body_flat:
            bad.append(f"{header} 未经 run_in_threadpool({call}) 漏斗")
    # 增量游标参数（W1）：/api/scan/status 必须带 since 查询参数
    if "since: int = Query(0, ge=0)" not in src:
        bad.append("/api/scan/status 缺少 since 增量游标参数（W1 未修复）")
    # FrontAPI 禁止直连扫描任务库（分层方向 FrontAPI → AppOrch → ScanStore）
    if "from App.ScanStore import" in src or "import ScanStore" in src:
        bad.append("FrontAPI 直连 App/ScanStore（应经 AppOrch 漏斗）")
    # AppOrch 禁止反向依赖 FrontAPI
    if re.search(r"import\s+FrontAPI|from\s+FrontAPI", read("App/AppOrch.py")):
        bad.append("AppOrch 反向依赖 FrontAPI（分层方向被破坏）")
    if bad:
        failures.extend(f"路由/漏斗: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ② 路由收敛与漏斗: {b}")
    else:
        print("[PASS] ② 路由收敛与漏斗: 3 条新路由全部经 run_in_threadpool"
              "(orch.scanner.*) 漏斗；since 增量游标在位；分层方向单向无反向依赖")


# ═══════════════════════════════════════════════════════════════════════
# ③ 装配契约（W3/W4/W10/W11 + 合并项）
# ═══════════════════════════════════════════════════════════════════════
def test_pool_assembly(failures):
    src_pool = read("App/ScanPool.py")
    src_store = read("App/ScanStore.py")
    bad = []
    # W4：显式 spawn 上下文（POSIX 默认 fork 在多线程 API 进程内有锁继承风险）
    if 'multiprocessing.get_context("spawn")' not in src_pool:
        bad.append("ProcessPoolExecutor 未使用 spawn 上下文（fork 锁继承风险）")
    if "mp_context=ctx" not in src_pool:
        bad.append("未将 spawn ctx 传入 mp_context")
    # 合并项：initializer 屏蔽 SIGINT（Windows Ctrl+C 进程组传播）
    if "initializer=_worker_init" not in src_pool:
        bad.append("ProcessPoolExecutor 未用 initializer 屏蔽 SIGINT")
    if "signal.SIGINT, signal.SIG_IGN" not in src_pool:
        bad.append("_worker_init 缺少 SIGINT 屏蔽（Windows Ctrl+C 将打断 worker）")
    # W3：线程池降级路径（受限环境 ThreadPoolExecutor 同一 worker 函数）
    if "ThreadPoolExecutor(" not in src_pool:
        bad.append("缺少线程池降级路径（受限环境将无兜底）")
    # W10：worker 数上限钳制（防 64 核机器打爆内存）
    if "min(_SCAN_POOL_MAX_WORKERS" not in src_pool:
        bad.append("worker 数未钳制上限 [1, 16]（W10 未修复）")
    # 合并项：<=0 按 CPU 自适应
    if "os.cpu_count()" not in src_pool:
        bad.append("worker 数缺少 <=0 时按 CPU 自适应")
    # W11：atexit 显式关池
    if "atexit.register(shutdown)" not in src_pool:
        bad.append("缺少 atexit.register(shutdown)（进程退出不清池）")
    if "cancel_futures=True" not in src_pool:
        bad.append("shutdown 未 cancel_futures（退出路径不可控）")
    # W12：提交响应带 engine 标识
    if '"engine"' not in src_pool or '"workers"' not in src_pool:
        bad.append("提交响应缺少 engine/workers 字段（W12 未修复）")
    # W13：task_id 时间戳前缀（评审「优势 5」采纳：生成逻辑收敛到
    # ScanStore.create_task 内部缺省自生成，格式保持可读可排序）
    if 'time.strftime("%Y%m%d%H%M%S")' not in src_store:
        bad.append("ScanStore.create_task 未生成时间戳前缀 task_id（W13 未修复）")
    if "uuid.uuid4().hex" not in src_store:
        bad.append("ScanStore.create_task 未生成 uuid 片段（W13 未修复）")
    # W7：提交入口 best-effort 清理历史任务（cleanup_old 不为死代码）
    if "cleanup_old(" not in src_pool:
        bad.append("提交入口未调用 cleanup_old（W7 未修复，库无限增长）")
    # spawn 安全：入口 __main__ 守卫（否则 Windows/多进程入口无限递归）
    for entry in ("FrontAPI.py", "api_server.py", "main.py"):
        if '__name__ == "__main__"' not in read(entry):
            bad.append(f"{entry} 缺少 __main__ 守卫（spawn 安全性被破坏）")
    # 配置面
    cfg = read("App/AppConfig.py")
    if "scan_pool_workers" not in cfg:
        bad.append("AppConfig 缺少 scan_pool_workers（阶段 7 配置面漂移）")
    # 独立冒烟工具在位（W14：spawn 安全，不依赖 fork monkeypatch）
    if not os.path.exists(os.path.join(TEST_DIR, "smoke_phase7.py")):
        bad.append("缺少 Test/smoke_phase7.py（手动冒烟工具）")
    if bad:
        failures.extend(f"装配契约: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ③ 装配契约: {b}")
    else:
        print("[PASS] ③ 装配契约: spawn 上下文 + SIGINT 屏蔽 + 线程池降级 + "
              "worker 钳制 [1,16] + atexit 关池 + engine/workers + 时间戳 task_id "
              "+ 提交清理 + 3 入口 __main__ 守卫 + 冒烟工具全部在位")


# ═══════════════════════════════════════════════════════════════════════
# ④ 跨进程存储契约（W1/W2/W8）
# ═══════════════════════════════════════════════════════════════════════
REQUIRED_STORE_METHODS = {
    "create_task", "set_status", "put_result", "get_task", "get_results",
    "get_status", "iter_error_rows", "cleanup_old", "is_aborted",
    "abort_all_running",
}


def test_store_contract(failures):
    import inspect
    from App.ScanStore import ScanStore

    bad = []
    missing = REQUIRED_STORE_METHODS - set(dir(ScanStore))
    if missing:
        bad.append(f"ScanStore 缺少方法: {sorted(missing)}")
    conn_src = inspect.getsource(ScanStore._connect)
    if "PRAGMA journal_mode=WAL" not in conn_src:
        bad.append("连接未启用 WAL（读写互斥将阻塞前端轮询）")
    if "busy_timeout" not in conn_src:
        bad.append("连接未设置 busy_timeout（锁等待无上限）")
    # W2：completed 由结果行数 COUNT 派生（单一事实源，不落列）
    if "SELECT COUNT(*) FROM scan_results" not in inspect.getsource(ScanStore.get_task):
        bad.append("completed 未由结果行数 COUNT 派生（双源漂移风险）")
    # W2：put_result INSERT OR IGNORE（collector 兜底幂等）
    if "INSERT OR IGNORE INTO scan_results" not in inspect.getsource(ScanStore.put_result):
        bad.append("put_result 非 INSERT OR IGNORE（collector 兜底将覆盖 worker 行）")
    # W2：主键 (task_id, seq)（重复 code 各占独立 seq）
    if "PRIMARY KEY (task_id, seq)" not in inspect.getsource(ScanStore.__init__) and \
       "PRIMARY KEY (task_id, seq)" not in read("App/ScanStore.py"):
        bad.append("结果表主键非 (task_id, seq)（重复 code 覆盖语义缺陷）")
    # W1：get_results 增量游标（>= 含首行）
    if "seq >= ?" not in inspect.getsource(ScanStore.get_results):
        bad.append("get_results 缺少 seq >= since 增量游标（W1 未修复）")
    # 写通道带重试（database is locked 退避）
    if "locked" not in inspect.getsource(ScanStore._execute).lower():
        bad.append("_execute 缺少 locked 重试（多进程锁冲突将直接失败）")
    # W8：惰性单例 + SCAN_TASK_DB 环境变量覆盖（import 零副作用）
    store_src = read("App/ScanStore.py")
    if "def get_scan_store()" not in store_src:
        bad.append("缺少 get_scan_store() 惰性单例（W8 未修复）")
    if "SCAN_TASK_DB" not in store_src:
        bad.append("DB 路径缺少 SCAN_TASK_DB 环境变量覆盖（W8 未修复）")
    if bad:
        failures.extend(f"存储契约: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ④ 跨进程存储契约: {b}")
    else:
        print("[PASS] ④ 跨进程存储契约: 10 个方法齐备，WAL + busy_timeout + "
              "COUNT 派生 completed + INSERT OR IGNORE 幂等 + (task_id, seq) 主键 "
              "+ since 增量游标 + locked 重试 + 惰性单例/环境变量覆盖")


# ═══════════════════════════════════════════════════════════════════════
# ⑤ 终止语义
# ═══════════════════════════════════════════════════════════════════════
def test_abort_semantics(failures):
    src_pool = read("App/ScanPool.py")
    bad = []
    # worker 入口检查 is_aborted（跨进程传播，快速落库 aborted 行）
    if "is_aborted(task_id)" not in src_pool:
        bad.append("worker 未检查 is_aborted(task_id)（跨进程中止）")
    if '"aborted"' not in src_pool:
        bad.append("worker 未落库 aborted 行（中止后 completed 无法收敛）")
    # queued 作业不取消：completed 收敛 total（进度条不悬挂）
    if ".cancel(" in src_pool:
        bad.append("出现 fut.cancel（queued 作业将被取消，completed 无法收敛）")
    # abort 入口：置 abort_requested 请求旗（不立即改 status，终态由收割线程收敛）
    if "request_abort(task_id)" not in src_pool:
        bad.append("abort 未置 abort_requested 请求旗")
    # 收割线程：全部 future 完成后才置 aborted 终态（中止后 completed 收敛 total）
    if 'task.get("abort_requested")' not in src_pool:
        bad.append("收割线程未按 abort_requested 收敛 aborted 终态（中止后进度悬挂）")
    # collector：合并错误明细时跳过 aborted 行（口径与旧路径一致）
    if 'data.get("aborted")' not in src_pool:
        bad.append("collector 未跳过中止行（跳过明细口径与旧路径不一致）")
    # collector：任务级 error 终态（worker 崩溃 ≠ 静默 done）
    if 'store.set_status(task_id, "error"' not in src_pool:
        bad.append("collector 缺少任务级 error 终态（worker 崩溃将静默标 done）")
    # W6：错误明细并入 _scan_skip_log（/api/scan_end 汇总口径与旧路径一致）
    if "_scan_skip_log" not in src_pool:
        bad.append("collector 未把错误行并入 _scan_skip_log（W6 未修复）")
    # 旧接口 abort 增强：必须同步中止所有进行中的批量任务
    if "abort_all_running" not in read("App/AppOrch.py"):
        bad.append("abort() 未调用 abort_all_running（旧接口中止对 worker 无效）")
    if bad:
        failures.extend(f"终止语义: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ⑤ 终止语义: {b}")
    else:
        print("[PASS] ⑤ 终止语义: 跨进程 is_aborted + aborted 落库 + queued 不取消，"
              "collector 跳过中止行 + error 终态 + 错误并入 _scan_skip_log")


# ═══════════════════════════════════════════════════════════════════════
# ⑥ 前端轮询契约（W1/W5/W15）
# ═══════════════════════════════════════════════════════════════════════
def test_frontend_polling(failures):
    src = read("Frontend/app.js")
    bad = []
    if "function _asyncScanAll(" not in src:
        bad.append("_asyncScanAll 函数缺失（批量扫描异步入口）")
    if 'fetch("/api/scan/submit"' not in src:
        bad.append("未提交至 POST /api/scan/submit")
    if "/api/scan/status?task_id=" not in src:
        bad.append("未轮询 /api/scan/status")
    # W1：since 按 row.seq + 1 推进（增量含首行）
    if "since = Math.max(since, rows[i].seq + 1)" not in src:
        bad.append("since 未按 row.seq + 1 推进（增量语义破坏，存在漏读/重读）")
    # W15：保留旧回调形状 onData/onDone（渲染零漂移）
    if "onData(rows[i].data || rows[i])" not in src:
        bad.append("未按 onData 逐票增量喂入（W15 契约破坏）")
    if "onDone(err || null, interrupted)" not in src:
        bad.append("未按 onDone(err, interrupted) 终态回调（W15 契约破坏）")
    # 终态三态停轮询
    if 'st.status === "done" || st.status === "aborted" || st.status === "error"' not in src:
        bad.append("终态判定缺失（done/aborted/error 三态停轮询）")
    # W5：轮询失败熔断（3 次退避重试）
    if "failCount >= 3" not in src:
        bad.append("轮询连续失败熔断缺失（单次网络抖动即废弃整个扫描）")
    # 三处调用点全部走异步径
    n_calls = len(re.findall(r"_asyncScanAll\(stocks", src))
    if n_calls < 3:
        bad.append(f"批量扫描调用点仅 {n_calls} 处（应 3 处：买卖点/底分型/均线分类全部走异步径）")
    # 旧 /api/scan_one 客户端并发循环清零
    if 'fetch("/api/scan_one' in src:
        bad.append("旧 /api/scan_one 客户端并发循环残留（应清零）")
    # 中止立即传播：点击中止必须同步调用 /api/scan/cancel
    if "scan/cancel" not in src:
        bad.append("缺少 /api/scan/cancel 调用（中止无法传播到 worker）")
    # 缓存击穿（合并自对方交付）：index.html 版本号必须 ≥8
    html = read("Frontend/index.html")
    m = re.search(r"app\.js\?v=(\d+)", html)
    if not m:
        bad.append("index.html 缺少 app.js?v=N 引用")
    elif int(m.group(1)) < 8:
        bad.append(f"index.html app.js?v={m.group(1)}（阶段 7 前端已改，版本号应 ≥8）")
    if bad:
        failures.extend(f"前端轮询: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ⑥ 前端轮询契约: {b}")
    else:
        print(f"[PASS] ⑥ 前端轮询契约: 提交 + 增量轮询（seq+1 推进）+ 三态终判 + "
              f"3 次熔断 + onData/onDone 回调；{n_calls} 处调用点全异步，旧 scan_one "
              f"并发循环清零；index.html 缓存版本 v={m.group(1)}（击穿在位）")


# ═══════════════════════════════════════════════════════════════════════
# ⑦ 功能冒烟（存储层 + 入口参数校验）
# ═══════════════════════════════════════════════════════════════════════
def test_store_smoke(failures):
    import sqlite3
    from App.ScanStore import ScanStore

    db = os.path.join(_SMOKE_TMPDIR, "store_smoke.db")
    if os.path.exists(db):
        os.remove(db)
    store = ScanStore(db_path=db, timeout=5.0)
    bad = []

    # 建任务 → completed 派生 0
    store.create_task("t1", total=3, params={"mode": "fx_d"})
    task = store.get_task("t1")
    if not task or task["total"] != 3 or task["completed"] != 0 or task["status"] != "pending":
        bad.append(f"create_task/get_task 形状异常: {task}")

    # 写 3 行 → completed 收敛 3；since=0 含首行（>= 语义）
    store.put_result("t1", 0, "000001", "ok", {"n": 1})
    store.put_result("t1", 1, "000002", "error", {"error": "测试错误"})
    store.put_result("t1", 2, "000003", "aborted", {"error": "扫描已终止", "aborted": True})
    task = store.get_task("t1")
    if task["completed"] != 3:
        bad.append(f"completed 未收敛: {task['completed']} != 3")
    rows0 = store.get_results("t1", since=0)
    if [r["seq"] for r in rows0] != [0, 1, 2]:
        bad.append(f"since=0 应返回全部 3 行（含首行）: {[r['seq'] for r in rows0]}")
    rows2 = store.get_results("t1", since=2)
    if [r["seq"] for r in rows2] != [2]:
        bad.append(f"since=2 增量语义异常: {[r['seq'] for r in rows2]}")
    if rows0[0]["data"].get("n") != 1:
        bad.append("result JSON 未正确解析回 data")

    # 同 seq 重复写 → 不覆盖不重复（collector 兜底幂等）
    store.put_result("t1", 1, "000002", "ok", {"n": 99})
    if store.get_task("t1")["completed"] != 3:
        bad.append("同 seq 重复写入导致行数漂移（幂等性破坏）")
    if store.get_results("t1", since=1)[0]["data"].get("error") != "测试错误":
        bad.append("重复写入覆盖了 worker 已写行（应 IGNORE）")

    # 错误/中止行汇总（collector 合并口径）
    errs = store.iter_error_rows("t1")
    if sorted(r["status"] for r in errs) != ["aborted", "error"]:
        bad.append(f"iter_error_rows 应只含非 ok 行: {errs}")

    # 终态 + WAL 生效性
    store.set_status("t1", "done")
    if store.get_task("t1")["status"] != "done":
        bad.append("set_status 未更新终态")
    with sqlite3.connect(db) as probe:
        mode = probe.execute("PRAGMA journal_mode").fetchone()[0]
    if str(mode).lower() != "wal":
        bad.append(f"journal_mode={mode}（WAL 未生效）")

    # 历史清理（keep_seconds=0 → 全清，验证删除链路）
    store.cleanup_old(keep_seconds=0)
    if store.get_task("t1") is not None:
        bad.append("cleanup_old 未清理过期任务")

    # 旧 schema 迁移（阶段 7 初版：含 done 列 / 结果主键 (task_id, code)）：
    # 必须无异常重建（曾出现 duplicate column name / no such column: seq 双 bug）
    old_db = os.path.join(_SMOKE_TMPDIR, "store_old_schema.db")
    if os.path.exists(old_db):
        os.remove(old_db)
    old_conn = sqlite3.connect(old_db)
    old_conn.executescript(
        "CREATE TABLE scan_tasks (task_id TEXT PRIMARY KEY, status TEXT, "
        "total INTEGER, created_at REAL, updated_at REAL, error TEXT, "
        "params_json TEXT, done INTEGER NOT NULL DEFAULT 0);"
        "CREATE TABLE scan_results (task_id TEXT NOT NULL, code TEXT NOT NULL, "
        "status TEXT, result_json TEXT, PRIMARY KEY (task_id, code));")
    old_conn.commit()
    old_conn.close()
    try:
        mig = ScanStore(db_path=old_db, timeout=5.0)
        mig_cols = [r[1] for r in sqlite3.connect(old_db).execute(
            "PRAGMA table_info(scan_tasks)").fetchall()]
        if "abort_requested" not in mig_cols or "done" in mig_cols:
            bad.append(f"旧库迁移后列异常: {mig_cols}")
        # 迁移后读写正常（completed 由 COUNT 派生）
        mig.create_task("m1", 2)
        mig.put_result("m1", 0, "000001", "ok", {"n": 1})
        mig.put_result("m1", 1, "000002", "error", {"error": "x"})
        if mig.get_task("m1")["completed"] != 2:
            bad.append("旧库迁移后 completed 派生异常")
    except Exception as exc:  # noqa: BLE001
        bad.append(f"旧库迁移抛异常: {type(exc).__name__}: {exc}")

    # 入口参数校验（空清单 / 不存在任务）
    from App import AppOrch as orch
    r = orch.scanner.submit_batch_scan([], freq="d")
    if "error" not in r:
        bad.append(f"空清单应报错: {r}")
    r = orch.scanner.get_batch_scan_status("no-such-task", since=0)
    if r is not None:
        bad.append(f"不存在任务应返回 None: {r}")

    if bad:
        failures.extend(f"存储冒烟: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ⑦ 功能冒烟: {b}")
    else:
        print("[PASS] ⑦ 功能冒烟: CRUD/增量语义/幂等/completed 收敛/错误汇总/"
              "WAL/历史清理/入口校验全部通过")


# ═══════════════════════════════════════════════════════════════════════
# ⑧ 端到端冒烟（真实 ProcessPool → 跨进程 SQLite 回流）
# ═══════════════════════════════════════════════════════════════════════
def test_e2e_smoke(failures):
    from App import AppOrch as orch

    stocks = [{"code": "ZZ%04d" % i, "prefix": "1", "_source": "zxg"}
              for i in range(3)]
    sub = orch.scanner.submit_batch_scan(stocks, freq="d", mode="", recent="1")
    if "error" in sub:
        failures.append(f"端到端: 提交失败: {sub}")
        print(f"[FAIL] ⑧ 端到端冒烟: 提交失败 {sub}")
        return
    task_id, total = sub["task_id"], sub["total"]
    if sub.get("engine") not in ("process_pool", "thread_fallback"):
        failures.append(f"端到端: engine 标记异常: {sub.get('engine')}")
        print(f"[FAIL] ⑧ 端到端冒烟: engine={sub.get('engine')}")
        return

    deadline = time.time() + 150
    since, seen, completed_max = 0, [], 0
    while time.time() < deadline:
        st = orch.scanner.get_batch_scan_status(task_id, since=since)
        if st is None:
            failures.append("端到端: 任务不存在")
            break
        for row in st["results"]:
            since = max(since, row["seq"] + 1)
            seen.append(row["seq"])
        completed_max = max(completed_max, st["completed"])
        if st["status"] in ("done", "aborted", "error"):
            break
        time.sleep(0.5)

    bad = []
    final = orch.scanner.get_batch_scan_status(task_id, since=0)
    if final is None or final.get("status") not in ("done", "aborted", "error"):
        bad.append(f"终态异常: {final.get('status') if final else None}（150s 内未收敛）")
    if final.get("completed") != total:
        bad.append(f"completed={final.get('completed')} != total={total}（进度悬挂）")
    if final.get("completed", 0) < completed_max:
        bad.append("completed 出现回退（非单调收敛）")
    full = [r["seq"] for r in final.get("results", [])]
    if sorted(full) != list(range(total)):
        bad.append(f"结果行 seq 不连续: {sorted(full)} != {list(range(total))}")
    shapes = all(isinstance(r.get("data"), dict) for r in final.get("results", []))
    if not shapes:
        bad.append("存在非 dict 结果（响应形状契约破坏）")
    from App.ScanPool import shutdown as _pool_shutdown
    _pool_shutdown()

    if bad:
        failures.extend(f"端到端: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ⑧ 端到端冒烟: {b}")
    else:
        print(f"[PASS] ⑧ 端到端冒烟: engine={sub['engine']}，{total} 只虚拟票 "
              f"completed 收敛 {final['completed']}，seq 0..{total - 1} 连续回流，"
              f"跨进程 SQLite 链路真实可用")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--update", action="store_true",
                    help="兼容参数：本守护为契约校验，无待更新快照")
    args = ap.parse_args()

    print("===== 阶段 7 成果防护：批量扫描异步化（设计 5.4/5.5/5.7/5.10/8.10） =====")
    failures = []
    test_lock_policy(failures)
    test_routes_funnel(failures)
    test_pool_assembly(failures)
    test_store_contract(failures)
    test_abort_semantics(failures)
    test_frontend_polling(failures)
    test_store_smoke(failures)
    test_e2e_smoke(failures)
    print("-" * 64)
    if failures:
        for f in failures:
            print(f"[FAIL] {f}")
        print("===== 阶段 7 成果防护: 失败 =====")
        return 1
    print("===== 阶段 7 成果防护: 全部通过（8 类守护） =====")
    return 0


if __name__ == "__main__":
    sys.exit(main())
