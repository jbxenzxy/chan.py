# -*- coding: utf-8 -*-
"""
eltdx 统计取数的「坏代码二分隔离」守护
=====================================================================
背景（2026-10-26）：
  A 股全市场扫描取流通市值时，`_chunked_stats_fetch` 按 80 只 / 批调
  `corporate.finance_batch`（0x0010）与 `quotes.get_snapshots`（0x054c）。
  本地 vipdoc 里已退市 / 长期停牌票的文件名仍会进候选，而这两条协议在遇到
  这类代码时**整批解析失败**（不是跳过单条）：
    - 0x054c → ProtocolError("snapshot record marker not found: <code>")
    - 0x0010 → ProtocolError("invalid ASCII response code")
  原实现逐只重试隔离，实测把 55~63ms 的批次拖到 4.72s（放大 75 倍）。

锁定契约：
  [1] 干净批：1 次请求取完，不做任何拆分；
  [2] 单颗毒丸：坏代码被准确识别、其余票数据完整，请求次数远小于逐只 80 次；
  [3] 多颗毒丸：仍能全部识别且数据不丢；
  [4] 传输类异常（超时 / 断连）：**不做二分**，整批重试 1 次、仍失败才跳过
      （2 次请求），避免 1 次网络故障被拆成 159 次超时，也不因瞬时抖动丢一批；
  [5] 空输入：0 次请求；
  [8] 错位载荷里的脏 code（实测见过 HTML/CSS 片段被当成 code）不得进结果键；
  [9] 协议失败与代码无关（整批错位等帧级故障）：请求预算止损
      （≤80 次，不再打满 2n−1=160），且不产出任何脏数据；
  [10] 传输类抖动重试 1 次成功：整批数据完整取回（2 次请求）。

全程打桩，不联网、不依赖 eltdx 服务器。跑法：
    python Test/test_eltdx_stats_bisect.py
"""
import os
import sys
import types

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

from DataAPI.ElTdxAPI import STATS_BATCH, _bisect_fetch, _chunked_stats_fetch

try:
    from eltdx.exceptions import ProtocolError
except ImportError:
    ProtocolError = type("ProtocolError", (Exception,), {})


def _record(code):
    return types.SimpleNamespace(full_code=code, last_price=10.0)


def _extract(rows):
    return {r.full_code: r.last_price for r in rows}


def _make_batch_fn(poison, calls, exc_type=ProtocolError):
    """模拟 eltdx：批次里含 poison 中任一代码即抛异常，否则逐条返回记录。"""
    poison = set(poison)

    def batch_fn(codes):
        codes = list(codes)
        calls.append(len(codes))
        for c in codes:
            if c in poison:
                raise exc_type(f"snapshot record marker not found: {c}")
        return [_record(c) for c in codes]

    return batch_fn


def _codes(n, prefix="sh60"):
    return [f"{prefix}{i:04d}" for i in range(n)]


def test_clean_batch(failures):
    """[1] 干净批：1 次请求。"""
    codes = _codes(80)
    calls = []
    got = _chunked_stats_fetch(codes, _make_batch_fn(set(), calls), _extract)
    if len(calls) != 1 or len(got) != 80:
        failures.append(f"[1] 干净批应 1 次请求取完 80 只，实际 {len(calls)} 次 / {len(got)} 只")
        print(f"[FAIL] [1] 干净批: {len(calls)} 次 / {len(got)} 只")
    else:
        print("[PASS] [1] 干净批：1 次请求取完 80 只")


def test_single_poison(failures):
    """[2] 单颗毒丸：识别准确、数据不丢、请求次数远小于 80。"""
    codes = _codes(80)
    bad_code = codes[37]
    calls = []
    got = _chunked_stats_fetch(codes, _make_batch_fn({bad_code}, calls), _extract)
    missing = [c for c in codes if c != bad_code and c not in got]
    if bad_code in got:
        failures.append("[2] 毒丸代码不应出现在结果里")
        print("[FAIL] [2] 毒丸未被剔除")
        return
    if missing:
        failures.append(f"[2] 好票数据丢失 {len(missing)} 只: {missing[:5]}")
        print(f"[FAIL] [2] 好票丢失 {len(missing)} 只")
        return
    if len(calls) > 20:
        failures.append(f"[2] 请求次数 {len(calls)} 超过 20（二分未生效？）")
        print(f"[FAIL] [2] 请求次数 {len(calls)}")
        return
    print(f"[PASS] [2] 单颗毒丸：79 只全部取回，请求 {len(calls)} 次（原逐只 80 次）")


