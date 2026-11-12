# -*- coding: utf-8 -*-
"""DataAPI/ThsCookieSource.py —— 同花顺 Cookie 自动供给（唯一事实源）
=========================================================================
「保存到自选」写云端自选股需要一份**已登录**的 Cookie。历史流程要人工三步：
Firefox 打开 https://t.10jqka.com.cn 登录 → 保持登录态 → 运行
`Script/ths_capture_cookie.py` 落盘。本模块把这三步收进后台，调用方只问
`ensure_cookie()`，不再关心 Cookie 从哪来。

取 Cookie 的三级通道（按成本从低到高）
---------------------------------------------------------------------
  优先级与「是否联网校验」是两件事，口径统一收在 `resolve_cookie()`：
  环境变量 `THS_COOKIE` → 缓存文件 → 本机浏览器 → 登录窗口。`validate=False`
  只解析不校验（`CThsCloudZxg` 构造用，零网络副作用），`validate=True` 则每个
  通道都要当场 probe 通过才算命中（`ensure_cookie` 对外承诺「拿到即可用」）。
  ① 缓存：`Script/ths_captured_cookie.txt`（含历史位置兼容），环境变量
     `THS_COOKIE` 优先；命中且**联网校验通过**直接返回，零交互；
  ② 浏览器：后台读本机**已登录**浏览器的同花顺 Cookie（Firefox / Chrome /
     Edge / Chromium，顺序可经 `THS_COOKIE_BROWSERS` 覆盖），命中即写回 ①；
  ③ 登录窗口：①② 都不可用时弹出一次同花顺登录页（Playwright + 本机 Edge），
     登录成功即写回 ①，会话状态留存 `Script/ths_storage_state.json`。
     ③ 由**子进程**执行（`Script/ths_capture_cookie.py --login`）：Playwright
     同步 API 不进入 uvicorn worker 线程，且卡死可 kill，不留不可回收线程。
     可用 `THS_AUTO_LOGIN=0` 关闭、`THS_LOGIN_TIMEOUT` 调整等待上限；
     失败后有 10 分钟冷却，避免每点一次保存就弹一次窗。

实测数据（2026-11-11，本机，供后来者判断取舍）
---------------------------------------------------------------------
  · browser_cookie3 读 Firefox：0.02s / 33 条 / errorCode=0（142 只自选）
  · 读 Chrome：0.04s / 20 条 / 已过期（errorCode=-1）；读 Edge：需管理员
    （app-bound 加密，RequiresAdminError）→ 必然失败，故排在最后
  · Playwright(channel="msedge", headless=True)：迁入 Cookie 后页内 fetch
    errorCode=0，全流程 1.4s；本机 playwright 期望 chromium-1254 但缓存只有
    1234/1243 → **必须走 msedge/chrome 通道**，不能依赖自带 chromium
  · 服务端**不下发** Set-Cookie（无滑动续期）：登录票据固定 7~8 天到期
    （ticket / utk / user / userid 剩 7 天，u_ukey / u_dpass 剩 8 天）。
    到期后仍需登录一次——这是同花顺服务端会话策略，程序无法代劳（要账号/扫码），
    能省掉的是「开网页 + 跑脚本」这些手工步骤。
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

log = logging.getLogger(__name__)

# ── 常量 ──────────────────────────────────────────────────────────────
THS_DOMAIN = "10jqka.com.cn"
LOGIN_URL = "https://t.10jqka.com.cn/"
API_PATH = "/newcircle/group/getSelfStockWithMarket/"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0")

PROBE_TIMEOUT = 8.0             # 单次联网校验超时（秒）
VALIDATE_TTL = 120.0            # 同一份 Cookie 的校验结论缓存时长（秒）
INTERACTIVE_COOLDOWN = 600.0    # 交互登录失败后的冷却（秒）
DEFAULT_LOGIN_TIMEOUT = 90.0    # 登录窗口等待上限（秒）

REACH_HOST = "t.10jqka.com.cn"  # 网络可达性探测目标（与 LOGIN_URL 同域）
REACH_TIMEOUT = 3.0             # 可达性探测**总时长**上限（秒，含 DNS 解析）
REACH_TTL = 60.0                # 可达性结论缓存时长（秒）：一次保存最多两次兜底

# `details` 里**算数**的前缀 = 「真的试过并失败了」的通道。判据只认这三类，
# 其余（`note:` 说明行、`network:` 探测结论）一律不参与 —— 见 `ensure_cookie`。
_COOKIE_ATTEMPT_PREFIXES = ("env", "file", "browser")

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_cache_lock = threading.Lock()
_probe_cache = {}               # cookie -> (ok, msg, monotonic_ts)
_reach_lock = threading.Lock()
_reach_cache = {}               # host -> (reachable, msg, monotonic_ts)
_login_lock = threading.Lock()
_last_login_fail = 0.0


class ThsCookieUnavailable(RuntimeError):
    """所有通道都拿不到可用的同花顺 Cookie。

    `details` 保留每条通道的失败原因，便于日志定位；消息本体对外展示，
    保持单行、可直接落到扫描面板的 `ths_msg`。
    """

    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = list(details or [])


# ═══════════════════════════════════════════════════════════════════════
# 路径与文件读写
# ═══════════════════════════════════════════════════════════════════════

def repo_root():
    return _REPO_ROOT


def cookie_file():
    """Cookie 落盘位置（规范位置 = Script/ths_captured_cookie.txt）。"""
    return os.path.join(_REPO_ROOT, "Script", "ths_captured_cookie.txt")


def state_file():
    """浏览器会话状态（含 Cookie，**不入库**，见 .gitignore）。"""
    return os.path.join(_REPO_ROOT, "Script", "ths_storage_state.json")


def candidate_cookie_files():
    """Cookie 文件的查找顺序：Script/（惯例）→ DataAPI/ → 仓库根（阶段 2 前）。"""
    return [
        os.path.join(_REPO_ROOT, "Script", "ths_captured_cookie.txt"),
        os.path.join(_REPO_ROOT, "DataAPI", "ths_captured_cookie.txt"),
        os.path.join(_REPO_ROOT, "ths_captured_cookie.txt"),
    ]


def read_cookie_file(path=None):
    """读取 Cookie 文本；`path` 为空时按 `candidate_cookie_files()` 逐个尝试。"""
    for p in ([path] if path else candidate_cookie_files()):
        try:
            with open(p, "r", encoding="utf-8") as f:
                value = f.read().strip()
        except OSError:
            continue
        if value:
            return value
    return ""


def write_cookie_file(cookie, path=None):
    """原子写 Cookie（同目录 mkstemp → os.replace，避免读到半截内容）。

    临时名必须**唯一**：并发 `ensure_cookie` 同时命中浏览器通道时，固定
    `.tmp` 名会让两个写手互相覆盖（`_login_lock` 只保护登录窗口那条路）。
    """
    cookie = (cookie or "").strip()
    if not cookie:
        return False
    path = path or cookie_file()
    tmp_fd = None
    tmp_path = None
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=os.path.dirname(path),
            prefix=os.path.basename(path) + ".",
            suffix=".tmp")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            tmp_fd = None                      # 已交给文件对象
            f.write(cookie)
        os.replace(tmp_path, path)
        tmp_path = None
        return True
    except OSError as e:
        log.info("[THS-Cookie] 写入 %s 失败：%s", path, e)
        return False
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ═══════════════════════════════════════════════════════════════════════
# Cookie 串 ↔ 键值对
# ═══════════════════════════════════════════════════════════════════════

def build_cookie_string(pairs):
    """[(name, value), ...] → "a=1; b=2"（同名保留**最后**一条，与历史实现一致）。"""
    merged = {}
    for name, value in pairs:
        if name:
            merged[name] = value if value is not None else ""
    return "; ".join(f"{k}={v}" for k, v in merged.items())


def parse_jsonp(text, callback_name=None):
    """解析同花顺返回的 JSON / JSONP，失败返回 errorCode=-1 的 dict。

    实测三种返回形态都要认：`{...}`、`selfStock({...})`、`selfStock({...});`
    """
    if not text:
        return {"errorCode": -1, "errorMsg": "empty response"}
    body = text.strip()
    try:
        if body.startswith("{"):
            return json.loads(body)
        if callback_name:
            m = re.search(rf"{re.escape(callback_name)}\((.*)\)\s*;?\s*$", body, re.DOTALL)
            if m:
                return json.loads(m.group(1))
        # 通用 JSONP：callback(...) 或 callback(...);
        if "(" in body and body.rstrip().rstrip(";").rstrip().endswith(")"):
            return json.loads(body[body.index("(") + 1: body.rindex(")")])
    except Exception as e:                      # noqa: BLE001 —— 解析失败统一降级
        return {"errorCode": -1, "errorMsg": f"parse error: {e}"}
    return {"errorCode": -1, "errorMsg": "unexpected format"}


# ═══════════════════════════════════════════════════════════════════════
# 联网校验（带 TTL 缓存）
# ═══════════════════════════════════════════════════════════════════════

def probe(cookie, timeout=PROBE_TIMEOUT):
    """用给定 Cookie 请求自选股接口，返回 (是否登录有效, 说明)。

    失败原因分三类，`ensure_cookie` 据此决定要不要弹登录窗口：
      · ``网络错误:`` —— 连接失败 / 超时 / DNS。网络本身不通，弹窗用户也登不进去，
        故「所有通道都是网络错误」时跳过登录窗口；
      · ``服务端拒绝:`` —— HTTP 4xx/5xx（WAF 拦截 / 429 限流 / 5xx）。网络是通的，
        Cookie 该换就换，**不能**归进网络错误，否则该弹的窗口被静默跳过；
      · ``校验异常:`` —— 其它意外（重定向过多 / URL 非法…），同样不算网络不可达。
    """
    if not cookie:
        return False, "空 Cookie"
    try:
        import requests
    except ImportError:
        return False, "未安装 requests"
    url = f"https://t.10jqka.com.cn{API_PATH}"
    headers = {"User-Agent": USER_AGENT, "Referer": LOGIN_URL, "Cookie": cookie}
    try:
        resp = requests.get(url, params={"_": int(time.time() * 1000)},
                            headers=headers, timeout=timeout)
        resp.raise_for_status()
        data = parse_jsonp(resp.text)
    except requests.exceptions.HTTPError as e:
        status = getattr(getattr(e, "response", None), "status_code", "?")
        return False, f"服务端拒绝: HTTP {status}"
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        return False, f"网络错误: {type(e).__name__}: {e}"
    except Exception as e:                      # noqa: BLE001 —— 其它异常不算网络不可达
        return False, f"校验异常: {type(e).__name__}: {e}"
    if data.get("errorCode") == 0:
        return True, f"OK（自选 {len(data.get('result') or [])} 只）"
    return False, str(data.get("errorMsg") or f"errorCode={data.get('errorCode')}")


def probe_cached(cookie, ttl=VALIDATE_TTL, timeout=PROBE_TIMEOUT):
    """`probe()` 的缓存版：`ttl=0` 表示强制重新校验。"""
    if not cookie:
        return False, "空 Cookie"
    now = time.monotonic()
    if ttl > 0:
        with _cache_lock:
            hit = _probe_cache.get(cookie)
        if hit and now - hit[2] < ttl:
            return hit[0], hit[1]
    ok, msg = probe(cookie, timeout=timeout)
    with _cache_lock:
        if len(_probe_cache) > 8:
            _probe_cache.clear()
        _probe_cache[cookie] = (ok, msg, now)
    return ok, msg


def invalidate(cookie=None):
    """清空校验缓存（不传参数 = 全清）。"""
    with _cache_lock:
        if cookie is None:
            _probe_cache.clear()
        else:
            _probe_cache.pop(cookie, None)


# ═══════════════════════════════════════════════════════════════════════
# 网络可达性（不需要 Cookie，只看"连接通不通"）
# ═══════════════════════════════════════════════════════════════════════

def _reach_once(host, timeout):
    """真正发一次 HEAD；返回 (可达, 说明裸文本)。

    说明文本**不带「可达/不可达」标签**，由调用方统一加（避免
    `network: 可达（可达（HTTP 200））` 这种套娃）。

    判据方向刻意不对称：**只有连接失败/超时才算不可达**，HTTP 4xx/5xx、
    TLS 告警、缺依赖、未知异常一律按可达处理。理由是该结论只用于「要不要
    弹登录窗口」——把可达误判成不可达会让用户永久看不到登录入口（死路），
    把不可达误判成可达只是多等一个超时（可恢复），代价不对等。
    """
    try:
        import requests
    except ImportError:
        return True, "未安装 requests，按可达处理"
    try:
        resp = requests.head(f"https://{host}/", timeout=(timeout, timeout),
                             allow_redirects=False,
                             headers={"User-Agent": USER_AGENT})
        return True, f"HTTP {resp.status_code}"
    except requests.exceptions.SSLError as e:
        # SSLError 是 ConnectionError 的子类，必须排在它前面：握手都到 TLS 了，
        # 说明链路是通的（证书过期/中间人拦截不该让我们判定"网络不可达"）。
        return True, f"TLS 异常: {type(e).__name__}"
    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
        return False, f"{type(e).__name__}: {e}"
    except Exception as e:                      # noqa: BLE001 —— 未知异常不算网络不可达
        return True, f"未知异常按可达处理: {type(e).__name__}"


def network_reachable(host=REACH_HOST, timeout=REACH_TIMEOUT, ttl=REACH_TTL):
    """判断到同花顺服务端的网络是否可达；带 TTL 缓存。

    **不复用 `probe()`**：空 Cookie 会被 `probe()` 直接短路成
    `(False, "空 Cookie")`，那既不是「网络错误」也不是「有响应」，拿它判
    可达性只会得到假阴性（详见 `ensure_cookie` 的零证据分支）。这里用
    HEAD 探测，不依赖任何 Cookie。

    三个实现约束（都有实测背景）：
      · **走 requests 而不是裸 socket** —— 与 `probe()` 共享 `HTTP(S)_PROXY`
        口径。裸 socket 在「直连被封、代理可用」的环境会判成不可达，于是
        本该弹的登录窗口被静默跳过，与 `probe()` 的结论互相打架；
      · **daemon 线程 + `join(timeout)` 定总时长** —— socket 的 timeout 不覆盖
        `getaddrinfo`，DNS 悬停（断网 / 企业 DNS 无响应）时"3s 超时"可能实际
        卡十几秒，守卫自己变成新的等待点；
      · 缺 `requests` / 未知异常 → 按可达处理（见 `_reach_once` 的不对称判据）。
    """
    now = time.monotonic()
    if ttl > 0:
        with _reach_lock:
            hit = _reach_cache.get(host)
        if hit and now - hit[2] < ttl:
            return hit[0], hit[1]

    box = {}

    def _run():
        box["res"] = _reach_once(host, timeout)

    th = threading.Thread(target=_run, name="ths-reach", daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        result = (False, f"探测超时（>{timeout:.0f}s 无响应，DNS 或连接阻塞）")
    else:
        result = box.get("res") or (True, "探测无结果按可达处理")

    with _reach_lock:
        if len(_reach_cache) > 8:
            _reach_cache.clear()
        _reach_cache[host] = (result[0], result[1], now)
    return result


# ═══════════════════════════════════════════════════════════════════════
# 通道 ②：本机已登录浏览器
# ═══════════════════════════════════════════════════════════════════════

def browser_order():
    """浏览器尝试顺序；`THS_COOKIE_BROWSERS=firefox,chrome` 可覆盖。"""
    raw = (os.environ.get("THS_COOKIE_BROWSERS") or "").strip()
    if raw:
        return [x.strip() for x in raw.split(",") if x.strip()]
    # Edge 排最后：Windows 上 app-bound 加密读 Cookie 需要管理员，失败还要
    # 白等 6s（实测），前面的 Firefox/Chrome 已覆盖绝大多数场景。
    return ["firefox", "chrome", "edge", "chromium"]


def iter_browser_cookies():
    """按优先级产出 (浏览器名, Cookie 串, 条数)；单个浏览器失败只记日志。"""
    try:
        import browser_cookie3 as bc3
    except ImportError:
        log.info("[THS-Cookie] 未安装 browser_cookie3，跳过浏览器通道")
        return
    for name in browser_order():
        loader = getattr(bc3, name, None)
        if loader is None:
            continue
        t0 = time.monotonic()
        try:
            jar = loader(domain_name=THS_DOMAIN)
        except Exception as e:                  # noqa: BLE001 —— 任一浏览器失败不影响其它
            log.info("[THS-Cookie] 读取 %s 失败（%s，%.1fs）",
                     name, type(e).__name__, time.monotonic() - t0)
            continue
        pairs = [(c.name, c.value or "") for c in jar if THS_DOMAIN in (c.domain or "")]
        if not pairs:
            continue
        yield name, build_cookie_string(pairs), len(pairs)


# ═══════════════════════════════════════════════════════════════════════
# 通道 ③：登录窗口（Playwright，子进程执行）
# ═══════════════════════════════════════════════════════════════════════

def interactive_allowed():
    """是否允许弹登录窗口（`THS_AUTO_LOGIN=0` 关闭；失败后有冷却）。"""
    flag = (os.environ.get("THS_AUTO_LOGIN") or "1").strip().lower()
    if flag in ("0", "false", "no", "off"):
        return False
    return time.monotonic() - _last_login_fail >= INTERACTIVE_COOLDOWN


def _launch_browser(p, headless):
    """本机 Edge → Chrome → 自带 chromium（本机 chromium 未下载，实测必失败）。"""
    channel = (os.environ.get("THS_BROWSER_CHANNEL") or "").strip()
    tried = []
    for ch in ([channel] if channel else []) + ["msedge", "chrome", None]:
        kwargs = {"headless": headless,
                  "args": ["--disable-blink-features=AutomationControlled"]}
        if ch:
            kwargs["channel"] = ch
        try:
            return p.chromium.launch(**kwargs)
        except Exception as e:                  # noqa: BLE001 —— 换下一个通道
            tried.append(f"{ch or 'chromium'}:{type(e).__name__}")
    raise ThsCookieUnavailable(
        "无法启动浏览器（" + "; ".join(tried) + "）：请安装 Edge/Chrome，"
        "或执行 playwright install chromium")


def interactive_login(timeout=DEFAULT_LOGIN_TIMEOUT, headless=False):
    """弹出同花顺登录页，等登录完成后把 Cookie 写回文件并返回；超时返回 ""。

    读的是**当前会话内存**里的 Cookie（不碰浏览器磁盘库），因此不存在
    Firefox 被占用 / Edge app-bound 加密那类问题。

    登录成功的判定信号 = 「同域 Cookie 的 (name, domain, value) 集合发生变化」。
    含 value 是必须的：`new_context(storage_state=...)` 会带进旧 Cookie，首轮
    信号即非空并把 `last_sig` 锁定；用户登录时往往**只换同一批 Cookie 的值**
    （名字/域不变），只看 (name, domain) 就恒等、永远检测不到 —— 表现为登录
    窗口等到超时、随即进入 10 分钟失败冷却。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise ThsCookieUnavailable("未安装 playwright：pip install playwright")

    state = state_file()
    log.info("[THS-Cookie] 正在打开同花顺登录窗口，请扫码或输入账号（等待上限 %ds）…", int(timeout))
    with sync_playwright() as p:
        browser = _launch_browser(p, headless=headless)
        context = None
        try:
            kwargs = {
                "user_agent": USER_AGENT,
                "locale": "zh-CN",
                "timezone_id": "Asia/Shanghai",
                "viewport": {"width": 1366, "height": 768},
            }
            if os.path.exists(state):
                try:
                    context = browser.new_context(storage_state=state, **kwargs)
                except Exception as e:          # noqa: BLE001 —— 状态文件坏掉就重建
                    log.info("[THS-Cookie] 会话状态不可用（%s），改用干净会话", e)
            if context is None:
                context = browser.new_context(**kwargs)
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});")
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)

            deadline = time.monotonic() + timeout
            last_sig = None
            while time.monotonic() < deadline:
                cookies = context.cookies()
                sig = tuple(sorted((c.get("name"), c.get("domain"),
                                    c.get("value") or "")
                                   for c in cookies if THS_DOMAIN in (c.get("domain") or "")))
                if sig and sig != last_sig:
                    last_sig = sig
                    cookie = build_cookie_string(
                        [(c.get("name"), c.get("value") or "") for c in cookies
                         if THS_DOMAIN in (c.get("domain") or "")])
                    if cookie:
                        ok, msg = probe_cached(cookie, ttl=0)
                        if ok:
                            write_cookie_file(cookie)
                            try:
                                context.storage_state(path=state)
                            except Exception as e:      # noqa: BLE001 —— 状态留存失败不影响 Cookie
                                log.info("[THS-Cookie] 会话状态留存失败：%s", e)
                            log.info("[THS-Cookie] ✅ 登录成功，Cookie 已写入 %s（%s）",
                                     cookie_file(), msg)
                            return cookie
                time.sleep(2)
            log.info("[THS-Cookie] ⏰ 等登录超时（%ds）", int(timeout))
            return ""
        finally:
            try:
                browser.close()
            except Exception:                   # noqa: BLE001 —— 关闭失败无需上抛
                pass


