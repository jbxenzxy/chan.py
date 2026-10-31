# -*- coding: utf-8 -*-
"""
eltdx 批量取数的「缺票跳过 + 坏批补救」守护
=====================================================================
背景（2026-10-31，eltdx 3.3.2 起）：
  A 股全市场扫描取流通市值时，`_chunked_stats_fetch` 按 80 只 / 批调
  `corporate.finance_batch`（0x0010）与 `quotes.get_snapshots`（0x054c）。
  本地 vipdoc 里已退市 / 长期停牌票的文件名仍会进候选。

  eltdx 3.3.2 之前：这两条协议遇到这类代码会**整批解析失败**
  （`snapshot record marker not found` / `invalid ASCII response code`），
  一颗毒丸拖掉整批 80 只。当时的对策是应用层递归二分定位（见 git 历史
  `_bisect_fetch`），并用「叶子失败 ≥32 且零成功 + 等距取样探测」止损。

  eltdx 3.3.2 起：缺票改由 `on_missing` 策略处理 —— 传 `"skip"` 时库内**在
  32 次请求预算内**自行定位缺失代码、返回其余记录，不再抛异常。应用层再二分
  等于给每个子批重新发放一份 32 次预算，实测把整批故障从 ~90 次请求放大到
  上千次。故改为「拆段 + 逐只」有界补救（`_salvage_fetch`），删掉二分与探测。

锁定契约：
  [1] 干净批：1 次请求取完，不做任何拆分；
  [2] 缺票（skip 语义）：1 次请求，缺的票不进结果、好票零丢失，**不抛异常**；
  [3] 缺票必须被记账：部分缺 → info（含代码清单），整批一只没回 → warning；
  [4] 传输类异常（超时 / 断连）：**不拆段**，整批重试 1 次、仍失败才跳过
      （2 次请求），避免 1 次网络故障被放大成 80 次超时；
  [5] 空输入：0 次请求；
  [6] `_salvage_fetch` 直接调用：bad 带可读原因、rows 覆盖全部好票；
  [7] STATS_BATCH 仍是 80（协议实测稳定值）；
  [8] 错位载荷里的脏 code（实测见过 HTML/CSS 片段、NUL 占位）不得进结果键，
      且必须**告警**（3.3.2 协议层已保证不会出现，命中即上游退化）；
  [9] 传输抖动重试 1 次成功：整批数据完整取回（2 次请求）；
  [10] 首轮传输错 + 重试抛协议错（同一故障链：超时后读到错位帧）：必须转入
      拆段补救，不得按「传输失败」整批丢弃；
  [11] 单颗坏码：拆段补救后好票零丢失，请求 ≤ 20 次（逐只要是 80 次）；
  [12] 多颗坏码（含头/尾）：全部隔离、好票零丢失；
  [13] 整批故障（拆段后所有段都失败）：**不逐只**，立刻止损、账目 n/n 无重复，
      请求 = ceil(n/8) 次（80 只 → 10 次）；
  [14] `fetch_float_mc` 必须以 `on_missing="skip"` 调两条批量命令 —— 这是整条
      缺票语义的源头，缺了它 3.3.2 的收益全部回退（默认 raise 仍会整批失败）。
  [15] 补救途中遇上网络故障（超时 / 断连）：**不得逐只放大**，立即收手；
      账目必须「已取到的进 rows、未取到的进 bad」且两者不重叠。
  [16] 逐只阶段「先一只协议错、随后另一只传输错」：同一 code 只许有一条 bad
      （[15] 的传输错落在段阶段、bad 为空，覆盖不到这条混合路径）；
  [17] n <= _SALVAGE_SUBCHUNK（自选股 / 小板块）遇载荷异常毒丸：仍须救回好票，
      不得按「所有段都失败」整批丢；
  [18] n <= _SALVAGE_SUBCHUNK 且每只都失败：逐只有界（n 次）、账目无重复；
  [19] 能力探测必须覆盖两条批量命令：只给一条加 on_missing 判不支持。

全程打桩，不联网、不依赖 eltdx 服务器。跑法：
    python Test/test_eltdx_batch_salvage.py
"""
import logging
import os
import sys
import types

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import DataAPI.ElTdxAPI as eltdx_api
from DataAPI.ElTdxAPI import (
    STATS_BATCH, _SALVAGE_SUBCHUNK, _salvage_fetch, _chunked_stats_fetch,
)

