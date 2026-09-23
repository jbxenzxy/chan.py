# -*- coding: utf-8 -*-
"""
P64 自动下单后台系统通知（页面不在前台 → Win11 右下角提醒）
==========================================================
背景（2026-09-23 用户三问）：
  ⑴ 页面在最前端时，自动下单弹窗看得见 —— 成立（severe→模态框不 ack 不清，
     warn/toast→页内 toast）。
  ⑵ 页面切到后台（用户去干别的）就看不见 —— 成立：弹窗/toast 都是页面内
     DOM；且 toast 5 秒自动消失、toasts 水位不回放历史，**错过即丢失**。
  ⑶ 解法：浏览器 Notification API —— 「页面在不在前台」只有页面自己知道，
     这是"只在后台时才提醒"的判定来源（App 进程侧的 winotify 分不清）。

本测试钉死三件事：
  [A] 组件与纪律：aoNotifyEligible / aoSysNotify / requestAoNotifyPermission
      三个函数存在，且四条纪律逐一落实 —— 只认授权态、后台判据
      （hidden || !focused）、点击通知回前台、new Notification 必须 try/catch；
  [B] 三通道接线：warn 循环 / severe 分支 / 成交 toast 循环都调 aoSysNotify，
      且权限请求挂在「开启自动下单」的用户手势上（await 之前）；
  [C] 行为矩阵：aoNotifyEligible 抽到 node 跑真函数 —— 7 种
      (supported, permission, hidden, focused) 组合逐项比对，
      前台 + 已授权必须 False（防"前台也弹系统通知"的噪音回归）。

判别力（护栏不恒真）：
  删掉 `hidden || !focused` 判据         → [1e] / [3-*]（前台组合全变 True）
  改成 permission 宽放（default 也发）   → [1d] / [3-default/denied 组]
  三通道任一接线被拆                     → [2a] / [2b] / [2c]
  requestAoNotifyPermission 挪到 await 后 → [2d]（手势上下文已丢，授权弹窗不出）

资源版本号不在本文件断言 —— 由 Test/test_aol_ledger_display.py ⑤ 组独占守卫
（同 p62 [8] 的分工约定：两处各写一份只增加抬版本号的同步成本）。

跑法：python Trading/Test/test_p64_ao_bg_notify.py
"""
from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_TG_ROOT = os.environ.get("TRADER_GATEWAY_HOME", "") or os.path.dirname(_HERE)
_REPO = os.path.dirname(_TG_ROOT)

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


_JS_P = os.path.join(_REPO, "Frontend", "app.js")
if not os.path.isfile(_JS_P):
    print("✗ 找不到 Frontend/app.js")
    sys.exit(2)
JS = io.open(_JS_P, encoding="utf-8").read()


def _fn_src(name):
    """按 8 空格缩进抽取一个顶层（IIFE 内）函数源码；抓不到返回 ''。"""
    m = re.search(r"(        function {}\([\w, ]*\) \{{[\s\S]*?\n        \}})"
                  .format(name), JS)
    return m.group(1) if m else ""


# ════════════════════════════════════════════════════════════════
# [1] 组件与四条纪律
# ════════════════════════════════════════════════════════════════
print("\n[1] 组件定义与纪律")
_elig = _fn_src("aoNotifyEligible")
_sys = _fn_src("aoSysNotify")
_req = _fn_src("requestAoNotifyPermission")
check("[1a] aoNotifyEligible 定义存在（且可整函数抽取）", bool(_elig), True)
check("[1b] aoSysNotify 定义存在", bool(_sys), True)
check("[1c] requestAoNotifyPermission 定义存在", bool(_req), True)
check("[1d] 只认授权态（default/denied 一律不发）",
      "permission !== 'granted') return false;" in _elig, True)
check("[1e] 后台判据 = hidden || !focused（缺一不可）",
      "return hidden || !focused;" in _elig, True)
