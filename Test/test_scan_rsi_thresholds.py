# -*- coding: utf-8 -*-
"""RSI 阈值可配置（前端显示 + 后端扫描同吃一份配置）护栏
====================================================================
需求（2026-10-13 第四轮，用户指定）：
  原先前端副图在 80 / 20 位置画横虚线、后端 RSI 扫描按 80 / 20 过滤 —— 两处是
  **两份同值常量**。用户要求能在设置抽屉把这对阈值改成 70 / 30 等，且**前端显示
  与后端扫描同时生效**。

实现口径（用户四项拍板）：
  ① 全局一组：K 线图副图 + 股票扫描共用一对阈值；RSI 周期（RSI_CYCLE=12）不动。
  ② 校验宽松：0 < 超卖 < 超买 < 100（与后端 scan_one 的防御判据同一句）。
  ③ 留空 = 用默认：placeholder 灰字显示默认值，留空即删持久键、请求不带参。
  ④ 披露行钉住**本次扫描实际生效值**：后端每行回传 rsi_over / rsi_under。

分发路径（关键设计；两处**都不推送全局**，多网页各配各的、互不覆盖）——
  · 显示侧：localStorage → `_applyRsiThresholds()` → `RSI_REF_VALUES`（参考线）/
    `RSI_AXIS_LEVELS`（纵轴档位）/ `RSI_SEG_OVER` / `RSI_SEG_UNDER`（分段着色
    与披露行回退值）—— 四件派生值只由这一个函数重算；
  · 扫描侧：**参数随请求走**（仅 rsi 模式且两侧都显式配置过才带参）→ FrontAPI
    Body → AppScan.submit_batch_scan → AppScanPool.submit_batch_scan（params_json
    留痕）→ `_worker_scan_one` → `scanner.scan_one(rsi_over=, rsi_under=)` →
    结果行回传。未配置不带参 ⇒ 后端回退默认，**默认值的权威源始终在 Python 侧**。

默认值 SSOT：`App/AppUtils.RSI_REF_VALUES = (50, 80, 20)` → `RSI_OVERBOUGHT` /
`RSI_OVERSOLD`；前端 `RSI_DEFAULT_OVER` / `RSI_DEFAULT_UNDER` 是**兜底副本**，
跨语言同值由 `Test/test_scan_rsi_mode.py` ② 钉住。

六段守护 ——
  ① 后端真行为（合成 K 线手算期望，经真实 scan_one 打桩驱动）：
     · 配置 70/30 真的改写判据 —— K75（窗口 RSI ≈ 73.4~76.6）默认**不命中**、
       配置后命中超买组；K25（≈ 23.4~26.7）默认不命中、配置后命中超卖组；
     · 配置 90/10 在 K87（≈ 86.5~88.4）上由**命中转不命中** —— 这是「配置值
       **替换**默认」而非「与默认 OR 叠加」的判别器（若代码把两者或起来，此条即红）；
     · 负对照 K50（≈ 47.9~52.2）两种口径都不命中；
     · 回传字段 = 本次生效值；部分配置；非法整对回退 + 告警恰好一次；
       非数静默兜底；字符串与小数原样 float 化；
  ② 后端源码契约：取值链、防御判据、回退语句恰好两处、默认常量未动、回传键名、
     rsi 分支内无 80/20 数值副本；
  ③ 全链路透传：AST 位置律（四处形参 / 实参逐一对照，顺序错即红）+ 真跑
     `submit_batch_scan`（假池）验 params_json 留痕与派发实参顺序 + 真跑
     `_worker_scan_one`（真 store）验 scan_one 关键字传参与结果行回传；
  ④ 前端抽屉真行为（node 抽真段跑真函数）：默认态 / 持久值回填 / 保存写盘 +
     重算 + render / 留空 = 默认（删键）/ 单侧留空 / 三种非法回滚且存储不动 /
     异常持久数据整体不应用 / 档位去重 / `rsiColorOf` 与 `rsiSegmentCuts` 跟随 /
     **阈值跨过中轴**（超买 45 < 中轴 50）时取色边界跟配置走、不被中轴顶住；
  ⑤ 请求带参条件：`_scanSubmitBody` 真跑 —— 仅 rsi 模式且两侧都非 null 才带参；
     调用点传的是**用户配置原值**（未配置须为 null，不许传生效值，否则后端默认被架空）；
  ⑥ 「运行值不再有第二份副本」棘轮：派生三件必须由**表达式**算、重算调用点恰好两处、
     阈值数字只在默认兜底常量定义处各出现一次、取色阈值只许在**阈值对**上取 max/min
     （对整个三元组取会把常量中轴 50 算进来）。

样本：合成 K 线（交替 +3/-1 收敛到 RSI ≈ 75 等），每个用例都先经真实 `scan_one`
打桩复核一轮再固化。跑法：`python Test/test_scan_rsi_thresholds.py`
（退出码 0/1 即判决；node 不在位时 ④⑤ 降级 SKIP）
"""
import ast
import concurrent.futures
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# 扫描任务库重定向到临时文件 —— **必须在任何 App 模块 import 之前**设好
# （ScanStore 单例按 SCAN_TASK_DB 解析路径，晚设会落到生产 App/scan_tasks.db）。
os.environ.setdefault("SCAN_TASK_DB",
                      os.path.join(tempfile.gettempdir(),
                                   "scan_tasks_rsi_thr_test.db"))

from App import AppScan as _sc                                # noqa: E402
from App import AppScanPool as _pool_mod                      # noqa: E402
from App.AppScanStore import get_scan_store                   # noqa: E402
from App.AppUtils import (                                    # noqa: E402
    RSI_OVERBOUGHT,
    RSI_OVERSOLD,
    RSI_REF_VALUES,
)

APPJS = os.path.join(ROOT, "Frontend", "app.js")
APPHTML = os.path.join(ROOT, "Frontend", "app.html")
APPUTILS = os.path.join(ROOT, "App", "AppUtils.py")
APPSCAN = os.path.join(ROOT, "App", "AppScan.py")
POOLPY = os.path.join(ROOT, "App", "AppScanPool.py")
FRONTAPI = os.path.join(ROOT, "FrontAPI.py")

_OK = []


def check(name, cond, detail=""):
    _OK.append(bool(cond))
    print(("  [PASS] " if cond else "  [FAIL] ") + name +
          ("" if cond else "  —— " + str(detail)[:300]))
    return cond


def _read(path):
    return io.open(path, encoding="utf-8").read()


# ══════════════════════════════════════════════════════════════════════
# 样本构造（手算期望；见文件头）
# ══════════════════════════════════════════════════════════════════════
def _series(deltas, start=100.0):
    out = [start]
    for d in deltas:
        out.append(round(out[-1] + d, 4))
    return out


def _mk(closes):
    return [{"date": "d%04d" % i, "open": c, "close": c, "high": c, "low": c,
             "vol": 1, "amount": 1.0} for i, c in enumerate(closes)]


# RSI 是「涨跌幅度比」的函数：交替 +a/-b 的序列收敛到 RS = a/b 的不动点。
K75 = _mk(_series([3.0, -1.0] * 30))    # RS=3  → RSI≈75.0（尾窗 73.36~76.62）
K25 = _mk(_series([1.0, -3.0] * 30))    # RS=1/3→ RSI≈25.0（尾窗 23.43~26.70）
K50 = _mk(_series([1.0, -1.0] * 30))    # RS=1  → RSI≈50.0（尾窗 47.87~52.21）
K87 = _mk(_series([7.0, -1.0] * 30))    # RS=7  → RSI≈87.5（尾窗 86.53~88.44）


def _run(klines, recent=3, name="测试股", code="002190", prefix="0", freq="d",
         mode="rsi", **kw):
    """经真实 scan_one 跑一票（只把行情源换成合成 K 线，不改被测逻辑）。"""
    def _stub(qualified_code, **k):
        return {"klines": klines, "meta": {"name": name}, "bsps": [], "bis": []}

    orig = _sc._m.analyze_stock
    _sc._m.analyze_stock = _stub
    try:
        return _sc.scanner.scan_one(code, freq=freq, prefix=prefix,
                                    recent=str(recent), source="zxg", mode=mode, **kw)
    finally:
        _sc._m.analyze_stock = orig


class _LogCapture(object):
    """抓 WARNING 级日志（判「非法整对回退」是否**恰好告警一次**）。"""

    def __init__(self):
        import logging
        self.buf = io.StringIO()
        self.h = logging.StreamHandler(self.buf)
        self.h.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        logging.getLogger().addHandler(self.h)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        import logging
        logging.getLogger().removeHandler(self.h)
        return False

    def text(self):
        return self.buf.getvalue()


