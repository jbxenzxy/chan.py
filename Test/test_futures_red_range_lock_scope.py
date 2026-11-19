# -*- coding: utf-8 -*-
"""期货红框/区间套读侧的「临界区范围」守护（P3-2，2026-11-19）

背景（评审 P3-2 的实质）：
  期货下窗缓存 `_futures_analysis_cache` 里存的是**活着的 CChan**。SSE 写侧
  每根 K 线 `_drain_chan` 会推进结构，其中增量路径
  `trigger_load → 末笔 CBi.update_new_end` **就地改写 __end_klc**，随后
  `clean_cache()` 清空该笔的 `_memoize_cache`（派生量 _high/_low/amp 全走它）。

  所以「锁内只做 list(bi_list) 浅拷贝、出锁再算」是**不够的**：
  锁只保证列表长度固定，不保证这些活对象在出锁后不被改写。一次请求里
  若先取 `pk.amp()`（入口判据）再取 `_high()/_low()`（中枢构造），中间被
  写侧插进来推进末笔，两次取值就来自**不同代**，出现
  `begin_val + amp != end_val` 的口径撕裂 ⇒ 构造出实际不成立的中枢，
  且同输入不同输出。前端 `updateDualNewZs` 又按
  `(subFreq, leftDate, rightDate)` 去重、同框不重算 ⇒ 错值会**静默停留**
  到用户重画红框（不是「下一帧自愈」）。

  判定路径（`check_nesting_divergence`）比显示路径更要紧：它的结果直接决定
  bsp 是否放行。

钉死的不变式（① ~ ④ 静态、⑤ 行为）：
  ① AppChart.compute_red_range_zs 的期货分支：`_red_range_bi_sequence` /
     `_red_range_amp` 必须全部在 `with futures_sub_chan_guarded_by_key(...)` 内，
     该分支里不许有锁外调用；
  ② BSPointList.check_nesting_divergence 的期货分支：`self._evaluate_nesting`
     必须在同一把对象图锁内调用（股票分支在锁外调用是**设计**，不在此列）；
  ③ `_evaluate_nesting` 自身不许再取锁（避免与写侧成环 / 重入）；
  ④ 区间套判定主体在文件里只有一份（防搬移时留下旧副本双跑）；
  ⑤ 行为：假 CBi 访问器在「锁外被读」时抛错，跑期货 `compute_red_range_zs`
     必须成功 —— 成功即证明遍历与计算确实发生在锁内；并自检该探针**真能**
     在锁外触发（防「探针失效 ⇒ 恒绿」）。

用法：python Test/test_futures_red_range_lock_scope.py（退出码 0 = 通过）
"""
import ast
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

_results = []


def rec(tag, desc, ok, detail=""):
    _results.append((tag, ok))
    print(f"  {'✓' if ok else '✗'} {tag} {desc}")
    if detail and not ok:
        print(f"      └─ {detail}")


# ── AST 小工具 ─────────────────────────────────────────────────────
def _parse(rel):
    with open(os.path.join(_ROOT, rel), encoding="utf-8") as f:
        return ast.parse(f.read())


def _call_name(node):
    """Call 节点的被调名：`a.b()` → "b"，`b()` → "b"。"""
    fn = node.func
    return getattr(fn, "attr", None) or getattr(fn, "id", None) or ""


def _calls(node):
    return [_call_name(n) for n in ast.walk(node) if isinstance(n, ast.Call)]


def _guard_withs(node, attr="futures_sub_chan_guarded_by_key"):
    """子树里所有 `with <attr>(...)` 节点。"""
    out = []
    for w in ast.walk(node):
        if isinstance(w, ast.With):
            for item in w.items:
                ce = item.context_expr
                if isinstance(ce, ast.Call) and _call_name(ce) == attr:
                    out.append(w)
    return out


def _find_func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    return None


