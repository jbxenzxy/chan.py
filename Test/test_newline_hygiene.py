"""换行卫生护栏（2026-10-01）。

钉两件事，都来自同一类事故：某个脚本「读 CRLF（newline="" 保留）+ 默认文本写回」
会把已经是 CRLF 的内容再翻一次，行尾变成 \\r\\r\\n。Python 把孤立的 \\r 也当行终止符，
于是整份文件在 import 时直接 `SyntaxError: expected ':'`（实测 SimNow.py 2039 处双写
就是这个报错）。

  ① 仓库内 .py 不得出现 \\r\\r\\n；
  ② 同一份 .py 不得 LF / CRLF 混用（混用说明有工具只改了一半）；
  ③ 端到端兜底：每个 .py 都能被 compile() 通过 —— 双写最终表现为语法错，
     这条直接盯症状，不依赖前两条的判据写对了没有。

为什么钉在仓库里而不是只钉在交付脚本里：交付脚本只管「包对不对」，管不到
「谁在什么时候把工作树写坏了」；而一旦写坏的文件被提交，后面每一次门禁、
每一次交付都会带着它。放在门禁里，写坏的那一刻就红。
"""
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

print("\n扫描 .py 文件数 = %d" % len(files))
print("=" * 60)
print("newline_hygiene: {} passed, {} failed".format(_passed, _failed))
print("=" * 60)
sys.exit(1 if _failed else 0)