try:
    from eltdx.exceptions import ProtocolError
except ImportError:
    ProtocolError = type("ProtocolError", (Exception,), {})


def _record(code):
    return types.SimpleNamespace(full_code=code, last_price=10.0)


def _extract(rows):
    return {r.full_code: r.last_price for r in rows}


def _make_batch_fn(poison, calls, exc_type=ProtocolError):
    """模拟 3.3.2 的坏批：批次里含 poison 中任一代码即抛协议错，否则逐条返回。"""
    poison = set(poison)

    def batch_fn(codes):
        codes = list(codes)
        calls.append(len(codes))
        for c in codes:
            if c in poison:
                raise exc_type(f"finance batch unexpected or duplicate record: {c}")
        return [_record(c) for c in codes]

    return batch_fn


def _make_skip_batch_fn(missing, calls):
    """模拟 3.3.2 的 skip 语义：缺票被丢掉，其余正常返回，**不抛异常**。"""
    missing = set(missing)

    def batch_fn(codes):
        codes = list(codes)
        calls.append(len(codes))
        return [_record(c) for c in codes if c not in missing]

    return batch_fn


def _codes(n, prefix="sh60"):
    return [f"{prefix}{i:04d}" for i in range(n)]


class _Capture(logging.Handler):
    """收集日志记录，用于断言「缺票必须被记账」。"""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def messages(self, level):
        return [r.getMessage() for r in self.records if r.levelno == level]


def _capture(fn):
    cap = _Capture()
    logger = logging.getLogger("DataAPI.ElTdxAPI")
    old_level = logger.level
    logger.addHandler(cap)
    logger.setLevel(logging.DEBUG)
    try:
        result = fn()
    finally:
        logger.removeHandler(cap)
        logger.setLevel(old_level)
    return result, cap


def test_clean_batch(failures):
    """[1] 干净批：1 次请求。"""
    codes = _codes(80)
    calls = []
    got = _chunked_stats_fetch(codes, _make_skip_batch_fn(set(), calls), _extract)
    if len(calls) != 1 or len(got) != 80:
        failures.append(f"[1] 干净批应 1 次请求取完 80 只，实际 {len(calls)} 次 / {len(got)} 只")
        print(f"[FAIL] [1] 干净批: {len(calls)} 次 / {len(got)} 只")
    else:
        print("[PASS] [1] 干净批：1 次请求取完 80 只")


def test_missing_codes_skipped(failures):
    """[2] 缺票（skip）：1 次请求、不抛异常、好票零丢失、缺票不进结果。"""
    codes = _codes(80)
    gone = {codes[5], codes[40], codes[79]}
    calls = []
    try:
        got = _chunked_stats_fetch(codes, _make_skip_batch_fn(gone, calls), _extract)
    except Exception as exc:
        failures.append(f"[2] 缺票不得抛异常，实际 {type(exc).__name__}: {exc}")
        print(f"[FAIL] [2] 缺票抛了 {type(exc).__name__}")
        return
    lost = [c for c in codes if c not in gone and c not in got]
    leaked = [c for c in gone if c in got]
    if len(calls) != 1 or lost or leaked:
        failures.append(f"[2] 缺票批应 1 次请求、好票零丢失、缺票零泄漏，"
                        f"实际 {len(calls)} 次 / 丢 {len(lost)} / 漏 {len(leaked)}")
        print(f"[FAIL] [2] {len(calls)} 次 / 丢 {len(lost)} / 漏 {len(leaked)}")
    else:
        print(f"[PASS] [2] 缺票（3 只）：1 次请求，77 只好票全取回、缺票不进结果、不抛异常")