def ensure_interactive_login(timeout=DEFAULT_LOGIN_TIMEOUT):
    """弹一次登录窗口（子进程），返回可用 Cookie；失败/超时返回 ""。

    走子进程的两个理由：
      · Playwright 同步 API 不进 uvicorn 的 worker 线程，避免事件循环冲突；
      · 卡死/浏览器崩溃可被 kill —— 不留回收不了的线程（指数归属那次的教训）。
    冻结版（PyInstaller）没有 .py 可跑，退化为进程内调用。
    """
    global _last_login_fail
    if not _login_lock.acquire(blocking=False):
        log.info("[THS-Cookie] 已有登录窗口在等待，跳过本次")
        return ""
    try:
        script = os.path.join(_REPO_ROOT, "Script", "ths_capture_cookie.py")
        if getattr(sys, "frozen", False) or not os.path.exists(script):
            cookie = interactive_login(timeout=timeout)
        else:
            cookie = _login_via_subprocess(script, timeout)
        if not cookie:
            _last_login_fail = time.monotonic()
        return cookie
    finally:
        _login_lock.release()


def _login_via_subprocess(script, timeout):
    cmd = [sys.executable, script, "--login", "--timeout", str(int(timeout))]
    if (os.environ.get("THS_LOGIN_HEADLESS") or "").strip().lower() in ("1", "true", "yes"):
        cmd.append("--headless")            # 无 GUI 环境（服务器/CI）不显示窗口
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)   # 只隐藏子进程控制台，浏览器窗口照常弹出
    try:
        proc = subprocess.Popen(cmd, cwd=_REPO_ROOT, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, creationflags=flags)
    except OSError as e:
        log.info("[THS-Cookie] 启动登录子进程失败（%s），改在进程内登录", e)
        return interactive_login(timeout=timeout)
    try:
        out, _ = proc.communicate(timeout=timeout + 15)
    except subprocess.TimeoutExpired:
        log.info("[THS-Cookie] 登录子进程超时，终止")
        proc.kill()
        try:
            proc.communicate(timeout=10)
        except Exception:                       # noqa: BLE001
            pass
        return ""
    tail = (out or b"").decode("utf-8", "replace").strip().splitlines()[-5:]
    log.info("[THS-Cookie] 登录子进程退出码=%s%s", proc.returncode,
             ("；末行：" + " | ".join(tail)) if tail else "")
    cookie = read_cookie_file()
    if not cookie:
        return ""
    ok, _msg = probe_cached(cookie, ttl=0)
    return cookie if ok else ""


