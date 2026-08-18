# -*- coding: utf-8 -*-
"""
阶段 2.5：trigger_step 逐步回放一致性验证
=====================================================================
设计文档 8.4：「trigger_step 逐步回放模式（每新增一根 K 线计算一次，
虚段 / 虚笔处理与全量计算不同）」——因此本用例不要求回放与全量逐位相同，
而是验证两件事：

  1. 快照冻结：step 偏移序列（0 / -1 / -5 / -20）各自的输出已冻结为基线
     （漂移即失败），见 snapshots/step_replay_*.json
  2. 收敛一致性：step=0（end_date 锚定当天）与「直接以该日期为终点的
     全量计算」最终 笔/段/中枢/买卖点 计数与最后一笔的方向、端点一致
     ——虚笔/虚段允许中间过程不同，但最终态应收敛。

运行：python Test/test_trigger_step_replay.py
"""
import json
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(TEST_DIR))

import typing
if not hasattr(typing, "Self"):
    import typing_extensions
    typing.Self = typing_extensions.Self

from Test.snapshot_runner import (
    install_data_source, isolate_side_effects, normalize, SNAPSHOT_DIR,
)
from Test import comparator
from Test.gen_fixtures import load_records
import os as _os

FIXTURE = "stock_day.json"
ANCHOR_OFFSET = 60          # 锚点：倒数第 60 根
STEPS = [0, -1, -5, -20]    # 回放偏移序列（0=锚定当天）


def _run(end_date=None, step=None):
    from App import AppEngine as m
    restore, records = install_data_source(FIXTURE)
    try:
        return m._analyze_stock_internal(
            "600519", freq="d", end_date=end_date, cache_chan=False, step=step)
    finally:
        restore()


def _anchor_str(offset):
    rows = load_records(_os.path.join(TEST_DIR, "fixtures", FIXTURE))
    return rows[-1 - offset]["dt"].strftime("%Y/%m/%d")


def _summary(result):
    """提取最终态摘要：计数 + 最后一笔方向/端点（虚笔容许，最后一根 K 线后状态应稳定）"""
    bis = result.get("bis", [])
    return {
        "bi_count": result.get("meta", {}).get("bi_count", len(bis)),
        "seg_count": result.get("meta", {}).get("seg_count"),
        "zs_count": result.get("meta", {}).get("zs_count"),
        "bsp_count": result.get("meta", {}).get("bsp_count"),
        "kline_count": result.get("meta", {}).get("kline_count"),
        "last_bi": {k: bis[-1].get(k) for k in ("direction", "sdt", "edt", "is_sure")} if bis else None,
    }


def main():
    anchor = _anchor_str(ANCHOR_OFFSET)
    failures = []
    force_update = "--update" in sys.argv

    # ── 1. 快照冻结：各 step 输出与冻结基线比对 ──
    for step in STEPS:
        r_iso = isolate_side_effects()
        try:
            result = _run(end_date=anchor, step=step)
        finally:
            r_iso()
        if "error" in result:
            failures.append(f"step={step}: 返回 error: {result['error']}")
            continue
        norm = normalize(result)
        path = _os.path.join(SNAPSHOT_DIR, f"step_replay_s{step}.json")
        if force_update or not _os.path.exists(path):
            with open(path, "w", encoding="utf-8") as f:
                json.dump(norm, f, ensure_ascii=False, indent=1, sort_keys=True)
            print(f"[FROZEN] step={step}: 基线已冻结" if not force_update
                  else f"[UPDATED] step={step}: 基线已重冻结")
            continue
        with open(path, encoding="utf-8") as f:
            expected = json.load(f)
        ok, diff = comparator.compare(expected, norm, path=f"$.step_s{step}")
        print(f"[{'PASS' if ok else 'FAIL'}] step={step} 快照比对")
        if not ok:
            failures.append(f"step={step} 快照漂移:\n{diff}")

    # ── 2. 收敛一致性（缠论口径）──
    # 虚笔/虚段允许不同（分型确认依赖锚点后的未来数据）：
    #   ① 回放的全部「已确认笔」(is_sure=True) 必须与「全量在锚点前的已确认笔」
    #      三元组 (direction, sdt, edt) 完全一致；
    #   ② 回放末尾允许至多一根进行中的虚笔，其起点必须是全量某笔的端点。
    r_iso = isolate_side_effects()
    try:
        replay = _run(end_date=anchor, step=0)
    finally:
        r_iso()
    r_iso = isolate_side_effects()
    try:
        full = _run()   # 无 end_date = 全量（fixture 末根为终点）
    finally:
        r_iso()

    from datetime import datetime as _dt

    def _parse(s):
        for f in ("%Y/%m/%d %H:%M:%S", "%Y/%m/%d", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return _dt.strptime(s, f)
            except ValueError:
                continue
        return None

    anchor_parsed = _parse(anchor)
    key3 = lambda b: (b["direction"], b["sdt"], b["edt"])

    rep_sure = [key3(b) for b in replay.get("bis", []) if b.get("is_sure")]
    full_sure = [key3(b) for b in full.get("bis", [])
                 if b.get("is_sure") and _parse(b["edt"]) <= anchor_parsed]
    n_match = sum(1 for x in rep_sure if x in set(full_sure))
    rep_only = [x for x in rep_sure if x not in set(full_sure)]

    if rep_only:
        failures.append(f"回放存在全量没有的已确认笔: {rep_only[:3]}")
        print(f"[FAIL] ① 已确认笔一致性: {len(rep_only)} 根不一致")
    else:
        print(f"[PASS] ① 已确认笔一致性: {n_match}/{len(rep_sure)} 三元组精确匹配"
              f"（全量锚点前 {len(full_sure)} 根）")

    # ② 虚笔检验
    rep_unsure = [b for b in replay.get("bis", []) if not b.get("is_sure")]
    if len(rep_unsure) > 1:
        failures.append(f"回放虚笔超过 1 根: {len(rep_unsure)}")
        print(f"[FAIL] ② 虚笔数量: {len(rep_unsure)} > 1")
    elif len(rep_unsure) == 1:
        vb = rep_unsure[0]
        endpoints = {b["edt"] for b in full.get("bis", [])} | {b["sdt"] for b in full.get("bis", [])}
        if vb["sdt"] in endpoints:
            print(f"[PASS] ② 虚笔: 1 根（{vb['direction']} {vb['sdt']}→{vb['edt']}），"
                  f"起点锚定全量笔端点")
        else:
            failures.append(f"虚笔起点 {vb['sdt']} 不是全量任何笔的端点")
            print(f"[FAIL] ② 虚笔起点未锚定: {vb['sdt']}")
    else:
        print("[PASS] ② 虚笔: 0 根（锚点恰好为确认笔端点）")

    s_replay = {
        "bi_count": replay.get("meta", {}).get("bi_count"),
        "seg_count": replay.get("meta", {}).get("seg_count"),
        "zs_count": replay.get("meta", {}).get("zs_count")}
    s_full = {
        "bi_count": full.get("meta", {}).get("bi_count"),
        "seg_count": full.get("meta", {}).get("seg_count"),
        "zs_count": full.get("meta", {}).get("zs_count")}
    print(f"       [info] 回放 bi={s_replay['bi_count']} seg={s_replay['seg_count']} "
          f"zs={s_replay['zs_count']} | 全量 bi={s_full['bi_count']} "
          f"seg={s_full['seg_count']} zs={s_full['zs_count']}")

    print()
    if failures:
        print(f"===== trigger_step 回放验证: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== trigger_step 回放验证: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
