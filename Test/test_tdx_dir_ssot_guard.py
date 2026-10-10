# -*- coding: utf-8 -*-
"""TDX 安装目录「单一事实源」护栏。

钉三件事：

  ① **路径字面量只许出现在 SSOT 一处** —— `App/AppConfig.py` 的
     `_default_tdx_install_dir()`。任何其它 `.py` / `.md` / `.html` / `.env*`
     再抄一份绝对路径副本，就是第二个 SSOT：目录一迁移就静默漂移
     （回测照跑、数字照出，只是序列已经不是页面那条）。**这是本护栏的首要目标**。

  ② **消费方必须现读 SSOT** —— `tdx_source.py` / `data_parity.py` 取目录一律
     走「`os.environ["TDX_INSTALL_DIR"]` → `AppConfig.tdx_install_dir`」，
     不许自带 `os.environ.get(..., <路径字面量>)` 形式的兜底默认值。

  ③ **注释里不许复制默认路径值** —— 注释写死 `D:\\xxx` 之后，改目录必然忘记改注释。
     允许的写法是指向 SSOT（如「见 _default_tdx_install_dir()」）。

豁免（白名单，逐条给理由）：
  · `Test/fixtures_real/manifest.json` 的 `vipdoc_dir` —— 真实行情切片的数据指纹，
    路径写进 manifest 是为了记录「这批切片从哪来」，不是「本机默认目录」；
    改它会让 sha256 校验与冻结基线失效。
  · `Docs/数据源.md` / `Docs/chan_行业映射单一源改造_报告.html` 的**历史实测**句
    （句子内含日期 2026-09-xx）—— 记录「当时在哪台机器的哪个目录实测」，
    属历史快照，不是当前默认值的声明。

退出码：0 = 全部通过；1 = 有守护失败。
"""
import io
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
BS = chr(92)

# SSOT 文件：唯一允许出现路径字面量的地方
SSOT = os.path.join("App", "AppConfig.py")
SSOT_FUNC = "_default_tdx_install_dir"

# 扫描范围：源码 + 文档 + 配置模板（排除 .git / 缓存 / 冻结数据）
SCAN_EXT = {".py", ".md", ".html", ".htm", ".sh", ".example", ".txt"}
SCAN_NAMES = {".env"}
SKIP_DIRS = {".git", "__pycache__", ".kcache_tdx", "State", "node_modules",
             "\u53c2\u7167\u65e5\u5fd7", "\u53c2\u7167\u7ed3\u679c",
             ".venv", "venv"}   # 运行产物目录 + 虚拟环境
# ↑ `.venv`：2026-10-10 门禁实测 —— 已安装的第三方包 `eltdx`（2026-10-06 装入，
#   早于基线）其 `docs/` `helpers/` `protocol/` 自带含 tdx 的路径字面量，被本护栏
#   扫成「本仓越权引用」3 处假红。第三方包不是本仓 SSOT 的约束对象 ⇒ 跳过。

# 历史快照豁免：这些文件里**带日期**的行允许保留旧路径
HISTORICAL_FILES = {
    os.path.join("Docs", "\u6570\u636e\u6e90.md"),
    os.path.join("Docs", "chan_" + "\u884c\u4e1a\u6620\u5c04\u5355\u4e00\u6e90\u6539\u9020_\u62a5\u544a.html"),
}
# 整体豁免：本身就是"某次改造的历史交付报告"，其「核实方式」段落记录当时实测目录
# （这些文档不会再被当作当前默认值的声明来读）。
HISTORICAL_DOCS_WHOLE = {
    os.path.join("Docs", "chan_" + "\u884c\u4e1a\u6620\u5c04\u5355\u4e00\u6e90\u6539\u9020_\u62a5\u544a.html"),
}
# 数据指纹豁免（JSON 值，非注释）
FINGERPRINT_FILE = os.path.join("Test", "fixtures_real", "manifest.json")

# 路径字面量模式：任意盘符/家目录前缀 + 含 tdx 的目录名
# 前置否定 (?<![A-Za-z0-9.]) 挡掉 URL 主机名（http://static.tdx.com.cn/...）
PATH_RE = re.compile(
    r"(?<![A-Za-z0-9.])[A-Za-z]:[\\/][^\s`\"'<>)\]|,;]*?tdx[^\s`\"'<>)\]|,;]*"
    r"|(?<![A-Za-z0-9.])~[\\/][^\s`\"'<>)\]|,;]*?tdx[^\s`\"'<>)\]|,;]*",
    re.IGNORECASE,
)
# 丢弃纯符号/占位写法（如 <TDX_INSTALL_DIR>/vipdoc、{TDX_INSTALL_DIR}\vipdoc）
PLACEHOLDER_RE = re.compile(r"[<>{}]")
# 占位目录名：your_/my_/xxx 等明显非真实路径（模板里的示意值）
# 尾部 [\\/]* 而非 [\\/]+ —— 允许 D:\your_tdx_dir 这种"目录名即结尾"的写法
PLACEHOLDER_DIR_RE = re.compile(r"[\\/](your|my|xxx|some|example)[_a-z0-9]*[\\/]?$",
                                re.IGNORECASE)
