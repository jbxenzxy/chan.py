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
  ⑨ JSONP 三形态解析 + Cookie 同名去重（后写覆盖，与历史实现一致）；
  ⑩ 全部通道都是「网络错误」→ 不弹登录窗口（网断时弹窗只会白等超时）；
  ⑪ 登录检测信号含 **value**：同名同域只换值也能检出（真跑 interactive_login）；
  ⑫ probe 异常分类：HTTP 4xx/5xx 归「服务端拒绝」而非「网络错误」，仍允许弹窗；
  ⑬ Cookie 优先级只有一份（env → file → 浏览器），构造期不联网、显式路径不静默回退；
  ⑭ 同一请求内不重复扫浏览器：`skip_browser` 从保存链路两次兜底一路透传到
     `resolve_cookie`（旧实现每次兜底都重扫四个浏览器 + 逐通道 8s 校验）。
  ⑮ 网络不可达四场景：「通道自证 / 说明行不污染判据 / 零证据靠独立探测 /
     对照（网络通必弹）」—— ⑭ 那条说明行曾把「全网络错误」判成假，
     导致网断时仍白等 90s 登录窗。
  ⑯ `network_reachable` 自身行为：连接失败 / TLS / HTTP 4xx / 线程超时 / 缓存。
  ⑰ 可达性结论不跨请求复用（产品默认 `ttl=0`）：网络状态翻转后判据必须跟着翻；
     弹窗被禁用时不做无谓探测。**不桩 `network_reachable`**，只桩 `_reach_once`
     —— 桩掉整个函数会吞掉产品默认参数（把默认 ttl 写死成 60 也测不出来）。
  ⑱ `THS_LOGIN_TIMEOUT` 真的生效：合法值 / 非法值回退默认 / 显式传值优先；
     旧实现只有文档写了这个变量，代码里恒用 90s（P3-1）。
  ⑲ 取 Cookie 失败时，`ThsCookieUnavailable.details`（逐通道原因）必须透到
     保存接口的 `ths_msg`；旧写法只取 `str(e)`，面板与日志都看不到是哪条通道坏的。
  ⑳ `Script/ths_capture_cookie.py` 的 `--login` 也跟随 `THS_LOGIN_TIMEOUT`、显式
     `--timeout` 优先、`nan`/`inf` 在 argparse 边界被拒（不留 traceback）。
  ㉑ 登录子进程超时但 Cookie 已落盘：必须读回并用 `ttl=0` 校验，且不刷新
     `_last_login_fail`；对照组（Cookie 失效）仍判失败并进冷却 —— 别吞真失败。
  ㉒ 超时后连进程树一起回收：`proc.kill()` + Windows `taskkill /F /T /PID`。

