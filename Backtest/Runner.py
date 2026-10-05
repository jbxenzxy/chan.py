# -*- coding: utf-8 -*-
"""
回测内核（Backtest/Runner.py）
==============================
设计文档 §5.2「核心循环：一次遍历同时做完三件事」的唯一实现。

为什么必须**一次遍历**（而不是"先采集全部信号、再逐笔评估"）
---------------------------------------------------------------------
拆成两步会立刻引入"最终态 vs 首见态"的问题：信号的止损锚
（`bsp.bi.get_end_klu()`）会随**笔延伸**漂移（实测 3/11，全是 3 类点）。
流式推进下，信号在哪一帧**首次出现**就在那一帧读分型 —— 那时笔尚未延伸，
`get_end_klu()` 返回的就是"当时当下"的值。**冻结 = 流式的自然结果**，
不是附加功能（设计文档 §2.6）。

每帧的处理顺序（顺序错了结果就错，§5.2 的"三个点"）
---------------------------------------------------------------------
    0️⃣ `pol.on_bar(bar, inst)`   —— **无条件**（持仓与否都收），ATR 从头连续
    1️⃣ 先结算已有持仓           —— 用刚闭合这根的 close 判 L1-L3
    2️⃣ 再看本帧新出现的信号      —— 只有 FLAT 态接收；一帧最多开一笔

    · ① 在 ② 之前 ⇒ **自动满足"入场那根不判出场"**，无需额外判断。
    · ① 触发平仓后**不 continue** ⇒ 同一根可以平了又开（Q11 定案）。
    · 冻结发生在 `pol.plan()` 那一行 ⇒ 分型天然是首见态。

`[L, R]` 的责任边界（设计文档 §4.4 / P0-2 方案 B）
---------------------------------------------------------------------
本模块**只保证"给什么区间就测什么区间"**。`start_dt` / `target_dt` 由调用方
（`FrontAPI` / `AppChart`，已 import `App`）按 App 侧口径
（`STOCKS_LOOKBACK_CONFIG` / `FULL_DATA_MODE`）解析好传入，本模块不做窗口规则解析
（那两处常量只在 `App/`，`Backtest` 禁 import `App`）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .ExitParams import STOCK_EXIT_PARAMS, TARGET_AMOUNT, round_trip_cost, shares_for
from .Filter import bsp_type_allowed
from .State import State


# ════════════════════════════════════════════════════════════════════
# 缠论侧构造（材料 ① ② ④ ⑤；设计文档 §6 P0「材料清单」）
# ════════════════════════════════════════════════════════════════════
def default_chan_config():
    """回测侧自持的缠论配置（P0-2 方案 B）。

    = `CChanConfig()` 全默认 = 页面 `App/AppUtils._make_chan_config()`。
    由 `Test/test_bt02_config_contract.py` 钉住"逐字段相等" —— 哪天 `App/` 侧
    给它加了一条覆盖，契约测试立刻变红，而不是让回测静默用另一套口径。

    ⚠ 必须带 `trigger_step=True`（`CChanConfig` 默认即真）：`CChan.step_load()`
      首行就是 `assert self.conf.trigger_step`。改成 False 会在第一帧就断言失败。
    """
    from ChanConfig import CChanConfig
    return CChanConfig()


def kl_type_of(freq: str):
    """回测侧的 `freq → KL_TYPE`（材料 ⑤）。

    ⚠ 刻意**不照抄** `App/AppUtils._get_kl_type`：它绕道
      `CTqSdkAPI.FREQ_SEC_MAP`（tqsdk 常量）再回退 `K_15S`，会让离线层多背一个
      tqsdk 依赖。回测直接用 `Common/CEnum.py` 的 `FREQ_TO_KL_TYPE` —— 那才是
      `FREQ_TABLE` 派生视图的 SSOT（`AppUtils.py:255-257` 的注释也这么写）。
    """
    from Common.CEnum import FREQ_TO_KL_TYPE
    key = str(freq)
    if key not in FREQ_TO_KL_TYPE:
        raise ValueError("Backtest: 未知周期 {!r}（合法值 = Common/CEnum.FREQ_TABLE 的键）"
                         .format(freq))
    return FREQ_TO_KL_TYPE[key]


def date_fmt_of(freq: str) -> str:
    """周期 → 日期格式串（与页面同源：`Common/func_util._get_date_fmt`）。

    只把分隔符 `/` 换成 `-`（回测内部统一横杠）。**不自己拼格式** ——
    `Signal.make_key` 是按字符串比较的，格式一变就是另一个键，两边必须同源。
    """
    from Common.func_util import _get_date_fmt
    return _get_date_fmt(freq).replace("/", "-")


def _ts_of(date_str: str, date_fmt: str) -> int:
    """日期串 → 毫秒时间戳（口径与页面一致：`App/AppEngine.py` 组装 bsp 时
    用 `datetime.strptime(date, date_fmt).timestamp()*1000`）。

    ⚠ **不用 `klu.time.ts`**：日线 klu 的 `CTime(auto=True)` 把 00:00 的
      timestamp 定在当天 23:59，与页面口径差一天内的 24 小时。
    """
    try:
        return int(datetime.strptime(date_str, date_fmt).timestamp()) * 1000
    except Exception:  # noqa: BLE001 —— 非法日期不该炸掉整轮回测；Signal 侧会拒绝 ts<=0
        return 0


def bar_from_klu(klu, date_fmt: str):
    """klu → `Trading.Infra.Records.Bar`（材料 ①）。

    ⚠ `Bar` **只有 `from_dict`、没有 `from_klu`**（`Trading/Infra/Records.py`）
      ⇒ 这个 ~10 行适配必须由 `Backtest` 自持（§5.9 R31：不得 import `App`）。
      字段名逐字照 `Bar.from_dict` 的键：timestamp/date/open/high/low/close/vol。
    """
    from Common.CEnum import DATA_FIELD
    from Trading.Infra.Records import Bar
    date = klu.time.toFmtStr(date_fmt)
    return Bar(
        timestamp=_ts_of(date, date_fmt),
        date=date,
        open=float(klu.open), high=float(klu.high),
        low=float(klu.low), close=float(klu.close),
        vol=klu.trade_info.metric.get(DATA_FIELD.FIELD_VOLUME),
    )


def bsp_to_dict(bsp, date_fmt: str) -> Dict[str, Any]:
    """`CBS_Point` → `Signal.from_bsp` 吃的 dict（材料 ②）。

    键与 `App/AppEngine.py:1141-1150`（主窗组装）**逐键一致** —— 那处是页面
    所见 bsp 的组装口径；回测禁 import `App` ⇒ 自持一份。
    常驻护栏：`Backtest/Test/test_bt05_bsp_dict.py` 做逐键比对（不做一次性对拍）。

    ⚠ `fractal_low` / `fractal_high` **必须显式给**：
      `Signal.from_bsp` 对它们走 `or 0.0` 兜底，缺省会**静默**退化成
      `fractal ≤ 0` 哨兵 ⇒ R 只剩 2×ATR，`Exit.py` 打 `[R 结构距离缺失]` 告警。
    """
    klu = bsp.klu
    f_klu = bsp.bi.get_end_klu()
    date = klu.time.toFmtStr(date_fmt)
    return {
        "date": date,
        "timestamp": _ts_of(date, date_fmt),
        "type": bsp.type2str(),
        "is_buy": bool(bsp.is_buy),
        "price": float(klu.close),
        "high": float(klu.high),
        "low": float(klu.low),
        "fractal_low": round(float(f_klu.low), 3),
        "fractal_high": round(float(f_klu.high), 3),
    }


def load_records(path: str) -> List[Dict[str, Any]]:
    """读 K 线记录 JSON → `list[dict]`（CLI 与测试的输入装载）。

    格式（与 `Test/gen_fixtures.py` 冻结切片、以及
    `DataAPI/CTdxAPI.read_main_level_records` 的输出**同构**）::

        [{"dt": "2021-01-04 00:00:00", "open": …, "high": …, "low": …,
          "close": …, "volume": …}, …]

    ⚠ 这 ~8 行**故意在 `Backtest/` 自持**，不 `import Test.gen_fixtures`：
      §5.1 层表给 `Backtest/` 的允许依赖是
      `Chan / Common / DataAPI / BuySellPoint / Trading.Strategy / Trading.Infra`
      —— `Test/` **不在内**。生产代码依赖测试树还有第二个后果：交付出去的
      `Backtest/` 单独一份就**跑不起来**。
      该约束由 `Backtest/Test/test_bt04_no_app_import.py` 的层表断言钉死。

    `dt` **必须**还原为 `datetime`：`DataAPI/CTdxAPI` 的读取路径按 `datetime`
    比较区间并按 `dt` 排序，喂字符串会在裁剪/排序处静默出错（不是报错，是错序）。
    """
    import json
    with open(path, "r", encoding="utf-8") as f:
        rows = json.load(f)
    for r in rows:
        r["dt"] = datetime.strptime(r["dt"], "%Y-%m-%d %H:%M:%S")
    return rows


# ════════════════════════════════════════════════════════════════════
# 结果结构
# ════════════════════════════════════════════════════════════════════
@dataclass
class BtTrade:
    """一笔往返（开 + 平）。`open_` 为真表示跑到末根仍未平仓。"""
    trade_id: int
    side: str                    # long / short
    bsp_type: str                # 信号类型串（type2str，可为 "1,11"）
    entry_date: str
    entry_price: float
    r_distance: float            # 该笔入场时刻算出的 R = max(A, 2×ATR)
    shares: int = 0              # 申报股数（按 target_amount + 板块最小申报单位）
    entry_frame: int = 0         # 入场帧号（算持仓根数用；与 records 下标同序）
    exit_date: Optional[str] = None
    exit_price: Optional[float] = None
    exit_reason: Optional[str] = None
    bars_held: Optional[int] = None
    r_multiple: Optional[float] = None      # 毛 R（不扣成本，§5.4c）
    gross_return: Optional[float] = None    # 毛收益率（纯价格比）
    net_return: Optional[float] = None      # 净收益率（扣双边成本）
    cost_cash: Optional[float] = None       # 双边成本（元）
    open_: bool = True


@dataclass
class RunResult:
    """一轮回测的完整结果（一只票、一个周期、一个区间）。"""
    market: str
    code: str
    freq: str
    start_dt: Optional[str] = None
    target_dt: Optional[str] = None
    bars_total: int = 0
    signals_seen: int = 0                # 首见信号总数（= 放行开仓 + 被过滤 + 被拒收）
    signals_filtered: int = 0            # 因**类型过滤**未放行（PRE，§4.7.3）
    signals_rejected: int = 0            # 放行了但持仓期被拒收（rej，Q13）
    type_appended_after_freeze: int = 0  # 冻结后类型才追加（预期 0，§2.6 代价 ①）
    trades: List[BtTrade] = field(default_factory=list)

    @property
    def closed(self) -> List[BtTrade]:
        return [t for t in self.trades if not t.open_]

    @property
    def still_open(self) -> List[BtTrade]:
        return [t for t in self.trades if t.open_]


# ════════════════════════════════════════════════════════════════════
# 内核
# ════════════════════════════════════════════════════════════════════
def _norm_day(x) -> str:
    """任意日期表示 → `YYYY-MM-DD`（`datetime` / `2021/08/23 00:00` 都收）。"""
    return str(x).replace("/", "-").strip()[:10]


def _slice_records(records: Sequence[Dict[str, Any]],
                   start_dt, target_dt) -> List[Dict[str, Any]]:
    """按 `[L, R]`（含端点）过滤 records。

    这是入口对 `[L, R]` 参数的**唯一**使用点 —— 本函数不解析窗口规则，
    只做"给我什么区间就测什么区间"的机械裁切（P0-2 方案 B 的边界）。
    """
    if start_dt is None and target_dt is None:
        return list(records)
    lo = _norm_day(start_dt) if start_dt is not None else None
    hi = _norm_day(target_dt) if target_dt is not None else None
    out = []
    for r in records:
        d = _norm_day(r.get("dt"))
        if lo is not None and d < lo:
            continue
        if hi is not None and d > hi:
            continue
        out.append(r)
    return out


def run(
    market: str,
    code: str,
    freq: str,
    start_dt: Optional[Any] = None,
    target_dt: Optional[Any] = None,
    *,
    records: Optional[Sequence[Dict[str, Any]]] = None,
    bsp_filter: Optional[Dict[str, Any]] = None,
    chan_config=None,
    exit_params: Optional[Dict[str, Any]] = None,
    target_amount: Optional[float] = None,
    max_bars: Optional[int] = None,
) -> RunResult:
    """跑一只票的一个周期（设计文档 §5.2）。

    参数
    ----
    market / code / freq   标的（`market` = "sh"/"sz"/"bj"，`code` = 6 位代码）
    start_dt / target_dt   `[L, R]`（含端点）。`None` = 不限；records 按此裁切
                           —— **由调用方按 App 侧口径解析好**（§4.4 / P0-2 方案 B）
    records                已加载的 K 线序列（`dt` 为 `datetime`，与
                           `Test/gen_fixtures.load_records` 输出同构）
    bsp_filter             类型过滤表 `{"0": True, ...}`；`None` = 全放行
    chan_config            缠论配置；`None` = `default_chan_config()`
    exit_params            出场参数；`None` = `STOCK_EXIT_PARAMS`
    target_amount          目标成交额；`None` = `m / k`（= 50 000）
    max_bars               调试用：最多跑多少帧
    """
    import Chan
    from DataAPI import TdxAPI
    from Common.CEnum import AUTYPE
    from Trading.Infra.Instrument import Instrument
    from Trading.Infra.Records import Position, Signal
    from Trading.Strategy.Exit import LayeredExitPolicy

    full_code = "{}{}".format(str(market).lower(), str(code))
    date_fmt = date_fmt_of(freq)
    cfg = chan_config if chan_config is not None else default_chan_config()
    params = dict(exit_params if exit_params is not None else STOCK_EXIT_PARAMS)
    tgt = float(target_amount) if target_amount is not None else TARGET_AMOUNT

    recs = _slice_records(records or [], start_dt, target_dt)
    result = RunResult(
        market=str(market).lower(), code=str(code), freq=str(freq),
        start_dt=(None if start_dt is None else str(start_dt)),
        target_dt=(None if target_dt is None else str(target_dt)),
    )

    # ★ 全程唯一实例：ATR 从头连续累积（`deque(maxlen=atr_period+2)` 只留末 16 根，
    #   与"每次入场前预热 60 根"的尾窗相同 ⇒ 两条路径算出的 ATR 完全相等，
    #   即 §5.2 的 v1.6 更正 —— 差异为零，不是近似）。
    pol = LayeredExitPolicy(params)
    inst = Instrument(product=None)   # 无品种档案 ⇒ round_price 原样返回（不做 tick 对齐，Q6）

    state = State.FLAT
    pos = None
    frozen_types: Dict[Tuple[str, bool], str] = {}   # 冻结键 → 首见时的 type2str
    trade_seq = 0
    frame = 0

    with TdxAPI.tdx_data_context(recs):
        chan = Chan.CChan(
            code=full_code, begin_time=None, end_time=None,
            data_src="custom:TdxAPI.CTdxAPI", lv_list=[kl_type_of(freq)],
            config=cfg, autype=AUTYPE.NONE, market_type="stock",
        )
        for _ in chan.step_load():
            frame += 1
            if max_bars is not None and frame > max_bars:
                break
            cur_klu = chan[0][-1][-1]      # 本帧刚加入的原始 K 线（最高级别）
            bar = bar_from_klu(cur_klu, date_fmt)
            result.bars_total = frame

            # 0️⃣ 无条件收 bar（`Exit.py:on_bar` 的契约：每根 K 线都调用）
            pol.on_bar(bar, inst)

            # 1️⃣ 先结算已有持仓（用刚闭合这根的 close 判 L1-L3）
            if state is State.IN_TRADE and pos is not None:
                chk = pol.check(pos, bar, inst)
                if chk is not None:
                    if chk.plan is not None:
                        pos.exit_plan = chk.plan     # 先抬价（进保本 / 跟踪）
                    if not chk.only_update:
                        _close_trade(result, pos, chk, bar.date, frame)
                        pos = None
                        state = State.FLAT
                        # ★ 不 continue（Q11）：同根允许再开新仓

            # 2️⃣ 再看本帧新出现的信号（PRE 过滤 → 状态机）
            new_sigs = _new_signals_this_frame(chan, cur_klu, frozen_types, result, date_fmt)
            for bsp, bdict in new_sigs:
                result.signals_seen += 1
                # 类型过滤在**进状态机之前**（§4.7.3）：未放行 ⇒ 不占持仓期、
                # 不产生"拒收"计数 —— 过滤与拒收是两层，计数分开。
                if not bsp_type_allowed(bdict["type"], bsp_filter):
                    result.signals_filtered += 1
                    continue
                if state is State.IN_TRADE:
                    result.signals_rejected += 1      # 持仓期拒收（Q13）
                    continue
                sig = Signal.from_bsp(bdict, symbol=full_code, freq=str(freq))
                entry = float(bdict["price"])
                plan = pol.plan(sig, entry, inst)     # ★ 冻结就在这一行（首见态）
                pos = Position(
                    symbol=full_code, side=sig.side, volume=1, entry_price=entry,
                    entry_at="", entry_bar_ts=int(bdict["timestamp"]),
                    signal_key=sig.key, open_order_id="", exit_plan=plan)
                trade_seq += 1
                result.trades.append(BtTrade(
                    trade_id=trade_seq,
                    side=("long" if sig.side.value > 0 else "short"),
                    bsp_type=str(bdict["type"]),
                    entry_date=bar.date,
                    entry_price=entry,
                    r_distance=float(plan.params.get("R") or 0.0),
                    shares=shares_for(entry, full_code, tgt),
                    entry_frame=frame,
                ))
                state = State.IN_TRADE
                break                                  # 一帧最多开一笔

    return result


def _new_signals_this_frame(chan, cur_klu, frozen_types: Dict, result: RunResult,
                            date_fmt: str) -> List[Tuple[Any, Dict[str, Any]]]:
    """本帧**首次出现且落在当前根**的 bsp（§2.6 定案 (i)）。

    ⚠ 遍历的是**全量** `bsp_iter()` 而非某个增量集合：`BSPointList.clear_store_end()`
      会**删除**失效买卖点（笔延伸导致 `bi.get_end_klu().idx <= last_sure_pos`），
      集合不是单调增长的 ⇒ 必须每帧看当前态。
      成本 = 帧数 × 当前 bsp 数（本样本 1393 × ~10），可忽略。

    「冻结后类型才追加」的检测（§2.6 代价 ①，预期为 0）：同一冻结键的 `type2str()`
    在首见之后发生变化 ⇒ 那一帧不会被放行（首个类型没勾选时），单独计数披露。

    ⚠ 只有一个信号会被处理的情形：类型过滤**未**放行的信号这里**不**剔除 ——
      剔除动作在上层（`run()`），因为"过滤"要计入 `signals_filtered`。
      本函数只负责"是不是本帧首见"，不负责"放不放行"。
    """
    out: List[Tuple[Any, Dict[str, Any]]] = []
    for bsp in chan[0].bs_point_lst.bsp_iter():
        key = (str(bsp.klu.time), bool(bsp.is_buy))
        types = bsp.type2str()
        if key in frozen_types:
            if frozen_types[key] != types:
                result.type_appended_after_freeze += 1
                frozen_types[key] = types
            continue
        if bsp.klu.idx != cur_klu.idx:
            continue                      # 历史信号仍留在 store 里，不筛就会重放
        frozen_types[key] = types
        out.append((bsp, bsp_to_dict(bsp, date_fmt)))
    return out


def _main(argv=None) -> int:
    """P0 的单标的单周期 CLI（§6 P0「CLI 跑单标的单周期」）。

    ⚠ **不建 `Cli.py`**：§5.1 目录树里的 `Cli.py` 是**全市场批跑**入口（= §9 = P3）。
      P0 只需要"跑一只票" ⇒ 做成本模块的 `__main__`，免得后来人往 `Cli.py` 里塞 §9。

    用法::

        python -m Backtest.Runner --records Test/fixtures_real/sz002190_d.json \\
            --market sz --code 002190 --freq d [--from 2021-01-01] [--to 2026-09-30] \\
            [--bsp-types 0,3] [--csv out.csv]
    """
    import argparse
    import json
    import os
    import sys

    ap = argparse.ArgumentParser(prog="python -m Backtest.Runner",
                                 description="股票胜率回测（单标的单周期）")
    ap.add_argument("--records", required=True,
                    help="K 线记录 JSON（Test/gen_fixtures.load_records 的格式）")
    ap.add_argument("--market", default=None, help="sh / sz / bj（默认从 code 前缀推断）")
    ap.add_argument("--code", required=True, help="6 位代码，如 002190")
    ap.add_argument("--freq", default="d", help="w / d / 30m / 15m / 5m")
    ap.add_argument("--from", dest="start_dt", default=None, help="区间左端 [L]（YYYY-MM-DD）")
    ap.add_argument("--to", dest="target_dt", default=None, help="区间右端 [R]（YYYY-MM-DD）")
    ap.add_argument("--bsp-types", default=None,
                    help="只放行这些类型，逗号分隔，如 0,3（默认全放行）")
    ap.add_argument("--csv", default=None, help="把逐笔清单写到该 CSV")
    args = ap.parse_args(argv)

    market = args.market or (args.code[:2].lower()
                             if args.code[:2].lower() in ("sh", "sz", "bj") else "sz")
    code = args.code[2:] if args.code[:2].lower() in ("sh", "sz", "bj") else args.code

    records = load_records(args.records)

    bsp_filter = None
    if args.bsp_types:
        from .Filter import filter_from_choices
        bsp_filter = filter_from_choices(args.bsp_types)

    result = run(market, code, args.freq, args.start_dt, args.target_dt,
                 records=records, bsp_filter=bsp_filter)

    from .Metrics import compute
    from .Report import summary_text, trades_table, write_csv
    print(summary_text(result, compute(result)))
    print()
    print(trades_table(result))
    if args.csv:
        write_csv(result, args.csv)
        print("\nCSV → {}".format(os.path.abspath(args.csv)))
    return 0


def _close_trade(result: RunResult, pos, chk, exit_date: str, frame: int) -> None:
    """把成交结果写回本笔的 trade 行（毛 / 净两个口径，§5.4c）。"""
    t = result.trades[-1]
    fill = float(chk.fill_price if chk.fill_price else chk.price)
    sign = int(pos.side.sign)
    R = float(t.r_distance or 0.0)
    t.exit_date = exit_date
    t.exit_price = fill
    t.exit_reason = str(chk.reason)
    t.bars_held = int(frame - t.entry_frame)
    t.r_multiple = round((fill - pos.entry_price) * sign / R, 4) if R > 0 else None
    t.gross_return = (((fill - pos.entry_price) * sign / pos.entry_price)
                      if pos.entry_price else None)
    cost = round_trip_cost(pos.entry_price, fill, sign, t.shares)
    t.cost_cash = round(cost, 4)
    notional = float(t.shares) * float(pos.entry_price)
    t.net_return = (t.gross_return - cost / notional) if notional else None
    t.open_ = False


if __name__ == "__main__":   # pragma: no cover
    import sys
    sys.exit(_main())
