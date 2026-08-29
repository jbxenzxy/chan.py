# -*- coding: utf-8 -*-
"""
P2-2 补测试缺口 ① —— API 集成测试（TestClient 起 app 打核心端点）
=====================================================================
背景：此前自动化测试覆盖了引擎/SSE/扫描服务层，但缺少「真实 HTTP 路由
层」的集成守护。本用例用 FastAPI TestClient 起 FrontAPI.app，直接打核心
REST 端点，锁定路由装配 / 参数校验 / 统一 JSON 响应 / 领域异常映射：

  ① 健康检查：/api/health 200 + status=ok + freq_sec_map（P2-8 前端共享）
  ② 期货状态/配置：/api/futures/read/status · /api/futures/read/config
  ③ 搜索：空关键词 400；有关键词 200（无缓存时返回 need_refresh 引导）
  ④ 扫描候选：/api/stocks/scan/read/candidates 200 + stocks 键（无数据空表）
  ⑤ 股票分析：空 code 404（FastAPI 路径校验）；无数据 400 优雅降级
  ⑥ 批量扫描：空 stocks 400；任务不存在 404；正常提交/轮询（scanner 打桩，
     避免测试环境真实拉起 ProcessPool 与 SQLite 落库）
  ⑦ 领域异常映射：AppError → 统一 JSON（error + detail）

运行：python Test/test_api_integration.py            # run_all 组件
      python Test/test_api_integration.py --update   # 兼容 run_all --update
"""
import argparse
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

from fastapi.testclient import TestClient
import FrontAPI
from App import AppOrch as orch

client = TestClient(FrontAPI.app)


def test_api_health(failures):
    """① 健康检查：200 + status=ok + freq_sec_map（P2-8 前端共享）"""
    r = client.get("/api/health")
    if r.status_code != 200:
        failures.append(f"① /api/health 状态码 {r.status_code} != 200")
        print(f"[FAIL] ① /api/health: {r.status_code}")
        return
    data = r.json()
    if data.get("status") != "ok":
        failures.append(f"① status={data.get('status')} != ok")
        print(f"[FAIL] ① /api/health: status {data.get('status')}")
        return
    if not isinstance(data.get("freq_sec_map"), dict) or not data["freq_sec_map"]:
        failures.append(f"① freq_sec_map 缺失或为空: {data.get('freq_sec_map')}")
        print(f"[FAIL] ① /api/health: freq_sec_map {data.get('freq_sec_map')}")
        return
    if data.get("entry") != "FrontAPI":
        failures.append(f"① entry={data.get('entry')} != FrontAPI")
        print(f"[FAIL] ① /api/health: entry {data.get('entry')}")
        return
    print(f"[PASS] ① /api/health: 200 + freq_sec_map({len(data['freq_sec_map'])} 项)")


def test_api_futures_status_config(failures):
    """② 期货状态/配置：200 + 结构断言"""
    r = client.get("/api/futures/read/status")
    if r.status_code != 200 or not r.json().get("ok"):
        failures.append(f"② /api/futures/read/status: {r.status_code} {r.text[:120]}")
        print(f"[FAIL] ② /api/futures/read/status: {r.status_code}")
        return
    r2 = client.get("/api/futures/read/config")
    if r2.status_code != 200:
        failures.append(f"② /api/futures/read/config: {r2.status_code}")
        print(f"[FAIL] ② /api/futures/read/config: {r2.status_code}")
        return
    cfg = r2.json()
    if not isinstance(cfg.get("supported_freqs"), list) or not cfg["supported_freqs"]:
        failures.append(f"② supported_freqs 缺失: {cfg}")
        print(f"[FAIL] ② /api/futures/read/config: {cfg}")
        return
    print(f"[PASS] ② 期货状态/配置: 200 + supported_freqs({len(cfg['supported_freqs'])})")


def test_api_search(failures):
    """③ 搜索：空关键词 400；有关键词 200（有缓存→results；无缓存→need_refresh 引导）"""
    r = client.get("/api/search", params={"q": ""})
    if r.status_code != 400:
        failures.append(f"③ 空关键词 {r.status_code} != 400")
        print(f"[FAIL] ③ /api/search?q= : {r.status_code}")
        return
    r2 = client.get("/api/search", params={"q": "600519"})
    if r2.status_code != 200:
        failures.append(f"③ 有关键词 {r2.status_code} != 200")
        print(f"[FAIL] ③ /api/search?q=600519 : {r2.status_code}")
        return
    data = r2.json()
    # search_stocks 返回两个合法形态之一：有缓存→{"results":[...]}，无缓存→{"need_refresh":...,"msg":...}
    if "results" not in data and "need_refresh" not in data:
        failures.append(f"③ 响应缺少 results/need_refresh: {data}")
        print(f"[FAIL] ③ /api/search?q=600519 : {data}")
        return
    print("[PASS] ③ 搜索: 空词 400 / 有词 200（results 或 need_refresh 引导）")


def test_api_scan_candidates(failures):
    """④ 扫描候选：200 + stocks 键（无数据空表，不抛错）"""
    r = client.get("/api/stocks/scan/read/candidates", params={"source": "zxg"})
    if r.status_code != 200:
        failures.append(f"④ 候选列表 {r.status_code} != 200")
        print(f"[FAIL] ④ /api/stocks/scan/read/candidates: {r.status_code}")
        return
    data = r.json()
    if "stocks" not in data or "total" not in data:
        failures.append(f"④ 响应缺少 stocks/total: {data}")
        print(f"[FAIL] ④ /api/stocks/scan/read/candidates: {data}")
        return
    print(f"[PASS] ④ 扫描候选: 200 + stocks/total（total={data['total']}）")


