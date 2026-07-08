"""
同花顺 Cookie 自动捕获工具（Edge 版）
三种方式：
  1. webdriver-manager 自动下载 EdgeDriver → Selenium 自动捕获
  2. browser-cookie3 直接从 Edge 本地数据库读取
  3. 手动粘贴 Cookie（100% 可靠的后备方案）

用法：
    python ths_capture_cookie.py
"""

import os
import time
import json
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
COOKIE_FILE = os.path.join(SCRIPT_DIR, "ths_captured_cookie.txt")
LOGIN_URL = "https://upass.10jqka.com.cn/login"
TARGET_URL = "https://t.10jqka.com.cn"


def method_selenium_webdriver_manager():
    """使用 webdriver-manager 自动管理 EdgeDriver"""
    try:
        from selenium import webdriver
        from selenium.webdriver.edge.options import Options
        from selenium.webdriver.edge.service import Service
        from webdriver_manager.microsoft import EdgeChromiumDriverManager
    except ImportError as e:
        print(f"缺少依赖: {e}")
        print("请运行: pip install selenium webdriver-manager --break-system-packages")
        return False

    print("=" * 60)
    print("  同花顺 Cookie 自动捕获（Edge）")
    print("=" * 60)
    print()
    print("即将打开 Edge 浏览器，请在浏览器中登录同花顺。")
    print("登录成功后自动捕获 Cookie，无需任何操作。")
    print("=" * 60)
    print()

    options = Options()
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_experimental_option("excludeSwitches", ["enable-automation"])

    driver = None
    try:
        print("[1/3] 自动下载/匹配 EdgeDriver...")
        service = Service(EdgeChromiumDriverManager().install())
        driver = webdriver.Edge(service=service, options=options)
        driver.execute_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"
        )

        print("[2/3] 打开登录页面，请在浏览器中登录...")
        driver.get(LOGIN_URL)
        time.sleep(2)

        max_wait = 300
        check_interval = 2
        for i in range(max_wait // check_interval):
            time.sleep(check_interval)
            try:
                url = driver.current_url
                if "upass" not in url and "login" not in url.lower():
                    break
                if "退出" in driver.page_source:
                    break
            except Exception:
                pass
            if i % 10 == 0:
                print(f"   等待中... ({(i+1)*check_interval}s)")

        print("[3/3] 登录成功，提取 Cookie...")
        driver.get(TARGET_URL)
        time.sleep(3)
        cookies = driver.get_cookies()

        if not cookies:
            print("❌ 未获取到 Cookie")
            return False

        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in cookies)
        with open(COOKIE_FILE, "w", encoding="utf-8") as f:
            f.write(cookie_str)

        print(f"✅ 成功！{len(cookies)} 个 Cookie → {COOKIE_FILE}")
        return True

    except Exception as e:
        print(f"❌ Selenium 方式失败: {e}")
        return False
    finally:
        if driver:
            try:
                driver.quit()
            except Exception:
                pass


def method_browser_cookie3():
    """从 Edge 本地数据库直接读取 Cookie（无需开浏览器）"""
    try:
        import browser_cookie3
    except ImportError:
        print("缺少 browser-cookie3，请运行: pip install browser-cookie3 --break-system-packages")
        return False

    print("=" * 60)
    print("  从 Edge 本地读取 Cookie")
    print("=" * 60)
    print("前提：你之前用 Edge 登录过同花顺且未清除 Cookie。")
    print()

    domains = ["10jqka.com.cn", "t.10jqka.com.cn", "upass.10jqka.com.cn"]
    all_cookies = []

    for domain in domains:
        try:
            cj = browser_cookie3.edge(domain_name=domain)
            cookies = list(cj)
            if cookies:
                all_cookies.extend(cookies)
                print(f"  从 {domain} 获取 {len(cookies)} 个 Cookie")
        except Exception as e:
            print(f"  {domain}: {e}")

    if not all_cookies:
        print("❌ 未找到同花顺 Cookie，请先在 Edge 中登录 https://t.10jqka.com.cn")
        return False

    cookie_str = "; ".join(f"{c.name}={c.value}" for c in all_cookies)
    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(cookie_str)

    print(f"✅ 成功！共 {len(all_cookies)} 个 Cookie → {COOKIE_FILE}")
    return True


def method_manual_input():
    """兜底方案：让用户手动粘贴 Cookie"""
    print("=" * 60)
    print("  手动输入 Cookie")
    print("=" * 60)
    print()
    print("获取步骤：")
    print("  1. Edge 打开 https://t.10jqka.com.cn 并登录")
    print("  2. 按 F12 → Application → Cookies → 10jqka.com.cn")
    print("  3. 或者 F12 → Network → 刷新 → 点击任意请求 →")
    print("     Request Headers → 复制 Cookie 整行值")
    print()
    print("将 Cookie 粘贴到下方（粘贴后按 Enter，然后按 Ctrl+Z 再按 Enter）：")
    print("-" * 60)

    lines = []
    print("请输入 Cookie（支持多行粘贴，输入空行结束）：")
    while True:
        try:
            line = input()
            if not line.strip():
                break
            lines.append(line.strip())
        except EOFError:
            break

    cookie_str = " ".join(lines)
    # 去掉可能带入的 "Cookie: " 前缀
    cookie_str = cookie_str.replace("Cookie: ", "").replace("cookie: ", "").strip()

    if not cookie_str or len(cookie_str) < 20:
        print("❌ Cookie 太短或为空，请重试。")
        return False

    with open(COOKIE_FILE, "w", encoding="utf-8") as f:
        f.write(cookie_str)

    print(f"✅ Cookie 已保存到 {COOKIE_FILE}（{len(cookie_str)} 字符）")
    return True


def main():
    print()
    print("请选择捕获方式：")
    print("  [1] Selenium 自动捕获（打开 Edge，自动检测登录）")
    print("  [2] 从 Edge 本地读取（无需开浏览器，如果之前登录过）")
    print("  [3] 手动粘贴 Cookie（100% 可靠，但需要你手动复制）")
    print()

    choice = input("请输入选项 [1/2/3]（默认 1）: ").strip() or "1"

    success = False
    if choice == "1":
        success = method_selenium_webdriver_manager()
        if not success:
            print()
            print("Selenium 方式失败，尝试从 Edge 本地读取...")
            success = method_browser_cookie3()
    elif choice == "2":
        success = method_browser_cookie3()
    elif choice == "3":
        success = method_manual_input()
    else:
        print("无效选项")
        return

    if success:
        print()
        print("🎉 现在可以测试同步功能：")
        print(f"   python {os.path.join(SCRIPT_DIR, 'ths_cloud_api.py')}")
    else:
        print()
        print("⚠️  所有方式都失败了，请用选项 3 手动粘贴 Cookie。")


if __name__ == "__main__":
    main()