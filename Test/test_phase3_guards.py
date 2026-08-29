# -*- coding: utf-8 -*-
"""
阶段 3：成果防护守护用例（3a REST 迁移 / 3b-1 SSE 双实现）
=====================================================================
守护阶段 3 的六类结构性成果（设计文档 V10 方案 8.6）：

  ① 锁分类建档（LOCK_POLICY 登记完备 + SERIAL 实现确实持锁 + RAW 不持锁）
  ② 直连引擎清零（FrontAPI/api_server 不再绕过 AppOrch 漏斗调引擎，
     阶段 2 遗留问题：原 api_server 3 处直连绕锁）
  ③ 路由收敛（api_server.py 已删除，31 条 REST/SSE 路由单源于 FrontAPI，
     命名为 RESTful 整理后的冻结基线，见 snapshots/phase3_routes.json）
  ④ 墓碑化（ChartHandler.do_GET/do_POST 已 410；SSE 方法保留至 3b-2）
  ⑤ SSE 双实现（impl=legacy|native 灰度开关，默认 legacy 零漂移；
     原生生成器 + CSSESource 数据源抽象就位）
  ⑥ 分层方向（FrontAPI → AppOrch → AppData 单向，禁止反向/跨层）

运行：python Test/test_phase3_guards.py           # 校验
      python Test/test_phase3_guards.py --update   # 重冻路由基线
"""
import argparse
import ast
import inspect
import io
import json
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    try:
        import typing_extensions
        typing.Self = typing_extensions.Self
    except ImportError:
        pass

SNAPSHOTS = os.path.join(TEST_DIR, "snapshots")
ROUTES_SNAPSHOT = os.path.join(SNAPSHOTS, "phase3_routes.json")

# 全量 RESTful 路由冻结基线（FrontAPI 单一路由源，命名经 API 整理统一）
EXPECTED_ROUTES = {
    ("GET", "/api/health"),
    ("GET", "/api/search"),
    ("GET", "/api/stocks/{code}/analyze"),
    ("GET", "/api/stocks/{code}/red-range"),
    ("GET", "/api/stocks/{code}/read/annotation"),
    ("POST", "/api/stocks/{code}/save/annotation"),
    ("POST", "/api/stocks/{code}/select/point"),
    ("DELETE", "/api/stocks/{code}/delete/point"),
    ("POST", "/api/stocks/refresh"),
    ("GET", "/api/stocks/refresh/read/status"),
    ("GET", "/api/stocks/scan/read/candidates"),
    ("PUT", "/api/stocks/scan/set/index"),
    ("POST", "/api/stocks/scan/start"),
    ("POST", "/api/stocks/scan/end"),
    ("POST", "/api/stocks/scan/close"),
    ("POST", "/api/stocks/scan/submit"),
    ("GET", "/api/stocks/scan/{task_id}/read/status"),
    ("POST", "/api/stocks/scan/{task_id}/cancel"),
    ("POST", "/api/stocks/scan/save/zxg"),
    ("GET", "/api/stocks/scan/annotation"),
    ("POST", "/api/stocks/download/start"),
    ("GET", "/api/stocks/download/read/status"),
    ("POST", "/api/stocks/download/cancel"),
    ("GET", "/api/futures/read/stream"),
    ("POST", "/api/futures/cleanup"),
    ("POST", "/api/futures/{symbol}/select/point"),
    ("DELETE", "/api/futures/{symbol}/delete/point"),
}

# REST 路由禁止直连的引擎原始入口（锁分类 SERIAL 对应的底层函数；
# 调用必须经 AppOrch.call_* 漏斗，见 LOCK_POLICY）
# D7：_analyze_futures_internal 已删除（生产零调用方），守护条目随之移除
FORBIDDEN_ENGINE_CALLS = [
    "m.analyze_stock(",
    "_m.analyze_stock(",
    "m.stock_manual_select_point(",
    "_m.stock_manual_select_point(",
    "m.futures_manual_select_point(",
    "_m.futures_manual_select_point(",
    "m.compute_red_range_zs(",
    "_m.compute_red_range_zs(",
]


def read_src(rel):
    with io.open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════════════
