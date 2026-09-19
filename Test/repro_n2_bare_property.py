# -*- coding: utf-8 -*-
"""N2 复现：AppData 裸 @property 违反「快照契约」的确定性交错。

指导书「快照契约」/ 附录 N2。

【N2 已收口 —— 方案 b】8 个可变容器出口一律改名 `*_raw_unsafe`（命名自带
告警），并由 `Test/test_lock_completeness.py` §⑥ **默认拒绝**任何未登记的
引用（新增引用 = 立即失败）。故本脚本的职责从「报告缺陷」转为**钉死收口**：

  ① 自证（故意走裸出口）：证明「裸出口 + 遍历」的竞态是真实存在的
     —— 这是**为什么**要给它加 `_raw_unsafe` 后缀、为什么要默认拒绝的实证；
     命中是**预期**的，不是失败。
  ② 对照（走 names_snapshot()）：证明正门不受影响。
  ③ 收口断言：旧名 `app_data.names_cache` 必须已消失，新名必须存在。

为什么不做方案 a（property 返回快照）——已由实验否掉，见
`Test/test_lock_completeness.py` 的 PROP_CONTRACT 段：「把某个 cache 出口改成
`return dict(self._pe)` 后，本检查反而"通过"了」（自动推导判据失效）；
且下游 `_stock_names_cache` 别名靠 `replace_names` 的「同对象 clear()+update()」
拿零漂移，返回快照会让别名永远读到旧表——**静默失效，不报错**。

运行：把本文件放在仓库 Test/ 下，在仓库根目录执行
    python Test/repro_n2_bare_property.py

退出码（与 repro_n3 同一门禁语义）：
    0 = N2 收口成立（旧名消失 + 快照对照安全 + 竞态自证仍可检出）
    1 = 收口被破坏（旧名复活 / 快照对照失效 / 自证失效）
"""
import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from App.AppData import app_data  # noqa: E402

# N2 收口后的出口名（自证用；本引用已在
# Test/test_lock_completeness.py :: RAW_EXIT_ALLOWLIST 登记）
RAW_EXIT = "names_cache_raw_unsafe"
OLD_EXIT = "names_cache"


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
    exposed = getattr(app_data, RAW_EXIT)   # ← 裸出口：拿到的是共享本体

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
    print("N2 复现：裸 @property 的遍历竞态 + 收口断言（出口已改名）")
    print("=" * 64)

    # ── 自证（维度 8.4）：先证明本脚本必然能检出该问题 ──
    errs = run_case(iterate_directly=True)
    hit = errs and errs[0] != "NO_HIT"
    print(f"\n[自证] 裸出口直接遍历：{errs[0] if errs else '(无输出)'}")
    print("  → 竞态" + ("命中 ✅（裸出口确实危险，故须 *_raw_unsafe + 默认拒绝）"
                        if hit else "未命中（提高迭代条数或重跑）"))

    # ── 对照组：走 snapshot 的正门 ──
    errs2 = run_case(iterate_directly=False)
    safe = errs2 and errs2[0] == "NO_HIT"
    print(f"\n[对照] names_snapshot() 快照遍历：{'未受影响 ✅' if safe else errs2}")

    # ── 收口断言：旧名必须消失、新名必须在位 ──
    old_gone = not hasattr(app_data, OLD_EXIT)
    new_ok = hasattr(app_data, RAW_EXIT)
    print(f"\n[收口] 旧名 app_data.{OLD_EXIT} 已消失："
          f"{'✅' if old_gone else '❌（仍在——收口被回退）'}")
    print(f"[收口] 新名 app_data.{RAW_EXIT} 在位："
          f"{'✅' if new_ok else '❌（缺失）'}")

    # ── 门禁语义：自证命中 + 对照安全 + 旧名消失 = 收口成立 ──
    print()
    if hit and safe and old_gone and new_ok:
        print("结论：N2 已收口 —— 8 个可变容器出口一律 `*_raw_unsafe`，契约由")
        print("      Test/test_lock_completeness.py §⑥ 默认拒绝扫描兜底；")
        print("      遍历的正门仍是 names_snapshot() / pe_snapshot() / "
              "belong_snapshot()。")
        return 0
    print("结论：N2 收口被破坏 ——")
    if not hit:
        print("      · 自证未命中：本脚本已失去检出能力（迭代条数/时序需复核）")
    if not safe:
        print("      · 对照失效：names_snapshot() 被并发写影响（快照契约破了）")
    if not old_gone:
        print(f"      · 旧裸出口 app_data.{OLD_EXIT} 复活")
    if not new_ok:
        print(f"      · 收口后的出口 app_data.{RAW_EXIT} 缺失")
    return 1


if __name__ == "__main__":
    sys.exit(main())
