# -*- coding: utf-8 -*-
"""0 类买卖点「第 5/7 笔」㈠ 闪电走势判据 —— 宽松口径（各回调笔只与笔1比较）

改动背景（2026-10-04）
----------------------
`BuySellPoint/BSPointList.py` 的 `_cal_bs0point_nth_ozs_57th` ㈠ 段原为
「相邻回调笔逐级收窄」（严格链式：买 笔n高 ≤ 笔(n-2)高 ≤ … ≤ 笔1高），
改为「各回调笔极值均不越过笔1」（宽松：买 每根回调笔高 ≤ 笔1高）。
严格实现整段以注释形态保留在该函数内。

为什么必须单列本护栏
--------------------
该分支在门禁的 8 个快照回归样本里**一次都没被触及**（dbg 无 ozs_57th 记录，
主级别 0 类点全走 `_cal_bs0point_3rd`）⇒ 快照全绿**证不了**这次改动。

注意这是「回归样本不含该形态」，不是「代码不可达」：全 A 股日线全量扫描
（5224 只）实测 57th 被进入 102,854 次、覆盖 5,176 只。故两条护栏分工——
本用例钉逻辑层（源码抽段 + stub 枚举，判别力不依赖任何样本），
`Test/test_bs0_ozs57_realdata.py` 钉端到端（真实行情冻结切片）。

四层断言
--------
[1] 结构契约：宽松段在 / docstring 描述已同步 / 严格段以注释保留 / 旧描述清零
[2] 超集不变量：凡严格口径通过的组合，宽松口径必通过（放宽只增不减）
[3] 判别力：[2] 不是恒真 —— 把宽松段换成严格段源码后，放宽面必须归零
[4] 放宽面非空：存在「严格拒 / 宽松收」的真实组合（改动确实生效）

重构 ㈠ 段时本用例必红，属预期 —— 请同步更新抽取标记与断言。
"""
import io
import itertools
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_PATH = os.path.join(ROOT, "BuySellPoint", "BSPointList.py")

M_LOOSE_START = "# ㈠ 闪电走势判定（回调笔极值均不越过笔1）—— 宽松口径"
M_LOOSE_END = "# ── 严格口径（已停用，保留备查）"
M_STRICT_HEAD = "        # for j in range(1, len(odd_bis)):"
M_STRICT_TAIL = "        #         return"

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


