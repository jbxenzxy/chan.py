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

log = logging.getLogger(__name__)

# 新浪行情接口地址（支持一次批量查询多只，逗号分隔）
_SINA_BASE = "http://hq.sinajs.cn/list="


def fetch_a_names(mkt_code_pairs, batch_size=50, timeout=15):
    """新浪财经行情接口批量获取 A股股票名称（字段 [0]）。

    mkt_code_pairs: list[(bare_code, market)]，market ∈ {sh, sz}；
    新浪仅 sh/sz 有 hq_str_ 记录，bj 无对应记录时自然跳过。
    返回 {(market+code): 名称}；网络 / 解析失败自动跳过该条，空数据返回 {}。
    返回为 GBK 编码，须显式解码。与 TxAPI.fetch_hk_names 的返回形态一致（market+code）。
    """
    if not mkt_code_pairs:
        return {}
    import urllib.request
    result = {}
    for i in range(0, len(mkt_code_pairs), batch_size):
        batch = mkt_code_pairs[i:i + batch_size]
        batch_num = i // batch_size + 1
        try:
            codes_str = ",".join(f"{m}{c}" for m, c in batch)
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
                        break
        except Exception as e:
            log.info(f"[股名刷新] 新浪A股批次{batch_num}失败: {e}")
    return result