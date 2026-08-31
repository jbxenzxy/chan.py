# -*- coding: utf-8 -*-
"""N3 回归：扫描会话（_ScanSession）超时回收 —— 弃扫泄漏 + 旧会话兜底串值。

指导书 v1.3 附录 N3。X3 修复（a334284）把扫描上下文下沉为按 scan_token
的会话对象，解决了跨页串参；但会话的注销只有一条路：前端调用
/api/stocks/scan/end。若页面在扫描中途被关闭/刷新（不会调 end），
该 _ScanSession 将永久驻留 _scan_sessions。

后果一（轻微）：内存泄漏——每会话一个对象 + skip_log 列表，量小但无界。
后果二（语义）：_legacy_scan_session 始终指向「最近一次 start」的会话，
旧客户端（不传 scan_token）的 end/skip 写入会落到这个可能早已被弃置的
会话上；弃扫页面的 skip_log 永远不会被打印（无 end）。

【N3 修复（本脚本配套）】new_scan_session 建会话前惰性清扫超时旧会话
（TTL 默认 24h，可经环境变量 CHAN_SCAN_SESSION_TTL_SEC 覆盖），并回收
_task_scan_token 死键。修复后本脚本所有场景应输出「未命中」。

自证方式：把弃扫会话的 start_time 拨回 25 小时前模拟"超时弃扫"，
不依赖真实等待（指导书维度 8.2 确定性交错思路）。

运行：把本文件放在仓库 Test/ 下，在仓库根目录执行
    python Test/repro_n3_scan_session_leak.py
退出码：0 = 未命中（已修复）；1 = 命中（缺陷仍在）。
"""
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from App import AppScan  # noqa: E402

_AGE = 25 * 3600  # 拨回 25 小时：超过默认 24h TTL


def _age(sess):
    """把会话伪装成 25 小时前创建（模拟超时弃扫，无需真实等待）"""
    sess.start_time = time.time() - _AGE


def main():
    print("=" * 64)
    print("N3 回归：弃扫会话泄漏与 _legacy_scan_session 兜底串值")
    print("=" * 64)

    AppScan._scan_sessions.clear()
    AppScan._legacy_scan_session = None
    AppScan._task_scan_token.clear()

    hit = False

    # ── 场景 1：页面 A 开扫描后中途关闭（不调 end）×3 次，随后正常开新一轮扫描 ──
    for i in range(3):
        token, sess = AppScan.new_scan_session(page_index_code=f"88049{i}")
        sess.skip_log.append(f"页面A第{i}轮的跳过记录")   # 收割线程已写入
        _age(sess)                                          # 页面随即关闭，end() 永远不会被调
    tok_fresh, _ = AppScan.new_scan_session(page_index_code="880491")  # 页面 B 正常新扫描
    leaked = len(AppScan._scan_sessions)
    ok1 = leaked == 1
    print(f"\n[场景1] 3 次弃扫 + 1 次正常扫描后 _scan_sessions 残留：{leaked}（期望 1）")
    print(f"  → {'❌ 命中：弃扫会话未回收' if not ok1 else '✅ 未命中：惰性清扫已回收'}")
    hit |= not ok1

    # ── 场景 1b：task 反查死键回收 ──
    AppScan.bind_task_scan_token("deadtask", "sc999999")   # 指向已不存在的会话
    AppScan.bind_task_scan_token("livetask", tok_fresh)
    with AppScan._scan_sessions_lock:
        AppScan._reap_stale_scan_sessions_locked()
    dead_left = AppScan.scan_token_for_task("deadtask") is not None
    live_kept = AppScan.scan_token_for_task("livetask") == tok_fresh
    ok1b = (not dead_left) and live_kept
    print(f"\n[场景1b] task 反查死键：deadtask 残留={dead_left}，livetask 保留={live_kept}")
    print(f"  → {'❌ 命中：死键未回收/活键误删' if not ok1b else '✅ 未命中：死键已回收且活键保留'}")
    hit |= not ok1b

    # ── 场景 2：旧客户端（不带 token）的写入落到哪个会话？ ──
    AppScan.append_scan_skip("旧客户端在 B 扫描期间发出的跳过记录")      # 未带 token
    sess_legacy = AppScan.get_scan_session(None)
    ok2 = sess_legacy is not None and sess_legacy.token == tok_fresh
    print(f"\n[场景2] 无 token 的 skip 落到会话：{sess_legacy.token if sess_legacy else None}"
          f"（B 的新会话是 {tok_fresh}）")
    print(f"  → {'✅ 未命中：正常落到当前活跃会话' if ok2 else '❌ 命中：兜底指向异常'}")
    hit |= not ok2

    # ── 场景 3：B 正常 end 后，弃扫会话不得残留 ──
    AppScan.drop_scan_session(tok_fresh)
    remain = len(AppScan._scan_sessions)
    ok3 = remain == 0
    print(f"\n[场景3] B 结算后：_scan_sessions 中残留 {remain} 个会话（期望 0）")
    print(f"  → {'❌ 命中：弃扫会话仍在' if not ok3 else '✅ 未命中：注册表干净'}")
    hit |= not ok3

    print()
    if hit:
        print("结论：N3 缺陷仍在（或回归引入），请检查 new_scan_session 惰性清扫逻辑。")
        return 1
    print("结论：N3 已修复（惰性超时回收生效），全部场景未命中。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