# 本护栏自身豁免（文件内必然出现用于说明反例的路径字面量）
SELF = os.path.join("Test", "test_tdx_dir_ssot_guard.py")
# 历史快照豁免：窗口行（含上一行，兼容 HTML 里日期与路径分处两行）
HIST_WINDOW = 2

_passed = 0
_failed = 0
_fail_lines = []


def check(name, ok, detail=""):
    global _passed, _failed
    if ok:
        _passed += 1
        print("  \u2713 %s" % name)
    else:
        _failed += 1
        print("  \u2717 %s%s" % (name, (" -> " + detail) if detail else ""))
        _fail_lines.append(name)


def rel(p):
    return os.path.relpath(p, ROOT).replace("/", os.sep)


def iter_scan_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in SCAN_EXT or fn in SCAN_NAMES:
                yield os.path.join(dirpath, fn)


def read(path):
    try:
        return io.open(path, encoding="utf-8", newline="").read()
    except (UnicodeDecodeError, OSError):
        return ""


def is_historical_ok(rp, lines, idx):
    """历史快照豁免：豁免文件 + 该行或紧邻上一行含日期戳。

    HTML 里「核实方式：…本机通达信目录 <code>D:\\xxx</code> 实测」常与日期
    分处两行，故按窗口判定，而非只看当前行。
    """
    if rp in HISTORICAL_DOCS_WHOLE:
        return True
    if rp not in HISTORICAL_FILES:
        return False
    lo = max(0, idx - HIST_WINDOW)
    for l in lines[lo:idx + 1]:
        if re.search(r"20\d{2}-\d{2}-\d{2}", l):
            return True
    return False


print("\n[1] 路径字面量只许出现在 SSOT（App/AppConfig.py）")

violations = []
for path in iter_scan_files():
    rp = rel(path)
    if rp in (SSOT, SELF):
        continue
    lines = read(path).splitlines()
    for i, line in enumerate(lines, 1):
        for m in PATH_RE.finditer(line):
            lit = m.group(0)
            if PLACEHOLDER_RE.search(lit):
                continue          # <TDX_INSTALL_DIR>/xxx 这类占位，不是真实路径副本
            if PLACEHOLDER_DIR_RE.search(lit):
                continue          # D:\your_tdx_dir 这类模板示意值
            if is_historical_ok(rp, lines, i - 1):
                continue          # 历史实测快照，允许保留
            violations.append("%s:%d  %s" % (rp, i, lit.strip()))

check("\u2460 全仓无第二份路径字面量副本", not violations,
      "\n      " + "\n      ".join(violations) if violations else "")

print("\n[2] SSOT 函数真实存在且返回非空路径")
ssot_src = read(os.path.join(ROOT, SSOT))
check("\u2461 SSOT 文件含 %s()" % SSOT_FUNC, ("def %s(" % SSOT_FUNC) in ssot_src)
# 确认 SSOT 里 Windows 分支真的给了路径（防"去值化"改过头把值也删了）
win_lits = [m.group(0) for m in PATH_RE.finditer(ssot_src)]
check("\u2461 SSOT 内仍有 Windows 路径字面量（未被清空）", len(win_lits) >= 1,
      repr(win_lits))

print("\n[3] 消费方现读 SSOT，不自带默认值兜底")
# 反模式：os.environ.get("TDX_INSTALL_DIR", <字面量>)
BAD_FALLBACK = re.compile(
    r"os\.environ\.get\(\s*[\"']TDX_INSTALL_DIR[\"']\s*,\s*[^)]*[A-Za-z]:",
)

consumers = [
    os.path.join("Backtest", "Exp", "common", "tdx_source.py"),
    os.path.join("Backtest", "Exp", "common", "data_parity.py"),
]
bad_consumers = []
for c in consumers:
    p = os.path.join(ROOT, c)
    if not os.path.isfile(p):
        bad_consumers.append("%s (缺失)" % c)
        continue
    if BAD_FALLBACK.search(read(p)):
        bad_consumers.append("%s (仍带硬编码兜底默认值)" % c)

check("\u2462 消费方无 os.environ.get(..., <路径字面量>) 兜底",
      not bad_consumers, "; ".join(bad_consumers))

# 正向断言：两个消费方都真的引到了 AppConfig（否则"现读 SSOT"是空话）
no_ref = []
for c in consumers:
    p = os.path.join(ROOT, c)
    if os.path.isfile(p) and "AppConfig" not in read(p):
        no_ref.append(c)
check("\u2462 消费方均引用 AppConfig（现读 SSOT）", not no_ref, "; ".join(no_ref))

print("\n[4] 配置模板不烧死绝对路径（防 git 里的模板成为第二个源）")
env_ex = read(os.path.join(ROOT, ".env.example"))
active_tdx = [
    l for l in env_ex.splitlines()
    if l.strip().startswith("TDX_INSTALL_DIR") and not l.strip().startswith("#")
]
check("\u2463 .env.example 中 TDX_INSTALL_DIR 处于注释态（不烧死路径）",
      not active_tdx, repr(active_tdx))

print("\n" + "-" * 60)
print("passed=%d  failed=%d" % (_passed, _failed))
if _failed:
    print("\n失败项：")
    for n in _fail_lines:
        print("  \u2717 " + n)
sys.exit(1 if _failed else 0)
