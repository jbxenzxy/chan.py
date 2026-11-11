"""
同花顺云端自选股同步模块
通过同花顺 Web API 直接增删自选股，云端实时同步，无需重启客户端。

Cookie 从哪来：**不需要人工准备**。取数逻辑统一收在
`DataAPI/ThsCookieSource.py`（缓存文件 → 本机已登录浏览器 → 登录窗口），
本模块只在「拿到的 Cookie 用不了」时再向它要一次（见 `save_scan_to_ths_cloud`）。

仍支持显式指定（优先级最高）：
  · `CThsCloudZxg(cookie="...")` / `(cookie_file="...")`
  · 环境变量 `THS_COOKIE`
"""

import requests
import time
import os
import logging
from DataAPI.ThsCookieSource import (
    ThsCookieUnavailable,
    ensure_cookie,
    parse_jsonp,
    probe_cached,
    read_cookie_file,
)
log = logging.getLogger(__name__)



class CThsCloudZxg:
    """同花顺云端自选股 API"""

    BASE_URL = "https://t.10jqka.com.cn"

    # 市场代码映射（通过 API 探测确认）
    MARKET_ID = {
        "16": "沪市(指数)",
        "17": "沪市",
        "20": "沪市(ETF等)",
        "32": "深市(指数)",
        "33": "深市",
        "48": "板块/概念",
        "151": "北交所",
        "87": "北交所(旧)",
        "169": "美股",
        "176": "港股(指数)",
        "177": "港股",
        "0": "未知",
    }

    def __init__(self, cookie: str = None, cookie_file: str = None):
        """
        初始化 API
        :param cookie: 同花顺登录后的完整 Cookie 字符串（显式指定，优先级最高）
        :param cookie_file: 指定 Cookie 文件路径；不传时按
                            `ThsCookieSource.candidate_cookie_files()` 的顺序查找
                            （① Script/ths_captured_cookie.txt ② DataAPI/ ③ 仓库根），
                            都读不到再向 `ensure_cookie()`（浏览器通道）要一次。
        """
        self.cookie_source = "explicit"
        if cookie:
            self.cookie = cookie
        elif cookie_file and os.path.exists(cookie_file):
            self.cookie_source = "cookie_file"
            self.cookie = read_cookie_file(cookie_file)
        else:
            self.cookie = read_cookie_file()
            self.cookie_source = "file"
            if not self.cookie:
                # 文件没有 → 后台读本机已登录浏览器（**不弹窗**，构造函数保持无交互）
                try:
                    self.cookie, self.cookie_source = ensure_cookie(allow_interactive=False)
                except ThsCookieUnavailable as e:
                    raise ValueError(str(e)) from e

        if not self.cookie:
            raise ValueError(
                "未找到同花顺 Cookie，且未能从本机浏览器自动获取。\n"
                "  解法：在浏览器打开 https://t.10jqka.com.cn 登录一次（之后本程序自动接管），"
                "或运行 python Script/ths_capture_cookie.py 登录一次。"
            )

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://upass.10jqka.com.cn/login",
            "Cookie": self.cookie,
            "DNT": "1",
        }

    def _parse_jsonp(self, text: str, callback_name: str = "selfStock") -> dict:
        """解析 JSONP 响应（实现统一在 `ThsCookieSource.parse_jsonp`）。"""
        return parse_jsonp(text, callback_name)

    def check_login(self) -> bool:
        """检查登录状态（走 `probe_cached`，与 Cookie 供给层共享 2 分钟校验缓存，
        因此「刚取到的 Cookie」不会在这里再打一次同样的请求）"""
        ok, msg = probe_cached(self.cookie)
        if not ok:
            log.info(f"[THS-API] 登录态无效: {msg}")
        return ok

    def get_self_stocks(self) -> list[dict]:
        """
        获取当前自选股列表
        返回: [{"code": "600519", "marketid": "17", "market_name": "沪市"}, ...]
        """
        resp = self._request("get", "/newcircle/group/getSelfStockWithMarket/")
        data = self._parse_jsonp(resp)
        if data.get("errorCode") != 0:
            log.info(f"[THS-API] 获取自选股失败: {data.get('errorMsg')}")
            return []
        result = data.get("result", [])
        for item in result:
            item["market_name"] = self.MARKET_ID.get(
                item.get("marketid", ""), "未知"
            )
        return result

    def add_stock(self, code: str, marketid: str = None) -> dict:
        """
        添加单只自选股
        :param code: 股票代码，如 "600519"
        :param marketid: 市场代码（可选，不传时由同花顺根据代码自动识别）
        :return: {"errorCode": 0, "errorMsg": ""} 表示成功
        """
        params = {"op": "add", "stockcode": code}
        if marketid is not None:
            params["marketid"] = marketid
        resp = self._request(
            "get",
            "/newcircle/group/modifySelfStock/",
            params,
        )
        return self._parse_jsonp(resp, "modifyStock")

    def delete_stock(self, code: str, marketid: str = "17") -> dict:
        """
        删除单只自选股
        :param code: 股票代码
        :param marketid: 市场代码
        """
        resp = self._request(
            "get",
            "/newcircle/group/modifySelfStock/",
            {"op": "del", "stockcode": code, "marketid": marketid},
        )
        return self._parse_jsonp(resp, "modifyStock")

    def batch_add(
        self, stocks: list[tuple[str, str]], delay: float = 0.5
    ) -> dict:
        """
        批量添加自选股
        :param stocks: [(code, marketid), ...] 如 [("600519", "17"), ("000001", "33")]
        :param delay: 每次请求间隔（秒），避免触发频率限制
        :return: {"added": [...], "skipped": [...], "failed": [...]}
        """
        # 先获取已有的自选股，避免重复添加
        existing = self.get_self_stocks()
        existing_set = set()   # 带 marketid，如 "600519:17"
        existing_codes = set() # 纯 code，用于 marketid=None 的港股去重
        for item in existing:
            existing_set.add(f"{item['code']}:{item.get('marketid', '')}")
            existing_codes.add(item['code'])

        result = {"added": [], "skipped": [], "failed": []}
        for code, marketid in stocks:
            # marketid=None（港股）时只按 code 去重；有 marketid 时按 code:marketid 去重
            if marketid is None:
                if code in existing_codes:
                    result["skipped"].append(code)
                    log.info(f"[THS-API] 跳过 {code}（已存在）")
                    continue
            else:
                if f"{code}:{marketid}" in existing_set:
                    result["skipped"].append(code)
                    log.info(f"[THS-API] 跳过 {code}（已存在）")
                    continue

            resp = self.add_stock(code, marketid)
            if resp.get("errorCode") == 0:
                result["added"].append(code)
                log.info(f"[THS-API] ✓ {code} 添加成功")
                existing_set.add(f"{code}:{marketid or ''}")
                existing_codes.add(code)
            elif "已经存在" in (resp.get("errorMsg") or ""):
                # 本地去重靠 `code:marketid`，一旦 marketid 与服务端不一致（例如前缀式
                # 代码被推断成默认沪市 17）就会漏判，请求打过去换来「股票已经存在」。
                # 这不是失败，计跳过——否则用户看到「失败 N 只」却什么都没做错。
                result["skipped"].append(code)
                existing_codes.add(code)
                log.info(f"[THS-API] 跳过 {code}（服务端：已存在）")
            else:
                result["failed"].append({"code": code, "msg": resp.get("errorMsg", "未知错误")})
                log.info(f"[THS-API] ✗ {code} 失败: {resp.get('errorMsg')}")

            time.sleep(delay)

        total = len(result["added"]) + len(result["skipped"]) + len(result["failed"])
        log.info(
            f"[THS-API] 批量操作完成: {total}只, "
            f"新增{len(result['added'])}只, "
            f"跳过{len(result['skipped'])}只, "
            f"失败{len(result['failed'])}只"
        )
        return result

    def batch_replace(self, stocks: list[tuple[str, str]], delay: float = 0.3) -> dict:
        """
        全量替换自选股（先删除所有，再添加新的）
        注意：此操作会清空现有自选股！
        :param stocks: [(code, marketid), ...]
        :return: {"deleted": int, "added": [...], "failed": [...]}
        """
        result = {"deleted": 0, "added": [], "failed": []}

        # 1. 获取并删除所有现有自选股
        existing = self.get_self_stocks()
        log.info(f"[THS-API] 当前自选股 {len(existing)} 只，准备全量替换...")
        for item in existing:
            self.delete_stock(item["code"], item.get("marketid", "17"))
            result["deleted"] += 1
            time.sleep(delay * 0.5)

        # 2. 添加新的
        add_result = self.batch_add(stocks, delay=delay)
        result["added"] = add_result["added"]
        result["failed"] = add_result["failed"]

        log.info(
            f"[THS-API] 全量替换完成: 删除{result['deleted']}只, "
            f"新增{len(result['added'])}只, 失败{len(result['failed'])}只"
        )
        return result

    def _request(self, method: str, path: str, params: dict = None) -> str:
        """发送 HTTP 请求"""
        ts = int(time.time() * 1000)
        url = f"{self.BASE_URL}{path}"
        if params is None:
            params = {}
        params["_"] = str(ts)

        try:
            if method.lower() == "get":
                resp = requests.get(url, headers=self.headers, params=params, timeout=15)
            else:
                resp = requests.post(url, headers=self.headers, data=params, timeout=15)
            resp.raise_for_status()
            return resp.text
        except requests.exceptions.Timeout:
            log.info(f"[THS-API] 请求超时: {url}")
            return ""
        except requests.exceptions.RequestException as e:
            log.info(f"[THS-API] 请求失败: {e}")
            return ""


