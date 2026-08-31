# -*- coding: utf-8 -*-
"""
P3 变异门禁 —— 证明 5 个 G 级守卫「能抓回退」（绿但不红 = 无效守卫）。

对每条守卫注入一个**真实回归形态**（不是简单删锁——CPython GIL 下普通
字典/列表写是原子的，裸删锁常常暴露不出竞态），再跑对应守卫：守卫必须
变红（退出码非 0）。若注入后守卫仍全绿，说明守卫是「假绿」，需重写。

变异清单（与审计矩阵 P3 缺口一一对应）：
  g2-nocap      双窗键限额失效 → 双窗键数突破 MAX_DUAL_CACHE_KEYS
  g2-rmw        cache_update 退化为「取-改-写」三段（GIL 让出窗口）→ 字段丢失
  g10-noguard   读侧对象图锁被弃用 → 写侧重建期间读者读到撕裂态
  g10-nopoplock futures_cache_pop 不再回收对象图锁 → 登记表无界增长
  g6-nosnap     names_snapshot 退化为返回活字典（不拷贝）→ 静默串表/迭代中改大小
  g8-isolate    get_scan_session 忽略 token 回退 legacy → 凭 token 取到别人会话
  g3-notomic    safe_write_json_file 退化为直接写（非原子）→ 读者读到撕裂 JSON
  g3-filelock   跨进程 file_lock 被置空 → 多进程 RMW 丢失更新

用法：
  python Test/_mutate_p3.py            # 跑全部变异，汇总
  python Test/_mutate_p3.py g2-nocap   # 只跑单一变异
退出码：全部变异均成功使守卫变红 → 0；任一变异未使守卫变红 → 1
"""
import contextlib
import os
import subprocess
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, _HERE)

import _stub_env  # noqa: E402
_stub_env.install()

from App import AppData, AppScan  # noqa: E402


# ── 各变异对应的测试文件 ──────────────────────────────────────────────
_TEST_FOR = {
    "g2-nocap": "test_dual_cache_concurrency",
    "g2-rmw": "test_dual_cache_concurrency",
    "g10-noguard": "test_futures_subchan_concurrency",
    "g10-nopoplock": "test_futures_subchan_concurrency",
    "g6-nosnap": "test_search_refresh_interleave",
    "g8-isolate": "test_legacy_scan_session",
    "g3-notomic": "test_annotation_filelock",
    "g3-filelock": "test_annotation_filelock",
}