def test_multi_poison(failures):
    """[3] 多颗毒丸：全部识别且数据不丢。"""
    codes = _codes(80)
    poison = {codes[0], codes[37], codes[79]}
    calls = []
    got = _chunked_stats_fetch(codes, _make_batch_fn(poison, calls), _extract)
    missing = [c for c in codes if c not in poison and c not in got]
    leaked = [c for c in poison if c in got]
    if missing or leaked:
        failures.append(f"[3] 多毒丸处理错: 丢失 {len(missing)} 只 / 未剔除 {len(leaked)} 只")
        print(f"[FAIL] [3] 丢失 {len(missing)} / 未剔除 {len(leaked)}")
        return
    print(f"[PASS] [3] 多颗毒丸：77 只全部取回，请求 {len(calls)} 次")


def test_transport_error_not_split(failures):
    """[4] 传输类异常：整批重试 1 次、仍失败才跳过（2 次请求，不做二分）。"""
    codes = _codes(80)
    calls = []

    def always_timeout(_codes_):
        calls.append(len(_codes_))
        raise TimeoutError("read timed out")

    got = _chunked_stats_fetch(codes, always_timeout, _extract)
    if len(calls) != 2 or got:
        failures.append(f"[4] 传输失败应重试 1 次后跳过（2 次请求无数据），实际 {len(calls)} 次 / {len(got)} 只")
        print(f"[FAIL] [4] 传输失败: {len(calls)} 次 / {len(got)} 只")
    else:
        print("[PASS] [4] 传输类异常：重试 1 次仍失败即整批跳过（2 次请求，不拆成 159 次超时）")


def test_transport_retry_once_succeeds(failures):
    """[10] 传输类抖动重试 1 次成功：整批数据完整取回。"""
    codes = _codes(80)
    calls = []
    state = {"n": 0}

    def flaky(_codes_):
        calls.append(len(_codes_))
        state["n"] += 1
        if state["n"] == 1:
            raise TimeoutError("read timed out")
        return [_record(c) for c in _codes_]

    got = _chunked_stats_fetch(codes, flaky, _extract)
    missing = [c for c in codes if c not in got]
    if len(calls) != 2 or missing:
        failures.append(f"[10] 传输抖动重试后应取回 80 只（2 次请求），实际 {len(calls)} 次 / 缺 {len(missing)} 只")
        print(f"[FAIL] [10] 传输抖动: {len(calls)} 次 / 缺 {len(missing)} 只")
    else:
        print("[PASS] [10] 传输类抖动：重试 1 次成功，80 只完整取回（瞬时抖动不丢批）")


def test_protocol_all_fail_budget(failures):
    """[9] 协议失败与代码无关（整批错位）：请求预算止损，不比逐只重试更差。

    原实现二分必失败到叶子：80 只全失败打满 2n−1=160 次请求。
    """
    from DataAPI.ElTdxAPI import _BISECT_MAX_REQUESTS
    codes = _codes(80)
    calls = []

    def always_protocol_error(codes_):
        calls.append(len(codes_))
        raise ProtocolError("invalid ASCII response code")

    got = _chunked_stats_fetch(codes, always_protocol_error, _extract)
    # 上限 80 = 「不比逐只重试 80 次更差」；预算生效时应为 _BISECT_MAX_REQUESTS 次
    if len(calls) > 80 or got:
        failures.append(f"[9] 全批协议失败应预算止损（≤80 次请求、无脏数据），"
                        f"实际 {len(calls)} 次 / {len(got)} 只（预算常量 {_BISECT_MAX_REQUESTS}）")
        print(f"[FAIL] [9] 全批协议失败: {len(calls)} 次 / {len(got)} 只")
    else:
        print(f"[PASS] [9] 协议失败与代码无关：预算止损 {len(calls)} 次"
              f"（原 2n−1=160 次），80 只全部记 bad、无脏数据")