# ══════════════════════════════════════════════════════════════════════
# ① 后端真行为：阈值真的驱动判据 / 回退 / 防御 / 回传
# ══════════════════════════════════════════════════════════════════════
def part1():
    print("══ ① 后端真行为（真实 scan_one 打桩驱动；合成 K 线手算期望）══")

    # ── 判别力：配置改的是**判据本身**，不是只改了回传字段 ──────────
    r_def = _run(K75)
    r_cfg = _run(K75, rsi_over=70, rsi_under=30)
    check("① 判别力 · 超买侧：K75（尾窗 RSI 73.36~76.62）默认 80/20 **不命中**；"
          "配置 70/30 后**命中超买组**（hit_o=3）",
          r_def.get("is_rsi") is False and r_cfg.get("is_rsi") is True
          and r_cfg.get("rsi_group") == "over" and r_cfg.get("rsi_hit_over") == 3
          and r_cfg.get("rsi_hit_under") == 0, (r_def, r_cfg))

    r_def25 = _run(K25)
    r_cfg25 = _run(K25, rsi_over=70, rsi_under=30)
    check("① 判别力 · 超卖侧：K25（尾窗 RSI 23.43~26.70）默认**不命中**；"
          "配置 70/30 后**命中超卖组**（hit_u=3）",
          r_def25.get("is_rsi") is False and r_cfg25.get("is_rsi") is True
          and r_cfg25.get("rsi_group") == "under" and r_cfg25.get("rsi_hit_under") == 3
          and r_cfg25.get("rsi_hit_over") == 0, (r_def25, r_cfg25))

    r_def87 = _run(K87)
    r_cfg87 = _run(K87, rsi_over=90, rsi_under=10)
    check("① 判别力 · **替换而非叠加**：K87（尾窗 RSI 86.53~88.44）默认 80/20 命中，"
          "配置 90/10 后**不命中** —— 代码若把配置值与默认值 OR 起来，此处即红",
          r_def87.get("is_rsi") is True and r_def87.get("rsi_group") == "over"
          and r_cfg87.get("is_rsi") is False, (r_def87, r_cfg87))

    r50d = _run(K50)
    r50c = _run(K50, rsi_over=70, rsi_under=30)
    r50w = _run(K50, rsi_over=90, rsi_under=10)
    check("① 负对照：K50（尾窗 RSI 47.87~52.21）在默认 80/20、70/30、90/10 三种口径下"
          "**都不命中**（判决不随阈值漂移而误报）",
          r50d.get("is_rsi") is False and r50c.get("is_rsi") is False
          and r50w.get("is_rsi") is False, (r50d, r50c, r50w))

    # ── 回传字段 = 本次生效值（披露行口径的根）───────────────────
    check("① 回传：配置 70/30 时结果行带回 rsi_over=70 / rsi_under=30（**本次生效值**）",
          r_cfg.get("rsi_over") == 70 and r_cfg.get("rsi_under") == 30,
          (r_cfg.get("rsi_over"), r_cfg.get("rsi_under")))
    check("① 回传：未配置时结果行带回默认 %s / %s（与 AppUtils SSOT 同值）"
          % (RSI_OVERBOUGHT, RSI_OVERSOLD),
          r_def87.get("rsi_over") == RSI_OVERBOUGHT
          and r_def87.get("rsi_under") == RSI_OVERSOLD,
          (r_def87.get("rsi_over"), r_def87.get("rsi_under")))
    check("① 回传：显式 None/None 与「不传参」等价（都回默认）",
          _run(K87, rsi_over=None, rsi_under=None).get("rsi_over") == RSI_OVERBOUGHT,
          _run(K87, rsi_over=None, rsi_under=None))

    # ── 部分配置：缺的一侧独立回退 ────────────────────────────────
    ro = _run(K75, rsi_over=70)
    ru = _run(K87, rsi_under=30)
    check("① 部分配置：只传 rsi_over=70 ⇒ 生效 70 / %s（under 独立回退）" % RSI_OVERSOLD,
          ro.get("is_rsi") is True and ro.get("rsi_over") == 70
          and ro.get("rsi_under") == RSI_OVERSOLD, ro)
    check("① 部分配置：只传 rsi_under=30 ⇒ 生效 %s / 30（over 独立回退）" % RSI_OVERBOUGHT,
          ru.get("is_rsi") is True and ru.get("rsi_over") == RSI_OVERBOUGHT
          and ru.get("rsi_under") == 30, ru)

    # ── 防御：非法组合整对回退 + 告警「每进程一次」 ────────────────
    #   告警闸门（`AppScan._RSI_BAD_THRESHOLD_WARNED`，定义处有注释）：阈值是**请求级**
    #   入参 —— 同一批扫描里每票拿到的都是同一对值，按票告警会在全 A 股（约 5000 票）
    #   时刷出 5000 条同文 warning，把同期真正的问题日志淹掉。故本进程**首次**命中才
    #   告警（带上原始入参，够定位），之后静默回退 —— 回退**行为**不变，只有日志条数变。
    #   这里显式复位闸门再跑，把「首次 1 条 / 后续 0 条」的完整语义钉住（写成每组都
    #   ==1 会与闸门互斥；不复位则会依赖本段是不是进程内第一次非法调用）。
    _sc._RSI_BAD_THRESHOLD_WARNED = False
    illegals = [
        ("反序 30/70", dict(rsi_over=30, rsi_under=70)),
        ("相等 70/70", dict(rsi_over=70, rsi_under=70)),
        ("越界 100/0", dict(rsi_over=100, rsi_under=0)),
        ("越界 101/19", dict(rsi_over=101, rsi_under=19)),
        ("负值 -5/20", dict(rsi_over=-5, rsi_under=20)),
        ("零值 70/0", dict(rsi_over=70, rsi_under=0)),
    ]
    for _i, (tag, kw) in enumerate(illegals):
        with _LogCapture() as lc:
            r = _run(K87, **kw)
        warn_n = lc.text().count("阈值参数非法")
        _ok_fallback = (r.get("is_rsi") is True and r.get("rsi_group") == "over"
                        and r.get("rsi_over") == RSI_OVERBOUGHT
                        and r.get("rsi_under") == RSI_OVERSOLD)
        if _i == 0:
            check("① 防御 · %s：整对回退默认（回 %s/%s）+ 告警 1 次（本进程首次命中）"
                  " + 判据仍按默认命中"
                  % (tag, RSI_OVERBOUGHT, RSI_OVERSOLD),
                  _ok_fallback and warn_n == 1, (warn_n, r, lc.text()[:200]))
        else:
            check("① 防御 · %s：整对回退默认（回 %s/%s）+ **不再重复告警**（闸门已置位，"
                  "warn_n=%d）+ 判据仍按默认命中" % (tag, RSI_OVERBOUGHT, RSI_OVERSOLD, warn_n),
                  _ok_fallback and warn_n == 0, (warn_n, r, lc.text()[:200]))

    # 闸门此时已被上面 6 组置位 ⇒ 不复位的话，下面这条「不告警」断言是**假通过**
    #   （就算非法组合路径真被误触发，闸门也会把它吞掉）。复位后才重新有判别力。
    _sc._RSI_BAD_THRESHOLD_WARNED = False
    with _LogCapture() as lc:
        r_bad = _run(K87, rsi_over="abc", rsi_under=20)
    check("① 防御 · 非数 'abc'/20：类型错走静默兜底（整对回退默认、不抛异常、不阻断本票）"
          "，且**不**触发「非法组合」告警（两条兜底路径互不重叠；已复位告警闸门，"
          "否则该断言会被闸门吞成假通过）",
          r_bad.get("is_rsi") is True and r_bad.get("rsi_over") == RSI_OVERBOUGHT
          and r_bad.get("rsi_under") == RSI_OVERSOLD
          and "阈值参数非法" not in lc.text(), (r_bad, lc.text()[:200]))

    # ── 合法边界值：float 化 / 字符串 / 小数 ──────────────────────
    r_str = _run(K75, rsi_over="70", rsi_under="30")
    check("① 合法性 · 字符串 '70'/'30' 原样 float 化后生效",
          r_str.get("is_rsi") is True and r_str.get("rsi_over") == 70
          and r_str.get("rsi_under") == 30, r_str)
    r_flt = _run(K75, rsi_over=70.5, rsi_under=29.5)
    check("① 合法性 · 小数 70.5/29.5 原样生效（不被取整、不被夹到整数）",
          r_flt.get("is_rsi") is True and r_flt.get("rsi_over") == 70.5
          and r_flt.get("rsi_under") == 29.5, r_flt)
    r_edge = _run(K87, rsi_over=88.5, rsi_under=10)
    check("① 合法性 · 阈值恰好落在窗口最大 RSI 之上（88.5 > 88.44）⇒ 不命中，"
          "落到其下（88.4）⇒ 命中 —— 边界是「含边界 >=」而非「严格 >」",
          r_edge.get("is_rsi") is False
          and _run(K87, rsi_over=88.4, rsi_under=10).get("is_rsi") is True,
          (r_edge, _run(K87, rsi_over=88.4, rsi_under=10)))

    # ── 模式隔离：非 rsi 模式不受这两个参数影响 ────────────────────
    r_other = _run(K75, mode="lianzhang", rsi_over=70, rsi_under=30)
    check("① 模式隔离：mode='lianzhang' 传了 RSI 阈值也不改其判据（阈值只被 rsi 分支消费）",
          r_other.get("is_rsi") is None and "is_lianzhang" in r_other, r_other)


