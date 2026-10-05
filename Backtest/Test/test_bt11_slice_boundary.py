# -*- coding: utf-8 -*-
"""`[L, R]` 区间裁切的**粒度**护栏（v1.20）。

为什么要有这个组件
---------------------------------------------------------------------
`Backtest/Runner.py::_slice_records` 是 `[L, R]` 参数的**唯一**使用点（设计文档
§4.4 / P0-2 方案 B），它的契约就写在自己那句 docstring 里：「给我什么区间就测
什么区间」。旧实现把端点与记录 `dt` 一律 `[:10]` 截到**日**再比较 ⇒ 端点的时间
部分被**静默丢弃**：

    --to 2026-09-29 10:00:00   ≡   --to 2026-09-29
    （5m 切片实测同为 11568 根 —— 当日 15:00 那根照样进样本）

而 `RunResult.target_dt` 原样保存用户传入值、`Report.caliber_lines`
（`Backtest/Report.py` 的 `lo / hi` 两行）照它印口径行 ⇒ **报告自述的区间 ≠
真实样本**，且全过程无告警。`run()` 的签名是 `start_dt: Optional[Any]`、CLI 也不
校验格式 —— 传错就静默多跑半天样本，属典型的"输入静默失效"。

页面路径不受影响：`App/AppBacktest.py` 传的是 `records[0]["dt"] / records[-1]["dt"]`
（首尾根的完整时间戳），与样本同粒度 ⇒ 裁切是恒等变换。所以它不是 P0，但同一族
的静默失效必须堵掉 —— 且改法对既有路径**逐条等价**（见 ③）。

本组件钉四件事
---------------------------------------------------------------------
  ① **端点粒度 = 传入粒度**：只给日期 ⇒ 含整天（L 补 00:00:00 / R 补 23:59:59）；
     给时刻 ⇒ 精确到该时刻（含端点）。左右两端各验、日线与分钟线各验。
  ② **判别力**：两种粒度跑出的样本**必须不等** —— 相等就说明护栏抓不到回潮。
  ③ **恒等性**：页面路径（首尾 `dt` 作端点）裁切后与输入**逐条相等**；日线样本
     （`dt` 只到日）在整天端点下也不多不少；两端 `None` / 空序列原样返回。
  ④ **回潮护栏 + 变异自证**：`Backtest/Runner.py` 全文不得再出现 `_norm_day`
     / 端点 `[:10]` 截断；把规范函数临时换回"按日截断"的旧写法，判据必须**转红**
     且**含「裁切」类失败**（证明真拦到样本，不只是红在纯函数上）。

用法：python Backtest/Test/test_bt11_slice_boundary.py（退出码 0 = 通过）
"""
import inspect
import io
import os
import sys
from datetime import datetime

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import Backtest.Runner as R                                     # noqa: E402

_passed = 0
_failed = 0


