# -*- coding: utf-8 -*-
"""
DataAPI/TxAPI.py —— 腾讯财经（股票行情）数据源适配器
=========================================================================
收口「腾讯股票行情接口」（qt.gtimg.cn/q=）的全部数据获取与字段解析：
  - PE-TTM（市盈率-动态，字段 [39]）
  - 流通市值（单位：亿元，字段 [44]）

调用方（AppRefresh / AppScan）只做「拼参 → 调 TxAPI → 缓存 / 落盘」，
不直连腾讯接口、不解析字段。单向依赖：App → TxAPI。
与 ElTdxAPI / AkshareAPI 同为 P1-1 数据源抽象单轨化的一个收口点。
"""
import logging

log = logging.getLogger(__name__)

# 腾讯行情接口地址（支持一次批量查询多只，逗号分隔）
_TENCENT_BASE = "https://qt.gtimg.cn/q="

# 单批最大股票数（腾讯接口约 200~300 只为限）
_BATCH = 300


def _http_get(q_codes, timeout):
    """对一批腾讯代码发起请求，返回响应文本（仅供本模块内部调用）。"""
    import requests as req
    resp = req.get(_TENCENT_BASE + ",".join(q_codes), timeout=timeout)
    return resp.text


def _iter_records(text):
    """解析腾讯返回的多行 v_xxx 记录，逐条产出 (行前缀市场, fields列表)。

    形如 v_sh600519="1~贵州茅台~600519~...~[39]市盈率~...";
    行前缀 v_sh / v_sz / v_hk → 市场 sh / sz / hk。解析失败的记录被跳过。
    """
    for line in text.strip().split("\n"):
        if "v_" not in line:
            continue
        try:
            body = line.split('="')[1].strip().strip('";')
            fields = body.split("~")
            line_mkt = line[2:4] if len(line) > 4 else ""
            yield line_mkt, fields
        except (ValueError, TypeError, IndexError):
            continue


def fetch_pe_ttm(mkt_codes, batch_size=_BATCH, timeout=10):
    """腾讯行情接口批量获取 PE-TTM（市盈率-动态，字段 [39]）。

    mkt_codes: list[(mkt, code)]，mkt ∈ {sh, sz, bj, hk}。
    返回 {mkt+code: float PE-TTM}；网络 / 解析失败自动跳过该条，空数据返回 {}。
    不带内置超时策略之外的逻辑：调用方自行决定是否再包一层线程池限时。
    """
    if not mkt_codes:
        return {}
    result = {}
    for i in range(0, len(mkt_codes), batch_size):
        batch_q = [f"{m}{c}" for m, c in mkt_codes[i:i + batch_size]]
        try:
            text = _http_get(batch_q, timeout)
            for line_mkt, fields in _iter_records(text):
                if len(fields) <= 39:
                    continue
                stock_code = fields[2]
                pe_str = fields[39]
                # 市盈率(动态)：仅接受纯数字（可含小数点/负号），且非 0
                if (stock_code and stock_code.isdigit() and pe_str
                        and pe_str.replace(".", "").replace("-", "").isdigit()):
                    pe_val = float(pe_str)
                    if pe_val != 0:
                        result[(line_mkt or "") + stock_code] = pe_val
        except Exception as e:
            log.info(f"[PE-TTM] 腾讯接口第{i // batch_size + 1}批失败: {e}")
    return result


def fetch_float_mc(stock_list, batch_size=_BATCH, timeout=5):
    """腾讯行情接口批量获取流通市值（单位：亿元，字段 [44]）。

    stock_list: list[{"code": "600519", "prefix": "1"}, ...]，prefix 0→sz、1→sh、2→bj。
    返回 {code: 流通市值(亿元)}；网络 / 解析失败自动跳过该条，空数据返回 {}。
    """
    if not stock_list:
        return {}
    _PFX = {"0": "sz", "1": "sh", "2": "bj"}
    mkt_codes = []
    for stk in stock_list:
        code = stk.get("code", "")
        mkt = _PFX.get(stk.get("prefix", ""), "")
        if mkt and code:
            mkt_codes.append((mkt, code))
    if not mkt_codes:
        return {}
    result = {}
    for i in range(0, len(mkt_codes), batch_size):
        batch_q = [f"{m}{c}" for m, c in mkt_codes[i:i + batch_size]]
        try:
            text = _http_get(batch_q, timeout)
            for _line_mkt, fields in _iter_records(text):
                if len(fields) <= 44:
                    continue
                stock_code = fields[2]
                nmc = fields[44]  # 流通市值(亿元，腾讯接口直接返回亿元)
                if stock_code and nmc:
                    try:
                        result[stock_code] = float(nmc)
                    except (ValueError, TypeError):
                        pass
        except Exception as e:
            log.info(f"[流通市值] 腾讯接口第{i // batch_size + 1}批失败: {type(e).__name__}: {e}")
    return result


def fetch_hk_names(hk_codes, batch_size=50, timeout=15):
    """腾讯行情接口批量获取港股股票名称（字段 [1]，新浪港股接口已失效）。

    hk_codes: list[str] 纯港股代码（如 ["00700", "09988"]）。
    返回 {("hk"+代码): 名称}；网络 / 解析失败自动跳过该条，空数据返回 {}。
    腾讯港股返回为 GBK 编码，须显式解码，不能复用 utf-8 的 _http_get。
    """
    if not hk_codes:
        return {}
    import requests as req
    result = {}
    for i in range(0, len(hk_codes), batch_size):
        batch = hk_codes[i:i + batch_size]
        batch_num = i // batch_size + 1
        try:
            url = _TENCENT_BASE + ",".join(f"hk{c}" for c in batch)
            resp = req.get(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.qq.com/",
            }, timeout=timeout)
            content = resp.content.decode("gbk", errors="ignore")
            # 格式：v_hk00700="1~腾讯控股~00700~...";
            for line in content.strip().split(";"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                var_part, val_part = line.split("=", 1)
                val_part = val_part.strip().strip('"').strip(";")
                if not val_part:
                    continue
                bare_code = var_part.strip().replace("v_", "").replace("hk", "").strip()
                fields = val_part.split("~")
                if len(fields) >= 2:
                    name = fields[1].strip()  # 股票名称在第 2 个字段
                    if name:
                        result["hk" + bare_code] = name
        except Exception as e:
            log.info(f"[股名刷新] 腾讯港股批次{batch_num}失败: {e}")
    return result