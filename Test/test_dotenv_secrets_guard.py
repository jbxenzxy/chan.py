"""仓库根 .env 凭据护栏（2026-10-02，评审 P3-7）。

背景：`.env` 已被仓库根 `.gitignore` 放行（`!/.env`），日后若往里写账号密码即公开
（见 Trading/Config.py 的 BaseSettings + extra="forbid"，裸键还会让子进程启动即崩）。
本护栏在门禁层拦截：仓库根 `.env` 出现 PASSWORD / ACCOUNT / SECRET / TOKEN /
API_KEY / PRIVATE_KEY / ACCESS_KEY / CREDENTIAL 等凭据键即红。

范围：仅扫描仓库根 `.env`（若存在）；文件不存在则跳过（护栏只管"已入库的 .env"）。
行首 `#` 注释与空行忽略，不判值（值可能是占位符），只拦键名出现。
用法：python Test/test_dotenv_secrets_guard.py（退出码 0 = 通过）
"""
import io
import os
import re
import sys

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
_ENV_PATH = os.path.join(ROOT, ".env")

# 键名首段（等号前），大小写不敏感
_KEY_RE = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_]*)")
# 敏感键模式（大小写不敏感）
_SENSITIVE_RE = re.compile(
    r"(PASSWORD|PASSWD|ACCOUNT|SECRET|TOKEN|API_?KEY|PRIVATE_?KEY|"
    r"ACCESS_?KEY|CREDENTIAL)",
    re.IGNORECASE,
)

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


print("\n[1] 仓库根 .env 不含凭据键")
if not os.path.isfile(_ENV_PATH):
    check("仓库根无 .env -> 跳过护栏（视为通过）", True, True)
else:
    bad = []
    with io.open(_ENV_PATH, encoding="utf-8", errors="ignore") as fh:
        for ln, raw in enumerate(fh, 1):
            s = raw.strip()
            if not s or s.startswith("#"):
                continue
            m = _KEY_RE.match(s)
            if not m:
                continue
            key = m.group(1)
            if _SENSITIVE_RE.search(key):
                bad.append("%s (line %d)" % (key, ln))
    check("无凭据键（PASSWORD/ACCOUNT/SECRET/TOKEN/...）", bad, [])

print("\n" + "=" * 60)
print("dotenv_secrets_guard: {} passed, {} failed".format(_passed, _failed))
print("=" * 60)
sys.exit(1 if _failed else 0)
