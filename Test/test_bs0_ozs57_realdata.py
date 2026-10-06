# -*- coding: utf-8 -*-
"""0 类买点「第 5/7 笔」㈠ 闪电走势判据 —— 真实行情端到端红线。

为什么需要这一条
----------------
`BuySellPoint/BSPointList.py::_cal_bs0point_nth_ozs_57th` 的 ㈠ 段由
「相邻回调笔逐级收窄」（严格链式）改为「各回调笔极值均不越过笔1」（宽松），
但**门禁里 8 个快照样本全部是合成 K 线，一次都没进入过 57th** ——
`snapshot_regression` 全绿**证不了**这次改动。逻辑层判别力护栏见
`Test/test_bs0_ozs57_lightning.py`；本用例补上端到端那一环。

样本是怎么来的（不是手挑的）
----------------------------
对本机通达信 vipdoc 全 A 股日线做了两次全量扫描（同一份代码，只差 ㈠ 段口径；
5224 只 × 2，各 ~290s，零错误）：

  口径          57th 进入次数   57th 产出 T0    T0 合计
  宽松（改后）      102,854          1,190       10,402
  严格（改前）      102,854            465        9,677

即 57th 在真实行情里**必被触及**（覆盖 5,176 / 5,224 只），放宽后 496 只股票
多产出 T0，且「仅严格有」的 T0 = 0（严格 ⊆ 宽松，逐日期全量验证）。

`Test/fixtures_real/sz002190_d.json` 是其中判别力最强的一只：1393 根日线，
57th 进入 30 次，57th 产出 T0 —— 宽松 6 个 / 严格 0 个。

四层断言
--------
  [1] 样本完整性：fixture 的 sha256 与 manifest 一致（冻结样本防手改）
  [2] 端到端可达：该样本上 57th 真被进入（不是「根本走不到」的假覆盖）
  [3] 端到端产出：宽松口径下 57th 产出 T0 == 冻结基线，且日期逐一相符
  [4] 判别力：把 ㈠ 段换成严格口径后，同一个样本上 57th 的 T0 **归零**，
      且其它 0 类点分支（3rd/4th/nzs/68th）产出**逐项不变**
      —— 证明 [3] 不是「怎么改都绿」，也证明改动边界只落在 57th

关于 [4] 的等价性：严格闸前置于 ㈠ 段外层，与「把 ㈠ 段源码换成严格实现」
逐分支等价 —— 严格拒时两者都在同一位置 return；严格收时宽松必收（严格 ⊆ 宽松），
后续 ㈡㈢㈣ 走的是同一段未改动源码。

零网络、零 vipdoc 依赖（数据全部来自冻结 JSON）。
跑法：python Test/test_bs0_ozs57_realdata.py
"""
import collections
import hashlib
import io
import json
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(TEST_DIR)
for _p in (TEST_DIR, ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

FIX_DIR = os.path.join(TEST_DIR, "fixtures_real")
SAMPLE = "sz002190_d.json"

# 冻结基线（来源见 docstring：全 A 股日线两次全量扫描）
# 2026-10-06 4th/nzs/68th 新增「DIF 背驰前置闸」（_is_stroke_divergence(MACD_ALGO.DIF)，
# 要求离开笔DIF峰值 < 进入笔DIF峰值；面积判据保留于后）——4th 原 2 笔 T0（2023/04）
# 过面积但未过 DIF 闸而归零，以下三项随新口径重冻：
BASE_ENTER_57TH = 30
BASE_LOOSE_T0_DATES = ["2025/07/17", "2025/07/18", "2025/07/21", "2025/07/22",
                       "2025/08/18", "2025/08/19"]
BASE_STRICT_T0_DATES = []
BASE_LOOSE_57TH_T0_COUNT = 6
BASE_STRICT_57TH_T0_COUNT = 0
BASE_4TH_T0_COUNT = 0
BASE_T3_COUNT = 3

BRANCHES = (
    ("_cal_bs0point_3rd", "3rd"),
    ("_cal_bs0point_4th", "4th"),
    ("_cal_bs0point_nth_nzs", "nzs"),
    ("_cal_bs0point_nth_ozs_57th", "57th"),
    ("_cal_bs0point_nth_ozs_68th", "68th"),
)

PASS = 0
FAIL = 0


def check(name, got, want):
    global PASS, FAIL
    if got == want:
        PASS += 1
        print("[PASS] %s" % name)
    else:
        FAIL += 1
        print("[FAIL] %s\n        got =%r\n        want=%r" % (name, got, want))


# ─────────────────────────────────────────────────────────────
# 1. 冻结样本完整性
# ─────────────────────────────────────────────────────────────
def load_sample():
    """读 manifest + 校验 sha256 + 读回 records（dt 还原为 datetime）"""
    with io.open(os.path.join(FIX_DIR, "manifest.json"), encoding="utf-8") as f:
        manifest = json.load(f)
    entry = [e for e in manifest["entries"] if e["file"] == SAMPLE]
    assert len(entry) == 1, "manifest 里找不到样本 %s" % SAMPLE
    entry = entry[0]

    p = os.path.join(FIX_DIR, SAMPLE)
    with io.open(p, "rb") as f:
        raw = f.read()
    return entry, hashlib.sha256(raw).hexdigest()


def load_records():
    from Test.gen_fixtures import load_records as _lr
    return _lr(os.path.join(FIX_DIR, SAMPLE))


# ─────────────────────────────────────────────────────────────
# 2. 端到端跑一趟（可切换 ㈠ 段口径）
# ─────────────────────────────────────────────────────────────
def _strict_gate(orig, count_enter):
    """严格口径前置闸：严格链式任一环不成立即原样 return（等价于换掉 ㈠ 段）"""

    def wrapper(self, bi_list, pivot_a, stroke_n):
        count_enter()
        nth_in_pivot = stroke_n.idx - pivot_a.begin_bi.idx + 1
        begin_idx = pivot_a.begin_bi.idx
        odd_bis = [bi_list[begin_idx + i] for i in range(0, nth_in_pivot, 2)]
        for j in range(1, len(odd_bis)):
            cur, prev = odd_bis[j], odd_bis[j - 1]
            if stroke_n.is_down():
                chain_ok = cur._high() <= prev._high()
            else:
                chain_ok = cur._low() >= prev._low()
            if not chain_ok:
                return
        return orig(self, bi_list, pivot_a, stroke_n)

    return wrapper


def run_once(records, strict):
    """跑完整缠论分析，返回 {branch: 进入次数} 与 {(branch, bsp_type): [日期]}"""
    from App.AppUtils import _make_chan_config
    from BuySellPoint import BSPointList as BSP
    from Common.CEnum import AUTYPE, KL_TYPE
    from DataAPI import TdxAPI
    import Chan

    cls = BSP.CMyBSPointList
    enter = collections.Counter()
    produced = collections.defaultdict(list)
    cur = {"branch": None}

    saved = {}

    def _count_enter(label):
        def _f():
            enter[label] += 1
        return _f

    for name, label in BRANCHES:
        orig = getattr(cls, name)
        saved[name] = orig
        if name == "_cal_bs0point_nth_ozs_57th" and strict:
            setattr(cls, name, _strict_gate(orig, _count_enter(label)))
            continue

        def make(orig=orig, label=label):
            def wrapper(self, bi_list, pivot_a, stroke_n):
                enter[label] += 1
                prev = cur["branch"]
                cur["branch"] = label
                try:
                    return orig(self, bi_list, pivot_a, stroke_n)
                finally:
                    cur["branch"] = prev
            return wrapper
        setattr(cls, name, make())

    orig_add = cls.add_bs

    def add_bs(self, bs_type, bi, relate_bsp11=None, is_target_bsp=True, feature_dict=None):
        produced[(cur["branch"] or "other", str(bs_type))].append(str(bi.get_end_klu().time))
        return orig_add(self, bs_type, bi, relate_bsp11, is_target_bsp, feature_dict)

    cls.add_bs = add_bs

    try:
        cfg = _make_chan_config()
        cfg.kl_data_check = False
        with TdxAPI.tdx_data_context(records):
            chan = Chan.CChan(
                code="sz002190", begin_time=None, end_time=None,
                data_src="custom:TdxAPI.CTdxAPI", lv_list=[KL_TYPE.K_DAY],
                config=cfg, autype=AUTYPE.NONE, market_type="stock",
            )
            for _ in chan.step_load():
                pass
        # 0 类点落点：与全量扫描同一取法（CBS_Point.klu.time）
        kl = chan[0]
        t0_dates = sorted(str(bsp.klu.time) for bsp in kl.bs_point_lst.bsp_iter()
                          if any(getattr(t, "value", str(t)) == "0" for t in bsp.type))
        return dict(enter), {k: sorted(v) for k, v in produced.items()}, t0_dates
    finally:
        for name, orig in saved.items():
            setattr(cls, name, orig)
        cls.add_bs = orig_add


def _by_branch(produced, branch):
    """该分支产出的 T0 日期（T0 == BSP_TYPE.T0，str() 形如 'BSP_TYPE.T0'）"""
    return produced.get((branch, "BSP_TYPE.T0"), [])


def main():
    entry, sha = load_sample()

    print("=== [1] 冻结样本完整性 ===")
    check("manifest 记录的 sha256 与文件一致", sha, entry["sha256"])
    check("样本根数", entry["bars"], 1393)

    records = load_records()
    check("读回 records 根数", len(records), 1393)

    print("\n=== [2]+[3] 宽松口径（当前源码）===")
    ent_loose, prod_loose, t0_loose = run_once(records, strict=False)
    check("57th 被进入次数（端到端可达，非假覆盖）", ent_loose.get("57th"), BASE_ENTER_57TH)
    check("0 类点落点日期 == 冻结基线（宽松）", t0_loose, BASE_LOOSE_T0_DATES)
    check("其中 57th 分支贡献的 T0 数",
          len(_by_branch(prod_loose, "57th")), BASE_LOOSE_57TH_T0_COUNT)

    print("\n=== [4] 判别力（同一样本换成严格口径）===")
    ent_strict, prod_strict, t0_strict = run_once(records, strict=True)
    check("0 类点落点日期 == 冻结基线（严格）", t0_strict, BASE_STRICT_T0_DATES)
    check("严格口径下 57th 产出 T0 数归零",
          len(_by_branch(prod_strict, "57th")), BASE_STRICT_57TH_T0_COUNT)
    check("进入次数与口径无关（闸门在 ㈠ 段内，不改分支可达性）",
          ent_strict.get("57th"), ent_loose.get("57th"))
    check("其它 0 类点分支产出逐项不变（改动边界只落在 57th）",
          {b: len(_by_branch(prod_strict, b)) for b in ("3rd", "4th", "nzs", "68th")},
          {b: len(_by_branch(prod_loose, b)) for b in ("3rd", "4th", "nzs", "68th")})
    check("4th 分支 T0 数 == 冻结基线", len(_by_branch(prod_loose, "4th")), BASE_4TH_T0_COUNT)
    check("T3 产出数 == 冻结基线",
          len(prod_loose.get(("other", "BSP_TYPE.T3"), [])), BASE_T3_COUNT)

    print("\n===== 0类点 57th 真实行情端到端: %d passed / %d failed =====" % (PASS, FAIL))
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
