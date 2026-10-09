"""长期文档「行号引用」棘轮护栏（2026-09-29）。

为什么需要它（起因）：
  铁律 17 早就写了「长期文档引用代码位置用稳定锚点，不用行号」，但 2026-09-29 那次
  评审里我连着三轮把 `Trading/README.md` 的 174 处行号**刷新了一遍**才被发现 ——
  铁律的摘要随 MEMORY.md 注入了、流程文件也在手边，照样没执行。根因是"流程指令比原则
  更具体、更贴手"，具体指令天然压过原则。所以防线不能是"下次记住"，只能是：
  **写进行号的那一刻，门禁就红**。

钉两件事：
  ① 棘轮（ratchet）：每份长期文档的行号引用数**只许减、不许增**。存量 317 处不强制
     一次改完（改完把基线调到 0 就永久锁死），但**新增任何一处行号引用都会红** ——
     包括新建的文档（不在基线表里 ⇒ 基线为 0 ⇒ 有行号即红）。
  ② 已改造完的 `Trading/README.md` 严格 = 0：它是这次的样板，回潮即红。
  ③ 临时豁免不许长期化：豁免生效的前提是文档里**留着那句临时声明**；声明一旦不在，
     基线必须已经是 0（否则豁免靠删一句话就能无限期续命）。

不纳入：`Docs/WorkBuddy/` —— 那是 ~/.workbuddy 记忆文件在仓库里的一份副本，属个人
  记忆资产而非项目交付文档，不受铁律 17（约束"交付/长期文档"）管辖。

用法：python Test/test_docs_line_refs.py（退出码 0 = 通过）
"""
import io
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))

# 行号引用：完整形式 `Config.py:387` / `app.js:1057-1059`，裸形式 `:404`
#
# 2026-10-10 扩到前端文件（.js/.html/.css）：原正则**只认 .py**，于是
# `Docs/底部指标区改造设计_单窗双槽位.md` 里 51 处 `app.js:NNNN` **完全不计入棘轮**，
# 评审时只能靠人工抽样才发现它们已 100% 漂移（文档冻结于首个提交，其后 app.js 净增
# 353 行）。护栏的口径漏掉整整一类文件 = 护栏对它们**不存在**，必须补上。
#
# 排除 `Docs/`：那是「文档引文档」（如 `Docs/xxx.html:249`），不是引用代码。
PAT_FULL = re.compile(r"(?<![/\w])((?!Docs/)[A-Za-z_][\w/]*\.(?:py|js|html|css)):(\d+)(?:-(\d+))?")
PAT_BARE = re.compile(r"`:\d+(?:-\d+)?`")
# 稳定锚点：`app.js::isFuturesMode` / `app.html::annotation-menu-mirror`
PAT_ANCHOR = re.compile(r"([A-Za-z_][\w/]*\.(?:py|js|html|css))::([A-Za-z_][\w-]*)")

