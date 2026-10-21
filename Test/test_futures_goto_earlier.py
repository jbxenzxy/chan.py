# -*- coding: utf-8 -*-
"""Test/test_futures_goto_earlier.py —— 期货复盘「目标早于窗口左界」四类诊断
=====================================================================
2026-10-17 v1.17：复盘输入日期 D 早于窗口左界 L 时**不改 L**（方案 §2 规则⑥），
而是按「L 被谁钉住」分四类，提示与出路各不相同（详见 Docs/选点&复盘方案v1.18.md §3.6）：

| code              | 触发条件                                   | 出路                     |
|-------------------|--------------------------------------------|--------------------------|
| contract_range    | 具体合约：D 不在该合约存续期（上市~交割）   | 切换到建议合约（最近交割优先）|
| tq_10000          | D 早于该周期「最近 10000 根」首根           | 切换建议合约 / 改用 5m/30m |
| before_selection  | 方式D：窗口以选点为界，D 早于选点           | 先取消选点                |
| way_c             | 方式C：窗口 = 最近 N 根（N<10000），D 可达   | **仅提示**「请加载更多数据」 |

判定顺序 = 硬约束优先（合约 → 10000 → 选点 → 方式C）；事实全部取数据真值
（合约存续期 = 该合约 30m 10000 根首/末根；10000 边界 = 当前周期首根；选点 = CSV）。
2026-10-17 v1.18：建议合约**最近交割优先**——最近候选存续期覆盖由生成规则结构保证
（免探），只验当前周期窗口；换月窗口内（≤15 日历天）与次月比目标日 vol，窗口外直接
当月；最近候选不覆盖才回退全扫 + vol 仲裁（异常兜底）。suggest 恒为 str（前端契约）。
2026-10-19 v1.19：**晚向（A′）在前端实时态不可达**的现场缺陷——`wantLive` 把「D ≥ 末根」
一律钳成回实时，已交割合约的 D 根本到不了后端（IF2409 1m + 日历 2024-09-23：图不变、
日历跳回末根、无提示）。修法两处：前端 wantLive 加合取项「D ≥ 今天」；后端晚向加
`_futures_series_stale` 守卫（放行后「查上周六」不得误报 A′）。见 [6]。

分层：[1] 合约推荐纯函数 / [2] 四类诊断（FakeSrc）/ [3] 源码契约
     / [5] init 晚向接线 / [6] 晚向守卫（休市 vs 停更）
     / [4] 真浏览器（tq_10000 切换合约、way_c 仅提示）。
运行：python Test/test_futures_goto_earlier.py
"""
import functools
import http.server
import json
import os
import sys
import threading
from datetime import datetime, timedelta

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

from Test.snapshot_runner import isolate_side_effects

APP_JS = os.path.join(REPO_ROOT, "Frontend", "app.js")
SSE_PY = os.path.join(REPO_ROOT, "App", "AppSSE.py")
SRC_PY = os.path.join(REPO_ROOT, "DataAPI", "TqSdkCSSESource.py")
FRONTEND_DIR = os.path.join(REPO_ROOT, "Frontend")
SYMBOL = "CFFEX.IF2409"
TARGET = datetime(2024, 4, 22, 15, 0)


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read().replace("\r\n", "\n")


def _span(first, last, vol=100, mid=None):
    """只给首/中/末三根：分类器读 [0]（边界）、[-1]（末根）、mid 那根的 vol（主力判定）。"""
    recs = [{"dt": first, "vol": vol}]
    if mid is not None:
        recs.append({"dt": mid, "vol": vol})
    recs.append({"dt": last, "vol": vol})
    return recs


class _FakeSrc:
    """按 (symbol, freq_sec) 返回预置 K 线；只服务 _classify_futures_replay 单测。"""

    def __init__(self, bars):
        self.bars = bars
        self.api = object()            # 默认「已连接」：诊断可直接取数
        self._futures_series_cache = {}   # 存续期与窗口探针共用这一份（2026-10-18）

    def fetch_kline(self, symbol, freq_sec=15, num_bars=None, display_key=None, start_time=None):
        if self.api is None:           # 与真实数据源一致：未连接就取不了数
            raise AttributeError("'NoneType' object has no attribute 'get_kline_serial'")
        recs = self.bars.get((symbol, freq_sec), [])
        return recs[-num_bars:] if num_bars else recs


class _LazySrc(_FakeSrc):
    """真实时序：倒挂分类发生在 `src.connect()` 之前（`api` 还是 None）。

    这是 2026-10-17 现场日志暴露的缺陷——不补连接，A/B 两个探针全失败，
    分类会退化成方式C「请加载更多数据」（配置已用满 10000 根时自相矛盾）。
    """

    def __init__(self, bars):
        super().__init__(bars)
        self.api = None
        self.connect_calls = 0

    def connect(self):
        self.connect_calls += 1
        if self.api is None:
            self.api = object()


class _CountSrc(_FakeSrc):
    """带 fetch_kline 计数的桩：钉死「最近优先」的取数分档（方案 v1.18 §5 / 评审稿 §11）。

    没有它，2~4 次这个唯一可量化收益没有断言保护——以后谁加一个探针，
    门禁不会红，收益悄悄退回 9~10，文档却还写着 2~4。
    """

    def __init__(self, bars):
        super().__init__(bars)
        self.fetch_calls = []

    def fetch_kline(self, symbol, freq_sec=15, num_bars=None, display_key=None, start_time=None):
        self.fetch_calls.append((symbol, freq_sec))
        return super().fetch_kline(symbol, freq_sec=freq_sec, num_bars=num_bars,
                                   display_key=display_key, start_time=start_time)


def _fake_2409():
    """IF2409：存续 2024/03/18 ~ 09/20；1m 最近 10000 根自 07/19；5m 自 2023/11/24。"""
    return _FakeSrc({
        (SYMBOL, 1800): _span(datetime(2024, 3, 18, 9, 30), datetime(2024, 9, 20, 14, 30)),
        (SYMBOL, 60): _span(datetime(2024, 7, 19, 9, 30), datetime(2024, 9, 20, 14, 59)),
        (SYMBOL, 300): _span(datetime(2023, 11, 24, 9, 30), datetime(2024, 9, 20, 14, 55)),
        # IF2404：2024/02/19 上市 ~ 04/19 交割（用于 contract_range「晚于最后交易日」）
        ("CFFEX.IF2404", 1800): _span(datetime(2024, 2, 19, 9, 30), datetime(2024, 4, 19, 14, 30)),
        ("CFFEX.IF2404", 60): _span(datetime(2024, 4, 1, 9, 30), datetime(2024, 4, 19, 14, 59),
                                    vol=50, mid=datetime(2024, 4, 22, 9, 30)),
        # IF2402：交割 2024/02/16（用于「早于 IF2409 上市日」的建议合约）
        ("CFFEX.IF2402", 1800): _span(datetime(2024, 1, 22, 9, 30), datetime(2024, 2, 16, 14, 30)),
        ("CFFEX.IF2402", 60): _span(datetime(2024, 1, 22, 9, 30), datetime(2024, 2, 16, 14, 59)),
        # IF2405：交割 2024/05/17；1m 的 10000 根自 03/20 ⇒ 04-22 可取（B 类的建议合约）
        ("CFFEX.IF2405", 1800): _span(datetime(2024, 3, 18, 9, 30), datetime(2024, 5, 17, 14, 30)),
        ("CFFEX.IF2405", 60): _span(datetime(2024, 3, 20, 9, 30), datetime(2024, 5, 17, 14, 59),
                                    vol=500, mid=datetime(2024, 4, 22, 9, 30)),
        ("CFFEX.IF2405", 300): _span(datetime(2024, 3, 18, 9, 30), datetime(2024, 5, 17, 14, 55)),
    })


# ═══════════════════════ [1] 合约推荐纯函数 ═══════════════════════