check("[1f] 点击通知把页面拉回前台",
      "n.onclick = function () { window.focus(); n.close(); };" in _sys, True)
check("[1g] new Notification 在 try/catch 里（API 异常不炸轮询回调）",
      "try {" in _sys and "catch (e)" in _sys, True)
check("[1h] 权限只在 default 时请求（已授权/已拒绝不再打扰）",
      "Notification.permission !== 'default') return;" in _req, True)
check("[1i] 拒绝授权时给页内提示（后台将收不到提醒）",
      "浏览器未授权系统通知" in _req, True)

# ════════════════════════════════════════════════════════════════
# [2] 三通道接线 + 开关手势
# ════════════════════════════════════════════════════════════════
print("\n[2] 三通道接线")
check("[2a] warn 告警 → 系统通知", "aoSysNotify('自动下单提醒', warn[i].msg, 'ao-warn');" in JS, True)
check("[2b] severe 告警 → 系统通知", "aoSysNotify('自动下单：需人工介入', severeMsg, 'ao-alert');" in JS, True)
check("[2c] 成交 toast → 系统通知", "aoSysNotify('自动下单', fresh[i], 'ao-toast');" in JS, True)
check("[2d] 权限请求挂在开关手势（await 之前）",
      "if (on) requestAoNotifyPermission();" in JS, True)
check("[2e] severe 弹窗与通知共用同一份正文（severeMsg）",
      "showAlert('需人工介入！\\n\\n' + severeMsg)" in JS, True)
check("[2f] aoSysNotify 全文件只定义一次（防复制出第二份判据）",
      JS.count("function aoSysNotify("), 1)

# ════════════════════════════════════════════════════════════════
# [3] 行为矩阵：aoNotifyEligible 抽到 node 跑真函数
# ════════════════════════════════════════════════════════════════
print("\n[3] 行为矩阵（node 真函数）")
_node = shutil.which("node")
if not _elig:
    print("  [SKIP] 判据函数抓取失败")
elif not _node:
    print("  [SKIP] node 不在位，只跑静态层")
else:
    _fn_js = re.sub(r"^        ", "", _elig, flags=re.M)
    _probe = (
        _fn_js
        + "\nconst M = [\n"
        + "\n".join(
            "  [{}, '{}', {}, {}, {}],".format(
                "true" if s else "false", p,
                "true" if h else "false", "true" if f else "false",
                "true" if w else "false")
            for s, p, h, f, w in [
                (True, "granted", True, True, True),    # 切了标签页/最小化
                (True, "granted", False, False, True),  # 被别的程序盖住
                (True, "granted", True, False, True),
                (True, "granted", False, True, False),  # 前台：不提醒
                (True, "default", False, False, False),  # 没授权
                (True, "denied", True, True, False),     # 已拒绝
                (False, "granted", True, True, False),   # 无 API（非安全上下文）
            ])
        + "\n];\nconst bad = M.filter(r => aoNotifyEligible(r[0], r[1], r[2], r[3]) !== r[4]);\n"
        + "console.log(JSON.stringify({bad: bad.length, total: M.length}));\n"
    )
    _tmp = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        ".tmp_p64_probe.js")
    try:
        io.open(_tmp, "w", encoding="utf-8", newline="\n").write(_probe)
        _r = subprocess.run([_node, _tmp], capture_output=True, text=True,
                            timeout=30)
        _out = (_r.stdout or "").strip()
        m = re.search(r'\{"bad":\s*(\d+),\s*"total":\s*(\d+)\}', _out)
        check("[3a] node 探针正常退出", _r.returncode, 0)
        check("[3b] 7 种组合全部命中期望", (m.groups() if m else None),
              ("0", "7"))
    finally:
        if os.path.isfile(_tmp):
            os.remove(_tmp)

print("\n" + "=" * 60)
print("p64_ao_bg_notify: {} passed, {} failed".format(_PASS, _FAIL))
print("=" * 60)
sys.exit(1 if _FAIL else 0)
