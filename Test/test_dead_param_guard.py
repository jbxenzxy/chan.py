# -*- coding: utf-8 -*-
"""
Test/test_dead_param_guard.py —— 死参数 `is_reverse` 防回潮护栏
=================================================================
背景：`Bi.Bi.CBi.cal_macd_metric` / `Seg.Seg.CSeg.cal_macd_metric` 曾有第二个
形参 `is_reverse`，其唯一消费者是 AREA_HALF 家族（"半段"面积算法，需按正/反向
柱分别处理）。2026-10-08 AREA_HALF 家族连同 `MACD_ALGO.AREA_HALF` 枚举成员一起
彻底移除后，两处函数体内**再无任何 `is_reverse` 的读取点** ⇒ 形参成为纯死参数；
而 17 处调用点仍机械地写着 `is_reverse=False/True`，读代码的人会误以为它还在
影响计算（实际传 True 与传 False 结果完全一样）。2026-10-09 收窄签名为
`(macd_algo)`，17 处调用点去掉关键字传参。

为什么需要护栏：删完就没人记得了。这类复活的危险在于**静默**——
若有人为"兼容外部调用"把形参加回来并给它默认值，语法检查、类型检查、
运行时全都不报错，只有读者会再次被误导。

判据（AST 静态，不读注释、不信 docstring）：
  ① 两处定义的形参集合必须**恰好**是 `{self, macd_algo}`：
     - 不许出现 `is_reverse`（本护栏主旨）；
     - 也不许把 `macd_algo` 一起删掉 —— 反向锚点，防「清理变破坏」。
  ② 全仓 `.py`（排除 `Backtest/Exp`、`.venv` 等）中所有 `cal_macd_metric`
     调用点不得带 `is_reverse` 关键字。
  ③ 纯文本兜底：全仓 `.py` 不得出现 `is_reverse=` / `, is_reverse` 传参形态。
     AST 只看调用点，管不住 `def foo(is_reverse)` 这类**新定义**，此条补上，
     拦截"照抄旧签名"进测试或新代码的写法（纯注释行不参与判定）。
  ④ 函数体必须真的消费 `macd_algo` —— 证明该函数仍是活的判据入口，
     防有人把它改成恒返回常量后靠"形参都没了"蒙混过关。

自证（变异敏感，两条）：
  ⑤ 把含 `is_reverse` 的**旧签名片段**喂给检测器 → 必须命中（否则是恒绿空壳）；
  ⑥ 把**已清理的正常片段**喂给检测器 → 必须不命中（防检测器写死后恒红）。

运行：python Test/test_dead_param_guard.py
"""
import ast
import io
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)

DEAD_PARAM = "is_reverse"
LIVE_PARAM = "macd_algo"
TARGET_FUNC = "cal_macd_metric"

# 两处定义点（相对仓库根）→ 期望形参集合。
# 写死"恰好这两个"而非"至少"，是因为新增第三个形参同样要在此处显式登记。
DEFINITIONS = (
    ("Bi/Bi.py", {"self", LIVE_PARAM}),
    ("Seg/Seg.py", {"self", LIVE_PARAM}),
)

SKIP_DIRS = {
    "__pycache__", ".git", ".venv", "venv", "node_modules",
    "Image", "Docs", ".workbuddy", ".idea",
}
SKIP_REL_PREFIX = ("Backtest/Exp",)

# 本护栏自身的相对路径：自证片段里含有旧签名样本，扫描时须排除自己
SELF_REL = "Test/test_dead_param_guard.py"


def iter_py_files(root=REPO_ROOT):
    """产出仓库内待扫 .py 的相对路径（POSIX 分隔符）。"""
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace("\\", "/")
            if rel == SELF_REL or rel.startswith(SKIP_REL_PREFIX):
                continue
            yield rel


def read(rel):
    return io.open(os.path.join(REPO_ROOT, rel.replace("/", os.sep)),
                   encoding="utf-8", errors="replace").read()


def func_params(src, func_name=TARGET_FUNC):
    """返回 {函数名: 形参名集合}，只看模块级与类内方法定义。"""
    out = {}
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return out
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            out[node.name] = {a.arg for a in node.args.args}
    return out


def body_reads(src, name, func_name=TARGET_FUNC):
    """函数体内对 `name` 的读取次数（Load 上下文）。"""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return 0
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and node.name == func_name:
            return sum(1 for n in ast.walk(node)
                       if isinstance(n, ast.Name) and n.id == name
                       and isinstance(n.ctx, ast.Load))
    return 0


def find_kwarg_calls(src, func_name=TARGET_FUNC, kwarg=DEAD_PARAM):
    """AST 找出所有带指定关键字的调用点，返回 [(行号, 调用对象文本)]。"""
    hits = []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return hits
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        f = node.func
        nm = f.attr if isinstance(f, ast.Attribute) \
            else (f.id if isinstance(f, ast.Name) else None)
        if nm != func_name:
            continue
        for k in node.keywords:
            if k.arg == kwarg:
                hits.append((node.lineno, nm))
    return hits


def text_hits(src, rel="<memory>"):
    """纯文本兜底：找 `is_reverse=` / `, is_reverse` 形态（纯注释行不算）。"""
    out = []
    for i, line in enumerate(src.splitlines(), 1):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if (DEAD_PARAM + "=") in s or (", " + DEAD_PARAM) in s:
            out.append((i, s[:100]))
    return out


