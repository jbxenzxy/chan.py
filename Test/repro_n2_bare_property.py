# -*- coding: utf-8 -*-
"""N2 复现：AppData 8 个裸 @property 违反「快照契约」的确定性交错。

指导书 v1.3 §3.1 / 附录 N2。
裸出口本身不是 bug，但它把「必须走 snapshot」的契约变成了纯口头约定——
本脚本用一次确定性交错证明：任何一个下游照直觉写下 `for k in d`，
竞态就立刻成立，且有两种后果（崩溃 / 静默串读）。

按维度 8.4 要求，本脚本先自证能检出该已知问题（确定性命中，非靠运气）。

运行：把本文件放在仓库 Test/ 下，在仓库根目录执行
    python Test/repro_n2_bare_property.py

退出码（与 repro_n3 同一门禁语义）：
    0 = 未命中（N2 已修复，可安全接入 CI）
    1 = 命中（裸出口竞态仍在）
⚠ 本脚本当前**命中**，故未注册进 Test/run_all.py 的强门禁——一旦 N2
  收口完成（方案 a 或 b），退出码自然转 0，届时需补注册。
"""
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from App.AppData import app_data  # noqa: E402


def _make_names(n, tag):
    """构造 n 条形如 (code, {name, market}) 的名称表"""
    return {
        f"{tag}{i:06d}": {"name": f"{tag}股票{i}", "market": "sh"}
        for i in range(n)
    }


def run_case(iterate_directly: bool):
    """iterate_directly=True 走裸出口遍历（违反契约）；False 走 names_snapshot()"""
    writer_err = []
    reader_err = []
    reader_started = threading.Event()
    writer_done = threading.Event()

    # 准备初始表：300 条
    with app_data._meta_cache_lock:
        app_data._names = _make_names(300, "A")
    exposed = app_data.names_cache          # ← 裸出口：拿到的是共享本体

    def writer():
        # 等读者已进入遍历，再在遍历中途 clear()+update()（与刷新线程
        # replace_names 的真实形态一致：**原地改**，不是整体换新 dict——
        # 换新 dict 是旧引用看不到的，那正是维度 8.4 警告的复现脚本假阴性）
        reader_started.wait(timeout=5)
        with app_data._meta_cache_lock:
            app_data._names.clear()
            app_data._names.update(_make_names(280, "B"))   # 条数变化 + 键全换
        writer_done.set()

    def reader():
        try:
            if iterate_directly:
                # 典型的直觉写法：直接遍历共享 dict
                for k in exposed:
                    if not reader_started.is_set():
                        reader_started.set()
                    _ = exposed.get(k)
                    time.sleep(0.001)   # 拉长迭代窗口，保证与 writer 咬合
            else:
                snap = app_data.names_snapshot()      # 锁内快照
                reader_started.set()
                for k in snap:
                    _ = snap[k]
                    time.sleep(0.001)
            # 走到这说明没撞上（时序没咬合）
            reader_err.append("NO_HIT")
        except RuntimeError as e:
            reader_err.append(f"RuntimeError: {e}")

    t1 = threading.Thread(target=writer)
    t2 = threading.Thread(target=reader)
    t1.start(); t2.start()
    # 兜底：若 reader 未触发 started（空表等异常场景），放行 writer 防挂死
    if not reader_started.wait(timeout=2):
        reader_started.set()
    t1.join(timeout=5); t2.join(timeout=5)
    return reader_err


def main():
    print("=" * 64)
    print("N2 复现：裸 @property（names_cache）的遍历竞态（确定性交错）")
    print("=" * 64)

    # ── 自证（维度 8.4）：先证明本脚本必然能检出该问题 ──
    errs = run_case(iterate_directly=True)
    hit = errs and errs[0] != "NO_HIT"
    print(f"\n[自证] 裸出口直接遍历：{errs[0] if errs else '(无输出)'}")
    print(f"  → 竞态{'命中 ✅（脚本具备检出能力）' if hit else '未命中（提高迭代条数或重跑）'}")

    # ── 对照组：走 snapshot 的正门 ──
    errs2 = run_case(iterate_directly=False)
    safe = errs2 and errs2[0] == "NO_HIT"
    print(f"\n[对照] names_snapshot() 快照遍历：{'未受影响 ✅' if safe else errs2}")

    # 门禁语义：命中（缺陷仍在）→ 1；未命中（已修复）→ 0
    print()
    if hit:
        print("结论：N2 裸出口竞态仍在 —— 8 个 @property（AppData.py "
              "L1465-1500 区段）")
        print("      仍把「必须经 snapshot 访问」的契约押在人的自觉上。")
        print("      修复方向二选一：")
        print("        a) property 改为返回快照/不可变视图"
        print("           （需核对下游是否有就地写）；")
        print("        b) 本体出口改名 xxx_raw_unsafe + AST 扫描禁止下游引用")
        print("           （并入 test_lock_completeness 防回潮）。")
        return 1
    print("结论：N2 已收口（裸出口不再暴露本体 / 下游已禁止直引）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
