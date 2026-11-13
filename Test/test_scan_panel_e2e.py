# -*- coding: utf-8 -*-
"""「保存到自选」真浏览器端到端（Edge headless + CDP + **真** app.html/app.js）

为什么单测不够
--------------
`Test/test_scan_panel_collapse.py` 把 app.js 里那段函数体原文抽出来，在 node 里
用桩 DOM + 假定时器跑——覆盖的是**状态机分支**。以下四类只有真浏览器能验：

  ① 真 4000ms 定时器：假时钟不会走到第 4000ms 那一帧，`slowTimer` 改的那句文案
     在桩里永远测不到（桩只断言"存在 clearTimeout"，不断言"到底改了没"）；
  ② `disabled` 元素在真浏览器里**根本不派发 click**：桩里 `btn.click()` 只是调
     函数，看不出"保存中被置 disabled ⇒ 取消点不动"这个真实后果；
  ③ `innerHTML` 里的 `&nbsp;` 在 `textContent` 里是 `\\u00a0`，渲染成三列的实际
     文本与源码字符串不是一回事；
  ④ `fetch` 返回 500 时 `r.json()` 仍然成功解析——这条**真 bug 就是本用例首次
     运行时抓到的**（旧代码不判 `r.ok`，500 被当成功渲染成「通达信： 已保存」）。

依赖与降级
----------
需要 Node ≥ 22（内置 fetch/WebSocket）与 Edge/Chrome（CDP）。缺任一项打印
`[SKIP]` 并 **exit 0**：门禁要能在没装浏览器的机器上跑通，而不是把环境缺失
判成代码缺陷。跳过原因会写进输出，不会静默。

  python Test/test_scan_panel_e2e.py            # 正常跑
  WB_SKIP_E2E=1 python Test/test_scan_panel_e2e.py   # 强制跳过（离线/慢机器）
"""
import json
import os
import shutil
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, "e2e"))
sys.path.insert(0, _REPO_ROOT)

EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/microsoft-edge",
    "/usr/bin/microsoft-edge-stable",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
]

DRIVE_TIMEOUT = 240


def _find_edge():
    for p in EDGE_CANDIDATES:
        if os.path.isfile(p):
            return p
    return None


def _skip(msg):
    print(f"[SKIP] 保存链路真浏览器 E2E：{msg}")
    print("[SKIP] 这不是通过，也不代表已覆盖——只是本机缺依赖。")
    sys.exit(0)


def main():
    if os.environ.get("WB_SKIP_E2E") == "1":
        _skip("WB_SKIP_E2E=1 显式跳过")

    node = shutil.which("node")
    if not node:
        _skip("找不到 node（需要 ≥22，内置 fetch/WebSocket）")
    try:
        ver = subprocess.run([node, "-e", "process.stdout.write(process.versions.node)"],
                             capture_output=True, text=True, timeout=20).stdout.strip()
        major = int(ver.split(".")[0])
    except Exception as e:                                    # noqa: BLE001
        _skip(f"node 版本探测失败：{e}")
    if major < 22:
        _skip(f"node {ver} 太旧（需要 ≥22，内置 WebSocket）")

    edge = _find_edge()
    if not edge:
        _skip("找不到 Edge/Chrome 可执行文件")

    drive = os.path.join(_HERE, "e2e", "drive_scan_save.mjs")
    if not os.path.isfile(drive):
        _skip("缺少驱动脚本 Test/e2e/drive_scan_save.mjs")

    # 起打桩后端（真静态站 + 脚本化 /api），再让浏览器打进来
    try:
        import stub_server
    except Exception as e:                                    # noqa: BLE001
        _skip(f"打桩后端导入失败：{e}")

    httpd, port = stub_server.serve()
    base = f"http://127.0.0.1:{port}"
    try:
        proc = subprocess.run(
            [node, drive, base, edge],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=DRIVE_TIMEOUT,
        )
    finally:
        httpd.shutdown()
        httpd.server_close()

    out = (proc.stdout or "").strip()
    if not out:
        print(f"[FAIL] 驱动无输出（exit={proc.returncode}）")
        if proc.stderr.strip():
            print(proc.stderr[:2000])
        return 1

    # 驱动只输出最后一行 JSON（前面可能有 vite/edge 的杂音）
    payload = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                payload = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if payload is None:
        print("[FAIL] 驱动输出不是合法 JSON")
        print(out[:2000])
        return 1

    if payload.get("fatal"):
        print(f"[FAIL] 驱动致命错误：{payload['fatal']}")
        if payload.get("stderr"):
            print(payload["stderr"][:2000])
        for c in payload.get("checks") or []:
            print(f"  {'PASS' if c['ok'] else 'FAIL'} {c['id']} {c['detail']}")
        return 1

    checks = payload.get("checks") or []
    failed = []
    for c in checks:
        print(f"[{'PASS' if c['ok'] else 'FAIL'}] {c['id']} {c['detail']}")
        if not c["ok"]:
            failed.append(c["id"])

    print()
    print("=" * 72)
    print(f"真浏览器 E2E：共 {len(checks)} 项，通过 {len(checks) - len(failed)}，失败 {len(failed)}")
    for fid in failed:
        print(f"  ✗ {fid}")
    print(f"浏览器：{os.path.basename(edge)}｜node {ver}｜真 app.html + 真 app.js + CDP")
    print("=" * 72)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