def test_contract_suggest():
    from App.AppSSE import (_third_friday, _contract_candidates,
                            _contract_candidates_with_expiry, _CFFEX_ROLL_WINDOW_DAYS,
                            _suggest_contract_for_date, _suggest_contract_with_expiry,
                            _classify_futures_replay)

    assert _third_friday(2024, 4).strftime("%Y-%m-%d") == "2024-04-19"
    assert _third_friday(2024, 5).strftime("%Y-%m-%d") == "2024-05-17"
    assert _CFFEX_ROLL_WINDOW_DAYS == 15, "换月窗口 15 日历天（评审定稿值）"

    # 候选月份：04-22 已过 4 月交割日（04-19）→ 首个候选必须是 2405（不能是 2404）
    cand = _contract_candidates("CFFEX.IF2409", TARGET)
    assert cand[0] == "CFFEX.IF2405", cand
    assert "CFFEX.IF2404" not in cand, cand

    # CFFEX 只挂牌「当月 / 下月 / 两个季月」（季月=3/6/9/12）：
    # 候选必须正好是这 4 个真实合约 —— 既不能出现 IF2407/2408（不挂牌），也不能漏 IF2409/2412。
    # 4 个 = 该日全部在市合约 ⇒ IF/IH/IC/IM 的「该日主力合约」必定落在候选里（不会漏主力）。
    for root in ("IF", "IH", "IC", "IM"):
        got = _contract_candidates(f"CFFEX.{root}2409", TARGET)
        assert got == [f"CFFEX.{root}2405", f"CFFEX.{root}2406",
                       f"CFFEX.{root}2409", f"CFFEX.{root}2412"], (root, got)
    # 交割日当天仍归当月；跨年 / 季月末边界
    assert _contract_candidates("CFFEX.IF2409", datetime(2024, 4, 19))[0] == "CFFEX.IF2404"
    assert _contract_candidates("CFFEX.IF2409", datetime(2024, 3, 1))[0] == "CFFEX.IF2403"
    assert _contract_candidates("CFFEX.IF2409", datetime(2024, 12, 23)) == \
        ["CFFEX.IF2501", "CFFEX.IF2502", "CFFEX.IF2503", "CFFEX.IF2506"], "跨年季月边界"
    assert _contract_candidates("CFFEX.IF2409", datetime(2024, 11, 5))[2:] == \
        ["CFFEX.IF2503", "CFFEX.IF2506"], "下月为季月时不能重复计入"
    # 非 CFFEX：按交易所代码格式生成（品种根大小写保留、郑商所三位月份）
    shfe = _contract_candidates("SHFE.rb2405", TARGET)
    assert shfe[0] == "SHFE.rb2404" and "SHFE.rb2405" in shfe, shfe
    assert _contract_candidates("CZCE.MA405", TARGET)[0] == "CZCE.MA404"
    assert _contract_candidates("", TARGET) == []

    # 候选 + 交割日（v1.18）：CFFEX 在生成处随附第三个周五（零解析、零失败路径）；
    # 公开形状（纯 code）与内部形状一一对应；非 CFFEX 交割日不可推算 → None
    cand_ex = _contract_candidates_with_expiry("CFFEX.IF2409", TARGET)
    assert [c for c, _ in cand_ex] == cand, "公开形状与内部形状候选一致"
    assert dict(cand_ex)["CFFEX.IF2405"] == datetime(2024, 5, 17).date(), "交割日在生成处随附"
    assert all(exp >= TARGET.date() for _, exp in cand_ex), "交割日 ≥ 目标日（已剔已交割月份）"
    assert dict(_contract_candidates_with_expiry("CFFEX.IF2409", datetime(2024, 4, 19)))[
        "CFFEX.IF2404"] == datetime(2024, 4, 19).date(), "交割日当天 days=0"
    ex_dec = _contract_candidates_with_expiry("CFFEX.IF2409", datetime(2024, 12, 23))
    assert dict(ex_dec)["CFFEX.IF2503"] == datetime(2025, 3, 21).date(), "跨年候选交割日随附"
    assert all(exp is None for _, exp in _contract_candidates_with_expiry("SHFE.rb2405", TARGET)), \
        "非 CFFEX 交割日不可推算 → None"

    # 国债（T/TF/TS/TL）走**另一套**规则：最后交易日 = 第二周五，挂牌 = 最近三个季月。
    # 2026-10-18 修：混用股指规则会让候选出现 T2404（不挂牌）、交割日算成第三周五，
    # 且建议的合约切过去仍被判「不在存续期」（自指 ⇒ 切换死循环）。
    for root in ("T", "TF", "TS", "TL"):
        got = _contract_candidates(f"CFFEX.{root}2409", datetime(2024, 3, 10))
        assert got == [f"CFFEX.{root}2406", f"CFFEX.{root}2409", f"CFFEX.{root}2412"], \
            (root, got)
    # 03-05 在第二周五（03-08）之前 ⇒ 当季 2403 仍在市，候选从 2403 起
    assert _contract_candidates("CFFEX.T2409", datetime(2024, 3, 5)) == \
        ["CFFEX.T2403", "CFFEX.T2406", "CFFEX.T2409"], "国债交割日前仍归当季"
    ex_bond = dict(_contract_candidates_with_expiry("CFFEX.T2409", datetime(2024, 3, 10)))
    assert ex_bond["CFFEX.T2406"] == datetime(2024, 6, 14).date(), \
        f"国债 2406 最后交易日是第二周五 06/14（非第三周五），实为 {ex_bond}"
    assert dict(_contract_candidates_with_expiry("CFFEX.T2409", datetime(2024, 3, 5)))[
        "CFFEX.T2403"] == datetime(2024, 3, 8).date(), "国债 2403 最后交易日 03/08"
    # 股指规则不受影响（第三周五仍由 _third_friday 出口）
    assert _contract_candidates("CFFEX.IF2409", datetime(2024, 3, 10)) == \
        ["CFFEX.IF2403", "CFFEX.IF2404", "CFFEX.IF2406", "CFFEX.IF2409"], "股指仍是当月+下月+两季月"

    # 数据验证：只有「存续期覆盖 + 当前周期 10000 根覆盖」的候选才会被建议
    src = _fake_2409()
    assert _suggest_contract_for_date(src, SYMBOL, 60, TARGET) == "CFFEX.IF2405"
    sug, exp = _suggest_contract_with_expiry(src, SYMBOL, 60, TARGET)
    assert sug == "CFFEX.IF2405" and isinstance(sug, str), "suggest 恒为 str（前端契约）"
    assert exp == datetime(2024, 5, 17).date(), "随附交割日供文案「最后交易日约」"
    assert _suggest_contract_for_date(src, "KQ.m@CFFEX.IF", 60, TARGET) == "CFFEX.IF2405"
    # 15s：候选的 10000 根只覆盖约 7 个交易日 → 04-22 取不到 → 不给建议（退化为换周期）
    assert _suggest_contract_for_date(src, SYMBOL, 15, TARGET) == ""

    # (a) 非换月窗口（04-22 距 IF2405 交割 25 天 > 15）+ 全覆盖 → **直接取最近候选（当月）**，
    #     不扫其余三个：即便次月/季月 vol 更大也不仲裁（窗口外当月为量头是经验规律）
    for root, hot in (("IF", "2406"), ("IH", "2406"), ("IC", "2406"), ("IM", "2409")):
        feed = {}
        for mm in ("2405", "2406", "2409", "2412"):
            vol = 900 if mm == hot else 100
            feed[(f"CFFEX.{root}{mm}", 1800)] = _span(
                datetime(2024, 3, 18, 9, 30), datetime(2024, 12, 20, 14, 30))
            feed[(f"CFFEX.{root}{mm}", 60)] = _span(
                datetime(2024, 2, 1, 9, 30), datetime(2024, 9, 20, 14, 59),
                vol=vol, mid=datetime(2024, 4, 22, 9, 30))
        got = _suggest_contract_for_date(_FakeSrc(feed), f"CFFEX.{root}2409", 60, TARGET)
        assert got == f"CFFEX.{root}2405", \
            f"{root} 非换月窗口应直接取最近候选（当月），实为 {got!r}"

    # (b) 换月窗口（05-14 距 IF2405 交割 05-17 仅 3 天 ≤ 15）→ 与次月比目标日 vol：
    #     次月量更大 → 建议次月（真主力）；次月量更小 → 仍当月
    def _roll_feed(v05, v06):
        return _FakeSrc({
            ("CFFEX.IF2405", 1800): _span(datetime(2024, 3, 18, 9, 30), datetime(2024, 5, 17, 14, 30)),
            ("CFFEX.IF2405", 60): _span(datetime(2024, 3, 20, 9, 30), datetime(2024, 5, 17, 14, 59),
                                        vol=v05, mid=datetime(2024, 5, 14, 9, 30)),
            ("CFFEX.IF2406", 1800): _span(datetime(2024, 4, 22, 9, 30), datetime(2024, 6, 21, 14, 30)),
            ("CFFEX.IF2406", 60): _span(datetime(2024, 4, 1, 9, 30), datetime(2024, 6, 21, 14, 59),
                                        vol=v06, mid=datetime(2024, 5, 14, 9, 30)),
        })
    roll_day = datetime(2024, 5, 14, 9, 30)
    sug, exp = _suggest_contract_with_expiry(_roll_feed(100, 900), SYMBOL, 60, roll_day)
    assert (sug, exp) == ("CFFEX.IF2406", datetime(2024, 6, 21).date()), \
        f"换月窗口内次月量更大应建议次月，实为 {(sug, exp)!r}"
    assert _suggest_contract_for_date(_roll_feed(900, 100), SYMBOL, 60, roll_day) == "CFFEX.IF2405", \
        "换月窗口内次月量更小仍建议当月"

    # (c) 回退形态（**异常兜底**：1m/5m/30m 下当月不覆盖数学上不可达（§4.1 结构保证），
    #     本用例靠构造「当月 60m 序列缺失」触发）→ 全扫 + vol 仲裁仍生效，不制造死路
    fallback = _FakeSrc({
        ("CFFEX.IF2405", 1800): _span(datetime(2024, 3, 18, 9, 30), datetime(2024, 5, 17, 14, 30)),
        ("CFFEX.IF2406", 1800): _span(datetime(2024, 4, 22, 9, 30), datetime(2024, 6, 21, 14, 30)),
        ("CFFEX.IF2406", 60): _span(datetime(2024, 4, 22, 9, 30), datetime(2024, 6, 21, 14, 59),
                                    vol=900, mid=datetime(2024, 4, 22, 9, 30)),
    })
    got = _suggest_contract_for_date(fallback, SYMBOL, 60, TARGET)
    assert got == "CFFEX.IF2406", f"回退全扫应按 vol 取覆盖最大者，实为 {got!r}"

    # (e) 国债切换不自指（2026-10-18 修 P1 的临床验真）：
    #     修前建议 T2403（D 当天已交割的当季合约），切过去又判 contract_range 又建议
    #     T2403 ⇒ 点几次「切换」都是同一个建议、出不去。修后建议 T2406 且切过去可达。
    _d_bond = datetime(2024, 3, 10, 10, 0)
    bond_feed = _FakeSrc({
        ("CFFEX.T2409", 1800): _span(datetime(2023, 9, 11, 9, 30), datetime(2024, 3, 8, 14, 30)),
        ("CFFEX.T2409", 60): _span(datetime(2024, 1, 2, 9, 30), datetime(2024, 3, 8, 14, 59)),
        ("CFFEX.T2406", 1800): _span(datetime(2023, 12, 11, 9, 30), datetime(2024, 6, 14, 14, 30)),
        ("CFFEX.T2406", 60): _span(datetime(2024, 1, 2, 9, 30), datetime(2024, 6, 14, 14, 59),
                                   vol=500, mid=datetime(2024, 3, 10, 9, 30)),
    })
    b = _classify_futures_replay(bond_feed, "CFFEX.T2409", 60, _d_bond,
                                 start_dt=datetime(2024, 2, 1, 9, 30))
    assert b is not None and b.code == "contract_range", (b and b.code)
    assert b.suggest == "CFFEX.T2406", f"国债应建议当季合约 T2406，实为 {b.suggest!r}"
    assert "06/14" in b.message, b.message
    assert _classify_futures_replay(bond_feed, "CFFEX.T2406", 60, _d_bond,
                                    start_dt=datetime(2024, 2, 1, 9, 30)) is None, \
        "切到建议合约后必须可达（修前会建议 T2403 并自指）"

    # 未连接的数据源（真实 SSE 时序）：探针必须先补一次连接，A/B 不得退化成方式C
    lazy = _LazySrc(_fake_2409().bars)
    b = _classify_futures_replay(lazy, SYMBOL, 60, TARGET,
                                 start_dt=datetime(2024, 7, 19, 9, 30))
    assert lazy.connect_calls == 1, f"探针应补一次连接（connect 幂等），实为 {lazy.connect_calls}"
    assert b is not None and b.code == "tq_10000", \
        f"未连接时 A/B 探针失败会误报方式C，实为 {b and b.code}"
    assert b.suggest == "CFFEX.IF2405" and isinstance(b.suggest, str), b.suggest

    # (d) 取数分档（探针计数桩，方案 v1.18 §5）：改前 9~10 次 → 2~4 次；谁加探针门禁就红
    from collections import Counter
    # A 类：页面 IF2404，D=04-22 → life(IF2404:1800) + 最近候选窗口(IF2405:60) = 2
    cnt = _CountSrc(_fake_2409().bars)
    _classify_futures_replay(cnt, "CFFEX.IF2404", 60, TARGET, start_dt=datetime(2024, 4, 1))
    assert Counter(cnt.fetch_calls) == Counter(
        {("CFFEX.IF2404", 1800), ("CFFEX.IF2405", 60)}), cnt.fetch_calls
    # B 类 · init（传 raw_first）→ life(IF2409:1800) + 最近候选窗口(IF2405:60) = 2
    cnt = _CountSrc(_fake_2409().bars)
    _classify_futures_replay(cnt, SYMBOL, 60, TARGET, raw_first=datetime(2024, 7, 19, 9, 30))
    assert Counter(cnt.fetch_calls) == Counter(
        {("CFFEX.IF2409", 1800), ("CFFEX.IF2405", 60)}), cnt.fetch_calls
    # B 类 · 倒挂（gotoDate 主路径，不传 raw_first）→ + 自身窗口(IF2409:60) = 3
    cnt = _CountSrc(_fake_2409().bars)
    _classify_futures_replay(cnt, SYMBOL, 60, TARGET, start_dt=datetime(2024, 7, 19, 9, 30))
    assert Counter(cnt.fetch_calls) == Counter(
        {("CFFEX.IF2409", 1800), ("CFFEX.IF2409", 60), ("CFFEX.IF2405", 60)}), cnt.fetch_calls
    # 换月窗口（D=05-14，距当月交割 3 天）→ 换 05-14 feed：+ 次月窗口(IF2406:60) = 4
    win_feed = dict(_roll_feed(100, 900).bars)
    win_feed[(SYMBOL, 1800)] = _span(datetime(2024, 3, 18, 9, 30), datetime(2024, 9, 20, 14, 30))
    win_feed[(SYMBOL, 60)] = _span(datetime(2024, 7, 19, 9, 30), datetime(2024, 9, 20, 14, 59))
    cnt = _CountSrc(win_feed)
    b = _classify_futures_replay(cnt, SYMBOL, 60, roll_day, start_dt=datetime(2024, 5, 20, 9, 30))
    assert b is not None and b.suggest == "CFFEX.IF2406", (b and b.code, b and b.suggest)
    assert Counter(cnt.fetch_calls) == Counter(
        {("CFFEX.IF2409", 1800), ("CFFEX.IF2409", 60),
         ("CFFEX.IF2405", 60), ("CFFEX.IF2406", 60)}), cnt.fetch_calls
    # 未来日期早退 → 零探针
    cnt = _CountSrc(_fake_2409().bars)
    sug, exp = _suggest_contract_with_expiry(cnt, SYMBOL, 60, datetime(2099, 1, 1))
    assert (sug, exp) == ("", None) and cnt.fetch_calls == [], (sug, exp, cnt.fetch_calls)

    # (f) 2026-10-18 修 P2-1：非 CFFEX 全扫的取数分档（改前 10 次 → 5 次）
    #     每个候选只取 1 份当前周期序列：区间判定（首/末根）与 vol 仲裁共用同一份，
    #     不再先探 30m 存续期（原：5×30m + 5×1m = 10）。
    _shfe = {}
    for _mm in ("2404", "2405", "2406", "2407", "2408"):
        _shfe[(f"SHFE.rb{_mm}", 60)] = _span(
            datetime(2024, 4, 1, 9, 30), datetime(2024, 9, 30, 14, 59),
            vol=(900 if _mm == "2405" else 100), mid=datetime(2024, 4, 22, 9, 30))
    cnt = _CountSrc(_shfe)
    sug, exp = _suggest_contract_with_expiry(cnt, "SHFE.rb2409", 60, TARGET)
    assert (sug, exp) == ("SHFE.rb2405", None), (sug, exp)
    assert len(cnt.fetch_calls) == 5, f"非 CFFEX 全扫应为 5 次，实为 {cnt.fetch_calls}"
    assert Counter(cnt.fetch_calls) == Counter(
        [(f"SHFE.rb{_mm}", 60) for _mm in ("2404", "2405", "2406", "2407", "2408")]), \
        cnt.fetch_calls
    # 30m 周期：存续期探针与窗口探针同 freq ⇒ 共用一份缓存，仍是 5 次
    # （改前两套缓存互不共享：5×30m + 5×30m = 10，其中 5 次是纯重复）
    _shfe30 = {(f"SHFE.rb{_mm}", 1800): _span(
        datetime(2024, 4, 1, 9, 30), datetime(2024, 9, 30, 14, 30),
        vol=(900 if _mm == "2405" else 100), mid=datetime(2024, 4, 22, 9, 30))
        for _mm in ("2404", "2405", "2406", "2407", "2408")}
    cnt = _CountSrc(_shfe30)
    assert _suggest_contract_for_date(cnt, "SHFE.rb2409", 1800, TARGET) == "SHFE.rb2405"
    assert len(cnt.fetch_calls) == 5, f"30m 场景应共用缓存只取 5 次，实为 {cnt.fetch_calls}"
    # 国债修好后也走 CFFEX 快路径（改前因候选月份全错、必然落全扫 = 7 次）→ 1 次
    cnt = _CountSrc(bond_feed.bars)
    _suggest_contract_with_expiry(cnt, "CFFEX.T2409", 60, _d_bond)
    assert Counter(cnt.fetch_calls) == Counter({("CFFEX.T2406", 60)}), cnt.fetch_calls
    # 国债换月窗口（D=06-10，距 T2406 交割 06-14 = 第二周五 仅 4 天 ≤ 15）
    # → 与次候选比目标日 vol：+ 次候选窗口探针 = 2 次（建议链分档，方案 §3.6 ⑤）
    cnt = _CountSrc({
        ("CFFEX.T2406", 60): _span(datetime(2024, 4, 1, 9, 30), datetime(2024, 6, 14, 14, 59),
                                   vol=100, mid=datetime(2024, 6, 10, 9, 30)),
        ("CFFEX.T2409", 60): _span(datetime(2024, 4, 1, 9, 30), datetime(2024, 9, 13, 14, 59),
                                   vol=900, mid=datetime(2024, 6, 10, 9, 30)),
    })
    sug, exp = _suggest_contract_with_expiry(cnt, "CFFEX.T2406", 60, datetime(2024, 6, 10, 10, 0))
    assert (sug, exp) == ("CFFEX.T2409", datetime(2024, 9, 13).date()), (sug, exp)
    assert Counter(cnt.fetch_calls) == Counter(
        {("CFFEX.T2406", 60), ("CFFEX.T2409", 60)}), cnt.fetch_calls

    print("[PASS] [1] 合约候选/建议：CFFEX 股指四个真实月份（当月/下月/两季月）+ 国债三个季月"
          "+ 生成处随附交割日（股指第三周五 / 国债第二周五）+ 非换月窗口直取当月 / 换月窗口 vol 仲裁"
          " / 回退兜底 + 国债切换不自指 + 数据验证当前周期可取 + 未连接时自动补连"
          "+ 取数分档计数桩（A=2 / B-init=2 / B-倒挂=3 / 换月=4 / 国债建议链=1、换月=2 / 非CFFEX全扫=5 / 早退=0）")