# ══════════════════════════════════════════════════════════════════════
# ② 后端源码契约
# ══════════════════════════════════════════════════════════════════════
def part2():
    print("══ ② 后端源码契约（取值链 / 防御 / 默认 SSOT / 回传键）══")
    scan_src = _read(APPSCAN)
    utils_src = _read(APPUTILS)

    check("② 取值链：`RSI_OVERBOUGHT if rsi_over is None else float(...)` "
          "两侧各恰好一处（缺省回退默认 = 默认 SSOT 在 Python 侧）",
          scan_src.count("_rsi_over = RSI_OVERBOUGHT if rsi_over is None else float(rsi_over)") == 1
          and scan_src.count("_rsi_under = RSI_OVERSOLD if rsi_under is None else float(rsi_under)") == 1)
    check("② 防御判据与前端同口径一句：`if not (0 < _rsi_under < _rsi_over < 100):` 恰好一处",
          scan_src.count("if not (0 < _rsi_under < _rsi_over < 100):") == 1)
    check("② 整对回退语句恰好两处（类型错兜底 + 非法组合回退），无第三处漏网",
          scan_src.count("_rsi_over, _rsi_under = RSI_OVERBOUGHT, RSI_OVERSOLD") == 2,
          scan_src.count("_rsi_over, _rsi_under = RSI_OVERBOUGHT, RSI_OVERSOLD"))
    check("② 非法组合回退必带告警（可观测：参数被拒时日志能看出原始入参）",
          'log.warning(f"[扫描-RSI] 阈值参数非法(超买={rsi_over} 超卖={rsi_under})' in scan_src)
    check("② 该告警**带每进程一次闸门**（阈值是请求级入参：同一批里每票都传同一对值，"
          "按票告警会在全 A 股约 5000 票时刷出 5000 条同文 warning，把真问题日志淹掉）"
          "—— 模块级闸门定义 / global 声明 / 判空 / 置位各恰好一处",
          scan_src.count("_RSI_BAD_THRESHOLD_WARNED = False") == 1
          and scan_src.count("global _RSI_BAD_THRESHOLD_WARNED") == 1
          and scan_src.count("if not _RSI_BAD_THRESHOLD_WARNED:") == 1
          and scan_src.count("_RSI_BAD_THRESHOLD_WARNED = True") == 1,
          scan_src.count("_RSI_BAD_THRESHOLD_WARNED"))

    check("② 判据用**变量**：命中计数 `if v <= _rsi_under` / `if v >= _rsi_over` "
          "与归类 `if rsi_last <= _rsi_under:` / `elif rsi_last >= _rsi_over:` 各恰好一处"
          "（含边界；换成默认常量即回到「硬编码」）",
          scan_src.count("if v <= _rsi_under") == 1
          and scan_src.count("if v >= _rsi_over") == 1
          and scan_src.count("if rsi_last <= _rsi_under:") == 1
          and scan_src.count("elif rsi_last >= _rsi_over:") == 1)

    check("② 回传键名 = 前端 `_rsiCaliberHtml` 读的那两个：`\"rsi_over\": _rsi_over,` / "
          "`\"rsi_under\": _rsi_under,` 各恰好一处",
          scan_src.count('"rsi_over": _rsi_over,') == 1
          and scan_src.count('"rsi_under": _rsi_under,') == 1)
    check("② 日志带生效阈值（排查「同一次扫描两页数字不同」先看这里）",
          "阈值{_rsi_over:g}/{_rsi_under:g} " in scan_src)

    check("② 默认值 SSOT **未被参数化**：AppUtils 仍是 `RSI_REF_VALUES = (50, 80, 20)` "
          "唯一一处 + `RSI_OVERBOUGHT = max(RSI_REF_VALUES[1:])` / "
          "`RSI_OVERSOLD = min(RSI_REF_VALUES[1:])` 派生"
          "（2026-10-14：派生式与前端 `slice(1)` 统一为「只取阈值对」，中轴不参与）",
          utils_src.count("RSI_REF_VALUES = (50, 80, 20)") == 1
          and utils_src.count("RSI_OVERBOUGHT = max(RSI_REF_VALUES[1:])") == 1
          and utils_src.count("RSI_OVERSOLD = min(RSI_REF_VALUES[1:])") == 1
          and list(RSI_REF_VALUES) == [50, 80, 20]
          and RSI_OVERBOUGHT == 80 and RSI_OVERSOLD == 20)
    check("② 默认值的权威源注释在位（配置化后 AppUtils 仍是「默认」的 SSOT，不是运行值）",
          "默认值" in utils_src and "权威源" in utils_src)


# ══════════════════════════════════════════════════════════════════════
# ③ 全链路透传（AST 位置律 + 真跑提交 / 真跑 worker）
# ══════════════════════════════════════════════════════════════════════
def _params_of(src, name):
    """取第一个同名函数的形参名（含 async 与 keyword-only）。"""
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ([a.arg for a in node.args.args]
                    + [a.arg for a in node.args.kwonlyargs])
    return None


def _calls(src, pred):
    return [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Call) and pred(n)]


def _seg(node, src):
    return ast.get_source_segment(src, node)