# ============================================================
# 辅助函数：从股票代码推断市场ID
# ============================================================

def _as_suffix_form(code_upper: str) -> str:
    """把 `sz002523` / `SH600519` 这类**市场前缀式**代码转成 `002523.SZ` / `600519.SH`。

    只有「前缀 + 纯数字」才转换，因此不会误伤 `SH`/`SZ` 开头的非 A 股代码；已是
    后缀式（`002523.SZ`）或不带市场标记（`002523`）时原样返回。项目内部主键是
    `sz002523` 这种小写前缀式，而同花顺的市场判定只认后缀分支，故先归一。
    """
    for pre in ("SH", "SZ", "BJ"):
        if code_upper.startswith(pre):
            rest = code_upper[len(pre):]
            if rest.isdigit():
                return rest + "." + pre
    return code_upper


def _is_hk_form(s: str) -> bool:
    """`HK0020` / `K1234`（字母前缀后接纯数字）才算港股写法。

    必须带数字约束：否则 `KO`（美股可口可乐）这类 K 开头的字母代码会被
    下面「港股」分支截胡成 177，永远走不到美股判定。
    """
    if s.startswith("HK"):
        return s[2:].isdigit()
    if s.startswith("K"):
        return s[1:].isdigit()
    return False


def get_market_id(code: str) -> str:
    """
    根据股票代码推断同花顺市场ID
    - 沪市股票=17, 沪市指数=16, 沪市ETF=20
    - 深市股票=33, 深市指数=32
    - 北交所=151
    - 港股=177, 港股指数=176
    - 美股=169
    - 板块/概念=48

    兼容三种写法：`sz002523`（项目内部主键）/ `002523.SZ`（东财式）/ `002523`。
    """
    code_upper = _as_suffix_form(code.strip().upper())

    # 港股：带 .HK 后缀 或 HK+数字 或 K+数字
    if code_upper.endswith(".HK"):
        return "177"
    if _is_hk_form(code_upper):
        return "177"
    # 港股指数：HS+数字
    if code_upper.startswith("HS"):
        return "176"
    # 美股：纯字母
    if code_upper.isalpha() and len(code_upper) >= 2:
        return "169"

    # 根据后缀确定市场基础，再细分为指数/股票
    if code_upper.endswith(".SH"):
        c = code_upper[:-3]  # 去掉 .SH
        # 沪市指数：000xxx, 1A/1B, 880xxx, 881xxx 等
        if c.startswith(("000", "1A", "1B", "88")) or len(c) < 6:
            return "16"
        return "17"
    if code_upper.endswith(".SZ"):
        c = code_upper[:-3]  # 去掉 .SZ
        if c.startswith("399"):
            return "32"
        return "33"
    if code_upper.endswith(".BJ"):
        return "151"

    # 无后缀：按纯数字推断
    c = code_upper
    if not c:
        return "17"

    # 港股：HK+数字 或 K+数字（港股通）
    if _is_hk_form(c):
        return "177"
    # 港股指数：HS+数字
    if c.startswith("HS"):
        return "176"
    # 美股：纯字母
    if c.isalpha() and len(c) >= 2:
        return "169"

    # 沪深指数
    if c.startswith("1A") or c.startswith("1B"):
        return "16"
    if c.startswith("399"):
        return "32"

    # 纯数字股票代码
    if len(c) < 6:
        return "17"
    if c.startswith(("6", "688", "689", "5")):
        return "17"   # 沪市（主板、科创板、ETF）
    elif c.startswith(("0", "3", "1", "159", "16")):
        return "33"   # 深市（主板、创业板、ETF/LOF）
    elif c.startswith(("8", "4", "43")):
        return "151"  # 北交所/新三板
    elif c.startswith("88"):
        return "48"   # 板块/概念
    return "17"