# ═══════════════════════ [2] 四类诊断 ═══════════════════════

def test_classify():
    from App.AppSSE import _classify_futures_replay

    src = _fake_2409()

    # ① contract_range（晚于最后交易日）：页面 IF2404，目标 04-22
    b = _classify_futures_replay(src, "CFFEX.IF2404", 60, TARGET, start_dt=datetime(2024, 4, 1))
    assert b is not None and b.code == "contract_range", b and b.code
    assert b.suggest == "CFFEX.IF2405", b.suggest
    assert "最后交易日" in b.message and "IF2405" in b.message, b.message

    # ② contract_range（早于上市日）
    b = _classify_futures_replay(src, SYMBOL, 60, datetime(2024, 2, 1, 10, 0),
                                 start_dt=datetime(2024, 8, 22, 9, 30))
    assert b is not None and b.code == "contract_range", b and b.code
    assert "上市日" in b.message and b.suggest == "CFFEX.IF2402", (b.message, b.suggest)
    assert isinstance(b.suggest, str), "suggest 恒为 str（前端契约）"

    # ③ tq_10000：IF2409 存续期内，但 1m 的 10000 根只到 07/19
    #    → 必须**绕过**该限制：给出同品种能取到 04-22 的合约（IF2405），而不是只说「换周期」
    b = _classify_futures_replay(src, SYMBOL, 60, TARGET, start_dt=datetime(2024, 7, 19, 9, 30))
    assert b is not None and b.code == "tq_10000", b and b.code
    assert "10000 根" in b.message and "2024/07/19 09:30" in b.message, b.message
    assert b.suggest == "CFFEX.IF2405", f"B 类必须给出可切换合约，实为 {b.suggest!r}"
    assert "主力合约 CFFEX.IF2405" in b.message, b.message
    assert "10000 根下限" in b.message, "B 类文案要点明天勤硬上限"
    assert isinstance(b.suggest, str), "suggest 恒为 str（前端契约）"

    # ③b 回退形态（**异常兜底**：当月 60m 序列缺失 → 最近候选窗口不覆盖 → 回退全扫 + vol 仲裁；
    #     1m/5m/30m 下当月不覆盖数学上不可达，本用例靠构造数据缺失触发）
    multi = _FakeSrc({
        (SYMBOL, 60): _span(datetime(2024, 7, 19, 9, 30), datetime(2024, 9, 20, 14, 59)),
        ("CFFEX.IF2405", 1800): _span(datetime(2024, 3, 18, 9, 30), datetime(2024, 5, 17, 14, 30)),
        ("CFFEX.IF2406", 1800): _span(datetime(2024, 4, 22, 9, 30), datetime(2024, 6, 21, 14, 30)),
        ("CFFEX.IF2406", 60): _span(datetime(2024, 4, 22, 9, 30), datetime(2024, 6, 21, 14, 59),
                                    vol=900, mid=datetime(2024, 4, 22, 9, 30)),
    })
    b = _classify_futures_replay(multi, SYMBOL, 60, TARGET, start_dt=datetime(2024, 7, 19, 9, 30))
    assert b is not None and b.suggest == "CFFEX.IF2406", \
        f"回退全扫应取当日成交量最大的覆盖候选，实为 {b.suggest!r}"

    # ④ before_selection：5m 下 D 可达，但早于选点
    b = _classify_futures_replay(src, SYMBOL, 300, TARGET,
                                 start_dt=datetime(2024, 5, 6, 9, 30), start_is_selection=True)
    assert b is not None and b.code == "before_selection", b and b.code
    assert "在选点" in b.message and "左侧" in b.message, b.message
    assert not b.suggest, "方式D 仅提示，不给切换/重载动作"
    assert "取消选点" in b.message, "方式D 的出路是取消选点，提示里必须写"

    # ⑤ way_c：5m 下 D 可达、窗口只加载了最近 960 根
    b = _classify_futures_replay(src, SYMBOL, 300, TARGET,
                                 start_dt=datetime(2024, 8, 22, 9, 30), window_bars=960)
    assert b is not None and b.code == "way_c", b and b.code
    # 方式C **只提示**（出路是把 FUTURES_LOOKBACK_CONFIG 调大并重启，用户自行操作）
    assert not b.suggest, (b.code, b.suggest)
    assert "请加载更多数据" in b.message and "最近 960 根" in b.message, b.message
    assert "2023/11/24 09:30" in b.message, "way_c 文案应给出该周期 10000 根可达的最早时间"

    # ⑤b 配置已用满 10000 根：不能再提示「请加载更多数据」（无更多可加载）
    b = _classify_futures_replay(src, SYMBOL, 300, TARGET,
                                 start_dt=datetime(2024, 8, 22, 9, 30), window_bars=10000)
    assert b is not None and b.code == "way_c", b and b.code
    assert "请加载更多数据" not in b.message and "取数上限" in b.message, b.message

    # ⑥ 优先级：D 既早于选点、又超出 10000 根 → 必须报 tq_10000（取消选点也解决不了）
    b = _classify_futures_replay(src, SYMBOL, 60, datetime(2024, 6, 3, 10, 0),
                                 start_dt=datetime(2024, 8, 1, 9, 30), start_is_selection=True)
    assert b is not None and b.code == "tq_10000", b and b.code

    # ⑥b 晚于最后交易日：后续合约未上市 → 不能给「改用更大周期」（换周期也到不了未来）
    b = _classify_futures_replay(src, SYMBOL, 60, datetime(2024, 10, 8, 10, 0),
                                 start_dt=datetime(2024, 9, 2, 9, 30))
    assert b is not None and b.code == "contract_range", b and b.code
    assert "最后交易日" in b.message, b.message
    assert b.suggest == "" and isinstance(b.suggest, str), f"未来日期不应建议合约，实为 {b.suggest!r}"
    assert "更大的周期" not in b.message, "未来日期换周期无效，不能这么指路"

    # ⑦ 可达：D 晚于窗口左界 → 不拦
    assert _classify_futures_replay(src, SYMBOL, 300, datetime(2024, 9, 2),
                                    start_dt=datetime(2024, 8, 22, 9, 30)) is None

    # ⑧ 主连无存续期：只可能 10000 / 选点 / 方式C；且 10000 类同样要给出可切换合约
    kq = _FakeSrc({
        ("KQ.m@CFFEX.IF", 60): _span(datetime(2023, 7, 20, 9, 0), datetime(2026, 10, 16, 15, 0)),
        ("CFFEX.IF2301", 1800): _span(datetime(2022, 12, 19, 9, 30), datetime(2023, 1, 20, 14, 30)),
        ("CFFEX.IF2301", 60): _span(datetime(2022, 12, 19, 9, 30), datetime(2023, 1, 20, 14, 59)),
    })
    b = _classify_futures_replay(kq, "KQ.m@CFFEX.IF", 60, datetime(2023, 1, 5),
                                 start_dt=datetime(2023, 7, 20, 9, 0))
    assert b is not None and b.code == "tq_10000", b and b.code
    assert b.suggest == "CFFEX.IF2301", f"主连也应建议具体合约，实为 {b.suggest!r}"
    print("[PASS] [2] 四类诊断：合约范围/10000根/选点/方式C + 硬约束优先 + 可达不拦")