def load_source():
    with io.open(SRC_PATH, encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def extract_loose(text):
    """抽取宽松段真实源码（未注释区）"""
    s = text.index(M_LOOSE_START)
    e = text.index(M_LOOSE_END)
    block = text[s:e].rstrip()
    assert "for cur in odd_bis[1:]:" in block, "宽松段抽取失败"
    assert "for j in range" not in block, "宽松段混入严格段"
    return block


def extract_strict_from_comments(text):
    """从注释形态里还原严格段源码 —— 同时钉住「原实现以注释保留」这一契约"""
    s = text.index(M_STRICT_HEAD)
    e = text.index(M_STRICT_TAIL, s) + len(M_STRICT_TAIL)
    lines = []
    for ln in text[s:e].split("\n"):
        if not ln.startswith("        #"):
            raise AssertionError("严格段注释形态变化: %r" % ln)
        rest = ln[8:]
        if rest.startswith("# "):
            rest = rest[2:]
        elif rest.startswith("#"):
            rest = rest[1:]
        lines.append("        " + rest)
    return "\n".join(lines)


def build_judge(block, fname):
    """把 8 空格方法体缩进的源码段包成模块级函数（整体减 4 缩进）"""
    body = "\n".join(ln[4:] if ln.startswith("    ") else ln for ln in block.split("\n"))
    src = "def %s(self, odd_bis, stroke_n, stroke_1):\n%s\n    return True\n" % (fname, body)
    ns = {}
    exec(compile(src, "<%s>" % fname, "exec"), ns)
    return ns[fname]


class BiStub(object):
    """笔桩：high / low 取同一极值（本判据只看一个方向）"""

    def __init__(self, ext, idx, is_down=False):
        self._ext = ext
        self.idx = idx
        self._d = is_down

    def _high(self):
        return self._ext

    def _low(self):
        return self._ext

    def is_down(self):
        return self._d


class CfgStub(object):
    def _dbg_bs0(self, *a, **k):
        pass


POOL = [1.0, 2.0, 3.0, 4.0]


def enumerate_cases(loose, strict):
    """返回 (总数, 严格通过数, 宽松通过数, 两者都拒数, 严格拒/宽松收列表, 严格收/宽松拒列表)"""
    both_ok = s_only_ok = l_only_ok = both_reject = 0
    loose_wins, strict_only = [], []
    me = CfgStub()
    for n_odd in (3, 4):            # 笔5 → 3 根回调笔；笔7 → 4 根
        for combo in itertools.product(POOL, repeat=n_odd):
            for is_down in (True, False):
                bis = [BiStub(e, i) for i, e in enumerate(combo)]
                stroke_n = BiStub(combo[-1], n_odd - 1, is_down)
                s_ok = bool(strict(me, bis, stroke_n, bis[0]))
                l_ok = bool(loose(me, bis, stroke_n, bis[0]))
                if s_ok and l_ok:
                    both_ok += 1
                elif s_ok and not l_ok:
                    strict_only.append((n_odd, is_down, combo))
                elif l_ok and not s_ok:
                    l_only_ok += 1
                    loose_wins.append((n_odd, is_down, combo))
                else:
                    both_reject += 1
    total = sum(len(POOL) ** n * 2 for n in (3, 4))
    return total, both_ok, both_ok + l_only_ok, both_reject, loose_wins, strict_only


def main():
    text = load_source()
    loose_src = extract_loose(text)
    strict_src = extract_strict_from_comments(text)

    print("=== [1] 结构契约 ===")
    check("宽松段在未注释区（for cur in odd_bis[1:]）",
          "for cur in odd_bis[1:]" in text.split(M_LOOSE_END)[0], True)
    check("docstring ㈠ 已同步为宽松口径",
          "㈠ 闪电走势：回调笔极值均不越过笔1" in text, True)
    check("严格段以注释形态保留",
          M_STRICT_HEAD in text, True)
    check("旧「逐级收窄」描述已从 docstring 清零",
          "回调笔极值逐级收窄（买：笔n 高点" in text, False)

    loose = build_judge(loose_src, "judge_loose")
    strict = build_judge(strict_src, "judge_strict")

    total, n_both, n_loose, n_reject, loose_wins, strict_only = enumerate_cases(loose, strict)

    print("\n=== [2] 超集不变量（严格通过 ⇒ 宽松通过）===")
    check("严格收 / 宽松拒 的组合数", len(strict_only), 0)
    print("        枚举组合总数=%d  严格通过=%d  宽松通过=%d  都拒=%d"
          % (total, n_both, n_loose, n_reject))

    print("\n=== [3] 判别力（[2] 与 [4] 非恒真的自证）===")
    _, _, n_loose_fake, _, wins_fake, _ = enumerate_cases(strict, strict)
    check("把宽松段换成严格段后，放宽面归零", len(wins_fake), 0)
    check("此时「宽松通过数」== 「严格通过数」", n_loose_fake, n_both)

    print("\n=== [4] 放宽面非空（改动确实生效）===")
    check("严格拒 / 宽松收 的组合数 > 0", len(loose_wins) > 0, True)
    for n_odd, is_down, combo in loose_wins[:3]:
        print("        样例：笔%d %s  极值链=%s（严格拒，宽松收）"
              % (2 * n_odd - 1, "向下买" if is_down else "向上卖",
                 "→".join("%.1f" % c for c in combo)))

    print("\n===== 0类点 57th 闪电走势判据: %d passed / %d failed =====" % (PASS, FAIL))
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