def split_ths_code(code: str) -> tuple[str, str]:
    """把各种写法的代码拆成同花顺接口要的 `(stockcode, marketid)`。

    `stockcode` 是**裸代码**：A 股 6 位数字、港股 `HK`+4 位。同花顺
    `modifySelfStock` 不接受带 `sh`/`sz` 前缀的代码（实测报「传入股票代码有误」），
    也不能靠它自己识别市场——前缀会让 `get_market_id` 退化成默认沪市 17。

    入参兼容：`sz002523`（项目内部主键）/ `002523.SZ` / `002523` /
    `00020.HK` / `HK0020`。
    """
    marketid = get_market_id(code)
    bare = _as_suffix_form(code.strip().upper())
    for suf in (".SH", ".SZ", ".BJ", ".HK"):
        if bare.endswith(suf):
            bare = bare[:-len(suf)]
            break
    if marketid == "177":
        # 港股：stockcode = HK + 4 位（去前导零后补零）。抓包确认 00020.HK → HK0020
        if bare.startswith("HK"):
            bare = bare[2:]
        elif bare.startswith("K") and bare[1:].isdigit():
            bare = bare[1:]
        return "HK" + bare.lstrip("0").zfill(4), marketid
    return bare, marketid


# ============================================================
# 便捷函数：直接保存扫描结果到同花顺云自选
# ============================================================

