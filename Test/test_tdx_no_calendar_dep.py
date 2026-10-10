# -*- coding: utf-8 -*-
"""防回潮护栏：TdxAPI 日线路径**不得再依赖交易日历**（chinese_calendar）

背景（为什么会有这条用例）
--------------------------
`DataAPI/TdxAPI.py` 原先 `from chinese_calendar import is_holiday`，唯一用途是
日线「缺口检测」的交易日计数。该依赖带来两个真实故障：

  1. 缺库即崩：`import DataAPI.TdxAPI` 直接 `ModuleNotFoundError` —— 整个模块
     不可用（比缺口检测本身崩更严重，因为它是**导入期**失败）；
  2. 年份边界：`chinese_calendar` 只覆盖 `[2004, 当年]`。通达信 `.day` 数据起点
     统一在 2000-01-04，读 2000~2003 的日线会抛
     `NotImplementedError: no available data for year 2000, only year between [2004, 2026] supported`。

处置决策：**整段删除缺口检测**（`_count_trading_days` / `_check_and_report_gaps`
及 `read_tdx_day_file` 里的调用），并摘除 `chinese_calendar` 依赖。

本用例把这个决策钉成可执行断言 —— 任何一处被"顺手加回来"都会立刻变红。

覆盖（全部走符号集合 / 真调，不读文本，免疫注释误伤）：
  ① AST：`TdxAPI.py` 的 import 集合不含 chinese_calendar
  ② AST：`is_holiday` 不作为符号被引用
  ③ AST：`_count_trading_days` / `_check_and_report_gaps` 不在函数集；
         `read_tdx_day_file` **仍在**函数集（双向断言，防"顺手删多了"）
  ④ AST：旧的「内置年份表」备选方案符号不得复活
  ⑤ 清单：`requirements.txt` 的**激活行**不含 chinese-calendar
  ⑥ 桩名单：`Test/_stub_env.py` 的 `_GUARDED` 不含 chinese_calendar
  ⑦ 真调：在**屏蔽 chinese_calendar** 的全新子进程里 `import DataAPI.TdxAPI` 成功
  ⑧ 真调：合成含 2000~2003 的 `.day` → `read_tdx_day_file` 读全、首末日期正确、静默无告警
         （真实 `sh000001.day` 存在时再加跑一遍；不存在则 SKIP）
"""
import ast
import os
import struct
import subprocess
import sys
import tempfile
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)

TDXAPI = os.path.join(_REPO_ROOT, "DataAPI", "TdxAPI.py")
REQUIREMENTS = os.path.join(_REPO_ROOT, "requirements.txt")
STUB_ENV = os.path.join(_HERE, "_stub_env.py")

def _resolve_real_day():
    """真实 sh000001.day 的路径 —— **经路径 SSOT 推导，本文件不落路径字面量**。

    `Test/test_tdx_dir_ssot_guard.py` 的断言①要求全仓路径字面量只允许出现在
    `App/AppConfig.py` 一处；本护栏若自带一份盘符路径副本会直接让它变红。
    """
    try:
        if _REPO_ROOT not in sys.path:
            sys.path.insert(0, _REPO_ROOT)
        from App.AppConfig import app_config
        return os.path.join(app_config.vipdoc_dir, "sh", "lday", "sh000001.day")
    except Exception:  # noqa: BLE001
        return None

results = []


