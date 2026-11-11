# -*- coding: utf-8 -*-
"""同花顺 Cookie 自动获取（2026-11-11 改造）验证

背景（用户原始流程）：
  点扫描面板「保存到自选」前，必须先手工打开 Firefox 登录 https://t.10jqka.com.cn，
  保持登录态，再运行 Script/ths_capture_cookie.py 把 Cookie 落到
  Script/ths_captured_cookie.txt，否则保存直接失败。三步人工只为拿一份 Cookie。

改造后：DataAPI/ThsCookieSource.ensure_cookie() 按「缓存文件 → 本机已登录浏览器 →
登录窗口」三级自动取，调用方只问结果。本用例锁住这些性质（**全程打桩：不联网、
不起浏览器、不写生产 Cookie 文件**）：

  ① 缓存有效 → 不碰浏览器、不弹窗；
  ② 缓存失效 → 后台读浏览器，命中即写回 Cookie 文件；
  ③ 全通道失败 → 抛 ThsCookieUnavailable，details 逐条记录原因；
  ④ allow_interactive=False 或处于失败冷却期 → **绝不**弹窗；
  ⑤ 弹窗登录成功 → 来源 login；
  ⑥ 校验结论有 TTL 缓存，ttl=0 强制重探；
  ⑦ Cookie 文件原子写（无 .tmp 残留、去首尾空白、空串不落盘）；
  ⑧ 静态契约：Script 工具只做 CLI（逻辑在 provider）、AppScan 不再要求「运行脚本」、
     DataAPI 不反向依赖 App（保持单向依赖）；
  ⑨ JSONP 三形态解析 + Cookie 同名去重（后写覆盖，与历史实现一致）。

运行：python Test/test_ths_cookie_autofetch.py     # 非 0 退出即失败
"""
import os
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

os.environ.pop("THS_COOKIE", None)          # 环境变量会抢占最高优先级，先摘掉

from DataAPI import ThsCookieSource as S    # noqa: E402

results = []


