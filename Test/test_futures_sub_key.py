# -*- coding: utf-8 -*-
"""
P0-2 回归：期货双窗区间套缓存 key 一致性断言测试
=====================================================================
背景：期货双窗区间套（check_nested_diver）读写下窗缓存的 key 曾不一致——

  写侧：AppSSE.sse_futures_stream_dual → app_data.set_futures_sub_chan(symbol, sub_freq)
        经 make_futures_sub_key → "SYMBOL:sub_freq"（如 "KQ.M@CFFEX.IM:1m"）
  读侧：BSPointList.check_nested_diver 曾直接以 parent.code（CChan.code，形如
        "SYMBOL:freq_sec"，带周期后缀）拼接得 "SYMBOL:freq_sec:sub_freq"，
        与写侧 key 永不相等 → 区间套 100% 静默失效（恒按子级别背驰处理）。

  修复：读侧改经 make_futures_sub_key_from_code（AppData 工厂）还原纯 symbol
        后生成 key，与写侧同源。本用例断言该等价关系，并做真实读写往返。

覆盖：
  ① 写/读 key 一致：对多组 (symbol, freq_sec, sub_freq)，读侧 key == 写侧 key
  ② 往返：set_futures_sub_chan → get_futures_sub_chan（大小写变体）取回同一对象
  ③ 反向守护：确认「老 bug 的拼接方式」与写侧 key 必然不同（防回退）
  ④ 非法/无冒号 chan_code 不崩溃（rsplit 容错）

运行：python Test/test_futures_sub_key.py
"""
import os
import sys

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

from App.AppData import app_data, make_futures_sub_key, make_futures_sub_key_from_code

# 真实配对：main_freq(KL) → sub_freq（_FUTURES_SUB_FREQ_MAP），freq_sec 为写 CChan.code 的秒数
# （30m=1800s, 5m=300s, 1m=60s, 15s=15s）
PAIRS = [
    ("KQ.m@SHFE.rb", 1800, "5m"),   # 30m → 5m
    ("KQ.m@SHFE.rb", 300, "1m"),    # 5m  → 1m
    ("KQ.M@CFFEX.IM", 60, "15s"),   # 1m  → 15s
    ("KQ.m@DCE.i", 60, "15s"),      # 大小写混合
]


def test_key_equivalence():
    """读侧 key（由 chan_code 还原）== 写侧 key（原始 symbol）"""
    for symbol, freq_sec, sub_freq in PAIRS:
        chan_code = f"{symbol}:{freq_sec}"       # CChan.code（_build_futures_chan 生成）
        read_key = make_futures_sub_key_from_code(chan_code, sub_freq)
        write_key = make_futures_sub_key(symbol, sub_freq)
        assert read_key == write_key, \
            f"读侧 key {read_key!r} != 写侧 key {write_key!r} ({symbol}:{freq_sec}->{sub_freq})"
        print(f"[OK] 写/读 key 一致: {write_key}")


def test_roundtrip_via_semantic_api():
    """set → get（大小写变体 symbol）往返取回同一对象"""
    symbol, freq_sec, sub_freq = PAIRS[2]   # KQ.M@CFFEX.IM:60 -> 15s
    dummy = object()
    app_data.set_futures_sub_chan(symbol, sub_freq, dummy)
    try:
        # 读侧以 chan_code（原符号大小写）经语义接口读取
        got = app_data.get_futures_sub_chan(symbol, sub_freq)
        assert got is dummy, "写读往返失败（同符号）"
        # 大小写变体也应命中（make_futures_sub_key 大写入键）
        got2 = app_data.get_futures_sub_chan("kq.m@cffex.im", sub_freq)
        assert got2 is dummy, "写读往返失败（小写变体）"
        print("[OK] 语义接口写读往返 + 大小写不敏感命中")
    finally:
        app_data.pop_futures_sub_chan(symbol, sub_freq)


def test_old_bug_pattern_never_matches():
    """反向守护：老 bug 的拼接（chan_code 直接带后缀）必须 != 写侧 key"""
    symbol, freq_sec, sub_freq = PAIRS[0]
    chan_code = f"{symbol}:{freq_sec}"
    old_key = f"{chan_code.upper()}:{sub_freq}"   # 老实现
    write_key = make_futures_sub_key(symbol, sub_freq)
    assert old_key != write_key, \
        "老 bug 拼接方式竟与写侧 key 相同（修复可能失效）"
    print(f"[OK] 老拼接 {old_key!r} != 写侧 {write_key!r}（修复生效）")


def test_no_colon_tolerance():
    """无冒号 chan_code（理论不发生）不崩溃，退回整串作为 symbol"""
    key = make_futures_sub_key_from_code("KQ.m@SHFE.rb", "5m")
    assert key == "KQ.M@SHFE.RB:5m", f"无冒号容错异常: {key!r}"
    print(f"[OK] 无冒号容错: {key}")


if __name__ == "__main__":
    test_key_equivalence()
    test_roundtrip_via_semantic_api()
    test_old_bug_pattern_never_matches()
    test_no_colon_tolerance()
    print("\nP0-2 断言测试全部通过 ✅")