# ① 锁分类建档
# ═══════════════════════════════════════════════════════════════════════
def test_lock_policy(failures):
    """LOCK_POLICY 登记完备 + engine_section 可执行化（P1-2）：
    SERIAL 漏斗实现须经 engine_section("<入口>") 取锁，RAW 不取锁。"""
    from App import AppOrch as orch

    required_keys = {
        "call_analysis", "call_manual_select_point",
        "call_futures_manual_select_point", "call_compute_red_range_zs",
        "analyze_stock", "Scanner.scan_one",
        "sse_futures_stream_single", "sse_futures_stream_dual",
    }
    missing = required_keys - set(orch.LOCK_POLICY)
    if missing:
        failures.append(f"锁分类: LOCK_POLICY 缺少登记 {sorted(missing)}")
        print(f"[FAIL] ① 锁分类: 缺少 {sorted(missing)}")
        return

    # 每个类别必须出现的取值域（新增类别需同步更新守护）
    # 阶段 7 新增 SCAN_ASYNC（批量扫描异步路径：API 进程零持锁）
    for key, (cat, _desc) in orch.LOCK_POLICY.items():
        if cat not in ("SERIAL", "SCAN", "SCAN_ASYNC", "SELF_CONTAINED", "RAW"):
            failures.append(f"锁分类: {key} 类别 {cat!r} 不在登记域内")
            print(f"[FAIL] ① 锁分类: {key} 类别 {cat!r} 非法")

    # SERIAL 入口（AppOrch 层函数）实现必须经 engine_section 取锁；
    # RAW 必须不取锁。P1-2：校验「登记即执行」，不再只是登记文本匹配。
    fn_map = {
        "call_analysis": orch.call_analysis,
        "call_manual_select_point": orch.call_manual_select_point,
        "call_futures_manual_select_point": orch.call_futures_manual_select_point,
        "call_compute_red_range_zs": orch.call_compute_red_range_zs,
        "call_amo": orch.call_amo,
        "analyze_stock": orch.analyze_stock,
    }
    bad = []
    for name, fn in fn_map.items():
        cat = orch.LOCK_POLICY[name][0]
        src = inspect.getsource(fn)
        uses_section = f'engine_section("{name}")' in src
        holds_raw_lock = "with _ENGINE_LOCK:" in src
        if cat == "SERIAL":
            if not uses_section:
                bad.append(f"{name} 登记 SERIAL 但实现未经 engine_section(\"{name}\") 取锁")
            if holds_raw_lock:
                bad.append(f"{name} 残留手写 with _ENGINE_LOCK:（应统一走 engine_section）")
        if cat == "RAW" and (uses_section or holds_raw_lock):
            bad.append(f"{name} 登记 RAW 但实现取锁（与登记矛盾）")

    if bad:
        failures.extend(f"锁分类: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ① 锁分类: {b}")
    else:
        n_serial = sum(1 for c, _ in orch.LOCK_POLICY.values() if c == "SERIAL")
        print(f"[PASS] ① 锁分类: {len(orch.LOCK_POLICY)} 项登记，"
              f"{n_serial} 个 SERIAL 漏斗均经 engine_section 取锁，RAW 无一取锁")


# ═══════════════════════════════════════════════════════════════════════
# ② 直连引擎清零 + ③ 路由收敛
# ═══════════════════════════════════════════════════════════════════════
def test_no_direct_engine_calls(failures):
    """FrontAPI.py 不得绕过 AppOrch 漏斗直连引擎
    （阶段 2 遗留问题：原 api_server L185/L206/L472 三处直连绕锁）。"""
    bad = []
    for rel in ("FrontAPI.py",):
        src = read_src(rel)
        for pat in FORBIDDEN_ENGINE_CALLS:
            for i, line in enumerate(src.splitlines(), 1):
                if pat in line and not line.strip().startswith("#"):
                    bad.append(f"{rel}:{i} 直连引擎 {pat}")
    if bad:
        failures.extend(bad)
        for b in bad[:6]:
            print(f"[FAIL] ② 直连清零: {b}")
        if len(bad) > 6:
            print(f"        …共 {len(bad)} 处")
    else:
        print("[PASS] ② 直连清零: FrontAPI 0 处直连引擎"
              "（4 类原始入口全部经 AppOrch.call_* 漏斗）")


def test_rest_routes_use_funnels(failures):
    """四条曾绕锁的路由现在必须调用持锁漏斗（运行时路由函数体校验）。"""
    import FrontAPI

    funnel_of = {
        "api_stocks_analyze": "call_analysis",
        "api_stocks_select_point": "call_manual_select_point",
        "api_stocks_red_range": "call_compute_red_range_zs",
        "api_futures_select_point": "call_futures_manual_select_point",
    }
    bad = []
    for route_fn, funnel in funnel_of.items():
        fn = getattr(FrontAPI, route_fn, None)
        if fn is None:
            bad.append(f"{route_fn} 不在 FrontAPI 命名空间")
            continue
        src = inspect.getsource(fn)
        if f"orch.{funnel}" not in src:
            bad.append(f"{route_fn} 未调用 orch.{funnel}（锁漏斗断链）")
    if bad:
        failures.extend(bad)
        for b in bad:
            print(f"[FAIL] ② 漏斗接线: {b}")
    else:
        print(f"[PASS] ② 漏斗接线: 4 条历史绕锁路由全部改走持锁漏斗"
              f"（{', '.join(sorted(funnel_of))}）")


