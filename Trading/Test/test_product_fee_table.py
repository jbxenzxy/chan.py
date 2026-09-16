#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
P-Fee 费率对账测试（R1 · D-B · 2026-09-15）
=============================================

为什么必须有这个文件
--------------------
交接文档 §5.4 / §6.9 / §7.1 **三处**要求建它，P-A 落地时漏建，只留了
`Product.py` 里三处悬空引用。后果：全仓唯一"真在算钱"的 8 个费率数字
**零测试覆盖** —— 其余都有护栏（周期有 test_period_consistency、术语有
test_p26、旧字段有负向断言），唯独费率裸奔。

`Docs/Infra划分治理_落地核验与再优化_20260915.md` 定性为 **P0**：删掉的
`test_p52_fee_economy.py`（−368 行）没有后继，而"全量测试全绿"的验收口径
结构性地看不见这件事（52 → 51 依然"全绿"）。

D-B（生成式 SSOT）之后本文件的职责
----------------------------------
生产链路上的手抄值已经消灭（费率只有一个手写处 = 券商 xlsx）：

    Docs/手续费标准-.xlsx → Tool/GenFeeTable.py → Product.py 的 GENERATED 区块 → 档案

⚠️ **生成物是"区块"不是"独立文件"**（2026-09-15 用户拍板）：交接文档 §5.1 把
`Trading/Infra/` 定死成 7 个模块、按变异轴排，因此生成的纯数据落在 `Product.py`
内的 `>>> GENERATED … <<< END GENERATED` 之间，靠**标记 + 逐字节护栏**与手写内容分离。

本测试对这条链路做**两层**独立校验，两层角色不同、都要在：

  [A] 业务口径断言（硬编码期望值）
      —— 锁"人工确认过的费率口径"。例如 CFFEX 平今取 xlsx 的 万2.3，
      而非早期代码的 万3.45（= 万2.3 × 1.5）。这一层即使 xlsx 与生成区块
      **同时**被换错也照样报警，是唯一不依赖 xlsx 的第二只眼睛。

  [B] 新鲜度断言（重解析 xlsx 比对区块）
      —— 锁两件事：① xlsx 改了但忘了重跑生成器；② **有人手改了区块里的数字**。
      这是生成式 SSOT 的配套护栏，没有它，xlsx 更新后区块会静默过期。

外加四条结构性护栏（R2 / R3 / D-A 的防回潮 + 内联设计本身的护栏）：

  [C] `LOCK_PATH_MULT` 必须已从 Product 删除（3× 费率口径判据消亡）
  [D] `fee_overrides` 字段位必须存在（方案 §6.3-b 明令"字段位必须留"）
  [E] 平今判据 = 品种执行策略表第 1 列 `close_mode`（静态、不读费率）
  [F] 费率数字只许出现在 GENERATED 区块内（手写部分一个字面量都不许有）

