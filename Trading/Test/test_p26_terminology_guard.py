# -*- coding: utf-8 -*-
"""
P26 术语护栏（防"腿"及已废弃术语回潮）
==================================================================
背景（为什么需要这个文件）
--------------------------
2026-09-10 方案 A 曾把 `Trading/` 的"腿"字按对照表全部替换（155 处 → 残留 0），
配套文档见 `Docs/术语对照表_腿_新旧_20260910.md`。

**但只清一次没用。** 随后两轮改动（规则 ⑸ 改造、PositionOrigin 改名 + 旧 schema
闸门）新写的注释又把已废弃术语重新引入 15 处 —— 因为"不要用 X"只写在文档里，
不在任何可执行断言里。本测试把它变成**可执行护栏**：任何文件一旦出现禁用词，
本测试立即变红。

扫描范围
--------
  · `Trading/**`（含 Test）、`App/**`、`Frontend/**` 的 .py / .js
  · 仓库根目录的 `verify_*.py` 探针脚本
  不扫 `Docs/**`（历史决策快照，批量改会篡改可追溯性）、不扫 `State/` `replay_data/`
  等运行时目录。

禁用词与依据（**改术语请先改对照表**，勿自行新造词）
----------------------------------------------------
  | 禁用（旧）        | 允许（新）      | 说明 |
  |------------------|----------------|------|
  | 腿（全部复合词）   | 见下方映射      | 三类语义需分别表述，压平会别扭 |
  | 反向腿            | 反向仓          | 锁仓时新开的那笔反向持仓 |
  | 锁仓腿            | 锁仓持仓        | 处于 SOFT_EXIT_LOCK 的持仓 |
  | 原仓腿 / 原腿      | 原仓            | "腿"本就多余 |
  | 配对腿/配对同向腿   | 配对持仓/配对同向持仓 | |
  | 剩腿              | 剩余持仓        | 解锁后剩下的那笔 |
  | 两腿 / 一腿        | 两笔 / 一笔     | 笔数口径 |
  | 双腿 / 留双腿      | 双向持仓 / 留双向持仓 | 期货标准说法 |
  | 腿数 / 总腿数      | 笔数 / 总笔数   | |
  | 腿态              | 持仓模式组合    | 测试里 `2 LOCKED + 1 OPEN` 这类组合 |
  | 挑腿规则           | 选仓规则        | Q3 决策：选哪笔来平 |
  | 今日腿 / 昨仓腿    | 今仓 / 昨仓     | 直接用官方术语 |
  | leg / legs（英文） | pos / positions | 含 `_leg` 前缀标识符（_is_today_leg / locked_leg） |
  | 双仓              | 双向持仓        | v2 修正：自造词 |
  | 丢仓              | 丢失持仓        | v2 修正：自造缺陷名 |
  | open_first        | SIGNAL_OPEN     | 2026-09-10 改名前的旧枚举值名 |
  | unlock_first      | UNLOCK_UPGRADE  | 同上（p13 历史文件名除外，见下方正则） |

为什么可以断言这件事
--------------------
安全评估结论（见《术语盘点_腿_替换方案_20260910.md》第二节）：这些词**只出现在
注释、docstring 与测试断言文案**，不参与任何逻辑判定 —— 事件 kind、持久化字段 key、
变量/函数/类名均为英文。故禁止它们 = 零功能风险的纯可读性约束。

注：本文件自身是"禁用词定义处"，docstring 与本表必然含禁用词，故扫描时排除自身
（见 `_scan` 内的 `__file__` 判断）。

跑法：python Trading/Test/test_p26_terminology_guard.py
"""
from __future__ import annotations

import io
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))      # <root>/Trading/Test
_ROOT = os.path.dirname(os.path.dirname(_HERE))         # <root>

_PASS = 0
_FAIL = 0


def check(name: str, got, want) -> None:
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


# ── 禁用词表（键 = 禁止出现的中文字面量；值 = 应改成的表述）──────────────────
_BANNED_CN = {
    "腿": "全部复合词按对照表替换（反向腿→反向仓 / 锁仓腿→锁仓持仓 / …）",
    "双仓": "→ 双向持仓",
    "丢仓": "→ 丢失持仓",
    "腿态": "→ 持仓模式组合",
    "挑腿": "→ 选仓规则",
}

# 英文 leg / legs：前后不得紧邻字母。
#   ✓ 命中：leg、legs、_is_today_leg、locked_leg、LEG
#   ✗ 不误伤：legal、legacy、privilege、elegant（leg 前后是字母）
_LEG_RE = re.compile(r"(?<![A-Za-z])legs?(?![A-Za-z])", re.IGNORECASE)

# 旧枚举值名（PositionOrigin 改名前的 open_first / unlock_first）。
# 这些名字已不在 Types.py 中，但当时改名的遗漏让它们残留在注释与测试文案里
# （如 `check("事件流含 open_first", ...)`），测试日志会误导读者以为旧名还在用。
# 前后不得紧邻字母/下划线：
#   ✓ 命中：来源标记 open_first、origin=unlock_first
#   ✗ 不误伤：tests/test_p13_unlock_first_entry.py（历史文件名，unlock 前是下划线）
#   ✗ 不误伤：unlock_upgrade / soft_exit_lock（新名）
_OLD_ENUM_RE = re.compile(r"(?<![A-Za-z_])(?:open_first|unlock_first)(?![A-Za-z_])")

_SKIP_DIRS = {"__pycache__", ".venv", ".git", "State", "replay_data", "node_modules"}
_EXTS = (".py", ".js")