def _collect_routes():
    """枚举 app 全部 API 路由（兼容 FastAPI 0.141 的 _IncludedRouter 延迟结构）"""
    from fastapi.routing import APIRoute
    import FrontAPI

    out = set()

    def walk(routes):
        for r in routes:
            if isinstance(r, APIRoute):
                for method in (r.methods or set()):
                    if method in ("GET", "POST", "PUT", "DELETE"):
                        out.add((method, r.path))
            elif type(r).__name__ == "_IncludedRouter":
                walk(r.original_router.routes)

    walk(FrontAPI.app.routes)
    return out


def test_route_convergence(failures, update=False):
    """路由单源于 FrontAPI（api_server.py 已删除）+ 基线比对。"""
    bad = []

    # 3a-3 路由集合与冻结基线比对
    current = _collect_routes()
    os.makedirs(SNAPSHOTS, exist_ok=True)
    if update:
        with io.open(ROUTES_SNAPSHOT, "w", encoding="utf-8") as f:
            json.dump(sorted(f"{m} {p}" for m, p in current), f,
                      ensure_ascii=False, indent=1)
        print(f"[FREEZE] ③ 路由基线已重冻: {len(current)} 条 → snapshots/phase3_routes.json")
        return
    frozen = set()
    if os.path.exists(ROUTES_SNAPSHOT):
        with io.open(ROUTES_SNAPSHOT, encoding="utf-8") as f:
            frozen = {tuple(x.split(" ", 1)) for x in json.load(f)}
    baseline = frozen or EXPECTED_ROUTES
    if current != baseline:
        only_cur = current - baseline
        only_base = baseline - current
        if only_cur:
            bad.append(f"路由基线外新增: {sorted(only_cur)}")
        if only_base:
            bad.append(f"路由基线缺失: {sorted(only_base)}")

    if bad:
        failures.extend(bad)
        for b in bad:
            print(f"[FAIL] ③ 路由收敛: {b}")
    else:
        print(f"[PASS] ③ 路由收敛: 单一路由源 = FrontAPI（api_server 已删除）；"
              f"{len(current)} 条路由与基线一致")


# ═══════════════════════════════════════════════════════════════════════
# ④ 墓碑化 / 遗留入口下线（阶段 10.1 完成态）
# ═══════════════════════════════════════════════════════════════════════
def test_tombstone(failures):
    """ChartHandler 类已整体下线（10.1：my_chan_main.py 删除，入口统一 FrontAPI）。

    阶段 3a 墓碑（do_GET/do_POST → 410）→ 阶段 3b-2 拆除 SSE 旧方法 →
    阶段 10.1 类整体删除。本守护校验最终态：ChartHandler 及其全部
    遗留符号在 AppEngine 中零残留。
    """
    from App import AppEngine as m

    bad = []
    # 10.1 完成态：ChartHandler 类必须整体消失（不再有墓碑类）
    if hasattr(m, "ChartHandler"):
        bad.append("ChartHandler 应已整体下线（10.1：遗留服务器类删除）")
    # 遗留服务器别名/符号零残留
    for name in ("ThreadingHTTPServer", "HTML_TEMPLATE"):
        if hasattr(m, name):
            bad.append(f"{name} 应已随遗留服务器下线")

    # 3b-2 拆除完成：旧 SSE 方法必须已删除（灰度通过后随 legacy 桥接下线）
    for name in ("_handle_sse_stream_dual", "_handle_sse_stream_single"):
        if hasattr(m, name):
            bad.append(f"ChartHandler.{name} 应已在 3b-2 拆除（legacy 桥接已下线）")

    if bad:
        failures.extend(bad)
        for b in bad:
            print(f"[FAIL] ④ 墓碑化: {b}")
    else:
        print("[PASS] ④ 墓碑化: ChartHandler 整体下线（10.1）；"
              "SSE 旧方法已拆除（3b-2）；api_server.py / my_chan_main.py 已删除")