def test_api_analyze_guards(failures):
    """⑤ 股票分析守卫：空 code 404（路径校验）；无数据 400 优雅降级"""
    r = client.get("/api/stocks//analyze")
    if r.status_code != 404:
        failures.append(f"⑤ 空 code {r.status_code} != 404")
        print(f"[FAIL] ⑤ /api/stocks//analyze: {r.status_code}")
        return
    r2 = client.get("/api/stocks/600519/analyze", params={"freq": "d"})
    # 无 TDX 数据环境下应优雅返回 400（K线数据不足），而非 500
    if r2.status_code not in (200, 400):
        failures.append(f"⑤ 无数据分析 {r2.status_code} 非 200/400")
        print(f"[FAIL] ⑤ /api/stocks/600519/analyze: {r2.status_code} {r2.text[:120]}")
        return
    if r2.status_code == 400:
        body = r2.json()
        if "error" not in body:
            failures.append(f"⑤ 400 响应缺少 error: {body}")
            print(f"[FAIL] ⑤ /api/stocks/600519/analyze: {body}")
            return
    print(f"[PASS] ⑤ 分析守卫: 空code 404 / 无数据 {r2.status_code} 优雅降级")


def test_api_scan_submit_guards(failures):
    """⑥ 批量扫描守卫：空 stocks 400；任务不存在 404"""
    r = client.post("/api/stocks/scan/submit", json={"stocks": []})
    if r.status_code != 400:
        failures.append(f"⑥ 空 stocks {r.status_code} != 400")
        print(f"[FAIL] ⑥ POST /api/stocks/scan/submit(空): {r.status_code}")
        return
    r2 = client.get("/api/stocks/scan/nonexistent-task/read/status")
    if r2.status_code != 404:
        failures.append(f"⑥ 不存在任务 {r2.status_code} != 404")
        print(f"[FAIL] ⑥ GET 不存在任务: {r2.status_code}")
        return
    print("[PASS] ⑥ 批量扫描守卫: 空stocks 400 / 不存在任务 404")


def test_api_scan_submit_mocked(failures):
    """⑥b 批量扫描提交/轮询（scanner 打桩，避免真实拉起 ProcessPool）"""
    orig_submit = orch.scanner.submit_batch_scan
    orig_status = orch.scanner.get_batch_scan_status
    try:
        orch.scanner.submit_batch_scan = lambda *a, **k: {
            "task_id": "mock-task-1", "total": 2, "workers": 1,
            "engine": "process_pool"}
        orch.scanner.get_batch_scan_status = lambda *a, **k: {
            "task_id": "mock-task-1", "status": "running", "total": 2,
            "completed": 1, "results": [], "error": None}

        r = client.post("/api/stocks/scan/submit",
                        json={"stocks": [{"code": "600519", "prefix": "1"},
                                         {"code": "000001", "prefix": "0"}]})
        if r.status_code != 200:
            failures.append(f"⑥b 提交 {r.status_code} != 200")
            print(f"[FAIL] ⑥b POST /api/stocks/scan/submit: {r.status_code}")
            return
        data = r.json()
        if data.get("task_id") != "mock-task-1" or data.get("total") != 2:
            failures.append(f"⑥b 提交响应异常: {data}")
            print(f"[FAIL] ⑥b POST /api/stocks/scan/submit: {data}")
            return

        r2 = client.get("/api/stocks/scan/mock-task-1/read/status")
        if r2.status_code != 200:
            failures.append(f"⑥b 轮询 {r2.status_code} != 200")
            print(f"[FAIL] ⑥b GET 状态: {r2.status_code}")
            return
        st = r2.json()
        if st.get("status") != "running" or st.get("completed") != 1:
            failures.append(f"⑥b 轮询响应异常: {st}")
            print(f"[FAIL] ⑥b GET 状态: {st}")
            return
        print("[PASS] ⑥b 批量扫描提交/轮询: 200 + task_id/status 透传")
    finally:
        orch.scanner.submit_batch_scan = orig_submit
        orch.scanner.get_batch_scan_status = orig_status


def test_api_app_error_mapping(failures):
    """⑦ 领域异常映射：AppError → 统一 JSON（error + detail）"""
    from App.AppOrch import AppError

    class _TestAppError(AppError):
        status_code = 400

    orig = orch.call_analysis
    try:
        def boom(code, **kw):
            raise _TestAppError("测试领域异常")
        orch.call_analysis = boom
        r = client.get("/api/stocks/600519/analyze", params={"freq": "d"})
        if r.status_code != 400:
            failures.append(f"⑦ AppError 状态码 {r.status_code} != 400")
            print(f"[FAIL] ⑦ AppError 映射: {r.status_code}")
            return
        body = r.json()
        if body.get("error") == "InternalError" or "detail" not in body:
            failures.append(f"⑦ AppError 响应结构异常: {body}")
            print(f"[FAIL] ⑦ AppError 映射: {body}")
            return
        print("[PASS] ⑦ 领域异常映射: AppError → 400 + {error, detail}")
    finally:
        orch.call_analysis = orig


def main():
    ap = argparse.ArgumentParser(description="P2-2 ① API 集成测试")
    ap.add_argument("--update", action="store_true", help="兼容 run_all --update")
    args = ap.parse_args()

    failures = []
    test_api_health(failures)
    test_api_futures_status_config(failures)
    test_api_search(failures)
    test_api_scan_candidates(failures)
    test_api_analyze_guards(failures)
    test_api_scan_submit_guards(failures)
    test_api_scan_submit_mocked(failures)
    test_api_app_error_mapping(failures)

    print()
    if failures:
        print(f"===== P2-2 ① API 集成测试: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== P2-2 ① API 集成测试: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
