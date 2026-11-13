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
    ("sh510300", "510300", "20", "沪市 ETF（20，见 ⑦ oracle：516020/520500 服务端就是 20）"),
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

# ── ⑥ 真实自选股 oracle：段级对照 ───────────────────────────────────
# 来源：`getSelfStockWithMarket` 真实返回的 142 条 (code, marketid)，按 3 位/2 位
# 前缀聚合成段。**只存段 + 代表代码，不存全量持仓**（自选股清单属于用户隐私）。
# 刷新：`python Test/gen_ths_oracle.py`（只读拉取，不写账号）。
#
# 这张表的作用是把「920 以外的新段只能等出现后逐个实测」变成可回归的断言：
# 旧实现在这 19 个 3 位段里错了 7 个（881/883/886→151 应 48、884→151 应 49、
# 159/161→33 应 36、516/520→17 应 20），占 oracle 的 13%——不是未来风险，是
# 当下就在错。新段出现时，只要它进了自选股，重跑 gen_ths_oracle.py 就会把
# 差异列出来，不需要人工猜段。
SEG_ORACLE_3 = [
    ("000", "33", "000001"), ("002", "33", "002007"), ("003", "33", "003021"),
    ("159", "36", "159530"), ("161", "36", "161725"),
    ("300", "33", "300015"), ("301", "33", "301606"), ("399", "32", "399001"),
    ("516", "20", "516020"), ("520", "20", "520500"),
    ("600", "17", "600004"), ("601", "17", "601021"), ("603", "17", "603259"),
    ("605", "17", "605598"), ("688", "17", "688017"),
    ("881", "48", "881121"), ("883", "48", "883421"), ("884", "49", "884069"),
    ("886", "48", "886015"),
]
SEG_ORACLE_2 = [
    ("00", "33", "000001"), ("15", "36", "159530"), ("16", "36", "161725"),
    ("30", "33", "300015"), ("39", "32", "399001"), ("51", "20", "516020"),
    ("52", "20", "520500"), ("60", "17", "600004"), ("68", "17", "688017"),
]
SEG_ORACLE_SHAPE = [
    ("1A0001", "16", "同花顺沪指码"), ("1B0300", "16", "同花顺沪指码"),
    ("HK0700", "177", "港股"), ("HS2083", "176", "港股指数"),
    ("SOXL", "169", "美股"),
]
# 信息不足、**无法**从代码推断的 3 条：断言「当前推断值」而不是服务端值，
# 这样实现一旦变动（比如有人给 URSI 加了特判）会立刻提示复核，而不是静默漂移。
SEG_ORACLE_EXEMPT = [
    ("830000", "151", "URSI", "服务端标记为退市/未识别，代码段本身是北交所"),
    ("HK3441", "177", "180", "港股个股级子市场，同代码段其余 13 只都是 177"),
    ("NBI", "169", "UNSI", "美股未识别占位"),
]

bad6 = []
for pre, want, sample in SEG_ORACLE_3 + SEG_ORACLE_2:
    got = T.get_market_id(sample)
    if got != want:
        bad6.append(f"{pre}*({sample}) 期望 {want} 实得 {got}")
rec("⑥a", f"oracle 数字段 {len(SEG_ORACLE_3) + len(SEG_ORACLE_2)} 条", not bad6,
    "；".join(bad6) or "全部与服务端 marketid 一致")

bad6b = []
for code, want, why in SEG_ORACLE_SHAPE:
    got = T.get_market_id(code)
    if got != want:
        bad6b.append(f"{code}({why}) 期望 {want} 实得 {got}")
rec("⑥b", f"oracle 非数字形态 {len(SEG_ORACLE_SHAPE)} 条", not bad6b,
    "；".join(bad6b) or "1A/1B/HK/HS/美股 全部命中")

bad6c = []
for code, infer, srv, why in SEG_ORACLE_EXEMPT:
    got = T.get_market_id(code)
    if got != infer:
        bad6c.append(f"{code} 推断 {got}（曾为 {infer}，服务端 {srv}）— {why}")
rec("⑥c", f"豁免项 {len(SEG_ORACLE_EXEMPT)} 条推断值未漂移", not bad6c,
    "；".join(bad6c) or f"830000→151 / HK3441→177 / NBI→169（服务端分别为 URSI/180/UNSI）")


# ── ⑦ 结构护栏：表自身不得有死规则，输出空间必须合法 ────────────────
# 旧实现的 `88` 分支排在 `8` 之后永远不可达，这正是「新段加了等于没加」的
# 自伤方式。以下四条是**性质**断言，不依赖具体段清单，新加段自动受约束。

TABLES = [
    ("_SEG_MARKET_FREE", T._SEG_MARKET_FREE),
    ("_SEG_SH", T._SEG_SH),
    ("_SEG_SZ", T._SEG_SZ),
    ("_SEG_BJ", T._SEG_BJ),
]

