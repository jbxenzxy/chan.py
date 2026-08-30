# -*- coding: utf-8 -*-
"""
阶段 2.5：回归测试基线 —— 统一入口
=====================================================================
一条命令跑完整个基线，产出汇总报告。各组件独立子进程执行
（monkeypatch 互不干扰），任一失败即整体退出码非 0（可直接接入
CI / 迁移每阶段的验收门禁）。

组件（按依赖顺序）：
  1. fixtures 完整性   gen_fixtures.py --check      冻结输入未被手改
  2. 核心快照回归      snapshot_runner.py --all     笔/段/中枢/买卖点 7 维度（股票+期货）
  3. trigger_step 回放 test_trigger_step_replay.py  逐步回放收敛一致性
  4. 阶段 2 成果防护   test_phase2_guards.py        配置一致性/异常链路/引擎边界/日期契约
  5. 确定性测试        test_determinism.py          重复调用/跨路径污染/双窗口语义
  6. 行业映射完整性    test_industry_mapping.py     双路径加载不静默降级 + 条目质量
  7. SSE 事件序列      test_sse_sequence.py         首事件/序列/正常关闭（legacy 桥接）
  8. 函数映射同步      func_map_check.py            阶段 2.6：74 函数/57 状态归属
                                                     完备·无幽灵·行号无漂移
  9. 阶段 3 成果防护   test_phase3_guards.py        锁分类/直连清零/路由收敛/墓碑/
                                                     SSE 双实现/分层方向
 10. 阶段 4 成果防护   test_phase4_guards.py        委托壳+目标存在/状态别名同一性/
                                                     配置别名清零/自选股收敛/语义子窗/
                                                     分层方向/LRU 语义/数据源 import 门禁(P1-1)
 11. 市场量能行为测试  test_app_amo.py               真行为测试（合成 .day 合成数据）
 12. 双窗公式纯函数    test_stocks_dual_algo.py      P0 4 向公式单测（方向×边界）
 13. .blk 解析与自选股  test_blk_parsing.py          黄金行为（对齐 DoubleOptimize）/
                                                     双解析器一致性/缺失文件/自选股链路/
                                                     扫描消费兼容/防 tdx_blk 回归/
                                                     成分股/板块指数2·3/多来源合并
 14. SSE 增量快照      test_sse_incremental.py       P2-4：增量 klines/MACD ≡ 全量 /
                                                     快照同构/状态缺失回退
 15. SSE 灰度比对      test_sse_gray.py              3b-1：native vs 冻结基线
                                                     （①类型序列 ②剥离时间戳结构 ③总数）
 16. 阶段 5 成果防护   test_phase5_guards.py         获取侧抽象完善：tdxhy 迁 App/统一加载/
                                                     元数据接口提升/
                                                     AppOrch 委托/fetch_kline 抽象
 17. 阶段 6 成果防护   test_phase6_guards.py         前端组件化：组件区块/KLineChart 契约/
                                                     window API 面冻结/事件引用/零构建/
                                                     注册表一致/缓存击穿/合并层完整
 18. 阶段 7 成果防护   test_phase7_guards.py         批量扫描异步化：ScanStore 分层缓存/
                                                     ScanPool ProcessPool 编排/AppOrch 薄封装/
                                                     双路径 API/前端三模式接入/依赖方向
 19. API 集成测试      test_api_integration.py       P2-2①：TestClient 起 app 打核心端点/
                                                     健康检查/搜索/扫描守卫/领域异常映射
 20. 代码输入链路守护  test_code_resolution_guards.py 沪深重名消歧/大小写契约/搜索双市场候选/
                                                     search 与引擎同源兜底
 21. SSE 多连接并发    test_sse_concurrent.py        P2-2②：8 连接并发隔离/事件序列一致
 22. 扫描池失败收敛  test_scanpool_fallback.py     P2-2③：装配/派发失败收敛为任务
                                                     error + 坏池自愈（无线程降级）
 23. 前端 JS 冒烟      test_frontend_smoke.py        P2-2④：HTML 骨架/JS 语法/组件注册/事件引用
 24. 锁 v5 守护        test_lock_v5_guards.py        2026-08 锁收敛：线程局部注入隔离 /
                                                     复盘标志线程局部 / AppData 锁覆盖 /
                                                     原子写 / 已删符号防回潮
 25. CChan 数据隔离    test_chan_data_isolation.py   8 线程×3 轮并发建链 ≡ 串行基线
 26. 数据隔离对照      test_chan_data_isolation_control.py
                                                     确定性交错：证明类变量注入串数据、
                                                     线程局部注入隔离（非空测试的自检）
每组件独立子进程执行，超时 300s 按失败终止（防死循环挂死）。

用法（在仓库根目录）：
    python Test/run_all.py             # 全量回归（比对冻结基线）
    python Test/run_all.py --update    # 重新冻结全部基线（迁移改动确认后）
    python Test/run_all.py --report out.json   # 额外落盘机器可读报告
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)

# (组件名, 命令) —— 顺序即执行顺序
COMPONENTS = [
    ("fixtures_integrity",
     [sys.executable, os.path.join("Test", "gen_fixtures.py"), "--check"]),
    ("snapshot_regression",
     [sys.executable, os.path.join("Test", "snapshot_runner.py")]),
    ("trigger_step_replay",
     [sys.executable, os.path.join("Test", "test_trigger_step_replay.py")]),
    ("phase2_guards",
     [sys.executable, os.path.join("Test", "test_phase2_guards.py")]),
    ("determinism",
     [sys.executable, os.path.join("Test", "test_determinism.py")]),
    ("industry_mapping",
     [sys.executable, os.path.join("Test", "test_industry_mapping.py")]),
    ("sse_sequence",
     [sys.executable, os.path.join("Test", "test_sse_sequence.py")]),
    ("func_map_sync",
     [sys.executable, os.path.join("Test", "func_map_check.py")]),
    ("phase3_guards",
     [sys.executable, os.path.join("Test", "test_phase3_guards.py")]),
    ("phase4_guards",
     [sys.executable, os.path.join("Test", "test_phase4_guards.py")]),
    ("app_amo_behavior",
     [sys.executable, os.path.join("Test", "test_app_amo.py")]),
    ("stocks_dual_algo",
     [sys.executable, os.path.join("Test", "test_stocks_dual_algo.py")]),
    ("blk_parsing",
     [sys.executable, os.path.join("Test", "test_blk_parsing.py")]),
    ("sse_incremental",
     [sys.executable, os.path.join("Test", "test_sse_incremental.py")]),
    ("phase5_guards",
     [sys.executable, os.path.join("Test", "test_phase5_guards.py")]),
    ("phase6_guards",
     [sys.executable, os.path.join("Test", "test_phase6_guards.py")]),
    ("phase7_guards",
     [sys.executable, os.path.join("Test", "test_phase7_guards.py")]),
    ("sse_gray",
     [sys.executable, os.path.join("Test", "test_sse_gray.py")]),
    ("api_integration",
     [sys.executable, os.path.join("Test", "test_api_integration.py")]),
    ("code_resolution_guards",
     [sys.executable, os.path.join("Test", "test_code_resolution_guards.py")]),
    ("lock_v5_guards",
     [sys.executable, os.path.join("Test", "test_lock_v5_guards.py")]),
    ("chan_data_isolation",
     [sys.executable, os.path.join("Test", "test_chan_data_isolation.py")]),
    ("chan_data_isolation_control",
     [sys.executable, os.path.join("Test", "test_chan_data_isolation_control.py")]),
    ("sse_concurrent",
     [sys.executable, os.path.join("Test", "test_sse_concurrent.py")]),
    ("futures_sub_key",
     [sys.executable, os.path.join("Test", "test_futures_sub_key.py")]),
    ("futures_session_binding",
     [sys.executable, os.path.join("Test", "test_futures_session_binding.py")]),
    ("scanpool_fallback",
     [sys.executable, os.path.join("Test", "test_scanpool_fallback.py")]),
    ("frontend_smoke",
     [sys.executable, os.path.join("Test", "test_frontend_smoke.py")]),
]

# 单组件超时（秒）：防阶段 3 重构引入死循环/长阻塞挂死整个 CI
COMPONENT_TIMEOUT_S = 300


def run_component(name, cmd, update=False, env=None):
    """执行单个组件，返回记录 dict。超时按失败处理（不无限等待）。"""
    real_cmd = list(cmd)
    if update and name in ("snapshot_regression", "trigger_step_replay",
                           "phase2_guards", "industry_mapping", "sse_sequence",
                           "func_map_sync", "phase3_guards", "phase4_guards",
                           "phase5_guards", "sse_gray"):
        real_cmd.append("--update")
    t0 = time.time()
    try:
        proc = subprocess.run(
            real_cmd, cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=COMPONENT_TIMEOUT_S)
        ok, exit_code, timed_out = proc.returncode == 0, proc.returncode, False
        out = proc.stdout
    except subprocess.TimeoutExpired as e:
        ok, exit_code, timed_out = False, None, True
        out = (e.stdout or "") + f"\n[TIMEOUT] 组件 {name} 超过 {COMPONENT_TIMEOUT_S}s 被终止"
    elapsed = time.time() - t0
    rec = {
        "name": name,
        "cmd": " ".join(os.path.relpath(c, REPO_ROOT) if os.path.isabs(c) else c
                        for c in real_cmd),
        "ok": ok,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_s": round(elapsed, 2),
        "output_tail": (out or "").strip().splitlines()[-12:],
    }
    return rec


def main():
    ap = argparse.ArgumentParser(description="阶段 2.5 回归测试基线统一入口")
    ap.add_argument("--update", action="store_true",
                    help="重新冻结全部基线（迁移改动经人工确认后使用）")
    ap.add_argument("--report", metavar="PATH",
                    help="额外写入机器可读 JSON 报告（默认 Test/report.json）")
    args = ap.parse_args()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    print("=" * 64)
    print("阶段 2.5 回归测试基线" + ("（重新冻结模式）" if args.update else ""))
    print(f"仓库: {REPO_ROOT}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 64)

    records = []
    for name, cmd in COMPONENTS:
        print(f"\n──── [{len(records) + 1}/{len(COMPONENTS)}] {name} ────")
        rec = run_component(name, cmd, update=args.update, env=env)
        records.append(rec)
        print("\n".join(rec["output_tail"]))
        print(f"──── {'PASS' if rec['ok'] else 'FAIL'} ({rec['elapsed_s']}s) ────")

    n_ok = sum(1 for r in records if r["ok"])
    n_all = len(records)
    summary = {
        "phase": "7",
        "mode": "update" if args.update else "verify",
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "total": n_all,
        "passed": n_ok,
        "failed": n_all - n_ok,
        "components": records,
    }

    report_path = args.report or os.path.join(TEST_DIR, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    print("\n" + "=" * 64)
    for r in records:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['name']:<24} {r['elapsed_s']:>6}s")
    print("=" * 64)
    print(f"结果: {n_ok}/{n_all} 通过 | 报告: {os.path.relpath(report_path, REPO_ROOT)}")
    if args.update:
        print("注意: 基线已重新冻结，请 git diff Test/snapshots/ 逐项审查后提交。")
    return n_ok == n_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
