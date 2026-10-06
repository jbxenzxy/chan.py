# -*- coding: utf-8 -*-
"""
bsp 组装契约：页面投影 vs 回测投影（Backtest/Test/test_bt05_bsp_dict.py）
=========================================================================
设计文档 §4.5 / §5.9 P1-3 的**常驻护栏**（不是一次性对拍 —— 一次性对拍在
页面改字段的那天就已经过期了，而且没人会发现）。

问题形状
---------------------------------------------------------------------
`CBS_Point` → dict 的组装在仓内是**多处投影**（全部 `grep '"fractal_low"'` 的结果）：

    App/AppEngine.py:1148    股票主窗
    App/AppEngine.py:1482    股票子级别
    App/AppSSE.py:1447       期货（SSE）
    Backtest/Runner.py       `bsp_to_dict()`  ← 回测侧自持（R31 禁 import App）

而这 9 个键是 `Trading/Infra/Records.py:Signal.from_bsp` 的**输入契约**：
少一个 `fractal_low` 就会静默退化成 `fractal ≤ 0` 哨兵 ⇒ R 只剩 ATR，
`Exit.py` 打 `[R 结构距离缺失]` 告警 —— 结果全错但没有一处报错。

护栏怎么钉（**机器提取源，不手抄**）
---------------------------------------------------------------------
① 用 `ast` 从 `App/*.py` 源码里**提取**所有含 `fractal_low` 的字典字面量，
   与 `Backtest.bsp_to_dict` 的键集做**集合相等**。App 侧加了 / 删了 / 改名了
   一个键，本用例立刻红 —— 而不是等回测的 R 悄悄变样。
② 对**股票侧两处**（`AppEngine.py`）进一步做**逐键值比对**：把 AST 提取到的那段
   表达式**当场 eval**（stub bsp 驱动）当作"页面口径"，与 `bsp_to_dict` 的输出
   逐键相等。舍入位数（`round(..., 3)`）、价格是否取原值、时间戳用哪种算法
   全都在这一条里被钉住 —— 改 App 侧的表达式，护栏跟着变，**不需要人来同步**。
③ 端到端可消费：`Signal.from_bsp(bsp_to_dict(..))` 必须不抛异常且字段落位正确
   （`from_bsp` 对缺 `date`/`timestamp` 是**抛 ValueError** 的，这里顺带证明回测
   侧给得出合法值）。
④ 期货路径（`AppSSE.py`）**只比键集**并**披露**差异：它的 `price/high/low` 是
   `round(..., 3)`，股票侧是原值 —— 这是 App 侧既有的两种口径，不是回测的锅，
   本用例不把它当缺陷报，只打印出来。
⑤ 判别力自证：喂一段"少一个键"和一段"舍入位数不同"的字面量，必须被检出。

跑法：`python Backtest/Test/test_bt05_bsp_dict.py`（退出码 0/1 即判决）
"""
import ast
import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKTEST_DIR)
for _p in (ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

APP_FILES = ("App/AppEngine.py", "App/AppSSE.py")

# Backtest.bsp_to_dict 的契约键集（应用侧必须与它一致；顺序不参与判定）
BSP_KEYS = {"date", "timestamp", "type", "is_buy",
            "price", "high", "low", "fractal_low", "fractal_high"}

DATE_FMT = "%Y-%m-%d"
DATE_STR = "2021-08-23"

PASS = 0
FAIL = 0


def check(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print("[PASS] %s" % name)
    else:
        FAIL += 1
        print("[FAIL] %s" % name)
        if detail:
            print("        " + str(detail).replace("\n", "\n        "))


# ─────────────────────────────────────────────────────────────────────
# stub bsp（只实现被那两段表达式用到的接口）
# ─────────────────────────────────────────────────────────────────────
class _Time:
    def __init__(self, s):
        self.s = s

    def toFmtStr(self, fmt):                 # noqa: N802 —— 照抄 chan.py 的命名
        return self.s


class _Klu:
    def __init__(self, date, o, h, low, c):
        self.time = _Time(date)
        self.open, self.high, self.low, self.close = o, h, low, c


class _Bi:
    def __init__(self, fklu):
        self._fklu = fklu

    def get_end_klu(self):
        return self._fklu


class StubBsp:
    def __init__(self, *, date, o, h, low, c, f_low, f_high, is_buy, tstr):
        self.klu = _Klu(date, o, h, low, c)
        self.bi = _Bi(_Klu(date, o, f_high, f_low, c))
        self.is_buy = is_buy
        self._tstr = tstr

    def type2str(self):
        return self._tstr


# ─────────────────────────────────────────────────────────────────────
# AST：提取含 fractal_low 的字典字面量
# ─────────────────────────────────────────────────────────────────────
def bsp_dict_literals(path):
    """返回 [(lineno, ast.Dict)] —— 源码里含键 `fractal_low` 的字典字面量。"""
    with open(path, "r", encoding="utf-8") as f:
        src = f.read()
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Dict):
            keys = [k.value for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)]
            if "fractal_low" in keys:
                out.append((node.lineno, node))
    return out


