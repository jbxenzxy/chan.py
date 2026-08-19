# -*- coding: utf-8 -*-
"""
App/AppScanPool.py —— 批量扫描 ProcessPool 编排（阶段 7）
=========================================================================
设计文档 V10 方案 5.4 / 5.5 / 5.7 / 5.8 / 8.10：

  - 双路径：交互请求走线程池快速路径（API 进程内），批量扫描走
    ProcessPool 进程池路径（多进程 worker），两者共享同一套 service 层
    函数（统一函数，分离通道）。
  - 批量路径先用 ProcessPoolExecutor（Python 标准库，零额外依赖、
    零运维成本）；升级到 Celery 需满足 5.7 决策条件（优先级队列 /
    持久化 / 超时严重 / 分布式）。
  - 共享缓存分层（5.10）：扫描结果（task_id → 结果）经 SQLite
    AppScanStore 跨进程共享（worker 写、API 读）；K 线等交互数据仍在
    API 进程内字典，不跨进程。

模块定位（4.5）：
  - 本文件是「入口适配器」：提交到进程池的函数负责参数序列化、调用
    service、返回结果，本身不含业务逻辑。
  - App/AppOrch.py 保持纯业务、零并发框架依赖（模块级不 import
    concurrent.futures），Scanner 的批量入口以薄封装委托本文件。

进程模型（交叉评审 W3/W4 修复）：
  - 显式 spawn 上下文（multiprocessing.get_context("spawn")）：避免
    POSIX 默认 fork 在多线程 API 进程内的锁继承风险（Python 官方文档
    明示）；入口（main/api_server/FrontAPI）均已带 __main__ 守卫。
  - 降级路径：spawn 池创建失败（受限容器无 /dev/shm、seccomp 限制等）
    时自动降级 ThreadPoolExecutor 执行同一 worker 函数——scan_one 内
    _scan_lock 自动回归「引擎串行、锁外并发」旧语义；提交响应带
    engine: process_pool | thread_fallback 供前端标示。
  - worker 数钳制 [1, 16]（W10）：防 64 核机器拉起 64 个独立引擎进程
    内存线性放大 OOM。
  - worker 函数为模块级函数（可 pickle），内部惰性 import AppOrch /
    AppScanStore，避免 spawn 模式下导入期副作用。
  - 中止：task 级中止经 AppScanStore 状态传播（worker 每票前检查
    is_aborted），不依赖进程内 _scan_aborted 标志。
  - 收割（W6 修复）：collector 把 worker 错误行合并进 API 进程
    _m._scan_skip_log（/api/scan_end 汇总口径与旧路径一致；中止行不计入）。
  - 生命周期（W7/W11 修复）：提交入口 best-effort 清理历史任务；
    atexit 注册显式关池（shutdown(wait=False, cancel_futures=True)）。
"""
import atexit
import multiprocessing
import os
import sys
import threading
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor

# ── spawn 模式引导：确保仓库根在 sys.path（App 包可导入）─────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# worker 数上限（W10 修复）：防误配/高核数机器打爆内存
_SCAN_POOL_MAX_WORKERS = 16
# 线程降级模式的并发上限（对齐旧前端并发量级，避免线程池内引擎锁竞争）
_SCAN_POOL_FALLBACK_WORKERS = 4

_pool = None
_pool_engine = None          # "process_pool" | "thread_fallback"
_pool_lock = threading.Lock()


def _worker_init():
    """worker 进程初始化：屏蔽 SIGINT。

    Windows 上 Ctrl+C 信号会传播到进程组内所有进程（含 ProcessPool
    worker）。worker 在 call_queue.get() 阻塞时直接收到 KeyboardInterrupt
    并打印 traceback（实测 SpawnProcess-1）。主进程退出经 shutdown()
    通过 call_queue sentinel 通知 worker 退出，无需 SIGINT。
    """
    try:
        import signal
        signal.signal(signal.SIGINT, signal.SIG_IGN)
    except Exception:  # noqa: BLE001 —— 屏蔽失败不阻断 worker 启动
        pass


