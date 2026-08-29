# -*- coding: utf-8 -*-
"""
App/AppScanPool.py —— 批量扫描 ProcessPool 编排
=========================================================================
  - 双路径：交互请求走线程池快速路径（API 进程内），批量扫描走
    ProcessPool 进程池路径（多进程 worker），两者共享同一套 service 层
    函数（统一函数，分离通道）。
  - 批量路径先用 ProcessPoolExecutor（Python 标准库，零额外依赖、
    零运维成本）；升级到 Celery 的条件：优先级队列 / 持久化 /
    超时严重 / 分布式。
  - 共享缓存分层：扫描结果（task_id → 结果）经 SQLite
    AppScanStore 跨进程共享（worker 写、API 读）；K 线等交互数据仍在
    API 进程内字典，不跨进程。

模块定位：
  - 本文件是「入口适配器」：提交到进程池的函数负责参数序列化、调用
    service、返回结果，本身不含业务逻辑。
  - App/AppOrch.py 保持纯业务、零并发框架依赖（模块级不 import
    concurrent.futures），Scanner 的批量入口以薄封装委托本文件。

进程模型：
  - 显式 spawn 上下文（multiprocessing.get_context("spawn")）：避免
    POSIX 默认 fork 在多线程 API 进程内的锁继承风险（Python 官方文档
    明示）；入口（main/api_server/FrontAPI）均已带 __main__ 守卫。
    仅用进程池：spawn 池装配是唯一路径，不提供线程降级——若受限容器
    （无 /dev/shm、seccomp 限制）装配失败则直接抛错，由运维解决环境
    问题而非静默降到同进程线程（后者会共享 _scan_lock，退化为进程内
    引擎调用串行）。
  - worker 数钳制 [1, 16]：防 64 核机器拉起 64 个独立引擎进程
    内存线性放大 OOM。
  - worker 函数为模块级函数（可 pickle），内部惰性 import AppOrch /
    AppScanStore，避免 spawn 模式下导入期副作用。
  - 中止：task 级中止经 AppScanStore 状态传播（worker 每票前检查
    is_aborted），不依赖进程内 _scan_aborted 标志。
  - 收割：collector 把 worker 错误行合并进 API 进程
    _m._scan_skip_log（/api/scan_end 汇总口径一致；中止行不计入）。
  - 生命周期：提交入口 best-effort 清理过期任务；
    atexit 注册显式关池（shutdown(wait=False, cancel_futures=True)）。
"""
import atexit
import multiprocessing
import os
import sys
import threading
from concurrent.futures import ProcessPoolExecutor
from App.AppLog import get_logger
log = get_logger(__name__)


# ── spawn 模式引导：确保仓库根在 sys.path（App 包可导入）─────────────
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

# worker 数上限：防误配/高核数机器打爆内存
_SCAN_POOL_MAX_WORKERS = 16

_pool = None
_pool_engine = None          # "process_pool"
_pool_lock = threading.Lock()
_pool_created_count = 0      # 池装配次数（区分首次/重新装配，用于打印）
_active_scans = 0            # 并发批次引用计数：>0 时禁止销毁池（防互毁）


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
    """惰性创建全局执行池（单例）：ProcessPool(spawn)。

    仅用进程池——spawn 池装配是唯一路径，失败即抛错（不降级线程池，
    专注暴露并解决环境问题）。scan_one 在各 worker 进程内执行，每进程
    独立 _scan_lock 与引擎缓存，互不共享。

    即用即弃：扫描完成即销毁（destroy_pool），故此处每次装配都是
    全新进程、空缓存；打印区分首次装配与重新装配，便于确认每次扫描
    状态已恢复初始。
    """
    global _pool, _pool_engine, _pool_created_count, _active_scans
    with _pool_lock:
        if _pool is None:
            from App.AppConfig import app_config
            workers = _resolve_workers(app_config)
            ctx = multiprocessing.get_context("spawn")
            pool = ProcessPoolExecutor(max_workers=workers,
                                       mp_context=ctx,
                                       initializer=_worker_init)
            # 构造成功后才计数：装配失败（如受限容器无 /dev/shm）保持
            # _pool=None 且不虚增计数，下次扫描可原样重试自愈。
            _pool_created_count += 1
            _pool = pool
            _pool_engine = "process_pool"
            if _pool_created_count == 1:
                log.info(f"[扫描池] ProcessPool 已装配: workers={workers} "
                      f"(spawn)")
            else:
                log.info(f"[扫描池] 重新装配（上次已销毁）: workers={workers} "
                      f"(spawn)")
        # 在本锁内成立引用（与池装配原子）：保证拿到池的同时登记一个
        # 进行中批次，后续其它批次也并发持有 → 任一批次都不该在还有人
        # 用时销毁池（修复两批并发扫描时先完成者 cancel 掉后者队列）。
        _active_scans += 1
        return _pool, _pool_engine


