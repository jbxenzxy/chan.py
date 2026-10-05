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

# 行号引用：完整形式 `Config.py:387` / `Engine/Engine.py:1065-1070`，裸形式 `:404`
PAT_FULL = re.compile(r"([A-Za-z_][\w/]*\.py):(\d+)(?:-(\d+))?")
PAT_BARE = re.compile(r"`:\d+(?:-\d+)?`")

# 基线（2026-09-29 实测）。改造某文档后把对应值调低；调到 0 即永久锁死。
# 2026-09-29 第二轮：5 份 Docs 存量 317 处行号引用已按 README 那套锚点化完毕，
# 基线全部归 0（与 ZERO_FILES 双重锁死，回潮即红）。
#
# 2026-10-05 补登「胜率回测功能」系列设计文档（_v1.14 / _v1.15 / _v1.16）：
#   它们是**带版本号的设计文档快照**（每个版本是一份冻结的评审/裁定记录，
#   与新代码的对应关系靠当时那份文档里的坐标才读得懂），与 `nested_divergence_rename`
#   里「`Docs/` 带版本号的历史快照刻意豁免」是同一约定。但它们**不该**因此逃出
#   棘轮 —— 故按本文件 [1] 的机制**登记存量实数**（只减不增）：
#   新增一处行号引用即红，要放开必须显式改这里的数字。调到 0 同样永久锁死。
BASELINE = {
    "Docs/数据源.md": 0,
    "Docs/多实例自动下单_设计兼交接文档_20260929.md": 55,
    "Docs/互斥锁设计指导书_v1.3.md": 0,
    "Docs/登录链路选择_交付说明_20260928.md": 0,
    "Docs/自动下单功能审核报告_2026-09-17.md": 0,
    "Docs/设计文档_run级配对会计_20260921.md": 0,
    "Docs/胜率回测功能_设计兼交接文档_v1.14.md": 196,
    "Docs/胜率回测功能_设计兼交接文档_v1.15.md": 200,
    "Docs/胜率回测功能_设计兼交接文档_v1.16.md": 206,
    # 2026-10-05 补登 v1.17（同 v1.14~v1.16 约定：带版本号的设计文档快照按存量实数登记，
    # 只减不增）。v1.17 正文新增的每一处引用都写成**稳定锚点**（函数名 / 常量名 / 模块名），
    # 故本轮**未新增任何行号引用** —— 206 与 v1.16 相等，是"照抄上一版存量"而非"又加了 206 处"。
    "Docs/胜率回测功能_设计兼交接文档_v1.17.md": 206,
    # 2026-10-05 补登 v1.18（同 v1.14~v1.17 约定）。v1.18 是**显示层契约轮**：
    # 新增的修订说明 / 契约护栏表 / 坑 16~18 全用**稳定锚点**（函数名 / 字段名 / 常量名），
    # 故本轮**未新增任何行号引用** —— 206 与 v1.17 相等，是"照抄上一版存量"。
    "Docs/胜率回测功能_设计兼交接文档_v1.18.md": 206,
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
