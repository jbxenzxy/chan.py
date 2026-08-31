# -*- coding: utf-8 -*-
"""
G6 · 搜索 × 刷新流 —— 并发守护（对应审计矩阵 G6，P3）

矩阵上「搜索」只标注了「读 path 在 _meta_cache_lock 下」✅，但搜索遍历
（search_stocks 经 names_snapshot 遍历）与刷新换表（replace_names 的
clear()+update()）的**交织**从未被测过。审计 P1-2 已指出风险：遍历共享
别名会在换表时抛「dictionary changed size during iteration」或**静默串表**。
本用例直达这条交织路径：

  AppChart.search_stocks          (遍历经 app_data.names_snapshot())
  AppData.names_snapshot          (锁内浅拷贝，遍历专用)
  AppData.replace_names           (刷新整体换表，持 _meta_cache_lock)

守护目标：
  ① 并发搜索 × 并发换表：读者永不抛异常（绝不「字典在迭代中改变大小」），
     搜索结果结构自洽
  ② names_snapshot 返回的是**独立副本**：读者既不被换表污染、也不反向污染
     内部 _names；并发下每个快照都是某一时刻的完整表（= 写者某次全量），
     绝不半截/混合
  ③ 无「静默串表」：并发换表期间读者拿到的表恒等于某写者全量或空（初态），
     绝不旧表残条 + 新表新条混在一起

退出码语义：发现缺陷 → 非 0；全绿 → 0（可直接接入 CI）。
"""
import os
import sys
import tempfile
import threading
import time
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)

import _stub_env  # noqa: E402
_stub_env.install()

from App import AppData, AppChart  # noqa: E402
from App.AppData import app_data  # noqa: E402

_results = []


def rec(tag, desc, ok, detail=""):
    _results.append((tag, ok))
    mark = "✅ PASS" if ok else "❌ FAIL"
    print(f"  {mark}  {tag}  {desc}")
    if detail:
        print(f"           └─ {detail}")


class _CfgProxy:
    """只读代理：覆盖 stock_names_cache_file，其余透传真配置。
    把名称缓存文件重定向到临时路径，避免触碰真实数据；退出随临时目录回收。"""
    def __init__(self, real, **over):
        object.__setattr__(self, "_real", real)
        object.__setattr__(self, "_over", over)

    def __getattr__(self, name):
        over = object.__getattribute__(self, "_over")
        if name in over:
            return over[name]
        return getattr(object.__getattribute__(self, "_real"), name)


def _table_a():
    return {f"sh{i:06d}": {"name": f"A股{i}", "pinyin": f"ag{i}", "market": "sh"}
            for i in range(200)}


def _table_b():
    return {f"sz{i:06d}": {"name": f"B股{i}", "pinyin": f"bg{i}", "market": "sz"}
            for i in range(200)}


def _setup(tmp_file):
    app_data._names = _table_a()
    app_data._names_loaded = True          # 跳过从文件加载，直接用夹具
    AD_proxy = _CfgProxy(AppData.app_config,
                         stock_names_cache_file=tmp_file)
    AppData.app_config = AD_proxy
    with open(tmp_file, "w", encoding="utf-8") as f:
        f.write("{}")                       # 让 search_stocks 的 exists 检查通过


def _restore(real_cfg):
    AppData.app_config = real_cfg