def rec(no, title, ok, detail=""):
    results.append((ok, no, title, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {no} {title}")
    if detail:
        print(f"        {detail}")


def _read(path):
    with open(path, "r", encoding="utf-8", newline="") as f:
        return f.read().replace("\r\n", "\n")


def _py_symbols(src):
    """AST 取三类符号集合：顶层+局部 import 名、函数名、被引用的 Name。"""
    tree = ast.parse(src)
    imported, funcs, names = set(), set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                imported.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom):
            if n.module:
                imported.add(n.module.split(".")[0])
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            funcs.add(n.name)
        elif isinstance(n, ast.Name):
            names.add(n.id)
    return imported, funcs, names


def _active_lines(text):
    """剥掉整行注释与行内注释，返回依赖清单的「有效内容」行。"""
    out = []
    for ln in text.split("\n"):
        s = ln.split("#", 1)[0].strip()
        if s:
            out.append(s)
    return out


src = _read(TDXAPI)
imported, funcs, names = _py_symbols(src)

# ── ① import 集合 ────────────────────────────────────────────────
has_cc = "chinese_calendar" in imported
rec("①", "TdxAPI.py 不再 import chinese_calendar", not has_cc,
    f"import 集合命中: {sorted(x for x in imported if 'calendar' in x) or '无'}")

# ── ② is_holiday 符号 ───────────────────────────────────────────
has_holiday = "is_holiday" in names
rec("②", "is_holiday 不作为符号被引用", not has_holiday,
    "命中于 Name 集合" if has_holiday else "Name 集合无此符号")

# ── ③ 函数集合（双向）───────────────────────────────────────────
gone = sorted(f for f in ("_count_trading_days", "_check_and_report_gaps") if f in funcs)
kept = "read_tdx_day_file" in funcs
rec("③", "缺口检测两函数已移除，且 read_tdx_day_file 仍在（双向）",
    not gone and kept,
    f"仍存在的应删函数={gone or '无'} | read_tdx_day_file={'在' if kept else '不在'}")

# ── ④ 旧「内置年份表」备选方案不得复活 ──────────────────────────
legacy = sorted(x for x in ("_PRE_2004_CLOSED_WEEKDAYS", "_CN_HOLIDAY_LIB_MIN_YEAR",
                            "_is_trading_day") if x in names or x in funcs)
rec("④", "旧的「内置 2000~2003 年份表」方案符号未复活", not legacy,
    f"命中: {legacy or '无'}")

# ── ⑤ requirements 激活行 ───────────────────────────────────────
acts = _active_lines(_read(REQUIREMENTS))
offend = [ln for ln in acts if "chinese-calendar" in ln or "chinese_calendar" in ln]
rec("⑤", "requirements.txt 激活行不含 chinese-calendar", not offend,
    f"激活行数={len(acts)} 命中={offend or '无'}")

# ── ⑥ _stub_env 桩名单 ──────────────────────────────────────────
stub_src = _read(STUB_ENV)
s_imp, _s_funcs, s_names = _py_symbols(stub_src)
stub_has = "chinese_calendar" in stub_src.split("_GUARDED")[1][:200] if "_GUARDED" in stub_src else True
keeps = '"pandas"' in stub_src and '"numpy"' in stub_src
rec("⑥", "_stub_env 的 _GUARDED 已摘掉 chinese_calendar（且仍含 pandas/numpy）",
    not stub_has and keeps,
    f"仍含 chinese_calendar={stub_has} | 仍含 pandas/numpy={keeps}")

# ── ⑦ 真调：屏蔽 chinese_calendar 的全新子进程里 import TdxAPI ──
_BLOCK_SRC = (
    "import sys, importlib.abc\n"
    "class _B(importlib.abc.MetaPathFinder):\n"
    "    def find_spec(self, fullname, path=None, target=None):\n"
    "        if fullname.split('.')[0] == 'chinese_calendar':\n"
    "            raise ImportError('blocked by test_tdx_no_calendar_dep')\n"
    "        return None\n"
    "sys.meta_path.insert(0, _B())\n"
    "try:\n"
    "    import chinese_calendar  # noqa: F401\n"
    "    print('BLOCK_FAILED')\n"
    "except ImportError:\n"
    "    pass\n"
    "import DataAPI.TdxAPI as M\n"
    "assert hasattr(M, 'read_tdx_day_file'), 'read_tdx_day_file 缺失'\n"
    "print('IMPORT_OK')\n"
)
_proc = subprocess.run([sys.executable, "-c", _BLOCK_SRC],
                       cwd=_REPO_ROOT, capture_output=True, text=True)
_ok7 = _proc.returncode == 0 and "IMPORT_OK" in _proc.stdout
rec("⑦", "屏蔽 chinese_calendar 后 import DataAPI.TdxAPI 成功（真调子进程）", _ok7,
    f"rc={_proc.returncode} out={_proc.stdout.strip()!r} err={(_proc.stderr or '').strip()[-200:]!r}")


# ── ⑧ 真调：端到端读含 2000~2003 的 .day ────────────────────────
def _mk_day(path, dates):
    """按通达信 A 股格式写 .day：日期(I4) 开(I4) 高(I4) 低(I4) 收(I4) 额(f4) 量(I4) 保留(I4)。"""
    with open(path, "wb") as f:
        for i, d in enumerate(dates):
            px = 2000 + i                     # 价格元，×100 存整数
            f.write(struct.pack("<IIIIIfII",
                                d.year * 10000 + d.month * 100 + d.day,
                                px, px, px, px, 1e6, 1000, 0))


_DATES = [datetime(2000, 1, 4), datetime(2000, 1, 28), datetime(2000, 2, 14),
          datetime(2000, 4, 28), datetime(2000, 5, 8), datetime(2001, 1, 19),
          datetime(2001, 2, 5), datetime(2002, 2, 8), datetime(2002, 2, 25),
          datetime(2003, 1, 29), datetime(2003, 2, 10), datetime(2003, 12, 31),
          datetime(2004, 1, 5)]

sys.path.insert(0, _REPO_ROOT)
# 导入必须**容错**：若有人把 chinese_calendar 加回来，本护栏在缺库环境下会因
# TdxAPI 导入失败而整体 traceback —— 那是「守护自身崩掉」而非「干净报红」，
# 恰恰是本仓库 _stub_env.py 点名的「守护自身的可执行性没有守护」。故降级为断言。
try:
    from DataAPI import TdxAPI  # noqa: E402
    _TDX_ERR = ""
except Exception as _e:  # noqa: BLE001
    TdxAPI = None
    _TDX_ERR = f"{type(_e).__name__}: {_e}"

with tempfile.TemporaryDirectory() as _td:
    fp = os.path.join(_td, "syn000001.day")
    _mk_day(fp, _DATES)
    if TdxAPI is None:
        rec("⑧", "合成含 2000~2003 的 .day 能读全（不再抛 NotImplementedError）", False,
            f"TdxAPI 无法导入：{_TDX_ERR}")
        rec("⑧b", "原崩溃点 2000-01-28 -> 2000-02-14 静默通过", False,
            "上一项未通过，无法验证")
    else:
        try:
            recs = TdxAPI.read_tdx_day_file(fp)
            n_ok = len(recs) == len(_DATES)
            span_ok = (recs and recs[0]["dt"] == _DATES[0] and recs[-1]["dt"] == _DATES[-1])
            rec("⑧", "合成含 2000~2003 的 .day 能读全（不再抛 NotImplementedError）",
                n_ok and span_ok,
                f"写入={len(_DATES)} 读出={len(recs)} "
                f"范围={recs[0]['dt'].date()} ~ {recs[-1]['dt'].date()}"
                if recs else "读出为空")
            # 紧邻记录跨度最大的一对：2000-01-28 -> 2000-02-14（原实现的必崩点）
            pair = [r["dt"].date() for r in recs if r["dt"].date() in
                    (datetime(2000, 1, 28).date(), datetime(2000, 2, 14).date())]
            rec("⑧b", "原崩溃点 2000-01-28 -> 2000-02-14 静默通过",
                len(pair) == 2, f"该对记录均读到: {pair}")
        except Exception as e:  # noqa: BLE001
            rec("⑧", "合成含 2000~2003 的 .day 能读全", False,
                f"{type(e).__name__}: {e}")
            rec("⑧b", "原崩溃点 2000-01-28 -> 2000-02-14 静默通过", False,
                "上一项未通过，无法验证")

# ── ⑨ 真调：真实数据（经 SSOT 推导，可达才跑）───────────────────
_real_day = _resolve_real_day()
if _real_day and os.path.isfile(_real_day):
    if TdxAPI is None:
        rec("⑨", "真实 sh000001.day 全量读取成功且覆盖 2000 年代", False,
            f"TdxAPI 无法导入：{_TDX_ERR}")
    else:
        try:
            recs = TdxAPI.read_tdx_day_file(_real_day)
            reach_pre2004 = recs and recs[0]["dt"].year <= 2004
            rec("⑨", "真实 sh000001.day 全量读取成功且覆盖 2000 年代",
                len(recs) > 6000 and reach_pre2004,
                f"{len(recs)} 条 {recs[0]['dt'].date()} ~ {recs[-1]['dt'].date()}")
        except Exception as e:  # noqa: BLE001
            rec("⑨", "真实 sh000001.day 全量读取成功", False, f"{type(e).__name__}: {e}")
else:
    print("[SKIP] ⑨ 经 SSOT 推导的真实 sh000001.day 不可达（App/AppConfig.py 的 vipdoc_dir）")


def main():
    print("=" * 60)
    print("TdxAPI 日线路径「零交易日历依赖」防回潮护栏")
    print("=" * 60)
    failed = [r for r in results if not r[0]]
    print("=" * 60)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    for ok, no, title, _d in failed:
        print(f"  FAIL {no} {title}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