def check(name, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print("  \u2713 %s" % name)
    else:
        _failed += 1
        print("  \u2717 %s -> got %r, want %r" % (name, got, want))


# ── 样本：分钟线（前一日 3 根 + 当日 4 根）与日线（4 天）各一份 ────────────
MIN_RECS = [
    {"dt": "2026-09-28 09:35:00"}, {"dt": "2026-09-28 10:00:00"},
    {"dt": "2026-09-28 15:00:00"},
    {"dt": "2026-09-29 09:35:00"}, {"dt": "2026-09-29 10:00:00"},
    {"dt": "2026-09-29 10:30:00"}, {"dt": "2026-09-29 15:00:00"},
]
DAY_RECS = [{"dt": d} for d in ("2026-09-27", "2026-09-28",
                                "2026-09-29", "2026-09-30")]
DTS = [r["dt"] for r in MIN_RECS]


def _battery():
    """跑完整判据，返回失败清单（空 = 全过）。① 与 ④-b 共用这一份。"""
    bad = []

    def eq(tag, name, got, want):
        if got != want:
            bad.append("%s %s: got %r, want %r" % (tag, name, got, want))

    # ①-a 规范函数：缺时刻补 00:00:00；斜杠 / 无秒 / datetime 都收
    eq("纯函数", "_canon_dt(只给日期)", R._canon_dt("2026-09-29"),
       "2026-09-29 00:00:00")
    eq("纯函数", "_canon_dt(斜杠且无秒)", R._canon_dt("2026/09/29 10:00"),
       "2026-09-29 10:00:00")
    eq("纯函数", "_canon_dt(datetime)",
       R._canon_dt(datetime(2026, 9, 29, 10, 0, 5)), "2026-09-29 10:00:05")

    # ①-b 端点：只给日期 ⇒ 整天；给时刻 ⇒ 原样（不被放宽）
    eq("端点", "R(只给日期) 补到当日末",
       R._boundary_key("2026-09-29", upper=True), "2026-09-29 23:59:59")
    eq("端点", "L(只给日期) 补到当日首",
       R._boundary_key("2026-09-29", upper=False), "2026-09-29 00:00:00")
    eq("端点", "R(带时刻) 不被放宽",
       R._boundary_key("2026-09-29 10:00:00", upper=True),
       "2026-09-29 10:00:00")
    eq("端点", "L(斜杠日期)",
       R._boundary_key("2026/09/29", upper=False), "2026-09-29 00:00:00")

    # ①-c 裁切：右端点
    eq("裁切", "R=日期 → 含当日全部",
       [r["dt"] for r in R._slice_records(MIN_RECS, None, "2026-09-29")], DTS)
    eq("裁切", "R=前一日 → 剔掉当日",
       [r["dt"] for r in R._slice_records(MIN_RECS, None, "2026-09-28")],
       DTS[:3])
    eq("裁切", "R=10:00 → 截止 10:00（含端点）",
       [r["dt"] for r in R._slice_records(MIN_RECS, None,
                                          "2026-09-29 10:00:00")], DTS[:5])

    # ①-d 裁切：左端点
    eq("裁切", "L=日期 → 含当日全部",
       [r["dt"] for r in R._slice_records(MIN_RECS, "2026-09-29", None)],
       DTS[3:])
    eq("裁切", "L=10:30 → 剔掉当日 10:30 之前",
       [r["dt"] for r in R._slice_records(MIN_RECS, "2026-09-29 10:30:00",
                                          None)], DTS[5:])

    # ①-e 日线样本：整天端点下不多不少（`dt` 只到日，补齐后仍落在整天里）
    eq("裁切", "日线 [2026-09-28, 2026-09-29]",
       [r["dt"] for r in R._slice_records(DAY_RECS, "2026-09-28",
                                          "2026-09-29")],
       ["2026-09-28", "2026-09-29"])

    # ② 判别力：两种粒度必须跑出不同样本（相等 = 护栏无拦截力）
    n_day = len(R._slice_records(MIN_RECS, None, "2026-09-29"))
    n_min = len(R._slice_records(MIN_RECS, None, "2026-09-29 10:00:00"))
    eq("判别力", "日粒度样本 ≠ 时刻粒度样本", n_day != n_min, True)

    # ③ 恒等性：页面路径（首尾 dt 作端点）与两类退化输入
    for tag, recs in (("分钟线", MIN_RECS), ("日线", DAY_RECS)):
        eq("恒等", "%s 页面路径裁切 == 原样" % tag,
           R._slice_records(recs, recs[0]["dt"], recs[-1]["dt"]), list(recs))
    eq("恒等", "两端 None → 原样", R._slice_records(MIN_RECS, None, None),
       MIN_RECS)
    eq("恒等", "空序列 → 空",
       R._slice_records([], "2026-09-01", "2026-09-30"), [])
    return bad


def main():
    print("区间裁切粒度护栏（Backtest/Runner.py::_slice_records）")

    bad = _battery()
    check("①–③ 端点粒度 / 判别力 / 恒等性（全项）", bad, [])

    # ④-a 回潮护栏：全文不得再有"按日截断"的旧实现
    src = io.open(os.path.join(ROOT, "Backtest", "Runner.py"),
                  encoding="utf-8").read()
    sfn = inspect.getsource(R._slice_records)
    check("④-a Runner.py 全文无 _norm_day（回潮即红）", "_norm_day" in src, False)
    check("④-a _slice_records 走 _boundary_key / _canon_dt",
          ("_boundary_key(" in sfn) and ("_canon_dt(" in sfn), True)

    # ④-b 变异自证：换回旧写法（两端截到日），判据必须转红
    orig = R._canon_dt

    def _day_trunc(x):
        """旧实现：`dt` 一律截到日 —— 端点的时间部分被丢弃。"""
        return str(x).replace("/", "-").strip()[:10]

    R._canon_dt = _day_trunc
    try:
        bad2 = _battery()
    finally:
        R._canon_dt = orig
    n_slice = len([b for b in bad2 if b.startswith("裁切")])
    check("④-b 变异（按日截断）后判据转红", len(bad2) > 0, True)
    check("④-b 其中「裁切」类失败 ≥1（真拦到样本，不是只红纯函数）",
          n_slice >= 1, True)

    print("\n通过 %d，失败 %d" % (_passed, _failed))
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
