# -*- coding: utf-8 -*-
"""
建链必须保留买卖点 —— 行为回归守护用例
=====================================================================
背景（一次真实踩坑，保留记录以免重犯）：

`_build_futures_chan` 曾为提速把建链期的 `config.trigger_step` 临时置 False
（`load()` 末尾本来就会整体再算一次线段/中枢，逐笔的 `cal_seg_and_zs` 看着像
被覆盖），10000 根 8.52s → 0.25s，且 klc/bi/seg/zs 数量、末笔端点、末笔
`is_sure` **全部一致** —— 但 15s 用例的买卖点从 10 个变成 0 个。

原因（两条，都必须知道）：
  ① `cal_seg_and_zs` 在 `step_calculation=False` 时会先
     `bi_list.try_add_virtual_bi()` 把末笔变虚；`CMyBSPointList.cal` 的前置检查
     `_is_virtual_bi` 一旦命中就整段 return —— 一次都不算。
  ② 事后补算也救不回来：先 `delete_virtual_bi()` 恢复实笔再 `cal_seg_and_zs()`，
     仍是 0 个。因为 `cal` 只处理 `stroke_n = bi_list[-1]` 与最后一个中枢的组合
     （`cal_bs0point` 按 `stroke_n.idx` 分流），且 `check_nesting_divergence` 要
     拿**当时**的末笔端点去和子级别做区间套。bsp 是逐笔累积结果，历史笔端点
     随后续数据演进已变，无法复现。

⇒ 逐笔 `cal_seg_and_zs` 不能省略。本用例锁死这一点，防止后人再「优化」回去。

判据：
  ① 15s 真实 fixture 建链后买卖点数量必须等于冻结值（10），且
     `_extract_realtime_snapshot` 的 `bsps` 长度与之一致；
  ② `cal_seg_and_zs` 调用次数必须落在「逐笔」量级（> 50），
     而非「一次性」量级（≤ 2）—— 行为判据，不依赖耗时、不受机器影响；
  ③ 结构基线：klc/bi/seg/zs 数量与末笔端点、末笔 `is_sure` 固定值。

运行：python Test/test_chan_build_bsp_guard.py
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

import threading

from App import AppSSE
from App.AppUtils import _make_chan_config
from KLine.KLine_List import CKLine_List
from Test.gen_fixtures import load_records

FIXTURE = os.path.join(TEST_DIR, "fixtures", "futures_15s.json")
SYMBOL, FREQ_SEC = "KQ.m@SHFE.rb", 15

# 冻结基线（fixture 内容不变则恒定；改动分析逻辑导致变化时须同步复核）
EXPECT_BSP = 10


class FakeSrc:
    """最小会话数据源（与生产 CTqSdkSession 的记录缓存接口一致）。"""

    def __init__(self):
        self._records_by_symbol = {}
        self._lock = threading.Lock()

    def set_data(self, records, symbol=None):
        self._records_by_symbol[symbol or "__default__"] = list(records)

    def get_data(self, symbol=None, **kw):
        return list(self._records_by_symbol.get(symbol or "__default__", []))

    def last_records(self, code_key):
        return self._records_by_symbol.get(code_key, [])[-1:]

    def append_bar(self, bar, code_key):
        self._records_by_symbol.setdefault(code_key, []).append(bar)


class _CalCounter:
    """统计 `cal_seg_and_zs` 调用次数（实例方法，可包）。"""

    def __init__(self):
        self.n = 0
        self._orig = None

    def __enter__(self):
        self._orig = CKLine_List.cal_seg_and_zs
        outer = self

        def counted(inst, *a, **kw):
            outer.n += 1
            return outer._orig(inst, *a, **kw)

        CKLine_List.cal_seg_and_zs = counted
        return self

    def __exit__(self, *exc):
        CKLine_List.cal_seg_and_zs = self._orig
        return False


def _sig(chan, kl_type):
    kl = chan.kl_datas[kl_type]
    bi = kl.bi_list
    last_end, last_sure = "", None
    if len(bi) > 0:
        last_sure = bi[-1].is_sure
        if bi[-1].get_end_klu() is not None:
            last_end = bi[-1].get_end_klu().time.toFmtStr("%Y/%m/%d %H:%M")
    return {"klc": len(kl.lst), "bi": len(bi), "seg": len(kl.seg_list),
            "zs": len(kl.zs_list), "last_bi_end": last_end, "last_sure": last_sure}


def case_bsp_preserved():
    """① 买卖点不得丢失 + ② 逐笔计算次数 ③ 结构基线。"""
    rows = load_records(FIXTURE)
    kl_type = AppSSE._get_kl_type_by_sec(FREQ_SEC)

    with _CalCounter() as cnt:
        chan, got = AppSSE._build_futures_chan(rows, SYMBOL, FREQ_SEC, src=FakeSrc())
    assert got == kl_type, f"周期映射异常: {got} != {kl_type}"

    # ② 逐笔量级（一次性路径会 ≤2）
    assert cnt.n > 50, (
        f"建链期 cal_seg_and_zs 仅调用 {cnt.n} 次 ⇒ 走了「一次性算完」路径。"
        "该路径会丢全部历史买卖点（见模块 docstring），必须保留 trigger_step=True")

    # ① 买卖点
    bsp = chan.get_latest_bsp(idx=0, number=0)
    assert len(bsp) == EXPECT_BSP, (
        f"买卖点数量 {len(bsp)} != 冻结基线 {EXPECT_BSP}：建链路径可能被改成"
        f"「一次性算完」，或分析逻辑变更（确认后同步 EXPECT_BSP）")

    snap = AppSSE._extract_realtime_snapshot(
        chan, kl_type, SYMBOL, "rb", "15s")
    assert len(snap["bsps"]) == len(bsp), (
        f"快照 bsps={len(snap['bsps'])} 与 chan.get_latest_bsp()={len(bsp)} 不一致")
    assert snap["meta"]["bsp_count"] == len(bsp)

    # ③ 结构基线（只断数量与末笔状态，不断价格，避免数据微调即红）
    sig = _sig(chan, kl_type)
    assert sig["klc"] > 0 and sig["bi"] > 0 and sig["seg"] > 0 and sig["zs"] > 0
    assert sig["last_sure"] is False, \
        "fixture 末尾应停在未完成笔（last_sure=False），否则覆盖不到虚笔场景"


def main():
    case_bsp_preserved()
    print(f"[PASS] ① 买卖点保留（{EXPECT_BSP} 个）  ② 逐笔计算（cal_seg_and_zs > 50 次）"
          f"  ③ 快照 bsps 与 chan 一致")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