# ═══════════════════════════════════════════════════════════════════════
# ⑤ SSE 双实现（3b-1 灰度开关）
# ═══════════════════════════════════════════════════════════════════════
def test_sse_dual_impl(failures):
    """/api/futures_stream 仅保留方案A 同步生成器 + 数据源抽象（3b-2 已拆除 legacy）。"""
    import FrontAPI
    from App import AppOrch as orch

    bad = []

    # 原生生成器 + 数据源抽象就位（CTqSdkSession/CSSESourceClosed 由
    # AppSSE 与测试直连 DataAPI.TqSdkCSSESource，FrontAPI 不再转口）
    for name in ("sse_futures_stream_single", "sse_futures_stream_dual",
                 "CSSESource"):
        if not hasattr(FrontAPI, name):
            bad.append(f"FrontAPI 缺少 {name}（native 实现/数据源抽象缺失）")

    # 不保留 legacy 桥接层符号（3b-2 已拆除）
    for name in ("_SSEMockWfile", "_SSEMockHandler", "_sse_generator"):
        if hasattr(FrontAPI, name):
            bad.append(f"FrontAPI 仍含 legacy 符号 {name}（3b-2 应已删除）")

    # 锁分类登记为 SELF_CONTAINED
    for key in ("sse_futures_stream_single", "sse_futures_stream_dual"):
        if orch.LOCK_POLICY.get(key, ("?",))[0] != "SELF_CONTAINED":
            bad.append(f"{key} 锁分类应为 SELF_CONTAINED")

    # 路由签名：无 impl 参数（仅 native 路径）
    fn = getattr(FrontAPI, "api_futures_read_stream", None)
    if fn is None:
        bad.append("api_futures_read_stream 路由函数缺失")
    else:
        sig = inspect.signature(fn)
        if "impl" in sig.parameters:
            bad.append("/api/futures/read/stream 仍含 impl 参数（3b-2 无 legacy 路径）")

    # 同步生成器可创建（方案A：同步生成器对象可实例化，不消费即关闭）
    try:
        gen = FrontAPI.sse_futures_stream_single(
            "KQ.m@SHFE.rb", freq="15s", source=FrontAPI.CSSESource())
        import types
        if not isinstance(gen, types.GeneratorType):
            bad.append(f"方案A 单窗口非同步生成器: {type(gen).__name__}")
        gen.close()
        gen2 = FrontAPI.sse_futures_stream_dual(
            "KQ.m@SHFE.rb", "1m", "15s", source=FrontAPI.CSSESource())
        if not isinstance(gen2, types.GeneratorType):
            bad.append(f"方案A 双窗口非同步生成器: {type(gen2).__name__}")
        gen2.close()
    except Exception as e:
        bad.append(f"方案A 生成器实例化失败: {type(e).__name__}: {e}")

    if bad:
        failures.extend(bad)
        for b in bad:
            print(f"[FAIL] ⑤ SSE native: {b}")
    else:
        print("[PASS] ⑤ SSE native: 仅保留 native 原生路径；"
              "原生生成器 + CSSESource 数据源抽象就位；"
              "SELF_CONTAINED 登记一致；legacy 符号已清理")


# ═══════════════════════════════════════════════════════════════════════
# ⑥ 分层方向（FrontAPI → AppOrch → AppData 单向）
# ═══════════════════════════════════════════════════════════════════════
def test_layering_direction(failures):
    """AST 级校验：下层禁止 import 上层/跨层（设计 6.2 单向依赖）。"""
    # (文件, 禁止出现的模块名)
    # 注 1: AppData/AppOrch 对 AppEngine 的委托属阶段 2 既定过渡
    #（薄封装设计，引擎在下层，阶段 4/5 收敛），不属反向依赖；
    # 此处仅锁定层间方向（上层模块不得被下层导入）。
    # 注 2: AppEngine 是引擎底座，AppConfig 不得反向引用。
    rules = [
        ("App/AppData.py", ["AppOrch", "FrontAPI", "api_server"]),
        ("App/AppConfig.py", ["AppOrch", "AppData", "FrontAPI", "api_server",
                              "AppEngine"]),
        ("App/AppOrch.py", ["FrontAPI", "api_server"]),
    ]
    bad = []
    for rel, forbidden in rules:
        tree = ast.parse(read_src(rel))
        for node in ast.walk(tree):
            names = set()
            if isinstance(node, ast.Import):
                names = {a.name.split(".")[0] for a in node.names}
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = {node.module.split(".")[0]}
                if node.level > 0:  # from . import xxx
                    names = {a.name.split(".")[0] for a in node.names} | names
            hit = names & set(forbidden)
            if hit:
                bad.append(f"{rel} L{node.lineno} 反向/跨层导入 {sorted(hit)}")

    if bad:
        failures.extend(bad)
        for b in bad:
            print(f"[FAIL] ⑥ 分层方向: {b}")
    else:
        print("[PASS] ⑥ 分层方向: AppData/AppConfig 无上层导入；"
              "AppOrch 不导入 FrontAPI/api_server（单向依赖成立）")


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="阶段 3 成果防护守护用例")
    ap.add_argument("--update", action="store_true",
                    help="重冻路由基线（路由集合变更经确认后使用）")
    args = ap.parse_args()

    failures = []
    test_lock_policy(failures)
    test_no_direct_engine_calls(failures)
    test_rest_routes_use_funnels(failures)
    test_route_convergence(failures, update=args.update)
    if not args.update:
        test_tombstone(failures)
        test_sse_dual_impl(failures)
        test_layering_direction(failures)

    print()
    if failures:
        print(f"===== 阶段 3 成果防护: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== 阶段 3 成果防护: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