运行：python Test/test_ths_cookie_autofetch.py     # 非 0 退出即失败
"""
import contextlib
import inspect
import io
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
    """每个用例共用的一批桩：记录浏览器/弹窗/探测/write 调用。

    `reach` 整体打桩 `network_reachable`（`None` = 默认可达；也可传可调用对象，
    按调用次序返回不同结论）；`reach_once` **只**打桩最底层的 `_reach_once`，
    真实的 `network_reachable`（含默认参数与 TTL 缓存）整段照跑 —— 要验证
    「结论跨请求复用」必须用后者：桩整体替换 `network_reachable` 时，
    `ensure_cookie()` 的无参调用落在桩的默认值上，产品把默认 `ttl` 写死成 60
    也测不出来（M4 盲点）。`reach_once` 模式不联网：HEAD 已被打桩。
    """

    def __init__(self, file_cookie="", browser=(), valid=(), allowed=True,
                 login_cookie="", probe=None, reach=None, reach_once=None):
        self.calls = {"browser": 0, "login": 0, "reach": 0}
        self.written = []
        self.browser = list(browser)
        self.login_cookie = login_cookie
        self.reach = reach
        self.reach_once = reach_once
        self.probe = probe or _probe_by_value(set(valid))
        targets = dict(
            read_cookie_file=lambda path=None: file_cookie,
            probe_cached=self._probe,
            iter_browser_cookies=self._iter_browser,
            ensure_interactive_login=self._login,
            interactive_allowed=lambda: allowed,
            write_cookie_file=self._write,
        )
        if reach_once is None:
            targets["network_reachable"] = self._reach
        else:
            # 不替换 `network_reachable`：必须让真实函数的默认参数真的被用上，
            # 否则「产品默认 ttl 被改坏」这类回潮在本用例里不可见。
            targets["_reach_once"] = self._reach_once
        self.restore = _patch(**targets)

    def _probe(self, cookie, ttl=S.VALIDATE_TTL, timeout=S.PROBE_TIMEOUT):
        return self.probe(cookie, ttl=ttl, timeout=timeout)

    def _reach(self, host=S.REACH_HOST, timeout=S.REACH_TIMEOUT, ttl=S.REACH_TTL):
        self.calls["reach"] += 1
        if callable(self.reach):
            return self.reach()
        if self.reach is None:
            return True, "HTTP 200"
        return self.reach

    def _reach_once(self, *args, **kwargs):
        """最底层探测替身：真实 `network_reachable` 每真探一次才进来一次。

        计数点放这里 —— 缓存命中根本进不来，所以次数就是「真的发了几次 HEAD」。
        参数原样忽略：`_reach_once(host, timeout)` 的签名不是被测契约。
        """
        self.calls["reach"] += 1
        return self.reach_once()

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


# ⑪ interactive_login 的「登录成功」信号必须含 value
#    缺陷形态：复用 storage_state 时首轮 Cookie 集非空 → last_sig 被锁定；用户
#    登录后往往**只换同一批 Cookie 的值**（名字/域不变）→ 只看 (name, domain)
#    永远等不到变化，窗口等到超时、随即进入 10 分钟失败冷却。
#    本用例**真跑** interactive_login 的函数体（此前的 ⑤ 把 ensure_interactive_login
#    整个打桩，函数体零执行），只把 playwright 与联网校验换成假件。
def _run_interactive_login(rounds, valid_values):
    """用假 playwright 跑真实 interactive_login；返回 (cookie, probe 记录, write 记录)。"""
    import types

    class _Page:
        def goto(self, *a, **k):
            return None

    class _Ctx:
        def __init__(self):
            self.i = -1

        def add_init_script(self, *a):
            return None

        def new_page(self):
            return _Page()

        def cookies(self):
            self.i += 1
            return rounds[min(self.i, len(rounds) - 1)]

        def storage_state(self, path=None):
            return None

    class _Browser:
        def __init__(self):
            self.ctx = _Ctx()

        def new_context(self, **kwargs):
            return self.ctx

        def close(self):
            return None

    class _PW:
        def __init__(self):
            self.chromium = self
            self.browser = _Browser()

        def launch(self, **kwargs):
            return self.browser

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    class _Clock:
        """假时钟：sleep 只推进虚拟时间，用例不真等。"""

        def __init__(self):
            self.t = 1000.0

        def monotonic(self):
            return self.t

        def sleep(self, s):
            self.t += s

    probes, writes = [], []
    fake_pw = types.ModuleType("playwright.sync_api")
    fake_pw.sync_playwright = lambda: _PW()
    snapshot = {k: sys.modules.get(k) for k in ("playwright", "playwright.sync_api")}
    restore = _patch(
        probe_cached=lambda cookie, ttl=S.VALIDATE_TTL, timeout=S.PROBE_TIMEOUT: (
            probes.append(cookie) or (cookie in valid_values, "OK（自选 1 只）")),
        write_cookie_file=lambda cookie, path=None: writes.append(cookie) or True,
        state_file=lambda: os.path.join(tmpdir, "no_state.json"),
        _launch_browser=lambda p, headless=False: p.chromium.launch(),
        time=_Clock(),
    )
    sys.modules["playwright"] = types.ModuleType("playwright")
    sys.modules["playwright.sync_api"] = fake_pw
    try:
        cookie = S.interactive_login(timeout=8, headless=True)
    finally:
        restore()
        for k, v in snapshot.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    return cookie, probes, writes


_ROUND_VALUE_ONLY = [
    [{"name": "userid", "domain": ".10jqka.com.cn", "value": "u1"},
     {"name": "ticket", "domain": ".10jqka.com.cn", "value": "OLD"}],
    [{"name": "userid", "domain": ".10jqka.com.cn", "value": "u1"},
     {"name": "ticket", "domain": ".10jqka.com.cn", "value": "NEW"}],
]
# 先用例形状自证：两轮的 (name, domain) 集合必须**完全相同**、只有 value 变，
# 否则这条用例测不到本缺陷（例如误加了新 Cookie 名字就变成「名字集变化」）。
_old_form = [tuple(sorted((c["name"], c["domain"]) for c in rnd))
             for rnd in _ROUND_VALUE_ONLY]
_fixture_ok = _old_form[0] == _old_form[1]
_cookie_v, _probes_v, _writes_v = _run_interactive_login(
    _ROUND_VALUE_ONLY, {"userid=u1; ticket=NEW"})
rec("⑪", "登录检测信号含 value（同名同域只换值也能检出）",
    _fixture_ok and _cookie_v == "userid=u1; ticket=NEW"
    and _writes_v == ["userid=u1; ticket=NEW"] and len(_probes_v) == 2,
    f"用例形状(两轮名字集相同)={_fixture_ok} 返回={_cookie_v!r} "
    f"probe={len(_probes_v)} write={_writes_v}")

# ⑫ probe 的异常分类：HTTP 4xx/5xx 归「服务端拒绝」，不得混进「网络错误」
import requests                                  # noqa: E402

class _Resp:
    status_code = 403


_orig_get = requests.get


def _raise_http(*a, **k):
    raise requests.exceptions.HTTPError("403 Client Error: Forbidden", response=_Resp())


def _raise_conn(*a, **k):
    raise requests.exceptions.ConnectionError("boom")


try:
    requests.get = _raise_http
    _ok403, _msg403 = S.probe("A=1")
    requests.get = _raise_conn
    _oknet, _msgnet = S.probe("A=1")
finally:
    requests.get = _orig_get
_cls_ok = (not _ok403 and _msg403.startswith("服务端拒绝")
           and not _oknet and _msgnet.startswith("网络错误"))
rec("⑫", "probe 异常分类：403→服务端拒绝 / 连接失败→网络错误", _cls_ok,
    f"403={_msg403!r} conn={_msgnet!r}")

# ⑫b 全通道都是「服务端拒绝」时必须**仍然弹窗**：网络是通的，用户完全能登录
with Case(file_cookie="OLD=1", valid=set(),
          probe=lambda cookie, ttl=S.VALIDATE_TTL, timeout=S.PROBE_TIMEOUT:
              (False, "服务端拒绝: HTTP 403"),
          browser=[("firefox", "B=1", 3)], allowed=True,
          login_cookie="FRESH=9") as c:
    _cookie_403, _source_403 = S.ensure_cookie(force=True, allow_interactive=True)
    _popup_ok = (_cookie_403 == "FRESH=9" and _source_403 == "login"
                 and c.calls["login"] == 1)
rec("⑫b", "服务端拒绝（403）仍允许弹登录窗口", _popup_ok,
    f"cookie={_cookie_403!r} source={_source_403} login={c.calls['login']}")

# ⑬ Cookie 优先级只有一份（env → file → 浏览器）；构造期不联网；显式路径不静默回退
os.environ["THS_COOKIE"] = "ENV=1"
restore = _patch(
    read_cookie_file=lambda path=None: "FILE=1",
    iter_browser_cookies=lambda: iter([("firefox", "BROWSER=1", 3)]),
    probe_cached=lambda *a, **k: (_ for _ in ()).throw(AssertionError("不该联网")))
try:
    _c1, _s1, _ = S.resolve_cookie(validate=False)
finally:
    restore()
    os.environ.pop("THS_COOKIE", None)

restore = _patch(
    read_cookie_file=lambda path=None: "FILE=1",
    iter_browser_cookies=lambda: iter([("firefox", "BROWSER=1", 3)]))
try:
    _c2, _s2, _ = S.resolve_cookie(validate=False)
finally:
    restore()
_env_first = (_c1, _s1) == ("ENV=1", "env")
_file_first = (_c2, _s2) == ("FILE=1", "file")

from DataAPI.ThsCloudZxgAPI import CThsCloudZxg    # noqa: E402

_explicit_err = ""
try:
    CThsCloudZxg(cookie_file=os.path.join(tmpdir, "nope.txt"))
except ValueError as e:
    _explicit_err = str(e)
_explicit_ok = "不存在" in _explicit_err

# 构造期不得联网：只剩浏览器通道时也必须能 new 出来，且不触发 probe
restore = _patch(
    read_cookie_file=lambda path=None: "",
    iter_browser_cookies=lambda: iter([("firefox", "BROWSER=1", 3)]),
    probe_cached=lambda *a, **k: (_ for _ in ()).throw(AssertionError("构造期不该联网")))
try:
    _api = CThsCloudZxg()
    _ctor_ok = (_api.cookie == "BROWSER=1" and _api.cookie_source == "browser:firefox")
    _ctor_detail = f"{_api.cookie!r} source={_api.cookie_source}"
except AssertionError as e:
    _ctor_ok, _ctor_detail = False, f"构造期联网了：{e}"
finally:
    restore()

rec("⑬", "Cookie 优先级统一 + 构造期不联网 + 显式路径不静默回退",
    _env_first and _file_first and _explicit_ok and _ctor_ok,
    f"env={_env_first}({(_c1, _s1)}) file={_file_first}({(_c2, _s2)}) "
    f"显式缺失={_explicit_err!r} 构造={_ctor_detail}")


# ⑭ 同一请求内不重复扫浏览器（P3-2：保存按钮最坏等「两轮浏览器 + 90s 登录窗」）
#
# `save_scan_to_ths_cloud` 的两次兜底（首次取不到 / check_login 失效）都发生在
# 一轮 `resolve_cookie` 之后：env / 文件 / 四个浏览器已逐个读过并校验过。同一
# 请求内再扫一遍拿不到新结果，只会把「Edge 通道 6s + 每通道 8s 校验」再付一次。
_src_api = _read("DataAPI/ThsCloudZxgAPI.py")
_i = _src_api.index("def _open_api(")
_j = _src_api.index("\ndef ", _i + 10)
_open_fwd = "skip_browser=skip_browser" in _src_api[_i:_j]
_n_skip = _src_api.count("_open_api(force=True, allow_interactive=True, skip_browser=True)")

with Case(file_cookie="OLD=1", valid=set(), browser=[("firefox", "BAD=2", 20)]) as c:
    _err = None
    try:
        S.ensure_cookie(force=True, allow_interactive=True, skip_browser=True)
    except S.ThsCookieUnavailable as e:
        _err = e
    _skipped_ok = (c.calls["browser"] == 0
                   and any("跳过" in d for d in getattr(_err, "details", [])))

# 默认不跳过：构造期与首次取 Cookie 仍要能读到本机浏览器
with Case(file_cookie="OLD=1", valid=set(), browser=[("firefox", "BAD=2", 20)]) as c2:
    try:
        S.ensure_cookie(force=True, allow_interactive=True)
    except S.ThsCookieUnavailable:
        pass
    _default_ok = c2.calls["browser"] == 1

rec("⑭", "同一请求内不重复扫浏览器（skip_browser 透传）",
    _open_fwd and _n_skip == 2 and _skipped_ok and _default_ok,
    f"_open_api 透传={_open_fwd} 兜底带参={_n_skip}(应 2) "
    f"跳过时扫描={c.calls['browser']}(应 0) 默认扫描={c2.calls['browser']}(应 1)")


# ⑮ P2-1 回归：网络不可达时，「有通道但全网络错误」与「一条通道都没有」都不许弹窗
#
# 缺陷形态（2026-11-12 复核实测）：`details` 里混着一条 `note: browser 通道跳过`
# **说明行**，旧判据 `all("网络错误" in d for d in details)` 把它当证据 →
# 判成"非全网络错误" → 网断时照样弹窗，白等 90s。
# 四个场景必须分开测，它们走两条不同的证据链 + 一条对照：
#   A 有通道且全网络错误   → 通道自证（前缀白名单判据）
#   B 同上但本轮跳过浏览器 → 说明行不许污染判据（原缺陷的触发形态）
#   C 一条通道都没有       → 零证据，靠独立可达性探测
#   D 对照：同样没有 Cookie，但网络通 → 必须弹窗
#     （否则"没 Cookie 就一律不弹"这种错误实现会假绿）
_NET = (False, "ConnectionError: boom")

# A：env 里的 Cookie 已过期 + 全网络错误
os.environ["THS_COOKIE"] = "ENV=OLD"
try:
    with Case(file_cookie="", valid=set(), probe=_net_probe) as c:
        try:
            S.ensure_cookie(force=True, allow_interactive=True)
        except S.ThsCookieUnavailable:
            pass
        _a_login, _a_reach = c.calls["login"], c.calls["reach"]
finally:
    os.environ.pop("THS_COOKIE", None)

# B：与 A 同因，但同一请求内已扫过浏览器（保存链路的第二次兜底）
with Case(file_cookie="OLD=1", valid=set(), probe=_net_probe) as c:
    try:
        S.ensure_cookie(force=True, allow_interactive=True, skip_browser=True)
    except S.ThsCookieUnavailable:
        pass
    _b_login, _b_reach = c.calls["login"], c.calls["reach"]

# C：全新部署（无 env / 无缓存文件 / 无浏览器登录态）+ 网络不可达
with Case(file_cookie="", valid=set(), reach=_NET) as c:
    try:
        S.ensure_cookie(allow_interactive=True)
    except S.ThsCookieUnavailable:
        pass
    _c_login, _c_reach = c.calls["login"], c.calls["reach"]

# D：对照组 —— 同样没有 Cookie，但网络可达
with Case(file_cookie="", valid=set(), reach=(True, "HTTP 200"),
          login_cookie="FRESH=9") as c:
    _d_cookie, _d_source = S.ensure_cookie(allow_interactive=True)
    _d_login, _d_reach = c.calls["login"], c.calls["reach"]

rec("⑮", "网络不可达四场景（A 通道自证 / B 说明行不污染 / C 零证据探测 / D 对照必弹）",
    _a_login == 0 and _b_login == 0 and _c_login == 0
    and _a_reach == 0 and _b_reach == 0 and _c_reach == 1 and _d_reach == 1
    and _d_login == 1 and _d_cookie == "FRESH=9" and _d_source == "login",
    f"A(env过期+断网)login={_a_login} B(跳过浏览器+断网)login={_b_login} "
    f"C(无Cookie+断网)login={_c_login} D(无Cookie+网通)login={_d_login} | "
    f"探测次数 a={_a_reach} b={_b_reach} c={_c_reach} d={_d_reach}（应 0/0/1/1）")


# ⑯ network_reachable 自身行为（⑮ 把它整个打桩了，函数体零覆盖）
#
# 判据方向是刻意不对称的，四条都必须钉住，其中 TLS 那条最容易写错：
# `requests.exceptions.SSLError` 是 `ConnectionError` 的**子类**，except 顺序
# 一颠倒，证书告警就会被当成"网络不可达"→ 用户永远看不到登录入口。
import time as _t                                        # noqa: E402
import requests as _rq                                   # noqa: E402

_orig_head = _rq.head
_head_urls = []


def _mk_head(exc=None, status=200, delay=0.0):
    def _head(url, **kw):
        _head_urls.append(url)
        if delay:
            _t.sleep(delay)
        if exc is not None:
            raise exc

        class _R:
            status_code = status
        return _R()
    return _head


try:
    S._reach_cache.clear()
    _rq.head = _mk_head(exc=_rq.exceptions.ConnectionError("boom"))
    _r_conn = S.network_reachable(ttl=0)

    _rq.head = _mk_head(exc=_rq.exceptions.SSLError("cert expired"))
    _r_ssl = S.network_reachable(ttl=0)

    _rq.head = _mk_head(status=403)
    _r_403 = S.network_reachable(ttl=0)

    # 线程超时：head 睡 2s、预算 0.3s ⇒ 必须 ~0.3s 返回 False，而不是等满 2s。
    # 这条钉的是「socket timeout 不覆盖 getaddrinfo」的防护（裸 requests timeout
    # 在 DNS 悬停时可能卡十几秒，守卫自己变成新的等待点）。
    _rq.head = _mk_head(delay=2.0)
    _t0 = _t.monotonic()
    _r_tmo = S.network_reachable(timeout=0.3, ttl=0)
    _elapsed = _t.monotonic() - _t0

    # TTL 缓存：ttl=0 写缓存，紧接着 ttl=60 必须命中、不再发请求
    S._reach_cache.clear()
    _rq.head = _mk_head(status=200)
    S.network_reachable(ttl=0)
    _n_before = len(_head_urls)
    _r_cached = S.network_reachable(ttl=60)
    _n_after = len(_head_urls)
finally:
    _rq.head = _orig_head
    S._reach_cache.clear()

rec("⑯", "network_reachable 自身：连接失败/TLS/HTTP4xx/线程超时/缓存",
    _r_conn[0] is False and "ConnectionError" in _r_conn[1]
    and _r_ssl[0] is True and _r_tmo[0] is False and _elapsed < 1.0
    and _r_403 == (True, "HTTP 403") and _r_cached == (True, "HTTP 200")
    and _n_after == _n_before,
    f"conn={_r_conn[0]}/{_r_conn[1][:32]} ssl={_r_ssl[0]} 403={_r_403} "
    f"超时={_r_tmo[0]}({_elapsed:.2f}s,应<1s) 缓存={_n_after - _n_before}次额外请求(应 0)")


# ⑰ P2-2 回归：可达性结论**不许跨请求复用**（`REACH_TTL` 默认 0）
#
# 缺陷形态（2026-11-12 复核实测）：`REACH_TTL=60` 让上一轮的探测结论在 60s 内
# 跨请求生效，网络状态翻转后判据还是旧的：
#   · 断网 → 恢复：第二次仍按「不可达」跳过弹窗（点了保存却不见登录窗）；
#   · 通 → 断：第二次仍按「可达」去弹窗，白等 90s（P2-1 的同一后果，另一条入口）。
# 两条方向各钉一次，外带「弹窗被禁用时不做无谓探测」。
# `Case` 同时桩了 `ensure_interactive_login` 与 `interactive_allowed`，10 分钟
# 失败冷却不会介入，login 计数是干净的判别信号（否则方向① 会假绿）。
class _Flip:
    """按调用次序返回给定结论；用尽后沿用最后一条（多探一次也不会炸）。"""

    def __init__(self, *results):
        self._results = list(results)
        self._idx = 0

    def __call__(self):
        i = min(self._idx, len(self._results) - 1)
        self._idx += 1
        return self._results[i]


def _two_saves(reach_once):
    """同一进程连做两次「保存」：无 env / 无缓存文件 / 无浏览器登录态。

    只桩最底层 `_reach_once`，真实 `network_reachable`（含 TTL 缓存）照跑 ——
    要证明「结论不许跨请求复用」，必须让缓存代码真的执行。
    """
    S._reach_cache.clear()
    with Case(file_cookie="", valid=set(), reach_once=reach_once) as c:
        for _ in range(2):
            try:
                S.ensure_cookie(allow_interactive=True)
            except S.ThsCookieUnavailable:
                pass
        return c.calls["login"], c.calls["reach"]


try:
    _UP_DOWN = _two_saves(_Flip((True, "HTTP 200"),
                                (False, "网络错误: ConnectionError: boom")))
    _DOWN_UP = _two_saves(_Flip((False, "网络错误: ConnectionError: boom"),
                                (True, "HTTP 200")))

    # 弹窗被禁用（THS_AUTO_LOGIN=0 / 冷却中）：探测结论不影响任何分支，不该发请求
    with Case(file_cookie="", valid=set(), allowed=False, reach=_NET) as c:
        try:
            S.ensure_cookie(allow_interactive=True)
        except S.ThsCookieUnavailable:
            pass
        _OFF = (c.calls["login"], c.calls["reach"])
finally:
    S._reach_cache.clear()          # 别把本用例的缓存留给后续（含门禁里的其他组件）

# 产品默认参数也必须落在 `ttl=0`：上面的行为断言已能咬住「默认值被写死成 60」
# （缓存一命中，第二次保存就不再真探、login 计数也不翻），这里再补一条静态
# 断言，把「是哪个值坏了」直接写进失败信息。两种口径一起钉。
_TTL_DEFAULT = inspect.signature(S.network_reachable).parameters["ttl"].default

rec("⑰", "可达性结论不跨请求复用（产品默认 ttl=0）：两方向各真探 2 次 / 弹窗禁用不探测",
    S.REACH_TTL == 0.0 and _TTL_DEFAULT == 0.0
    and _UP_DOWN == (1, 2) and _DOWN_UP == (1, 2)
    and _OFF == (0, 0),
    f"网通→断网 login={_UP_DOWN[0]}(应1) 真探={_UP_DOWN[1]}(应2) | "
    f"断网→网通 login={_DOWN_UP[0]}(应1) 真探={_DOWN_UP[1]}(应2) | "
    f"弹窗禁用 login={_OFF[0]} 探测={_OFF[1]}(应0) | "
    f"REACH_TTL={S.REACH_TTL} 签名默认={_TTL_DEFAULT}(应 0.0)")


# ⑱ `THS_LOGIN_TIMEOUT` 生效（P3-1）：文档早就写了这个变量，代码旧实现恒用 90s
#
# 唯一解析点是 `resolve_login_timeout()`：合法值原样返回、非法值（非数字 / ≤0）回退默认、
# 显式传值优先于环境变量。还要证明 `ensure_interactive_login` 把解析结果**真的
# 传下去** —— 只测 `resolve_login_timeout()` 会漏掉「解析了但没人用」这种半截实现。
_seen_timeouts = []
restore = _patch(_login_via_subprocess=lambda script, timeout:
                 _seen_timeouts.append(timeout) or "")
_last_fail_bak = S._last_login_fail
try:
    os.environ["THS_LOGIN_TIMEOUT"] = "7.5"
    _t_env = S.resolve_login_timeout()
    _c_auto = S.ensure_interactive_login()                  # 无参 → 7.5
    _c_explicit = S.ensure_interactive_login(timeout=12)    # 显式优先于 env
    os.environ["THS_LOGIN_TIMEOUT"] = "abc"
    _t_bad = S.resolve_login_timeout()
    os.environ["THS_LOGIN_TIMEOUT"] = "-3"
    _t_neg = S.resolve_login_timeout()
    os.environ["THS_LOGIN_TIMEOUT"] = "nan"      # float() 认，但 int() 会抛
    _t_nan = S.resolve_login_timeout()
    os.environ["THS_LOGIN_TIMEOUT"] = "inf"
    _t_inf = S.resolve_login_timeout()
    os.environ.pop("THS_LOGIN_TIMEOUT", None)
    _t_unset = S.resolve_login_timeout()
finally:
    restore()
    os.environ.pop("THS_LOGIN_TIMEOUT", None)
    S._last_login_fail = _last_fail_bak
rec("⑱", "THS_LOGIN_TIMEOUT 生效（合法值 / 非法回退 / 显式优先 / nan、inf 回退）",
    _t_env == 7.5 and _t_bad == S.DEFAULT_LOGIN_TIMEOUT
    and _t_neg == S.DEFAULT_LOGIN_TIMEOUT and _t_unset == S.DEFAULT_LOGIN_TIMEOUT
    and _t_nan == S.DEFAULT_LOGIN_TIMEOUT and _t_inf == S.DEFAULT_LOGIN_TIMEOUT
    and _c_auto == "" and _c_explicit == "" and _seen_timeouts == [7.5, 12],
    f"env=7.5→{_t_env}(应7.5) 非法→{_t_bad} 负数→{_t_neg} 未设→{_t_unset} "
    f"nan→{_t_nan} inf→{_t_inf}（后五者应 {S.DEFAULT_LOGIN_TIMEOUT}）"
    f" 登录窗收到={_seen_timeouts}(应 [7.5, 12])")


# ⑲ 保存失败时逐通道明细必须透出（P3-2）
#
# 保存链路把 `ThsCookieUnavailable` 收敛成一句 `ths_msg`，前端只把 `ths_msg`
# 挂到按钮 title。旧写法 `ths_msg = str(e)` 丢掉 `e.details`（哪条通道试过、
# 为什么失败），面板只剩「同花顺：失败」，日志也查不出根因。
import App.AppScan as _scan                              # noqa: E402


def _shadow(obj, **attrs):
    """临时替换模块/实例属性；restore 时区分「自有属性」与「继承来的」。"""
    saved = {k: (k in obj.__dict__, obj.__dict__.get(k)) for k in attrs}
    for k, v in attrs.items():
        setattr(obj, k, v)

    def restore():
        for k, (was_own, old) in saved.items():
            if was_own:
                obj.__dict__[k] = old
            else:
                obj.__dict__.pop(k, None)
    return restore


_cookie_err = S.ThsCookieUnavailable(
    "未取到可用的同花顺登录态", ["file: Cookie 已过期", "browser:firefox: 未登录"])


def _raise_cookie_err(codes):
    raise _cookie_err


_restore_m = _shadow(_scan._m, _THS_CLOUD_AVAILABLE=True,
                     save_scan_to_ths_cloud=_raise_cookie_err)
_restore_data = _shadow(_scan.app_data, save_to_zxg_blk=lambda codes: len(codes))
try:
    _save_res, _save_code = _scan.zxg_save("600000, 000001")
finally:
    _restore_m()
    _restore_data()
_msg = _save_res.get("ths_msg") or ""
rec("⑲", "取 Cookie 失败：逐通道明细落到 ths_msg",
    _save_code == 200 and _save_res.get("ths_saved") == 0
    and _save_res.get("tdx_saved") == 2
    and "未取到可用的同花顺登录态" in _msg
    and "file: Cookie 已过期" in _msg and "browser:firefox: 未登录" in _msg,
    f"code={_save_code} ths_saved={_save_res.get('ths_saved')} "
    f"tdx_saved={_save_res.get('tdx_saved')} ths_msg={_msg!r}")


# ⑳ CLI 入口（`--login`）也跟随 `THS_LOGIN_TIMEOUT`（M18c：CLI 不读 env 无人发现）
#
# `Script/ths_capture_cookie.py` 既是子进程入口（父进程用 `--timeout` 显式传值），
# 也是手工诊断入口（不带 `--timeout`）。手工跑时必须自己解析环境变量 —— 这条
# 链路原先没有任何断言盯着：把 `main()` 的解析改成恒 90，20 项照样全绿。
# 顺带钉住「非有限值在 CLI 边界就被拒」：`--timeout nan` 不该炸出 traceback。
import importlib.util as _ilu

_spec = _ilu.spec_from_file_location(
    "_ths_cli_under_test", os.path.join(_REPO_ROOT, "Script", "ths_capture_cookie.py"))
_cli = _ilu.module_from_spec(_spec)
_spec.loader.exec_module(_cli)

_cli_seen = []
restore = _patch(interactive_login=lambda timeout=None, headless=False:
                 (_cli_seen.append(timeout), "cookie")[1])
os.environ["THS_LOGIN_TIMEOUT"] = "7.5"
try:
    _rc_env = _cli.main(["--login"])                              # 无 --timeout → 7.5
    _rc_explicit = _cli.main(["--login", "--timeout", "3"])       # 显式优先
    try:
        with contextlib.redirect_stderr(io.StringIO()):     # argparse 的 usage 不必刷屏
            _cli.main(["--login", "--timeout", "nan"])
        _rc_nan = None
    except SystemExit as e:                                       # argparse 拒绝 → 2
        _rc_nan = e.code
    except Exception as e:                     # traceback 也算不合格，别让它打断整个组件
        _rc_nan = type(e).__name__
finally:
    restore()
    os.environ.pop("THS_LOGIN_TIMEOUT", None)


# 子进程收到的秒数不许截断：`0.6s` 若被 `int()` 成 `0`，argparse 会直接拒（见上），
# 表现为「设了 0.6 反而完全不弹窗」。这里只验命令行拼装，不启进程、不读真 Cookie。
class _FakeProc:
    returncode = 0

    def communicate(self, timeout=None):
        return b"", None

    def kill(self):
        pass


_cmds = []
_orig_popen = S.subprocess.Popen
S.subprocess.Popen = lambda cmd, **kw: (_cmds.append(cmd), _FakeProc())[1]
try:
    _rbf = _patch(read_cookie_file=lambda: "")
    try:
        S._login_via_subprocess(
            os.path.join(_REPO_ROOT, "Script", "ths_capture_cookie.py"), 0.6)
    finally:
        _rbf()
finally:
    S.subprocess.Popen = _orig_popen
_cmd_timeout = ""
if _cmds and "--timeout" in _cmds[-1]:
    _cmd_timeout = _cmds[-1][_cmds[-1].index("--timeout") + 1]
rec("⑳", "CLI --login 跟随 THS_LOGIN_TIMEOUT / 显式优先 / nan 被拒 / 秒数不截断",
    _rc_env == 0 and _rc_explicit == 0 and _rc_nan == 2
    and _cli_seen == [7.5, 3.0] and _cmd_timeout == "0.6",
    f"rc={_rc_env},{_rc_explicit} nan_rc={_rc_nan}(应2) "
    f"登录窗收到={_cli_seen}(应 [7.5, 3.0]) 子进程 --timeout={_cmd_timeout!r}(应 '0.6')")


# ㉑/㉒ 登录子进程超时：Cookie 已落盘仍要读回，并回收整棵进程树
# 复现条件（P2）：用户在上限内登录成功、Cookie 已原子落盘，但父进程的
# communicate(timeout+15) 先超时。旧实现 kill 后直接 return "" → 本次保存失败
# 且 _last_login_fail 被刷新 → 600s 冷却期内不再弹窗。
class _TimeoutProc:
    """communicate 首次抛 TimeoutExpired；第二次（收尾）返回空输出。"""

    pid = 424242

    def __init__(self):
        self.kills = 0
        self.comm = []

    def communicate(self, timeout=None):
        self.comm.append(timeout)
        if len(self.comm) == 1:
            raise S.subprocess.TimeoutExpired("ths_capture_cookie.py --login",
                                             timeout)
        return b"", None

    def kill(self):
        self.kills += 1


_orig_run = S.subprocess.run
_orig_popen_timeout = S.subprocess.Popen
_last_fail_bak = S._last_login_fail
_procs = []
_taskkill_cmds = []
_probe_seen = []
_got_valid = _got_stale = ""
_no_cooldown = False
_cooldown_set = False
try:
    with tempfile.TemporaryDirectory() as _tmpd:
        _cookie_path = os.path.join(_tmpd, "ths_captured_cookie.txt")

        def _fake_run(cmd, **kw):
            _taskkill_cmds.append(list(cmd))
            return None

        def _fake_popen(cmd, **kw):
            p = _TimeoutProc()
            _procs.append(p)
            return p

        S.subprocess.run = _fake_run
        S.subprocess.Popen = _fake_popen

        # A) 磁盘上是一份「登录已成功」的有效 Cookie
        S.write_cookie_file("FRESH_AFTER_LOGIN=1", _cookie_path)
        _r = _patch(candidate_cookie_files=lambda: [_cookie_path],
                    probe_cached=lambda cookie, ttl=S.VALIDATE_TTL,
                                       timeout=S.PROBE_TIMEOUT: (
                        _probe_seen.append((cookie, ttl)) or (True, "OK")))
        try:
            S._last_login_fail = 0.0
            _got_valid = S.ensure_interactive_login(timeout=1.0)
            _no_cooldown = (S._last_login_fail == 0.0)
        finally:
            _r()

        # B) 对照：磁盘上是失效 Cookie，仍须判失败并进冷却（别把真失败吞掉）
        S.write_cookie_file("STALE=0", _cookie_path)
        _r2 = _patch(candidate_cookie_files=lambda: [_cookie_path],
                     probe_cached=lambda cookie, ttl=S.VALIDATE_TTL,
                                        timeout=S.PROBE_TIMEOUT: (
                         _probe_seen.append((cookie, ttl)) or (False, "未登录")))
        try:
            S._last_login_fail = 0.0
            _got_stale = S.ensure_interactive_login(timeout=1.0)
            _cooldown_set = (S._last_login_fail > 0.0)
        finally:
            _r2()
finally:
    S.subprocess.run = _orig_run
    S.subprocess.Popen = _orig_popen_timeout
    S._last_login_fail = _last_fail_bak

rec("㉑", "登录子进程超时：已落盘 Cookie 读回 / 失效 Cookie 仍判失败",
    _got_valid == "FRESH_AFTER_LOGIN=1" and _no_cooldown
    and _got_stale == "" and _cooldown_set
    and _probe_seen == [("FRESH_AFTER_LOGIN=1", 0), ("STALE=0", 0)]
    and all(p.comm[0] == 16.0 for p in _procs),
    f"有效返回={_got_valid!r} 冷却未刷新={_no_cooldown} "
    f"失效返回={_got_stale!r} 冷却已刷新={_cooldown_set} "
    f"probe={_probe_seen} 首轮communicate={[p.comm[:1] for p in _procs]}")

_tree_cmds = [c for c in _taskkill_cmds if c and "taskkill" in c[0]]
_tree_ok = (bool(_procs) and all(p.kills >= 1 for p in _procs)
            and (os.name != "nt" or (
                len(_tree_cmds) >= len(_procs)
                and all("/F" in c and "/T" in c
                        and str(_procs[0].pid) in c for c in _tree_cmds))))
rec("㉒", "超时后进程树被回收（proc.kill + Windows taskkill /F /T /PID）",
    _tree_ok,
    f"procs={len(_procs)} kills={[p.kills for p in _procs]} "
    f"taskkill={_tree_cmds} os.name={os.name}")


# ── 汇总 ──────────────────────────────────────────────────────────────
failed = [r for r in results if not r[0]]
print("\n" + "=" * 64)
print(f"共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
if failed:
    for _, tag, name, detail in failed:
        print(f"  ✗ {tag} {name}: {detail}")
print("=" * 64)
sys.exit(1 if failed else 0)
