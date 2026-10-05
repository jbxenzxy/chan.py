"""换行卫生护栏（2026-10-01）。

钉两件事，都来自同一类事故：某个脚本「读 CRLF（newline="" 保留）+ 默认文本写回」
会把已经是 CRLF 的内容再翻一次，行尾变成 \\r\\r\\n。Python 把孤立的 \\r 也当行终止符，
于是整份文件在 import 时直接 `SyntaxError: expected ':'`（实测 SimNow.py 2039 处双写
就是这个报错）。

  ① 仓库内 .py 不得出现 \\r\\r\\n；
  ② 同一份 .py 不得 LF / CRLF 混用（混用说明有工具只改了一半）；
  ③ 端到端兜底：每个 .py 都能被 compile() 通过 —— 双写最终表现为语法错，
     这条直接盯症状，不依赖前两条的判据写对了没有。
  ④ 行尾与仓库约定一致（2026-10-05 补）：`.gitattributes` 里显式钉 `eol=lf`
     的路径走**纯 LF**，其余文本文件走**纯 CRLF**。前三条查的是「写坏」，
     这条查的是「写歪」—— 用 Write 类工具新建的文件默认落 LF，一旦提交就是
     整文件 diff，而 `\\r\\r\\n` / 混用 / 语法错三条**全都不报**（纯 LF 是合法的
     Python 源码）。实测 306 个 .py 里唯一一份纯 LF 就是这么来的。
     **豁免**：0 字节文件（9 个空 `__init__.py`）、**完全不含换行**的单行文件
     （`App/stock_names.json` / `stock_float_mc.json` / `stock_index_belong.json` /
     `last_code_freq.json` —— 运行时由 `json.dump` 直接落盘，本就没有行尾），
     以及 **`.` 前缀的隐藏 / 跑期临时文件**（`Trading/Test/.tmp_p64_probe.js` 这类由
     别的测试生成、跑完自清理的产物：它在门禁里是否被扫到，取决于**谁先跑**）。
     三类少豁免哪一类，护栏都会恒红或随机红。

为什么钉在仓库里而不是只钉在交付脚本里：交付脚本只管「包对不对」，管不到
「谁在什么时候把工作树写坏了」；而一旦写坏的文件被提交，后面每一次门禁、
每一次交付都会带着它。放在门禁里，写坏的那一刻就红。
"""
import fnmatch
import io
import os
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules",
             ".pytest_cache", ".idea"}

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


def iter_py():
    for dp, dn, fn in os.walk(ROOT):
        dn[:] = [d for d in dn if d not in SKIP_DIRS]
        for f in sorted(fn):
            if f.endswith(".py"):
                yield os.path.join(dp, f)


files = list(iter_py())
blobs = {}
for p in files:
    with io.open(p, "rb") as fh:
        blobs[p] = fh.read()

print("\n[1] 无 \\r\\r\\n（CRLF 被重复转换）")
doubled = [os.path.relpath(p, ROOT).replace("\\", "/")
           for p, b in blobs.items() if b.count(b"\r\r\n")]
check("\u2460 \\r\\r\\n 文件数 = 0", sorted(doubled), [])

print("\n[2] 无 LF / CRLF 混用")
mixed = []
for p, b in blobs.items():
    crlf = b.count(b"\r\n")
    bare_lf = b.count(b"\n") - crlf
    if crlf and bare_lf:
        mixed.append(os.path.relpath(p, ROOT).replace("\\", "/"))
check("\u2461 混用文件数 = 0", sorted(mixed), [])

print("\n[3] 每个 .py 都能被 compile() 通过")
broken = []
for p, b in blobs.items():
    try:
        compile(b.decode("utf-8"), p, "exec")
    except Exception as e:                                     # noqa: BLE001
        broken.append("%s: %s" % (os.path.relpath(p, ROOT).replace("\\", "/"),
                                  str(e).splitlines()[-1][:80]))
check("\u2462 编译失败文件数 = 0", sorted(broken), [])

print("\n[4] 行尾与仓库约定一致（.gitattributes 钉 eol=lf 的走 LF，其余走 CRLF）")
ATTR = os.path.join(ROOT, ".gitattributes")
TEXT_EXTS = (".py", ".js", ".html", ".css", ".md", ".json", ".txt")
_lf_pats = []
if os.path.isfile(ATTR):
    with io.open(ATTR, encoding="utf-8") as fh:
        for raw in fh:
            parts = raw.split("#", 1)[0].split()
            if len(parts) >= 3 and parts[-1].lower() == "eol=lf":
                _lf_pats.append(parts[0].replace("\\", "/"))
check("\u2463 .gitattributes 里读到 eol=lf 规则（SSOT 只有 .gitattributes 一份）",
      len(_lf_pats) >= 3, True)

_texts = []
for dp, dn, fn in os.walk(ROOT):
    dn[:] = [d for d in dn if d not in SKIP_DIRS]
    for f in sorted(fn):
        if f.startswith("."):
            continue        # 隐藏 / 跑期临时文件（如 `Trading/Test/.tmp_p64_probe.js`）：不是仓库资产
        if os.path.splitext(f)[1].lower() in TEXT_EXTS:
            p = os.path.join(dp, f)
            _texts.append((os.path.relpath(p, ROOT).replace("\\", "/"), p))
wrong = []
for rel, p in _texts:
    with io.open(p, "rb") as fh:
        b = fh.read()
    if not b or not (b.count(b"\n") or b.count(b"\r")):
        continue                        # 空文件 / 单行不换行（App 的运行时数据 json）：无行尾可言
    crlf = b.count(b"\r\n")
    bare_lf = b.count(b"\n") - crlf
    stray_cr = b.count(b"\r") - crlf
    exp_lf = any(fnmatch.fnmatch(rel, pat) for pat in _lf_pats)
    if exp_lf:
        if not (bare_lf and not crlf and not stray_cr):
            wrong.append("%s 应为纯 LF（crlf=%d lf=%d 孤\\r=%d）"
                         % (rel, crlf, bare_lf, stray_cr))
    elif not (crlf and not bare_lf and not stray_cr):
        wrong.append("%s 应为纯 CRLF（crlf=%d lf=%d 孤\\r=%d）"
                     % (rel, crlf, bare_lf, stray_cr))
check("\u2464 行尾不符合约定的文本文件数 = 0（扫 %d 个，LF 模式 %d 条）"
      % (len(_texts), len(_lf_pats)), sorted(wrong), [])

print("\n扫描 .py 文件数 = %d" % len(files))
print("=" * 60)
print("newline_hygiene: {} passed, {} failed".format(_passed, _failed))
print("=" * 60)
sys.exit(1 if _failed else 0)
