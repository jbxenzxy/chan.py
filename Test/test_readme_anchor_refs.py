"""Trading/README.md「行号 → 锚点」护栏（2026-09-29）。

钉四件事：
  ① README 里**不许再有行号引用**（`文件.py:NNN` 或裸 `:NNN`）—— 行号是长期文档里
     最易腐坏又最难被发现的资产，代码一插删就整片漂（本次改造前 174 处引用，
     两个 commit 就漂了 20 处）；
  ② 写进 README 的锚点**真的能在源码里 grep 到** —— 错锚点比行号更糟：行号至少
     曾经对过，错的锚点从一开始就指不到地方；
  ③ 反引号配对不被写坏（奇数行数量 = 基线）—— 引用未必在反引号内，替换时漏补
     会留下孤儿反引号，Markdown 行内代码直接断掉；
  ④ 表格结构未被改动（竖线总数 = 基线）—— 挡住"替换时把 `|` 吃进引用里"。

用法：python Test/test_readme_anchor_refs.py（退出码 0 = 通过）
"""
import io
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
README = "Trading/README.md"
BT = chr(96)

# 基线（2026-09-29 锚点化改造后实测）：新增代码围栏 / 表格行需同步这两个数
ODD_BT_BASELINE = 16
PIPE_BASELINE = 542

PAT_FULL = re.compile(r"([A-Za-z_][\w/]*\.py):(\d+)(?:-(\d+))?")
PAT_BARE = re.compile(r"`:\d+(?:-\d+)?`")
PAT_PAIR = re.compile(r"`([A-Za-z_][\w/]*\.py)`\s+`([A-Za-z_][\w]{2,})`")

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


def resolve(rel):
    """README 里的文件名可能是 `Config.py` / `Trading/Config.py` / `Engine/Engine.py`
    —— 统一解析到磁盘真实路径（基准目录 Trading/）。"""
    rel = rel.replace("\\", "/")
    for c in (os.path.join(ROOT, "Trading", rel), os.path.join(ROOT, rel)):
        if os.path.isfile(c):
            return c
    base = os.path.basename(rel)
    for dp, dn, fn in os.walk(os.path.join(ROOT, "Trading")):
        if base in fn:
            return os.path.join(dp, base)
    return None


_src_cache = {}


def src_of(rel):
    if rel not in _src_cache:
        p = resolve(rel)
        try:
            _src_cache[rel] = io.open(p, encoding="utf-8").read() if p else ""
        except OSError:
            _src_cache[rel] = ""
    return _src_cache[rel]


print("\n[1] README 零行号引用")
lines = read(README).splitlines()
check("\u2460 无 `文件.py:行号` 引用",
      len([l for l in lines if PAT_FULL.search(l)]), 0)
check("\u2460 无裸行号引用（`:123`）",
      len([l for l in lines if PAT_BARE.search(l)]), 0)

print("\n[2] Markdown 结构未被写坏")
check("\u2462 反引号奇数行 = 基线 %d（配对完好）" % ODD_BT_BASELINE,
      len([l for l in lines if l.count(BT) % 2]), ODD_BT_BASELINE)
check("\u2463 表格竖线总数 = 基线 %d（结构未破）" % PIPE_BASELINE,
      sum(l.count("|") for l in lines), PIPE_BASELINE)

print("\n[3] 锚点真的能 grep 到（错锚点比行号更糟）")
pairs = []
for l in lines:
    pairs.extend(PAT_PAIR.findall(l))
bad = [(rel, an) for rel, an in pairs if an not in src_of(rel)]
check("\u2461 全部 `文件.py` `锚点` 配对都能在源码里找到", bad, [])
check("\u2461 配对数量不低于改造后基线 90（防整片删空）",
      len(pairs) >= 90, True)
_unresolvable = sorted({rel for rel, _ in pairs if not src_of(rel)})
check("\u2461 配对里的文件都能解析到磁盘", _unresolvable, [])

print("\n" + "=" * 60)
print("readme_anchor_refs: {} passed, {} failed".format(_passed, _failed))
print("=" * 60)
sys.exit(1 if _failed else 0)
