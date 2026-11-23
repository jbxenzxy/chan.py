# -*- coding: utf-8 -*-
"""期货区间套「显式下窗周期优先」—— 运行时行为断言（补源码文本断言的盲区）
=====================================================================
为什么单独一条：`Test/test_15m_period.py :: test_futures_dual_pairs_mirror` 里
三条接线断言（`dual_sub_freq=sub_freq` / `chan._futures_dual_sub_freq = dual_sub_freq`
/ `getattr(...)`）都是**读源码文本**——能防删行，不能证明运行时真的拿显式周期去取
子级别缓存。若有人改成「读到了但没用」（赋给局部变量、仍走静态表），文本断言照样
绿，而手工配 30m+15m 会静默按 5m 判背驰（红框画 15m、背驰拿 5m）。

做法：把 `app_data.futures_sub_chan_guarded_by_key` 换成记录器（`yield None` ⇒ 走
「下窗暂无缓存 → 按子级别背驰处理」早退分支），构造假 parent（上窗级别 / 市场 /
code），调 `CMyBSPointList.check_nesting_divergence`，直接观测它实际用的
`cache_key`——key 的尾巴就是判背驰用的级别。

判别力最强的一组：上窗 15m + 显式 1m。静态表 `_FUTURES_SUB_FREQ_MAP['15m']` 是
5m，只有真读了显式属性才会拿到 1m；反过来「上窗 30m 无显式」必须拿到 5m，证明
没有把显式值写死。

运行： python Test/test_futures_nesting_subfreq.py
"""
import contextlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from App import AppData  # noqa: E402
from BuySellPoint.BSPointList import (  # noqa: E402
    CMyBSPointList,
    _FUTURES_SUB_FREQ_MAP,
)
from Common.CEnum import KL_TYPE  # noqa: E402

app_data = AppData.app_data
_ORIG_GUARD = app_data.futures_sub_chan_guarded_by_key
_hits = []


@contextlib.contextmanager
def _record_guard(cache_key):
    """替换真实缓存守卫：记录 key 后 yield None（触发「下窗暂无缓存」早退）。"""
    _hits.append(cache_key)
    yield None


class _Obj:
    pass


def _cache_key(kl_type, market_type, code, explicit):
    """跑一次 check_nesting_divergence，返回它实际使用的下窗 cache_key。"""
    parent = _Obj()
    parent.kl_type = kl_type
    parent.market_type = market_type
    parent.code = code
    chan = _Obj()
    if explicit is not None:
        chan._futures_dual_sub_freq = explicit
    parent.chan = chan

    obj = CMyBSPointList.__new__(CMyBSPointList)
    obj.parent = parent
    _hits.clear()
    ret = obj.check_nesting_divergence([], [])
    assert ret is True, f"下窗无缓存时应按子级别背驰放行（True），实为 {ret!r}"
    assert len(_hits) == 1, f"应恰好取一次下窗缓存，实为 {_hits}"
    return _hits[0]


def main():
    # 前提：本测试的判别力依赖「静态表给 5m，显式给别的」。
    # 若将来默认配对改了（如 15m→1m），这里会先红，提示需要重选样本 —— 不是误报。
    assert _FUTURES_SUB_FREQ_MAP["15m"] == "5m", (
        f"静态表 15m 默认已变为 {_FUTURES_SUB_FREQ_MAP['15m']!r}，"
        f"本测试「显式 1m 覆盖静态 5m」的样本失去判别力，请重选")
    assert _FUTURES_SUB_FREQ_MAP["30m"] == "5m", \
        f"静态表 30m 默认已变为 {_FUTURES_SUB_FREQ_MAP['30m']!r}，同上"

    CODE = "KQ.m@SHFE.rb:1800"     # CChan.code 形如 SYMBOL:freq_sec（1800=30m）
    CASES = [
        # (说明, 上窗 KL, 市场, 显式下窗, 期望 cache_key, 该组证明什么)
        ("单窗 / 旧调用：无显式属性", KL_TYPE.K_30M, "futures", None,
         "KQ.M@SHFE.RB:5m", "回落静态表（没有把显式值写死）"),
        ("手工配 30m+15m", KL_TYPE.K_30M, "futures", "15m",
         "KQ.M@SHFE.RB:15m", "显式覆盖静态表的 5m"),
        ("手工配 15m+1m", KL_TYPE.K_15M, "futures", "1m",
         "KQ.M@SHFE.RB:1m", "**判别力最强**：静态表 15m→5m，取到 1m 只能来自显式属性"),
        ("手工配 30m+1m", KL_TYPE.K_30M, "futures", "1m",
         "KQ.M@SHFE.RB:1m", "反向再验一次（15m 那组之外的上窗）"),
    ]

    app_data.futures_sub_chan_guarded_by_key = _record_guard
    try:
        for label, kl, market, explicit, want, why in CASES:
            got = _cache_key(kl, market, CODE, explicit)
            assert got == want, (
                f"{label}：下窗 cache_key 应为 {want}，实为 {got}"
                f"（说明读侧没用显式下窗周期 —— {why}）")
            print(f"  [ok] {label:<22} -> {got}   （{why}）")
    finally:
        app_data.futures_sub_chan_guarded_by_key = _ORIG_GUARD

    # 还原后守卫必须回到原对象（防 monkeypatch 泄漏到同进程内的其它用例）
    assert app_data.futures_sub_chan_guarded_by_key is _ORIG_GUARD, "缓存守卫未还原"
    print("[PASS] 期货区间套下窗取用的是「显式配对周期」，无显式时回落静态表；"
          "红框与背驰同级别（行为断言，非源码文本断言）")


if __name__ == "__main__":
    main()
