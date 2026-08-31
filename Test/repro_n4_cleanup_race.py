# -*- coding: utf-8 -*-
"""N4 复现：/api/futures/cleanup 双页同切的「清扫跳过」窗口。

指导书 v1.3 附录 N4。X1 修复（a334284）给全局清扫加了守卫：
active_session_count() > 0 时跳过清扫。守卫在「有别的页面正在看期货」时
跳过是完全正确的；但存在一个自伤窗口——

时序：页面 A、B 同时把期货切回股票 → 两页各自 disconnectRealtime()
（SSE 生成器进入 finally 撕分会话，但尚未走完）→ 两页几乎同时
fire-and-forget POST /api/futures/cleanup → 两个请求各自看到对方的
「仍在册」会话 → 都跳过清扫 → 全部期货缓存成为孤儿残留。

后果：低危（残留的是内存缓存，下次访问自动重载；连接已由 finally 回收），
但与该端点「清理残留」的承诺不符，且残留量随切换次数累积。

运行：把本文件放在仓库 Test/ 下，在仓库根目录执行
    python Test/repro_n4_cleanup_race.py
"""
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))   # 取用 Test/ 同目录模块

import _stub_env                                          # noqa: E402
_stub_env.install()          # 缺三方依赖时兜底；有真库则零介入

from DataAPI import TqSdkCSSESource as Src          # noqa: E402
from App import AppSSE                               # noqa: E402


class _FakeSource:
    """最小桩：只要能进 _ACTIVE_SOURCES 并支持 close 即可"""
    def __init__(self, name):
        self.name = name
        self.closed = False
    def close(self):
        self.closed = True


def main():
    print("=" * 64)
    print("N4 复现：双页同切时 cleanup 守卫互相跳过 → 孤儿缓存")
    print("=" * 64)

    # ── 场景 1：A、B 两页的会话都在撕分中（仍在册）→ cleanup 全跳过 ──
    a = _FakeSource("pageA"); b = _FakeSource("pageB")
    with Src._ACTIVE_SOURCES_LOCK:
        Src._ACTIVE_SOURCES.add(a); Src._ACTIVE_SOURCES.add(b)

    r1 = AppSSE.futures_cleanup()
    r2 = AppSSE.futures_cleanup()      # 模拟两页几乎同时发 cleanup
    print(f"\n[场景1] cleanup 结果：{r1} / {r2}")
    both_skipped = (not r1["swept"]) and (not r2["swept"])
    print(f"  → {'✅ 命中：两次均跳过，期货 K线/分析缓存残留为孤儿' if both_skipped else '未命中'}")

    # ── 场景 2：两页 finally 走完（会话注销）后，第三次 cleanup 才真正清 ──
    with Src._ACTIVE_SOURCES_LOCK:
        Src._ACTIVE_SOURCES.discard(a); Src._ACTIVE_SOURCES.discard(b)
    time.sleep(0.05)
    r3 = AppSSE.futures_cleanup()
    print(f"\n[场景2] 会话全部注销后的第三次 cleanup：{r3}")
    print(f"  → {'✅ 此时清扫才执行（孤儿残留已由本 清扫 兜底回收）' if r3['swept'] else '未命中'}")

    print("\n结论：守卫的判据「active>0 即跳过」把『正在撕分的自己人』也算成了")
    print("『别的页面在看』。这不是每次必错，而是依赖 teardown 与 cleanup 的")
    print("到达时序——单页偶发、双页同切大概率。修法建议：")
    print("  · cleanup 端点改为「等待在册会话归零（带 0.2s 超时）后再清扫」；或")
    print("  · 更彻底：缓存条目挂 owner 会话 id，只清 owner 已销毁的条目")
    print("    （把『全局清扫』收窄成『孤儿回收』，与指导书 §0.4 一致）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
