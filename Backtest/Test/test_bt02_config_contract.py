# -*- coding: utf-8 -*-
"""
缠论配置契约（Backtest/Test/test_bt02_config_contract.py）
==========================================================
把 `Backtest.Runner.default_chan_config()` 与页面侧 `App/AppUtils._make_chan_config()`
钉成**逐字段相等**（设计文档 §6 P0-2 方案 B 的落点）。

为什么需要这条护栏
---------------------------------------------------------------------
回测**禁 import `App/`**（§5.9 R31）⇒ 配置只能自持一份 `CChanConfig()`。
今天两边都是"全默认"，所以相等；但只要哪天有人在 `App/` 侧给它加一条覆盖
（例如页面为了提速打开 `skip_step`、或改 `bi_algo`），回测就会**静默地用另一套
口径**分析 —— 结果数字全变而没有任何报错。

`CChanConfig()` 无 `__eq__` ⇒ 不能直接 `==`：本用例把两边都递归拍平成
可比较结构（原始值 / 枚举名 / 嵌套对象按属性名排序）再比。

覆盖
---------------------------------------------------------------------
  ① 逐字段相等：`default_chan_config()` ≡ `App.AppUtils._make_chan_config()`
  ② `trigger_step is True` —— `CChan.step_load()` 首行 `assert self.conf.trigger_step`，
     为假会在**第一帧**断言失败（不是"跑得慢"，是直接炸）
  ③ `Backtest.Runner.kl_type_of(k)` == `Common.CEnum.FREQ_TO_KL_TYPE[k]`
     对 `FREQ_TABLE` **全量键**成立（含未知键必须抛 `ValueError`）
  ④ `date_fmt_of(k)` 与 `Common.func_util._get_date_fmt(k)` 同源（仅 `/`→`-`）
  ⑤ 与页面**同判据**（条件断言）：`App.AppUtils._get_kl_type(k)` 与
     `kl_type_of(k)` 逐 freq 相等 —— 前提是 `DataAPI.TqSdkAPI.CTqSdkAPI`
     可导入。它不可导入时 `_get_kl_type` 会退化成"一切都是日线"（源码里
     `if CTqSdkAPI else 86400`），那是 App 侧的降级、不是回测的问题 ⇒
     **SKIP 并把原因打出来**，不做假红。

跑法：`python Backtest/Test/test_bt02_config_contract.py`（退出码 0/1 即判决）
"""
import enum
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
BACKTEST_DIR = os.path.dirname(HERE)
ROOT = os.path.dirname(BACKTEST_DIR)
for _p in (ROOT,):
    if _p not in sys.path:
        sys.path.insert(0, _p)

PASS = 0
FAIL = 0
SKIP = 0


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


def skip(name, why):
    global SKIP
    SKIP += 1
    print("[SKIP] %s（%s）" % (name, why))


# ─────────────────────────────────────────────────────────────────────
# 递归拍平：把两个 `CChanConfig` 变成可 `==` 的结构
# ─────────────────────────────────────────────────────────────────────
def snap(o, depth=0):
    if depth > 8:
        return ("DEEP", type(o).__name__)
    if o is None or isinstance(o, (bool, int, float, str)):
        return o
    if isinstance(o, enum.Enum):
        return ("Enum", type(o).__name__, o.name)
    if isinstance(o, dict):
        return {k: snap(v, depth + 1) for k, v in sorted(o.items(), key=lambda kv: str(kv[0]))}
    if isinstance(o, (list, tuple, set)):
        return [snap(v, depth + 1) for v in o]
    d = getattr(o, "__dict__", None)
    if d:
        return ("obj", type(o).__name__,
                {k: snap(v, depth + 1) for k, v in sorted(d.items())})
    return ("raw", type(o).__name__)


def diff(got, want, path="", out=None):
    if out is None:
        out = []
    if len(out) > 30:
        return out
    if isinstance(want, dict) and isinstance(got, dict):
        for k in sorted(set(want) | set(got), key=str):
            if k not in got:
                out.append("%s.%s 缺失（页面有 %r）" % (path, k, want[k]))
            elif k not in want:
                out.append("%s.%s 多出（页面无 %r）" % (path, k, got[k]))
            else:
                diff(got[k], want[k], "%s.%s" % (path, k), out)
    elif isinstance(want, list) and isinstance(got, list):
        if len(got) != len(want):
            out.append("%s 长度 %d ≠ %d" % (path, len(got), len(want)))
        for i in range(min(len(got), len(want))):
            diff(got[i], want[i], "%s[%d]" % (path, i), out)
    elif got != want:
        out.append("%s 回测=%r 页面=%r" % (path, got, want))
    return out


