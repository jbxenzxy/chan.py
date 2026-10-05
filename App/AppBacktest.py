# -*- coding: utf-8 -*-
"""
App/AppBacktest.py —— 股票页「回测」入口（/api/stocks/{code}/backtest）
=========================================================================
把 `Backtest/` 的回测内核接到当前页面：**前端把页面正在显示的那条 K 线序列
（`chartData.klines`）送上来，本模块跑一遍，把报告原路返回**。

数据来源与区间口径（设计文档 §4.4 / §4.3 Q8）
---------------------------------------------------------------------
`chartData.klines` 就是后端的**加载序列**（`AppEngine._extract_main_level_data`
里由 `for i, row in enumerate(records)` 逐根产出），**不是前端视口** —— 视口
`VIEW_COUNT = 233` 只影响渲染。⇒ 前端送什么区间，回测就测什么区间，
**所见即所测**；`[L, R]` 无需另传、也不该由本模块重新解析（P0-2 方案 B
"区间由调用方解析好"：这里的"调用方"就是持有 `records` 的页面本身）。

为什么不学"后端自己取数"
---------------------------------------------------------------------
与 `App/AppTPSL.py` 同一模式（`compute_stock_tpsl` 也吃前端 klines）。区间继承
窗口状态（冷启动 / 复盘 `end_date` / 选点 `start_time` / 双窗下窗），要在后端重解析
就得复制 `AppEngine` 里那 100 行 `[L,R]` 优先级逻辑（§4.4 的方式 A/B/C/D），
复制品必然与页面漂移。

**不落盘、不做持久化**（Q8 定案）：请求进来 → 内存里跑完 → 响应返回。
CSV / HTML 报告导出留给 CLI 批跑。

已知边界（写进注释防后来者当 bug 修）
---------------------------------------------------------------------
  - 股票 K 线是**前复权**价，与真实成交价不同；不建模停牌 / 涨跌停（按 T+0 处理，
    不套 T+1）—— 与「止盈止损」推演同一条披露，见响应 `disclosures`。
  - 双窗态：本入口只复刻**单窗口**口径（`lv_list=[主级别]`、不启用区间套）。
    页处于双窗态时由**前端**拦截（禁用 + 提示"请切回单窗"），本模块不做猜测。
  - **指数标的**（判定 SSOT = `App/AppUtils.is_index`，与 `meta.is_index` /
    前端「成分股」置灰同源）：取数分支与形态引擎与个股
    完全同源，但**元口径整族置 null** —— `shares` / `cost_cash` / `net_return_pct`、
    `caliber.{target_amount,min_lot,lot_step,min_lot_derived_trades,max_notional,
    max_notional_multiple}` 以及 `summary.avg_net_return_pct` 一律 `None`，
    并在 `caliber.lines` 末尾与 `disclosures` 各追加一条「不适用」声明。
    指数不可交易 ⇒ 这些数字没有对应标的物（实测上证指数 `max_notional` 达
    `target_amount` 的 7.9 倍，纯属"指数点位高/`min_lot` 兜底"的假象）。
    **价格侧口径照常**：胜率 / 毛 R / 盈亏比 / 持仓根数 —— 指数页的价值就在于此。
  - 出场原因是**三选一**（止损 / 保本(1R) / 跟踪止盈），文案由
    `Backtest.Report.exit_reason_labels()` 生成并随响应下发（`exit_reason_labels` /
    `exit_reason_legend`）⇒ 前端**不硬编码**任何 reason 文案，与 `python -m
    Backtest.Runner` 的控制台摘要永远是同一份。逐笔的 `exit_reason` 仍是引擎原值
    （数据契约，`sl` / `breakeven` / `trailing`）。
  - **未平仓笔**多三个字段：`unrealized_price` / `unrealized_r` /
    `unrealized_net_return_pct` —— 截止**最后一根 K 线收盘价**的"假如现在平"估值。
    已平仓笔这三项恒为 `null`（两族字段不共用槽位，见 `Backtest/Runner.BtTrade`）。
    它们**不进**任何胜率口径：`Metrics` 只吃已平仓笔。

依赖方向：FrontAPI（路由）→ AppChart（漏斗壳）→ 本模块 → Backtest / Trading
（顺向，App 在最上层）。`Backtest/` 对 `App` 零依赖由
`Backtest/Test/test_bt04_no_app_import.py` 钉住；本模块反向 import 它们合法。
"""
from __future__ import annotations