def part3():
    print("══ ③ 全链路透传（AST 位置律 + 真跑提交 + 真跑 worker）══")
    scan_src = _read(APPSCAN)
    pool_src = _read(POOLPY)
    api_src = _read(FRONTAPI)

    # ── AST 位置律：形参顺序 ──────────────────────────────────────
    p = _params_of(scan_src, "scan_one")
    check("③ 形参 · Scanner.scan_one 尾部 = (..., bsp_types, rsi_over, rsi_under)",
          p is not None and p[-3:] == ["bsp_types", "rsi_over", "rsi_under"], p)
    p = _params_of(pool_src, "_worker_scan_one")
    check("③ 形参 · _worker_scan_one 尾部 = (..., bsp_types, rsi_over, rsi_under, seq)"
          "（seq 必须垫在最后）",
          p is not None and p[-4:] == ["bsp_types", "rsi_over", "rsi_under", "seq"], p)
    p = _params_of(pool_src, "submit_batch_scan")
    check("③ 形参 · AppScanPool.submit_batch_scan 尾部 = (..., bsp_types, rsi_over, rsi_under)",
          p is not None and p[-3:] == ["bsp_types", "rsi_over", "rsi_under"], p)
    p = _params_of(scan_src, "submit_batch_scan")
    check("③ 形参 · AppScan.Scanner.submit_batch_scan 尾部 = (..., bsp_types, rsi_over, rsi_under)"
          "（委托壳与实现同形，见 test_funnel_signature 同一约定）",
          p is not None and p[-3:] == ["bsp_types", "rsi_over", "rsi_under"], p)
    p = _params_of(api_src, "api_stocks_scan_submit")
    check("③ 形参 · FrontAPI.api_stocks_scan_submit 尾部 = (..., bsp_types, rsi_over, rsi_under)",
          p is not None and p[-3:] == ["bsp_types", "rsi_over", "rsi_under"], p)

    # ── AST 位置律：实参顺序 ──────────────────────────────────────
    cs = _calls(pool_src, lambda c: isinstance(c.func, ast.Attribute)
                and c.func.attr == "submit" and len(c.args) >= 2
                and _seg(c.args[0], pool_src) == "_worker_scan_one")
    check("③ 实参 · pool.submit(_worker_scan_one, ...) 恰好一处（派发点唯一）",
          len(cs) == 1, len(cs))
    if len(cs) == 1:
        got = [_seg(a, pool_src) for a in cs[0].args[1:]]
        check("③ 实参 · 派发位置实参逐位对齐（task_id, code, freq, prefix, recent, src, "
              "mode, bsp_types, rsi_over, rsi_under, seq）—— 顺序错就会把阈值喂成 src",
              got == ["task_id", "code", "freq", "prefix", "recent", "src", "mode",
                      "bsp_types", "rsi_over", "rsi_under", "seq"], got)

    cs = _calls(api_src, lambda c: isinstance(c.func, ast.Name)
                and c.func.id == "run_in_threadpool" and len(c.args) >= 2
                and _seg(c.args[0], api_src) == "orch.scanner.submit_batch_scan")
    check("③ 实参 · run_in_threadpool(orch.scanner.submit_batch_scan, ...) 恰好一处",
          len(cs) == 1, len(cs))
    if len(cs) == 1:
        got = [_seg(a, api_src) for a in cs[0].args[1:]]
        check("③ 实参 · API → 业务层实参逐位对齐（stocks, freq, mode, recent, source, "
              "scan_token or None, bsp_types, rsi_over, rsi_under）",
              got == ["stocks", "freq", "mode", "recent", "source", "scan_token or None",
                      "bsp_types", "rsi_over", "rsi_under"], got)

    cs = _calls(scan_src, lambda c: isinstance(c.func, ast.Name) and c.func.id == "_submit")
    check("③ 实参 · AppScan.submit_batch_scan 委托 _submit(...) 带 rsi_over/rsi_under 关键字"
          "（关键字传参，不靠位置）",
          len(cs) == 1 and {"rsi_over", "rsi_under", "bsp_types"} <= {k.arg for k in cs[0].keywords},
          [k.arg for k in cs[0].keywords] if cs else None)

    cs = _calls(pool_src, lambda c: isinstance(c.func, ast.Attribute) and c.func.attr == "scan_one")
    check("③ 实参 · _worker_scan_one 调 scanner.scan_one(...) 恰好一处且带 rsi_over/rsi_under 关键字",
          len(cs) == 1
          and {"rsi_over", "rsi_under"} <= {k.arg for k in cs[0].keywords},
          [k.arg for k in cs[0].keywords] if cs else None)

    # ── 真跑提交：params_json 留痕 + 派发实参（假池 + monitor 打桩，全同步）──
    class _RecPool(object):
        def __init__(self):
            self.calls = []

        def submit(self, fn, *a, **k):
            self.calls.append((fn, a, k))
            f = concurrent.futures.Future()
            f.set_result(None)
            return f

        def shutdown(self, wait=False, cancel_futures=True):
            pass

    rec_pool = _RecPool()
    orig = (getattr(_pool_mod, "_get_pool", None), _pool_mod._monitor_task)
    _pool_mod._get_pool = lambda: (rec_pool, "fake")
    _pool_mod._monitor_task = lambda *a, **k: None
    try:
        resp = _pool_mod.submit_batch_scan(
            [{"code": "sz000001", "prefix": "0"}], freq="d", mode="rsi",
            recent="3", source="zxg", rsi_over=70, rsi_under=30)
    finally:
        _pool_mod._get_pool, _pool_mod._monitor_task = orig

    check("③ 真跑 · submit_batch_scan 正常返回 task_id（假池不改变成功路径返回形）",
          isinstance(resp, dict) and resp.get("task_id"), resp)
    task = get_scan_store().get_task(resp.get("task_id")) if resp.get("task_id") else None
    check("③ 真跑 · params_json 留痕 rsi_over=70 / rsi_under=30（JSON 列，零 migration；"
          "排查时先看这里）",
          task is not None and task["params"].get("rsi_over") == 70
          and task["params"].get("rsi_under") == 30,
          task["params"] if task else None)
    check("③ 真跑 · 派发给 worker 的位置实参把 70/30 放在 bsp_types 之后、seq 之前",
          len(rec_pool.calls) == 1
          and rec_pool.calls[0][0] is _pool_mod._worker_scan_one
          and rec_pool.calls[0][1][8] == 70 and rec_pool.calls[0][1][9] == 30
          and rec_pool.calls[0][1][7] is None,
          rec_pool.calls[0][1] if rec_pool.calls else None)

    # ── 真跑 worker：真实 store + 真实 scan_one（只换行情源）─────────
    store = get_scan_store()
    tid = store.create_task(total=1, params={"probe": "rsi_thresholds"})
    orig_an = _sc._m.analyze_stock
    _sc._m.analyze_stock = (lambda qc, **k: {"klines": K75, "meta": {"name": "W"},
                                             "bsps": [], "bis": []})
    try:
        wr = _pool_mod._worker_scan_one(tid, "002190", "d", "0", "3", "zxg",
                                        "rsi", None, 70, 30, 0)
    finally:
        _sc._m.analyze_stock = orig_an
    check("③ 真跑 · _worker_scan_one 把 rsi_over/rsi_under 原样喂进 scan_one 并用上"
          "（K75 打桩下由「不命中」翻成「命中超买组」）",
          isinstance(wr, dict) and wr.get("is_rsi") is True
          and wr.get("rsi_group") == "over" and wr.get("rsi_over") == 70
          and wr.get("rsi_under") == 30, wr)
    rows = store.get_results(tid, since=0)
    check("③ 真跑 · 结果行落库并回传 rsi_over/rsi_under（前端披露行读的就是这两个字段）",
          len(rows) == 1 and rows[0]["status"] == "ok"
          and rows[0]["data"].get("rsi_over") == 70
          and rows[0]["data"].get("rsi_under") == 30, rows)

    # 未传阈值的那一票：留痕 None（不能把前端默认值写进 params，否则后端默认被架空）
    rec_pool2 = _RecPool()
    _pool_mod._get_pool = lambda: (rec_pool2, "fake")
    _pool_mod._monitor_task = lambda *a, **k: None
    try:
        resp2 = _pool_mod.submit_batch_scan([{"code": "sz000001", "prefix": "0"}],
                                            freq="d", mode="rsi", recent="3",
                                            source="zxg")
    finally:
        _pool_mod._get_pool, _pool_mod._monitor_task = orig
    t2 = get_scan_store().get_task(resp2.get("task_id")) if resp2.get("task_id") else None
    check("③ 真跑 · 未配置时 params_json 两键均为 None（**不写死默认值** ⇒ 后端回退 SSOT）",
          t2 is not None and t2["params"].get("rsi_over") is None
          and t2["params"].get("rsi_under") is None,
          t2["params"] if t2 else None)


# ══════════════════════════════════════════════════════════════════════
# ④ 前端抽屉真行为（node 抽真段跑真函数）
# ══════════════════════════════════════════════════════════════════════
def _extract_any_fn(appjs, fn_name):
    """按花括号配平抽一个 8 空格缩进的函数（兼容单行 / 多行写法）。"""
    m = re.search(r"        function " + fn_name + r"\(", appjs)
    assert m, "app.js 抽取失败: " + fn_name
    i = appjs.index("{", m.end())
    depth = 0
    for j in range(i, len(appjs)):
        if appjs[j] == "{":
            depth += 1
        elif appjs[j] == "}":
            depth -= 1
            if depth == 0:
                return appjs[appjs.rfind("\n", 0, m.start()) + 1:j + 1]
    raise AssertionError("app.js 花括号不配平: " + fn_name)


def _extract_iife(appjs, marker):
    """从 marker 起抽紧随其后的第一个 `(function() { ... })();` 整体（含调用括号）。

    花括号配平只定位到内层 `}`；尾部 `)();` 形态**断言校验**（源写法变了即报错，
    不许静默返回半截表达式 —— 少一个 `)` 会变成「合法但不执行」的 JS，护栏假绿）。
    """
    i = appjs.index(marker)
    j = appjs.index("(function()", i)
    k = appjs.index("{", j)
    depth = 0
    for n in range(k, len(appjs)):
        if appjs[n] == "{":
            depth += 1
        elif appjs[n] == "}":
            depth -= 1
            if depth == 0:
                m2 = re.match(r"\)\s*\(\s*\)\s*;", appjs[n + 1:])
                assert m2, "抽屉 IIFE 尾部形态意外: %r" % appjs[n:n + 12]
                return appjs[j:n + 1 + m2.end()]
    raise AssertionError("抽屉 IIFE 花括号不配平")