跑法：python Trading/Test/test_product_fee_table.py
依赖：openpyxl 仅 [B] 组需要；缺失时该组自动 skip（离线/裁剪仓库不算失败）。
"""
from __future__ import annotations

import dataclasses
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))


def _locate_tg_root() -> str:
    d = _HERE
    for _ in range(6):
        if os.path.basename(d) == "Trading" and os.path.isfile(
                os.path.join(d, "__init__.py")):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return ""


_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or _locate_tg_root()
if not _TG_ROOT:
    print("✗ 找不到 Trading 包。")
    raise SystemExit(2)
_REPO_ROOT = os.path.dirname(_TG_ROOT)
sys.path.insert(0, _REPO_ROOT)

from Trading.Infra.Product import (BASE, BASE_LABELS, R_OPEN,  # noqa: E402
                                   CLOSETODAY, EXEC_POLICY, OVERRIDES,
                                   PRODUCT_PROFILES, Fee, Product)

try:
    import openpyxl                                                   # noqa: F401
    _HAS_OPENPYXL = True
except ImportError:                                                   # pragma: no cover
    _HAS_OPENPYXL = False

# 生成器（直接按文件路径加载：Tool/ 不在包扫描路径上，且本测试要的就是
# "生成器解析结果"这个独立事实，绕开包结构最省事）。
_gen_path = os.path.join(_TG_ROOT, "Tool", "GenFeeTable.py")
_spec = importlib.util.spec_from_file_location("_gen_fee_table_under_test", _gen_path)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)

_XLSX = os.path.join(_REPO_ROOT, gen.XLSX_REL)
_PRODUCT_PATH = os.path.join(_TG_ROOT, "Infra", "Product.py")

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {} -> {!r}".format(name, got))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


def skip(name, why):
    print("  ⊘ {}（skip：{}）".format(name, why))


def _read(path: str) -> str:
    with open(path, encoding="utf-8", newline="") as f:
        return f.read()


_PROD_SRC = _read(_PRODUCT_PATH)


def _block_span(text: str):
    """返回 (BEGIN 标记匹配, END 标记匹配)，各自必须恰好一次。"""
    b = list(gen._BEGIN_RE.finditer(text))
    e = list(gen._END_RE.finditer(text))
    return b, e


def _outside_block(text: str) -> str:
    """剥掉 GENERATED 区块后的**手写部分**（用于"费率数字只许在区块里"的断言）。"""
    b, e = _block_span(text)
    if len(b) != 1 or len(e) != 1:
        return text
    return text[:b[0].start()] + text[e[0].end():]


def _first_diff_line(a: str, b: str) -> str:
    """返回首个不一致行的紧凑描述（仅失败时用；通过时不该把整份文件打进日志）。"""
    la = a.replace("\r\n", "\n").split("\n")
    lb = b.replace("\r\n", "\n").split("\n")
    for i in range(max(len(la), len(lb))):
        x = la[i] if i < len(la) else "<EOF>"
        y = lb[i] if i < len(lb) else "<EOF>"
        if x != y:
            return "首处差异 第{}行: 期望 {!r} / 磁盘 {!r}".format(i + 1, x[:70], y[:70])
    return "(无差异？长度不同)"


# ══════════════════════════════════════════════════════════════════
# [A] 业务口径（硬编码期望值 —— 人工确认的费率真值，不依赖 xlsx）
# ══════════════════════════════════════════════════════════════════
# 口径确认记录（2026-09-15 用户拍板"以 xlsx 为准"）：
#   · CFFEX IF/IH/IC/IM：交易万0.23 / 平今万2.3（旧代码曾用万3.45 = 万2.3×1.5）；
#   · 有独立平今行的基准条目里，**没有一行把"平昨"单独列出** → 平昨 ≡ 开仓；
#   · AU/TA 平今免收 → per_lot 0.0（显式 free，不靠"0 兼表免与未填"）；
#   · AG 无独立平今行 → None（回开仓档）；
#   · CU 平今万1.0 = 2× 开仓万0.5 → 落在 3× 判据分歧区，派生走平今。
_EXPECT_BASE = {
    "IF": (("rate", 0.23), ("rate", 2.3)),
    "IH": (("rate", 0.23), ("rate", 2.3)),
    "IC": (("rate", 0.23), ("rate", 2.3)),
    "IM": (("rate", 0.23), ("rate", 2.3)),
    "AU": (("per_lot", 10.0), ("per_lot", 0.0)),
    "AG": (("rate", 0.1), None),
    "CU": (("rate", 0.5), ("rate", 1.0)),
    "TA": (("per_lot", 3.0), ("per_lot", 0.0)),
}

# 覆盖档期望（合约月份差异化费率；字段位已留、当前不消费）
_EXPECT_OVERRIDES = {
    "AU": (("6、12合约&2607、2608、2609、2610合约", ("per_lot", 20.0), ("per_lot", 0.0)),),
    "AG": (("6、12合约&2607、2608、2609、2610合约", ("rate", 0.5), None),),
}

# 平今判据期望（品种执行策略表第 1 列 close_mode；不看交易所、不算费率）
_EXPECT_CLOSE_MODE = {"IF": "R-OPEN", "IH": "R-OPEN",
                      "IC": "R-OPEN", "IM": "R-OPEN",
                      "AU": "CLOSETODAY", "AG": "CLOSETODAY",
                      "CU": "CLOSETODAY", "TA": "R-OPEN"}


# ══════════════════════════════════════════════════════════════════
print("\n[0] 生成区块自身结构（Product.py 内的 GENERATED 区块）")
# ══════════════════════════════════════════════════════════════════
check("[0a] 基准档覆盖全部 8 个档案键",
      sorted(BASE), sorted(_EXPECT_BASE))
check("[0b] 生成区块 ⇄ 硬编码业务口径逐项一致（费率数字的唯一人工确认点）",
      {k: (tuple(BASE[k][0]), BASE[k][1] and tuple(BASE[k][1])) for k in BASE},
      {k: (tuple(o), ct and tuple(ct)) for k, (o, ct) in _EXPECT_BASE.items()})
for _code in sorted(_EXPECT_BASE):
    _o, _ct = BASE[_code]
    check("[0c] {} 开仓档 == 期望".format(_code),
          (_o[0], _o[1]), _EXPECT_BASE[_code][0])
    check("[0d] {} 平今档 == 期望".format(_code),
          None if _ct is None else (_ct[0], _ct[1]), _EXPECT_BASE[_code][1])
check("[0e] 覆盖档（AU/AG）= 期望",
      {k: tuple(OVERRIDES.get(k, ())) for k in ("AU", "AG", "IF")},
      {"AU": _EXPECT_OVERRIDES["AU"],
       "AG": _EXPECT_OVERRIDES["AG"], "IF": ()})
check("[0f] BASE_LABELS 是源表中文名备案（8 品种，便于人工回查 xlsx 行）",
      sorted(BASE_LABELS), sorted(_EXPECT_BASE))


# ══════════════════════════════════════════════════════════════════
print("\n[1] 档案 ⇄ 生成区块（Product 手写部分不得有第二份费率）")
# ══════════════════════════════════════════════════════════════════
for _code, _p in sorted(PRODUCT_PROFILES.items()):
    _exp_open = Fee(*_EXPECT_BASE[_code][0])
    _exp_ct = None if _EXPECT_BASE[_code][1] is None else Fee(*_EXPECT_BASE[_code][1])
    check("[1a] {} open_fee == 期望（kind/value 双比）".format(_code),
          (_p.open_fee.kind, _p.open_fee.value), (_exp_open.kind, _exp_open.value))
    check("[1b] {} closetoday_fee == 期望".format(_code),
          None if _p.closetoday_fee is None
          else (_p.closetoday_fee.kind, _p.closetoday_fee.value),
          None if _exp_ct is None else (_exp_ct.kind, _exp_ct.value))
    # fee_pair 的两档必须与生成区块逐字段同源（防"改了 open_fee 忘了 closetoday"）
    _po, _pct = _p.fee_pair()
    check("[1c] {} fee_pair() 两档 == 档案两档".format(_code),
          (_po.kind, _po.value, _pct.kind, _pct.value),
          (_p.open_fee.kind, _p.open_fee.value,
           (_p.closetoday_fee or _p.open_fee).kind,
           (_p.closetoday_fee or _p.open_fee).value))

_seg = _PROD_SRC[_PROD_SRC.index("PRODUCT_PROFILES: Dict[str, Product]"):]
check("[1d] PRODUCT_PROFILES 段内 Fee(...) 字面量数 = 0",
      _seg.count("Fee("), 0)
check("[1e] 每个品种条目都用 **_fee_kw(code) 注入费率（费率只此一个入口）",
      sorted(re.findall(r'\*\*_fee_kw\("([A-Z0-9]+)"\)', _seg)),
      sorted(PRODUCT_PROFILES))
# [F] 内联设计的核心承诺：费率原始元组**只许在区块里**（手写部分剥掉后必须为空）
_outside = _outside_block(_PROD_SRC)
check("[1f] 手写部分（区块外）不含任何费率原始元组 ('rate'/'per_lot', 数字)",
      re.findall(r"\('(?:rate|per_lot)',\s*[0-9.]+\)", _outside), [])


# ══════════════════════════════════════════════════════════════════
print("\n[2] 生成器解析口径（文本 → (kind, value) 的边界）")
# ══════════════════════════════════════════════════════════════════
for _text, _want in (("0.23%%", (None, ("rate", 0.23))),
                     ("交易0.23%%", ("交易", ("rate", 0.23))),
                     ("平今2.3%%", ("平今", ("rate", 2.3))),
                     ("平今免", ("平今", ("per_lot", 0.0))),
                     ("平今免收", ("平今", ("per_lot", 0.0))),
                     ("10", (None, ("per_lot", 10.0))),
                     ("平今60", ("平今", ("per_lot", 60.0))),
                     ("交割0.5%%", ("交割", ("rate", 0.5))),
                     ("1%%", (None, ("rate", 1.0)))):
    _tag, _fee = gen._parse_fee(_text)
    check("[2a] _parse_fee({!r})".format(_text), (_tag, _fee), _want)
check("[2b] _parse_fee 空串 → None", gen._parse_fee(""), None)
for _name, _want in (("沪深300指数", ("IF", False)),
                     ("上证50", ("IH", False)),
                     ("中证500", ("IC", False)),
                     ("中证1000", ("IM", False)),
                     ("黄金", ("AU", False)),
                     ("黄金6、12合约&2607、2608、2609、2610合约", ("AU", True)),
                     ("白银", ("AG", False)),
                     ("白银6、12合约&2607、2608、2609、2610合约", ("AG", True)),
                     ("铜", ("CU", False)),
                     ("PTA", ("TA", False)),
                     ("黄金期权", (None, False)),
                     ("铜期权", (None, False)),
                     ("苹果", (None, False))):
    check("[2c] _classify({!r})".format(_name), gen._classify(_name), _want)


# ══════════════════════════════════════════════════════════════════
print("\n[3] 新鲜度 + 手改护栏：重解析 xlsx ⇄ Product.py 的区块")
# ══════════════════════════════════════════════════════════════════
_begin_hits, _end_hits = _block_span(_PROD_SRC)
check("[3a] 区块标记在 Product.py 内各出现且仅出现一次",
      (len(_begin_hits), len(_end_hits)), (1, 1))
check("[3b] 区块标记之间存在实质内容（不是空壳标记）",
      _begin_hits[0].end() < _end_hits[0].start(), True)

if not os.path.isfile(_XLSX):
    skip("[3c-3g] xlsx 新鲜度", "找不到 {}".format(_XLSX))
elif not _HAS_OPENPYXL:
    skip("[3c-3g] xlsx 新鲜度", "缺 openpyxl（pip install openpyxl 后重跑）")
else:
    _tables = gen.build_tables(_XLSX)
    _nl = gen.detect_nl(_PROD_SRC)
    _body = gen.render_block(_tables, _nl)

    check("[3c] 重解析的基准档 ⇄ 生成区块的 BASE 完全一致",
          {k: (tuple(v[0]), v[1] and tuple(v[1])) for k, v in _tables["base"].items()},
          {k: (tuple(BASE[k][0]), BASE[k][1] and tuple(BASE[k][1])) for k in BASE})
    check("[3d] 重解析的覆盖档 ⇄ 生成区块的 OVERRIDES 完全一致",
          {k: [tuple(x) for x in v] for k, v in _tables["overrides"].items()},
          {k: [tuple(x) for x in v] for k, v in OVERRIDES.items()})
    check("[3e] 重解析结果 ⇄ 硬编码业务口径（三方一致，最强断言）",
          {k: (tuple(v[0]), v[1] and tuple(v[1])) for k, v in _tables["base"].items()},
          {k: (tuple(o), ct and tuple(ct)) for k, (o, ct) in _EXPECT_BASE.items()})
    # [3f] 最关键的一条：拿"重新渲染的区块"整体替换进磁盘文件，若**逐字节无差异**
    #      则 xlsx 与区块同步、且区块没被手改。等价于"此刻重跑生成器 = 空操作"。
    #      断言值取"首处差异行"而非整份文本 —— 否则通过时会把 500 行文件打进日志。
    _expected = gen.replace_block(_PROD_SRC, _body, _nl)
    check("[3f] 磁盘区块 ⇄ 按 xlsx 重新渲染的区块（逐字节，忽略 CRLF）",
          True if _expected == _PROD_SRC else _first_diff_line(_expected, _PROD_SRC),
          True)

    # [3g] 护栏自检：证明 `--check` **不是恒返回 0**（不真跑一遍就没法确认它会咬人）
    def _run_check(target: str) -> int:
        p = subprocess.run(
            [sys.executable, _gen_path, "--check", "--target", target,
             "--xlsx", _XLSX],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            cwd=_REPO_ROOT)
        return p.returncode

    with tempfile.TemporaryDirectory() as _td:
        _tmp_ok = os.path.join(_td, "Product_ok.py")
        shutil.copy2(_PRODUCT_PATH, _tmp_ok)
        check("[3g] 未改动的副本 → --check rc=0（证明下面的 1 不是恒错）",
              _run_check(_tmp_ok), 0)

        # 在手写部分**之外**（即区块内）改一个费率数字，模拟"有人手改了区块"
        _b, _e = _block_span(_PROD_SRC)
        _head, _mid, _tail = (_PROD_SRC[:_b[0].end()],
                              _PROD_SRC[_b[0].end():_e[0].start()],
                              _PROD_SRC[_e[0].start():])
        _mid2 = _mid.replace("('rate', 0.23)", "('rate', 0.99)")
        check("[3h] 自检前置：区块内确实找得到可篡改的费率数字",
              _mid2 != _mid, True)
        _tmp_bad = os.path.join(_td, "Product_handedited.py")
        with open(_tmp_bad, "w", encoding="utf-8", newline="") as f:
            f.write(_head + _mid2 + _tail)
        check("[3i] 手改区块一个数字 → --check rc=1（护栏真的会咬人）",
              _run_check(_tmp_bad), 1)


# ══════════════════════════════════════════════════════════════════
print("\n[4] 决策侧不再有平今费率派生（2026-09-16：改由品种执行策略表给定）")
# ══════════════════════════════════════════════════════════════════
check("[4a] LOCK_PATH_MULT 已从 Product 删除（3× 口径判据随之消亡）",
      hasattr(Product, "LOCK_PATH_MULT"), False)
check("[4b] prefer_closetoday 已从 Product 删除（决策侧不再读费率）",
      hasattr(PRODUCT_PROFILES["IF"], "prefer_closetoday"), False)
check("[4c] prefer_closetoday_at 已从 Product 删除",
      hasattr(PRODUCT_PROFILES["IF"], "prefer_closetoday_at"), False)
check("[4d] LOCK_PATH_MULT 不在 dataclasses.fields 里",
      "LOCK_PATH_MULT" in [f.name for f in dataclasses.fields(Product)], False)
check("[4e] exec_policy 是新的 dataclass 字段（执行策略表注入口）",
      "exec_policy" in [f.name for f in dataclasses.fields(Product)], True)
check("[4f] ★ 费率数据仍在（会计侧 cost_cash 要用），只是决策侧不读",
      PRODUCT_PROFILES["CU"].fee_pair(),
      (Fee("rate", 0.5), Fee("rate", 1.0)))


# ══════════════════════════════════════════════════════════════════
print("\n[5] R3 护栏：fee_overrides 字段位存在")
# ══════════════════════════════════════════════════════════════════
check("[5a] fee_overrides 是 dataclass 字段（字段位已留）",
      "fee_overrides" in [f.name for f in dataclasses.fields(Product)], True)
check("[5b] 默认值 = 空元组（未消费 → 行为零变化）",
      Product(product="X", r_multiple_tp=1.0, multiplier=1.0,
              open_fee=Fee("rate", 1.0),
              exec_policy=PRODUCT_PROFILES["IF"].exec_policy).fee_overrides, ())
check("[5c] AU 覆盖档已由生成区块填入字段位",
      tuple((lb, (o.kind, o.value), None if ct is None else (ct.kind, ct.value))
            for lb, o, ct in PRODUCT_PROFILES["AU"].fee_overrides),
      _EXPECT_OVERRIDES["AU"])
check("[5d] AG 覆盖档已由生成区块填入字段位",
      tuple((lb, (o.kind, o.value), None if ct is None else (ct.kind, ct.value))
            for lb, o, ct in PRODUCT_PROFILES["AG"].fee_overrides),
      _EXPECT_OVERRIDES["AG"])
check("[5e] 无覆盖档品种 = 空元组", PRODUCT_PROFILES["IF"].fee_overrides, ())
# 字段位存在 ≠ 已消费：fee_pair 必须仍读基准档（防"悄悄启用覆盖档"改行为）
check("[5f] 未消费：fee_pair() 仍返回基准档（AU 10 元/手，非覆盖档 20）",
      PRODUCT_PROFILES["AU"].fee_pair()[0].value, 10.0)


# ══════════════════════════════════════════════════════════════════
print("\n[6] D-A 护栏（新）：平今判据 = 表第 1 列 close_mode（静态、不读费率）")
# ══════════════════════════════════════════════════════════════════
check("[6a] close_mode 取值域 = {R_OPEN, CLOSETODAY}（无第三种、无 None）",
      sorted({p.exec_policy.close_mode for p in PRODUCT_PROFILES.values()}),
      sorted({R_OPEN, CLOSETODAY}))
for _code in sorted(_EXPECT_CLOSE_MODE):
    check("[6b] {} close_mode == 期望".format(_code),
          PRODUCT_PROFILES[_code].exec_policy.close_mode,
          _EXPECT_CLOSE_MODE[_code])
check("[6c] 8 品种全部静态可判定（无空值 → 不依赖运行期价格/费率比较）",
      [c for c in PRODUCT_PROFILES
       if not PRODUCT_PROFILES[c].exec_policy.close_mode], [])
check("[6d] Product 上不残留旧费率派生符号",
      [n for n in ("prefer_closetoday", "prefer_closetoday_at",
                   "supports_closetoday")
       if hasattr(PRODUCT_PROFILES["IF"], n)], [])
check("[6e] Product.exec_policy ⇄ EXEC_POLICY 同源（不是另算一遍）",
      all(PRODUCT_PROFILES[c].exec_policy is EXEC_POLICY[c]
          for c in PRODUCT_PROFILES), True)
check("[6f] close_mode 与交易所名字无关：同表不同交易所同值亦允许"
      "（IF=R-OPEN 与 TA=R-OPEN 分属 CFFEX/CZCE）",
      (PRODUCT_PROFILES["IF"].exec_policy.close_mode,
       PRODUCT_PROFILES["TA"].exec_policy.close_mode), (R_OPEN, R_OPEN))


print("\n" + "=" * 62)
print("P-Fee 费率表对账 结果: {} 通过 / {} 失败".format(_PASS, _FAIL))
print("=" * 62)
sys.exit(1 if _FAIL else 0)