# 基线（2026-09-29 实测）。改造某文档后把对应值调低；调到 0 即永久锁死。
# 2026-09-29 第二轮：5 份 Docs 存量 317 处行号引用已按 README 那套锚点化完毕，
# 基线全部归 0（与 ZERO_FILES 双重锁死，回潮即红）。
#
# 2026-10-05 补登「胜率回测功能」系列设计文档：
#   它是**带版本号的设计文档快照**（每个版本是一份冻结的评审/裁定记录，
#   与新代码的对应关系靠当时那份文档里的坐标才读得懂），与 `nested_divergence_rename`
#   里「`Docs/` 带版本号的历史快照刻意豁免」是同一约定。但它**不该**因此逃出
#   棘轮 —— 故按本文件 [1] 的机制**登记存量实数**（只减不增）：
#   新增一处行号引用即红，要放开必须显式改这里的数字。调到 0 同样永久锁死。
#
#   ⚠ **版本策略 = 仓库只留当前版**（2026-10-05 用户拍板）。换版时：
#     ① 旧版文档从仓库删除；② **同一批**把它在本表里的那行键删掉（棘轮只减不增，
#     删键合法）。两个方向漏了都会红，且都点名到文件 —— 不需要记规则：
#     · 删了文件没删键 → [2]「基线表无失效条目」列出该路径；
#     · 留了文件没登记 → [3]「无基线外文档含行号引用」列出 `(路径, 处数)`。
#   交付包同理：换版后旧版文档**不再随包投递**（v1.18 之前一直带着 v1.14~1.16，
#   与本策略相反 —— 那正是 4 个键悬空的直接原因：包把旧文档送来、仓库又把它们
#   清走，只剩基线表还留着键）。
#
#   v1.14~v1.18 五个键已按本策略于 v1.19 轮删除（v1.17/v1.18 随版本更替出仓，
#   v1.14~v1.16 从未入库）。
BASELINE = {
    "Docs/数据源.md": 0,
    "Docs/多实例自动下单_设计兼交接文档_20260929.md": 55,
    "Docs/互斥锁设计指导书_v1.3.md": 0,
    "Docs/登录链路选择_交付说明_20260928.md": 0,
    "Docs/自动下单功能审核报告_2026-09-17.md": 0,
    "Docs/设计文档_run级配对会计_20260921.md": 0,
    # 2026-10-05 补登 v1.19。带版本号的设计文档快照按**存量实数**登记（只减不增）：
    # v1.19 正文新增的每一处引用都写成**稳定锚点**（函数名 / 常量名 / 字段名 / DOM id），
    # 故本轮**未新增任何行号引用** —— 206 是"照抄上一版存量"，不是"又加了 206 处"。
    #
    # 2026-10-06 换版 v1.19 → v2.0（版本策略 = 仓库只留当前版）：**键随文件改名**。
    # v2.0 那轮只补了 §8.10 后半的披露（函数名锚点，无行号）⇒ 存量实数仍是 206。
    #
    # 2026-10-06 再换版 v2.0 → v2.2（文件名前缀「胜率回测功能」→「股票回测功能」，
    # 版本策略不变：仓库只留当前版，v2.0/v2.1 随合并删除）：**键随文件改名**。
    # v2.2 轮删除 §9 全 A 批跑整节 ⇒ 净减 1 处行号引用（206 → 205），新增引用
    # 全部是稳定锚点（函数名 / 符号名），零行号。
    # 2026-10-09：该文档引《选点&复盘方案》v1.15 的 `:82-83` 行号引用，随方案升 v1.16
    #   改为节锚点（§1「改变 L 的方式」表 C/D 两行）⇒ 净减 1 处（铁律 17：既有行号改锚点、
    #   不许刷新）。基线随之 205 → 204（棘轮只减不增，故必须同步下调以免空出假额度）。
    "Docs/股票回测功能_设计兼交接文档_v2.2.md": 204,
    # 2026-10-09 补登「底部指标区改造（单窗双槽位）」设计文档：同属**带版本号的
    # 设计文档快照**（正文里 174 处 `文件:行号` 是"基线 dd8f754 的坐标"，用来逐项
    # 核改动的落点），按上面的约定**登记存量实数**（只减不增）：新增一处行号引用即红。
    # 该文档随本轮改造入库（提交 27cf891），此前漏登记 ⇒ [3] 一直判红。
    #
    # 2026-10-10 下调 174 → 87：该文档里 51 处 `app.js` 行号**全部漂移**（文档冻结于
    # 首个提交，之后 app.js 净增 353 行），已按铁律 17 改为稳定锚点（`app.js::函数名`，
    # 由 [6] 逐条校验其存在）。**减少的 87 处全是失效坐标**，不是"删掉了有用信息"。
    # 基线必须同步下调，否则棘轮会凭空多出 87 处额度（见文件头 [1] 的说明）。
    # 剩余 87 处是 .py 存量（基线 dd8f754 的坐标），本轮未动。
    "Docs/底部指标区改造设计_单窗双槽位.md": 87,
}

# 已锚点化、不许回潮
ZERO_FILES = [
    "Trading/README.md",
    "Docs/数据源.md",
    "Docs/互斥锁设计指导书_v1.3.md",
    "Docs/登录链路选择_交付说明_20260928.md",
    "Docs/自动下单功能审核报告_2026-09-17.md",
    "Docs/设计文档_run级配对会计_20260921.md",
]

EXCLUDE_DIRS = (".git", "__pycache__", "node_modules", "WorkBuddy")

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


def iter_docs():
    for dp, dn, fn in os.walk(ROOT):
        rel_dir = os.path.relpath(dp, ROOT).replace("\\", "/")
        if any(d in rel_dir.split("/") for d in EXCLUDE_DIRS):
            continue
        for f in sorted(fn):
            if f.endswith(".md"):
                yield os.path.relpath(os.path.join(dp, f), ROOT).replace("\\", "/")


def count(rel):
    t = io.open(os.path.join(ROOT, rel), encoding="utf-8", errors="ignore").read()
    return len(PAT_FULL.findall(t)) + len(PAT_BARE.findall(t))


def resolve_src(fname):
    """`app.js` / `Frontend/app.js` / `App/AppUtils.py` → 仓库内的真实路径"""
    p = os.path.join(ROOT, fname)
    if os.path.isfile(p):
        return p
    base = os.path.basename(fname)
    for dp, dn, fns in os.walk(ROOT):
        rd = os.path.relpath(dp, ROOT).replace("\\", "/")
        if any(x in rd.split("/") for x in EXCLUDE_DIRS):
            continue
        if base in fns:
            return os.path.join(dp, base)
    return None


_src_cache = {}


def src_has(path, sym):
    if path not in _src_cache:
        _src_cache[path] = io.open(path, encoding="utf-8", errors="ignore").read()
    return re.search(r"(?<![A-Za-z0-9_])" + re.escape(sym) + r"(?![A-Za-z0-9_])",
                     _src_cache[path]) is not None