# ═══════════════════════════════════════════════════════════════════════
# 对外唯一入口
# ═══════════════════════════════════════════════════════════════════════

def resolve_cookie(validate=False, force=False, timeout=PROBE_TIMEOUT,
                   skip_browser=False):
    """按**统一优先级**解析一份 Cookie：环境变量 → 文件 → 本机浏览器。

    优先级只有这一处定义：面板保存链路（`ensure_cookie`）与同步脚本
    （`CThsCloudZxg.__init__`）都从这里取，避免两条链路口径相反 ——
    旧实现 `__init__` 是「文件优先且完全不读 THS_COOKIE」，同时存在环境变量
    （有效）与旧 Cookie 文件（已过期）时，脚本会用旧文件并提示"请更新 Cookie"。

    `validate` 决定**要不要当场联网校验**，与优先级无关：
      · True  —— 每个通道都必须 probe 通过才算命中（`ensure_cookie` 用）；
      · False —— 只看内容非空（构造函数用：`CThsCloudZxg(...)` 是纯配置动作，
        不该在构造期产生网络副作用，也不该阻塞数十秒）。
    只解析、不落盘：`validate=False` 命中浏览器通道时**不**写回 Cookie 文件
    （避免把「构造一个对象」变成写操作）。

    `skip_browser=True` 跳过「本机浏览器」通道：调用方在**同一请求内**刚扫过
    一轮浏览器时（`save_scan_to_ths_cloud` 的兜底与失效重取），重复扫拿不到
    新结果，却要为每个浏览器各付一次「读取 + `PROBE_TIMEOUT` 校验」（Edge
    通道在 Windows 上最坏 6s，四通道全失败时最坏数十秒）。跳过会在 `details`
    留一条 `note:` 说明，诊断时可区分「本机确实没有登录态」与「本轮主动跳过」。

    返回 (cookie, source, details)；全通道未命中时 cookie 为空串，
    `details` 逐条记录每个通道的失败原因。**只有 `env:` / `file:` /
    `browser:` 前缀是「真的试过并失败」**；`note:` 前缀是说明，不是证据 ——
    `ensure_cookie` 的判据按前缀白名单取，不按关键字黑名单剔。
    """
    ttl = 0 if force else VALIDATE_TTL
    details = []

    env_cookie = (os.environ.get("THS_COOKIE") or "").strip()
    file_cookie = read_cookie_file()
    for cookie, source in ((env_cookie, "env"), (file_cookie, "file")):
        if not cookie:
            continue
        if not validate:
            log.info("[THS-Cookie] 使用%s中的 Cookie（未校验）",
                     "环境变量" if source == "env" else "缓存文件")
            return cookie, source, details
        ok, msg = probe_cached(cookie, ttl=ttl, timeout=timeout)
        if ok:
            log.info("[THS-Cookie] 使用%s中的 Cookie（%s）",
                     "环境变量" if source == "env" else "缓存文件", msg)
            return cookie, source, details
        details.append(f"{source}: {msg}")
        log.info("[THS-Cookie] %s 通道的 Cookie 不可用：%s", source, msg)

    if skip_browser:
        details.append("note: browser 通道跳过（同一请求内已扫过一轮，不重复读取）")
    else:
        for name, cookie, count in iter_browser_cookies():
            if not validate:
                log.info("[THS-Cookie] 从 %s 取到 Cookie（%d 条，未校验）", name, count)
                return cookie, f"browser:{name}", details
            ok, msg = probe_cached(cookie, ttl=0, timeout=timeout)
            if ok:
                write_cookie_file(cookie)
                log.info("[THS-Cookie] ✅ 从 %s 取到有效 Cookie（%d 条，%s），已写回 %s",
                         name, count, msg, cookie_file())
                return cookie, f"browser:{name}", details
            details.append(f"browser:{name}: {msg}")
            log.info("[THS-Cookie] 浏览器 %s 的 Cookie 不可用：%s", name, msg)
    return "", "", details