class _Flaky30mSrc(_FakeSrc):
    """建议合约的 30m 取数失败桩（2026-10-18 评审 P2-1 回归钉）。

    v1.18 之后 `_contract_covers` 只取**当前周期**，不再预热 30m 缓存；
    建议合约的 30m 存续期探针是一次**新的取数**、可能失败 → `_sl` 为 None。
    没有本用例，`exp_hint` 的 None 下标（TypeError → 诊断帧 0 帧）无人守。
    """

    def __init__(self, bars, fail_keys):
        super().__init__(bars)
        self.fail_keys = set(fail_keys)   # (symbol, freq_sec) 集合：取数直接抛

    def fetch_kline(self, symbol, freq_sec=15, num_bars=None, display_key=None, start_time=None):
        if (symbol, freq_sec) in self.fail_keys:
            raise TimeoutError("simulated 30m probe failure")
        return super().fetch_kline(symbol, freq_sec=freq_sec, num_bars=num_bars,
                                   display_key=display_key, start_time=start_time)


def test_non_cffex_suggest_30m_missing():
    """非 CFFEX + 建议合约 30m 取数失败：分类器必须产出带 code 的诊断帧，不得 TypeError。"""
    from App.AppSSE import _classify_futures_replay
    # 场景一：tq_10000 路径（D 早于 rb2405 当前周期 10000 根下限）。
    # rb2406 在 1m 覆盖 D → 成为建议合约；其 30m 探针失败 → _sl None。
    src = _Flaky30mSrc({
        ("SHFE.rb2405", 60): _span(datetime(2024, 4, 1, 9, 0), datetime(2024, 4, 22, 15, 0)),
        ("SHFE.rb2406", 60): _span(datetime(2024, 3, 1, 9, 0), datetime(2024, 4, 22, 15, 0),
                                   vol=500, mid=datetime(2024, 3, 15, 9, 30)),
    }, fail_keys={("SHFE.rb2405", 1800), ("SHFE.rb2406", 1800)})
    b = _classify_futures_replay(src, "SHFE.rb2405", 60, datetime(2024, 3, 15, 10, 0),
                                 start_dt=datetime(2024, 3, 10, 10, 0))
    assert b is not None and b.code == "tq_10000", b and (b.code if b else None)
    assert b.suggest == "SHFE.rb2406", f"应建议覆盖目标日的后续合约，实为 {b.suggest!r}"
    assert "存续期未取到" in b.message, f"30m 探针失败必须走 None 兜底文案，实为：{b.message}"

    # 场景二：contract_range 路径（D 晚于 rb2405 最后交易日；本合约 30m 在、建议合约 30m 失败）。
    src2 = _Flaky30mSrc({
        ("SHFE.rb2405", 60): _span(datetime(2024, 4, 1, 9, 0), datetime(2024, 5, 6, 15, 0)),
        ("SHFE.rb2405", 1800): _span(datetime(2024, 4, 1, 9, 0), datetime(2024, 5, 6, 15, 0)),
        ("SHFE.rb2406", 60): _span(datetime(2024, 4, 20, 9, 0), datetime(2024, 6, 28, 15, 0),
                                   vol=500, mid=datetime(2024, 5, 20, 9, 30)),
    }, fail_keys={("SHFE.rb2406", 1800)})
    b = _classify_futures_replay(src2, "SHFE.rb2405", 60, datetime(2024, 5, 20, 10, 0),
                                 start_dt=datetime(2024, 5, 15, 10, 0))
    assert b is not None and b.code == "contract_range", b and (b.code if b else None)
    assert b.suggest == "SHFE.rb2406", f"应建议存续期覆盖 D 的后续合约，实为 {b.suggest!r}"
    assert "存续期未取到" in b.message, f"建议合约 30m 失败必须走 None 兜底，实为：{b.message}"
    print("[PASS] [2b] 非 CFFEX + 建议合约 30m 缺失：诊断帧不丢（P2-1 回归钉）")


# ═══════════════════ [5] init 晚向接线（A′ 可达性） ═══════════════════

def test_init_out_late():
    """端到端接线：D 晚于合约末根且非未来 → init 路径必须抛 A′。

    F1 指出的接线缺失：分类器单测覆盖了 A′，但 `init_chan_symbol` 原先只判
    「D < 10000 根首根」；晚向被 `_truncate_records_by_end` 静默吞掉，页面停在
    存续期末根、既无提示也无切换建议。
    ⚠️ 勘误（v1.19）：本 docstring 原写「前端 wantLive 只拦今天/未来」——**不成立**。
    非复盘态的 wantLive 是 `dateStr >= lastKlineInput`，比「今天/未来」宽得多，
    因此本用例只能证明**后端**接线，证明不了 UI 可达；UI 侧由 [6] 的源码契约兜住。
    """
    from App.AppSSE import init_chan_symbol, FuturesReplayBlocked

    src = _LazySrc({
        (SYMBOL, 1800): _span(datetime(2024, 3, 18, 9, 30), datetime(2024, 9, 20, 14, 30)),
        (SYMBOL, 60): _span(datetime(2024, 7, 19, 9, 30), datetime(2024, 9, 20, 14, 59)),
        # IF2410：09/20 挂牌 ~ 10/18 交割；10/01 在其存续期内且 1m 可取到
        ("CFFEX.IF2410", 1800): _span(datetime(2024, 9, 20, 9, 30), datetime(2024, 10, 18, 14, 30)),
        ("CFFEX.IF2410", 60): _span(datetime(2024, 9, 2, 9, 30), datetime(2024, 10, 18, 14, 59),
                                   vol=900, mid=datetime(2024, 10, 1, 9, 30)),
    })
    try:
        init_chan_symbol(src, SYMBOL, "IF2409", 60, "1m",
                         "2024/09/02 09:30", "2024/10/01 10:00")
        raise AssertionError("晚于最后交易日应抛 A′（原实现静默截断到 09/20）")
    except FuturesReplayBlocked as b:
        assert b.code == "contract_range", b.code
        assert "最后交易日" in b.message, b.message
        assert b.suggest == "CFFEX.IF2410" and isinstance(b.suggest, str), b.suggest

    # 未来日期：分类器如实判「不在存续期」（未来当然不在），但 init 的晚向分支必须
    # 用 `_end_dt < datetime.now()` 把它排除——否则每天正常复盘/查未来都会弹 A′。
    assert "_end_dt < datetime.now()" in _read(SSE_PY), \
        "晚向分支必须排除未来日期（否则「查未来」会弹成 A′）"
    print("[PASS] [5] init 晚向接线：D 晚于末根 → A′ + 建议 IF2410；未来日期不报")


# ═══════════════════════ [3] 源码契约 ═══════════════════════