def keys_of(dict_node):
    """保持源码顺序的字符串键 list。"""
    return [k.value for k in dict_node.keys
            if isinstance(k, ast.Constant) and isinstance(k.value, str)]


def eval_dict_node(dict_node, ns):
    """把提取到的字典字面量**当场求值**（用 stub 命名空间）。"""
    code = compile(ast.Expression(body=dict_node), "<app-dict>", "eval")
    return eval(code, {"__builtins__": {}}, ns)     # noqa: S307 —— 源是本仓自己的代码


def ns_for(bsp, bsp_date, bsp_ts):
    return {"round": round, "bsp": bsp, "klu": bsp.klu,
            "f_klu": bsp.bi.get_end_klu(),
            "bsp_date": bsp_date, "bsp_ts": bsp_ts, "date_fmt": DATE_FMT}


def main():
    print("=" * 68)
    print("bsp 组装契约（Backtest/Test/test_bt05_bsp_dict.py）")
    print("=" * 68)

    from Backtest.Runner import bsp_to_dict

    # stub 场景：买点 + 卖点各一（is_buy 的两种取值都要走到）。
    # ⚠ 价格/分型**刻意给到 4 位小数**：若给 3 位以内，④ 的"期货侧 round(...,3)
    #   与原值不同"这条披露会因为两边恰好相等而**空转**（断言仍绿但什么都没验）。
    cases = [
        StubBsp(date=DATE_STR, o=29.5123, h=30.2456, low=28.9876, c=30.0123,
                f_low=28.3945, f_high=29.3356, is_buy=True, tstr="3"),
        StubBsp(date=DATE_STR, o=23.9123, h=25.2345, low=23.2123, c=23.3656,
                f_low=23.2834, f_high=25.1178, is_buy=False, tstr="3"),
    ]

    # ── 收集源码字面量 ───────────────────────────────────────────
    literals = []
    for rel in APP_FILES:
        p = os.path.join(ROOT, rel)
        for lineno, node in bsp_dict_literals(p):
            literals.append(("%s:%d" % (rel, lineno), rel, node))
    if not literals:
        check("① App 侧 bsp 字典字面量可被提取", False,
              "在 %r 里没找到含 fractal_low 的字典字面量 —— 组装点可能被搬走/改名了"
              % (APP_FILES,))
        return 1
    print("     提取到 App 侧组装点 %d 处：%s"
          % (len(literals), ", ".join(l[0] for l in literals)))

    # ── ① 键集相等 ───────────────────────────────────────────────
    bad = []
    for label, rel, node in literals:
        got = set(keys_of(node))
        if got != BSP_KEYS:
            bad.append("%s：缺 %r / 多 %r"
                       % (label, sorted(BSP_KEYS - got), sorted(got - BSP_KEYS)))
    check("① 每个 App 侧组装点的键集 ≡ Backtest.bsp_to_dict 的键集", not bad,
          "\n".join(bad))

    # 回测侧自身键集（防止两边一起漂）
    bt_keys = set()
    for bsp in cases:
        bt_keys |= set(bsp_to_dict(bsp, DATE_FMT))
    check("①b Backtest.bsp_to_dict 自身键集 ≡ 契约键集", bt_keys == BSP_KEYS,
          "回测侧=%r 契约=%r" % (sorted(bt_keys), sorted(BSP_KEYS)))

    # ── ② 股票侧（AppEngine）逐键值比对：eval 真实表达式 ─────────
    stock = [(lb, rel, node) for lb, rel, node in literals if rel == "App/AppEngine.py"]
    if not stock:
        check("② 股票侧（AppEngine）逐键值比对", False,
              "App/AppEngine.py 里没找到组装点")
    else:
        diffs = []
        for label, rel, node in stock:
            for i, bsp in enumerate(cases):
                bsp_date = bsp.klu.time.toFmtStr(DATE_FMT)
                bsp_ts = int(datetime.strptime(bsp_date, DATE_FMT).timestamp()) * 1000
                want = eval_dict_node(node, ns_for(bsp, bsp_date, bsp_ts))
                got = bsp_to_dict(bsp, DATE_FMT)
                for k in sorted(BSP_KEYS):
                    if got.get(k) != want.get(k):
                        diffs.append("case%d 键 %r：回测=%r 页面=%r"
                                     % (i, k, got.get(k), want.get(k)))
        check("② 股票侧 %d 处 × %d 场景 逐键值 ≡ 页面表达式（当场 eval）"
              % (len(stock), len(cases)), not diffs, "\n".join(diffs))

    # ── ③ 端到端可消费（Signal.from_bsp）─────────────────────────
    from Trading.Infra.Records import Signal
    d = []
    for i, bsp in enumerate(cases):
        got = bsp_to_dict(bsp, DATE_FMT)
        try:
            sig = Signal.from_bsp(got, symbol="sz002190", freq="d")
        except Exception as e:                                 # noqa: BLE001
            d.append("case%d from_bsp 抛 %s: %s" % (i, type(e).__name__, e))
            continue
        for attr, key in (("date", "date"), ("bsp_type", "type"),
                          ("is_buy", "is_buy"), ("price", "price"),
                          ("high", "high"), ("low", "low"),
                          ("fractal_low", "fractal_low"),
                          ("fractal_high", "fractal_high"),
                          ("timestamp", "timestamp")):
            if getattr(sig, attr) != got[key]:
                d.append("case%d %s：Signal=%r dict=%r"
                         % (i, attr, getattr(sig, attr), got[key]))
    check("③ Signal.from_bsp(bsp_to_dict(..)) 可消费且字段落位", not d, "\n".join(d))

    # ── ④ 期货路径只比键集 + 披露价格口径差异 ────────────────────
    sse = [(lb, rel, node) for lb, rel, node in literals if rel == "App/AppSSE.py"]
    if not sse:
        print("[NOTE] App/AppSSE.py 未找到组装点（期货路径可能已迁移），键集断言①已覆盖现存点")
    else:
        note = []
        for label, rel, node in sse:
            for i, bsp in enumerate(cases):
                bsp_date = bsp.klu.time.toFmtStr(DATE_FMT)
                want = eval_dict_node(
                    node, ns_for(bsp, bsp_date,
                                 int(datetime.strptime(bsp_date, DATE_FMT).timestamp()) * 1000))
                got = bsp_to_dict(bsp, DATE_FMT)
                for k in ("price", "high", "low"):
                    if got.get(k) != want.get(k):
                        note.append("%s case%d 键 %r：回测=%r 期货页面=%r"
                                    % (label, i, k, got.get(k), want.get(k)))
        check("④a 期货路径（AppSSE）键集与回测一致（值口径允许不同）",
              all(set(keys_of(n)) == BSP_KEYS for _, _, n in sse), "")
        if note:
            print("[NOTE] 期货路径价格口径与股票侧不同（**App 侧既有差异，非回测缺陷**）：")
            for line in sorted(set(note)):
                print("       " + line)
            print("       ⇒ 回测侧跟随股票口径（原值），故此处不求相等，仅披露。")

    # ── ⑤ 判别力自证（证明①/②不是恒真式）───────────────────────
    src_missing = "d = {'date': 1, 'type': '3', 'is_buy': True}"
    src_round = ("d = {'date': 1, 'timestamp': 2, 'type': '3', 'is_buy': True,"
                 " 'price': 1.0, 'high': 2.0, 'low': 3.0,"
                 " 'fractal_low': round(0.12345, 4), 'fractal_high': round(0.5, 4)}")
    n_missing = ast.parse(src_missing).body[0].value
    n_round = ast.parse(src_round).body[0].value

    caught_missing = set(keys_of(n_missing)) != BSP_KEYS
    bsp = cases[0]
    bsp_date = bsp.klu.time.toFmtStr(DATE_FMT)
    evaled = eval_dict_node(n_round, ns_for(
        bsp, bsp_date, int(datetime.strptime(bsp_date, DATE_FMT).timestamp()) * 1000))
    caught_round = bsp_to_dict(bsp, DATE_FMT)["fractal_low"] != evaled["fractal_low"]

    check("⑤ 判别力自证：缺键被检出 + 舍入位数不同被检出",
          caught_missing and caught_round,
          "缺键检出=%r 舍入差异检出=%r" % (caught_missing, caught_round))

    print("-" * 68)
    print("合计 %d 项，通过 %d，失败 %d" % (PASS + FAIL, PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
