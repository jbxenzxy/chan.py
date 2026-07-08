"""
同花顺云端自选股同步模块
通过同花顺 Web API 直接增删自选股，云端实时同步，无需重启客户端。

使用方法：
  1. 浏览器打开 https://t.10jqka.com.cn 并登录同花顺账号
  2. F12 → Network → 刷新页面 → 点击任意请求 → Request Headers → 复制 Cookie 值
  3. 将 Cookie 写入 ths_captured_cookie.txt 文件（与脚本同目录），或通过环境变量 THS_COOKIE 设置
"""

import requests
import json
import time
import re
import os


class THSCloudAPI:
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
        :param cookie: 同花顺登录后的完整 Cookie 字符串
        :param cookie_file: Cookie 文件路径，默认读取同目录下的 ths_captured_cookie.txt
        """
        if cookie:
            self.cookie = cookie
        elif cookie_file and os.path.exists(cookie_file):
            with open(cookie_file, 'r', encoding='utf-8') as f:
                self.cookie = f.read().strip()
        else:
            # 尝试从默认位置读取
            default_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ths_captured_cookie.txt")
            if os.path.exists(default_path):
                with open(default_path, 'r', encoding='utf-8') as f:
                    self.cookie = f.read().strip()
            else:
                # 尝试环境变量
                self.cookie = os.environ.get("THS_COOKIE", "")

        if not self.cookie:
            raise ValueError(
                "未找到同花顺 Cookie。请运行 ths_capture_cookie.py 重新获取。\n"
                "  手动方式：在脚本同目录创建 ths_captured_cookie.txt 文件并写入 Cookie"
            )

        self.headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                          "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
            "Referer": "https://upass.10jqka.com.cn/login",
            "Cookie": self.cookie,
            "DNT": "1",
        }

    def _parse_jsonp(self, text: str, callback_name: str = "selfStock") -> dict:
        """解析 JSONP 响应"""
        if not text:
            return {"errorCode": -1, "errorMsg": "empty response"}
        try:
            # 尝试直接 JSON 解析
            if text.strip().startswith("{"):
                return json.loads(text)
            # JSONP 格式
            pattern = re.compile(rf"{re.escape(callback_name)}\((.*)\);?", re.DOTALL)
            match = pattern.search(text)
            if match:
                return json.loads(match.group(1))
            # 尝试通用 JSONP 解析
            if "(" in text and text.endswith(");"):
                json_str = text[text.index("(") + 1 : -2]
                return json.loads(json_str)
        except Exception as e:
            print(f"[THS-API] JSONP 解析失败: {e}")
        return {"errorCode": -1, "errorMsg": "parse error"}

    def check_login(self) -> bool:
        """检查登录状态"""
        resp = self._request("get", "/newcircle/group/getSelfStockWithMarket/")
        data = self._parse_jsonp(resp)
        return data.get("errorCode") == 0

    def get_self_stocks(self) -> list[dict]:
        """
        获取当前自选股列表
        返回: [{"code": "600519", "marketid": "17", "market_name": "沪市"}, ...]
        """
        resp = self._request("get", "/newcircle/group/getSelfStockWithMarket/")
        data = self._parse_jsonp(resp)
        if data.get("errorCode") != 0:
            print(f"[THS-API] 获取自选股失败: {data.get('errorMsg')}")
            return []
        result = data.get("result", [])
        for item in result:
            item["market_name"] = self.MARKET_ID.get(
                item.get("marketid", ""), "未知"
            )
        return result

    def add_stock(self, code: str, marketid: str = "17") -> dict:
        """
        添加单只自选股
        :param code: 股票代码，如 "600519"
        :param marketid: 市场代码，沪市=17, 深市=33, 北交所=151
        :return: {"errorCode": 0, "errorMsg": ""} 表示成功
        """
        resp = self._request(
            "get",
            "/newcircle/group/modifySelfStock/",
            {"op": "add", "stockcode": code},
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
        existing_set = set()
        for item in existing:
            existing_set.add(f"{item['code']}:{item.get('marketid', '')}")

        result = {"added": [], "skipped": [], "failed": []}
        for code, marketid in stocks:
            if f"{code}:{marketid}" in existing_set:
                result["skipped"].append(code)
                print(f"[THS-API] 跳过 {code}（已存在）")
                continue

            resp = self.add_stock(code, marketid)
            if resp.get("errorCode") == 0:
                result["added"].append(code)
                print(f"[THS-API] ✓ {code} 添加成功")
                existing_set.add(f"{code}:{marketid}")
            else:
                result["failed"].append({"code": code, "msg": resp.get("errorMsg", "未知错误")})
                print(f"[THS-API] ✗ {code} 失败: {resp.get('errorMsg')}")

            time.sleep(delay)

        total = len(result["added"]) + len(result["skipped"]) + len(result["failed"])
        print(
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
        print(f"[THS-API] 当前自选股 {len(existing)} 只，准备全量替换...")
        for item in existing:
            self.delete_stock(item["code"], item.get("marketid", "17"))
            result["deleted"] += 1
            time.sleep(delay * 0.5)

        # 2. 添加新的
        add_result = self.batch_add(stocks, delay=delay)
        result["added"] = add_result["added"]
        result["failed"] = add_result["failed"]

        print(
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
            print(f"[THS-API] 请求超时: {url}")
            return ""
        except requests.exceptions.RequestException as e:
            print(f"[THS-API] 请求失败: {e}")
            return ""


# ============================================================
# 辅助函数：从股票代码推断市场ID
# ============================================================

def get_market_id(code: str) -> str:
    """
    根据股票代码推断同花顺市场ID
    - 沪市股票=17, 沪市指数=16, 沪市ETF=20
    - 深市股票=33, 深市指数=32
    - 北交所=151
    - 港股=177, 港股指数=176
    - 美股=169
    - 板块/概念=48
    """
    c = code.strip().replace(".SH", "").replace(".SZ", "").replace(".BJ", "").replace(".HK", "").upper()
    if not c:
        return "17"

    # 港股：HK+4位数字 或 K+5位数字（港股通） 或纯5位数字
    if c.startswith("HK") or c.startswith("K"):
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


# ============================================================
# 便捷函数：直接保存扫描结果到同花顺云自选
# ============================================================

def save_scan_to_ths_cloud(
    codes: list[str],
    cookie: str = None,
    cookie_file: str = None,
    replace: bool = False,
) -> dict:
    """
    将扫描结果保存到同花顺云端自选股

    :param codes: 股票代码列表，如 ["600519", "000001.SZ", "300750"]
    :param cookie: 同花顺 Cookie
    :param cookie_file: Cookie 文件路径
    :param replace: True=全量替换, False=增量添加
    :return: 操作结果
    """
    api = THSCloudAPI(cookie=cookie, cookie_file=cookie_file)

    if not api.check_login():
        return {"error": "登录状态失效，请更新 Cookie"}

    # 处理代码格式
    stocks = []
    for code in codes:
        c = code.strip()
        # 去掉市场后缀
        if "." in c:
            c = c.split(".")[0]
        if c:
            marketid = get_market_id(c)
            stocks.append((c, marketid))

    if not stocks:
        return {"error": "无有效股票代码"}

    if replace:
        return api.batch_replace(stocks)
    else:
        return api.batch_add(stocks)


# ============================================================
# 命令行测试
# ============================================================

if __name__ == "__main__":
    print("=" * 60)
    print("同花顺云端自选股同步 - 测试")
    print("=" * 60)

    try:
        api = THSCloudAPI()
        if api.check_login():
            print("✅ 登录状态有效")
            stocks = api.get_self_stocks()
            print(f"当前自选股: {len(stocks)} 只\n")

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
                print(f"  [{name}] ({len(codes)}只)")
                for c in codes:
                    print(f"    {c}")
                print()
        else:
            print("❌ 登录状态失效，请检查 Cookie")
    except ValueError as e:
        print(f"❌ {e}")
        print("\n请按以下步骤获取 Cookie：")
        print("  1. 浏览器打开 https://t.10jqka.com.cn 并登录")
        print("  2. F12 → Network → 刷新页面")
        print("  3. 点击任意请求 → Request Headers → 复制 Cookie 值")
        print("  4. 将 Cookie 保存到 ths_captured_cookie.txt 文件中")