def test_frontend_contracts():
    sse = _read(SSE_PY)
    js = _read(APP_JS)

    # 后端：四类诊断与三处接入
    assert "class FuturesReplayBlocked" in sse, "缺 FuturesReplayBlocked"
    assert "except FuturesReplayBlocked:" in sse and "raise _blk" in sse, \
        "init_chan_symbol 未把诊断结果上抛（或被通用 except 吞掉）"
    assert sse.count('"code": _blk.code') >= 2, "单窗/双窗错误帧必须带 code"
    for code in ("contract_range", "tq_10000", "before_selection", "way_c"):
        assert f'"{code}"' in sse, f"缺诊断类别 {code}"
    assert "_third_friday" in sse and "_suggest_contract_for_date" in sse, "缺合约推荐实现"
    assert "_contract_covers" in sse and "end_dt >= first" in sse, \
        "候选合约必须验证「当前周期 10000 根也能取到该日期」，不能只看存续期"
    assert "_split_product" in sse and '"CZCE"' in sse, "候选代码需按交易所格式生成（郑商所三位）"
    assert "_FUTURES_LIFE_FREQ = 1800" in sse, "合约存续期探针应走 30m（10000 根跨度≫存续期）"
    # v1.18 最近优先 + 换月窗口 + 契约护栏（评审第三/四轮）
    assert "_contract_window_covers" in sse and "_CFFEX_ROLL_WINDOW_DAYS = 15" in sse,         "缺最近优先/换月窗口实现"
    assert "_suggest_contract_with_expiry" in sse and "_contract_candidates_with_expiry" in sse,         "缺带交割日的内部形态（生成处随附，消除字符串解析）"
    assert "target_dt > datetime.now()" in sse, "缺未来日期早退（公开接口直调可达）"
    assert "最后交易日约" in sse, "方案二文案（最后交易日约）未落地"
    assert "suggest 恒为 str" in sse, "FuturesReplayBlocked 契约注记缺失"
    assert "目标日全天" in sse, "vol 时点口径（目标日全天累计）未注明"
    # v1.19：晚向必须带「序列确实停更」守卫 —— 前端放行「D ≥ 末根 且 D < 今天」后，
    # 「查上周六」（末根=周五）也会带 end_time 进来，无守卫会被 out_late 误报成 A′。
    assert "def _futures_series_stale(" in sse and sse.count("_futures_series_stale(") >= 2, \
        "晚向缺 _futures_series_stale 守卫：休市日会被误报成「晚于最后交易日」"
    # v1.19：复盘态判据必须**统一**走 `_is_replay_eff`（= end_time 且非「晚向但仍在市」）。
    # 五处快照 + 两条更新循环判据都要用它；留一处 `bool(end_time)` 就会出现
    # 「meta 说复盘、or 循环仍冻结」的不一致（休市日输入后开盘不更新）。
    assert "is_replay=bool(end_time)" not in sse, \
        "快照的复盘态判据未统一走 _is_replay_eff（休市日会得到「● 实时但永不更新」的图）"
    assert "def _late_boundary_is_live(" in sse and sse.count("is_replay=_is_replay_eff") >= 5, \
        "缺 _late_boundary_is_live / 五处快照未接线"
    # v1.19 P1 修复（2026-10-19）：晚向判据第三参必须收死为未截断末根——签名留
    # records 就会有人传截断后的 init 返回值自比（= P1 回归形态，历史休市日复盘被改实时）。
    assert "def _late_boundary_is_live(end_time, end_dt, series_last_dt)" in sse, \
        "晚向判据签名未收死为 series_last_dt（传截断后 records 自比 = P1 回归）"
    assert "def _series_last_dt(" in sse, "缺 _series_last_dt（未截断末根提取）"
    # ⚠️ 调用点形态必须用 AST 查：字符串计数会被 docstring 引用稀释（2026-10-19
    # 变异 A 实测漏拦）——调用点退回「传截断后末根」时计数不变、护栏假绿。
    import ast as _ast
    _ast_tree = _ast.parse(sse)
    _lb_calls = [n for n in _ast.walk(_ast_tree) if isinstance(n, _ast.Call)
                 and isinstance(n.func, _ast.Name) and n.func.id == "_late_boundary_is_live"]
    assert len(_lb_calls) == 2, \
        f"晚向判据调用点应恰为 2 处（单/双窗），实测 {len(_lb_calls)}"
    for _c in _lb_calls:
        _a3 = _c.args[2] if len(_c.args) >= 3 else None
        assert isinstance(_a3, _ast.Call) and isinstance(_a3.func, _ast.Name) \
            and _a3.func.id == "_series_last_dt", \
            "晚向判据第三参必须是 _series_last_dt(...) 直接调用（传截断后末根 = P1 回归形态）"
    # 探针取数前必须确保已连接（否则 api=None → AttributeError → A/B 全退化）
    src_py = _read(SRC_PY)
    assert "_ensure_futures_src_connected(src)" in sse and sse.count("_ensure_futures_src_connected(src)") >= 2, \
        "两个探针（存续期 / 10000 根首根）都必须先确保数据源已连接"
    assert "def _ensure_futures_src_connected(src):" in sse, "缺补连实现"
    # 幂等断言必须锚定 connect 内的**完整形态**：短子串 "if self.api is not None:"
    # 在旧树的 get_kline_serial 里就有（恒真，2026-10-18 评审 P3-b），钉不住 connect
    assert "if self.api is not None and not self._closed:" in src_py, \
        "CTqSdkSession.connect 必须幂等且不复用已关闭会话（重复 TqApi 多建 websocket 泄漏）"

    # 前端：期货分支不再有「请扩大数据范围」；改为按 code 渲染
    assert '请扩大数据范围' in js, "股票分支的越界提示不应被误删"
    assert '复盘日期 " + apiDate + " 早于已加载数据起点 " + _futReplayFloor' not in js, \
        "期货分支仍在前端拦（应交给后端分类）"
    # 方式C 只提示：前端不得给任何动作分支（出路是用户改配置并重启）
    assert 'if (_code === "way_c"' not in js, "方式C 不应有前端动作分支（仅提示）"
    assert '{okText: "加载更多"}' not in js, "不应提供「加载更多」按钮"
    assert 'connectRealtimeInit(symbol, _freqNow, "", _target)' not in js, \
        "不得替用户改 L 重载（方式C 的出路是调大配置后重启）"
    # retryable 是死字段（2026-10-18 删）：三处构造从未传过它，前端也从不读——
    # 留着只会让人误以为「能不能重试」有后端判据。谁加回来这里就红。
    assert 'data.retryable' not in js, "前端不应依赖 retryable"
    assert "retryable" not in sse, "FuturesReplayBlocked 不应再有 retryable 字段（无人消费）"
    # v1.19：非复盘态钳位必须带「D ≥ 今天」合取项。少这一项 ⇒ 已交割合约的
    # 「D ≥ 末根」被当成回实时 ⇒ 请求不带 end_time ⇒ 后端晚向 A′ 永远不可达
    # （2026-10-19 用户现场：CFFEX.IF2409 1m + 日历 2024-09-23 → 图不变、日历跳回末根）。
    assert "dateStr.slice(0, 10) >= todayStr" in js, \
        "wantLive 缺「D ≥ 今天」合取项：已交割合约的晚向复盘会被静默回实时"
    assert 'def __init__(self, code, message, suggest=""):' in sse, \
        "FuturesReplayBlocked 构造签名不应再带 retryable"
    assert 'if (data.suggest && (_code === "contract_range" || _code === "tq_10000"))' in js, \
        "缺「切换到建议合约」分支（10000 根 / 合约范围两类都要能一键绕过）"
    assert '{okText: "切换"}' in js, "切换按钮文案必须是「切换」（不是含糊的「确定」）"
    assert "let _pendingFuturesReplay = null;" in js, \
        "缺闭包变量（window API 面冻结，不能用 window.*）"
    assert 'connectRealtimeInit(code, fetchFreq, "", _endForLoad, _resetSel)' in js, \
        "切合约后必须以目标日为右端加载（方式C：L = D 往前 N 根），不能先方式A 冷启动"
    assert 'connectRealtimeDual(code, fetchFreq, subFreq, _endForLoad, "", "", _resetSel)' in js, \
        "双窗切合约同样要以目标日为右端加载且不继承选点"
    # 切合约连接必须显式声明不继承选点（后端对空 start_time 会从 CSV 恢复）
    assert js.count("'&reset_selection=1'") == 2, "单/双窗都必须能拼 reset_selection=1"
    assert "reset_selection: bool = Query(False)" in _read(REPO_ROOT + "/FrontAPI.py"), \
        "路由未接收 reset_selection"
    assert ("sse_futures_stream_single(symbol, freq, start_time, end_time," in
            _read(REPO_ROOT + "/FrontAPI.py")
            and "reset_selection=reset_selection" in _read(REPO_ROOT + "/FrontAPI.py")), \
        "路由未把 reset_selection 透传给生成器"
    assert sse.count("reset_selection") >= 4, "单/双窗 gen 都要消费 reset_selection"
    assert "setTimeout(function () { gotoDate(); }, 0)" not in js, \
        "不应再用「冷启动后再 gotoDate」两步走（对已交割合约会先落在交割日附近、再弹一次加载更多）"
    assert "window.__pendingFuturesReplay" not in js, "不应新增 window 全局（phase6 window API 面冻结）"
    assert "_contract_volume_on" in sse and "vol > best_vol" in sse, \
        "多候选覆盖时应按当日成交量取主力合约"
    # 诊断帧必须在三处 init 调用点被接住（否则 = 连接异常中断，前端拿不到切换按钮）
    assert sse.count("except FuturesReplayBlocked as _blk:") >= 3, \
        "单窗/双窗上下窗三处 init 调用都必须捕获诊断，转成带 code 的错误帧"

    # 阈值 7 = 旧树 5 处 + 本轮新增 2 处（init 失败分支恢复日历框）。
    # 阈值 5 时旧树也绿（2026-10-18 评审 P3-b：判别力为零），新增修复无人守。
    assert js.count('document.getElementById("goto-date-input").disabled = false;') >= 7, \
        "init 失败分支必须恢复日历输入框（单+双窗，含本轮新增 2 处）"
    # pending 清空必须在 loadStock **入口**（2026-10-18 评审 P3-e）：期货分支内清的话，
    # 早退路径会残留 pending，之后同合约加载被静默套上旧 endTime。
    # 判别点：期货分支读的是入口捕获的 _pendCapture，不再直接读闭包变量。
    assert "const _pendCapture = _pendingFuturesReplay;" in js \
        and "_pendingFuturesReplay.symbol" not in js, \
        "pending 必须在 loadStock 入口一次性捕获清空（残余只在守卫复原点存活）"
    # 引擎运行中早退必须**复原**切换意图（2026-10-18 复审 N2）：用户点「切换」时
    # 引擎还在跑 → 提示关引擎，但意图不丢——关掉后的下一次加载仍自动复盘到目标日。
    # 复原后的意图最多存活到下一次 loadStock（入口捕获消费或清空），不会无界残留。
    # 锚「存在 + 位置」（第三轮复审 P3；第四轮补下界）：只验存在的话，把复原行挪到
    # return 之后（成死代码）照样绿。锚串全仓都不唯一，必须先用守卫前**唯一**的注释
    # 「交易引擎运行中禁止切换合约」圈定 loadStock 的守卫，再在其后取两个结构锚：
    #   if (autoOrderRunning && isFuturesMode()) {   → 下界（全仓 6 处）
    #   其后的第一个 return;                          → 上界
    # 复原行必须严格落在两者之间：落在 return 之后 ⇒ 死代码必红；落在 guard-if 之前
    # ⇒ 等于每次加载无条件复原，入口清空作废、pending 会无界残留（P3-e 回归）也必红；
    # 整行删除 rst=-1 必红。上界取 return 而非 showAlert：复原行放在 alert 之后、
    # return 之前是等价写法，不该误报。
    _cmt = js.find("交易引擎运行中禁止切换合约")
    assert _cmt >= 0, "loadStock 守卫注释锚丢失"
    _grd = js.find("if (autoOrderRunning && isFuturesMode()) {", _cmt)
    assert _grd >= 0, "loadStock 守卫 if 锚丢失"
    _ret = js.find("return;", _grd)
    _rst = js.find("_pendingFuturesReplay = _pendCapture;")
    assert _grd < _rst < _ret, \
        "复原 _pendCapture 必须落在 autoOrderRunning 守卫内、return 之前"

    # 两个 UI bug 的修复锚点
    assert "if (e.key.startsWith('Arrow')) { _dateKeyArrow = true; _datePickerInteracted = true; return; }" in js, \
        "方向键未标记 _datePickerInteracted（blur 会回滚日期）"
    assert "本次输入值已被 gotoDate 消费" in js, "gotoDate 未清 _dateFocusOriginal（disabled 触发 blur 会回滚）"
    print("[PASS] [3] 源码契约：三处 init 都捕获诊断 / 前端按 code 渲染"
          "（A/B/E 切换；C/D/F/G 仅提示）/ UI 回滚与禁用两处修复")


# ═══════════════════ [6] 切合约连接不继承选点 ═══════════════════

class _SseSrc(_FakeSrc):
    """SSE 生成器桩：只补生成器生命周期方法（数据仍由 _FakeSrc 按 (symbol, freq) 供）。"""

    def connect(self):
        pass

    def close(self):
        pass

    def close_api(self):
        pass

    def cleanup_records(self, code_key):
        pass


def test_switch_connection_drops_selection():
    """切到建议合约的这次连接**不继承** CSV 选点（2026-10-18 修 P2-2）。

    协议里空 start_time 同时表示两种意思：「前端没带 L」和「前端不要 L」，而后端
    对空值的语义是前者（从 CSV 恢复）。切到建议合约（月份合约）时，若该合约在
    CSV 里存过晚于 D 的选点，复盘窗口 L 会被这个选点钉住 → 重连后立刻又报
    before_selection（仅提示、无「切换」按钮），用户点「切换」等于白切一次。
    切过去的目标是「看那一天」，所以必须显式声明不继承。
    """
    from App import AppSSE as _sse_mod
    import FrontAPI

    _POINT = "2024/05/06 09:30"     # 晚于 D=04-22 的选点
    _END = "2024/04/22 15:00"
    _sse_mod.app_data.save_point_time("CFFEX.IF2405", "IF2405", "1m", _POINT)

    captured = {}

    class _Stop(Exception):
        """止步哨兵：跑到取数就够，后面的快照/推送逻辑本测试不关心。"""

    def _stub_init(api, symbol, name, freq_sec, freq_label, start_time=None,
                   end_time=None, num_bars=None, live=False):
        captured["args"] = (symbol, start_time, end_time)
        captured["live"] = live
        raise _Stop("stop at init_chan_symbol")

    _orig = _sse_mod.init_chan_symbol
    _sse_mod.init_chan_symbol = _stub_init

    def _drive(reset):
        captured.pop("args", None)
        frames = []
        gen = FrontAPI.sse_futures_stream_single(
            "CFFEX.IF2405", "1m", None, _END,
            reset_selection=reset, source=_SseSrc({}))
        for _f in gen:
            frames.append(_f)
        return frames

    try:
        # 默认（不声明 reset）：继承选点 → L 被钉在 05-06，D=04-22 在它左侧 → 报方式D
        frames = _drive(False)
        assert frames, "默认路径应先复现 before_selection（未产出任何帧）"
        raw = frames[0]
        raw = raw.decode("utf-8") if isinstance(raw, bytes) else raw
        payload = json.loads(raw.split("data: ", 1)[1].split("\n")[0])
        assert payload["code"] == "before_selection", payload
        assert "取消选点" in payload["error"], payload["error"]

        # 声明 reset_selection：不继承 → 不报 before_selection，且取数不传 start_time
        frames = _drive(True)
        assert frames == [], f"切合约连接不应再报 before_selection，实为 {frames}"
        assert captured["args"][1] is None, \
            f"切合约连接不应继承选点，实为 {captured['args']!r}"
        assert captured["args"][2] == _END, captured["args"]
    finally:
        _sse_mod.init_chan_symbol = _orig

    print("[PASS] [6] 切合约连接不继承选点：默认路径报 before_selection（复现），"
          "reset_selection=1 时 L 不再被旧选点钉住")