def _open_api(cookie=None, cookie_file=None, force=False, allow_interactive=False):
    """按 `ensure_cookie()` 取到**当场校验可用**的 Cookie 再构造 API。

    返回 (api, 来源)；`cookie` / `cookie_file` 显式给出时不走自动获取。
    """
    if cookie:
        return CThsCloudZxg(cookie=cookie), "explicit"
    if cookie_file:
        return CThsCloudZxg(cookie_file=cookie_file), "cookie_file"
    value, source = ensure_cookie(force=force, allow_interactive=allow_interactive)
    return CThsCloudZxg(cookie=value), source


def save_scan_to_ths_cloud(
    codes: list[str],
    cookie: str = None,
    cookie_file: str = None,
    replace: bool = False,
) -> dict:
    """
    将扫描结果保存到同花顺云端自选股

    Cookie 自动获取（无需人工准备）：缓存文件 / 本机已登录浏览器读不到可用
    Cookie 时，弹出一次登录窗口（`THS_AUTO_LOGIN=0` 可关闭）。

    :param codes: 股票代码列表，如 ["600519", "000001.SZ", "300750"]
    :param cookie: 同花顺 Cookie（显式指定，不自动刷新）
    :param cookie_file: Cookie 文件路径（显式指定，不自动刷新）
    :param replace: True=全量替换, False=增量添加
    :return: 操作结果（含 `source`：本次 Cookie 来源）
    """
    source = "explicit" if cookie else "cookie_file"
    if cookie or cookie_file:
        api = CThsCloudZxg(cookie=cookie, cookie_file=cookie_file)
    else:
        try:
            api, source = _open_api()
        except ThsCookieUnavailable:
            # 本机浏览器里没有登录态 → 弹一次登录窗口（用户此刻就在屏幕前点的按钮）
            log.info("[THS-API] 未找到登录态，打开同花顺登录窗口等待一次登录…")
            api, source = _open_api(force=True, allow_interactive=True)

    if not api.check_login():
        if cookie or cookie_file:
            return {"error": "登录状态失效（显式指定的 Cookie 不可用），请更新后重试"}
        log.info("[THS-API] Cookie 已失效，强制重新获取…")
        api, source = _open_api(force=True, allow_interactive=True)
        if not api.check_login():
            return {"error": "同花顺登录态无效：已自动尝试缓存文件/浏览器/登录窗口，仍未取到可用 Cookie"}

    # 代码归一化：调用方传的是项目内部主键 `sz002523`，而同花顺只认裸代码
    # `002523`（带前缀报「传入股票代码有误」），且前缀会让 marketid 退化成
    # 默认沪市 17（深市票会被判成沪市）。归一化统一走 split_ths_code。
    stocks = []
    for code in codes:
        if not code or not code.strip():
            continue
        stockcode, marketid = split_ths_code(code)
        if marketid == "177":
            log.info(f"[THS-API] save_scan: {code} -> {stockcode}(港股)")
            stocks.append((stockcode, None))
        else:
            log.info(f"[THS-API] save_scan: {code} -> {stockcode}(marketid={marketid})")
            stocks.append((stockcode, marketid))

    if not stocks:
        return {"error": "无有效股票代码"}

    result = api.batch_replace(stocks) if replace else api.batch_add(stocks)
    if isinstance(result, dict):
        result["source"] = source
    return result