def test_missing_codes_are_logged(failures):
    """[3] 缺票必须被记账：部分缺 → info；整批没回 → warning。"""
    codes = _codes(80)
    calls = []
    _, cap = _capture(lambda: _chunked_stats_fetch(
        codes, _make_skip_batch_fn({codes[7], codes[8]}, calls), _extract))
    infos = cap.messages(logging.INFO)
    if not any("未返回 2/80" in m and codes[7] in m for m in infos):
        failures.append(f"[3] 缺票未按 info 记账（含代码清单），实际 info={infos[:3]}")
        print(f"[FAIL] [3] info 日志缺失: {infos[:3]}")
        return
    calls2 = []
    _, cap2 = _capture(lambda: _chunked_stats_fetch(
        codes, _make_skip_batch_fn(set(codes), calls2), _extract))
    warns = cap2.messages(logging.WARNING)
    if not any("整批 80 只均未返回" in m for m in warns):
        failures.append(f"[3] 整批缺失未按 warning 记账，实际 warning={warns[:3]}")
        print(f"[FAIL] [3] 整批缺失无 warning: {warns[:3]}")
        return
    print("[PASS] [3] 缺票被记账：部分缺 → info（含代码清单），整批缺失 → warning")


def test_transport_error_not_split(failures):
    """[4] 传输类异常：整批重试 1 次、仍失败才跳过（2 次请求，不拆段）。"""
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
        print("[PASS] [4] 传输类异常：重试 1 次仍失败即整批跳过（2 次请求，不拆成 80 次超时）")


def test_empty(failures):
    """[5] 空输入：0 次请求。"""
    calls = []
    got = _chunked_stats_fetch([], _make_skip_batch_fn(set(), calls), _extract)
    if calls or got:
        failures.append(f"[5] 空输入应 0 次请求，实际 {len(calls)} 次")
        print(f"[FAIL] [5] 空输入: {len(calls)} 次")
    else:
        print("[PASS] [5] 空输入：0 次请求")


def test_salvage_returns_bad_detail(failures):
    """[6] _salvage_fetch 直接调用：bad 带可读原因，rows 覆盖全部好票。"""
    codes = _codes(20)
    bad_code = codes[5]
    calls = []
    rows, bad = _salvage_fetch(codes, _make_batch_fn({bad_code}, calls))
    ok_codes = {r.full_code for r in rows}
    if [c for c, _ in bad] != [bad_code]:
        failures.append(f"[6] bad 列表不符: {bad}")
        print(f"[FAIL] [6] bad: {bad}")
    elif ok_codes != set(codes) - {bad_code}:
        failures.append(f"[6] rows 覆盖不全: 缺 {sorted(set(codes) - {bad_code} - ok_codes)}")
        print("[FAIL] [6] rows 覆盖不全")
    elif not bad[0][1].strip() or bad_code in ok_codes:
        failures.append("[6] bad 缺少原因文本或毒丸进了结果")
        print("[FAIL] [6] 原因文本 / 毒丸泄漏")
    else:
        print(f"[PASS] [6] _salvage_fetch：bad={bad_code} 原因文本在位，好票 19 只齐全")


def test_stats_batch_constant(failures):
    """[7] 分批上限仍是 80（协议实测稳定值，改动会影响服务端截断行为）。"""
    if STATS_BATCH != 80:
        failures.append(f"[7] STATS_BATCH 被改为 {STATS_BATCH}（实测 200 会被服务端截断）")
        print(f"[FAIL] [7] STATS_BATCH = {STATS_BATCH}")
    else:
        print("[PASS] [7] STATS_BATCH 仍为 80")


def test_dirty_code_filtered(failures):
    """[8] 错位载荷的脏 code 不得进结果键，且必须告警。"""
    from DataAPI.ElTdxAPI import _finance_shares_map, _snapshot_price_map

    recs = [
        types.SimpleNamespace(exchange="sh", code="601066", circulating_shares=123.0),
        types.SimpleNamespace(exchange="sh", code='tyle["', circulating_shares=999.0),
        types.SimpleNamespace(exchange="sz", code="\x00\x00\x00\x00\x00\x00",
                              circulating_shares=5.0),
        types.SimpleNamespace(exchange="sh", code=None, circulating_shares=7.0),
        types.SimpleNamespace(exchange="sh", code="6010", circulating_shares=8.0),
    ]
    rows = [
        types.SimpleNamespace(full_code="sh601066", last_price=22.9),
        types.SimpleNamespace(full_code="shxk7DX+", last_price=1.0),
        types.SimpleNamespace(full_code="sh6010", last_price=1.0),
        types.SimpleNamespace(full_code="sh\u0000\u0000\u0000\u0000\u0000\u0000", last_price=1.0),
    ]
    (shares, prices), cap = _capture(lambda: (_finance_shares_map(recs),
                                              _snapshot_price_map(rows)))
    warns = cap.messages(logging.WARNING)
    if list(shares) != ["sh601066"] or list(prices) != ["sh601066"]:
        failures.append(f"[8] 脏 code 未被挡住: shares={list(shares)} prices={list(prices)}")
        print(f"[FAIL] [8] shares={list(shares)} prices={list(prices)}")
    elif not any("0x0010 出现 4 条" in m for m in warns) or \
            not any("0x054c 出现 3 条" in m for m in warns):
        failures.append(f"[8] 脏 code 被静默丢弃（未告警），实际 warning={warns[:3]}")
        print(f"[FAIL] [8] 未告警: {warns[:3]}")
    else:
        print("[PASS] [8] 脏 code（HTML/CSS 片段、NUL 占位、非 6 位）挡在结果键外且告警")