# ═══════════════════════ [4] 真浏览器 ═══════════════════════

def _bars_5m(end_day, n_days=20):
    days, d = [], end_day
    while len(days) < n_days:
        if d.weekday() < 5 and d.strftime("%m-%d") not in ("09-16", "09-17"):
            days.append(d)
        d -= timedelta(days=1)
    days.reverse()
    times = []
    for h, m0 in ((9, 30), (13, 0)):
        t = datetime(2024, 1, 1, h, m0)
        for _ in range(24):
            times.append(t.strftime("%H:%M"))
            t += timedelta(minutes=5)
    klines, price = [], 3500.0
    for day in days:
        for hm in times:
            price += 0.2
            o, c = price, price + 0.5
            klines.append({
                "date": "%s %s" % (day.strftime("%Y/%m/%d"), hm),
                "timestamp": int(datetime(day.year, day.month, day.day,
                                          int(hm[:2]), int(hm[3:])).timestamp() * 1000),
                "open": round(o, 2), "high": round(max(o, c) + 0.4, 2),
                "low": round(min(o, c) - 0.4, 2), "close": round(c, 2),
                "vol": 100, "amount": 0, "dif": 0, "dea": 0, "macd": 0, "rsi": 50,
            })
    return klines


def _payload(end_day, is_replay=False):
    kl = _bars_5m(end_day)
    return {
        "meta": {"symbol": SYMBOL, "name": SYMBOL, "freq": "5分钟",
                 "kline_count": len(kl), "bi_count": 0, "fx_count": 0,
                 "zs_count": 0, "seg_count": 0, "bsp_count": 0,
                 "generated_at": kl[-1]["date"], "is_realtime": True,
                 "is_replay": bool(is_replay), "market": "futures",
                 "saved_selection_date": ""},
        "klines": kl, "bis": [], "fxs": [], "segs": [], "zs": [],
        "zs_stars": [], "bsps": [], "white_hline": None,
    }


FAKE_ES_JS = r"""
window.__esUrls = [];
window.__autoPayload = {};
window.__failNext = null;
window.EventSource = function (url) {
  var self = this;
  this.url = url; this.readyState = 0; this.withCredentials = false;
  this._h = {};
  this.addEventListener = function (ev, fn) { (self._h[ev] = self._h[ev] || []).push(fn); };
  this.removeEventListener = function () {};
  this.dispatchEvent = function () { return false; };
  this.close = function () { self.readyState = 2; };
  window.__esUrls.push(url);
  setTimeout(function () {
    if (self.readyState === 2) return;
    var payload = window.__autoPayload.initial;
    // 「回最新/今天」（live=1）：模拟后端 A″（已交割合约没有今天的行情）
    if (window.__liveFail && /live=1/.test(url)) { payload = window.__liveFail; }
    else if (window.__failNext) { payload = window.__failNext; window.__failNext = null; }
    else if (/end_time=/.test(url)) payload = window.__autoPayload.replay;
    var m = /symbol=([^&]+)/.exec(url);
    if (m && payload && payload.meta) {
      payload = JSON.parse(JSON.stringify(payload));
      payload.meta.symbol = decodeURIComponent(m[1]);
    }
    if (payload && (self._h['init'] || []).length) {
      var ev = {data: JSON.stringify(payload)};
      (self._h['init'] || []).forEach(function (fn) { try { fn(ev); } catch (e) {} });
    }
  }, 40);
};
localStorage.setItem('lastCodeFreq', JSON.stringify({code: 'sh600519', freq: 'd', name: '贵州茅台'}));
"""

STOCK_STUB = {
    "meta": {"symbol": "sh600519", "name": "贵州茅台", "freq": "日线",
             "kline_count": 2, "bi_count": 0, "fx_count": 0, "zs_count": 0,
             "seg_count": 0, "bsp_count": 0, "generated_at": "2026/10/17",
             "is_realtime": False, "market": "stock"},
    "klines": [
        {"date": "2026/10/16", "timestamp": 0, "open": 1, "high": 1, "low": 1,
         "close": 1, "vol": 1, "amount": 0, "dif": 0, "dea": 0, "macd": 0, "rsi": 50},
        {"date": "2026/10/17", "timestamp": 0, "open": 1, "high": 1, "low": 1,
         "close": 1, "vol": 1, "amount": 0, "dif": 0, "dea": 0, "macd": 0, "rsi": 50},
    ],
    "bis": [], "fxs": [], "segs": [], "zs": [], "zs_stars": [], "bsps": [],
    "white_hline": None,
}


def _launch(pw):
    import glob as _glob
    try:
        return pw.chromium.launch(channel="chrome")
    except Exception:
        pass
    try:
        return pw.chromium.launch()
    except Exception:
        pass
    for pat in ("~/AppData/Local/ms-playwright/chromium-*/chrome-win64/chrome.exe",):
        for cand in _glob.glob(os.path.expanduser(pat)):
            try:
                return pw.chromium.launch(executable_path=cand)
            except Exception:
                pass
    return None


