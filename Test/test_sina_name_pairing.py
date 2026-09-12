# -*- coding: utf-8 -*-
"""A 股名称补全「拼参顺序」护栏（2026-09-12 回归）

回归背景（真实事故，非假想）：
  `SinaAPI.fetch_a_names` 的入参契约是 list[(bare_code, market)]，如
  ("600519", "sh")。但拼接写成 `f"{m}{c}"`，把顺序解反 → 请求 URL 成了
  `list=600519sh`。**新浪对非法代码返回 HTTP 200 + `hq_str_sys_auth="FAILED"`**
  —— 不抛异常、也没有任何 ERROR，于是 5367 只 A 股名称被静默全部丢弃。

  后果链：补全全失败 → 步骤5 把无名条目过滤掉 → 只剩「缓存里原有名字的」
  → 若照旧落盘 + replace_names，**完好的名称表被换成残表**，表现为
  页面股票名退化成代码（AppEngine 的 `market+code` 兜底）、拼音搜不到
  （输入 GZMT 联想不出贵州茅台），且重启也不恢复。

本用例**完全不联网**：把 `urllib.request.urlopen` 换成打桩，断言
  ① 请求 URL 必须是 `sh600519`（market 前缀在 code 之前），绝不能是 `600519sh`;
  ② 两种入参顺序（(code, market) / (market, code)）都归一为同一正确 URL;
  ③ 解析结果落到 `market+code` 键;
  ④ 整批无效返回时输出 WARNING —— 这类静默失败必须留痕;
  ⑤ 跨批（>batch_size）时每一批的 URL 都正确。

运行：python Test/test_sina_name_pairing.py     # 非 0 退出即失败
"""
import contextlib
import logging
import os
import sys
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)

from DataAPI import SinaAPI                                 # noqa: E402

results = []