print("\n[1] 棘轮：行号引用数只减不增（存量改造后请把基线调到 0）")
missing = []
for rel in sorted(BASELINE):
    p = os.path.join(ROOT, rel)
    if not os.path.isfile(p):
        missing.append(rel)
        continue
    got = count(rel)
    check("%s 行号引用 %d 处 \u2264 基线 %d" % (rel, got, BASELINE[rel]),
          got <= BASELINE[rel], True)

print("\n[2] 基线表里的文件都真实存在")
# 文档改名 / 移动后 count 取不到值会让 [1] 恒真 = 护栏**静默失效**，必须钉住
check("基线表无失效条目（改名请同步更新基线键）", missing, [])

print("\n[3] 新建文档不许带行号（不在基线表 = 基线 0）")
new_bad = []
for rel in iter_docs():
    if rel in BASELINE or rel in ZERO_FILES:
        continue
    n = count(rel)
    if n:
        new_bad.append((rel, n))
check("无基线外文档含行号引用", new_bad, [])

print("\n[4] 已锚点化的文档不许回潮")
for rel in ZERO_FILES:
    check("%s 行号引用 = 0" % rel, count(rel), 0)

print("\n[6] 锚点校验：`文件::符号` 的符号必须在源码里真实存在")
# 光「数行号个数」抓不到**内容漂移** —— 行号可以一处不动，指向的内容却早换了。
# 这条才是真正对症的检查：锚点写的是函数名 / 常量名 / DOM id，源码里一旦改名或
# 删除，这里立刻红。没写锚点、只写文件名的引用无法校验（也就无法保护），
# 这正是改造时优先写 `app.js::符号` 而不是只写 `app.js` 的原因。
bad_anchor = []
n_anchor = 0
for rel in iter_docs():
    t = io.open(os.path.join(ROOT, rel), encoding="utf-8", errors="ignore").read()
    for m in PAT_ANCHOR.finditer(t):
        n_anchor += 1
        path = resolve_src(m.group(1))
        if path is None:
            bad_anchor.append((rel, m.group(0), "找不到文件 %s" % m.group(1)))
        elif not src_has(path, m.group(2)):
            bad_anchor.append((rel, m.group(0), "符号 %s 不在 %s 里" % (m.group(2), m.group(1))))
check("已登记 %d 个 `文件::符号` 锚点" % n_anchor, n_anchor > 0, True)
check("全部锚点在源码里存在", bad_anchor, [])

print("\n[7] 本轮改造的设计文档：前端行号已清零，不许回潮")
# 底部指标区文档那 51 处 `app.js:NNNN` 100% 漂移，已全部锚点化。钉一条口径更窄的
# 检查：该文档里**任何**指向前端代码的行号都不许再出现（[1] 的棘轮只管总数，
# 总数降下来后仍可能悄悄往回加前端行号 —— 那条它抓不到）。
BOTTOM_DOC = "Docs/底部指标区改造设计_单窗双槽位.md"
if os.path.isfile(os.path.join(ROOT, BOTTOM_DOC)):
    t = io.open(os.path.join(ROOT, BOTTOM_DOC), encoding="utf-8", errors="ignore").read()
    fe = [m.group(0) for m in PAT_FULL.finditer(t) if not m.group(1).endswith(".py")]
    check("%s 前端行号引用 = 0" % BOTTOM_DOC, fe, [])

print("\n[5] 临时豁免不许长期化（评审 P3-7）")
# 多实例文档那 55 处行号是**临时豁免**：§8.4 自陈「实施前坐标快照……实施后一并
# 锚点化，基线降回 0」。方向与铁律 17（行号只减不增）相反 —— 不钉一条，"临时"
# 就会变成永久。判据取**文档自己那句声明**：
#   声明还在  → 豁免仍有效，基线允许 > 0；
#   声明没了  → 要么已实施完（基线就该是 0），要么有人悄悄撤了声明换个说法 ——
#               两种都必须红，否则豁免凭"删一句话"就无限期续命。
TEMPORARY = {
    "Docs/多实例自动下单_设计兼交接文档_20260929.md": "实施前坐标快照",
}
for rel in sorted(TEMPORARY):
    p = os.path.join(ROOT, rel)
    txt = (io.open(p, encoding="utf-8", errors="ignore").read()
           if os.path.isfile(p) else "")
    if TEMPORARY[rel] in txt:
        check("%s 仍有临时豁免声明 → 基线可 > 0（当前 %d）"
              % (rel, BASELINE.get(rel, 0)), BASELINE.get(rel, 0) > 0, True)
    else:
        check("%s 豁免声明已不在 → 基线必须归 0" % rel,
              BASELINE.get(rel, 0), 0)

print("\n" + "=" * 60)
print("docs_line_refs: {} passed, {} failed".format(_passed, _failed))
print("=" * 60)
sys.exit(1 if _failed else 0)