def test_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as e:
        print(f"  [SKIP] 无 playwright（{e}）；静态契约已覆盖")
        return

    class Quiet(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *a):
            pass

    httpd = http.server.ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(Quiet, directory=FRONTEND_DIR))
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    errors = []
    try:
        with sync_playwright() as pw:
            browser = _launch(pw)
            if browser is None:
                print("  [SKIP] 无可用无头浏览器；静态契约已覆盖")
                return
            page = browser.new_page(viewport={"width": 1600, "height": 900})
            page.add_init_script(FAKE_ES_JS)
            page.on("pageerror", lambda e: errors.append(str(e).splitlines()[0][:140]))

            def route_api(route):
                url = route.request.url
                if "/api/health" in url:
                    body = {"config": {"view_count": 233},
                            "freq_sec_map": {"15s": 15, "1m": 60, "5m": 300, "30m": 1800}}
                elif "/api/stocks/" in url:
                    body = STOCK_STUB
                else:
                    body = {}
                route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

            page.route("**/api/**", route_api)

            def reset_to_if2409():
                """不刷新页面（页面自恢复会先连 SSE、拿不到桩数据会卡住），直接重连 IF2409。"""
                page.evaluate(
                    "(p) => { window.__autoPayload = p; window.__failNext = null;"
                    " window.__esUrls.length = 0;"
                    " document.getElementById('stock-code-input').value = 'CFFEX.IF2409';"
                    " window.loadStock(); }",
                    {"initial": _payload(datetime(2024, 9, 20)),
                     "replay": _payload(datetime(2024, 4, 22))})
                page.wait_for_function(
                    "() => window.ChanApp.state.chartData"
                    " && window.ChanApp.state.chartData.meta.symbol === 'CFFEX.IF2409'", timeout=20000)
                page.wait_for_timeout(350)

            page.goto("http://127.0.0.1:%d/app.html" % port, wait_until="load")
            page.wait_for_function(
                "() => !!(window.ChanApp && window.ChanApp.state && window.ChanApp.state.chartData)",
                timeout=20000)

            def state():
                return page.evaluate(
                    "() => ({es: window.__esUrls,"
                    " symbol: window.ChanApp.state.chartData.meta.symbol,"
                    " last: window.ChanApp.state.chartData.klines.slice(-1)[0].date,"
                    " value: document.getElementById('goto-date-input').value,"
                    " dialog: (() => { const d = document.getElementById('alert-dialog');"
                    "   return d ? d.innerText : ''; })(),"
                    # 关闭的弹层**不销毁**（只摘 show 类）⇒ 判「有没有弹窗」必须看类，
                    # 只看 innerText 会把上一场景关掉的弹窗当成还在（2026-10-21 踩过）
                    " dialog_shown: (() => { const d = document.getElementById('alert-dialog');"
                    "   return !!(d && d.classList.contains('show')); })()})")

            def enter_with(date_input):
                page.evaluate(
                    "(v) => { const i = document.getElementById('goto-date-input'); i.focus();"
                    " i.value = v; i.dispatchEvent(new Event('input', {bubbles: true})); }", date_input)
                page.keyboard.press("Enter")
                page.wait_for_timeout(650)

            # ── A：天勤 10000 根 → 「切换」到 IF2405 → 直接以 04-22 为右端加载 ──
            reset_to_if2409()
            page.evaluate(
                "(m) => { window.__failNext = {error: m, code: 'tq_10000',"
                " suggest: 'CFFEX.IF2405', symbol: 'CFFEX.IF2409'}; }",
                "1分钟 最多回看 10000 根，CFFEX.IF2409 最早只到 2024/07/19 09:30；"
                "目标 2024/04/22 15:00 已超出。同品种的 CFFEX.IF2405 可以取到该日期"
                "（存续期 2024/03/18 ~ 2024/05/17）—— 请切换到 CFFEX.IF2405，或改用 5分钟 / 30分钟。")
            enter_with("2024-04-22T15:00")
            sA = state()
            assert "CFFEX.IF2405" in sA["dialog"] and "切换" in sA["dialog"], \
                f"A 未弹「切换合约」确认：{sA['dialog']!r}"
            page.click('#alert-dialog button[data-act="ok"]')
            page.wait_for_timeout(1400)
            sB = state()
            assert sB["symbol"] == "CFFEX.IF2405", f"A 未切换合约：{sB['symbol']}"
            assert sB["last"] == "2024/04/22 14:55", f"A 切换后未自动复盘到 04-22：{sB['last']}"
            assert any("symbol=CFFEX.IF2405" in u and "end_time=" in u
                       and "start_time=" not in u for u in sB["es"]), \
                f"A 切合约后必须直接以目标日为右端加载（不带 start）：{sB['es']}"

            # ── B：方式C → **只提示**「请加载更多数据」，不动窗口、不重连 ──
            reset_to_if2409()
            _es_before = len(state()["es"])
            page.evaluate(
                "(m) => { window.__failNext = {error: m, code: 'way_c',"
                " symbol: 'CFFEX.IF2409'}; }",
                "请加载更多数据：复盘日期 2024/04/22 15:00 早于已加载数据起点 "
                "2024/08/22 09:30（当前窗口 = 最近 960 根；10000 根内最早可到 2023/11/24 09:30）。")
            enter_with("2024-04-22T15:00")
            sC = state()
            assert "请加载更多数据" in sC["dialog"], f"B 未提示加载更多数据：{sC['dialog']!r}"
            _btns = page.evaluate(
                "() => Array.from(document.querySelectorAll('#alert-dialog button'))"
                ".map(b => b.textContent.trim())")
            assert _btns == ["确定"], f"B 应为单按钮提示（不给动作按钮）：{_btns}"
            page.click('#alert-dialog button[data-act="ok"]')
            page.wait_for_timeout(800)
            sD = state()
            assert sD["last"] == "2024/09/20 14:55", \
                f"B 方式C 不得改动当前窗口（应仍在 09/20）：{sD['last']}"
            _replays = [u for u in sD["es"] if "end_time=" in u]
            assert len(_replays) == 1, f"B 方式C 只许一次复盘尝试、不得自动重载：{sD['es']}"
            assert len(sD["es"]) == _es_before + 1, \
                f"B 方式C 不得发起额外连接（初始 + 1 次尝试）：{sD['es']}"

            # ── C：已交割合约点日历「今天」（live=1）→ A″ 提示 + 「切换」到今日主力 ──
            # 现场（2026-10-21 用户报障）：IF2410 复盘到 2024-09-24 → 点「今天」→ 图被静默
            # 重载成该合约末根 2024/10/18、日历同样被反写，无任何提示。
            from App.AppSSE import _contract_candidates
            _today_main = _contract_candidates("CFFEX.IF2410", datetime.now())[0]
            reset_to_if2409()
            page.evaluate(
                "(p) => { window.__autoPayload = p.payload; window.__liveFail = p.liveFail;"
                " window.__failNext = null; window.__esUrls.length = 0;"
                " document.getElementById('stock-code-input').value = 'CFFEX.IF2410';"
                " window.loadStock(); }",
                {"payload": {"initial": _payload(datetime(2024, 10, 18)),
                             "replay": _payload(datetime(2024, 9, 24), is_replay=True)},
                 "liveFail": {"error": "今天（%s）已无 CFFEX.IF2410 的行情：该合约最后交易日为 "
                                       "2024/10/18；请切换到当前主力合约 %s"
                                       "（当前周期可取到今天）。"
                                       % (datetime.now().strftime("%Y/%m/%d"), _today_main),
                              "code": "contract_range", "suggest": _today_main,
                              "symbol": "CFFEX.IF2410"}})
            page.wait_for_function(
                "() => window.ChanApp.state.chartData"
                " && window.ChanApp.state.chartData.meta.symbol === 'CFFEX.IF2410'", timeout=20000)
            page.wait_for_timeout(300)
            enter_with("2024-09-24T14:55")          # 复盘（带 end_time）
            assert state()["last"] == "2024/09/24 14:55", f"C 未进入复盘：{state()}"
            # Edge 的「今天」按钮：**只改日期段、时间保持原值**，picker 关闭时再发 change
            # （input 阶段判不出「今天」，由 handleDateChange 的期货兜底改 23:59 并 gotoDate）
            page.evaluate(
                "() => { const i = document.getElementById('goto-date-input');"
                " const n = new Date();"
                " const t = n.getFullYear() + '-' + String(n.getMonth()+1).padStart(2,'0')"
                "   + '-' + String(n.getDate()).padStart(2,'0');"
                " i.focus(); i.value = t + 'T14:55';"
                " i.dispatchEvent(new Event('input', {bubbles: true}));"
                " i.dispatchEvent(new Event('change', {bubbles: true})); }")
            page.wait_for_timeout(700)
            sC1 = state()
            assert any("live=1" in u for u in sC1["es"]), \
                f"C「今天」必须带 live=1（否则后端无从判停更）：{sC1['es']}"
            assert "已无 CFFEX.IF2410 的行情" in sC1["dialog"] and "切换" in sC1["dialog"], \
                f"C 未弹 A″（今天无该合约行情 + 切换）：{sC1['dialog']!r}"
            assert "并回到实时行情" in sC1["dialog"], \
                f"C A″ 的切换文案必须是「回到实时行情」（不是「复盘到该日期」）：{sC1['dialog']!r}"
            assert sC1["dialog_shown"], "C A″ 弹窗必须是**可见**状态"
            assert sC1["last"] == "2024/09/24 14:55", \
                f"C 被拒时图不得变动（旧实现会跳到 2024/10/18）：{sC1['last']}"
            assert sC1["value"] == "2024-09-24T14:55", \
                f"C 被拒后日历必须回落到图上末根：{sC1['value']}"
            page.click('#alert-dialog button[data-act="ok"]')
            page.wait_for_timeout(1400)
            sC2 = state()
            assert sC2["symbol"] == _today_main, f"C 未切到今日主力 {_today_main}：{sC2['symbol']}"
            assert any(("symbol=" + _today_main) in u and "end_time=" not in u
                       for u in sC2["es"]), \
                f"C 切过去必须是**实时**加载（不带 end_time）：{sC2['es']}"

            # ── D：「取消复盘」走同一条 wantLive 路径，但**不得**被判成 A″ ──
            # 已交割合约上「取消复盘」= 退出复盘、回到该合约最新一根（=末根），这与
            # 「点今天」是两回事：若也带 live=1，用户就再也退不出这张合约的复盘态。
            page.evaluate(
                "(p) => { window.__autoPayload = p; window.__liveFail = null;"
                " window.__esUrls.length = 0;"
                " document.getElementById('stock-code-input').value = 'CFFEX.IF2410';"
                " window.loadStock(); }",
                {"initial": _payload(datetime(2024, 10, 18)),
                 "replay": _payload(datetime(2024, 9, 24), is_replay=True)})
            page.wait_for_function(
                "() => window.ChanApp.state.chartData"
                " && window.ChanApp.state.chartData.meta.symbol === 'CFFEX.IF2410'"
                " && window.ChanApp.state.chartData.meta.is_replay === false", timeout=20000)
            page.wait_for_timeout(250)
            enter_with("2024-09-24T14:55")
            assert state()["last"] == "2024/09/24 14:55"
            page.evaluate("() => window.annotationReplayToHere()")   # 菜单项「取消复盘」
            page.wait_for_timeout(900)
            sD = state()
            assert not sD["dialog_shown"], \
                f"D「取消复盘」不得弹 A″（当前仍是上一场景的弹层文本：{sD['dialog']!r}）"
            assert sD["last"] == "2024/10/18 14:55", \
                f"D「取消复盘」应回到该合约最新一根：{sD['last']}"
            assert all("live=1" not in u for u in sD["es"]), \
                f"D「取消复盘」不得带 live=1：{sD['es']}"

            assert not errors, f"页面报错：{errors[:2]}"
            browser.close()
    finally:
        httpd.shutdown()
    print("[PASS] [4] 真浏览器：10000 根 → 「切换」到 IF2405 并以 04-22 为右端加载；"
          "方式C → 仅提示「请加载更多数据」，不动窗口、不重连；"
          "已交割合约点「今天」→ live=1 + A″ 提示 + 切到今日主力（实时）；"
          "「取消复盘」→ 不带 live=1、无 A″、回到该合约末根")


# ═══════════════ [6] 晚向守卫：休市 vs 停更（v1.19） ═══════════════

def test_late_direction_stale_guard():
    """`D > 末根 且 D < now` 必须分开「休市」与「停更」。

    为什么需要这条守卫：v1.19 之前前端 `wantLive` 把「D ≥ 末根」一律钳成回实时，
    已交割合约的 D 到不了后端（A′ 不可达）；放行之后「查上周六」也会带 `end_time`
    进来，而 `_classify_futures_replay` 的 out_late 判据是 `day > last_bar.date()`，
    对「末根=周五、D=周六」同样命中 ⇒ 会把休市误报成「晚于最后交易日」。
    """
    from App.AppSSE import (_cffex_contract_month, _futures_series_stale,
                            _classify_futures_replay, init_chan_symbol,
                            FuturesReplayBlocked)
    import contextlib
    import io

    # ① 合约月份解析：CFFEX 四位 YYMM（股指/国债同形）；主连、非 CFFEX → None
    assert _cffex_contract_month("CFFEX.IF2409") == (2024, 9)
    assert _cffex_contract_month("CFFEX.T2403") == (2024, 3)
    assert _cffex_contract_month("KQ.m@CFFEX.IF") is None
    assert _cffex_contract_month("SHFE.rb2405") is None

    # ② 停更判定（CFFEX 走交割日精确值；其它交易所走 _FUTURES_STALE_DAYS 时限）
    assert _futures_series_stale(
        "CFFEX.IF2409", datetime(2024, 9, 20, 14, 59), datetime(2024, 9, 23, 9, 30)), \
        "IF2409 交割 2024-09-20，D=2024-09-23 ⇒ 停更"
    _now = datetime.now()
    _y, _m = _now.year, _now.month + 3
    if _m > 12:
        _y, _m = _y + 1, _m - 12
    _live = f"CFFEX.IF{_y % 100:02d}{_m:02d}"   # 交割日远未到 ⇒ 仍在市
    _last = _now - timedelta(days=3)             # 末根 ≈ 上一交易日
    _next = _last + timedelta(days=1)            # D = 次日（休市日形态）
    assert not _futures_series_stale(_live, _last, _next), \
        f"{_live} 仍在市，「D 略晚于末根」只是休市 ⇒ 不得判停更"
    assert not _futures_series_stale("SHFE.rb2405", _last, _next), \
        "非 CFFEX：末根距今 3 天在 15 天容差内 ⇒ 仍在市"
    assert _futures_series_stale("SHFE.rb2405", _now - timedelta(days=30),
                                 _now - timedelta(days=29)), \
        "非 CFFEX：末根距今 30 天 ⇒ 停更（时限兜底）"

    # ③ 行为接线：分类器本身对该形态就判 contract_range（= 守卫必须挡在它前面），
    #    而 init 的晚向分支带守卫 ⇒ 不得抛 A′。
    _bars = {
        (_live, 1800): _span(_last - timedelta(days=60), _last),
        (_live, 60): _span(_last - timedelta(days=10), _last),
    }
    blk = _classify_futures_replay(_LazySrc(dict(_bars)), _live, 60, _next)
    assert blk is not None and blk.code == "contract_range", \
        f"分类器对「D 略晚于末根」本就判 contract_range（这正是守卫的必要性）：{blk}"
    # 桩无 set_data，建 chan 那步必然失败并打 traceback（与本节无关）⇒ 屏蔽输出。
    # ⚠️ 同时 monkeypatch 分类器计数：休市形态下 init 的晚向守卫必须在分类器**之前**
    # 拦下（计数 0）——只断言「未抛 FuturesReplayBlocked」区分不了「守卫生效」与
    # 「压根没走到守卫」（2026-10-19 P3-2）。
    import App.AppSSE as _appsse
    _orig_cls = _appsse._classify_futures_replay
    _cls_calls = {"n": 0}
    def _spy_cls(*a, **kw):
        _cls_calls["n"] += 1
        return _orig_cls(*a, **kw)
    _appsse._classify_futures_replay = _spy_cls
    _blocked, _sink = None, io.StringIO()
    try:
        with contextlib.redirect_stdout(_sink), contextlib.redirect_stderr(_sink):
            try:
                init_chan_symbol(_LazySrc(dict(_bars)), _live, "IF", 60, "1m", None,
                                 _next.strftime("%Y/%m/%d %H:%M"))
            except FuturesReplayBlocked as b:
                _blocked = b
            except Exception:
                pass
    finally:
        _appsse._classify_futures_replay = _orig_cls
    assert _blocked is None, \
        f"休市日不得报 A′（_futures_series_stale 守卫失效）：{_blocked.code} {_blocked.message}"
    assert _cls_calls["n"] == 0, \
        f"晚向守卫必须挡在分类器之前（休市形态分类器被调 {_cls_calls['n']} 次）"

    # ④ 边界落在**数据源**末根之后 ⇒ 本次连接按「回最新」服务（不冻结）。
    #    否则盘前输入上周六会得到一张徽标 ● 实时、实际永不更新的图。
    #    今天的边界例外（用户可能想看今天早些时候）⇒ 仍冻结。
    #    ⚠️ 第三参必须是未截断末根：2026-10-19 P1 回归 = 传 init 已截断的 records
    #    自比 ⇒ 「D=历史休市日/盘前时刻」恒判实时（截断后末根=D 前一交易日）。
    from App.AppSSE import _late_boundary_is_live, _series_last_dt
    assert _late_boundary_is_live(_next.strftime("%Y/%m/%d %H:%M"), _next, _last), \
        "边界落在数据源末根之后且是历史日期 ⇒ 应按实时服务（_is_replay_eff=False）"
    _today_early = _now.replace(hour=9, minute=30, second=0, microsecond=0)
    assert not _late_boundary_is_live(_today_early.strftime("%Y/%m/%d %H:%M"),
                                      _today_early, _last), \
        "今天的边界不得改判为实时（那是「复盘到今天早些时候」）"
    assert not _late_boundary_is_live(None, _next, _last), "无 end_time ⇒ 不是晚向边界"
    assert not _late_boundary_is_live(_next.strftime("%Y/%m/%d %H:%M"),
                                      _last - timedelta(days=1), _last), \
        "边界早于末根 ⇒ 真复盘，不得改判为实时"
    # P1 回归钉（2026-10-19）：D=历史休市日、真实末根晚于 D ⇒ 必须冻结复盘。
    # 修复前的错误口径（拿截断后末根自比）在此恒判实时 ⇒ 本断言红。
    _p1_d = datetime(2025, 10, 4, 15, 0)          # 历史周六（休市日形态）
    assert not _late_boundary_is_live(
        _p1_d.strftime("%Y/%m/%d %H:%M"), _p1_d,
        _now.replace(hour=15, minute=0, second=0, microsecond=0)), \
        "P1 回归钉：D=历史休市日、真实末根晚于 D ⇒ 必须冻结复盘（不得用截断后末根自比）"
    # 末根取不到 ⇒ 保守冻结（误冻结可由前端「回实时」纠正，误实时=错乱图）
    assert not _late_boundary_is_live(_next.strftime("%Y/%m/%d %H:%M"), _next, None), \
        "series_last_dt=None ⇒ 保守判冻结（不得误判实时）"
    # _series_last_dt：klines 末根纳秒时间戳 → datetime；取不到 → None
    import pandas as pd
    assert _series_last_dt(None) is None
    assert _series_last_dt(pd.DataFrame()) is None
    assert _series_last_dt(pd.DataFrame([{"datetime": None}])) is None
    _df = pd.DataFrame([{"datetime": None}, {"datetime": 1727000000 * 10**9}])
    assert _series_last_dt(_df) == datetime.fromtimestamp(1727000000), \
        "末根时间戳转换口径（纳秒 → datetime）"
    print("[PASS] [6] 晚向守卫：已交割 ⇒ 停更；休市日 ⇒ 不报；"
          "边界落数据源末根后 ⇒ 按实时服务；P1 回归钉（截断自比=实时）已封")