def rec(tag, name, ok, detail=""):
    results.append((ok, tag, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tag} {name}: {detail}")


def _read(rel):
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def _patch(**attrs):
    """临时替换模块属性；返回 restore()。"""
    old = {k: getattr(S, k) for k in attrs}
    for k, v in attrs.items():
        setattr(S, k, v)

    def restore():
        for k, v in old.items():
            setattr(S, k, v)
    return restore


def _probe_by_value(valid_values, calls=None):
    """构造 probe_cached 替身：只有值在 valid_values 里才算有效。"""
    def fake(cookie, ttl=S.VALIDATE_TTL, timeout=S.PROBE_TIMEOUT):
        if calls is not None:
            calls.append((cookie, ttl))
        return (cookie in valid_values,
                "OK（自选 142 只）" if cookie in valid_values else "当前用户未登录")
    return fake


class Case:
    """每个用例共用的一批桩：记录浏览器/弹窗/write 调用。"""

    def __init__(self, file_cookie="", browser=(), valid=(), allowed=True,
                 login_cookie="", probe=None):
        self.calls = {"browser": 0, "login": 0}
        self.written = []
        self.browser = list(browser)
        self.login_cookie = login_cookie
        self.probe = probe or _probe_by_value(set(valid))
        self.restore = _patch(
            read_cookie_file=lambda path=None: file_cookie,
            probe_cached=self._probe,
            iter_browser_cookies=self._iter_browser,
            ensure_interactive_login=self._login,
            interactive_allowed=lambda: allowed,
            write_cookie_file=self._write,
        )

    def _probe(self, cookie, ttl=S.VALIDATE_TTL, timeout=S.PROBE_TIMEOUT):
        return self.probe(cookie, ttl=ttl, timeout=timeout)

    def _iter_browser(self):
        self.calls["browser"] += 1
        return iter(self.browser)

    def _login(self, timeout=S.DEFAULT_LOGIN_TIMEOUT):
        self.calls["login"] += 1
        return self.login_cookie

    def _write(self, cookie, path=None):
        self.written.append(cookie)
        return True

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.restore()
        return False


# ① 缓存有效 → 直接返回，浏览器与弹窗都不碰
with Case(file_cookie="UID=1; ticket=t1", valid={"UID=1; ticket=t1"}) as c:
    cookie, source = S.ensure_cookie(allow_interactive=True)
    rec("①", "缓存有效即用、不碰浏览器/不弹窗",
        cookie == "UID=1; ticket=t1" and source == "file"
        and c.calls["browser"] == 0 and c.calls["login"] == 0,
        f"cookie={cookie!r} source={source} browser={c.calls['browser']} login={c.calls['login']}")

# ② 缓存失效 → 后台读浏览器，命中即写回文件
with Case(file_cookie="OLD=1", valid={"FRESH=2"},
          browser=[("firefox", "FRESH=2", 33)]) as c:
    cookie, source = S.ensure_cookie(force=True)
    rec("②", "缓存失效→浏览器命中→写回文件",
        cookie == "FRESH=2" and source == "browser:firefox" and c.written == ["FRESH=2"],
        f"cookie={cookie!r} source={source} written={c.written}")

# ③ 全通道失败 → 抛 ThsCookieUnavailable，details 记录每级原因，且不回写
with Case(file_cookie="OLD=1", valid=set(), browser=[("firefox", "BAD=2", 20)]) as c:
    err = None
    try:
        S.ensure_cookie(force=True)
    except S.ThsCookieUnavailable as e:
        err = e
    ok = (err is not None and len(err.details) >= 2 and c.written == []
          and any(d.startswith("file") for d in err.details)
          and any(d.startswith("browser:firefox") for d in err.details))
    rec("③", "全失败抛异常并保留逐条原因", ok,
        f"msg={err} details={getattr(err, 'details', None)}")

# ④ allow_interactive=False / 冷却期 → 绝不弹窗
with Case(file_cookie="", valid=set()) as c:
    err = None
    try:
        S.ensure_cookie(allow_interactive=False)
    except S.ThsCookieUnavailable as e:
        err = e
    no_popup_off = err is not None and c.calls["login"] == 0

with Case(file_cookie="", valid=set(), allowed=False) as c2:
    err2 = None
    try:
        S.ensure_cookie(allow_interactive=True)
    except S.ThsCookieUnavailable as e:
        err2 = e
    no_popup_cool = (err2 is not None and c2.calls["login"] == 0
                     and any("冷却" in d or "THS_AUTO_LOGIN" in d for d in err2.details))
rec("④", "禁止弹窗/冷却期内不弹窗",
    no_popup_off and no_popup_cool,
    f"off:login={c.calls['login']} cool:login={c2.calls['login']} details={getattr(err2, 'details', None)}")

# ⑤ 弹窗登录成功 → 来源 login
with Case(file_cookie="", valid=set(), allowed=True, login_cookie="FRESH=9") as c:
    cookie, source = S.ensure_cookie(allow_interactive=True)
    rec("⑤", "登录窗口成功后来源=login",
        cookie == "FRESH=9" and source == "login" and c.calls["login"] == 1,
        f"cookie={cookie!r} source={source} login={c.calls['login']}")

# ⑥ 校验结论 TTL 缓存：同值两次只探 1 次；ttl=0 强制重探
S.invalidate()
probe_calls = []
restore = _patch(probe=lambda cookie, timeout=S.PROBE_TIMEOUT: (
    probe_calls.append(cookie) or (True, "OK（自选 1 只）")))
try:
    S.probe_cached("C=1")
    S.probe_cached("C=1")
    after_ttl = len(probe_calls)
    S.probe_cached("C=1", ttl=0)
    after_force = len(probe_calls)
finally:
    restore()
    S.invalidate()
rec("⑥", "校验缓存命中/强制刷新", after_ttl == 1 and after_force == 2,
    f"ttl 内={after_ttl} 次，ttl=0 后={after_force} 次")

# ⑦ Cookie 文件原子写
tmpdir = tempfile.mkdtemp(prefix="ths_cookie_test_")
target = os.path.join(tmpdir, "sub", "cookie.txt")
w_ok = S.write_cookie_file("  A=1; B=2  \n", target)
content = open(target, "r", encoding="utf-8").read() if os.path.exists(target) else ""
leftovers = [f for f in os.listdir(os.path.dirname(target)) if f.endswith(".tmp")]
empty_rejected = S.write_cookie_file("", target) is False
rec("⑦", "原子写：去空白/无 .tmp 残留/空串拒绝",
    w_ok and content == "A=1; B=2" and not leftovers and empty_rejected,
    f"content={content!r} leftovers={leftovers} empty_rejected={empty_rejected}")

# ⑧ 静态契约
script_src = _read(os.path.join("Script", "ths_capture_cookie.py"))
scan_src = _read(os.path.join("App", "AppScan.py"))
api_src = _read(os.path.join("DataAPI", "ThsCloudZxgAPI.py"))
src_src = _read(os.path.join("DataAPI", "ThsCookieSource.py"))

script_delegates = ("ThsCookieSource" in script_src
                    and "import browser_cookie3" not in script_src
                    and "sync_playwright" not in script_src)
# AppScan 的失败文案不再要求用户去跑脚本（Cookie 由程序自动取）
scan_msg_ok = ("请运行 ths_capture_cookie.py" not in scan_src
               and "Cookie过期" not in scan_src
               and "cloud_result.get('source'" in scan_src)
# ThsCloudZxgAPI：缺 Cookie 时不再指引「运行 Script/ths_capture_cookie.py」
api_msg_ok = ("请运行 Script/ths_capture_cookie.py" not in api_src
              and "ensure_cookie" in api_src and "_open_api" in api_src)
# 依赖方向：DataAPI 不反向 import App（单向依赖，同名层不被上层污染）
no_reverse_dep = not any(
    ln.strip().startswith(("from App", "import App"))
    for ln in src_src.splitlines())
rec("⑧", "静态契约（脚本只做 CLI / 文案 / 单向依赖）",
    script_delegates and scan_msg_ok and api_msg_ok and no_reverse_dep,
    f"script={script_delegates} scan={scan_msg_ok} api={api_msg_ok} 单向依赖={no_reverse_dep}")

# ⑨ JSONP 三形态 + 同名 Cookie 去重
p_json = S.parse_jsonp('{"errorCode":0,"result":[1,2]}')
p_cb = S.parse_jsonp('selfStock({"errorCode":0,"result":[1]})')
p_cb_semicolon = S.parse_jsonp('selfStock({"errorCode":0,"result":[1,2,3]});')
p_bad = S.parse_jsonp("<html>403</html>")
dedupe = S.build_cookie_string([("a", "1"), ("b", "2"), ("a", "9")])
rec("⑨", "JSONP 三形态 + 同名后写覆盖",
    p_json.get("errorCode") == 0 and len(p_json.get("result")) == 2
    and p_cb.get("errorCode") == 0 and len(p_cb_semicolon.get("result")) == 3
    and p_bad.get("errorCode") == -1 and dedupe == "a=9; b=2",
    f"json={p_json} cb={p_cb} bad={p_bad} dedupe={dedupe!r}")

# ⑩ 全部失败都是网络错误 → 不弹登录窗口（网断时弹窗只会白等一个超时）
def _net_probe(cookie, ttl=S.VALIDATE_TTL, timeout=S.PROBE_TIMEOUT):
    return False, "网络错误: ConnectionError: boom"


with Case(file_cookie="OLD=1", valid=set(), probe=_net_probe,
          browser=[("firefox", "B=1", 3)], allowed=True) as c:
    err = None
    try:
        S.ensure_cookie(force=True, allow_interactive=True)
    except S.ThsCookieUnavailable as e:
        err = e
    rec("⑩", "网络不可达时不弹登录窗口",
        err is not None and c.calls["login"] == 0
        and any("网络不可达" in d for d in err.details),
        f"login={c.calls['login']} details={getattr(err, 'details', None)}")


# ── 汇总 ──────────────────────────────────────────────────────────────
failed = [r for r in results if not r[0]]
print("\n" + "=" * 64)
print(f"共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
if failed:
    for _, tag, name, detail in failed:
        print(f"  ✗ {tag} {name}: {detail}")
print("=" * 64)
sys.exit(1 if failed else 0)