# ============================================================
# 命令行测试
# ============================================================

if __name__ == "__main__":
    log.info("=" * 60)
    log.info("同花顺云端自选股同步 - 测试")
    log.info("=" * 60)

    try:
        api = CThsCloudZxg()
        log.info(f"Cookie 来源: {api.cookie_source}")
        if api.check_login():
            log.info("✅ 登录状态有效")
            stocks = api.get_self_stocks()
            log.info(f"当前自选股: {len(stocks)} 只\n")

            # 按 marketid 分组显示
            by_market = {}
            for s in stocks:
                mid = s.get("marketid", "?")
                name = api.MARKET_ID.get(mid, f"未知({mid})")
                if name not in by_market:
                    by_market[name] = []
                by_market[name].append(s["code"])

            for name in sorted(by_market.keys()):
                codes = by_market[name]
                log.info(f"  [{name}] ({len(codes)}只)")
                for c in codes:
                    log.info(f"    {c}")
                log.info("")
        else:
            log.info("❌ 登录状态失效，请检查 Cookie")
    except ValueError as e:
        log.info(f"❌ {e}")
        log.info("\nCookie 获取方式（任选其一，程序会自动接管）：")
        log.info("  · 本机浏览器登录过 https://t.10jqka.com.cn → 后台自动读取（推荐）")
        log.info("  · 运行 python Script/ths_capture_cookie.py → 弹出登录窗口，登录一次")
        log.info("  · 设置环境变量 THS_COOKIE，或手工写 Script/ths_captured_cookie.txt")