# ══════════ [7] 「回最新/今天」落在停更合约上（A″，2026-10-21） ══════════

def test_live_target_on_stale_contract():
    """日历「今天」（前端 `live=1`）撞上已交割合约 ⇒ A″，不得静默停在末根。

    现场（2026-10-21 用户报障）：`CFFEX.IF2410`（最后交易日 2024/10/18）复盘到 2024-09-24
    后点日历「今天」——wantLive 钳位让这次重连**不带 end_time**，于是早向（10000 根）与
    晚向（`_end_dt < now`）两个诊断分支都够不到，图被静默重载成该合约末根 2024/10/18、
    日历同样被反写成该日，全程无提示（用户看到「日历 2026-10-21、图停在两年前」）。

    修法两半：
      · 前端：wantLive 带 `live=1`（「取消复盘」显式抑制——它要的是本合约最新一根）；
      · 后端：init 在**建 chan 之前**按 `_futures_series_stale` 判停更 ⇒ A″（事实源与 A′
        同一个守卫，出路与 A/A′ 同一条建议合约链 ⇒ 前端复用「切换」渲染）。
    """
    import contextlib
    import io
    from App.AppSSE import (init_chan_symbol, FuturesReplayBlocked,
                            _contract_candidates, _live_target_blocked)

    now = datetime.now()
    today_main = _contract_candidates("CFFEX.IF2410", now)[0]   # 按同一套候选规则取，抗日期漂移
    bars = {
        # IF2410：2024/09/20 挂牌 ~ 10/18 交割（末根 = 最后交易日）
        ("CFFEX.IF2410", 1800): _span(datetime(2024, 9, 20, 9, 30), datetime(2024, 10, 18, 14, 30)),
        ("CFFEX.IF2410", 300): _span(datetime(2024, 9, 20, 9, 30), datetime(2024, 10, 18, 14, 55)),
        # 今日主力：数据覆盖到今天（建议合约探针）
        (today_main, 1800): _span(now - timedelta(days=120), now - timedelta(days=1)),
        (today_main, 300): _span(now - timedelta(days=20), now - timedelta(minutes=10)),
    }

    # ① 诊断本体：文案说「今天」+ 最后交易日，出路 = 今日主力（suggest 恒 str）
    blk = _live_target_blocked(_LazySrc(dict(bars)), "CFFEX.IF2410", 300,
                               datetime(2024, 10, 18, 14, 55))
    assert blk.code == "contract_range", blk.code
    assert blk.suggest == today_main and isinstance(blk.suggest, str), blk.suggest
    assert "已无 CFFEX.IF2410 的行情" in blk.message, blk.message
    assert "最后交易日为 2024/10/18" in blk.message, blk.message
    assert "今天（%s）" % now.strftime("%Y/%m/%d") in blk.message, blk.message

    def _drive(src, symbol, live):
        """跑 init；只认 FuturesReplayBlocked（其余异常 = 桩缺 set_data，与本用例无关）。"""
        sink = io.StringIO()
        try:
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                init_chan_symbol(src, symbol, "IF", 300, "5m", live=live)
        except FuturesReplayBlocked as b:
            return b
        except Exception:
            return None
        return None

    # ② live=True（日历「今天」）⇒ 必须 A″，且挡在建 chan 之前（桩没有 set_data，能到
    #    chan 构建就说明没拦住）
    got = _drive(_LazySrc(dict(bars)), "CFFEX.IF2410", True)
    assert got is not None and got.code == "contract_range", \
        f"live=1 撞已交割合约必须报 A″（旧实现静默停在末根）：{got}"
    assert got.suggest == today_main, got.suggest

    # ③ live=False（初载 / 切换合约 / **取消复盘**）⇒ 不得报 A″：那三处要的就是
    #    「这个合约的数据」，停在末根是正确行为
    assert _drive(_LazySrc(dict(bars)), "CFFEX.IF2410", False) is None, \
        "初载/取消复盘不得报 A″（已交割合约仍应能正常查看其历史）"

    # ④ 仍在市的合约（末根 = 最近一根）⇒ live=1 也不得误报
    live_bars = {
        (today_main, 1800): _span(now - timedelta(days=120), now - timedelta(days=1)),
        (today_main, 300): _span(now - timedelta(days=20), now - timedelta(minutes=10)),
    }
    assert _drive(_LazySrc(live_bars), today_main, True) is None, \
        "仍在市的合约点「今天」不得报 A″"

    # ⑤ 源码契约：live 全链路透传 + 「取消复盘」独占抑制位
    js = _read(APP_JS)
    sse = _read(SSE_PY)
    route = _read(os.path.join(REPO_ROOT, "FrontAPI.py"))
    assert js.count("'&live=1'") == 2, "单/双窗都必须能拼 live=1"
    assert "let _cancelReplayIntent = false;" in js, "缺「取消复盘」抑制位"
    assert js.count("_cancelReplayIntent = true") == 1 and \
        "显式抑制 live=1" in js, "只有「取消复盘」置位（日历「今天」不得被抑制）"
    assert "const liveIntent = !cancelReplay;" in js, "wantLive 分支未按意图算 liveIntent"
    assert "connectRealtimeInit(code, freq, savedDate, null, false, liveIntent)" in js, \
        "单窗 wantLive 未透传 liveIntent"
    assert "connectRealtimeDual(code, freq, dualSubFreq, null, null, null, false, liveIntent)" in js, \
        "双窗 wantLive 未透传 liveIntent"
    assert "function connectRealtimeInit(symbol, freq, startTime, endTime, resetSelection, liveIntent)" in js
    assert "function connectRealtimeDual(symbol, mainFreq, subFreq, endTime, startTime, subStartTime, resetSelection, liveIntent)" in js
    assert "_restoreDateInputToChart();" in js, "init 失败未把日历回落到图上末根"
    assert "live: bool = Query(False)" in route and route.count("live=live") == 2, \
        "路由未接收/未透传 live"
    assert "live=False):" in sse and sse.count("live=live") >= 3, \
        "生成器未把 live 透传给 init（单窗 + 双窗上下两处）"
    assert "if live and end_time is None and records:" in sse, \
        "live 判定位置丢失（必须只在无 end_time 的实时语义下判）"

    # ⑥ 端到端接线：init 抛出的 A″ 必须落成**带 code/suggest 的 init 错误帧**
    #    （漏传 live=live ⇒ 这一帧不出现，前端只会看到图被静默重载 —— 正是本次现场）
    import FrontAPI
    frames = list(FrontAPI.sse_futures_stream_single(
        "CFFEX.IF2410", "5m", None, None, live=True, source=_SseSrc(dict(bars))))
    assert len(frames) == 1, f"A″ 必须中止本次连接（实为 {len(frames)} 帧）"
    _raw = frames[0].decode("utf-8") if isinstance(frames[0], bytes) else frames[0]
    _pay = json.loads(_raw.split("data: ", 1)[1].split("\n")[0])
    assert _pay.get("code") == "contract_range", _pay
    assert _pay.get("suggest") == today_main, _pay
    assert "已无 CFFEX.IF2410 的行情" in _pay.get("error", ""), _pay
    print("[PASS] [7] A″：live=1 + 已交割合约 ⇒ 今天无行情 + 今日主力「切换」；"
          "初载 / 取消复盘不报；在市委约不误报；生成器转成带 code/suggest 的错误帧")


def main():
    restore_iso = isolate_side_effects()
    try:
        test_contract_suggest()
        test_classify()
        test_non_cffex_suggest_30m_missing()
        test_frontend_contracts()
        test_init_out_late()
        test_late_direction_stale_guard()
        test_switch_connection_drops_selection()
        test_live_target_on_stale_contract()
        test_browser()
    finally:
        restore_iso()
    print("\n全部通过：期货复盘四类诊断（v1.18 最近优先 + 换月窗口仲裁 + v1.20 A″ 回最新）")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)