def _get_pool():
    """惰性创建全局执行池（单例）：ProcessPool(spawn) 优先，失败降级 ThreadPool。

    返回 (executor, engine_tag)。降级模式下同一 worker 函数在 API 进程内
    执行：scan_one 的 _scan_lock 自动恢复旧串行语义（W3 修复）。
    """
    global _pool, _pool_engine
    with _pool_lock:
        if _pool is None:
            from App.AppConfig import app_config
            workers = _resolve_workers(app_config)
            try:
                ctx = multiprocessing.get_context("spawn")
                _pool = ProcessPoolExecutor(max_workers=workers,
                                            mp_context=ctx,
                                            initializer=_worker_init)
                _pool_engine = "process_pool"
                print(f"[扫描池] ProcessPool 已装配: workers={workers} "
                      f"(spawn)", flush=True)
            except Exception as exc:  # noqa: BLE001 —— 受限环境降级线程池
                print(f"[扫描池] ProcessPool 装配失败，降级线程池: "
                      f"{type(exc).__name__}: {exc}", flush=True)
                _pool = ThreadPoolExecutor(
                    max_workers=min(workers, _SCAN_POOL_FALLBACK_WORKERS),
                    thread_name_prefix="scan-fallback")
                _pool_engine = "thread_fallback"
        return _pool, _pool_engine


def _resolve_workers(app_config):
    """worker 数解析：scan_pool_workers > 0 用之；否则按 CPU 核数自动适配
    （os.cpu_count()，至少 1）。钳制上限 [1, 16]（W10 修复：防高核数机器
    内存线性放大 OOM）。

    根因修复（阶段 8 后续）：此前回退链含 scan_concurrency（阶段 7 前
    「前端并发提示」遗留字段，默认 1），被错误复用作 worker 数回退目标，
    导致默认配置下 worker 恒为 1、CPU 自动适配永远走不到——进程池退化为
    单核，与 v1.0 的 _scan_lock 全局串行等价。现移除该参与，默认即自动
    适配当前机器核数，换电脑无需改配置。
    """
    workers = getattr(app_config, "scan_pool_workers", 0) or 0
    if workers <= 0:
        workers = os.cpu_count() or 2
    return max(1, min(_SCAN_POOL_MAX_WORKERS, workers))


def _worker_scan_one(task_id, code, freq, prefix, recent, source, mode, seq):
    """执行池 worker 函数：调用统一业务函数 scanner.scan_one，写结果到 SQLite。

    ⚠ 必须为模块级函数（可 pickle）。内部惰性 import，避免 spawn 导入期副作用。
    每票前检查任务中止标志（跨进程传播），不依赖进程内 _scan_aborted。
    """
    from App.AppScanStore import get_scan_store
    store = get_scan_store()

    if store.is_aborted(task_id):
        store.put_result(task_id, seq, code, "aborted",
                         {"code": code, "error": "扫描已终止", "aborted": True})
        return {"code": code, "error": "扫描已终止", "aborted": True}

    try:
        from App.AppOrch import scanner
        result = scanner.scan_one(code, freq=freq, prefix=prefix,
                                  recent=recent, source=source, mode=mode)
        if not isinstance(result, dict):
            result = {"code": code, "error": f"非字典结果: {type(result).__name__}"}
    except Exception as exc:  # noqa: BLE001 —— worker 兜底，保证 completed 收敛
        result = {"code": code, "error": f"{type(exc).__name__}: {exc}"}

    status = "error" if "error" in result else "ok"
    if status == "error" and isinstance(result, dict) and result.get("aborted"):
        status = "aborted"
    try:
        store.put_result(task_id, seq, code, status, result)
    except Exception as _e:  # noqa: BLE001 —— 结果落库失败不阻断扫描，但记录缺口
        _line = f"[扫描池落库失败] task={task_id} seq={seq} {code} {type(_e).__name__}: {_e}"
        print(_line, flush=True)
    return result


def _monitor_task(task_id, futures):
    """后台收割线程：等待全部 future 完成 → 合并错误明细 → 终态标记。

    - future 异常（worker 崩溃/序列化失败）时兜底写错误行，保证
      completed 收敛到 total（前端进度不悬挂）；
    - 错误明细合并进 API 进程 _m._scan_skip_log（/api/scan_end 的汇总
      打印口径与旧路径一致；中止行不计入，对齐旧行为）（W6 修复）；
    - 终态：done / aborted / error（任一 future 以异常收场＝基础设施级
      故障，任务标 error 而非 done，前端可区分并向用户提示）。
    """
    from App.AppScanStore import get_scan_store
    store = get_scan_store()
    crashed = 0
    crash_msgs = []
    for fut, seq, code in futures:
        try:
            fut.result()
        except Exception as exc:  # noqa: BLE001
            crashed += 1
            crash_msgs.append(f"{type(exc).__name__}: {exc}")
            try:
                store.put_result(task_id, seq, code, "error",
                                 {"code": code,
                                  "error": f"{type(exc).__name__}: {exc}"})
            except Exception:  # noqa: BLE001
                pass

    # 错误明细并入 _scan_skip_log（中止行不计入，对齐旧路径口径）
    try:
        from App.AppEngine import _scan_skip_log
        for row in store.iter_error_rows(task_id):
            data = row.get("data") or {}
            if isinstance(data, dict) and data.get("aborted"):
                continue
            msg = str(data.get("error", "")) if isinstance(data, dict) else ""
            _scan_skip_log.append(
                f"{row.get('code', '')} - {msg or row.get('status', '')}")
    except Exception:  # noqa: BLE001
        pass

    task = store.get_task(task_id)
    if task is None:
        return

    # 中止收敛：请求旗已置（或旧库残留 aborted 终态）→ 全部 future 完成后
    # 才置 aborted 终态，保证中止后 completed 收敛 total（queued 不悬挂）
    if task.get("abort_requested") or task["status"] == "aborted":
        store.set_status(task_id, "aborted")
        return
    if crashed:
        store.set_status(task_id, "error", "; ".join(crash_msgs[:5]))
    else:
        store.set_status(task_id, "done")


