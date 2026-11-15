"""源码注释「Test/ 引用存在性」棘轮护栏（2026-11-15，复审 8a1defa §7.2）。

为什么需要它（起因）：
  上一轮（47263e7）`DataAPI/ThsCloudZxgAPI.py` 两处注释引用了从未入过库的
  oracle 测试文件（文件名含 test_ths_market_oracle），活过 23 个提交才被人工评审
  发现。复审（8a1defa）做了反向变异实证：把悬空引用原样注回复制树后，现有 6 个
  最可能拦截它的组件全部 rc=0 —— 代码注释里的悬空引用是护栏真空白，修好靠人工、
  没有防回潮机制。这条护栏把「注释引用的测试文件/符号必须真实存在」变成门禁断言。

判据（口径刻意收窄，宁窄勿误报）：
  ① 只查 `.py` 文件的 **注释与 docstring** 里 `Test` 前缀、至少一层目录、
     `.py` 结尾的仓库相对引用（如 `Test/run_all.py`）。字符串字面量不查 ——
     print/f-string 里的路径是运行期输出，不是给维护者的引用。`Test/` 是
     仓库根锚定、无相对歧义的一层，也是历史上唯一出过悬空事故的引用形态；
     同包相对写法（Infra/Product.py）与第三方库写法（tqsdk/objs.py）口径
     天然模糊，不纳入。
  ② 解析锚点：仓库根 + 引用方文件的各级祖先目录（`Trading/Test/` 下的文件写
     `Test/` 加文件名的引用、指同目录 Test，是本仓惯用写法，`Backtest/Test/`
     同理）。所有锚点都找不到才算悬空。
  ③ 符号配对：引用后紧跟 `::符号` 或「的 `符号`」时，再断言符号在目标文件
     AST 顶层（module 级 Assign / def / class）。
  ④ **棘轮**：存量悬空引用冻结在下方 BASELINE（历史上 Backtest/Trading 重构
     遗留的真实存量），**只许减、不许增** —— 当前违规集超出基线的部分即红。
     往 BASELINE 加条目 = 给新悬空引用开绿灯，必须有评审理由并留档。

用法：python Test/test_comment_ref_guard.py（退出码 0 = 通过）
"""
import ast
import io
import os
import re
import sys
import tokenize

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Test 前缀、至少一层目录的 .py 引用；前导排除 盘符冒号 / URL 斜杠 / 路径中间
PATH_RE = re.compile(
    r"(?<![:(/\w\\-])"
    r"(Test(?:[/\\][\w\-\.]+)+\.py)"
    r"(?![\w\-])"
)
PAIR_COLON_RE = re.compile(r"::([A-Za-z_]\w*)")
PAIR_DE_RE = re.compile(r"\s+的\s*`([A-Za-z_]\w*)`")
SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "Image", "sandbox",
             "node_modules", ".workbuddy", ".idea", ".vscode"}

# 棘轮基线（冻结于 8a1defa，2026-11-15）：历史遗留的注释悬空引用，只许减不许增。
# 条目格式 "引用方文件|kind|引用内容|符号"，kind: path=路径不存在 / symbol=符号不在目标顶层。
# 其余 9 条历史存量经祖先锚点解析后均真实存在（Backtest/Test/、Trading/Test/ 包内目录），不列入。
BASELINE = frozenset({
    "DataAPI/TdxAPI.py|symbol|Test/test_15m_period.py|test_resample_30m_hk_bucket_fix",
})


def _chunks(path):
    """返回 [(line_no, text)]：全部注释行 + docstring 文本。"""
    out = []
    with tokenize.open(path) as f:
        src = f.read()
    for tok in tokenize.generate_tokens(io.StringIO(src).readline):
        if tok.type == tokenize.COMMENT:
            out.append((tok.start[0], tok.string))
    tree = ast.parse(src)
    doc_nodes = []
    if tree.body and isinstance(tree.body[0], ast.Expr) \
            and isinstance(tree.body[0].value, ast.Constant) \
            and isinstance(tree.body[0].value.value, str):
        doc_nodes.append(tree.body[0])
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) \
                    and isinstance(b[0].value, ast.Constant) \
                    and isinstance(b[0].value.value, str):
                doc_nodes.append(b[0])
    for node in doc_nodes:
        out.append((node.lineno, node.value.value.replace("\r\n", "\n")))
    return out


def _top_symbols(py_path):
    with tokenize.open(py_path) as f:
        tree = ast.parse(f.read())
    names = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                for n in ast.walk(t):
                    if isinstance(n, ast.Name):
                        names.add(n.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
    return names


def _resolve(rel_file, ref):
    """仓库根 + 引用方各级祖先目录 逐个试，返回存在的绝对路径或 None。"""
    here = os.path.dirname(os.path.join(ROOT, rel_file))
    anchors = [ROOT]
    while True:
        parent = os.path.dirname(here)
        if parent == here:
            break
        anchors.append(here)
        here = parent
    for a in anchors:
        cand = os.path.join(a, ref.replace("/", os.sep))
        if os.path.isfile(cand):
            return cand
    return None


def main():
    current = {}   # identity -> 展示行（带行号）
    n_files = n_skip = 0
    py_files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            if fn.endswith(".py"):
                py_files.append(os.path.join(dirpath, fn))
    for py in sorted(py_files):
        rel = os.path.relpath(py, ROOT).replace("\\", "/")
        n_files += 1
        try:
            chunks = _chunks(py)
        except Exception:
            n_skip += 1
            continue
        for line_no, text in chunks:
            norm = text.replace("\\", "/")
            for m in PATH_RE.finditer(norm):
                ref = m.group(1).replace("\\", "/")
                target = _resolve(rel, ref)
                if target is None:
                    ident = "%s|path|%s|" % (rel, ref)
                    current.setdefault(ident, "%s:%d 路径不存在: %s"
                                       % (rel, line_no, ref))
                    continue
                tail = norm[m.end():m.end() + 120]
                for rx in (PAIR_COLON_RE, PAIR_DE_RE):
                    pm = rx.search(tail)
                    if pm:
                        s = pm.group(1)
                        if s not in _top_symbols(target):
                            ident = "%s|symbol|%s|%s" % (rel, ref, s)
                            current.setdefault(ident, "%s:%d 符号不在 %s 顶层: %s"
                                               % (rel, line_no, ref, s))
    in_base = {k for k in current if k in BASELINE}
    new = {k: v for k, v in current.items() if k not in BASELINE}
    stale = BASELINE - set(current)
    for v in new.values():
        print("  ✗ [新增悬空引用] " + v)
    for k in sorted(in_base):
        print("  · [基线存量，不阻断] " + current[k])
    print("comment_ref_guard: 扫 %d 个文件（跳过 %d 不可解析）；"
          "新增 %d / 基线存量 %d / 已修复可从基线删除 %d"
          % (n_files, n_skip, len(new), len(in_base), len(stale)))
    if stale:
        print("  ↳ 已修复的基线条目（建议人工核对后从 BASELINE 删除）: "
              + "; ".join(sorted(stale)))
    if new:
        print("comment_ref_guard: FAIL —— 注释悬空引用只许减不许增")
        return 1
    print("comment_ref_guard: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