_DRAWER_PRELUDE = r"""
var DOM = {}, LS = {}, TOASTS = [], RENDERS = 0;
function mkEl(id) {
    if (!DOM[id]) DOM[id] = { id: id, value: "", placeholder: "", _h: {},
        addEventListener: function (ev, fn) { this._h[ev] = fn; },
        fire: function (ev) { if (this._h[ev]) this._h[ev](); } };
    return DOM[id];
}
var document = { getElementById: function (id) { return DOM[id] || null; } };
var localStorage = {
    getItem: function (k) {
        return Object.prototype.hasOwnProperty.call(LS, k) ? LS[k] : null; },
    setItem: function (k, v) { LS[k] = String(v); },
    removeItem: function (k) { delete LS[k]; }
};
function showToast(m) { TOASTS.push(m); }
function render() { RENDERS++; }
var COLORS = { macdUp: "UP", macdDown: "DN", rsi: "WH" };
var _isMirrorMode = false;   // rsiColorOf 读它（翻转视图换色），桩里按正常视图
function clearAll() { DOM = {}; LS = {}; TOASTS = []; RENDERS = 0; }
"""


def _build_drawer_harness(appjs):
    lines = []
    for pat in (r"        const RSI_DEFAULT_OVER = .*",
                r"        const RSI_DEFAULT_UNDER = .*",
                r"        let _rsiOverUser = .*",
                r"        let _rsiUnderUser = .*",
                r"        let RSI_REF_VALUES = .*",
                r"        let RSI_AXIS_LEVELS = .*",
                r"        let RSI_SEG_OVER = .*",
                r"        let RSI_SEG_UNDER = .*"):
        m = re.search(pat, appjs)
        assert m, "app.js 常量行抽取失败: " + pat
        lines.append(m.group(0).rstrip())

    iife = _extract_iife(appjs, "// RSI 阈值输入框（设置抽屉）")
    body = "\n".join([
        lines[0], lines[1], lines[2], lines[3], lines[4], lines[5], lines[6], lines[7],
        _extract_any_fn(appjs, "_applyRsiThresholds"),
        _extract_any_fn(appjs, "rsiColorOf"),
        _extract_any_fn(appjs, "rsiSegmentCuts"),
        "    return {",
        "        snap: function () { return { ref: RSI_REF_VALUES.slice(),",
        "            axis: RSI_AXIS_LEVELS.slice(), seg: [RSI_SEG_OVER, RSI_SEG_UNDER] }; },",
        "        over: function () { return _rsiOverUser; },",
        "        under: function () { return _rsiUnderUser; },",
        "        el: function (id) { return mkEl(id); },",
        "        color: function (v) { return rsiColorOf(v); },",
        "        cuts: function (a, b) { return rsiSegmentCuts(a, b); },",
        "        boot: function () {",
        "            // 真实页面里这两个 input 是 app.html 的静态标签、在 app.js 执行前就已在",
        "            // DOM 中 —— 预建以复现「启动即载入持久值」。getElementById 仍保持",
        "            // 「缺失即 null」语义（IIFE 的 `if (!overInput || !underInput) return;`",
        "            // 因此仍可被触发，不许改成「按需创建」那种永不为 null 的桩）。",
        "            mkEl(\"rsi-threshold-over\"); mkEl(\"rsi-threshold-under\");",
        iife,
        "        }",
        "    };",
    ])
    return _DRAWER_PRELUDE + "\nfunction __mk() {\n" + body + "\n}\n" + _DRAWER_DRIVER


_DRAWER_DRIVER = r"""
var out = [];
var M, o, u;

// ── D1 默认态（无持久值）：输入框空 + placeholder = 默认 + 四件 = 默认 ──
clearAll(); M = __mk();
o = M.el("rsi-threshold-over"); u = M.el("rsi-threshold-under");
M.boot();
out.push("D1=" + JSON.stringify({ ov: o.value, uv: u.value,
    pho: o.placeholder, phu: u.placeholder, snap: M.snap(),
    over: M.over(), under: M.under(), lsKeys: Object.keys(LS) }));

// ── D2 持久值 70/30：回填输入框 + 四件跟随 + 取色/切点跟随 ──────────
clearAll(); M = __mk();
o = M.el("rsi-threshold-over"); u = M.el("rsi-threshold-under");
LS["scan_rsi_overbought"] = "70"; LS["scan_rsi_oversold"] = "30";
M.boot();
out.push("D2=" + JSON.stringify({ ov: o.value, uv: u.value, snap: M.snap(),
    over: M.over(), under: M.under(),
    c75: M.color(75), c65: M.color(65), c35: M.color(35), c25: M.color(25),
    cuts_50_90: M.cuts(50, 90), cuts_90_10: M.cuts(90, 10) }));

// ── D3 保存 90/10：写盘 + 重算 + render 恰好一次 ────────────────────
clearAll(); M = __mk();
o = M.el("rsi-threshold-over"); u = M.el("rsi-threshold-under");
M.boot();
o.value = "90"; u.value = "10"; o.fire("change");
out.push("D3=" + JSON.stringify({ ls: [LS["scan_rsi_overbought"], LS["scan_rsi_oversold"]],
    snap: M.snap(), renders: RENDERS, toasts: TOASTS.length,
    ov: o.value, uv: u.value }));

// ── D3b 另一侧触发 change 也生效（两个输入框绑同一提交函数）──────────
clearAll(); M = __mk();
o = M.el("rsi-threshold-over"); u = M.el("rsi-threshold-under");
M.boot();
o.value = "72"; u.value = "28"; u.fire("change");
out.push("D3b=" + JSON.stringify({ snap: M.snap(), renders: RENDERS,
    ls: [LS["scan_rsi_overbought"], LS["scan_rsi_oversold"]] }));

// ── D4 留空 = 默认：两侧都清空 ⇒ 删键 + 回默认 + 输入框保持空 ────────
clearAll(); M = __mk();
o = M.el("rsi-threshold-over"); u = M.el("rsi-threshold-under");
LS["scan_rsi_overbought"] = "70"; LS["scan_rsi_oversold"] = "30";
M.boot();
o.value = ""; u.value = ""; u.fire("change");
out.push("D4=" + JSON.stringify({ lsKeys: Object.keys(LS), snap: M.snap(),
    over: M.over(), under: M.under(), ov: o.value, uv: u.value, renders: RENDERS }));

// ── D5 单侧留空：只配超买 70 ⇒ 只存 over 键、under 键被删 ──────────
clearAll(); M = __mk();
o = M.el("rsi-threshold-over"); u = M.el("rsi-threshold-under");
M.boot();
o.value = "70"; u.value = ""; o.fire("change");
out.push("D5=" + JSON.stringify({ lsKeys: Object.keys(LS),
    lsOver: LS["scan_rsi_overbought"], hasUnder: ("scan_rsi_oversold" in LS),
    snap: M.snap(), over: M.over(), under: M.under() }));

// ── D6 三种非法：回滚输入框 + 存储不动 + 阈值不动 + toast + 不 render ─
clearAll(); M = __mk();
o = M.el("rsi-threshold-over"); u = M.el("rsi-threshold-under");
LS["scan_rsi_overbought"] = "70"; LS["scan_rsi_oversold"] = "30";
M.boot();
var bads = [["30", "70"], ["70", "70"], ["100", "0"], ["-5", "20"], ["abc", "20"]];
var badOut = [];
for (var bi = 0; bi < bads.length; bi++) {
    TOASTS = []; RENDERS = 0;
    o.value = bads[bi][0]; u.value = bads[bi][1]; o.fire("change");
    badOut.push({ in: bads[bi], toasts: TOASTS.length, renders: RENDERS,
        ov: o.value, uv: u.value, ls: [LS["scan_rsi_overbought"], LS["scan_rsi_oversold"]],
        snap: M.snap() });
}
out.push("D6=" + JSON.stringify(badOut));

// ── D7 异常持久数据（反序 30/70）：整体不应用，走默认 ────────────────
clearAll(); M = __mk();
LS["scan_rsi_overbought"] = "30"; LS["scan_rsi_oversold"] = "70";
M.boot();
out.push("D7=" + JSON.stringify({ snap: M.snap(), over: M.over(), under: M.under() }));

// ── D8 异常持久数据（非数）：单键读成 null ⇒ 回落默认后仍合法 ────────
clearAll(); M = __mk();
LS["scan_rsi_overbought"] = "xyz"; LS["scan_rsi_oversold"] = "30";
M.boot();
out.push("D8=" + JSON.stringify({ snap: M.snap(), over: M.over(), under: M.under() }));

// ── D9 档位去重：超买配到与中轴同值 50 ⇒ 档位不印两遍 ────────────────
clearAll(); M = __mk();
o = M.el("rsi-threshold-over"); u = M.el("rsi-threshold-under");
M.boot();
o.value = "50"; u.value = "20"; o.fire("change");
out.push("D9=" + JSON.stringify({ snap: M.snap(), toasts: TOASTS.length }));

// ── D10 默认态取色（负对照：75 不是红、25 不是绿）────────────────────
clearAll(); M = __mk();
M.boot();
out.push("D10=" + JSON.stringify({ snap: M.snap(), c75: M.color(75), c80: M.color(80),
    c25: M.color(25), c20: M.color(20), c19: M.color(19), cuts_75_85: M.cuts(75, 85) }));

// ── D11 默认口径负对照：47 落在 80/20 之间 ⇒ 白（不上红、不下青）──
clearAll(); M = __mk();
M.boot();
out.push("D11=" + JSON.stringify({ snap: M.snap(), c47: M.color(47) }));

// ── D12 阈值跨中轴（超买 45 < 中轴 50）：取色边界必须跟 45 走 ──
//        若取 max 时把中轴 50 算进来，c47 会变白、cuts(40→50) 会空
//        （切点落在 1.0 被过滤）—— 与后端「按 45 判超买」分叉。
clearAll(); M = __mk();
o = M.el("rsi-threshold-over"); u = M.el("rsi-threshold-under");
M.boot();
o.value = "45"; u.value = "30"; o.fire("change");
out.push("D12=" + JSON.stringify({ snap: M.snap(), c47: M.color(47), c45: M.color(45),
    c44: M.color(44), c31: M.color(31), c30: M.color(30), c29: M.color(29),
    cuts_40_50: M.cuts(40, 50), toasts: TOASTS.length }));

console.log(out.join("\n"));
"""