from typing import Any, Dict, List

from App.AppErrors import AppError, BadRequestError
# 页面级「是不是指数」的单一事实源（同时驱动 meta.is_index 与前端「成分股」置灰）。
# 回测面板的指数徽标必须与它同源 —— 否则会出现「成分股按钮已置灰、回测面板却仍在
# 报成本倍率」的自相矛盾。别名 `_app_is_index` 避免与响应字段 `is_index` 撞名。
from App.AppUtils import is_index as _app_is_index

# K 线条数上限：页面加载序列的上界 —— 股票侧 `STOCKS_LOOKBACK_CONFIG` 最大
# 960（5m/15m）、周线不限量（全历史 ≈ 300），留足余量即可。
# 注意与 `AppTPSL._MAX_KLINES = 2000` 不同：那个是单笔推演（只用到入场点之后
# 的一小段），本模块要跑**整条序列**，故上限按加载序列的上界给。
_MAX_KLINES = 20000

# 股票侧开放周期（SSOT：`Common.CEnum.STOCKS_FREQS`；60m 不在产品开放列表）
def _stock_freqs() -> set:
    from Common.CEnum import STOCKS_FREQS
    return set(STOCKS_FREQS)


def _split_code(code: str):
    """`sz002190` → `('sz', '002190')`；校验失败抛 BadRequestError。"""
    c = str(code or "").strip()
    if not c or "." in c or len(c) < 3:
        raise BadRequestError("非股票代码: {!r}".format(code))
    market, num = c[:2].lower(), c[2:]
    if market not in ("sh", "sz", "bj", "hk"):
        raise BadRequestError("未知市场前缀: {!r}".format(code))
    return market, num


def _klines_to_records(klines: List[Dict[str, Any]], freq: str):
    """前端 `chartData.klines` → `Backtest.Runner` 期望的 records。

    日期：前端是斜杠格式（`Common.func_util._get_date_fmt`：日线 `%Y/%m/%d`、
    分钟级 `%Y/%m/%d %H:%M`），而 `Runner.date_fmt_of` 用连字符 —— 两处都是
    各自层的 SSOT，不做其一去迁就另一，转换放在本边界层。
    `dt` 必须是 `datetime`：喂字符串会在取数层的区间比较/排序处**静默错序**。
    """
    from datetime import datetime
    from Backtest.Runner import date_fmt_of

    fmt = date_fmt_of(freq)
    out = []
    for k in klines:
        raw = str(k.get("date") or "")
        if not raw:
            raise BadRequestError("klines 条目缺 date")
        try:
            dt = datetime.strptime(raw.replace("/", "-"), fmt)
        except ValueError as e:
            raise BadRequestError("klines.date 与周期不匹配: {!r} ({})".format(raw, e))
        missing = [key for key in ("open", "high", "low", "close", "vol")
                   if k.get(key) is None]
        if missing:
            raise BadRequestError("klines 条目缺字段: {}".format(",".join(missing)))
        out.append({
            "dt": dt,
            "open": float(k["open"]), "high": float(k["high"]),
            "low": float(k["low"]), "close": float(k["close"]),
            "vol": float(k["vol"]), "amount": float(k.get("amount") or 0.0),
        })
    return out


def _pct(x):
    """小数收益率 → 百分数（响应里所有 `*_pct` 字段都已 ×100，前端直接用）。

    为什么不把小数原样给前端：面板口径就是「每笔净收益率 %」（§5.4d-quater
    第 10 节），前端每处都要记得 ×100 的话，漏一处就是一个安静的数值错误。
    """
    return None if x is None else round(float(x) * 100.0, 6)


