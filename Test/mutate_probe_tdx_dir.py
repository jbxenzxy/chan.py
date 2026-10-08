# -*- coding: utf-8 -*-
"""护栏判别力自证：三种回潮形态各注入一次，验证护栏确实报警。

用法：python mutate_probe.py
退出码：0 = 三种变异全部被抓（护栏有效）；1 = 有变异逃逸（护栏失效）。
"""
import io
import os
import subprocess
import sys

SB = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
PY = sys.executable

BS = chr(92)
TARGET = os.path.join(SB, "Backtest", "Exp", "common", "data_parity.py")
DOC = os.path.join(SB, "Docs", "chan-arch-concurrency-v7.0.html")
ENVEX = os.path.join(SB, ".env.example")

CASES = [
    ("变异1 硬编码兜底默认值回填",
     TARGET,
     'TDX_INSTALL = os.environ.get("TDX_INSTALL_DIR") or _ac.tdx_install_dir',
     'TDX_INSTALL = os.environ.get("TDX_INSTALL_DIR", r"D:' + BS + 'new_tdx64")'),
    ("变异2 文档注释复制绝对路径",
     DOC,
     "默认值由 <code>AppConfig._default_tdx_install_dir()</code> 单点定义",
     "默认值 <code>D:" + BS + "new_tdx64</code>"),
    ("变异3 配置模板烧死绝对路径",
     ENVEX,
     "# TDX_INSTALL_DIR=D:" + BS + "your_tdx_dir",
     "TDX_INSTALL_DIR=D:" + BS + "CChan_tdx"),
]


def run_guard():
    r = subprocess.run([PY, os.path.join(SB, "Test", "test_tdx_dir_ssot_guard.py")],
                       cwd=SB, capture_output=True, text=True, timeout=180)
    return r.returncode


print("=== 基线（未变异）应通过 ===")
base = run_guard()
print("基线退出码 =", base)
if base != 0:
    print("!! 基线未通过，自证无意义")
    sys.exit(1)

escaped = []
for name, path, old, new in CASES:
    s = io.open(path, encoding="utf-8", newline="").read()
    if old not in s:
        print("\n[跳过] %s — 锚点未找到" % name)
        escaped.append(name + " (锚点缺失)")
        continue
    io.open(path, "w", encoding="utf-8", newline="").write(s.replace(old, new, 1))
    rc = run_guard()
    io.open(path, "w", encoding="utf-8", newline="").write(s)   # 还原
    status = "\u2713 被抓" if rc != 0 else "\u2717 逃逸"
    print("\n[%s] %s (退出码=%d)" % (name, status, rc))
    if rc == 0:
        escaped.append(name)

print("\n=== 还原后再跑一次确认 ===")
final = run_guard()
print("还原后退出码 =", final)

print("\n" + "=" * 50)
if escaped or final != 0:
    print("自证失败：%s" % (escaped or "还原后不为 0"))
    sys.exit(1)
print("自证通过：3/3 变异均被护栏抓住，且还原后恢复全绿")