def submit_batch_scan(stocks, freq="d", mode="", recent="1", source="zxg"):
    """提交批量扫描 → task_id（薄封装入口，AppOrch.Scanner 委托）。

    stocks: [{code, prefix, _source}, ...]（来自 scan_stock_list 的合并列表）
    返回: {task_id, total, workers, engine}。任务异步在执行池中执行，
    进度经 get_status(task_id, since) 轮询。
    """
    from App.AppScanStore import get_scan_store
    store = get_scan_store()

    valid = [s for s in (stocks or []) if s.get("code")]
    if not valid:
        return {"error": "股票列表为空"}

    # 提交入口 best-effort 清理历史任务（W7 修复：cleanup_old 不再为死代码）
    try:
        store.cleanup_old(keep_seconds=7 * 86400)
    except Exception:  # noqa: BLE001 —— 清理失败不阻断主流程
        pass

    # 时间戳前缀 task_id（W13 修复 + 评审「优势 5」采纳）：生成逻辑收敛到
    # create_task 内部（缺省自生成，格式保持可读可排序），调用方零心智负担
    total = len(valid)
    task_id = store.create_task(total=total, params={
        "freq": freq, "mode": mode, "recent": recent,
        "source": source, "count": total,
    })

    pool, engine = _get_pool()
    workers = _resolve_workers(_get_config())
    # 启动扫描前打印实际执行核数（worker 数 = 并行执行的进程数；先显示 CPU 核数，执行核数基于它）
    print(f"[扫描] 启动批量扫描: 共{total}只 | CPU核数={os.cpu_count()} | "
          f"执行核数={workers} | 引擎={engine}", flush=True)
    futures = []
    for seq, stk in enumerate(valid):
        code = stk.get("code", "")
        prefix = stk.get("prefix", "")
        src = stk.get("_source") or source
        fut = pool.submit(_worker_scan_one, task_id, code, freq, prefix,
                          recent, src, mode, seq)
        futures.append((fut, seq, code))

    store.set_status(task_id, "running")
    threading.Thread(target=_monitor_task, args=(task_id, futures),
                     daemon=True).start()
    return {"task_id": task_id, "total": total, "workers": workers,
            "engine": engine}


def _get_config():
    from App.AppConfig import app_config
    return app_config


def get_status(task_id, since=0):
    """前端轮询视图（委托 ScanStore，增量读取）"""
    from App.AppScanStore import get_scan_store
    return get_scan_store().get_status(task_id, since=since)


def abort(task_id):
    """中止任务：置 abort_requested 请求旗（worker 每票前检查）。

    不立即改 status——queued 票由 worker 快速落库 aborted 行，收割线程
    等全部 future 完成后才置 aborted 终态，保证中止后 completed 收敛
    total（交叉评审「中止后 completed 收敛」语义）。
    """
    from App.AppScanStore import get_scan_store
    store = get_scan_store()
    task = store.get_task(task_id)
    if task is None:
        return {"error": f"任务不存在: {task_id}"}
    if task["status"] in ("done", "aborted", "error"):
        return {"ok": True, "status": task["status"]}
    store.request_abort(task_id)
    return {"ok": True, "status": task["status"]}


def shutdown():
    """优雅关闭执行池（应用退出时 atexit 调用，幂等）"""
    global _pool, _pool_engine
    with _pool_lock:
        if _pool is not None:
            try:
                _pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001
                pass
            _pool = None
            _pool_engine = None


atexit.register(shutdown)
