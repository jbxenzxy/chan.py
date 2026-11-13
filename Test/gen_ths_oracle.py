# -*- coding: utf-8 -*-
"""用真实自选股当 oracle，自检 `get_market_id` 的段表是否漏了/错了代码段。

为什么需要它
------------
`get_market_id` 的市场号只能从**代码段**推断，而代码段是交易所的外部事实：
北交所 920 段、同花顺 884 二级板块、深市 159/161 基金……都不是在代码里推导出来
的。旧做法是「等线上报了『传入股票代码有误』再去实测」，代价是一次真实的写入
失败。

`getSelfStockWithMarket` 返回的 `marketid` 是**服务端真值**，等于一个免费的
oracle：只要某段代码进过一次自选股，就能拿它的真实市场号反查本地表对不对。
本脚本把这个对照做成一条命令。

用法
----
  python Test/gen_ths_oracle.py            # 只读对照，打印差异；有差异退出码 1
  python Test/gen_ths_oracle.py -v         # additionally 打印每条 (code, marketid)

只读：只调 `getSelfStockWithMarket`，**不 add / 不 delete / 不改任何分组**。

不进 `Test/run_all.py` 门禁：它需要联网 + 有效 Cookie，而门禁必须离线可复现。
门禁里用 `Test/test_ths_code_normalize.py` ⑥ 组的**段级冻结表**，本脚本负责
在那张表过期时把它刷新出来。
"""
import os
import sys
from collections import defaultdict

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

# 同花顺对「退市/未识别」不给正常市场号，这两个占位值不参与对照
_PLACEHOLDER = {"URSI", "UNSI"}


def _group(pairs):
    """按 3 位 → 2 位前缀聚合，返回 {前缀: {marketid: [codes]}}。"""
    out = {}
    for length in (3, 2):
        buckets = defaultdict(lambda: defaultdict(list))
        for code, mid in pairs:
            if not code.isdigit() or mid in _PLACEHOLDER or len(code) < length:
                continue
            buckets[code[:length]][mid].append(code)
        out[length] = buckets
    return out


def main():
    verbose = "-v" in sys.argv
    from DataAPI.ThsCloudZxgAPI import get_market_id
    from DataAPI.ThsCookieSource import resolve_cookie, ThsCookieUnavailable

    try:
        cookie, source, _ = resolve_cookie(validate=False)
    except ThsCookieUnavailable as e:
        print(f"[oracle] 拿不到 Cookie，无法对照：{e}")
        print("[oracle] 这不是失败——设 THS_COOKIE 或登录一次浏览器后重跑。")
        return 2
    if not cookie:
        print("[oracle] 无 Cookie，跳过（设 THS_COOKIE 后重跑）")
        return 2

    from DataAPI.ThsCloudZxgAPI import CThsCloudZxg
    api = CThsCloudZxg(cookie=cookie)
    if not api.check_login():
        print("[oracle] Cookie 无效/已过期，无法对照")
        return 2

    stocks = api.get_self_stocks()
    if not stocks:
        print("[oracle] 自选股为空，oracle 无判别力")
        return 2
    pairs = sorted({(str(s.get("code", "")), str(s.get("marketid", ""))) for s in stocks})
    print(f"[oracle] Cookie 来源 {source}｜自选 {len(pairs)} 条不重复代码\n")

    bad, unknown = [], []
    groups = _group(pairs)
    for length in (3, 2):
        for pre in sorted(groups[length]):
            mids = groups[length][pre]
            if len(mids) > 1:
                # 同前缀多市场：交给更长前缀或个股级判定，这里只提示
                detail = "、".join(f"{m}×{len(c)}({c[0]})" for m, c in mids.items())
                print(f"  [混合] {pre}{'*' * (6 - length)}: {detail}")
                continue
            mid, codes = next(iter(mids.items()))
            sample = codes[0]
            got = get_market_id(sample)
            if got != mid:
                bad.append((length, pre, sample, mid, got, len(codes)))

    if verbose:
        print()
        for code, mid in pairs:
            got = get_market_id(code)
            flag = "  " if got == mid else "✗ "
            print(f"  {flag}{code:<8} 服务端={mid:<5} 本地={got}")

    if bad:
        print(f"\n[oracle] ✗ {len(bad)} 个段与本地表不符：")
        for length, pre, sample, mid, got, n in bad:
            print(f"  {pre}{'*' * (6 - length):<4} 例 {sample:<8} "
                  f"服务端={mid:<5} 本地={got:<5}（该段 {n} 只）")
        print("\n处置：修 `DataAPI/ThsCloudZxgAPI.py` 的 `_SEG_*` 表，再把新段补进"
              " `Test/test_ths_code_normalize.py` 的 `SEG_ORACLE_3/2`。")
        return 1

    print(f"[oracle] ✓ {len(groups[3]) + len(groups[2])} 个可判定段全部与服务端一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