def _bucket_rows(groups: Dict[str, Any], is_index: bool = False) -> Dict[str, Any]:
    """分类型 / 分原因桶（`is_index` = True 时金额口径的净收益率置 null）。

    `n/w/l/e` 与 `expectancy_r` 是价格侧口径，指数与个股都成立，照常给。
    """
    out = {}
    for k, v in sorted((groups or {}).items()):
        out[str(k)] = {
            "n": int(v["n"]), "w": int(v["w"]), "l": int(v["l"]), "e": int(v["e"]),
            "avg_net_return_pct": None if is_index else _pct(v.get("avg_net_return")),
            "expectancy_r": (None if v.get("expectancy_r") is None
                             else round(float(v["expectancy_r"]), 6)),
        }
    return out


def _disclosures(is_index: bool) -> List[str]:
    """口径披露（与「止盈止损」推演同款三条）；指数额外追加「不适用」一条。

    为什么单列成函数而不是就地写列表字面量：`is_index` 分流是本响应契约的一部分
    （护栏 `Test/test_stock_backtest.py` 会逐条比对），集中一处便于断言。
    """
    base = [
        "不套 T+1（按 T+0）",
        "不建模涨跌停 / 停牌",
        "前复权价（非真实成交价）",
    ]
    if is_index:
        base.append("指数不可交易：股数 / 成本 / 净收益率口径不适用，"
                    "仅价格侧指标（胜率、毛 R、盈亏比、持仓根数）有效")
    return base


