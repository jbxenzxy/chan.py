# -*- coding: utf-8 -*-
"""Gate ⑦ (2026-10-02)：两测试入口必须覆盖同一组**真实用例**。

背景
---------------------------------------------------------------------
`Test/run_all.py` 是**注册式门禁入口**（COMPONENTS 列表，冻结基线比对），
`run_all_tests.py` 是**发现式全量入口**（discover() 扫 Test/ + Trading/Test/
下所有 test_/repro_/smoke_* 脚本）。两条入口必须覆盖同一组脚本，否则：
  · 注册式漏登记某 test_* = 该用例从不进门禁（回归裸奔）；
  · 注册式指向缺失文件 = 门禁组件悬空（跑一个不存在的脚本）。

历史教训（2026-10-02）：run_all_tests.py 文档曾长期写「142 个组件」，而
run_all.py 实际已 143——两入口组件数慢性漂移无人察觉。本测试把"两入口覆盖
一致"从口头约定钉成门禁，杜绝此类回归。

特例（故意只在发现式里、不进注册式）：少数 repro_*/smoke_* 诊断脚本按
run_all.py 第 610 行起的「暂不注册」注释与 run_all_tests.py 文档约定，属于
"命中即非 0 的复现脚本 / 需真实 SimNow 连接 / 端到端冒烟已内置"，不该进冻结
基线门禁，故列入 EXEMPT，不参与 [C1] 漏登记判定。

断言
---------------------------------------------------------------------
  [C1] 除 EXEMPT 外，discover() 发现的每一支脚本都必须登记在 COMPONENTS；
  [C2] COMPONENTS 每一条都必须指向**真实存在**的文件（无悬空登记）。

零网络、无需凭据。跑法：python Test/test_gate_component_count.py
"""
from __future__ import annotations

import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
for _p in (TEST_DIR, REPO_ROOT):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import run_all_tests as _disc
import run_all as _reg

# 故意只在发现式入口、不进注册式门禁的诊断/环境依赖型脚本——
# 与 run_all.py 第 610 行「暂不注册」清单一致。
EXEMPT = {
    "Test/repro_n4_cleanup_race.py",
    "Test/smoke_phase7.py",
    "Trading/Test/smoke_simnow_phase_g.py",
}


def _check():
    discovered = set(_disc.discover(REPO_ROOT))
    registered = {c[1].replace("\\", "/") for _, c in _reg.COMPONENTS}

    # [C1] 非豁免的真实脚本必须全部登记（否则回归裸奔）
    orphans = sorted((discovered - EXEMPT) - registered)
    # [C2] 注册式不得指向缺失文件（否则门禁组件悬空）
    dangling = sorted(r for r in registered
                      if not os.path.exists(os.path.join(REPO_ROOT, r)))

    ok = (not orphans) and (not dangling)
    print("[gate⑦] discover() 发现 %d 支；COMPONENTS 登记 %d 条；EXEMPT %d 支"
          % (len(discovered), len(registered), len(EXEMPT)))
    if orphans:
        print("[gate⑦] FAIL [C1] 以下脚本被发现但未登记进 COMPONENTS"
              "（回归裸奔，%d 支）：" % len(orphans))
        for r in orphans:
            print("    + %s" % r)
    if dangling:
        print("[gate⑦] FAIL [C2] 以下 COMPONENTS 登记指向缺失文件（%d 条）："
              % len(dangling))
        for r in dangling:
            print("    - %s" % r)
    if ok:
        print("[gate⑦] PASS：两入口覆盖一致（无孤儿、无悬空）")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(_check())
