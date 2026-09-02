"""
同花顺 Cookie 自动捕获工具
自动从浏览器提取同花顺登录 Cookie 并保存到文件。

用法：
    python Script/ths_capture_cookie.py         # 默认：有人值守登录（PyCharm 直接运行即此），
                                                #       在真实浏览器窗口完成登录/滑块后抓取会话
    python Script/ths_capture_cookie.py --auto  # 旧版：自动提取浏览器 Cookie（无手动粘贴兜底）
    （P2-2 起本工具位于 Script/，Cookie 文件 Script/ths_captured_cookie.txt 随之自动跟随）

说明：
    默认(有人值守登录)采用浏览器自动化(Playwright)，页面原生完成账号密码加密与滑块校验，
            登录成功后从当前会话内存读取 Cookie，不触碰磁盘库，避免 browser_cookie3
            读运行中浏览器(VSS 需管理员 / 新版 Edge app-bound 加密)的根因问题。

前提：
    1. 浏览器已登录 https://t.10jqka.com.cn
    2. 安装依赖：pip install browser_cookie3 requests playwright
    3. --login 模式首次使用需：playwright install chromium（若本机无 Edge）
"""

import os
import sys
import time
import requests
import json

# P2-2：本脚本位于 Script/，仓库根 = 父目录；App/ 包自仓库根导入
script_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.dirname(script_dir)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from App.AppLog import get_logger
log = get_logger(__name__)


COOKIE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ths_captured_cookie.txt")
THS_DOMAIN = "10jqka.com.cn"
THS_API_URL = "https://t.10jqka.com.cn/newcircle/group/getSelfStockWithMarket/"


def _request_with_cookie(cookie: str) -> dict:
    """用给定 Cookie 请求同花顺 API，返回解析后的 JSON"""
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://t.10jqka.com.cn/",
        "Cookie": cookie,
    }
    url = f"{THS_API_URL}?_={int(time.time() * 1000)}"
    try:
        resp = requests.get(url, headers=headers, timeout=15)
        resp.raise_for_status()
        text = resp.text
        # JSONP 解析
        if text.strip().startswith("{"):
            return json.loads(text)
        if "(" in text and text.endswith(");"):
            json_str = text[text.index("(") + 1 : -2]
            return json.loads(json_str)
        return {"errorCode": -1, "errorMsg": "unexpected format"}
    except Exception as e:
        return {"errorCode": -1, "errorMsg": str(e)}


def check_cookie_valid(cookie: str) -> bool:
    """检查 Cookie 是否能使同花顺 API 正常返回"""
    data = _request_with_cookie(cookie)
    return data.get("errorCode") == 0


def extract_cookie_from_browser() -> str:
    """尝试从浏览器自动提取同花顺 Cookie"""
    try:
        import browser_cookie3 as bc3
    except ImportError:
        log.info("[Cookie] 未安装 browser_cookie3，无法自动提取浏览器 Cookie")
        log.info("[Cookie] 请执行:  pip install browser_cookie3")
        return ""

    cookie_dict = {}

    # 仅尝试 Firefox（Chrome/Edge 读取需管理员权限且新版有 app-bound 加密，已移除）
    browsers = [
        ("Firefox", bc3.firefox),
    ]

    for name, loader in browsers:
        try:
            cj = loader(domain_name=THS_DOMAIN)
            count = 0
            for cookie in cj:
                # 只保留同花顺主域名下的关键登录 Cookie
                if THS_DOMAIN in cookie.domain:
                    cookie_dict[cookie.name] = cookie.value
                    count += 1
            if count > 0:
                log.info(f"[Cookie] 从 {name} 浏览器提取到 {count} 条同花顺 Cookie")
                break
        except Exception as e:
            log.info(f"[Cookie] 从 {name} 提取失败: {e}")
            continue
    else:
        log.info("[Cookie] 未能从任何浏览器提取到同花顺 Cookie")
        return ""

    if not cookie_dict:
        return ""

    # 拼接成 Cookie 字符串
    cookie_str = "; ".join(f"{k}={v}" for k, v in cookie_dict.items())
    return cookie_str


def load_existing_cookie() -> str:
    """读取已有的 Cookie 文件"""
    if os.path.exists(COOKIE_FILE):
        with open(COOKIE_FILE, "r", encoding="utf-8") as f:
            return f.read().strip()
    return ""


def save_cookie(cookie: str):
    """保存 Cookie 到文件"""
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(cookie)
    log.info(f"[Cookie] 已保存到: {COOKIE_FILE}")


def _launch_login_browser(p):
    """优先复用本机 Edge；无 Edge 时回退默认 Chromium。
    增加常见反自动化参数，降低被同花顺 WAF 当作爬虫的几率。"""
    args = ["--disable-blink-features=AutomationControlled"]
    try:
        return p.chromium.launch(channel="msedge", headless=False, args=args)
    except Exception as e:
        log.info(f"[Login] Edge 启动失败({e})，回退默认 Chromium...")
        return p.chromium.launch(headless=False, args=args)