def main():
    print("=" * 68)
    print("缠论配置契约（Backtest/Test/test_bt02_config_contract.py）")
    print("=" * 68)

    from Backtest import Runner

    # ── ① 逐字段相等 ─────────────────────────────────────────────
    try:
        from App.AppUtils import _make_chan_config
        page = _make_chan_config()
    except Exception as e:                                     # noqa: BLE001
        check("① default_chan_config() ≡ App.AppUtils._make_chan_config()", False,
              "无法 import App.AppUtils：%s: %s" % (type(e).__name__, e))
        page = None

    if page is not None:
        a, b = snap(Runner.default_chan_config()), snap(page)
        d = diff(a, b, "chan_config")
        check("① default_chan_config() ≡ App.AppUtils._make_chan_config()（逐字段）",
              not d, "\n".join(d) or "")

    # ── ② trigger_step 必须为真 ──────────────────────────────────
    cfg = Runner.default_chan_config()
    check("② trigger_step is True（step_load 的首行断言）",
          bool(getattr(cfg, "trigger_step", False)),
          "trigger_step=%r" % getattr(cfg, "trigger_step", None))

    # ── ③ kl_type_of 对 FREQ_TABLE 全量键成立 ────────────────────
    from Common.CEnum import FREQ_TABLE, FREQ_TO_KL_TYPE
    bad = []
    for k in FREQ_TABLE:
        try:
            if Runner.kl_type_of(k) != FREQ_TO_KL_TYPE[k]:
                bad.append("%s: %r ≠ %r" % (k, Runner.kl_type_of(k), FREQ_TO_KL_TYPE[k]))
        except Exception as e:                                 # noqa: BLE001
            bad.append("%s: 抛 %s: %s" % (k, type(e).__name__, e))
    check("③ kl_type_of(k) == FREQ_TO_KL_TYPE[k]（FREQ_TABLE 全量 %d 键）"
          % len(FREQ_TABLE), not bad, "\n".join(bad))

    # 未知键必须**抛**，不许静默兜底成日线
    try:
        Runner.kl_type_of("__no_such_freq__")
        raised = False
        detail = "未抛异常（静默兜底 = 回测跑错级别没人知道）"
    except ValueError as e:
        raised = True
        detail = ""
    except Exception as e:                                     # noqa: BLE001
        raised = False
        detail = "抛了 %s（期望 ValueError）：%s" % (type(e).__name__, e)
    check("③b 未知周期抛 ValueError（不静默兜底）", raised, detail)

    # ── ④ date_fmt 与页面同源 ────────────────────────────────────
    from Common.func_util import _get_date_fmt
    bad = []
    for k in FREQ_TABLE:
        want = _get_date_fmt(k).replace("/", "-")
        got = Runner.date_fmt_of(k)
        if got != want:
            bad.append("%s: 回测=%r 页面(去斜杠)=%r" % (k, got, want))
    check("④ date_fmt_of(k) ≡ _get_date_fmt(k) 去斜杠（全量 %d 键）" % len(FREQ_TABLE),
          not bad, "\n".join(bad))

    # ── ⑤ 与页面 _get_kl_type 同判据（条件断言）─────────────────
    try:
        from DataAPI.TqSdkAPI import CTqSdkAPI
    except Exception:                                          # noqa: BLE001
        CTqSdkAPI = None
    if CTqSdkAPI is None:
        skip("⑤ kl_type_of ≡ App.AppUtils._get_kl_type（页面同判据）",
             "DataAPI.TqSdkAPI.CTqSdkAPI 不可导入 ⇒ 页面侧 _get_kl_type 会退化成"
             "一律 K_DAY，属 App 侧降级，不构成回测的口径分歧")
    else:
        from App.AppUtils import _get_kl_type
        bad = []
        for k in FREQ_TABLE:
            a, b = Runner.kl_type_of(k), _get_kl_type(k)
            if a != b:
                bad.append("%s: 回测=%r 页面=%r" % (k, a, b))
        check("⑤ kl_type_of ≡ App.AppUtils._get_kl_type（全量 %d 键）" % len(FREQ_TABLE),
              not bad, "\n".join(bad))

    print("-" * 68)
    print("合计 %d 项，通过 %d，失败 %d，跳过 %d" % (PASS + FAIL + SKIP, PASS, FAIL, SKIP))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