# ⑦a 同表内不得有被前序规则完全遮蔽的项（否则该规则永远不可达）
shadowed = []
for name, table in TABLES:
    pres = [p for p, _m, _w in table]
    for j, later in enumerate(pres):
        for i, earlier in enumerate(pres[:j]):
            if later.startswith(earlier):
                shadowed.append(f"{name}: '{later}' 被前序 '{earlier}' 遮蔽")
rec("⑦a", "段表内无不可达规则（旧实现 88 被 8 遮蔽）", not shadowed,
    "；".join(shadowed) or f"四张表共 {sum(len(t) for _n, t in TABLES)} 条，长度降序且无遮蔽")

# ⑦b 判定只看前缀 ⇒ 3 位段抽样即可覆盖全空间；输出必须落在 MARKET_ID 且非 "0"
illegal, vacant = [], []
for p in range(1000):
    pre = f"{p:03d}"
    for tail in ("000", "001", "500", "999"):
        out = T.get_market_id(pre + tail)
        if out not in T.CThsCloudZxg.MARKET_ID:
            illegal.append(f"{pre}{tail}→{out}")
rec("⑦b", "全 1000 个 3 位段 × 4 个边界尾，输出均为已登记市场号", not illegal,
    "；".join(illegal[:5]) or f"4000 次判定全部 ∈ MARKET_ID（{len(T.CThsCloudZxg.MARKET_ID)} 项）")
_ = vacant

# ⑦c 段内不得跳跃（同一 3 位前缀内输出必须恒定，否则说明规则用了更长前缀却漏了边界）
jumpy = []
for p in range(1000):
    pre = f"{p:03d}"
    outs = {T.get_market_id(pre + t) for t in ("000", "001", "500", "999")}
    if len(outs) > 1:
        jumpy.append(f"{pre}*→{sorted(outs)}")
rec("⑦c", "同一 3 位段内市场号恒定", not jumpy,
    "；".join(jumpy[:6]) or "1000 段全部单值")

# ⑦d 三种写法同解，且**值**必须落在该写法声明的市场里（只同解不够：
# 把沪市转债从 17 改成 33，三种写法会"一致地错"，必须同时钉住归属）
FORM_CASES = [
    # (裸代码, 前缀式, 后缀式, 允许的市场号)
    ("600519", "sh600519", "600519.SH", {"17"}),
    ("688017", "sh688017", "688017.SH", {"17"}),
    ("510300", "sh510300", "510300.SH", {"20"}),
    ("110059", "sh110059", "110059.SH", {"17"}),
    ("1A0001", "sh1A0001", "1A0001.SH", {"16"}),
    ("881121", "sh881121", "881121.SH", {"48"}),
    ("884069", "sh884069", "884069.SH", {"49"}),
    ("002523", "sz002523", "002523.SZ", {"33"}),
    ("300750", "sz300750", "300750.SZ", {"33"}),
    ("159530", "sz159530", "159530.SZ", {"36"}),
    ("161725", "sz161725", "161725.SZ", {"36"}),
    ("399001", "sz399001", "399001.SZ", {"32"}),
    ("430047", "bj430047", "430047.BJ", {"151"}),
    ("920002", "bj920002", "920002.BJ", {"151"}),
]
drift7d, wrong7d = [], []
for bare, pre, suf, allow in FORM_CASES:
    got = {T.get_market_id(bare), T.get_market_id(pre), T.get_market_id(suf)}
    if len(got) > 1:
        drift7d.append(f"{bare}: 裸={T.get_market_id(bare)} 前缀={T.get_market_id(pre)} "
                       f"后缀={T.get_market_id(suf)}")
    if not got <= allow:
        wrong7d.append(f"{bare}: 实得 {sorted(got)} 应属 {sorted(allow)}")
rec("⑦d", f"三种写法同解且归属正确 {len(FORM_CASES)} 组", not drift7d and not wrong7d,
    "；".join(drift7d + wrong7d) or "裸/前缀式/后缀式 同市场，且落在声明市场内")

# ⑦e stockcode 里不得残留市场前缀（同花顺会报「传入股票代码有误」）
leak = []
for bare, pre, suf, _allow in FORM_CASES:
    code, _mid = T.split_ths_code(pre)
    if any(code.upper().startswith(m) for m in ("SH", "SZ", "BJ")) or "." in code:
        leak.append(f"{pre}→{code}")
rec("⑦e", "前缀式代码归一后不含市场标记", not leak,
    "；".join(leak) or f"{len(FORM_CASES)} 组 stockcode 均为裸代码")

# ── 汇总 ──────────────────────────────────────────────────────────
failed = [r for r in results if not r[0]]
print("\n" + "=" * 72)
print(f"共 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
for _, tag, name, detail in failed:
    print(f"  ✗ {tag} {name}: {detail}")
print("=" * 72)
sys.exit(1 if failed else 0)