def test_search_x_refresh_no_iteration_error():
    """① 并发搜索 × 并发换表：读者永不抛「字典在迭代中改变大小」等异常。"""
    tmp = tempfile.mkdtemp(prefix="g6_names_")
    tmp_file = os.path.join(tmp, "stock_names.json")
    real_cfg = AppData.app_config
    _setup(tmp_file)
    try:
        reader_errs = []
        writer_errs = []
        stop = threading.Event()

        def reader(q):
            while not stop.is_set():
                try:
                    res = AppChart.search_stocks(q)
                    if not isinstance(res, dict):
                        reader_errs.append(f"非 dict 结果: {type(res)}")
                except Exception as exc:   # noqa: BLE001
                    reader_errs.append(f"{type(exc).__name__}: {exc}")

        def writer():
            t = 0
            while not stop.is_set():
                try:
                    AppData.app_data.replace_names(_table_a() if t % 2 == 0
                                                  else _table_b())
                    t += 1
                except Exception as exc:    # noqa: BLE001
                    writer_errs.append(f"{type(exc).__name__}: {exc}")

        rs = [threading.Thread(target=reader, args=(f"A{i}",))
              for i in range(3)]
        rs += [threading.Thread(target=reader, args=(f"B{i}",))
               for i in range(2)]
        ws = [threading.Thread(target=writer) for _ in range(3)]
        for t in rs + ws:
            t.start()
        time.sleep(1.2)
        stop.set()
        for t in rs + ws:
            t.join(10)

        ok = (not reader_errs) and (not writer_errs)
        rec("①", "并发搜索×换表：读者无异常（无迭代中改大小）、写者无异常",
            ok,
            f"reader_errs={len(reader_errs)} writer_errs={len(writer_errs)} "
            f"samples={reader_errs[:2] or writer_errs[:2]}" if not ok else "")
    finally:
        _restore(real_cfg)


def test_snapshot_isolation_and_consistency():
    """②+③ names_snapshot 隔离 + 并发下无静默串表。"""
    tmp = tempfile.mkdtemp(prefix="g6_names_")
    tmp_file = os.path.join(tmp, "stock_names.json")
    real_cfg = AppData.app_config
    _setup(tmp_file)
    try:
        # 隔离性：快照副本的就地修改不得回写到内部 _names
        snap = app_data.names_snapshot()
        snap["__poison__"] = {"name": "x", "pinyin": "x", "market": "x"}
        iso_ok = "__poison__" not in app_data._names
        rec("②", "names_snapshot 返回独立副本：读者修改不回写内部 _names",
            iso_ok, f"iso_ok={iso_ok}")

        # 并发一致性：换表 A/B 与读者抢快照；每个快照必须 == A 或 == B 或空
        A, B = _table_a(), _table_b()
        consistent_errs = []
        stop = threading.Event()

        def reader():
            while not stop.is_set():
                try:
                    s = app_data.names_snapshot()
                    if len(s) == 0:
                        continue
                    if s != A and s != B:
                        consistent_errs.append(f"mixed len={len(s)}")
                except Exception as exc:   # noqa: BLE001
                    consistent_errs.append(f"{type(exc).__name__}: {exc}")

        def writer():
            t = 0
            while not stop.is_set():
                try:
                    AppData.app_data.replace_names(A if t % 2 == 0 else B)
                    t += 1
                except Exception:           # noqa: BLE001
                    pass

        rs = [threading.Thread(target=reader) for _ in range(3)]
        ws = [threading.Thread(target=writer) for _ in range(3)]
        for t in rs + ws:
            t.start()
        time.sleep(1.2)
        stop.set()
        for t in rs + ws:
            t.join(10)

        ok = (not consistent_errs)
        rec("③", "并发换表期间快照无静默串表（恒等于某写者全量或空）",
            ok,
            f"mixed/err={len(consistent_errs)} samples={consistent_errs[:3]}"
            if not ok else "")
    finally:
        _restore(real_cfg)


def main():
    print("=" * 68)
    print("G6 · 搜索 × 刷新流 并发守护")
    print("=" * 68)
    try:
        test_search_x_refresh_no_iteration_error()
        test_snapshot_isolation_and_consistency()
    except Exception:
        traceback.print_exc()
        return 2
    failed = [t for (t, ok) in _results if not ok]
    print("-" * 68)
    print(f"合计 {len(_results)} 项，{'全部通过' if not failed else str(len(failed)) + ' 项失败'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