def test_transport_retry_once_succeeds(failures):
    """[9] 传输类抖动重试 1 次成功：整批数据完整取回。"""
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
        failures.append(f"[9] 传输抖动重试后应取回 80 只（2 次请求），实际 {len(calls)} 次 / 缺 {len(missing)} 只")
        print(f"[FAIL] [9] 传输抖动: {len(calls)} 次 / 缺 {len(missing)} 只")
    else:
        print("[PASS] [9] 传输类抖动：重试 1 次成功，80 只完整取回（瞬时抖动不丢批）")


def test_retry_turns_protocol_salvages(failures):
    """[10] 首轮传输错 + 重试抛协议错：必须转入拆段补救，不得整批丢弃。

    机制：前一个请求失败会让紧随其后的请求读到异常帧，一次异常即可触发
    ProtocolError —— 「首轮超时 + 重试协议错」是同一故障链的相邻两跳。
    """
    codes = _codes(80)
    bad_code = codes[37]
    calls = []
    state = {"n": 0}

    def timeout_then_protocol(codes_):
        calls.append(len(codes_))
        state["n"] += 1
        if state["n"] == 1:
            raise TimeoutError("read timed out")
        for c in codes_:
            if c == bad_code:
                raise ProtocolError(f"finance batch invalid record: {c}")
        return [_record(c) for c in codes_]

    got = _chunked_stats_fetch(codes, timeout_then_protocol, _extract)
    good = [c for c in codes if c != bad_code]
    missing = [c for c in good if c not in got]
    if missing:
        failures.append(f"[10] 重试抛协议错应转入拆段（取回 {len(got)}/79），"
                        f"实际丢 {len(missing)} 只: {missing[:5]}")
        print(f"[FAIL] [10] 取回 {len(got)} 只 / 请求 {len(calls)} 次")
    elif bad_code in got:
        failures.append("[10] 坏码不应出现在结果里")
        print("[FAIL] [10] 坏码未剔除")
    elif len(calls) > 21:
        failures.append(f"[10] 请求次数 {len(calls)} 超过 21（未转入拆段？）")
        print(f"[FAIL] [10] 请求次数 {len(calls)}")
    else:
        print(f"[PASS] [10] 重试抛协议错：转入拆段补救，79 只好票全部取回，请求 {len(calls)} 次")


def test_single_bad_code(failures):
    """[11] 单颗坏码：好票零丢失，请求 ≤ 20（逐只是 80）。"""
    codes = _codes(80)
    bad_code = codes[37]
    calls = []
    got = _chunked_stats_fetch(codes, _make_batch_fn({bad_code}, calls), _extract)
    missing = [c for c in codes if c != bad_code and c not in got]
    if bad_code in got:
        failures.append("[11] 坏码不应出现在结果里")
        print("[FAIL] [11] 坏码未剔除")
    elif missing:
        failures.append(f"[11] 好票数据丢失 {len(missing)} 只: {missing[:5]}")
        print(f"[FAIL] [11] 好票丢失 {len(missing)} 只")
    elif len(calls) > 20:
        failures.append(f"[11] 请求次数 {len(calls)} 超过 20（拆段未生效？）")
        print(f"[FAIL] [11] 请求次数 {len(calls)}")
    else:
        print(f"[PASS] [11] 单颗坏码：79 只全部取回，请求 {len(calls)} 次"
              f"（1 次首轮 + ceil(80/{_SALVAGE_SUBCHUNK}) 段 + 段内逐只）")


