# -*- coding: utf-8 -*-
"""
DataAPI/SinaAPI.py —— 新浪财经数据源适配器
=========================================================================
收口「新浪财经行情接口」（hq.sinajs.cn/list=）的数据获取与字段解析：
  - A股股票名称（字段 [0]）

调用方（AppRefresh）只做「拼参 → 调 SinaAPI → 缓存」，
不直连新浪接口、不解析字段。单向依赖：App → SinaAPI。
与 ElTdxAPI / AkshareAPI / TxAPI 同为 P1-1 数据源抽象单轨化的一个收口点。
"""
import logging
import time

log = logging.getLogger(__name__)

# 新浪行情接口地址（支持一次批量查询多只，逗号分隔）
_SINA_BASE = "http://hq.sinajs.cn/list="

# 新浪行情接口的市场前缀（bj 无 hq_str_ 记录，请求后被自然跳过）
_SINA_MARKETS = ("sh", "sz", "bj")


def _normalize_sina_pair(pair):
    """把调用方传入的 (bare_code, market) 归一到确定顺序，容忍两种写法。

    契约：mkt_code_pairs 的每一项都是 (bare_code, market)，如 ("600519", "sh")。
    **但顺序写反的代价极大**：本模块历史上把顺序解反，拼出 `600519sh` 这种非法
    list= 参数，而新浪对非法代码**返回 HTTP 200 + `hq_str_sys_auth="FAILED"`**
    —— 既不抛异常、也没有任何日志，5000+ 只 A 股名称被静默全部丢弃，最终表现为
    「刷新后股票名变成代码、拼音搜不到」。故此处显式归一，两种顺序都能正确工作。
    """
    a, b = str(pair[0]).strip().lower(), str(pair[1]).strip().lower()
    if a in _SINA_MARKETS and b not in _SINA_MARKETS:
        return b, a                      # 调用方写成了 (market, code)
    return a, b


def fetch_a_names(mkt_code_pairs, batch_size=50, timeout=15, interval=0.3):
    """新浪财经行情接口批量获取 A股股票名称（字段 [0]）。

    mkt_code_pairs: list[(bare_code, market)]，market ∈ {sh, sz}；
    新浪仅 sh/sz 有 hq_str_ 记录，bj 无对应记录时自然跳过。
    interval: 相邻批次间的间隔秒数（新浪对高频访问有限流，0 表示不节流）。
    返回 {(market+code): 名称}；网络 / 解析失败自动跳过该条，空数据返回 {}。
    返回为 GBK 编码，须显式解码。与 TxAPI.fetch_hk_names 的返回形态一致（market+code）。
    """
    if not mkt_code_pairs:
        return {}
    import urllib.request
    result = {}
    total = len(mkt_code_pairs)
    batch_count = 0
    empty_batches = 0
    for i in range(0, total, batch_size):
        batch = [_normalize_sina_pair(p) for p in mkt_code_pairs[i:i + batch_size]]
        batch_num = i // batch_size + 1
        batch_count += 1
        parsed_here = 0
        try:
            codes_str = ",".join(f"{market}{code}" for code, market in batch)
            url = _SINA_BASE + codes_str
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://finance.sina.com.cn/",
            })
            resp = urllib.request.urlopen(req, timeout=timeout)
            content = resp.read().decode("gbk", errors="ignore")
            for line in content.strip().split("\n"):
                line = line.strip()
                if not line or "=" not in line:
                    continue
                var_part, val_part = line.split("=", 1)
                val_part = val_part.strip().strip('"').strip(";").strip('"')
                if not val_part:
                    continue
                var_name = var_part.strip().replace("var ", "")
                # 形如 hq_str_sh600519="贵州茅台,..."
                for mkt_prefix in ("sh", "sz"):
                    marker = f"hq_str_{mkt_prefix}"
                    if var_name.startswith(marker):
                        bare_code = var_name[len(marker):]
                        fields = val_part.split(",")
                        if fields:
                            name = fields[0].strip()  # 股票名称在第 1 个字段
                            if name:
                                result[mkt_prefix + bare_code] = name
                                parsed_here += 1
                        break
        except Exception as e:
            log.info(f"[股名刷新] 新浪A股批次{batch_num}失败: {e}")
        if parsed_here == 0:
            empty_batches += 1
        if interval and i + batch_size < total:
            time.sleep(interval)
    if batch_count and empty_batches == batch_count:
        log.warning(
            f"[股名刷新] 新浪A股 {batch_count} 批全部无有效返回（共请求 {total} 只）"
            f" —— 疑似拼参/接口异常：正确形式为 sh600519，非法代码会被新浪以 "
            f'HTTP 200 + hq_str_sys_auth="FAILED" 静默拒绝，请核查 SinaAPI 拼参顺序')
    return result