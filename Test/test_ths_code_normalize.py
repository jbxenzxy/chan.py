# -*- coding: utf-8 -*-
"""同花顺自选股「代码格式归一化」验证（2026-11-11）

背景（线上实际报错）：
  扫描面板勾选后点「保存到自选」，通达信侧正常写入，同花顺侧两只全失败：

      [THS-API] save_scan: sz002523 -> sz002523(marketid=17)
      [THS-API] ✗ sz002523 失败: 传入股票代码有误

  根因有两层，都出在 `save_scan_to_ths_cloud` 的入参处理：
    ① 前端 checkbox 的 value 是项目内部主键 `sz002523`，而 `plain` 只按 `.` 切后缀，
       不带点的前缀式代码被原样当 stockcode 提交 → 同花顺报「传入股票代码有误」；
    ② `get_market_id` 只认 `.SH/.SZ/.BJ` 后缀分支，`sz002523` 落到末尾兜底 return "17"
       → 深市票被判成沪市，`batch_add` 的 `code:marketid` 本地去重随之失效。

本用例锁住修复后的性质（**全程打桩：不联网、不构造真实 API**）：
  ① `split_ths_code` 覆盖 5 种写法 × 沪深北/指数/港股，返回裸代码 + 正确 marketid；
  ② 根因回归：`get_market_id('sz002523')` 必须是 33（旧实现返回 17）；
  ③ 端到端（打桩）：`save_scan_to_ths_cloud(['sz002523','sz301028'])` 提交给
     `batch_add` 的必须是 `[('002523','33'), ('301028','33')]`；
  ④ `batch_add` 遇服务端「股票已经存在」记 skipped，不再记 failed；
  ⑤ 静态契约：不再有「按点切后缀」的旧写法，归一化只经 `split_ths_code`。

运行：python Test/test_ths_code_normalize.py     # 非 0 退出即失败
"""
import inspect
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

from DataAPI import ThsCloudZxgAPI as T     # noqa: E402

results = []


