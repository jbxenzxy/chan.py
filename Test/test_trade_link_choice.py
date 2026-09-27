# -*- coding: utf-8 -*-
"""登录链路选择（SimNow 仿真 / 实盘 CTP）—— 行为护栏。

需求原文（2026-09-28 用户）：
  ⑴ 自动下单当前支持 SimNow 和实盘两种登录方式；
  ⑵ 两种方式的账号密码都在环境变量，但其它配置要改 .env 或配置文件，
     切换登录方式很不方便；
  ⑶ 开启页面上的「自动下单」开关 → 先弹对话框，提供 SimNow 和实盘两个选项，
     默认 SimNow，选项支持持久化，用户选一个点「确认」完成登录，
     无需手工改 .env 或配置文件；点「取消」（点对话框外的区域等价）则取消。

用户同日拍板的三个设计点（本用例据此定判据）：
  · 期货公司名（tq_market）在 .env 配一次，不每次弹输入框；
  · 实盘安全闸门保留双层：.env 的 confirm_live_trading=true 是「有实盘资格」
    的总开关，界面选择只是第二层确认，不取代它；
  · 界面上要能看出当前跑的是哪条链路（顶栏徽标）。

各断言防什么（摘掉对应实现必须变红）：
  · [1] 组：选 simnow 时 broker 三件套落对值 —— 落错会连到实盘或起不来；
  · [2] 组：实盘的两个前置（期货公司名 / 资格开关）缺任一必须拒绝 ——
    放过去就是"以为在仿真其实在真钱"；
  · [2c]：选 SimNow 时，即便 .env 里填着期货公司名也不许被实盘闸门误拦
    （判据必须取选定值，不能取配置原值）—— 这条是本次改出来的坑，
    不钉住就会回潮成"选了仿真还要先开实盘资格开关"；
  · [3] 组：注入子进程的环境变量键名必须真是 Config 消费的那三个 ——
    键名写错（如漏了 TRADING_ 前缀）不会报错，只会静默沿用 .env，
    整个功能退化成"点了没反应"（最难查的一类失效）；
  · [4] 组：持久化往返 + 非法值不认（防脏数据把默认项带歪）；
  · [5]/[6] 组：前端静态与真函数（node）—— 静态断言证不了渲染结果，
    禁用项必须真的带 disabled、默认选中必须真的落在可用项上。

跑法：python Test/test_trade_link_choice.py
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

_PASS = 0
_FAIL = 0


def check(name, got, want):
    global _PASS, _FAIL
    ok = got == want
    if ok:
        _PASS += 1
        print("  ✓ {}".format(name))
    else:
        _FAIL += 1
        print("  ✗ {} -> got {!r}, want {!r}".format(name, got, want))


TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO)

with open(os.path.join(REPO, "Frontend", "app.html"), encoding="utf-8") as f:
    HTML = f.read()
with open(os.path.join(REPO, "Frontend", "app.js"), encoding="utf-8") as f:
    JS = f.read()

from App.AppErrors import BadRequestError                      # noqa: E402
from App.AppTrader import (                                    # noqa: E402
    AppTrader, LINK_CHOICES, LINK_LIVE, LINK_SIMNOW, LINK_STATE_KEY,
)


def _cfg(broker="dry_run", market="simnow", confirm=False):
    """构造一份 TradingConfig（不读 .env：显式传参压过一切环境变量）。"""
    from Trading.Config import TradingConfig
    return TradingConfig(broker=broker,
                         broker_params={"tq_market": market,
                                        "confirm_live_trading": confirm})


# ═══ ① 解析：选择 → broker 三件套 ═══
print("\n[1] 链路解析（选择 → 子进程生效值）")
check("① 未指定链路 → None（沿用配置，等价改造前）",
      AppTrader._resolve_link(_cfg(), None), None)
check("① 未指定链路（空串）同 None",
      AppTrader._resolve_link(_cfg(), ""), None)
check("① 选 SimNow → broker/tq_market/confirm 三件套",
      AppTrader._resolve_link(_cfg(), LINK_SIMNOW),
      {"link": "simnow", "broker": "simnow",
       "tq_market": "simnow", "confirm": False})
check("① 选实盘（已配期货公司 + 已开资格）→ 三件套",
      AppTrader._resolve_link(_cfg(market="创元期货", confirm=True), LINK_LIVE),
      {"link": "live", "broker": "live",
       "tq_market": "创元期货", "confirm": True})
check("① 大写/带空格的链路名归一（LIVE 也算实盘）",
      AppTrader._resolve_link(_cfg(market="创元期货", confirm=True), " LIVE "),
      {"link": "live", "broker": "live",
       "tq_market": "创元期货", "confirm": True})
try:
    AppTrader._resolve_link(_cfg(), "nonsense")
    check("① 未知链路 → BadRequestError", "no-raise", "raise")
except BadRequestError:
    check("① 未知链路 → BadRequestError", "raise", "raise")
check("① 可选链路恰两项（simnow / live）", sorted(LINK_CHOICES),
      ["live", "simnow"])

# ═══ ② 实盘的两个前置（缺任一即拒绝）═══
print("\n[2] 实盘前置：期货公司名 + 资格开关（双层闸门）")
try:
    AppTrader._resolve_link(_cfg(market="simnow", confirm=True), LINK_LIVE)
    check("② 未配期货公司选实盘 → 拒绝", "no-raise", "raise")
except BadRequestError as e:
    check("② 未配期货公司选实盘 → 拒绝", "raise", "raise")
    check("② 拒绝文案指路 TQ_MARKET（用户照着改即可）",
          "TRADING_BROKER_PARAMS__TQ_MARKET" in str(e), True)
try:
    AppTrader._resolve_link(_cfg(market="创元期货", confirm=False), LINK_LIVE)
    check("② 资格开关未开选实盘 → 拒绝", "no-raise", "raise")
except BadRequestError as e:
    check("② 资格开关未开选实盘 → 拒绝", "raise", "raise")
    check("② 拒绝文案指路 CONFIRM_LIVE_TRADING",
          "TRADING_BROKER_PARAMS__CONFIRM_LIVE_TRADING" in str(e), True)

# ②c 选 SimNow 时闸门不得按配置原值判（配置里填着期货公司名也不算实盘）
try:
    AppTrader._check_live_gate(_cfg(market="创元期货", confirm=False),
                               "simnow", market="simnow", confirm=False)
    check("②c 选 SimNow：配置里填着期货公司名也放行（闸门取选定值）",
          "pass", "pass")
except BadRequestError as e:
    check("②c 选 SimNow：配置里填着期货公司名也放行（闸门取选定值）",
          "raised: {}".format(e), "pass")
# 反向：不传覆盖值时仍按配置判 —— broker=simnow 但 .env 填着期货公司名、
# 又没开资格开关，这条在改造前就被拦（闸门只看配置），改造后照旧。
try:
    AppTrader._check_live_gate(_cfg(market="创元期货", confirm=False), "simnow")
    check("②c 未选链路时闸门仍按配置判（改造前行为不变：照拦）",
          "pass", "raised")
except BadRequestError:
    check("②c 未选链路时闸门仍按配置判（改造前行为不变：照拦）",
          "raised", "raised")

# ═══ ③ 选项可用性（前端据此置灰）═══
print("\n[3] 选项可用性（link_options）")
opts = AppTrader.link_options(_cfg())["options"]
check("③ 恰好两个选项", len(opts), 2)
check("③ 选项顺序 = SimNow 在前、实盘在后",
      [o["value"] for o in opts], ["simnow", "live"])
check("③ SimNow 恒可用", opts[0]["enabled"], True)
check("③ 默认配置下实盘不可用（未配期货公司）", opts[1]["enabled"], False)
check("③ 不可用时给出原因（前端置灰展示，不静默）",
      bool(opts[1]["reason"]), True)
opts2 = AppTrader.link_options(_cfg(market="创元期货", confirm=True))["options"]
check("③ 配齐两项后实盘可用", opts2[1]["enabled"], True)
check("③ 可用时原因为空", opts2[1]["reason"], "")
check("③ 实盘项带期货公司名（让用户确认连的是哪家）",
      "创元期货" in opts2[1]["hint"], True)
opts3 = AppTrader.link_options(_cfg(market="创元期货", confirm=False))["options"]
check("③ 只缺资格开关 → 实盘仍不可用", opts3[1]["enabled"], False)
check("③ 原因区分得出是缺哪一项（指路 CONFIRM）",
      "CONFIRM_LIVE_TRADING" in opts3[1]["reason"], True)

# ═══ ④ 环境变量键名：必须是 Config 真消费的那三个 ═══
print("\n[4] 子进程 env 注入（键名写错会静默失效，故逐键钉死）")
env = AppTrader._link_env(
    AppTrader._resolve_link(_cfg(market="创元期货", confirm=True), LINK_LIVE))
check("④ 注入恰好三个键", sorted(env.keys()),
      ["TRADING_BROKER",
       "TRADING_BROKER_PARAMS__CONFIRM_LIVE_TRADING",
       "TRADING_BROKER_PARAMS__TQ_MARKET"])
# 一律 .get()：键名被改坏时是**红一条**，不是 KeyError 崩在头一条、
# 后面 20 项一条都看不到（门禁只显示一个 Traceback，等于失去定位力）。
check("④ broker 键名带 TRADING_ 前缀", env.get("TRADING_BROKER"), "live")
check("④ tq_market 走嵌套双下划线",
      env.get("TRADING_BROKER_PARAMS__TQ_MARKET"), "创元期货")
check("④ confirm 用小写 true（pydantic 布尔解析）",
      env.get("TRADING_BROKER_PARAMS__CONFIRM_LIVE_TRADING"), "true")
check("④ 选 SimNow 时 confirm 显式 false（不留上一轮的 true）",
      AppTrader._link_env(AppTrader._resolve_link(_cfg(), LINK_SIMNOW)).get(
          "TRADING_BROKER_PARAMS__CONFIRM_LIVE_TRADING"), "false")

# 真消费验证：把这三个键塞进 os.environ，TradingConfig 必须读到 ——
# 这是"注入真的生效"的唯一硬证据（静态断言证不了键名对错）。
_saved = {k: os.environ.get(k) for k in env}
try:
    os.environ.update(env)
    from Trading.Config import default_config
    cfg2 = default_config()
    check("④ Config 真读到 broker=live", cfg2.broker, "live")
    check("④ Config 真读到 tq_market=创元期货",
          cfg2.broker_params.tq_market, "创元期货")
    check("④ Config 真读到 confirm_live_trading=True",
          cfg2.broker_params.confirm_live_trading, True)
    # 与 SimNow.py:594 的判定式对齐：不实例化 broker（免连网），复算同一式子
    check("④ 判定式 is_live 成立（broker=live 或 tq_market≠simnow）",
          (cfg2.broker == "live"
           or str(cfg2.broker_params.tq_market).strip().lower() != "simnow"),
          True)
finally:
    for k, v in _saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v

# ═══ ⑤ 持久化（state.db kv 往返）═══
print("\n[5] 持久化：state.db kv 往返")
_tmp = tempfile.mkdtemp(prefix="linkchoice_")
try:
    check("⑤ 空目录读回 None（不臆造默认项）",
          AppTrader._read_link_choice(_tmp), None)
    check("⑤ 不存在的目录读回 None",
          AppTrader._read_link_choice(os.path.join(_tmp, "nope")), None)
    AppTrader._write_link_choice(_tmp, LINK_LIVE)
    check("⑤ 写入 live → 读回 live",
          AppTrader._read_link_choice(_tmp), "live")
    AppTrader._write_link_choice(_tmp, LINK_SIMNOW)
    check("⑤ 覆盖写 simnow → 读回 simnow",
          AppTrader._read_link_choice(_tmp), "simnow")
    check("⑤ 持久化键名（与 auto_order_enabled 同库）",
          LINK_STATE_KEY, "ao_link")
    # 脏值不认：库里被写成别的东西时宁可没有默认项，也不能带出一个非法链路
    from Trading.Infra.StateDB import Store
    s = Store(os.path.join(_tmp, "state.db"))
    s.set_json(LINK_STATE_KEY, "dry_run")
    s.close()
    check("⑤ 非法值读回 None（不把 dry_run 当默认项）",
          AppTrader._read_link_choice(_tmp), None)
finally:
    shutil.rmtree(_tmp, ignore_errors=True)

# ═══ ⑥ 前端静态（元素 / 函数 / 取消语义）═══
print("\n[6] 前端静态层")
check("⑥ 顶栏有登录方式徽标元素", 'id="auto-order-link"' in HTML, True)
check("⑥ 徽标排在账本之后（不破坏既有顺序契约）",
      HTML.index('id="auto-order-ledger-btn"') < HTML.index('id="auto-order-link"'),
      True)
check("⑥ choice 形态对话框已落地（_pushDialog 收 opts）",
      'function _pushDialog(msg, kind, opts)' in JS, True)
check("⑥ showChoice 出口已挂", 'function showChoice(' in JS, True)
check("⑥ 选项区构造函数就位", 'function _buildChoiceOpts(' in JS, True)
check("⑥ 选中值读取函数就位", 'function _choiceValue(' in JS, True)
check("⑥ 徽标渲染函数就位", 'function renderAutoOrderLink(' in JS, True)
check("⑥ 点框外 = 取消（choice 与 confirm 同义，不落到 alert 的确定分支）",
      "const outsideResult = !isConfirm && !isChoice;" in JS, True)
check("⑥ 禁用项保留可见并给原因（置灰而非隐藏）",
      all(k in JS for k in ["(on ? '' : ' disabled')",
                            'const sub = on ? String((o && o.hint) || "")'
                            ' : String((o && o.reason) || "");']), True)
check("⑥ 开关开启路径先选链路再发请求",
      "link = await pickTradeLink();" in JS, True)
check("⑥ 取消 → 不发请求且开关回弹",
      "if (!link) { checkbox.checked = false; return; }" in JS, True)
check("⑥ 启动请求体带上 link", "link: link            // 登录链路" in JS, True)
check("⑥ 默认项取上次选择（info.last）",
      "default: String((info && info.last) || '')" in JS, True)
check("⑥ 选项只认后端下发的 link_view（前端不复制判据）",
      "autoOrderLinkInfo = data.link_view" in JS, True)
# 文案契约（2026-09-28 用户拍板）：标题就三个字「登录方式」，不带操作说明行。
# 早先写的是「选择自动下单的登录方式\n（取消或点击对话框外区域＝不启动）」——
# 既啰嗦又把"怎么取消"当成免责声明印在框里。行为（Esc / 点框外 / 取消按钮）
# 由 choice 形态本身保证，不靠这行字，删掉不损失任何东西。
_choice_msg = re.search(r"await showChoice\(\s*'([^']*)'", JS)
_msg = _choice_msg.group(1) if _choice_msg else "\n"   # 找不到时故意给脏值，让下面两条都红
check("⑥ 选择框标题恰为「登录方式」", _msg, "登录方式")
check("⑥ 标题是单行（不掺操作说明行、不带括注）",
      (chr(10) in _msg) or ("\\n" in _msg) or ("（" in _msg), False)

# ═══ ⑦ 前端行为层：真函数跑在 node 上 ═══
print("\n[7] 前端行为层（node 真函数）")
node = shutil.which("node")
if not node:
    print("  [SKIP] node 不在位，只跑静态层")
else:
    m_opt = re.search(r"(        function _buildChoiceOpts\(opts\) \{[\s\S]*?\n        \})", JS)
    m_esc = re.search(r"(        function _escHtml\(s\) \{[\s\S]*?\n        \}\n"
                      r"        function _escAttr\(s\) \{[\s\S]*?\n        \})", JS)
    m_val = re.search(r"(        function _choiceValue\(overlay, result\) \{[\s\S]*?\n        \})", JS)
    m_rend = re.search(r"(        function renderAutoOrderLink\(view, running\) \{[\s\S]*?\n        \})", JS)
    check("⑦ 四个真函数源码均可抽取",
          all([m_opt, m_esc, m_val, m_rend]), True)
    if all([m_opt, m_esc, m_val, m_rend]):
        src = "\n".join(re.sub(r"^        ", "", m.group(1), flags=re.M)
                        for m in (m_esc, m_opt, m_val, m_rend))
        opts_case = [
            {"value": "simnow", "label": "SimNow", "enabled": True,
             "hint": "仿真环境", "reason": ""},
            {"value": "live", "label": "实盘", "enabled": False,
             "hint": "实盘 · X", "reason": "未配置期货公司"},
        ]
        script = (
            "const _els = {};\n"
            "global.document = { getElementById: function (id) { return _els[id] || null; } };\n"
            + src + "\n"
            "function mk() { const e = {textContent:'', className:'', title:'',"
            " style:{display:''}}; return e; }\n"
            "const out = {};\n"
            # 三组：默认上次选 live（不可用 → 必须退回 simnow）/ 上次选 simnow / 无上次
            "out.a = _buildChoiceOpts({options: " + json.dumps(opts_case)
            + ", default: 'live'});\n"
            "out.b = _buildChoiceOpts({options: " + json.dumps(opts_case)
            + ", default: 'simnow'});\n"
            "out.c = _buildChoiceOpts({options: " + json.dumps(opts_case)
            + ", default: ''});\n"
            "out.a_checked = (out.a.match(/value=\"([^\"]*)\" checked/g) || []);\n"
            "out.a_disabled = (out.a.match(/disabled/g) || []).length;\n"
            "out.a_has_reason = out.a.indexOf('未配置期货公司') >= 0;\n"
            "out.a_esc = out.a.indexOf('&lt;') < 0 && out.a.indexOf('<label') >= 0;\n"
            "out.val_none = _choiceValue({querySelector: function(){return null;}}, true);\n"
            "out.val_cancel = _choiceValue({querySelector: function(){"
            "return {value:'simnow'};}}, false);\n"
            "out.val_ok = _choiceValue({querySelector: function(){"
            "return {value:'live'};}}, true);\n"
            "let e1 = mk(); _els['auto-order-link'] = e1;\n"
            "renderAutoOrderLink({current:'live'}, true);\n"
            "out.live_txt = e1.textContent; out.live_cls = e1.className;"
            " out.live_disp = e1.style.display;\n"
            "let e2 = mk(); _els['auto-order-link'] = e2;\n"
            "renderAutoOrderLink({current:'simnow'}, true);\n"
            "out.sn_txt = e2.textContent; out.sn_cls = e2.className;\n"
            "let e3 = mk(); _els['auto-order-link'] = e3;\n"
            "renderAutoOrderLink({current:'live'}, false);\n"
            "out.off_disp = e3.style.display; out.off_txt = e3.textContent;\n"
            "let e4 = mk(); _els['auto-order-link'] = e4;\n"
            "renderAutoOrderLink(null, true);\n"
            "out.null_disp = e4.style.display;\n"
            "console.log(JSON.stringify(out));\n")
        tmp = os.path.join(TEST_DIR, "_link_choice_tmp.js")
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(script)
        try:
            r = subprocess.run([node, tmp], capture_output=True,
                               text=True, timeout=30)
            if r.returncode != 0:
                check("⑦ node 执行真函数", r.stderr.strip()[-160:], "")
            else:
                o = json.loads(r.stdout.strip())
                check("⑦ 上次选了实盘但实盘不可用 → 默认选中退回 SimNow",
                      o["a_checked"], ['value="simnow" checked'])
                check("⑦ 禁用项真的带 disabled（两处：类 + input）",
                      o["a_disabled"], 2)
                check("⑦ 禁用项显示原因（用户知道为什么点不了）",
                      o["a_has_reason"], True)
                check("⑦ 选项区按 HTML 拼装且已转义", o["a_esc"], True)
                check("⑦ 上次选 SimNow → 默认仍是 SimNow",
                      o["b"].count('value="simnow" checked'), 1)
                check("⑦ 无上次选择 → 落到第一个可用项",
                      o["c"].count('value="simnow" checked'), 1)
                check("⑦ 确定但无选中项 → null（不臆造）",
                      o["val_none"], None)
                check("⑦ 取消 → null（点框外/Esc 同义）",
                      o["val_cancel"], None)
                check("⑦ 确定 → 返回选中值", o["val_ok"], "live")
                check("⑦ 徽标：实盘运行中 → 文本「实盘」+ live 类",
                      [o["live_txt"], "live" in o["live_cls"]], ["实盘", True])
                check("⑦ 徽标：仿真运行中 → 文本「SimNow」+ simnow 类",
                      [o["sn_txt"], "simnow" in o["sn_cls"]], ["SimNow", True])
                check("⑦ 徽标：运行中才显示（实盘链路但进程未跑 → 隐藏且清空）",
                      [o["off_disp"], o["off_txt"]], ["none", ""])
                check("⑦ 徽标：无链路视图 → 隐藏（不给假标记）",
                      o["null_disp"], "none")
        finally:
            os.remove(tmp)

print("\n" + "=" * 60)
print("trade_link_choice: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