def _kq_branch(func):
    """`if normalized_code.startswith("KQ.")` 的 body（期货分支）。"""
    for n in ast.walk(func):
        if isinstance(n, ast.If) and isinstance(n.test, ast.Call):
            t = n.test
            if _call_name(t) == "startswith" and t.args:
                a0 = t.args[0]
                if isinstance(a0, ast.Constant) and a0.value == "KQ.":
                    return n
    return None


# ── ① AppChart 期货分支：计算全在锁内 ─────────────────────────────
def test_appchart_scope():
    tree = _parse(os.path.join("App", "AppChart.py"))
    func = _find_func(tree, "compute_red_range_zs")
    rec("①a", "找到 compute_red_range_zs", func is not None)
    if func is None:
        return

    branch = _kq_branch(func)
    rec("①b", "找到期货分支 `startswith(\"KQ.\")`", branch is not None)
    if branch is None:
        return

    guards = _guard_withs(branch)
    rec("①c", "期货分支内有且仅有 1 个 futures_sub_chan_guarded_by_key",
        len(guards) == 1, f"实际 {len(guards)} 个")
    if len(guards) != 1:
        return

    in_guard = set(_calls(guards[0]))
    need = {"_red_range_bi_sequence", "_red_range_amp"}
    rec("①d", "两个红框计算函数都在锁内被调用",
        need <= in_guard, f"锁内缺 {sorted(need - in_guard)}")

    outside = set()
    for st in branch.body:
        if st is guards[0]:
            continue
        outside |= set(_calls(st)) - in_guard
    stray = need & outside
    rec("①e", "期货分支内不存在锁外的红框计算调用",
        not stray, f"锁外仍有 {sorted(stray)}")


# ── ② BSPointList 期货分支：判定主体在锁内 ────────────────────────
def test_bsp_scope():
    tree = _parse(os.path.join("BuySellPoint", "BSPointList.py"))
    func = _find_func(tree, "check_nesting_divergence")
    rec("②a", "找到 check_nesting_divergence", func is not None)
    if func is None:
        return

    guards = _guard_withs(func)
    rec("②b", "有且仅有 1 个期货对象图锁（股票走 stocks_sub_chan_guarded）",
        len(guards) == 1, f"实际 {len(guards)} 个")
    if len(guards) != 1:
        return

    in_guard = _calls(guards[0])
    rec("②c", "锁内调用了 self._evaluate_nesting",
        "_evaluate_nesting" in in_guard)

    n_total = _calls(func).count("_evaluate_nesting")
    rec("②d", "全函数共 2 处调用：1 处锁内（期货）+ 1 处锁外（股票）",
        n_total == 2, f"实际 {n_total} 处")

    # 期货分支（else:）里不得再有锁外调用
    else_body = []
    for n in ast.walk(func):
        if isinstance(n, ast.If):
            test_src = ast.dump(n.test)
            if "is_stocks" in test_src:
                else_body = n.orelse
                break
    outside = []
    for st in else_body:
        if st is guards[0]:
            continue
        outside += [c for c in _calls(st) if c == "_evaluate_nesting"]
    rec("②e", "期货分支（else）内无锁外的判定调用", not outside,
        f"锁外调用 {len(outside)} 处")


# ── ③ 判定主体自身不取锁 ─────────────────────────────────────────
def test_eval_no_lock():
    tree = _parse(os.path.join("BuySellPoint", "BSPointList.py"))
    func = _find_func(tree, "_evaluate_nesting")
    rec("③a", "找到 _evaluate_nesting", func is not None)
    if func is None:
        return

    withs = [w for w in ast.walk(func) if isinstance(w, ast.With)]
    locks = [c for c in _calls(func) if c.endswith(("_guarded", "_guarded_by_key", "acquire"))]
    rec("③b", "主体内无 with / 无加锁调用（保持纯计算，不成环）",
        not withs and not locks, f"with={len(withs)} lock_calls={locks}")