def rec(tag, name, ok, detail=""):
    results.append((ok, tag, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tag} {name}: {detail}")


def _read(rel):
    with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as f:
        return f.read()


def _patch(**attrs):
    """临时替换模块属性；返回 restore()。"""
    old = {k: getattr(T, k) for k in attrs}
    for k, v in attrs.items():
        setattr(T, k, v)

    def restore():
        for k, v in old.items():
            setattr(T, k, v)
    return restore


# ── ① split_ths_code：写法 × 市场全表 ────────────────────────────────
# (输入, 期望 stockcode, 期望 marketid, 说明)
CASES = [
    # 项目内部主键（前端 checkbox 的 value，本次线上就是这种）
    ("sz002523", "002523", "33", "深主板"),
    ("sz301028", "301028", "33", "创业板"),
    ("sz000001", "000001", "33", "深主板"),
    ("sh600519", "600519", "17", "沪主板"),
    ("sh688111", "688111", "17", "科创板"),
    ("sh510300", "510300", "17", "沪市 ETF"),
    ("sh000300", "000300", "16", "沪市指数"),
    ("sz399001", "399001", "32", "深市指数"),
    ("bj430047", "430047", "151", "北交所"),
    # 东财式后缀
    ("002523.SZ", "002523", "33", "后缀式深市"),
    ("600519.SH", "600519", "17", "后缀式沪市"),
    ("000300.SH", "000300", "16", "后缀式沪指"),
    ("430047.BJ", "430047", "151", "后缀式北交所"),
    # 纯数字
    ("002523", "002523", "33", "纯数字深市"),
    ("600519", "600519", "17", "纯数字沪市"),
    # 北交所 920 段：旧实现落到末尾兜底 return "17"，把北交所票当沪市发出去
    ("920736", "920736", "151", "北交所 920 段（新段）"),
    # 港股：裸 stockcode 必须是 HK+4 位
    ("00020.HK", "HK0020", "177", "港股后缀式"),
    ("00700.HK", "HK0700", "177", "港股腾讯"),
    ("HK0020", "HK0020", "177", "港股 HK 前缀式"),
    ("K1234", "HK1234", "177", "港股通 K 前缀式"),
    # 美股：K 开头的字母代码不能被港股分支截胡（港股判定已收紧为「前缀+数字」）
    ("KO", "KO", "169", "美股可口可乐"),
    # 大小写/空白
    ("  SZ002523  ", "002523", "33", "带空白大写"),
]

bad = []
for raw, want_code, want_mid, why in CASES:
    got_code, got_mid = T.split_ths_code(raw)
    if (got_code, got_mid) != (want_code, want_mid):
        bad.append(f"{raw!r}({why}): 期望 ({want_code},{want_mid}) 实得 ({got_code},{got_mid})")
rec("①", f"split_ths_code 全表 {len(CASES)} 例", not bad, "；".join(bad) or "全部符合")

# ── ② 根因回归：前缀式代码的市场判定 ─────────────────────────────────
root = {
    "sz002523": "33", "sz301028": "33", "sh600519": "17",
    "sh000300": "16", "bj430047": "151", "sz399001": "32",
    "920736": "151",             # 北交所 920 段（旧实现落到兜底 17）
}
wrong = {k: (v, T.get_market_id(k)) for k, v in root.items() if T.get_market_id(k) != v}
rec("②", "get_market_id 认前缀式（旧实现一律退化成 17）", not wrong,
    f"错判: {wrong}" if wrong else f"sz002523→{T.get_market_id('sz002523')}(旧值 17)")

# 纯数字/后缀式不得被新增的前缀归一化带偏；K 开头字母代码不得被判成港股
keep = {"002523": "33", "002523.SZ": "33", "600519.SH": "17", "00020.HK": "177",
        "KO": "169", "HSI": "176",
        # 沪 B（900 段）不得被 920 的修复顺手吞成北交所：9 开头的判定必须窄到 920
        "900901": "17"}
drift = {k: (v, T.get_market_id(k)) for k, v in keep.items() if T.get_market_id(k) != v}
rec("②b", "既有写法不受影响", not drift, str(drift) if drift else "纯数字/后缀式/港股 均未变")


# ── ③ 端到端（打桩）：提交给 batch_add 的就是裸代码 + 正确市场 ────────
class _FakeApi:
    def __init__(self):
        self.login_calls = 0
        self.batch_calls = []

    def check_login(self):
        self.login_calls += 1
        return True

    def batch_add(self, stocks, delay=0.5):
        self.batch_calls.append(list(stocks))
        return {"added": [c for c, _ in stocks], "skipped": [], "failed": []}

    def batch_replace(self, stocks, delay=0.3):
        self.batch_calls.append(list(stocks))
        return {"added": [c for c, _ in stocks], "skipped": [], "failed": []}


fake = _FakeApi()
restore = _patch(_open_api=lambda **kw: (fake, "stub"))
try:
    out = T.save_scan_to_ths_cloud(["sz002523", "sz301028"])
finally:
    restore()

want = [("002523", "33"), ("301028", "33")]
rec("③", "save_scan_to_ths_cloud 端到端（线上两只票）",
    fake.batch_calls == [want] and out.get("source") == "stub",
    f"batch_add 收到 {fake.batch_calls}；返回 source={out.get('source')}")

# 港股走 marketid=None 分支（同花顺要求不带 marketid）
fake2 = _FakeApi()
restore = _patch(_open_api=lambda **kw: (fake2, "stub"))
try:
    T.save_scan_to_ths_cloud(["00020.HK", "sh600519", "bj430047"])
finally:
    restore()
want2 = [("HK0020", None), ("600519", "17"), ("430047", "151")]
rec("③b", "港股/沪市/北交所混合批次", fake2.batch_calls == [want2], str(fake2.batch_calls))

# 空串/空白必须被丢弃，不能变成 ("", ...) 打过去
fake3 = _FakeApi()
restore = _patch(_open_api=lambda **kw: (fake3, "stub"))
try:
    T.save_scan_to_ths_cloud(["", "  ", "sz002523"])
finally:
    restore()
rec("③c", "空串被丢弃", fake3.batch_calls == [[("002523", "33")]], str(fake3.batch_calls))

# ── ④ batch_add：服务端「已经存在」记跳过，不记失败 ─────────────────
class _DupApi(T.CThsCloudZxg):
    """只覆写网络方法，专门喂「股票已经存在」这一种响应。"""

    def __init__(self):
        self.calls = []

    def get_self_stocks(self):
        return []          # 本地去重为空，强制走服务端判定

    def add_stock(self, code, marketid=None):
        self.calls.append((code, marketid))
        return {"errorCode": -1, "errorMsg": "股票已经存在"}


dup = _DupApi()
res = dup.batch_add([("002523", "33")], delay=0)
rec("④", "「已经存在」计 skipped（旧实现计 failed）",
    res == {"added": [], "skipped": ["002523"], "failed": []}, str(res))

# 真失败仍必须记 failed，不能被上面的宽松判定吞掉
class _ErrApi(_DupApi):
    def add_stock(self, code, marketid=None):
        return {"errorCode": -1, "errorMsg": "传入股票代码有误"}


err = _ErrApi()
res2 = err.batch_add([("sz002523", "17")], delay=0)
rec("④b", "真错误仍记 failed", len(res2["failed"]) == 1 and not res2["skipped"],
    str(res2["failed"]))

# ── ⑤ 静态契约：旧写法不得回归 ──────────────────────────────────────
src = _read(os.path.join("DataAPI", "ThsCloudZxgAPI.py"))
old_style = 'c.split(".")[0] if "." in c else c'
save_body = inspect.getsource(T.save_scan_to_ths_cloud)
rec("⑤", "不再按点切后缀、改走 split_ths_code",
    old_style not in src and "split_ths_code(code)" in save_body,
    f"旧写法残留={'是' if old_style in src else '否'}；调用 split_ths_code={'是' if 'split_ths_code(code)' in save_body else '否'}")

# 归一化只能有一处实现：save_scan 内部不得再自己拼 HK 代码
rec("⑤b", "港股 HK+4 位拼接只在 split_ths_code 内",
    'zfill(4)' in inspect.getsource(T.split_ths_code)
    and 'zfill(4)' not in save_body,
    "save_scan 内 zfill(4) 残留" if 'zfill(4)' in save_body else "单一实现")

# ── 汇总 ──────────────────────────────────────────────────────────
failed = [r for r in results if not r[0]]
print("\n" + "=" * 72)
print(f"共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
for _, tag, name, detail in failed:
    print(f"  ✗ {tag} {name}: {detail}")
print("=" * 72)
sys.exit(1 if failed else 0)