# ─────────────────────────── ① 签名恰好收窄 ───────────────────────────
def test_signature_is_exactly_macd_algo():
    for rel, expected in DEFINITIONS:
        src = read(rel)
        params = func_params(src)
        assert TARGET_FUNC in params, (
            f"{rel} 中找不到 {TARGET_FUNC} 的定义 —— 函数被改名或删除，"
            f"本护栏的锚点失效，须同步更新 DEFINITIONS")
        got = params[TARGET_FUNC]
        assert DEAD_PARAM not in got, (
            f"{rel}::{TARGET_FUNC} 形参中又出现了死参数 {DEAD_PARAM!r} "
            f"（当前形参 {sorted(got)}）—— 该参数已无消费方，不得复活")
        assert got == expected, (
            f"{rel}::{TARGET_FUNC} 形参集合应为 {sorted(expected)}，"
            f"实际 {sorted(got)}")
    print(f"[PASS] ① 两处 {TARGET_FUNC} 形参恰好为 {{self, {LIVE_PARAM}}}，"
          f"{DEAD_PARAM} 未复活")


# ─────────────────────────── ② 调用点不带死参数 ───────────────────────────
def test_no_call_site_passes_dead_param():
    total = 0
    offenders = []
    for rel in iter_py_files():
        hits = find_kwarg_calls(read(rel))
        total += len(hits)
        for ln, nm in hits:
            offenders.append(f"{rel}:{ln} {nm}(...{DEAD_PARAM}=...)")
    assert not offenders, (
        f"仍有 {len(offenders)} 处调用点传死参数 {DEAD_PARAM!r}：\n  "
        + "\n  ".join(offenders))
    assert total == 0, f"调用点命中数应为 0，实际 {total}"
    print(f"[PASS] ② 全仓 {TARGET_FUNC} 调用点均无 {DEAD_PARAM} 关键字传参")


# ─────────────────────────── ③ 纯文本兜底 ───────────────────────────
def test_no_textual_dead_param_usage():
    offenders = []
    for rel in iter_py_files():
        for ln, snippet in text_hits(read(rel), rel):
            offenders.append(f"{rel}:{ln}  {snippet}")
    assert not offenders, (
        f"仍有源码行使用死参数形态 {DEAD_PARAM + '='!r} / {', ' + DEAD_PARAM!r}：\n  "
        + "\n  ".join(offenders)
        + "\n（若是历史说明性注释，请改写措辞避开该形态，不要加白名单）")
    print(f"[PASS] ③ 纯文本兜底：全仓无 {DEAD_PARAM} 传参/新定义形态")


# ─────────────────────────── ④ 函数体仍消费 macd_algo ───────────────────────────
def test_body_still_consumes_macd_algo():
    for rel, _expected in DEFINITIONS:
        n = body_reads(read(rel), LIVE_PARAM)
        assert n >= 1, (
            f"{rel}::{TARGET_FUNC} 函数体内已不读取 {LIVE_PARAM!r} —— "
            f"该函数被改成不依赖入参的常量返回，判据入口已死，本护栏失去意义")
    print(f"[PASS] ④ 两处函数体仍真实消费 {LIVE_PARAM}（判据入口存活）")


# ─────────────────────────── ⑤⑥ 检测器自证 ───────────────────────────
def test_detector_catches_regression():
    """旧签名片段必须被命中 —— 证明检测器不是恒绿空壳。"""
    # 用拼接构造，避免本文件出现 `, is_reverse` 字面量形态干扰 ③
    old_call = f"x.{TARGET_FUNC}(a, {DEAD_PARAM}=True)"
    old_def = f"def {TARGET_FUNC}(self, {LIVE_PARAM}, {DEAD_PARAM}):\n    return 1\n"
    assert find_kwarg_calls(old_call), (
        f"自证失败：含 {DEAD_PARAM} 关键字的调用未被命中 —— 检测器失效")
    params = func_params(old_def)
    assert params and DEAD_PARAM in params[TARGET_FUNC], (
        f"自证失败：含 {DEAD_PARAM} 形参的定义未被识别")
    assert text_hits(old_call), "自证失败：纯文本兜底未命中旧调用形态"
    print(f"[PASS] ⑤ 检测器自证：旧签名片段（调用/定义/文本）三条路径均被命中")


def test_detector_no_false_positive():
    """已清理的正常片段必须不命中 —— 防检测器写死后恒红。"""
    clean_call = f"x.{TARGET_FUNC}({LIVE_PARAM})"
    clean_def = f"def {TARGET_FUNC}(self, {LIVE_PARAM}):\n    return {LIVE_PARAM}\n"
    assert not find_kwarg_calls(clean_call), (
        "自证失败：干净的调用被误报")
    assert DEAD_PARAM not in func_params(clean_def)[TARGET_FUNC], (
        "自证失败：干净的定义被误报")
    assert not text_hits(clean_call), "自证失败：干净的调用被纯文本兜底误报"
    # 历史说明性注释不得被 ③ 误伤（口径：纯注释行不参与判定）
    comment_only = f"# {DEAD_PARAM} 已于 2026-10-09 移除\n"
    assert not text_hits(comment_only), "自证失败：注释行被误判为传参形态"
    print("[PASS] ⑥ 检测器自证：干净片段与说明性注释均不误报")


def main():
    test_signature_is_exactly_macd_algo()
    test_no_call_site_passes_dead_param()
    test_no_textual_dead_param_usage()
    test_body_still_consumes_macd_algo()
    test_detector_catches_regression()
    test_detector_no_false_positive()
    print("ALL 死参数防回潮护栏 TESTS PASS")


if __name__ == "__main__":
    try:
        main()
    except AssertionError as e:
        print(f"[FAIL] {e}")
        sys.exit(1)