def part4():
    print("══ ④ 前端抽屉真行为（node 抽真段；node 不在位则 SKIP）══")
    appjs = _read(APPJS)
    html = _read(APPHTML)

    check("④ app.html：两个 number 输入框 id 齐备（抽屉里，与流通市值过滤同区块）",
          'id="rsi-threshold-over"' in html and 'id="rsi-threshold-under"' in html
          and html.count('type="number" id="rsi-threshold-over" min="1" max="99"') == 1
          and html.count('type="number" id="rsi-threshold-under" min="1" max="99"') == 1)
    check("④ app.html：标题点明「K线图与股票扫描共用」+ 尾注「留空 = 默认」"
          "（用户须知道这一对值同时管显示与扫描）",
          "RSI 阈值（超买 / 超卖，K线图与股票扫描共用）" in html
          and "<span>留空 = 默认</span>" in html)
    check("④ app.html：两个输入框落在**设置抽屉**内（在流通市值过滤之后、抽屉分隔线之前）",
          html.index('id="scan-min-float-mc"') < html.index('id="rsi-threshold-over"')
          < html.index('id="rsi-threshold-under"')
          < html.index('<div style="border-top:1px solid #2a2a3e;margin:16px 0 12px;"></div>'))

    node = shutil.which("node")
    if not node:
        check("node 不在位，抽屉真行为层 SKIP", True)
        return

    jf = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    jf.write(_build_drawer_harness(appjs))
    jf.close()
    try:
        proc = subprocess.run([node, jf.name], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    finally:
        os.unlink(jf.name)
    if proc.returncode != 0:
        check("node 执行抽屉 harness", False, (proc.stderr or proc.stdout)[:500])
        return
    kv = dict(x.split("=", 1) for x in proc.stdout.splitlines()
              if re.match(r"^[A-Za-z0-9]+=", x))
    if "D2" not in kv:
        check("抽屉 harness 输出 D2", False, proc.stdout[:300])
        return

    d1 = json.loads(kv["D1"])
    check("④ D1 默认态：输入框空 + placeholder 显示默认 80 / 20"
          "（引用常量而非第二遍数字）+ 四件派生值 = [50,80,20]/[80,50,20]/80/20",
          d1["ov"] == "" and d1["uv"] == ""
          and d1["pho"] == "80" and d1["phu"] == "20"
          and d1["snap"]["ref"] == [50, 80, 20] and d1["snap"]["axis"] == [80, 50, 20]
          and d1["snap"]["seg"] == [80, 20]
          and d1["over"] is None and d1["under"] is None and d1["lsKeys"] == [], d1)

    d2 = json.loads(kv["D2"])
    check("④ D2 持久值 70/30：输入框回填 70 / 30 + 用户变量 70/30 + "
          "参考线 [50,70,30] + 纵轴 [70,50,30] + 着色阈值 70/30",
          d2["ov"] == "70" and d2["uv"] == "30"
          and d2["over"] == 70 and d2["under"] == 30
          and d2["snap"]["ref"] == [50, 70, 30] and d2["snap"]["axis"] == [70, 50, 30]
          and d2["snap"]["seg"] == [70, 30], d2)
    check("④ D2 **显示侧真的跟着换**：70/30 生效后 rsiColorOf(75)=红 / (65)=白 / "
          "(35)=白 / (25)=绿（默认口径下 75 / 25 都该是白）",
          d2["c75"] == "UP" and d2["c65"] == "WH"
          and d2["c35"] == "WH" and d2["c25"] == "DN", d2)
    check("④ D2 变色点跟着换：cuts(50→90) 由默认的一点变 0.5；cuts(90→10) 由 "
          "0.125/0.875 变 0.25/0.75；50→70 这一段**不再切**（未跨阈值）",
          d2["cuts_50_90"] == [0.5] and d2["cuts_90_10"] == [0.25, 0.75], d2)

    d3 = json.loads(kv["D3"])
    check("④ D3 保存 90/10：写 localStorage 两个键 + 四件重算 + render 恰好一次 + 无 toast",
          d3["ls"] == ["90", "10"] and d3["snap"]["ref"] == [50, 90, 10]
          and d3["snap"]["axis"] == [90, 50, 10] and d3["snap"]["seg"] == [90, 10]
          and d3["renders"] == 1 and d3["toasts"] == 0
          and d3["ov"] == "90" and d3["uv"] == "10", d3)

    d3b = json.loads(kv["D3b"])
    check("④ D3b 两个输入框绑同一提交函数（改超卖那一侧同样落盘 + 重算）",
          d3b["ls"] == ["72", "28"] and d3b["snap"]["ref"] == [50, 72, 28]
          and d3b["renders"] == 1, d3b)

    d4 = json.loads(kv["D4"])
    check("④ D4 留空 = 默认：两个持久键都被删 + 四件回默认 + 用户变量回 null"
          "（请求不带参 ⇒ 后端回退 SSOT）",
          d4["lsKeys"] == [] and d4["snap"]["ref"] == [50, 80, 20]
          and d4["snap"]["seg"] == [80, 20] and d4["over"] is None
          and d4["under"] is None and d4["ov"] == "" and d4["uv"] == ""
          and d4["renders"] == 1, d4)

    d5 = json.loads(kv["D5"])
    check("④ D5 单侧留空：只配超买 70 ⇒ 只存 over 键、under 键被删；"
          "生效 [50, 70, 20]（超卖那侧独立回默认）",
          d5["lsKeys"] == ["scan_rsi_overbought"] and d5["lsOver"] == "70"
          and d5["hasUnder"] is False and d5["snap"]["ref"] == [50, 70, 20]
          and d5["over"] == 70 and d5["under"] is None, d5)

    d6 = json.loads(kv["D6"])
    check("④ D6 五种非法（反序 / 相等 / 上越界 / 负值 / 非数）：每次都 toast 恰好一次、"
          "不 render、输入框回滚到已存值、存储与阈值一动不动",
          len(d6) == 5 and all(x["toasts"] == 1 and x["renders"] == 0
                               and x["ov"] == "70" and x["uv"] == "30"
                               and x["ls"] == ["70", "30"]
                               and x["snap"]["seg"] == [70, 30] for x in d6), d6)
    check("④ D6 非法提示文案给出可执行的判据（不是只说「不合法」）",
          "0 < 超卖 < 超买 < 100" in _read(APPJS), "")

    d7 = json.loads(kv["D7"])
    check("④ D7 持久数据本身就非法（30/70 反序）⇒ 整体不应用、走默认（不半应用成 "
          "[50,30,70]）",
          d7["snap"]["ref"] == [50, 80, 20] and d7["snap"]["seg"] == [80, 20]
          and d7["over"] is None and d7["under"] is None, d7)

    d8 = json.loads(kv["D8"])
    check("④ D8 持久值非数：单键读成 null ⇒ 该侧回落默认后组合仍合法 ⇒ 另一侧 30 照常生效",
          d8["snap"]["ref"] == [50, 80, 30] and d8["over"] is None
          and d8["under"] == 30, d8)

    d9 = json.loads(kv["D9"])
    check("④ D9 档位去重：超买配到与中轴同值 50 ⇒ 纵轴档位 [50, 20]（50 不印两遍），"
          "着色阈值 max/min = 50/20",
          d9["snap"]["axis"] == [50, 20] and d9["snap"]["seg"] == [50, 20]
          and d9["snap"]["ref"] == [50, 50, 20] and d9["toasts"] == 0, d9)

    d10 = json.loads(kv["D10"])
    check("④ D10 默认态负对照：75 / 25 都是白，80 / 20 才是红 / 绿；"
          "cuts(75→85) 只切一次 0.5（默认口径）",
          d10["c75"] == "WH" and d10["c80"] == "UP" and d10["c25"] == "WH"
          and d10["c20"] == "DN" and d10["c19"] == "DN"
          and d10["cuts_75_85"] == [0.5], d10)

    d11 = json.loads(kv["D11"])
    check("④ D11 默认口径负对照：RSI 47 落在 80 / 20 之间 ⇒ 白"
          "（D12 的红确由配置驱动，不是恒红）",
          d11["c47"] == "WH" and d11["snap"]["seg"] == [80, 20], d11)

    d12 = json.loads(kv["D12"])
    check("④ D12 阈值跨中轴（超买 45 < 中轴 50）：取色边界跟 45 走 ——"
          "47 / 45 红、44 / 31 白、30 / 29 青；切点 [0.5] 对应 45"
          "（若 max 把中轴 50 算进来，c47 会变白、cut 会被过滤成空）",
          d12["snap"]["ref"] == [50, 45, 30] and d12["snap"]["axis"] == [50, 45, 30]
          and d12["snap"]["seg"] == [45, 30] and d12["toasts"] == 0
          and d12["c47"] == "UP" and d12["c45"] == "UP" and d12["c44"] == "WH"
          and d12["c31"] == "WH" and d12["c30"] == "DN" and d12["c29"] == "DN"
          and d12["cuts_40_50"] == [0.5], d12)

    # ── 前端显示侧生效值存全局，供 ⑤ 段的端到端对齐取用 ──────────────
    #   （对齐必须在 ⑤ 段做：后端入参要**由前端 body 实际带哪些键**派生，
    #     见 ⑤ 段末尾那两条 —— 直接调 scan_one 会绕过「前端发什么」，判不出分叉。）
    global _FRONT_SEG
    _FRONT_SEG = {"d5": list(d5["snap"]["seg"]), "d3b": list(d3b["snap"]["seg"])}


# ══════════════════════════════════════════════════════════════════════
# ⑤ 请求带参条件（_scanSubmitBody 真跑 + 调用点静态负锚点）
# ══════════════════════════════════════════════════════════════════════
def part5():
    print("══ ⑤ 请求带参条件（_scanSubmitBody 真跑）══")
    appjs = _read(APPJS)

    node = shutil.which("node")
    if not node:
        check("node 不在位，请求体层 SKIP", True)
        return

    harness = (_extract_any_fn(appjs, "_scanSubmitBody") + "\n" + r"""
function B(mode, ro, ru) {
    return _scanSubmitBody([{ code: "sz000001" }],
        { freq: "d", mode: mode, recent: 3, source: "zxg",
          rsi_over: ro, rsi_under: ru });
}
var out = [];
out.push("E1=" + JSON.stringify(Object.keys(B("rsi", 70, 30))));
out.push("E2=" + JSON.stringify([B("rsi", 70, 30).rsi_over, B("rsi", 70, 30).rsi_under]));
// E3：**按侧单独带参** —— 输出每种组合下 body 里实际出现的 rsi_* 键（排序后）。
//   旧口径是「两侧都非 null 才整对带」⇒ 单侧留空时整对不带 ⇒ 后端回 80/20，
//   而副图已按 70 显示 ⇒ 副图与扫描分叉（2026-10-14 修正）。
function RK(mode, ro, ru) {
    return Object.keys(B(mode, ro, ru)).filter(function (k) {
        return k.indexOf("rsi_") === 0;
    }).sort();
}
out.push("E3=" + JSON.stringify([RK("rsi", 70, null), RK("rsi", null, 30),
    RK("rsi", null, null), RK("rsi", undefined, undefined)]));
// E6：单侧带的是**用户原值**（不是被默认值替换后的生效值）
out.push("E6=" + JSON.stringify([B("rsi", 70, null).rsi_over, B("rsi", null, 30).rsi_under,
    ("rsi_under" in B("rsi", 70, null)), ("rsi_over" in B("rsi", null, 30))]));
out.push("E4=" + JSON.stringify([("rsi_over" in B("lianzhang", 70, 30)),
    ("rsi_over" in B("ann", 70, 30)), ("rsi_over" in B("", 70, 30)),
    ("rsi_over" in B("fangliang", 70, 30))]));
out.push("E5=" + JSON.stringify([B("lianzhang", 70, 30).mode, ("rsi_over" in B("rsi", 0, 30))]));
console.log(out.join("\n"));
""")
    jf = tempfile.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    jf.write(harness)
    jf.close()
    try:
        proc = subprocess.run([node, jf.name], capture_output=True, text=True,
                              encoding="utf-8", timeout=60)
    finally:
        os.unlink(jf.name)
    if proc.returncode != 0:
        check("node 执行 _scanSubmitBody harness", False, (proc.stderr or proc.stdout)[:500])
        return
    kv = dict(x.split("=", 1) for x in proc.stdout.splitlines()
              if re.match(r"^[A-Za-z0-9]+=", x))

    e1 = json.loads(kv["E1"])
    check("⑤ 带参：rsi 模式 + 两侧都给 ⇒ body 里出现 rsi_over / rsi_under 两键；"
          "未给的 bsp_types 仍不出现（不凭空补键）",
          set(e1) == {"stocks", "freq", "mode", "recent", "source", "scan_token",
                      "rsi_over", "rsi_under"}, e1)
    e2 = json.loads(kv["E2"])
    check("⑤ 带参值原样：body.rsi_over / rsi_under == 70 / 30（数值，不是字符串）",
          e2 == [70, 30], e2)

    e3 = json.loads(kv["E3"])
    check("⑤ **按侧单独带参**：只配超买 ⇒ 只带 rsi_over；只配超卖 ⇒ 只带 rsi_under；"
          "两侧都没配（null / undefined）⇒ 一个都不带"
          "（2026-10-14 修正：旧口径「任一侧 null 就整对不带」会让单侧配置的用户"
          "副图按 70 显示、扫描按 80 判定 ⇒ 同屏分叉）",
          e3 == [["rsi_over"], ["rsi_under"], [], []], e3)
    e6 = json.loads(kv["E6"])
    check("⑤ 单侧带的是**用户原值**（70 / 30），且另一侧确实**没被凭空补上默认值**"
          "（补了就把后端 SSOT 架空）",
          e6 == [70, 30, False, False], e6)
    # 静态反锚点（2026-10-14 增补）：闸门**不得**用 `&&` 把两侧捆成一句 ——
    #   那正是刚修掉的分叉写法。正则只认 _scanSubmitBody 那一段里的 rsi 带参判定。
    _sub = _extract_any_fn(appjs, "_scanSubmitBody")
    check("⑤ 静态反锚点：带参判定**不得**把 rsi_over / rsi_under 用 `&&` 捆成一句"
          "（「都非 null 才整对带」= 已修掉的分叉写法）",
          not re.search(r"rsi_over\s*!=\s*null\s*&&\s*opts\.rsi_under\s*!=\s*null", _sub)
          and not re.search(r"rsi_over\s*!=\s*null[^;\n]*&&[^;\n]*rsi_under", _sub),
          _sub[-400:] if _sub else "")
    check("⑤ 静态反锚点：也不得在前端补默认值再发（回退必须发生在后端）",
          not re.search(r"body\.rsi_(over|under)\s*=\s*(RSI_SEG_|RSI_DEFAULT_)", _sub),
          _sub[-400:] if _sub else "")

    e4 = json.loads(kv["E4"])
    check("⑤ 模式闸门：lianzhang / ann / \"\"（买·卖点）/ fangliang 四种模式即使给了"
          "阈值也**不带参**（只有 rsi 模式消费）",
          e4 == [False, False, False, False], e4)

    e5 = json.loads(kv["E5"])
    check("⑤ 0 是合法阈值（0 < 超卖 < 超买 < 100 允许小数与 0 以上的任意值，"
          "判据用 `!= null` 而不是真值判断 ⇒ 0 不会被当成「未配置」）",
          e5 == ["lianzhang", True], e5)

    check("⑤ 调用点传的是**用户配置原值** `_rsiOverUser / _rsiUnderUser`"
          "（恰好一处；未配置时它们就是 null）",
          appjs.count("rsi_over: _rsiOverUser, rsi_under: _rsiUnderUser") == 1, "")

    # ── 端到端对齐（2026-10-14 增补，补**真正的缺口**）───────────────
    #   此前：④ 只测「前端显示侧生效 [50,70,20]」、① 只测「后端部分配置独立回退」，
    #   **中间那条「前端到底把什么发给后端」没人管** —— 这正是「副图按 70 着色、
    #   扫描按 80 判定」能溜过去的原因（两端各自都测过、都对，合起来却分叉）。
    #   ⚠ 后端入参必须**由 E3 实测出的 body 键**派生：直接调 `scan_one(70, None)`
    #   等于替前端把 70 送进去了，闸门改成「整对不带」也照样绿 —— 判不出分叉。
    #   ⇒ 旧写法下 E3[0] == [] ⇒ 后端收到 None/None ⇒ 生效 80/20 ≠ 前端 70/20 ⇒ 本条红。
    _fs = globals().get("_FRONT_SEG") or {}
    if not _fs:
        check("⑤ 端到端对齐：④ 段前端生效值未取到（D 段 harness 未产出）", False, _fs)
        return

    def _backend_effect(keys, klines, over_val, under_val):
        """按 body **实际带的键**调后端（缺哪侧就不给哪侧 ⇒ 后端独立回退）。"""
        kw = {}
        if "rsi_over" in keys:
            kw["rsi_over"] = over_val
        if "rsi_under" in keys:
            kw["rsi_under"] = under_val
        return _run(klines, **kw)

    # 单侧（只配超买 70）：K75 尾窗 ≈73.4~76.6 ⇒ 70 命中 / 80 不命中 ⇒ 判别力足够
    _b_one = _backend_effect(e3[0], K75, 70, None)
    check("⑤ 端到端对齐 · **单侧**（只配超买 70）：前端显示侧生效对 ≡ 后端实际收到的"
          "入参下 scan_one 的生效对（旧写法前端整对不带 ⇒ 后端按 80/20 判 ⇒ 与副图"
          "70/20 分叉，本条即判别器）",
          _fs["d5"] == [_b_one.get("rsi_over"), _b_one.get("rsi_under")],
          (_fs["d5"], _b_one.get("rsi_over"), _b_one.get("rsi_under"), e3[0]))
    # 双侧（D3b 的 72 / 28）：K87 尾窗 ≈86.5~88.4 ⇒ 72 命中
    _b_both = _backend_effect(e3[0] + e3[1], K87, 72, 28)
    check("⑤ 端到端对齐 · **双侧**（72 / 28）：前端显示侧生效对 ≡ 后端 scan_one 生效对",
          _fs["d3b"] == [_b_both.get("rsi_over"), _b_both.get("rsi_under")],
          (_fs["d3b"], _b_both.get("rsi_over"), _b_both.get("rsi_under")))
    check("⑤ 负锚点：调用点**不得**改传生效值 `RSI_SEG_OVER / RSI_SEG_UNDER` —— "
          "那样未配置的用户也会显式发 80/20，后端默认值被架空",
          "rsi_over: RSI_SEG_OVER" not in appjs
          and "rsi_under: RSI_SEG_UNDER" not in appjs, "")


# ══════════════════════════════════════════════════════════════════════
# ⑥ 「运行值不再有第二份副本」棘轮
# ══════════════════════════════════════════════════════════════════════
def part6():
    print("══ ⑥ 运行值单源棘轮（第二份副本即红）══")
    appjs = _read(APPJS)

    check("⑥ 派生三件必须是**表达式**，不许写成数字字面量"
          "（RSI_SEG_OVER/UNDER 走 max/min；RSI_REF_VALUES 取三元组；"
          "RSI_AXIS_LEVELS 由 RSI_REF_VALUES 派生）",
          re.search(r"RSI_SEG_OVER\s*=\s*[\d.]+\s*;", appjs) is None
          and re.search(r"RSI_SEG_UNDER\s*=\s*[\d.]+\s*;", appjs) is None
          and re.search(r"RSI_REF_VALUES\s*=\s*\[\s*50\s*,\s*[\d.]+\s*,\s*[\d.]+\s*\]", appjs) is None
          and re.search(r"RSI_AXIS_LEVELS\s*=\s*\[[^\]]*\d[^\]]*\]", appjs) is None, "")

    check("⑥ 取色阈值只许在**阈值对**上取 max/min（`slice(1)`），"
          "不许对整个三元组取 —— 后者会把常量中轴 50 算进来，"
          "用户把超买配到 50 以下时取色边界被顶在 50、"
          "与后端按配置值判的扫描结果分叉（D12 是行为判别器）",
          appjs.count("Math.max.apply(null, RSI_REF_VALUES.slice(1));") == 2
          and appjs.count("Math.min.apply(null, RSI_REF_VALUES.slice(1));") == 2
          and "Math.max.apply(null, RSI_REF_VALUES);" not in appjs
          and "Math.min.apply(null, RSI_REF_VALUES);" not in appjs, "")

    check("⑥ 重算调用点恰好 2 处（启动载入 + 抽屉保存）且定义恰好 1 处 —— "
          "加第三处（如渲染时顺手重算）即红",
          appjs.count("_applyRsiThresholds();") == 2
          and appjs.count("function _applyRsiThresholds()") == 1,
          (appjs.count("_applyRsiThresholds();"),
           appjs.count("function _applyRsiThresholds()")))

    check("⑥ 阈值数字只在**默认兜底常量**定义处各出现一次"
          "（`const RSI_DEFAULT_OVER = 80;` / `const RSI_DEFAULT_UNDER = 20;`）",
          appjs.count("const RSI_DEFAULT_OVER = 80;") == 1
          and appjs.count("const RSI_DEFAULT_UNDER = 20;") == 1, "")

    check("⑥ 参考线 / 纵轴刻度 / 分段着色三处**都读变量**（参考线遍历 RSI_REF_VALUES、"
          "档位过滤 RSI_AXIS_LEVELS、取色比较 RSI_SEG_OVER/UNDER）",
          "RSI_REF_VALUES.forEach(v => {" in appjs
          and "RSI_AXIS_LEVELS.filter(v => v >= range.min && v <= range.max)" in appjs
          and "if (v >= RSI_SEG_OVER) return _isMirrorMode ? COLORS.macdDown : COLORS.macdUp;" in appjs
          and "if (v <= RSI_SEG_UNDER) return _isMirrorMode ? COLORS.macdUp : COLORS.macdDown;" in appjs, "")

    check("⑥ 注释不复述运行值：`drawRsi` 函数头把 80/20 标成**默认**并指向 "
          "RSI_REF_VALUES / _applyRsiThresholds（配置化后写死数字即陈旧文档）",
          "默认 80 / 20，见 RSI_REF_VALUES / _applyRsiThresholds" in appjs
          and "RSI 折线（单线**分段着色**）+ 50 中轴 + 80/20 超买超卖参考线。" not in appjs, "")


def main():
    print("=" * 70)
    print("RSI 阈值可配置（前端显示 + 后端扫描同吃一份配置）护栏")
    print("=" * 70)
    # 单段异常不吞后续段、也不让整测崩成 traceback（否则汇总行不打印、
    # 后面的断言一个都跑不到）—— 该段计一条 FAIL，其余段照跑。
    for fn in (part1, part2, part3, part4, part5, part6):
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 —— 护栏自身的兜底，非被测代码
            check("%s 段执行未抛异常" % fn.__name__, False,
                  "%s: %s" % (type(exc).__name__, exc))
    print("-" * 70)
    total, passed = len(_OK), sum(_OK)
    print("合计 %d 项，通过 %d，失败 %d" % (total, passed, total - passed))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
