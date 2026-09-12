# -*- coding: utf-8 -*-
"""
DataAPI/MarketStatsAPI.py —— 行情统计口径（PE-TTM / 流通市值）统一取数入口
=========================================================================
收口「按市场选择数据源」这**唯一一条**规则，供 App 层消费：

    A 股（sh / sz / bj） → DataAPI.ElTdxAPI（通达信网络行情，单源）
    港股（hk）           → DataAPI.TxAPI（腾讯行情）

为什么需要本模块：eltdx 无港股通道（`codes.all("hk")` 抛 ValueError、0x054c
快照对 hk 不可用），而腾讯行情不提供北交所全量口径；若把「按市场分流」写在
各调用方，规则会有多份副本、随数据源变动而漂移。此处收成一份，调用方只认
本模块，不再直接 import ElTdxAPI / TxAPI 取这两个口径。

契约（与两个数据源模块的同名函数一致，故可互换）：
  - fetch_pe_ttm(mkt_codes)      → {mkt+code: float}   PE-TTM（倍），批量
  - fetch_pe_ttm_live(market, code) → {mkt+code: float} PE-TTM（倍），单只实时
  - fetch_float_mc(stock_list)   → {code: float}       流通市值（亿元）

失败语义（**按源隔离，不静默降级**）：
  - 本模块只做分流与合并，不做单位换算、不做字段解析、不做值兜底。
  - fetch_pe_ttm：某一源异常时 log.error 显著上报，并继续返回另一源的结果
    ——两个市场互不阻塞（PE 刷新与「指数归属刷新」在同一次刷新流程里顺序执行，
    此处抛异常会连带跳过后续无关步骤）。缺失部分由调用方按「未获取到」统计。
  - fetch_pe_ttm_live：**不吞异常**。它是 K 线页面「打开即取」的单只实时入口，
    调用方（AppData 实时层）需要据异常区分「取数失败」与「该票确实无 PE」，
    失败时保留旧值；若此处也静默返回 {}，两者将不可区分，页面会被刷成空。
  - fetch_float_mc：**不吞异常**。唯一调用方（AppScan 扫描前置取数）已有
    「回退本地缓存 + 缓存过期告警」兜底路径，此处重复吞异常会造出第二条
    兜底路径（同一件事两处实现），故异常直接上抛。
"""
import logging

from DataAPI import ElTdxAPI, TxAPI

log = logging.getLogger(__name__)


def _split_by_source(mkt_codes):
    """把 (mkt, code) 序列按市场分流为 (eltdx 组, 腾讯组)。

    分流规则在本模块内的**唯一实现**——fetch_pe_ttm 与 fetch_pe_ttm_live
    共用本 helper，避免「A股→eltdx / 港股→腾讯」这条规则出现第二份副本。
    """
    eltdx_pairs = [(m, c) for m, c in mkt_codes if m in ElTdxAPI.ELTDX_MARKETS]
    # 未列入 eltdx 支持范围的市场仍走腾讯（保持改造前对各市场的既有覆盖）
    tx_pairs = [(m, c) for m, c in mkt_codes if m not in ElTdxAPI.ELTDX_MARKETS]
    return eltdx_pairs, tx_pairs


def fetch_pe_ttm(mkt_codes):
    """批量获取 PE-TTM（滚动市盈率），返回 {mkt+code: float}，单位：倍。

    mkt_codes: list[(mkt, code)]，mkt ∈ {sh, sz, bj, hk}。
    A 股走 eltdx（0x06B9 统计文件，单请求覆盖全市场）；港股走腾讯行情。
    """
    if not mkt_codes:
        return {}

    eltdx_pairs, tx_pairs = _split_by_source(mkt_codes)

    result = {}
    if eltdx_pairs:
        try:
            result.update(ElTdxAPI.fetch_pe_ttm(eltdx_pairs))
        except Exception as e:                      # noqa: BLE001 —— 单源失败不阻塞另一市场
            log.error("[行情统计] PE-TTM 的 A 股源(eltdx) 取数失败，"
                      "本次 A 股 PE 沿用缓存值: %s: %s", type(e).__name__, e)
    if tx_pairs:
        try:
            result.update(TxAPI.fetch_pe_ttm(tx_pairs))
        except Exception as e:                      # noqa: BLE001
            log.error("[行情统计] PE-TTM 的港股源(腾讯) 取数失败，"
                      "本次港股 PE 沿用缓存值: %s: %s", type(e).__name__, e)
    return result


def fetch_pe_ttm_live(market, code):
    """单只 PE-TTM **实时**取数，返回 {mkt+code: float}（该票无值时返回 {}）。

    K 线页面「打开这个标的就取一次」的入口（不经任何落盘缓存），调用方为
    AppData 的 PE-TTM 实时层。

    market: "sh" / "sz" / "bj" / "hk"；code: 6 位（A股）或 5 位（港股）。
    分流规则与 fetch_pe_ttm 共用 _split_by_source（单源，不另立副本）；
    与它的唯一差异是**异常语义**：此处不吞异常，直接上抛给调用方，
    使其能区分「取数失败（保留旧值）」与「该票确实无 PE（显示为空）」。
    """
    eltdx_pairs, tx_pairs = _split_by_source([(market, code)])
    if eltdx_pairs:
        return ElTdxAPI.fetch_pe_ttm(eltdx_pairs)
    if tx_pairs:
        return TxAPI.fetch_pe_ttm(tx_pairs)
    return {}


def fetch_float_mc(stock_list):
    """批量获取流通市值，返回 {code: 流通市值(亿元)}。

    stock_list: list[{"code": "600519", "prefix": "1"}, ...]。
    该口径**仅覆盖 A 股**（prefix 0/1/2）：eltdx 是唯一数据源，prefix 为
    hk/us 的条目被跳过——与改造前腾讯路径的行为一致（腾讯分支同样只认
    0/1/2，港股/美股本就没有流通市值）。
    """
    if not stock_list:
        return {}
    return ElTdxAPI.fetch_float_mc(stock_list)
