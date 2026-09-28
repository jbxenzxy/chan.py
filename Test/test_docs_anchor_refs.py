"""交接文档「行号 → 锚点」护栏（2026-09-28）。

钉三件事：
  ① 交接文档里**不许再有行号引用**（`文件:行号` 或裸 `:行号`）—— 行号会随任何一次
     插入/删除漂移，实测本次登录链路改造就让 `Frontend/app.js` 的止盈止损一批统一
     后移 24 行（文档写 8554，实际已 8578）；
  ② 反引号配对不被写坏（奇数行数量与基线一致）—— 上一版脚本删空引用时留下过
     孤儿反引号，Markdown 行内代码直接断掉；
  ③ 写进文档的锚点**真的能在源码里 grep 到** —— 否则锚点比行号更糟：行号至少
     曾经对过，错的锚点从一开始就指不到地方。

外加 `.env.example` 的两条：非注释行必须是 `TRADING_` 前缀（TradingConfig 是
extra="forbid"，裸键会让网关子进程启动即崩）；且"界面可选链路"这段说明不许被删。
"""
import io
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DOC = "Docs/股票页「止盈止损」功能_设计兼交接文档_v1.7.md"
ENV_EXAMPLE = ".env.example"
BT = chr(96)

_passed = 0
_failed = 0


def check(name, got, want):
    global _passed, _failed
    if got == want:
        _passed += 1
        print("  \u2713 %s" % name)
    else:
        _failed += 1
        print("  \u2717 %s -> got %r, want %r" % (name, got, want))


def read(rel):
    return io.open(os.path.join(ROOT, rel), encoding="utf-8").read()


print("\n[1] 交接文档：零行号引用")
doc_lines = read(DOC).splitlines()
lineno_refs = [l for l in doc_lines
               if re.search(r"[A-Za-z0-9_/.\-]+\.(?:js|html|py|css):\d", l)]
check("\u2460 无 `文件:行号` 引用", len(lineno_refs), 0)
check("\u2460 无裸行号引用（`:123`）",
      len([l for l in doc_lines if re.search(r"`:\d", l)]), 0)
# 台账行（版本变更表）里保留行号是刻意的 —— 它记录的是"当时发生了什么"
check("\u2460 行号仅留在 §0.0 版本台账（该行描述的就是行号迁移）",
      len([l for l in doc_lines if "行号" in l and re.search(r":\d", l)]) <= 2, True)

print("\n[2] Markdown 结构未被写坏")
# 基线：文档里 ``` 代码围栏共 10 行（反引号计数为奇数）。新增围栏需同步这个值，
# 但任何"删了一半的引用"都会让它变大 —— 这正是要挡的。
odd = len([l for l in doc_lines if l.count(BT) % 2])
check("\u2461 反引号奇数行 = 基线 10（配对完好）", odd, 10)
# 表格列数：972 = 改造前的列数（改造只动反引号串内部，理论上列数不变）。
# 写死是为了拦住"替换时把 `|` 吃进引用里"这类结构破坏；新增/删除表格行时同步此值。
check("\u2461 表格列数总量未被改动",
      sum(l.count("|") for l in doc_lines), 972)

print("\n[3] 锚点真的能 grep 到（错锚点比行号更糟）")
ANCHORS = [
    ("Frontend/app.js", "calcRunSegments"),
    ("Frontend/app.js", "drawRunSegments"),
    ("Frontend/app.js", "drawTpslLines"),
    ("Frontend/app.js", "tpslSegLabel"),
    ("Frontend/app.js", "_tpslTypeHit"),
    ("Frontend/app.js", "_isMirrorMode"),
    ("Frontend/app.html", "annotation-menu-tpsl"),
    ("App/AppTPSL.py", "_STOCK_EXIT_OVERRIDES"),
    ("App/AppTPSL.py", "compute_stock_tpsl"),
    ("Test/test_phase3_guards.py", "ROUTES_SNAPSHOT"),
    ("FrontAPI.py", "api_stocks_save_annotation"),
    ("App/AppChart.py", "call_stock_tpsl"),
    ("Trading/Broker/SimNow.py", "_traded_price_from_records"),
    ("Trading/Engine/Engine.py", "_run_start"),
    ("Trading/Engine/Engine.py", "_settle_positions"),
    ("Trading/Strategy/Exit.py", "_phase_reason"),
    ("Trading/Config.py", "trailing_trigger_r"),
    ("Trading/Config.py", "breakeven_trigger_r"),
]
_src_cache = {}


def src_of(rel):
    if rel not in _src_cache:
        try:
            _src_cache[rel] = io.open(os.path.join(ROOT, rel), encoding="utf-8").read()
        except OSError:
            _src_cache[rel] = ""
    return _src_cache[rel]


missing = [a for rel, a in ANCHORS if a not in src_of(rel)]
check("\u2462 全部锚点都能在对应源码里找到", missing, [])
# 文档里凡写「（见 X）」的，X 都必须在文档提到的某个文件里存在 —— 兜住手改锚点写错
see = re.findall(r"（见 ([A-Za-z_$][\w$.]{3,})", "\n".join(doc_lines))
orphan = [s for s in set(see)
          if not any(s.split("(")[0] in src_of(rel) for rel in {r for r, _ in ANCHORS})]
check("\u2462 文档「（见 X）」里的 X 无孤儿", sorted(orphan), [])

print("\n[4] .env.example")
env = read(ENV_EXAMPLE).splitlines()
active = [l for l in env if l.strip() and not l.lstrip().startswith("#")]
# 本文件前半段是 App 层配置（HOST / PORT / TDX_INSTALL_DIR 由 AppConfig 读，
# extra="ignore"），不受交易网关约束 —— 真正会炸的是**凭据裸键**：
# 网关 TradingConfig 是 extra="forbid"，任何非 TRADING_ 键都会让它启动即崩。
# 凭据（SN_/TQ_/LIVE_）更不该出现：这个文件进 git。
cred = [l for l in active
        if re.match(r"(SN|TQ|LIVE)_(ACCOUNT|PASSWORD)\s*=", l)]
check("\u2463 无凭据裸键（SN_/TQ_/LIVE_，本文件进 git）", cred, [])
trade_bad = [l for l in active
             if not l.split("=")[0].startswith(("TRADING_", "HOST", "PORT",
                                                "TDX_INSTALL_DIR", "CHAN_PATH",
                                                "SCAN_POOL_WORKERS", "SYMBOL_CODE",
                                                "BI_", "SEG_", "ZS_", "TRIGGER_STEP",
                                                "MEAN_METRICS", "TREND_METRICS",
                                                "THS_COOKIE", "SCAN_", "VIEW_COUNT",
                                                "DUAL_", "MAX_", "STOCKS_LOOKBACK",
                                                "FUTURES_LOOKBACK", "FULL_DATA_MODE"))]
check("\u2463 无非白名单裸键（extra=forbid 会拒绝未知键）", trade_bad, [])
# 检测串要具体到"只有这段说明会说的话"：用"自动下单"会命中第 5 段标题，
# 说明被整段删掉也照样绿（变异验证时就是这么漏过去的）。
check("\u2463 说明了界面可选链路（切换不用改 .env）",
      any("不用改这个文件" in l or "登录方式" in l for l in env), True)
check("\u2463 说明了实盘两个前置的用途",
      any("实盘选项" in l or "能不能选" in l for l in env), True)

print("\n" + "=" * 60)
print("docs_anchor_refs: {} passed, {} failed".format(_passed, _failed))
print("=" * 60)
sys.exit(1 if _failed else 0)