def _apply(mut):
    """在**当前进程**内注入变异（针对 g3-filelock 之外的变异）。"""
    AD = AppData.AppData
    AS = AppScan

    if mut == "g2-nocap":
        # 双窗限额失效：_evict_dual_overflow_locked 永远不淘汰
        AD._evict_dual_overflow_locked = lambda self: False

    elif mut == "g2-rmw":
        # cache_update 退化为「取-改-写」三段，且用**全新副本整体替换**，
        # 精确还原审计 P0-3 描述的真实回退：各线程取到同一 dict、各自补字段、
        # 先后 put 整体覆盖——后者吞掉前者。注意：原地 entry.update 不会丢
        # 字段（它们改的是同一个活对象），所以必须「复制→改→替换」才能暴露
        # 丢失更新。
        def buggy_cache_update(self, key, **fields):
            entry = dict(self._stocks_analysis_cache.get(key) or {})
            time.sleep(0.002)                 # 读-改-写之间让出 GIL，制造交错
            entry.update(fields)
            self._cache_put_locked(key, entry)  # 整体替换（后者覆盖前者）
            return entry
        AD.cache_update = buggy_cache_update

    elif mut == "g10-noguard":
        # 读侧对象图锁被弃用：futures_sub_chan_guarded 只持容器锁，不持对象图锁
        @contextlib.contextmanager
        def patched_guarded(self, symbol, sub_freq):
            with self._futures_cache_lock:
                yield self.get_futures_sub_chan(symbol, sub_freq)
        AD.futures_sub_chan_guarded = patched_guarded

    elif mut == "g10-nopoplock":
        # futures_cache_pop 不再回收对象图锁 → 登记表无界增长
        def patched_pop(self, key, default=None):
            with self._futures_cache_lock:
                return self._futures_analysis_cache.pop(key, default)
            # 注意：故意不 pop _futures_chan_locks
        AD.futures_cache_pop = patched_pop

    elif mut == "g6-nosnap":
        # names_snapshot 退化为返回活字典（不拷贝）→ 写者 clear+update 时读者串表
        def patched_snap(self):
            return self._names
        AD.names_snapshot = patched_snap

    elif mut == "g8-isolate":
        # get_scan_session 忽略 token，一律回退 legacy（旧客户端参数串号）
        def patched_get(scan_token=None):
            return AS._legacy_scan_session
        AS.get_scan_session = patched_get

    elif mut == "g3-notomic":
        # safe_write_json_file 退化为「直接覆盖写、非原子」：截断后分两段写，
        # 段间 flush + sleep 制造撕裂窗口，并发读者可观察到半截/截断 JSON。
        # 关键点：必须 patch **模块级**函数 AppData.safe_write_json_file ——
        # save_annotations 在模块内以全局名调用它，patch 类属性
        # (AppData.AppData.safe_write_json_file) 完全无效（这正是此前「假绿」
        # 的根因：补丁没生效，守卫自然一直绿）。
        import json
        def buggy_write(path, data, *, ensure_ascii=False, indent=None):
            s = json.dumps(data, ensure_ascii=ensure_ascii, indent=indent)
            half = max(1, len(s) // 2)
            with open(path, "w", encoding="utf-8") as f:
                f.write(s[:half])
                f.flush()
                try:
                    os.fsync(f.fileno())
                except OSError:
                    pass
                time.sleep(0.004)   # 撕裂窗口：并发读者大概率捕获半截 JSON
                f.write(s[half:])
        AppData.safe_write_json_file = buggy_write   # 模块级函数，非类属性

    # g3-filelock 由子进程 env 注入，无需在当前进程 patch


def _run_test_module(mod_name):
    import importlib
    mod = importlib.import_module(mod_name)
    return mod.main()


def run_one(mut):
    """每个变异在**独立子进程**中运行：注入变异 + 跑对应守卫。

    用子进程而非进程内 patch，彻底杜绝「变异 A 改了模块/类属性，污染后续
    变异 B」的串扰（尤其 g3-notomic 修改的是模块级函数）。"""
    test_mod = _TEST_FOR[mut]
    env = dict(os.environ)
    if mut == "g3-filelock":
        # 跨进程变异：以环境变量通知 RMW 子进程丢弃 file_lock
        env["G3_FILELOCK_MUTATE"] = "1"
    # 子进程内：装 stub → 注入变异 → 跑守卫 main()，退出码即守卫结果
    code = (
        "import sys;"
        "sys.path.insert(0, %r); sys.path.insert(0, %r);"
        "import _stub_env; _stub_env.install();"
        "import _mutate_p3;"
        "_mutate_p3._apply(%r);"
        "sys.exit(_mutate_p3._run_test_module(%r));"
    ) % (_REPO_ROOT, _HERE, mut, test_mod)
    proc = subprocess.run(
        [sys.executable, "-c", code],
        env=env, cwd=_REPO_ROOT,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    rc = proc.returncode
    # 守卫有效 ⟺ 注入变异后它变红（退出码非 0）
    caught = (rc != 0)
    return caught, rc


def main():
    wanted = sys.argv[1:]
    all_muts = list(_TEST_FOR.keys())
    muts = [m for m in all_muts if (not wanted or m in wanted)]
    print("=" * 64)
    print("P3 变异门禁：注入回归形态，验证守卫能抓回退")
    print("=" * 64)
    results = []
    for mut in muts:
        try:
            caught, rc = run_one(mut)
        except Exception as exc:  # noqa: BLE001
            caught, rc = False, -1
            print(f"  [异常] {mut}: {type(exc).__name__}: {exc}")
        results.append((mut, caught, rc))
        mark = "✅ 抓到回退" if caught else "❌ 守卫假绿"
        print(f"  {mark}  {mut:14s} (测试={_TEST_FOR[mut]}, 退出码={rc})")
    failed = [m for (m, c, _) in results if not c]
    print("-" * 64)
    print(f"变异 {len(results)} 项，成功使守卫变红 {len(results) - len(failed)} 项"
          + (f"，假绿 {len(failed)} 项：{failed}" if failed else "，全部有效"))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