def compute_stock_backtest(code: str, body: Dict[str, Any]) -> Dict[str, Any]:
    """股票页「回测」主入口（/api/stocks/{code}/backtest）。

    body: `{code, freq, klines, bsp_types?}`
      - `klines`   当前页面加载序列（`chartData.klines`），**必填**
      - `bsp_types` 页面「设置 → 买卖点类型」勾选串（如 `"0,3"`），缺失 = 全放行。
                    与自动下单/成交统计共用同一份 SSOT（§4.7.3），回测按**事前
                    过滤**口径消费（不是事后过滤成交）。

    校验失败抛 `BadRequestError`（FrontAPI 统一映射 400）。
    """
    freq = str(body.get("freq") or "d")
    klines = body.get("klines") or []

    market, num = _split_code(code)
    if freq not in _stock_freqs():
        raise BadRequestError("不支持的周期: {!r}（股票开放周期 {}）".format(
            freq, ",".join(sorted(_stock_freqs()))))
    if not isinstance(klines, list) or not klines:
        raise BadRequestError("klines 为空")
    if len(klines) > _MAX_KLINES:
        raise BadRequestError("klines 超上限（{} > {}）".format(len(klines), _MAX_KLINES))

    records = _klines_to_records(klines, freq)

    # 惰性 import（照 AppTPSL 范式）：`Backtest.Runner` 会链式 import
    # `Trading.Strategy.Exit`，而 `Trading/Config.py` 在导入期构造 DEFAULT_CONFIG
    # （.env 笔误会让 import 失败）⇒ 导入期副作用必须收敛成明确的服务端错误。
    try:
        from Backtest import Runner
        from Backtest.Metrics import compute as compute_metrics
        from Backtest.Filter import filter_from_choices
        from Backtest.Report import (caliber_lines, exit_reason_labels,
                                     exit_reason_legend)
        from Backtest.ExitParams import TARGET_AMOUNT, lot_rule
    except Exception as e:                                            # noqa: BLE001
        raise AppError("回测初始化失败（Backtest/Trading 导入异常）：{}: {}".format(
            type(e).__name__, e)) from e

    choices = body.get("bsp_types")
    # `None`（前端未启用过滤）与 `""`（四类全不勾）是**两种不同语义**：
    #   前者 = 全放行，后者 = 全部过滤掉（0 笔）。用 `if choices` 会把二者合并，
    #   于是"全不勾"静默变成"全放行" —— 恰好相反。
    filt = None if choices is None else filter_from_choices(choices)

    # ── 指数分流（§4.4 指数页）──────────────────────────────────────────
    #    指数**不可交易** ⇒ 「元口径」整族（target_amount / 股数 / 名义金额 / 成本 /
    #    净收益率）在这类标的上是虚构数字，一律置 null 并在 caliber / disclosures
    #    里点名「不适用」。**价格侧口径不动**（胜率 / 毛 R / 盈亏比 / 持仓根数），
    #    它们在指数上依然成立 —— 指数页的价值本来就在于"形态信号本身的胜率"。
    #    判定 SSOT = `App.AppUtils.is_index`（页面级：含 88xx 板块指数 / ds 扩展指数 /
    #    hk 字母代码），**不是** `DataAPI.TdxAPI._is_index_code`（那只是取数层的
    #    A 股指数段判定）—— 用后者会漏 88xxxx，与 `meta.is_index` 自相矛盾。
    #    判定结果注入 `Runner.run(is_index=...)`：`Backtest/` 禁 import `App`
    #    （§5.9 R31 层表），故判定只能在 App 层做、在 Backtest 层消费。
    is_index = bool(_app_is_index(market, num))

    # `[L, R]` = 页面加载序列的首尾（§4.4：所见即所测）。传进去而不是留 None：
    #   ① 口径行要显示真实区间（留 None 会打成"(不限)"）；② `_slice_records` 按同
    #   一区间裁切是**恒等变换**（records 本来就等于这个区间），不改变样本。
    res = Runner.run(market, num, freq, records=records,
                     start_dt=(records[0]["dt"] if records else None),
                     target_dt=(records[-1]["dt"] if records else None),
                     bsp_filter=filt, is_index=is_index)
    met = compute_metrics(res)

    def _amt(v):
        """金额 / 成本族字段 → 指数时置 null（个股原样返回）。

        ⚠ **只在构响应字典时调用**，不要提前把局部变量置 None：下游还有
          `int(min_lot)` / `max_notional / TARGET_AMOUNT` 这类运算，
          早置空会先炸在 `int(None)` 上（而不是安静地给出 null）。
        """
        return None if is_index else v

    # ── 仓位口径（三项披露：min_lot / 借道笔数 / 放大倍数）──
    #    "借道" = 该笔股数由 `min_lot` 兜底（而非 target_amount/price）决定。
    #    判据直接照 `shares_for` 的式子：`target_amount / price <= min_lot`
    #    ⇒ 内层 max 取了 min_lot。高价股（茅台）典型。
    #    这里的**计算照常做**（指数也走一遍，保证两路同源）；分流发生在构响应处。
    min_lot, lot_step = lot_rule("%s%s" % (market, num))
    min_lot_derived = sum(
        1 for t in res.trades
        if t.entry_price and t.entry_price > 0
        and (TARGET_AMOUNT / float(t.entry_price)) <= min_lot
    )
    notionals = [float(t.shares) * float(t.entry_price or 0.0) for t in res.trades]
    max_notional = max(notionals) if notionals else 0.0

    # ── 出场原因显示文案（三选一）───────────────────────────────────────
    #    SSOT 在 `Backtest/Report.py`（层表：Backtest 不得 import App，反过来合法）
    #    ⇒ 前端**不硬编码**任何 reason 文案，只按后端给的映射查表；
    #    控制台摘要与页面面板因此永远是同一份文案。
    #    「保本(1R)」里的 1 来自 `res.exit_params`（本轮实际用的参数），不是常量。
    _reason_labels = exit_reason_labels(res.exit_params)
    _reason_legend = exit_reason_legend(res.exit_params)

    trades = []
    for t in res.trades:
        trades.append({
            "trade_id": int(t.trade_id),
            "side": str(t.side),
            "bsp_type": str(t.bsp_type),
            "entry_date": t.entry_date,
            "entry_price": None if t.entry_price is None else round(float(t.entry_price), 6),
            "r_distance": None if t.r_distance is None else round(float(t.r_distance), 6),
            "shares": _amt(int(t.shares)),
            "exit_date": t.exit_date,
            "exit_price": None if t.exit_price is None else round(float(t.exit_price), 6),
            "exit_reason": t.exit_reason,
            "bars_held": None if t.bars_held is None else int(t.bars_held),
            "r_multiple": (None if t.r_multiple is None
                           else round(float(t.r_multiple), 6)),
            "gross_return_pct": _pct(t.gross_return),
            "net_return_pct": _amt(_pct(t.net_return)),
            "cost_cash": _amt(None if t.cost_cash is None else round(float(t.cost_cash), 4)),
            # 未平仓浮动（截止最后一根 K 线收盘价的估值，**不是**成交结果）：
            #   已平笔这三项恒为 null（Runner 只在 `still_open` 上填），前端据此分流。
            #   指数时百分比不适用（净收益率族）⇒ 只留价格侧的 `unrealized_r`。
            "unrealized_price": (None if t.unrealized_price is None
                                 else round(float(t.unrealized_price), 6)),
            "unrealized_r": (None if t.unrealized_r is None
                             else round(float(t.unrealized_r), 6)),
            "unrealized_net_return_pct": _amt(_pct(t.unrealized_net_return)),
            "open": bool(t.open_),
        })

    from Common.CEnum import FREQ_TABLE
    label = FREQ_TABLE.get(freq, (None, 0, freq, False, False))[2] or freq

    return {
        "ok": True,
        "target": {
            "code": "%s%s" % (market, num),
            "market": market,
            "freq": freq,
            "freq_label": label,
            "is_index": is_index,
            "bars": int(res.bars_total),
            "date_from": records[0]["dt"].strftime("%Y-%m-%d") if records else None,
            "date_to": records[-1]["dt"].strftime("%Y-%m-%d") if records else None,
        },
        "run": {
            "bars_total": int(res.bars_total),
            "signals_seen": int(res.signals_seen),
            "signals_filtered": int(res.signals_filtered),
            "signals_rejected": int(res.signals_rejected),
            "filled": int(len(res.trades)),
            "closed": int(met.n),
            "still_open": int(met.u),
        },
        "summary": {
            "n": int(met.n), "w": int(met.w), "l": int(met.l), "e": int(met.e),
            "u": int(met.u),
            "win_rate": None if met.win_rate is None else round(float(met.win_rate), 6),
            "profit_loss_ratio": (None if met.profit_loss_ratio is None
                                  else round(float(met.profit_loss_ratio), 6)),
            "profit_factor": (None if met.profit_factor is None
                              else round(float(met.profit_factor), 6)),
            "avg_net_return_pct": _amt(_pct(met.avg_net_return)),
            "expectancy_r": (None if met.expectancy_r is None
                             else round(float(met.expectancy_r), 6)),
            "avg_bars_held": (None if met.avg_bars_held is None
                              else round(float(met.avg_bars_held), 6)),
            "max_win_r": None if met.max_win_r is None else round(float(met.max_win_r), 6),
            "max_loss_r": None if met.max_loss_r is None else round(float(met.max_loss_r), 6),
        },
        "by_bsp_type": _bucket_rows(met.by_bsp_type, is_index),
        "by_reason": _bucket_rows(met.by_reason, is_index),
        # 出场原因三选一的显示文案 + 口径说明（键序 = sl / breakeven / trailing，稳定）
        "exit_reason_labels": _reason_labels,
        "exit_reason_legend": _reason_legend,
        "trades": trades,
        "caliber": {
            "lines": list(caliber_lines(res)),
            "is_index": is_index,
            "target_amount": _amt(float(TARGET_AMOUNT)),
            "min_lot": _amt(int(min_lot)),
            "lot_step": _amt(int(lot_step)),
            "min_lot_derived_trades": _amt(int(min_lot_derived)),
            "max_notional": _amt(round(max_notional, 2)),
            "max_notional_multiple": (_amt(round(max_notional / TARGET_AMOUNT, 4))
                                      if TARGET_AMOUNT else None),
            "bsp_types": (None if choices is None else str(choices)),
        },
        "disclosures": _disclosures(is_index),
    }