def ensure_cookie(force=False, allow_interactive=False, login_timeout=DEFAULT_LOGIN_TIMEOUT,
                  skip_browser=False):
    """取一份**当场校验可用**的 Cookie，返回 (cookie, 来源)。

    来源取值：`env` / `file` / `browser:<浏览器>` / `login`。
    `force=True` 表示跳过校验缓存（调用方已发现 Cookie 不好使）。
    通道顺序由 `resolve_cookie()` 统一给出（env → file → 浏览器 → 登录窗口）；
    全通道失败抛 `ThsCookieUnavailable`。
    `skip_browser=True` 原样透传（同一请求内已扫过浏览器，不重复扫）。

    `allow_interactive=True` 时才考虑弹窗，且**网络不可达一律不弹**：
    Cookie 通道全报网络错误是直接证据；一条通道都没试过时用
    `network_reachable()` 补一次探测（不能默认"可弹"，否则新机器断网仍白等）。
    """
    cookie, source, details = resolve_cookie(validate=True, force=force,
                                             skip_browser=skip_browser)
    if cookie:
        return cookie, source

    # 决定要不要弹登录窗口的唯一判据：**网络是不是根本不通**。
    # 两道证据，任一条成立即判不可达（网断时用户登不进去，只会白等一个超时）：
    #
    # ① Cookie 通道自证：真试过的通道（env / file / browser 前缀）**全部**
    #    报「网络错误」。这里必须按前缀白名单取，不能按关键字黑名单剔 ——
    #    `details` 里混着 `note:` 说明行（如「browser 跳过」），旧写法
    #    `all("网络错误" in d for d in details)` 把说明行当证据，只要出现一条
    #    说明行就判成"非全网络错误"，于是网断时照样弹窗白等 90s（P2-1）。
    #    黑名单的毛病是每加一条说明/证据行就要再破一次，白名单天然免疫。
    # ② 一条通道都没试过（全新部署 / 从未抓过 Cookie / 本轮跳过浏览器）：
    #    `details` 里只有说明行，**零证据**，不能因为"没有反例"就放行弹窗。
    #    补一次独立可达性探测 —— 不能复用 `probe()`：空 Cookie 会被它短路成
    #    `(False, "空 Cookie")`，既不是网络错误也算不上响应，判不出可达性。
    attempts = [d for d in details
                if d.split(":", 1)[0].strip() in _COOKIE_ATTEMPT_PREFIXES]

    network_only = False
    if allow_interactive:
        if attempts:
            network_only = all("网络错误" in d for d in attempts)
        else:
            reachable, reach_msg = network_reachable()
            network_only = not reachable
            details.append(f"network: {'可达' if reachable else '不可达'}（{reach_msg}）")

    if allow_interactive and not network_only:
        if interactive_allowed():
            cookie = ensure_interactive_login(timeout=login_timeout)
            if cookie:
                return cookie, "login"
            details.append("login: 登录窗口未在时限内完成")
        else:
            details.append("login: 已关闭或处于失败冷却期（THS_AUTO_LOGIN / 10 分钟）")
    elif allow_interactive:
        details.append("login: 网络不可达，跳过登录窗口")

    raise ThsCookieUnavailable(
        "未取到可用的同花顺登录态（缓存文件 / 环境变量 / 本机浏览器"
        + ("/ 登录窗口" if allow_interactive else "") + " 均未命中）",
        details)


def status():
    """诊断各通道现状，供 `ths_capture_cookie.py --status` 使用。"""
    result = {"file": {"path": cookie_file(), "exists": os.path.exists(cookie_file()),
                       "ok": False, "msg": "文件不存在"}}
    file_cookie = read_cookie_file()
    if file_cookie:
        ok, msg = probe(file_cookie)
        result["file"].update(ok=ok, msg=msg, bytes=len(file_cookie))
    result["browsers"] = []
    for name, cookie, count in iter_browser_cookies():
        ok, msg = probe(cookie)
        result["browsers"].append({"name": name, "count": count, "ok": ok, "msg": msg})
    result["playwright"] = _playwright_ready()
    return result


def _playwright_ready():
    try:
        import playwright  # noqa: F401
    except ImportError:
        return "未安装"
    return "已安装（内核 msedge/chrome 优先）"