def _resolve_workers(app_config):
    """worker 数解析：scan_pool_workers > 0 用之；否则按 CPU 核数自动适配
    （os.cpu_count()，至少 1）。钳制上限 [1, 16]（防高核数机器内存线性
    放大 OOM）。默认即自动适配当前机器核数，换电脑无需改配置。
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
        log.info(_line)
    return result


def _monitor_task(task_id, futures):
    """后台收割线程：等待全部 future 完成 → 合并错误明细 → 终态标记。

    - future 异常（worker 崩溃/序列化失败）时兜底写错误行，保证
      completed 收敛到 total（前端进度不悬挂）；
    - 错误明细合并进 API 进程 _m._scan_skip_log（/api/scan_end 的汇总
      打印口径一致；中止行不计入）；
    - 终态：done / aborted / error（任一 future 以异常收场＝基础设施级
      故障，任务标 error 而非 done，前端可区分并向用户提示）。
    """
    # —— 整个收割体包进外层 try/finally：无论 get_scan_store（首次访问会
    # 建库/建表，可能抛异常）还是后续任一步 store 调用抛异常，都经 finally
    # 归还池引用，杜绝「收割线程带未归还引用死亡 → 池永不销毁」。
    try:
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

        # 错误明细并入 _scan_skip_log（中止行不计入）
        try:
            from App.AppScan import _scan_skip_log
            for row in store.iter_error_rows(task_id):
                data = row.get("data") or {}
                if isinstance(data, dict) and data.get("aborted"):
                    continue
                msg = str(data.get("error", "")) if isinstance(data, dict) else ""
                _scan_skip_log.append(
                    f"{row.get('code', '')} - {msg or row.get('status', '')}")
        except Exception:  # noqa: BLE001
            pass

        # 终态标记：task 缺失时跳过落库（不早退 return）
        try:
            task = store.get_task(task_id)
            if task is not None:
                # 中止收敛：请求旗已置（或旧库残留 aborted 终态）→ 全部 future 完成后
                # 才置 aborted 终态，保证中止后 completed 收敛 total（queued 不悬挂）
                if task.get("abort_requested") or task["status"] == "aborted":
                    store.set_status(task_id, "aborted")
                elif crashed:
                    store.set_status(task_id, "error", "; ".join(crash_msgs[:5]))
                else:
                    store.set_status(task_id, "done")
        except Exception:  # noqa: BLE001 —— 终态落库失败不阻断引用归还（外层 finally 兜底）
            pass
    finally:
        # 无论如何都归还池引用（即用即弃）：本批 future 已全部结束。仅当全局
        # 无其它进行中批次（并发批量扫描）时才真正销毁进程池（worker 缓存
        # 释放、下次重 new spawn 全新进程、空缓存）；否则保留池供尚在运行的当批，
        # 以免先完成者 cancel_futures 取消后者队列中的票。
        # 修复：先前 task is None 走 return 早退、且无 try/finally，get_task/
        # set_status 抛异常会让收割线程带未归还引用死亡 → 池永不销毁。现无论
        # task 是否存在、是否异常，都经 finally 归还引用。
        _release_scan()


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

    # 提交入口 best-effort 清理过期任务
    try:
        store.cleanup_old(keep_seconds=7 * 86400)
    except Exception:  # noqa: BLE001 —— 清理失败不阻断主流程
        pass

    # 时间戳前缀 task_id：生成逻辑收敛到 create_task 内部
    # （缺省自生成，可读可排序），调用方零心智负担
    total = len(valid)
    task_id = store.create_task(total=total, params={
        "freq": freq, "mode": mode, "recent": recent,
        "source": source, "count": total,
    })

    # 池装配：失败即置任务 error 并向调用方返回，绝不把异常带出去。
    # （ProcessPoolExecutor 构造通常成功，受限容器典型失败发生在首次
    #   submit 期抛 BrokenProcessPool；故装配与派发分两层各自收敛。）
    try:
        pool, engine = _get_pool()
    except Exception as exc:  # noqa: BLE001 —— 收敛为任务级错误
        msg = f"扫描进程池装配失败: {type(exc).__name__}: {exc}"
        log.error(f"[扫描池] {msg}")
        store.set_status(task_id, "error", msg)
        return {"error": msg}

    workers = _resolve_workers(_get_config())
    # 启动扫描前打印实际执行核数（worker 数 = 并行执行的进程数；先显示 CPU 核数，执行核数基于它）
    log.info(f"[扫描] 启动批量扫描: 共{total}只 | CPU核数={os.cpu_count()} | "
          f"执行核数={workers} | 引擎={engine}")
    futures = []
    try:
        for seq, stk in enumerate(valid):
            code = stk.get("code", "")
            prefix = stk.get("prefix", "")
            src = stk.get("_source") or source
            fut = pool.submit(_worker_scan_one, task_id, code, freq, prefix,
                              recent, src, mode, seq)
            futures.append((fut, seq, code))
    except Exception as exc:  # noqa: BLE001 —— 派发期失败需销毁坏池自愈
        # 受限容器典型失败：队列创建失败 → 首次 submit 抛 BrokenProcessPool。
        # 必须销毁坏池（_active_scans 归零）+ 置任务 error，否则坏池被
        # 永久复用、_active_scans 泄漏，批量扫描再无自愈机会、任务永挂。
        # 已知取舍（有意为之，勿当 bug）：若此刻另有批次共用同一坏池，
        # 其 futures 也会被 destroy_pool() 一并取消并标 error——因池已损坏、
        # 那批本就必然失败，强销毁换来下次扫描可重新装配自愈。
        msg = f"扫描任务派发失败: {type(exc).__name__}: {exc}"
        log.error(f"[扫描池] {msg}")
        destroy_pool()
        store.set_status(task_id, "error", msg)
        return {"error": msg}

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
    total（中止后 completed 收敛语义）。
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


def _release_scan():
    """任务收割后归还池引用；仅当无任何进行中批次时销毁池（幂等）。

    修复前：每批扫描收割完直接 destroy_pool()。当两批扫描并发共享同一
    单例池时，先完成的那批会 shutdown(cancel_futures=True)，把另一批
    仍在队列的 future 取消，导致后者票被记 error（并发互毁）。改为
    引用计数：池在用期间（_active_scans>0）一律不销毁，直到最后一批
    完成才真正 shutdown。
    """
    global _pool, _pool_engine, _active_scans
    with _pool_lock:
        _active_scans = max(0, _active_scans - 1)
        if _active_scans == 0 and _pool is not None:
            try:
                _pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 —— 关池失败不阻断主流程
                pass
            _pool = None
            _pool_engine = None
            log.info("[扫描池] 进程池已销毁（下次扫描将重新装配，worker 缓存已释放）")


def destroy_pool():
    """销毁执行池（幂等）：释放 worker 进程与缓存。

    当前生产路径不再直接调用（扫描归还经 _release_scan，引用计数为 0 时才
    销毁），本函数保留供外部/测试显式销毁兜底。同步把 _active_scans 归零，
    避免显式销毁后计数与实际池状态脱钩（否则后续再扫描将永远无法销毁池）。
    """
    global _pool, _pool_engine, _active_scans
    with _pool_lock:
        _active_scans = 0
        if _pool is not None:
            try:
                _pool.shutdown(wait=False, cancel_futures=True)
            except Exception:  # noqa: BLE001 —— 关池失败不阻断主流程
                pass
            _pool = None
            _pool_engine = None
            log.info("[扫描池] 任务完成，销毁进程池（worker 缓存已释放）")


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
