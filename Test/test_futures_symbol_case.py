# -*- coding: utf-8 -*-
"""期货符号大小写规范化 · 契约测试（零网络）

背景（2026-10-20 用户实测）：
  输入 `KQ.m@CFFEX.Im` 报
      `contains non-existent instrument: KQ.m@CFFEX.Im`
  而 `KQ.m@CFFEX.IM` 能正常连上 —— 天勤 instrument_id **大小写敏感**。
  但**不能**用统一 upper()/lower() 修：各交易所品种根大小写并不统一，这是
  交易所原生代码（中金所/郑商所大写 IF/MA，上期所/大商所/能源/广期所小写
  rb/m/sc/lc）。全大写会把 `SHFE.rb` 变成 `SHFE.RB`，全小写会把 `CFFEX.IM`
  变成 `CFFEX.im` —— 两种都是把能用的改成不能用的。

本测试锁死的断言：
  [1] 规范表由既有两表派生、无「同一品种两种写法」的歧义；
  [2] 用户报的那条输入 `KQ.m@CFFEX.Im` → `KQ.m@CFFEX.IM`；
  [3] **负向（最关键）**：小写品种根不得被放大 —— `SHFE.rb2410` 保持小写、
      `DCE.m` 保持小写、`CZCE.MA` 保持大写；全大写/全小写输入都要被纠正回
      各自的事实写法（证明"不能统一大小写"这条前提真的成立）；
  [4] 股票/指数代码不被误判成期货（返回 None）；
  [5] 幂等：规范化结果再规范化一次不变；
  [6] `_get_futures_code` 与 `normalize_futures_symbol` 同源（委托，不是第二份实现）；
  [7] `AppUtils._get_market_code` 不再预先 upper()（会把品种根写坏）；
  [8] AppSSE 三处入口统一走同一函数，无残留的 `symbol_upper` 别名解析副本；
  [9] 前端 `isFuturesCode` 大小写不敏感、且全站只剩一处定义（用 node 跑真实函数）；
  [10] **静态**：`isFuturesCode` 只允许被调用 —— 裸引用得到函数对象、在条件里恒真
       （2026-10-20 实测后果：所有代码都走期货 SSE，`sh000001` 被送进天勤报
        non-existent instrument）；
  [11] 期货 SSE 入口对股票/指数代码回可读错误，**不连天勤**（省 1.2s + websocket）。

跑法：python Test/test_futures_symbol_case.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_HERE = os.path.dirname(os.path.abspath(__file__))
_RROOT = os.path.dirname(_HERE)
if _RROOT not in sys.path:
    sys.path.insert(0, _RROOT)

from DataAPI.TqSdkAPI import (  # noqa: E402
    _FUTURES_ROOT_CANON,
    _get_futures_code,
    normalize_futures_symbol,
)

APP_JS = os.path.join(_RROOT, "Frontend", "app.js")
APP_SSE = os.path.join(_RROOT, "App", "AppSSE.py")
APP_UTILS = os.path.join(_RROOT, "App", "AppUtils.py")

failures = []


def check(label, got, want):
    if got != want:
        failures.append("%s: 期望 %r，实际 %r" % (label, want, got))
        print("[FAIL] %s: 期望 %r，实际 %r" % (label, want, got))
        return False
    print("[PASS] %s → %r" % (label, got))
    return True


def check_true(label, cond, detail=""):
    if not cond:
        failures.append("%s%s" % (label, ("：" + detail) if detail else ""))
        print("[FAIL] %s %s" % (label, detail))
        return False
    print("[PASS] %s" % label)
    return True


def test_canon_table():
    """[1] 规范表派生自既有两表，且无歧义。"""
    print("\n[1] 规范表（派生自 FUTURES_NAMES / FUTURES_ALIASES）")
    check_true("规范表非空", len(_FUTURES_ROOT_CANON) >= 60,
               "实际 %d 条" % len(_FUTURES_ROOT_CANON))
    # 构建函数在发现「同一 (所,根) 两种写法」时会抛错；能走到这里说明无歧义。
    # 再抽样确认「大写所 vs 小写所」两类都在表里，否则归一逻辑没被真正覆盖。
    check("中金所 IF 规范根", _FUTURES_ROOT_CANON.get(("CFFEX", "IF")), "IF")
    check("上期所 RB 规范根", _FUTURES_ROOT_CANON.get(("SHFE", "RB")), "rb")
    check("大商所 M 规范根", _FUTURES_ROOT_CANON.get(("DCE", "M")), "m")
    check("郑商所 MA 规范根", _FUTURES_ROOT_CANON.get(("CZCE", "MA")), "MA")
    check("能源 SC 规范根", _FUTURES_ROOT_CANON.get(("INE", "SC")), "sc")
    check("广期所 LC 规范根", _FUTURES_ROOT_CANON.get(("GFEX", "LC")), "lc")


def test_user_reported_input():
    """[2] 用户报的那条输入。"""
    print("\n[2] 用户报错输入（KQ.m@CFFEX.Im）")
    check("KQ.m@CFFEX.Im", normalize_futures_symbol("KQ.m@CFFEX.Im"), "KQ.m@CFFEX.IM")
    check("KQ.m@CFFEX.IM（已规范，应原样）",
          normalize_futures_symbol("KQ.m@CFFEX.IM"), "KQ.m@CFFEX.IM")
    check("KQ.M@CFFEX.IM（前缀 m 归小写）",
          normalize_futures_symbol("KQ.M@CFFEX.IM"), "KQ.m@CFFEX.IM")
    check("kq.m@cffex.im（全小写）",
          normalize_futures_symbol("kq.m@cffex.im"), "KQ.m@CFFEX.IM")


def test_negative_no_blanket_case():
    """[3] 负向：不得统一大小写 —— 各所事实写法必须被精确保留。"""
    print("\n[3] 负向：不能统一 upper()/lower()")
    # 小写所：任何输入都要落回小写
    for src_ in ("KQ.m@SHFE.RB2410", "KQ.m@SHFE.rb2410", "KQ.M@SHFE.Rb2410"):
        check("%s 归一小写品种根" % src_,
              normalize_futures_symbol(src_), "KQ.m@SHFE.rb2410")
    check("DCE.M → m", normalize_futures_symbol("KQ.m@DCE.M"), "KQ.m@DCE.m")
    check("INE.SC → sc", normalize_futures_symbol("KQ.m@INE.SC"), "KQ.m@INE.sc")
    check("GFEX.LC → lc", normalize_futures_symbol("KQ.m@GFEX.LC"), "KQ.m@GFEX.lc")
    # 大写所：任何输入都要落回大写
    for src_ in ("KQ.m@CZCE.ma", "KQ.m@czce.MA", "KQ.m@CZCE.Ma"):
        check("%s 归一大写品种根" % src_,
              normalize_futures_symbol(src_), "KQ.m@CZCE.MA")
    check("CFFEX.im → IM", normalize_futures_symbol("CFFEX.im"), "CFFEX.IM")
    # 直接证明「全大写会写坏小写品种」这条前提：若不查表而 upper，rb 会变 RB
    check_true("全大写化会写坏小写品种（前提校验）",
               normalize_futures_symbol("KQ.m@SHFE.RB") != "KQ.m@SHFE.RB",
               "规范化把 RB 还原成 rb，说明 upper 方案不可行")


def test_non_futures():
    """[4] 股票/指数代码不得被误判。"""
    print("\n[4] 非期货代码返回 None")
    for code in ("600519", "sh600519", "sz000001", "SZZS", "hk00700", "",
                 "   ", None, "GZMT"):
        check("%r 不是期货" % (code,), normalize_futures_symbol(code), None)


def test_idempotent():
    """[5] 幂等。"""
    print("\n[5] 幂等")
    for code in ("KQ.m@CFFEX.Im", "KQ.m@SHFE.RB2410", "PTA", "cffex.if2409",
                 "KQ.m@CZCE.ma", "KQD.M@SGX.CN"):
        once = normalize_futures_symbol(code)
        twice = normalize_futures_symbol(once)
        check("%r 归一两次相同" % code, twice, once)


def test_get_futures_code_delegates():
    """[6] _get_futures_code 是委托，不是第二份实现。"""
    print("\n[6] _get_futures_code 委托")
    for code in ("KQ.m@CFFEX.Im", "SHFE.RB2410", "PTA", "if", "600519", "SZZS"):
        check("%r 两条入口结果一致" % code,
              _get_futures_code(code), normalize_futures_symbol(code))
    with open(os.path.join(_RROOT, "DataAPI", "TqSdkAPI.py"), encoding="utf-8") as f:
        src = f.read()
    m = re.search(r"def _get_futures_code\(code\):\n(?:.*?\n)*?    return ([^\n]+)\n", src)
    check_true("_get_futures_code 函数体已委托", bool(m) and
               m.group(1).strip() == "normalize_futures_symbol(code)",
               "实际 return %r" % (m.group(1) if m else None))
    # 服务层（AppSSE）走的是类这一层：`CTqSdkAPI.normalize_futures_symbol`。
    # 光有模块级函数不够 —— 本轮首次提交就漏了这层挂载，导致 sse_sequence /
    # sse_gray / sse_concurrent / futures_replay_window / futures_goto_earlier
    # 五条用例一起红（AttributeError）。这条断言把「挂载」钉住。
    from DataAPI.TqSdkAPI import CTqSdkAPI
    check_true("CTqSdkAPI.normalize_futures_symbol 已挂载（服务层入口）",
               hasattr(CTqSdkAPI, "normalize_futures_symbol"),
               "类上缺该方法 → AppSSE 三处入口会 AttributeError")
    check("类入口行为与模块级一致",
          CTqSdkAPI.normalize_futures_symbol("KQ.m@CFFEX.Im"), "KQ.m@CFFEX.IM")
    check("类入口非期货返回 None",
          CTqSdkAPI.normalize_futures_symbol("600519"), None)


def test_market_code_no_upper():
    """[7] AppUtils 不得预先 upper（会把 SHFE.rb 写成 SHFE.RB）。"""
    print("\n[7] _get_market_code 不预 upper")
    with open(APP_UTILS, encoding="utf-8") as f:
        src = f.read()
    check_true("_get_market_code 内无 _get_futures_code(code.upper())",
               "_get_futures_code(code.upper())" not in src)
    from App.AppUtils import _get_market_code
    check("KQ.m@CFFEX.Im 识别为期货",
          _get_market_code("KQ.m@CFFEX.Im"), ("futures", "KQ.m@CFFEX.IM"))
    check("SHFE.RB2410 识别为期货",
          _get_market_code("SHFE.RB2410"), ("futures", "SHFE.rb2410"))
    check("ptA 识别为期货主连",
          _get_market_code("ptA"), ("futures", "KQ.m@CZCE.TA"))
    check("600519 识别为股票",
          _get_market_code("600519"), ("sh", "600519"))


def test_appsse_single_entry():
    """[8] AppSSE 三处入口统一，无残留副本。"""
    print("\n[8] AppSSE 统一入口")
    with open(APP_SSE, encoding="utf-8") as f:
        src = f.read()
    check_true("无残留 symbol_upper 别名解析",
               "symbol_upper in CTqSdkAPI.FUTURES_ALIASES" not in src)
    # 双窗（12 空格缩进）1 处 + 单窗（8 空格）1 处 + 定位入口（4 空格）1 处
    n = len(re.findall(r"CTqSdkAPI\.normalize_futures_symbol\(symbol\) or symbol", src))
    check_true("三处入口均走 normalize_futures_symbol", n == 3, "实际 %d 处" % n)


def test_frontend_is_futures_code():
    """[9] 前端判定：大小写不敏感 + 唯一实现。"""
    print("\n[9] 前端 isFuturesCode")
    with open(APP_JS, encoding="utf-8") as f:
        js = f.read()
    check_true("全站只有一处 function isFuturesCode",
               js.count("function isFuturesCode(code)") == 1,
               "实际 %d 处" % js.count("function isFuturesCode(code)"))
    check_true("无局部 const isFuturesCode 副本",
               "const isFuturesCode" not in js)
    check_true("别名表只有一处定义",
               js.count("const FUTURES_ALIAS_KEYS = new Set(") == 1)
    check_true("旧的大小写敏感 includes('KQ.m@') 已移除",
               "code.includes('KQ.m@')" not in js)

    node = shutil.which("node")
    if not node:
        print("[SKIP] node 不在位，前端判定行为层跳过")
        return
    start = js.find("const FUTURES_ALIAS_KEYS = new Set(")
    end = js.find(";", start)
    alias_line = js[start:end + 1]
    fn_start = js.find("function isFuturesCode(code)")
    depth, j = 0, js.find("{", fn_start)
    while j < len(js):
        if js[j] == "{":
            depth += 1
        elif js[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    fn_src = js[fn_start:j + 1]
    cases = {
        "kq.m@cffex.im": True, "KQ.M@CFFEX.IM": True, "KQD.M@SGX.CN": True,
        "cffex.if2409": True, "shfe.rb2410": True,
        "IF": True, "if": True, "pta": True, "rb": True,
        "600519": False, "sh600519": False, "SZZS": False, "hk00700": False,
    }
    harness = "\n".join([
        alias_line,
        fn_src,
        "var cases = " + json.dumps(cases) + ";",
        "var out = {};",
        "for (var k in cases) { out[k] = isFuturesCode(k); }",
        "console.log(JSON.stringify(out));",
    ]) + "\n"
    fd, jf = tempfile.mkstemp(suffix=".js", prefix="isfut_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(harness)
        proc = subprocess.run([node, jf], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=60)
    finally:
        try:
            os.unlink(jf)
        except OSError:
            pass
    if proc.returncode != 0:
        failures.append("前端 isFuturesCode 执行失败: " + (proc.stderr or "")[:300])
        print("[FAIL] 前端 isFuturesCode 执行失败")
        return
    got = json.loads((proc.stdout or "").strip().splitlines()[-1])
    for k, want in cases.items():
        check("isFuturesCode(%r)" % k, got.get(k), want)



def _strip_js_comments(src):
    """去掉 JS 注释（保留换行，行号仍对得上）；字符串与正则字面量原样保留。

    必须识别正则字面量：app.js 里有 `replace(/'/g, ...)`，只按引号配对的朴素
    扫描会把它当成字符串开头，从此引号状态全乱（后续注释都剥不掉）。
    """
    out = []
    i, n = 0, len(src)
    quote = None
    last_sig = ""      # 上一个非空白字符，用来判 `/` 是除号还是正则开头
    while i < n:
        c = src[i]
        if quote:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
            last_sig = c
            i += 1
            continue
        if c in "\"'`":
            quote = c
            out.append(c)
            last_sig = c
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            while i < n and src[i] not in "\r\n":
                i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            i += 2
            while i + 1 < n and not (src[i] == "*" and src[i + 1] == "/"):
                if src[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
            continue
        if c == "/" and (last_sig == "" or last_sig in "(,=:[!&|?{};+-*%<>~^"):
            j = i + 1
            in_class = False
            while j < n:
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_class = True
                elif ch == "]":
                    in_class = False
                elif ch == "/" and not in_class:
                    j += 1
                    break
                elif ch == "\n":
                    break
                j += 1
            out.append(src[i:j])
            last_sig = "/"
            i = j
            continue
        out.append(c)
        if not c.isspace():
            last_sig = c
        i += 1
    return "".join(out)


def test_frontend_no_bare_reference():
    """[10] 前端 isFuturesCode 只能被**调用**，不得当布尔用（静态判据）。"""
    print("\n[10] 前端 isFuturesCode 无裸引用")
    with open(APP_JS, encoding="utf-8") as f:
        raw = f.read()
    src = _strip_js_comments(raw)
    bad = []
    for m in re.finditer(r"\bisFuturesCode\b", src):
        pos = m.start()
        before = src[max(0, pos - 40):pos]
        if re.search(r"\bfunction\s+$", before):
            continue                      # 函数定义本身
        rest = src[pos + len("isFuturesCode"):]
        if rest.lstrip().startswith("("):
            continue                      # 正常调用
        line = src.count("\n", 0, pos) + 1
        bad.append("L%d: %s" % (line, src[pos:pos + 30].split("\n")[0]))
    check_true("全部引用都是调用（无「函数对象当布尔」写法）", not bad,
               "裸引用 %d 处：%s" % (len(bad), bad[:4]))
    check_true("loadStock 的判据只算一次",
               src.count("const targetIsFutures = isFuturesCode(code);") == 1,
               "实际 %d 处" % src.count("const targetIsFutures = isFuturesCode(code);"))


class _NoConnectSource(object):
    """假数据源：只用来证明「股票代码不会走到 src.connect()」。"""

    def connect(self):
        raise AssertionError("股票代码不应连天勤")

    def close(self):
        pass

    def close_api(self):
        pass

    def cleanup_records(self, key):
        pass


def _first_frame(gen):
    try:
        return next(gen)
    except StopIteration:
        return b""
    except Exception as e:      # 变异（守卫被删）时假源会抛：转成可读的失败信息
        return ("EXC: %s: %s" % (type(e).__name__, e)).encode("utf-8")


def test_sse_rejects_non_futures():
    """[11] 期货 SSE 入口：股票/指数代码回可读错误，且**不连天勤**。"""
    print("\n[11] 期货 SSE 拒绝股票代码")
    from App.AppSSE import _non_futures_reject, _sse_single_gen, _sse_dual_gen

    for code in ("sh000001", "sz399001", "SZ399001", "bj430047", "hk00700"):
        check_true("拒绝 %s" % code, bool(_non_futures_reject(code)), "未拦下")
    for code in ("KQ.m@CFFEX.IM", "CFFEX.IF2409", "IF", "pta", "KQD.M@SGX.CN", ""):
        check_true("放行 %r" % code, _non_futures_reject(code) is None,
                   "被误拦：%r" % (_non_futures_reject(code),))

    gen = _sse_single_gen("sh000001", "5m", source=_NoConnectSource())
    frame = _first_frame(gen)
    text = frame.decode("utf-8") if isinstance(frame, bytes) else str(frame)
    check_true("单窗回 init 错误帧", text.startswith("event: init"), text[:80])
    check_true("单窗错误文案点明股票接口", "/api/stocks/sh000001/analyze" in text, text[:200])
    try:
        next(gen)
        _more = True
    except StopIteration:
        _more = False
    check_true("单窗发完错误帧即结束（不进入实时循环）", not _more, "还有后续帧")

    frame2 = _first_frame(_sse_dual_gen("sh000001", "5m", "1m", source=_NoConnectSource()))
    text2 = frame2.decode("utf-8") if isinstance(frame2, bytes) else str(frame2)
    check_true("双窗回 init 错误帧", text2.startswith("event: init"), text2[:80])


def main():
    test_canon_table()
    test_user_reported_input()
    test_negative_no_blanket_case()
    test_non_futures()
    test_idempotent()
    test_get_futures_code_delegates()
    test_market_code_no_upper()
    test_appsse_single_entry()
    test_frontend_is_futures_code()
    test_frontend_no_bare_reference()
    test_sse_rejects_non_futures()
    print()
    if failures:
        print("===== 期货符号大小写规范化: 失败 %d 项 =====" % len(failures))
        for x in failures:
            print(" -", x)
        return False
    print("===== 期货符号大小写规范化: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
