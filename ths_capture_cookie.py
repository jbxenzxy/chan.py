"""
同花顺 Cookie 自动捕获工具
自动从浏览器(Chrome/Edge/Firefox)提取同花顺登录 Cookie 并保存到文件。

用法：
    python ths_capture_cookie.py

前提：
    1. 浏览器已登录 https://t.10jqka.com.cn
    2. 安装依赖：pip install browser_cookie3 requests
"""

import os
import sys
import time
import requests
import json

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
        print("[Cookie] 未安装 browser_cookie3，无法自动提取浏览器 Cookie")
        print("[Cookie] 请执行:  pip install browser_cookie3")
        return ""

    cookie_dict = {}

    # 依次尝试 Chrome、Edge、Firefox
    browsers = [
        ("Chrome", bc3.chrome),
        ("Edge", bc3.edge),
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
                print(f"[Cookie] 从 {name} 浏览器提取到 {count} 条同花顺 Cookie")
                break
        except Exception as e:
            print(f"[Cookie] 从 {name} 提取失败: {e}")
            continue
    else:
        print("[Cookie] 未能从任何浏览器提取到同花顺 Cookie")
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
    print(f"[Cookie] 已保存到: {COOKIE_FILE}")


def manual_input_cookie() -> str:
    """引导用户手动粘贴 Cookie"""
    print("\n" + "=" * 60)
    print("请手动获取 Cookie：")
    print("  1. 浏览器打开 https://t.10jqka.com.cn 并登录同花顺")
    print("  2. F12 → Network → 刷新页面")
    print("  3. 点击任意请求 → Request Headers → 复制 Cookie 值")
    print("  4. 在下面粘贴 Cookie（回车两次结束输入）：")
    print("=" * 60)

    lines = []
    while True:
        try:
            line = input()
        except EOFError:
            break
        if line.strip() == "" and lines:
            break
        lines.append(line)
    return " ".join(lines).strip()


def main():
    print("=" * 60)
    print("同花顺 Cookie 捕获工具")
    print("=" * 60)

    cookie = ""

    # 1. 先尝试从浏览器自动提取
    print("[Cookie] 尝试从浏览器自动提取...")
    cookie = extract_cookie_from_browser()

    if cookie and check_cookie_valid(cookie):
        print("[Cookie] ✅ 浏览器提取的 Cookie 有效")
        save_cookie(cookie)
        print("[Cookie] 登录状态正常，无需额外操作")
        return

    if cookie:
        print("[Cookie] ⚠️ 浏览器提取的 Cookie 无效或已过期")
    else:
        print("[Cookie] 自动提取未获取到 Cookie")

    # 2. 尝试读取已有的旧 Cookie 文件
    old_cookie = load_existing_cookie()
    if old_cookie:
        print("[Cookie] 检测到已有 Cookie 文件，尝试验证...")
        if check_cookie_valid(old_cookie):
            print("[Cookie] ✅ 已有 Cookie 仍然有效")
            return
        else:
            print("[Cookie] ❌ 已有 Cookie 已过期")

    # 3. 引导手动输入
    print("\n[Cookie] 进入手动输入模式...")
    cookie = manual_input_cookie()
    if not cookie:
        print("[Cookie] 未输入 Cookie，退出")
        return

    print("[Cookie] 正在验证手动输入的 Cookie...")
    if check_cookie_valid(cookie):
        print("[Cookie] ✅ Cookie 验证通过")
        save_cookie(cookie)
    else:
        print("[Cookie] ❌ Cookie 验证失败，请确认：")
        print("        - 是否已登录同花顺 https://t.10jqka.com.cn")
        print("        - Cookie 是否复制完整（不要遗漏分号）")


if __name__ == "__main__":
    main()