def _iter_scan_files():
    """产出待扫描文件路径（绝对路径）。"""
    for sub in ("Trading", "App", "Frontend"):
        base = os.path.join(_ROOT, sub)
        if not os.path.isdir(base):
            continue
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if d not in _SKIP_DIRS]
            for fn in fns:
                if fn.endswith(_EXTS):
                    yield os.path.join(dp, fn)
    # 根目录探针脚本
    for fn in sorted(os.listdir(_ROOT)):
        if fn.startswith("verify_") and fn.endswith(".py"):
            yield os.path.join(_ROOT, fn)


def _scan():
    """返回 (cn_hits, leg_hits, old_hits)，元素为 (相对路径, 行号, 命中词, 该行内容)。"""
    cn_hits, leg_hits, old_hits = [], [], []
    self_path = os.path.abspath(__file__)
    for p in _iter_scan_files():
        if os.path.abspath(p) == self_path:
            continue                      # 本文件是禁用词定义处，排除自身
        rel = os.path.relpath(p, _ROOT)
        try:
            text = io.open(p, encoding="utf-8").read()
        except (UnicodeDecodeError, OSError):
            continue
        for i, ln in enumerate(text.splitlines(), 1):
            for w in _BANNED_CN:
                if w in ln:
                    cn_hits.append((rel, i, w, ln.strip()[:110]))
                    break
            m = _LEG_RE.search(ln)
            if m:
                leg_hits.append((rel, i, m.group(0), ln.strip()[:110]))
            m2 = _OLD_ENUM_RE.search(ln)
            if m2:
                old_hits.append((rel, i, m2.group(0), ln.strip()[:110]))
    return cn_hits, leg_hits, old_hits


print("=" * 60)
print("P26 术语护栏（禁用词回潮检测）")
print("=" * 60)


# ── [0] 元护栏：先证明检测器本身有效（否则"零命中"可能是假的）──────────────
print("\n[0] 元护栏：检测器自证")
check("中文禁用词表非空（总根 + 4 个自造废弃词）", len(_BANNED_CN) >= 5, True)
check("每个中文禁用词都能被 `in` 检出",
      all(w in ("前缀" + w + "后缀") for w in _BANNED_CN), True)
check("leg 正则命中 'leg'", bool(_LEG_RE.search("leg")), True)
check("leg 正则命中 'legs'", bool(_LEG_RE.search("legs")), True)
check("leg 正则命中 '_is_today_leg'（下划线前缀）",
      bool(_LEG_RE.search("_is_today_leg")), True)
check("leg 正则命中 'locked_leg'", bool(_LEG_RE.search("locked_leg")), True)
check("leg 正则不误伤 'legacy'", bool(_LEG_RE.search("legacy")), False)
check("leg 正则不误伤 'legal'", bool(_LEG_RE.search("legal")), False)
check("leg 正则不误伤 'privilege'", bool(_LEG_RE.search("privilege")), False)
check("旧枚举名正则命中 '来源标记 open_first'",
      bool(_OLD_ENUM_RE.search("来源标记 open_first")), True)
check("旧枚举名正则命中 'origin=unlock_first'",
      bool(_OLD_ENUM_RE.search("origin=unlock_first")), True)
check("旧枚举名正则不误伤 'unlock_upgrade'",
      bool(_OLD_ENUM_RE.search("unlock_upgrade")), False)
check("旧枚举名正则不误伤 p13 历史文件名",
      bool(_OLD_ENUM_RE.search("tests/test_p13_unlock_first_entry.py")), False)


# ── [1] 实际扫描 ──────────────────────────────────────────────────────────
print("\n[1] 全仓扫描（Trading/ App/ Frontend/ + 根目录 verify_*.py）")
_cn, _leg, _old = _scan()

_n_files = len(list(_iter_scan_files()))
check("扫描文件数 >= 50（确认真的扫到了）", _n_files >= 50, True)
check("无已废弃中文术语（腿 / 双仓 / 丢仓 / 腿态 / 挑腿）", len(_cn), 0)
check("无英文 leg / legs 标识符", len(_leg), 0)
check("无旧枚举值名残留（open_first / unlock_first）", len(_old), 0)

if _cn:
    print("\n    !! 中文禁用词命中的位置（请按对照表改写）：")
    for rel, i, w, ln in _cn:
        print("      {}:{}  [{}]  {}".format(rel, i, w, ln))
if _leg:
    print("\n    !! 英文 leg 命中的位置（请改名为 pos / positions）：")
    for rel, i, w, ln in _leg:
        print("      {}:{}  [{}]  {}".format(rel, i, w, ln))
if _old:
    print("\n    !! 旧枚举值名残留（请改为 SIGNAL_OPEN / UNLOCK_UPGRADE）：")
    for rel, i, w, ln in _old:
        print("      {}:{}  [{}]  {}".format(rel, i, w, ln))


# ── [2] 关键文件确实被纳入扫描（防止扫描逻辑被改窄而静默失效）──────────────
print("\n[2] 扫描覆盖面自检（防扫描范围被改窄）")
_rels = {os.path.relpath(p, _ROOT).replace("\\", "/") for p in _iter_scan_files()}
for _must in ("Trading/Infra/Types.py",
              "Trading/Engine/Engine.py",
              "Trading/Engine/PositionBook.py",
              "Trading/Broker/SimNow.py",
              "Trading/Test/test_p25_origin_decoupled.py",
              "Frontend/app.js"):
    check("已纳入扫描: %s" % _must, _must in _rels, True)


print("\n" + "=" * 60)
print("P26 结果: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
