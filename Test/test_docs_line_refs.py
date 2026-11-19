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
  ④ 行号引用的**写法**也要钉全（2026-10-28）：`AppScan:422` 这种无扩展名的
     `模块:行号` 同样是行号引用，一律计入棘轮。上一版正则只认带 `.py/.js` 的
     完整形式，于是 `Docs/数据源.md` 附 A 的 11 处完全不计入，该文档基线写着 0
     还列在 ZERO_FILES 里却照旧漂 —— 与 2026-10-10「只认 .py ⇒ 51 处 app.js
     行号不计入」同类，是「口径漏一整类写法」的第二次出现。
  ⑤ `@ L1064-1069` 这种「at L 行号」写法定要计入（2026-11-19）：
     `Docs/互斥锁设计指导书_v1.3.md` 的 86 处引用**全部**是这个写法，三个正则
     一个都认不出；该文档又摆在 ZERO_FILES「不许回潮」里、基线写 0 ——
     于是护栏对「它有 86 处行号引用」这件事完全无感（实测 10 处抽查全漂，
     其中 2 处指向已删除符号）。与 ④ 同源，是「口径漏一整类写法」的第三次出现。
     结论：**新写法必须先并入 count 口径**，否则「基线 0 + ZERO_FILES」锁住的
     只是一个空集合。（2026-11-19 晚：该文档 85 处已全部锚点化、升版 v1.4，
     现由 ZERO_FILES 锁死；根因见 ⑦ —— 是「约定」允许行号，不只是正则漏写法。）
  ⑥ `文件.py :: 符号` 这种**带空格**的锚点同样计入（2026-11-19）：PAT_ANCHOR 原来
     要求 `::` 紧贴扩展名，于是 `Docs/互斥锁设计指导书_v1.3.md` 里 35 处带空格的
     锚点**一处也没被校验**（`::` 后接空格即失配）——[6] 报「112 个锚点全部存在」，
     实际该文档只被看见 2 个。是同一个病（**写法比正则宽**）的第四次出现，
     故把分隔符放宽成 `\\s*::\\s*`，并顺手修掉因此暴露的 3 处失效锚点。
  ⑦ 「书写约定」本身允许行号 ⇒ 只清存量治不了根（2026-11-19）：⑤⑥ 修完口径后，
     该文档 85 处仍是清一色 `@ L`，因为文首《阅读约定》② 明文写着「需要精确定位到
     某条语句时：文件 :: 符号名 @ L行号 (commit)」——**约定给了行号合法性**。
     门禁只管「数个数」，下一版照样按约定写回来。故批次 2 先改约定（② 改为
     「符号 + 段描述，不写行号」），再清 85 处存量。这是同一病理的第五次出现，
     但层级更高：前四次是「正则漏写法」，这次是「规范漏约束」。

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
# 无扩展名的 `模块:行号`（2026-10-28 补）：`AppScan:422` / `TdxAPI:674` / `App/AppUtils:35`
#
# 上一版 PAT_FULL 要求**带扩展名**（.py/.js/.html/.css），于是 `Docs/数据源.md` 附 A
# 那 11 处 `模块:行号` **完全不计入棘轮**：该文档基线写着 0、还列在 ZERO_FILES
# 「不许回潮」里，护栏却看不见它们（20 passed / 0 failed 全绿，实测 11 处仍在漂）。
# 与 2026-10-10「只认 .py ⇒ 51 处 app.js:NNNN 不计入」是同一类洞的第二次出现 ——
# 护栏口径漏掉一整类写法 = 护栏对它们不存在。
#
# 口径：反引号内 `名字:数字`，且「名字」能**解析到仓库里真实存在的源文件**
# （路径 / 路径+扩展名 / basename 三种试）才算。这样 `min:0`、`max:100`、
# `test_p24:201`（函数名而非文件）不会被误计。
PAT_BARE_MODULE = re.compile(r"`([A-Za-z_][\w/]*):(\d+)(?:-\d+)?`")
_SRC_EXT = (".py", ".js", ".html", ".css")
_src_stems = None

# 「at L」行号（2026-11-19 补）：`AppSSE.py @ L1064-1069` / `` `_drain_chan @ L816` ``
#
# 上面三个正则都要求「文件名 + 冒号」或「反引号内 名字:数字」，于是这类写法
# **一处也认不出**：`Docs/互斥锁设计指导书_v1.3.md` 全篇 86 处用它，而该文档
# 同时躺在 BASELINE（写 0）与 ZERO_FILES 里 ⇒ 护栏判定「零引用、干净」，
# 实际 10 处抽查全部漂移（最多偏 +974 行，2 处指向已删除的符号）。
# 与「只认 .py」「只认带扩展名」同源：**口径漏掉一整类写法 = 护栏对它不存在**。
PAT_AT_L = re.compile(r"@\s*L(\d+)(?:-(\d+))?")


def _src_stem_index():
    """源文件 basename（去扩展名）集合，供无扩展名引用解析。只建一次。"""
    global _src_stems
    if _src_stems is None:
        stems = set()
        for dp, dn, fns in os.walk(ROOT):
            rd = os.path.relpath(dp, ROOT).replace("\\", "/")
            if any(x in rd.split("/") for x in EXCLUDE_DIRS):
                continue
            for f in fns:
                stem, ext = os.path.splitext(f)
                if ext in _SRC_EXT:
                    stems.add(stem)
        _src_stems = stems
    return _src_stems


