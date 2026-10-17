# -*- coding: utf-8 -*-
"""Test/test_futures_goto_earlier.py —— 期货复盘「目标早于窗口左界」四类诊断
=====================================================================
2026-10-17 v1.17：复盘输入日期 D 早于窗口左界 L 时**不改 L**（方案 §2 规则⑥），
而是按「L 被谁钉住」分四类，提示与出路各不相同（详见 Docs/选点&复盘方案v1.17.md §3.6）：

| code              | 触发条件                                   | 出路                     |
|-------------------|--------------------------------------------|--------------------------|
| contract_range    | 具体合约：D 不在该合约存续期（上市~交割）   | 切换到建议合约（按交割日算）|
| tq_10000          | D 早于该周期「最近 10000 根」首根           | 改用 5m / 30m            |
| before_selection  | 方式D：窗口以选点为界，D 早于选点           | 先取消选点                |
| way_c             | 方式C：窗口 = 最近 N 根（N<10000），D 可达   | **仅提示**「请加载更多数据」 |

判定顺序 = 硬约束优先（合约 → 10000 → 选点 → 方式C）；事实全部取数据真值
（合约存续期 = 该合约 30m 10000 根首/末根；10000 边界 = 当前周期首根；选点 = CSV）。

分层：[1] 合约推荐纯函数 / [2] 四类诊断（FakeSrc）/ [3] 源码契约
     / [4] 真浏览器（way_c 确认后重载、contract_range 提示）。
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
        self._futures_life_cache = {}
        self._futures_series_cache = {}

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
                            _suggest_contract_for_date, _classify_futures_replay)

    assert _third_friday(2024, 4).strftime("%Y-%m-%d") == "2024-04-19"
    assert _third_friday(2024, 5).strftime("%Y-%m-%d") == "2024-05-17"

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

    # 数据验证：只有「存续期覆盖 + 当前周期 10000 根覆盖」的候选才会被建议
    src = _fake_2409()
    assert _suggest_contract_for_date(src, SYMBOL, 60, TARGET) == "CFFEX.IF2405"
    assert _suggest_contract_for_date(src, "KQ.m@CFFEX.IF", 60, TARGET) == "CFFEX.IF2405"
    # 15s：候选的 10000 根只覆盖约 7 个交易日 → 04-22 取不到 → 不给建议（退化为换周期）
    assert _suggest_contract_for_date(src, SYMBOL, 15, TARGET) == ""

    # 四品种（IF/IH/IC/IM）同一套规则；主力按「目标日成交量最大」判，不看交割日远近：
    # IM 的季月 2409 量最大 → 必须建议 IM2409（不是交割日最近的 IM2405）
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
        assert got == f"CFFEX.{root}{hot}", f"{root} 应按当日量取主力，实为 {got!r}"
    # 未连接的数据源（真实 SSE 时序）：探针必须先补一次连接，A/B 不得退化成方式C
    lazy = _LazySrc(_fake_2409().bars)
    b = _classify_futures_replay(lazy, SYMBOL, 60, TARGET,
                                 start_dt=datetime(2024, 7, 19, 9, 30))
    assert lazy.connect_calls == 1, f"探针应补一次连接（connect 幂等），实为 {lazy.connect_calls}"
    assert b is not None and b.code == "tq_10000", \
        f"未连接时 A/B 探针失败会误报方式C，实为 {b and b.code}"
    assert b.suggest == "CFFEX.IF2405", b.suggest

    print("[PASS] [1] 合约候选/建议：CFFEX 四个真实月份（当月/下月/两季月）"
          "+ IF/IH/IC/IM 按目标日量取主力 + 数据验证当前周期可取 + 未连接时自动补连")


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

    # ③ tq_10000：IF2409 存续期内，但 1m 的 10000 根只到 07/19
    #    → 必须**绕过**该限制：给出同品种能取到 04-22 的合约（IF2405），而不是只说「换周期」
    b = _classify_futures_replay(src, SYMBOL, 60, TARGET, start_dt=datetime(2024, 7, 19, 9, 30))
    assert b is not None and b.code == "tq_10000", b and b.code
    assert "10000 根" in b.message and "2024/07/19 09:30" in b.message, b.message
    assert b.suggest == "CFFEX.IF2405", f"B 类必须给出可切换合约，实为 {b.suggest!r}"
    assert "主力合约 CFFEX.IF2405" in b.message, b.message
    assert "10000 根下限" in b.message, "B 类文案要点明天勤硬上限"

    # ③b 多个候选都能覆盖 → 必须选**目标日成交量最大**的（主力），不是交割日最近的非主力
    multi = _FakeSrc({
        (SYMBOL, 60): _span(datetime(2024, 7, 19, 9, 30), datetime(2024, 9, 20, 14, 59)),
        ("CFFEX.IF2405", 1800): _span(datetime(2024, 3, 18, 9, 30), datetime(2024, 5, 17, 14, 30)),
        ("CFFEX.IF2405", 60): _span(datetime(2024, 3, 20, 9, 30), datetime(2024, 5, 17, 14, 59),
                                    vol=50, mid=datetime(2024, 4, 22, 9, 30)),
        ("CFFEX.IF2406", 1800): _span(datetime(2024, 4, 22, 9, 30), datetime(2024, 6, 21, 14, 30)),
        ("CFFEX.IF2406", 60): _span(datetime(2024, 4, 22, 9, 30), datetime(2024, 6, 21, 14, 59),
                                    vol=900, mid=datetime(2024, 4, 22, 9, 30)),
    })
    b = _classify_futures_replay(multi, SYMBOL, 60, TARGET, start_dt=datetime(2024, 7, 19, 9, 30))
    assert b is not None and b.suggest == "CFFEX.IF2406", \
        f"多候选覆盖时应取当日成交量最大的主力合约，实为 {b.suggest!r}"

    # ④ before_selection：5m 下 D 可达，但早于选点
    b = _classify_futures_replay(src, SYMBOL, 300, TARGET,
                                 start_dt=datetime(2024, 5, 6, 9, 30), start_is_selection=True)
    assert b is not None and b.code == "before_selection", b and b.code
    assert "在选点" in b.message and "左侧" in b.message, b.message
    assert not b.suggest and not b.retryable, "方式D 仅提示，不给切换/重载动作"
    assert "取消选点" in b.message, "方式D 的出路是取消选点，提示里必须写"

    # ⑤ way_c：5m 下 D 可达、窗口只加载了最近 960 根
    b = _classify_futures_replay(src, SYMBOL, 300, TARGET,
                                 start_dt=datetime(2024, 8, 22, 9, 30), window_bars=960)
    assert b is not None and b.code == "way_c", b and b.code
    # 方式C **只提示**（出路是把 FUTURES_LOOKBACK_CONFIG 调大并重启，用户自行操作）
    assert not b.retryable and not b.suggest, (b.code, b.retryable, b.suggest)
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
    assert b.suggest == "", f"未来日期不应建议合约，实为 {b.suggest!r}"
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


# ═══════════════════ [5] init 晚向接线（A′ 可达性） ═══════════════════

def test_init_out_late():
    """端到端接线：D 晚于合约末根且非未来 → init 路径必须抛 A′。

    F1 指出的接线缺失：分类器单测覆盖了 A′，但 `init_chan_symbol` 原先只判
    「D < 10000 根首根」；晚向被 `_truncate_records_by_end` 静默吞掉，页面停在
    存续期末根、既无提示也无切换建议（前端 wantLive 只拦「今天/未来」）。
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
        assert b.suggest == "CFFEX.IF2410", b.suggest

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
    # 探针取数前必须确保已连接（否则 api=None → AttributeError → A/B 全退化）
    src_py = _read(SRC_PY)
    assert "_ensure_futures_src_connected(src)" in sse and sse.count("_ensure_futures_src_connected(src)") >= 2, \
        "两个探针（存续期 / 10000 根首根）都必须先确保数据源已连接"
    assert "def _ensure_futures_src_connected(src):" in sse, "缺补连实现"
    assert "if self.api is not None:" in src_py and src_py.count("def connect(self):") >= 1, \
        "CTqSdkSession.connect 必须幂等（重复调用会多建 websocket）"

    # 前端：期货分支不再有「请扩大数据范围」；改为按 code 渲染
    assert '请扩大数据范围' in js, "股票分支的越界提示不应被误删"
    assert '复盘日期 " + apiDate + " 早于已加载数据起点 " + _futReplayFloor' not in js, \
        "期货分支仍在前端拦（应交给后端分类）"
    # 方式C 只提示：前端不得给任何动作分支（出路是用户改配置并重启）
    assert 'if (_code === "way_c"' not in js, "方式C 不应有前端动作分支（仅提示）"
    assert '{okText: "加载更多"}' not in js, "不应提供「加载更多」按钮"
    assert 'connectRealtimeInit(symbol, _freqNow, "", _target)' not in js, \
        "不得替用户改 L 重载（方式C 的出路是调大配置后重启）"
    assert 'data.retryable' not in js, "前端不应依赖 retryable"
    assert 'if (data.suggest && (_code === "contract_range" || _code === "tq_10000"))' in js, \
        "缺「切换到建议合约」分支（10000 根 / 合约范围两类都要能一键绕过）"
    assert '{okText: "切换"}' in js, "切换按钮文案必须是「切换」（不是含糊的「确定」）"
    assert "let _pendingFuturesReplay = null;" in js, \
        "缺闭包变量（window API 面冻结，不能用 window.*）"
    assert 'connectRealtimeInit(code, fetchFreq, "", _endForLoad)' in js, \
        "切合约后必须以目标日为右端加载（方式C：L = D 往前 N 根），不能先方式A 冷启动"
    assert 'connectRealtimeDual(code, fetchFreq, subFreq, _endForLoad)' in js, "双窗同样要带目标日"
    assert "setTimeout(function () { gotoDate(); }, 0)" not in js, \
        "不应再用「冷启动后再 gotoDate」两步走（对已交割合约会先落在交割日附近、再弹一次加载更多）"
    assert "window.__pendingFuturesReplay" not in js, "不应新增 window 全局（phase6 window API 面冻结）"
    assert "_contract_volume_on" in sse and "vol > best_vol" in sse, \
        "多候选覆盖时应按当日成交量取主力合约"
    # 诊断帧必须在三处 init 调用点被接住（否则 = 连接异常中断，前端拿不到切换按钮）
    assert sse.count("except FuturesReplayBlocked as _blk:") >= 3, \
        "单窗/双窗上下窗三处 init 调用都必须捕获诊断，转成带 code 的错误帧"

    assert js.count('document.getElementById("goto-date-input").disabled = false;') >= 5, \
        "init 失败分支必须恢复日历输入框（单+双窗）"

    # 两个 UI bug 的修复锚点
    assert "if (e.key.startsWith('Arrow')) { _dateKeyArrow = true; _datePickerInteracted = true; return; }" in js, \
        "方向键未标记 _datePickerInteracted（blur 会回滚日期）"
    assert "本次输入值已被 gotoDate 消费" in js, "gotoDate 未清 _dateFocusOriginal（disabled 触发 blur 会回滚）"
    print("[PASS] [3] 源码契约：三处 init 都捕获诊断 / 前端按 code 渲染"
          "（A/B/E 切换；C/D/F/G 仅提示）/ UI 回滚与禁用两处修复")


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


def _payload(end_day):
    kl = _bars_5m(end_day)
    return {
        "meta": {"symbol": SYMBOL, "name": SYMBOL, "freq": "5分钟",
                 "kline_count": len(kl), "bi_count": 0, "fx_count": 0,
                 "zs_count": 0, "seg_count": 0, "bsp_count": 0,
                 "generated_at": kl[-1]["date"], "is_realtime": True,
                 "is_replay": False, "market": "futures",
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
    if (window.__failNext) { payload = window.__failNext; window.__failNext = null; }
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
                    "   return d ? d.innerText : ''; })()})")

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

            assert not errors, f"页面报错：{errors[:2]}"
            browser.close()
    finally:
        httpd.shutdown()
    print("[PASS] [4] 真浏览器：10000 根 → 「切换」到 IF2405 并以 04-22 为右端加载；"
          "方式C → 仅提示「请加载更多数据」，不动窗口、不重连")


def main():
    restore_iso = isolate_side_effects()
    try:
        test_contract_suggest()
        test_classify()
        test_frontend_contracts()
        test_init_out_late()
        test_browser()
    finally:
        restore_iso()
    print("\n全部通过：期货复盘四类诊断（v1.17）")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)