def rec(tag, name, ok, detail=""):
    results.append((ok, tag, name, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {tag} {name}: {detail}")


# ═══════════════════════════════════════════════════════════════════════
# 打桩：把新浪响应换成「按请求代码回伪造行情串」，不回真网络
# ═══════════════════════════════════════════════════════════════════════
class _FakeResp:
    status = 200

    def __init__(self, payload):
        self._payload = payload

    def read(self):
        return self._payload


def _make_fake_urlopen(urls):
    """记录每次请求的 URL；按 list= 里的代码回伪造 GBK 响应。

    合法代码 `sh600519` → `var hq_str_sh600519="测试名600519,...";`
    非法代码（market 不在前，如 `600519sh`）→ 复刻新浪真实行为：
        返回 `var hq_str_sys_auth="FAILED";`，HTTP 仍是 200。
    """

    def _fake(req, timeout=None):                           # noqa: ARG001
        url = req.full_url if hasattr(req, "full_url") else str(req)
        urls.append(url)
        codes = url.split("list=", 1)[-1].split(",") if "list=" in url else []
        lines = []
        for raw in codes:
            c = raw.strip()
            if len(c) > 2 and c[:2].lower() in ("sh", "sz", "bj"):
                lines.append(f'var hq_str_{c.lower()}="测试名{c[2:]},1,2,3,4,5";')
            else:
                lines.append('var hq_str_sys_auth="FAILED";')
        return _FakeResp(("\n".join(lines) + "\n").encode("gbk"))

    return _fake


def _make_failing_urlopen():
    """复刻事故当天的真实响应：HTTP 200 但只有 sys_auth="FAILED"，无任何有效行。"""

    def _fake(req, timeout=None):                           # noqa: ARG001
        return _FakeResp(b'var hq_str_sys_auth="FAILED";\n')

    return _fake


@contextlib.contextmanager
def _patched_urlopen(urls, responder=None):
    orig = urllib.request.urlopen
    urllib.request.urlopen = responder or _make_fake_urlopen(urls)
    try:
        yield
    finally:
        urllib.request.urlopen = orig


class _Captured(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []

    def emit(self, record):
        self.records.append(record)


def test_url_order_is_market_first():
    """① 正序入参：(bare_code, market) → URL 必须是 sh600519 形式"""
    urls = []
    with _patched_urlopen(urls):
        got = SinaAPI.fetch_a_names([("600519", "sh"), ("000001", "sz")])
    joined = urls[0] if urls else ""
    ok = joined.endswith("/list=sh600519,sz000001")
    rec("①", "请求 URL 为 market 前缀在前", ok,
        f"url={joined!r} 返回={got!r}")
    rec("①b", "绝不能拼出 code+market（本次事故形态）",
        "600519sh" not in joined and "000001sz" not in joined,
        f"url={joined!r}")


def test_reversed_input_normalized():
    """② 反序入参：(market, code) 也应归一为同一正确 URL"""
    urls = []
    with _patched_urlopen(urls):
        got = SinaAPI.fetch_a_names([("sh", "600519"), ("sz", "000001")])
    joined = urls[0] if urls else ""
    ok = joined.endswith("/list=sh600519,sz000001") and len(got) == 2
    rec("②", "反序入参被归一（容错两种写法）", ok, f"url={joined!r} 返回={got!r}")


def test_result_key_is_market_plus_code():
    """③ 解析结果键为 market+code，值为名称"""
    urls = []
    with _patched_urlopen(urls):
        got = SinaAPI.fetch_a_names([("600519", "sh"), ("000001", "sz")])
    ok = got == {"sh600519": "测试名600519", "sz000001": "测试名000001"}
    rec("③", "解析键= market+code、值=名称", ok, f"返回={got!r}")


def test_invalid_codes_yield_warning():
    """④ 整批无有效返回时必须 WARNING（让静默失败留痕），且不抛异常"""
    lg = logging.getLogger("DataAPI.SinaAPI")
    old_level = lg.level
    cap = _Captured()
    lg.addHandler(cap)
    lg.setLevel(logging.WARNING)
    try:
        with _patched_urlopen([], responder=_make_failing_urlopen()):
            got = SinaAPI.fetch_a_names([("600519", "sh"), ("000001", "sz")])
    finally:
        lg.removeHandler(cap)
        lg.setLevel(old_level)
    warned = any(r.levelno >= logging.WARNING for r in cap.records)
    rec("④", "整批无有效返回 → WARNING 留痕且不抛异常",
        warned and got == {}, f"warned={warned} 返回={got!r}")


def test_batching_keeps_correct_order():
    """⑤ 跨批（60 只 / batch_size=50）时每批 URL 都正确"""
    urls = []
    pairs = [(str(600000 + i), "sh") for i in range(60)]
    with _patched_urlopen(urls):
        got = SinaAPI.fetch_a_names(pairs, interval=0)
    expected_first = "/list=" + ",".join(f"sh{600000 + i}" for i in range(50))
    expected_second = "/list=" + ",".join(f"sh{600000 + i}" for i in range(50, 60))
    ok = (len(urls) == 2 and urls[0].endswith(expected_first)
          and urls[1].endswith(expected_second) and len(got) == 60)
    rec("⑤", "跨批每批 URL 顺序正确", ok,
        f"批数={len(urls)} 命中={len(got)}/60 首批结尾={urls[0][-24:] if urls else ''!r}")


def test_sh_sz_same_bare_code_not_lost():
    """⑥ 沪深同号（sh000001 上证指数 / sz000001 平安银行）必须两条都拿到名字。

    回归背景：AppRefresh._fetch_names_from_sina_once 里的 compound_key_map 曾
    用**裸代码**当键（`compound_key_map[bare_code] = compound_key`），沪深同号
    时后遍历到的一只（sz）会覆盖前一只（sh）的映射 → 上证指数拿不到名字 →
    被刷新步骤5 的「无名称」过滤删除 → 搜索框输入 000001 只剩平安银行一条。
    该缺陷在「新浪拼参修正（本文件 ①~⑤）」之前被掩盖：那时新浪整批返回空，
    两只都没名字，症状不同但同样是坏的。
    """
    from App import AppRefresh as R

    def fake_fetch_a_names(mkt_code_pairs, batch_size=50, timeout=15, interval=0.3):
        return {m + c: f"名称{m}{c}" for c, m in mkt_code_pairs}

    orig = R.fetch_a_names
    R.fetch_a_names = fake_fetch_a_names
    try:
        codes = {
            "sh000001": {"name": "", "market": "sh"},
            "sz000001": {"name": "", "market": "sz"},
            "sh600519": {"name": "", "market": "sh"},
        }
        R._fetch_names_from_sina_once(codes)
        lost = sorted(k for k, v in codes.items() if not v.get("name"))
        ok = (not lost
              and codes["sh000001"]["name"] == "名称sh000001"
              and codes["sz000001"]["name"] == "名称sz000001")
        rec("⑥", "沪深同号两只都拿到名称（000001 不丢上证指数）", ok,
            f"结果={ {k: v['name'] for k, v in codes.items()} }；无名称={lost or '无'}")
    finally:
        R.fetch_a_names = orig


def main():
    print("=" * 64)
    print("A 股名称补全「拼参顺序」护栏（2026-09-12 回归）")
    print("=" * 64)
    for fn in (test_url_order_is_market_first, test_reversed_input_normalized,
               test_result_key_is_market_plus_code, test_invalid_codes_yield_warning,
               test_batching_keeps_correct_order,
               test_sh_sz_same_bare_code_not_lost):
        try:
            fn()
        except Exception as e:                              # noqa: BLE001
            rec(fn.__name__, "执行异常", False, f"{type(e).__name__}: {e}")
    failed = [r for r in results if not r[0]]
    print("=" * 64)
    print(f"合计 {len(results)} 项，通过 {len(results) - len(failed)}，失败 {len(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