def _resolves_to_src(name):
    """`AppScan` / `App/AppUtils` 是否指向仓库里真实存在的源文件。"""
    if name.endswith(_SRC_EXT):
        return False            # 带扩展名的已由 PAT_FULL 计过，避免双重计数
    base = name.rsplit("/", 1)[-1]
    if "." in base:
        return False            # `xxx.md` / `xxx.json`：文档引文档或配置，不属本口径
    if os.path.isfile(os.path.join(ROOT, name)):
        return True
    if any(os.path.isfile(os.path.join(ROOT, name + e)) for e in _SRC_EXT):
        return True
    return base in _src_stem_index()


# 稳定锚点：`app.js::isFuturesMode` / `app.html::annotation-menu-id`
# 2026-11-19：分隔符放宽为 `\s*::\s*` —— 原来要求 `::` 紧贴扩展名，于是带空格的写法（`AppData.py :: save_to_zxg_blk`）整类漏校验。
PAT_ANCHOR = re.compile(r"([A-Za-z_][\w/]*\.(?:py|js|html|css))\s*::\s*([A-Za-z_][\w-]*)")

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
    # 2026-11-19 校正 0 → 86：这个键此前写 0 是**错的**，它从来不是「零引用文件」——
    # 全篇 86 处 `@ L` 行号引用一直存在，只是 count 认不出那种写法（见文件头 ⑤）。
    # 登记的是**存量实数**（棘轮只减不增）：新增任何一处 `@ L` / `文件:行号` 即红。
    # 处置方向是铁律 17 的锚点化（`文件::符号`，由 [6] 逐条校验存在性），
    # 每锚点化一批就把这里的数字下调，调到 0 后移进 ZERO_FILES 永久锁死。（86 → 85：修失效锚点时把 `load_pe_ttm_cache` 那处的 `@ L1904-1937` 一并去掉）
    #
    # 2026-11-19 批次 2（85 → 0，**键已删除**）：85 处全部锚点化，文档同时升版
    # v1.3 → v1.4。按**版本策略**（仓库只留当前版）旧版已从仓库删除，故这里必须
    # **同批删键**——留着就是 [2] 的失效条目。新键直接进 ZERO_FILES（= 0 永久锁死），
    # 不再进本表：棘轮表登记的是「还有存量的文件」，已清零的不该占一行。
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
    # 「Docs/互斥锁设计指导书_v1.3.md」2026-11-19 曾移出 ZERO_FILES：它在里面待了
    # 很久，但 count 认不出 `@ L` 写法 ⇒ 「= 0」这条检查恒真，锁了个空集合。
    # 同日晚批次 2 把 85 处全部锚点化、文档升版 v1.4，重新回到本表——这次是真的 0。
    "Docs/互斥锁设计指导书_v1.4.md",
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


def _n_bare_module(t):
    """文本里「无扩展名、且能解析到真实源文件」的 `模块:行号` 处数。"""
    return sum(1 for m in PAT_BARE_MODULE.finditer(t) if _resolves_to_src(m.group(1)))


def _n_at_l(t):
    """文本里 `@ L数字`（可带 `-数字` 范围后缀）的处数。"""
    return len(PAT_AT_L.findall(t))


def _count_text(t):
    """一段文本的行号引用处数（四个口径相加）。纯文本入口，供 [9] 自检直接调用。"""
    return (len(PAT_FULL.findall(t)) + len(PAT_BARE.findall(t))
            + _n_bare_module(t) + _n_at_l(t))


def count(rel):
    t = io.open(os.path.join(ROOT, rel), encoding="utf-8", errors="ignore").read()
    return _count_text(t)


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

print("\n[8] 无扩展名 `模块:行号`：已锚点化的文档必须为 0（2026-10-28 补口径）")
# 这一节就是「口径洞」本身的守卫。`Docs/数据源.md` 附 A 曾有 11 处 `AppScan:422`
# 这种无扩展名写法，旧 PAT_FULL 认不出 ⇒ [1]/[4] 一路绿、文档里 11 处照旧漂。
# 现在两个方向都钉：该文档必须 = 0（口径更窄，防总数降下来后又悄悄加回去）；
# 全库方向由 [1]/[3] 承担（count 已并入无扩展名口径）。
for rel in ("Docs/数据源.md",):
    p = os.path.join(ROOT, rel)
    if os.path.isfile(p):
        check("%s 无扩展名行号引用 = 0" % rel,
              _n_bare_module(io.open(p, encoding="utf-8", errors="ignore").read()), 0)

print("\n[9] `@ L` 形态的护栏自检（2026-11-19 补口径，防再被优化掉）")
# 这一节守的是「口径」本身，而不是某个文档：一旦有人把 _n_at_l 从 count 里摘掉、
# 或把 PAT_AT_L 写窄（例如只认带反引号的形态），下面第一条立即红。
# 用合成样本文本断言，不依赖仓库里当前是否还有这种写法。
_at_l_samples = [
    ("`AppSSE.py @ L1064-1069`", 1),
    ("`_drain_chan @ L816`", 1),
    ("A @ L12，B @ L34-56", 2),
    ("old@L7 无空格也要认", 1),
    ("没有行号引用的普通文本", 0),
]
check("PAT_AT_L 识别各种 `@ L` 形态", [_n_at_l(t) for t, _ in _at_l_samples],
      [n for _, n in _at_l_samples])
# 断言的是**聚合口径**（_count_text），不是 _n_at_l 自己：把 _n_at_l 从聚合里摘掉，
# 这一条立刻红 —— 只测 _n_at_l("x") 是测不到「有没有人被摘掉」的。
check("聚合口径 _count_text 已并入 @L",
      [_count_text(t) for t, _ in _at_l_samples],
      [n for _, n in _at_l_samples])

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