def login_with_browser() -> str:
    """有人值守登录：真实浏览器窗口里完成登录/滑块，随后抓取当前会话 Cookie。

    与 browser_cookie3 的区别：读的是 Playwright 当前会话(内存)的 Cookie，
    不触碰磁盘上的 Cookie 数据库，因此不存在 VSS 需管理员 / 新版 Edge app-bound
    加密 / 文件被运行中浏览器锁定的问题。滑块由用户在同一窗口手动完成。
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log.info("[Login] 未安装 playwright，请执行:  pip install playwright")
        return ""

    log.info("[Cookie] 正在打开浏览器窗口，请完成登录...")
    log.info("=" * 60)
    log.info("登录步骤：")
    log.info("  1) 在打开的浏览器窗口输入同花顺账号密码（页面原生完成加密）")
    log.info("  2) 若出现滑块验证码，请拖动一次")
    log.info("  3) 登录成功后脚本会自动检测并继续，无需在控制台操作")
    log.info("     等待超时上限 1 分钟，超时自动退出")
    log.info("=" * 60)

    def build_cookie_str():
        # 只收集同花顺域下的会话 Cookie，拼成请求头字符串
        cookie_map = {}
        for c in context.cookies():
            if THS_DOMAIN in c.get("domain", "") and c.get("name") not in cookie_map:
                cookie_map[c["name"]] = c["value"]
        return "; ".join(f"{k}={v}" for k, v in cookie_map.items())

    with sync_playwright() as p:
        browser = _launch_login_browser(p)
        try:
            # 用真实 Edge UA + 隐藏自动化痕迹，尽量规避 WAF 的爬虫判定
            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
                ),
                locale="zh-CN",
                timezone_id="Asia/Shanghai",
                viewport={"width": 1366, "height": 768},
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            page = context.new_page()
            page.goto("https://t.10jqka.com.cn")

            # 自动检测：轮询浏览器会话，只有当 10jqka 域 Cookie 集合发生变化
            #（即你已登录、新增了会话/鉴权 Cookie）时才联网校验，登录成功即返回。
            deadline = time.time() + 60
            last_sig = None
            last_notice = 0
            while True:
                remaining = deadline - time.time()
                if remaining <= 0:
                    log.info("[Cookie] ⏰ 等待登录超时(1 分钟)，退出")
                    return ""

                cookie_str = build_cookie_str()
                # 以 10jqka 域下的 Cookie(name, domain) 集合作为"是否有变化"的信号
                sig = tuple(
                    sorted(
                        (c["name"], c["domain"])
                        for c in context.cookies()
                        if THS_DOMAIN in c.get("domain", "")
                    )
                )
                if cookie_str and sig and sig != last_sig:
                    last_sig = sig
                    log.info("[Cookie] 检测到登录态变化，正在校验...")
                    if check_cookie_valid(cookie_str):
                        return cookie_str

                if time.time() - last_notice >= 30:
                    last_notice = time.time()
                    log.info(f"[Cookie] 仍在等待登录......（剩余约 {int(remaining)} 秒）")
                time.sleep(3)
        finally:
            try:
                browser.close()
            except Exception:
                pass


def main():
    # 无参数(在 PyCharm 直接运行)默认进入有人值守登录；
    # 需要旧版自动提取(读浏览器 Cookie 盘库)时显式传 --auto
    if "--auto" not in sys.argv[1:]:
        log.info("=" * 60)
        log.info("同花顺 Cookie 捕获工具（有人值守登录）")
        log.info("=" * 60)
        cookie = login_with_browser()
        if cookie and check_cookie_valid(cookie):
            log.info("[Cookie] ✅ 登录获取的 Cookie 有效")
            save_cookie(cookie)
        else:
            log.info("[Cookie] ❌ 登录获取的 Cookie 无效，请确认登录是否完成或账号受限")
        return

    log.info("=" * 60)
    log.info("同花顺 Cookie 捕获工具（--auto 自动提取）")
    log.info("=" * 60)

    cookie = ""

    # 1. 先尝试从浏览器自动提取
    log.info("[Cookie] 尝试从浏览器自动提取...")
    cookie = extract_cookie_from_browser()

    if cookie and check_cookie_valid(cookie):
        log.info("[Cookie] ✅ 浏览器提取的 Cookie 有效")
        save_cookie(cookie)
        log.info("[Cookie] 登录状态正常，无需额外操作")
        return

    if cookie:
        log.info("[Cookie] ⚠️ 浏览器提取的 Cookie 无效或已过期")
    else:
        log.info("[Cookie] 自动提取未获取到 Cookie")
        # 根因提示：浏览器运行时数据库被锁，browser_cookie3 走卷影(VSS)需要管理员；
        # 且新版 Edge 有 app-bound 加密。给明确解法，而不是静默降级。
        log.info("[Cookie] 提示：请改为默认的有人值守登录方式（不加参数直接运行本脚本）")

    # 2. 尝试读取已有的旧 Cookie 文件
    old_cookie = load_existing_cookie()
    if old_cookie:
        log.info("[Cookie] 检测到已有 Cookie 文件，尝试验证...")
        if check_cookie_valid(old_cookie):
            log.info("[Cookie] ✅ 已有 Cookie 仍然有效")
            return
        else:
            log.info("[Cookie] ❌ 已有 Cookie 已过期")

    # 提取失败也没有有效旧 Cookie，直接退出（不支持手动粘贴）
    log.info("[Cookie] ❌ 未获取到有效的同花顺 Cookie")
    log.info("[Cookie] 提示：请改用默认的有人值守登录方式（不加参数直接运行本脚本）")
    return


if __name__ == "__main__":
    main()
