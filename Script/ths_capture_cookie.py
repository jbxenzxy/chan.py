# -*- coding: utf-8 -*-
"""同花顺 Cookie 获取工具（Script 层 CLI，取数逻辑在 DataAPI/ThsCookieSource.py）
=========================================================================
日常**不需要**运行本脚本：点扫描面板的「保存到自选」时，程序自己在后台按
「缓存文件 → 本机已登录浏览器 → 登录窗口」三级取 Cookie。本脚本是同一套
逻辑的手工入口 / 诊断入口。

用法：
    python Script/ths_capture_cookie.py            # 默认：先试浏览器，取不到才弹登录窗口
    python Script/ths_capture_cookie.py --auto     # 只走后台通道（文件/浏览器），不弹窗
    python Script/ths_capture_cookie.py --status   # 诊断：哪条通道可用、Cookie 是否还有效
    python Script/ths_capture_cookie.py --login --timeout 90
                                                   # 只弹登录窗口（供程序内部子进程调用）

退出码：0 取到可用 Cookie；1 失败。
"""
import argparse
import logging
import os
import sys

# 本脚本位于 Script/，仓库根 = 父目录；DataAPI/ 自仓库根导入
script_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.dirname(script_dir)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)

from DataAPI import ThsCookieSource as src  # noqa: E402  （sys.path 需先就位）

log = logging.getLogger("ths_capture_cookie")


def _print_status():
    info = src.status()
    f = info["file"]
    log.info("Cookie 文件：%s", f["path"])
    log.info("  存在=%s  可用=%s  %s", f["exists"], f["ok"], f["msg"])
    if info["browsers"]:
        log.info("本机浏览器：")
        for b in info["browsers"]:
            log.info("  %-9s %2d 条  可用=%s  %s", b["name"], b["count"], b["ok"], b["msg"])
    else:
        log.info("本机浏览器：未读到同花顺 Cookie（browser_cookie3 未安装，或都没登录过）")
    log.info("Playwright：%s", info["playwright"])
    return 0 if (f["ok"] or any(b["ok"] for b in info["browsers"])) else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description="同花顺 Cookie 获取 / 诊断")
    parser.add_argument("--auto", action="store_true", help="只走后台通道，不弹登录窗口")
    parser.add_argument("--login", action="store_true", help="只弹登录窗口（程序内部调用）")
    parser.add_argument("--status", action="store_true", help="只诊断，不取 Cookie")
    parser.add_argument("--timeout", type=float, default=src.DEFAULT_LOGIN_TIMEOUT,
                        help="登录窗口等待上限（秒），默认 %(default)s")
    parser.add_argument("--headless", action="store_true",
                        help="无头模式（不显示窗口；无 GUI 环境自检用，正常登录别加）")
    args = parser.parse_args(argv)

    if args.status:
        return _print_status()

    if args.login:
        cookie = src.interactive_login(timeout=args.timeout, headless=args.headless)
        return 0 if cookie else 1

    if args.auto:
        try:
            _cookie, source = src.ensure_cookie(force=True, allow_interactive=False)
        except src.ThsCookieUnavailable as e:
            log.info("[Cookie] ❌ %s", e)
            for d in e.details:
                log.info("  · %s", d)
            return 1
        log.info("[Cookie] ✅ 有效（来源 %s，文件 %s）", source, src.cookie_file())
        return 0

    # 默认：能不打扰就不打扰 —— 浏览器里已有登录态就直接用，否则弹一次登录窗口
    try:
        _cookie, source = src.ensure_cookie(force=True, allow_interactive=False)
        log.info("[Cookie] ✅ 已从 %s 取到有效 Cookie（无需登录）", source)
        return 0
    except src.ThsCookieUnavailable:
        log.info("[Cookie] 后台通道没有可用 Cookie，打开登录窗口…")
    cookie = src.interactive_login(timeout=args.timeout)
    if cookie:
        log.info("[Cookie] ✅ 登录成功，已写入 %s", src.cookie_file())
        return 0
    log.info("[Cookie] ❌ 未取到可用 Cookie")
    return 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stdout,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s")
    sys.exit(main())