# ── ④ 判定主体只有一份（防搬移留旧副本） ─────────────────────────
def test_single_copy():
    path = os.path.join(_ROOT, "BuySellPoint", "BSPointList.py")
    with open(path, encoding="utf-8") as f:
        src = f.read()
    for probe in ("shoulder_result = _main_bi_range", "main_bi = bi_list[-1]"):
        n = src.count(probe)
        rec("④", f"`{probe}` 在文件内唯一（残留副本 {n - 1}）", n == 1, f"出现 {n} 次")


# ── ⑤ 行为：锁外读活 CBi 会炸，期货红框计算必须全程在锁内 ─────────
class _State:
    holding = False


class _FakeGuard:
    def __init__(self, state, chan):
        self._s = state
        self._chan = chan

    def __enter__(self):
        self._s.holding = True
        return self._chan

    def __exit__(self, *exc):
        self._s.holding = False
        return False


class _FakeAppData:
    def __init__(self, state, chan):
        self._s = state
        self._chan = chan

    def futures_sub_chan_guarded_by_key(self, key):
        return _FakeGuard(self._s, self._chan)


class _FakeKLList:
    def __init__(self, bis):
        self.bi_list = bis


class _FakeChan:
    """CChan 替身：`chan[kl_type]` → CKLine_List 替身。"""

    def __init__(self, kl_list):
        self._kl = kl_list

    def __getitem__(self, kl_type):
        return self._kl


def test_behavior_scope():
    from App import AppChart

    state = _State()
    chan = _FakeChan(_FakeKLList([object(), object(), object()]))
    real_app_data, real_seq, real_amp = (
        AppChart.app_data, AppChart._red_range_bi_sequence, AppChart._red_range_amp)

    def guarded(fn, name):
        def wrapper(*a, **kw):
            if not state.holding:
                raise AssertionError(f"{name} 在锁外被调用（活 CBi 会被跨代读取）")
            return fn(*a, **kw)
        return wrapper

    try:
        AppChart.app_data = _FakeAppData(state, chan)
        AppChart._red_range_bi_sequence = guarded(lambda *a, **k: (0, 2), "_red_range_bi_sequence")
        AppChart._red_range_amp = guarded(lambda *a, **k: [], "_red_range_amp")

        # 自检：探针在锁外必须炸，否则本用例恒绿（无效测试）
        tripped = False
        try:
            AppChart._red_range_amp([], [], "%Y")
        except AssertionError:
            tripped = True
        rec("⑤a", "探针自检：锁外调用必然抛错（防恒绿）", tripped)

        try:
            got = AppChart.compute_red_range_zs(
                "KQ.m@CFFEX.IF", sub_freq="15s", left_date="a", right_date="b")
        except Exception as exc:  # noqa: BLE001
            # 锁外读到活 CBi ⇒ 探针抛错。这里必须记成「失败」而不是让异常冒泡，
            # 否则用例变成 traceback 而非可读的红项。
            rec("⑤b", "期货 compute_red_range_zs 全程在锁内完成", False,
                f"{type(exc).__name__}: {exc}")
            return
        rec("⑤b", "期货 compute_red_range_zs 全程在锁内完成",
            got == {"zs": [], "start_bi": 0, "end_bi": 2}, repr(got))
        rec("⑤c", "退出后锁标记已复位", state.holding is False)
    finally:
        AppChart.app_data = real_app_data
        AppChart._red_range_bi_sequence = real_seq
        AppChart._red_range_amp = real_amp


def main():
    print("=" * 68)
    print("期货红框/区间套读侧 —— 临界区范围守护（P3-2）")
    print("=" * 68)
    test_appchart_scope()
    test_bsp_scope()
    test_eval_no_lock()
    test_single_copy()
    test_behavior_scope()
    failed = [t for t, ok in _results if not ok]
    print("-" * 68)
    print(f"共 {len(_results)} 项，失败 {len(failed)} 项")
    if failed:
        print("  FAIL:", ", ".join(failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