def test_multi_bad_codes(failures):
    """[12] 多颗坏码（含批首批尾）：全部隔离、好票零丢失。"""
    codes = _codes(80)
    poison = {codes[0], codes[37], codes[79]}
    calls = []
    got = _chunked_stats_fetch(codes, _make_batch_fn(poison, calls), _extract)
    missing = [c for c in codes if c not in poison and c not in got]
    leaked = [c for c in poison if c in got]
    if missing or leaked:
        failures.append(f"[12] 多坏码处理错: 丢失 {len(missing)} 只 / 未剔除 {len(leaked)} 只")
        print(f"[FAIL] [12] 丢失 {len(missing)} / 未剔除 {len(leaked)}")
    else:
        print(f"[PASS] [12] 多颗坏码（头/中/尾）：77 只全部取回，请求 {len(calls)} 次")


def test_whole_batch_fault_stops(failures):
    """[13] 整批故障：拆段后所有段都失败 ⇒ 立刻止损、不逐只、账目 n/n 无重复。"""
    codes = _codes(80)
    calls = []

    def always_protocol_error(codes_):
        calls.append(len(codes_))
        raise ProtocolError("finance batch response count exceeds request: 0 > 80")

    rows, bad = _salvage_fetch(codes, always_protocol_error)
    bad_codes = [c for c, _ in bad]
    limit = -(-len(codes) // _SALVAGE_SUBCHUNK)      # ceil(80/8) = 10
    if rows or len(bad) != 80 or len(set(bad_codes)) != 80 or len(calls) != limit:
        failures.append(f"[13] 整批故障应只拆段不逐只（{limit} 次），"
                        f"实际 rows={len(rows)}、bad={len(bad)}、去重 {len(set(bad_codes))}、"
                        f"请求 {len(calls)}")
        print(f"[FAIL] [13] rows={len(rows)} bad={len(bad)} 请求={len(calls)}")
    else:
        print(f"[PASS] [13] 整批故障：拆段全失败即止损，bad 80 只无重复，"
              f"请求 {len(calls)} 次（旧的二分 + 探测是 76 次应用层调用）")


def test_fetch_float_mc_uses_skip(failures):
    """[14] fetch_float_mc 必须以 on_missing="skip" 调两条批量命令。"""
    seen = []

    class _FakeBatch:
        def __init__(self, kind):
            self.kind = kind

        def __call__(self, codes, **kwargs):
            seen.append((self.kind, kwargs.get("on_missing")))
            if self.kind == "finance":
                return types.SimpleNamespace(records=tuple(
                    types.SimpleNamespace(full_code=c, exchange=c[:2], code=c[2:],
                                          circulating_shares=1e8)
                    for c in codes))
            return tuple(_record(c) for c in codes)

    class _FakeClient:
        def __init__(self):
            self.corporate = types.SimpleNamespace(finance_batch=_FakeBatch("finance"))
            self.quotes = types.SimpleNamespace(get_snapshots=_FakeBatch("snapshot"))

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    orig_client = eltdx_api._ensure_eltdx_client
    orig_compat = eltdx_api._check_eltdx_api_compat
    eltdx_api._ensure_eltdx_client = lambda: _FakeClient()
    eltdx_api._check_eltdx_api_compat = lambda client: client
    try:
        out = eltdx_api.fetch_float_mc([{"code": "600000", "prefix": "1"},
                                        {"code": "000001", "prefix": "0"}])
    finally:
        eltdx_api._ensure_eltdx_client = orig_client
        eltdx_api._check_eltdx_api_compat = orig_compat

    kinds = dict(seen)
    wrong = {k: v for k, v in kinds.items() if v != "skip"}
    if wrong or set(kinds) != {"finance", "snapshot"}:
        failures.append(f"[14] 批量调用未传 on_missing='skip'：{seen}")
        print(f"[FAIL] [14] on_missing 传参: {seen}")
    elif len(out) != 2:
        failures.append(f"[14] 市值应为 2 只，实际 {len(out)}：{out}")
        print(f"[FAIL] [14] 结果 {out}")
    else:
        print("[PASS] [14] fetch_float_mc 两条批量命令都传 on_missing='skip'（缺票语义的源头）")


def test_salvage_transport_no_fanout(failures):
    """[15] 补救途中遇上网络故障：不得逐只放大，立即收手、只记未取到的代码。"""
    codes = _codes(80)
    calls = []
    state = {"n": 0}

    def first_ok_then_timeout(codes_):
        calls.append(len(codes_))
        state["n"] += 1
        if state["n"] == 1:
            return [_record(c) for c in codes_]
        raise TimeoutError("read timed out")

    rows, bad = _salvage_fetch(codes, first_ok_then_timeout)
    got = {r.full_code for r in rows}
    bad_codes = [c for c, _ in bad]
    if len(calls) != 2:
        failures.append(f"[15] 补救途中断连不应逐只（应 2 次请求），实际 {len(calls)} 次")
        print(f"[FAIL] [15] 请求 {len(calls)} 次")
    elif set(bad_codes) & got or set(bad_codes) | got != set(codes):
        failures.append(f"[15] 账目应「已取到的进 rows、未取到的进 bad」且不重叠，"
                        f"实际 rows={len(got)} / bad={len(bad_codes)} / 重叠 "
                        f"{len(set(bad_codes) & got)}")
        print(f"[FAIL] [15] rows={len(got)} bad={len(bad_codes)}")
    else:
        print(f"[PASS] [15] 补救途中断连：2 次请求即收手（原会逐个再打 {len(codes)} 次），"
              f"rows {len(got)} / bad {len(bad_codes)} 账目无重叠")


def test_transport_after_protocol_no_dup(failures):
    """[16] 逐只阶段「先一只协议错、随后另一只传输错」：同一 code 不得记两条 bad。

    [15] 的传输错发生在**段阶段**，那时 `bad` 还是空的，按 rows 反推未取到的
    代码已经够用；逐只阶段 `bad` 非空，那只先前协议失败的票并不在 rows 里，
    只按 rows 过滤就会再记一次 —— 日志失败只数虚报，而 `_salvage_or_drop`
    的计数与预览直接读这个列表。
    """
    codes = _codes(16)
    calls = []
    first_piece = set(codes[:8])
    poisoned = codes[8]
    timed_out = codes[9]

    def batch_fn(codes_):
        codes_ = list(codes_)
        calls.append(len(codes_))
        if len(codes_) > 1:
            if set(codes_) == first_piece:
                return [_record(c) for c in codes_]
            raise ProtocolError("finance batch unexpected or duplicate record")
        if codes_[0] == poisoned:
            raise ProtocolError("invalid record: all-zero placeholder")
        if codes_[0] == timed_out:
            raise TimeoutError("read timed out")
        return [_record(c) for c in codes_]

    rows, bad = _salvage_fetch(codes, batch_fn)
    bad_codes = [c for c, _ in bad]
    dup = sorted(c for c in set(bad_codes) if bad_codes.count(c) > 1)
    got = {r.full_code for r in rows}
    if dup:
        failures.append(f"[16] 逐只阶段混合路径重复记账：{dup}（bad {len(bad)} 条 / "
                        f"不同 code {len(set(bad_codes))} 个）")
        print(f"[FAIL] [16] 重复记账 {dup}：bad={len(bad)} 去重={len(set(bad_codes))}")
    elif len(got) != 8 or set(bad_codes) & got:
        failures.append(f"[16] 账目错：rows={len(got)}、bad={len(bad_codes)}、"
                        f"重叠 {len(set(bad_codes) & got)}")
        print(f"[FAIL] [16] rows={len(got)} bad={len(bad_codes)}")
    else:
        print(f"[PASS] [16] 逐只阶段先协议错后传输错：bad {len(bad)} 条对应 "
              f"{len(set(bad_codes))} 个 code，无重复、与 rows 不重叠")


def test_small_batch_recovers(failures):
    """[17] n <= 拆段粒度（自选股 / 小板块）遇载荷异常毒丸：必须救回好票。

    整批只有一段时「所有段都失败」没有判别力 —— 按它止损会把 5 只全记 bad，
    市值列整列 `--`；逐只最多再打 n 次请求即可救回。
    """
    codes = _codes(5)
    calls = []
    poison = {codes[2]}
    rows, bad = _salvage_fetch(codes, _make_batch_fn(poison, calls))
    got = {r.full_code for r in rows}
    expect_calls = 1 + len(codes)          # 1 段 + 逐只 5 次
    if got != set(codes) - poison:
        failures.append(f"[17] 小批应救回 {len(codes) - 1} 只，实际 {len(got)} 只"
                        f"（整批丢）")
        print(f"[FAIL] [17] 只救回 {len(got)}/{len(codes)}")
    elif len(bad) != 1 or len(calls) != expect_calls:
        failures.append(f"[17] 账目/请求数错：bad={len(bad)}（应 1）、"
                        f"请求 {len(calls)} 次（应 {expect_calls}）")
        print(f"[FAIL] [17] bad={len(bad)} 请求={len(calls)}")
    else:
        print(f"[PASS] [17] {len(codes)} 只小批遇 1 颗毒丸：救回 {len(got)} 只，"
              f"请求 {len(calls)} 次（按段数止损会 0 只救回）")


def test_small_batch_all_bad_bounded(failures):
    """[18] n <= 拆段粒度且每只都失败：逐只有界（n 次），账目无重复。"""
    codes = _codes(8)
    calls = []
    rows, bad = _salvage_fetch(codes, _make_batch_fn(set(codes), calls))
    bad_codes = [c for c, _ in bad]
    expect_calls = 1 + len(codes)
    if rows or len(bad) != len(codes) or len(set(bad_codes)) != len(codes):
        failures.append(f"[18] rows={len(rows)}、bad={len(bad)}、"
                        f"去重 {len(set(bad_codes))}（应 0 / 8 / 8）")
        print(f"[FAIL] [18] rows={len(rows)} bad={len(bad)}")
    elif len(calls) != expect_calls:
        failures.append(f"[18] 请求 {len(calls)} 次，应 {expect_calls} 次（不得放大）")
        print(f"[FAIL] [18] 请求={len(calls)}")
    else:
        print(f"[PASS] [18] {len(codes)} 只全坏：逐只有界 {len(calls)} 次请求，"
              f"bad {len(set(bad_codes))} 只无重复")


def test_supports_on_missing_checks_both(failures):
    """[19] 能力探测必须覆盖两条批量命令：只给一条加 on_missing 即判不支持。"""
    def snap_new(codes, on_missing="raise"):
        return ()

    def fin_new(codes, on_missing="raise"):
        return ()

    def fin_old(codes):
        return ()

    def probe(quotes_fn, finance_fn):
        eltdx_api._ON_MISSING_SUPPORTED = None
        fake = types.SimpleNamespace(
            quotes=types.SimpleNamespace(get_snapshots=quotes_fn),
            corporate=types.SimpleNamespace(finance_batch=finance_fn))
        return eltdx_api._supports_on_missing(fake)

    try:
        both = probe(snap_new, fin_new)
        only_quotes = probe(snap_new, fin_old)
        only_finance = probe(fin_old, fin_new)
    finally:
        eltdx_api._ON_MISSING_SUPPORTED = None      # 还原真实探测缓存

    if not both or only_quotes or only_finance:
        failures.append(f"[19] 探测未覆盖两条命令：both={both}、"
                        f"仅 quotes={only_quotes}、仅 finance={only_finance}"
                        f"（后两者应为 False）")
        print(f"[FAIL] [19] both={both} only_quotes={only_quotes} "
              f"only_finance={only_finance}")
    else:
        print("[PASS] [19] on_missing 探测覆盖 quotes + corporate："
              "缺任一条即判不支持")


def main():
    failures = []
    test_clean_batch(failures)
    test_missing_codes_skipped(failures)
    test_missing_codes_are_logged(failures)
    test_transport_error_not_split(failures)
    test_empty(failures)
    test_salvage_returns_bad_detail(failures)
    test_stats_batch_constant(failures)
    test_dirty_code_filtered(failures)
    test_transport_retry_once_succeeds(failures)
    test_retry_turns_protocol_salvages(failures)
    test_single_bad_code(failures)
    test_multi_bad_codes(failures)
    test_whole_batch_fault_stops(failures)
    test_fetch_float_mc_uses_skip(failures)
    test_salvage_transport_no_fanout(failures)
    test_transport_after_protocol_no_dup(failures)
    test_small_batch_recovers(failures)
    test_small_batch_all_bad_bounded(failures)
    test_supports_on_missing_checks_both(failures)
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