def test_empty(failures):
    """[5] 空输入：0 次请求。"""
    calls = []
    got = _chunked_stats_fetch([], _make_batch_fn(set(), calls), _extract)
    if calls or got:
        failures.append(f"[5] 空输入应 0 次请求，实际 {len(calls)} 次")
        print(f"[FAIL] [5] 空输入: {len(calls)} 次")
    else:
        print("[PASS] [5] 空输入：0 次请求")


def test_bisect_returns_poison_detail(failures):
    """[6] _bisect_fetch 直接调用：bad 带可读原因，rows 覆盖全部好票。"""
    codes = _codes(20)
    bad_code = codes[5]
    calls = []
    rows, bad = _bisect_fetch(codes, _make_batch_fn({bad_code}, calls))
    ok_codes = {r.full_code for r in rows}
    if [c for c, _ in bad] != [bad_code]:
        failures.append(f"[6] bad 列表不符: {bad}")
        print(f"[FAIL] [6] bad: {bad}")
    elif ok_codes != set(codes) - {bad_code}:
        failures.append(f"[6] rows 覆盖不全: 缺 {sorted(set(codes) - {bad_code} - ok_codes)}")
        print("[FAIL] [6] rows 覆盖不全")
    elif not bad[0][1].strip():
        failures.append("[6] bad 缺少错误原因文本")
        print("[FAIL] [6] 无原因文本")
    else:
        print(f"[PASS] [6] _bisect_fetch：bad={bad_code} 原因文本在位，好票 19 只齐全")


def test_stats_batch_constant(failures):
    """[7] 分批上限仍是 80（协议实测稳定值，改动会影响服务端截断行为）。"""
    if STATS_BATCH != 80:
        failures.append(f"[7] STATS_BATCH 被改为 {STATS_BATCH}（实测 200 会被服务端截断）")
        print(f"[FAIL] [7] STATS_BATCH = {STATS_BATCH}")
    else:
        print("[PASS] [7] STATS_BATCH 仍为 80")


def test_dirty_code_filtered(failures):
    """[8] 错位载荷的脏 code 不得进结果键（实测见过 HTML/CSS 片段）。"""
    from DataAPI.ElTdxAPI import _finance_shares_map, _snapshot_price_map

    recs = [
        types.SimpleNamespace(exchange="sh", code="601066", circulating_shares=123.0),
        types.SimpleNamespace(exchange="sh", code='tyle["', circulating_shares=999.0),
        types.SimpleNamespace(exchange="sz", code="\x00\x00\x00\x00\x00\x00",
                              circulating_shares=5.0),
        types.SimpleNamespace(exchange="sh", code=None, circulating_shares=7.0),
        types.SimpleNamespace(exchange="sh", code="6010", circulating_shares=8.0),
    ]
    shares = _finance_shares_map(recs)
    rows = [
        types.SimpleNamespace(full_code="sh601066", last_price=22.9),
        types.SimpleNamespace(full_code="shxk7DX+", last_price=1.0),
        types.SimpleNamespace(full_code="sh6010", last_price=1.0),
        types.SimpleNamespace(full_code="sh\u0000\u0000\u0000\u0000\u0000\u0000", last_price=1.0),
    ]
    prices = _snapshot_price_map(rows)
    if list(shares) != ["sh601066"] or list(prices) != ["sh601066"]:
        failures.append(f"[8] 脏 code 未被挡住: shares={list(shares)} prices={list(prices)}")
        print(f"[FAIL] [8] shares={list(shares)} prices={list(prices)}")
    else:
        print("[PASS] [8] 脏 code（HTML/CSS 片段、NUL 占位、非 6 位）全部挡在结果键之外")


def main():
    failures = []
    test_clean_batch(failures)
    test_single_poison(failures)
    test_multi_poison(failures)
    test_transport_error_not_split(failures)
    test_empty(failures)
    test_bisect_returns_poison_detail(failures)
    test_stats_batch_constant(failures)
    test_dirty_code_filtered(failures)
    test_transport_retry_once_succeeds(failures)
    test_protocol_all_fail_budget(failures)
    print("=" * 60)
    if failures:
        print(f"结果: {len(failures)} 处 FAIL")
        for f in failures:
            print("  -", f)
        return 1
    print("结果: 全部 PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
