# -*- coding: utf-8 -*-
"""
阶段 4：数据层收敛 —— 成果防护守护用例
=====================================================================
守护阶段 4 的八类结构性成果（设计文档 V10 方案 8.7 / 4.4 / 6.3）：

  ① 兼容壳纯委托：AppEngine 12 个 DATA 族函数体为纯委托
     （AST 校验：docstring + 局部 import + 单条 return app_data.* 调用），
     实现单源于 App/AppData.py；委托目标在 app_data 实例上真实存在
  ② 状态别名同对象：7 组模块级状态别名与 app_data 实例字段
     共享同一对象（identity 校验，防「悄悄改成拷贝」导致双态漂移）
  ③ 配置别名清零：12 个路径/容量别名不得在 AppEngine 复活
  ④ 自选股收敛：DataAPI/TdxAPI.py 不再有 read_zxg_stocks /
     save_to_zxg_blk；ths_sync_to_tdx 与 AppEngine 改从 AppData 取
  ⑤ 分层方向：FrontAPI → AppOrch → AppData → AppConfig 严格单向
     （import 静态分析；FrontAPI 不得直连 AppData / AppEngine 数据状态）
  ⑥ 启动行为零漂移 + LRU 语义：app_data 实例化即完成选点/标注加载；
     cache_put 超 50 条淘汰最旧（LRU）；cache_get 命中移尾
  ⑦ 语义化子窗接口（吸收外部评审）：set/get/pop_futures_sub_chan
     把 "{SYMBOL}:{sub_freq}" key 规则内聚于数据层，symbol 大小写
     不敏感；FrontAPI 不再手工拼 key（源码级扫描）
  ⑧ 数据层反向依赖禁令（吸收外部评审教训）：App/AppData.py 在
     **任何层级**（模块级或函数内）都不得 import AppEngine /
     DataAPI / FrontAPI / AppOrch —— 防「影子数据层」模式回归
     （即引擎保留实现、数据层复制一份反向引用的双源结构）
  ⑨ 引擎引用有效性（迁移遗漏防护）：AppOrch 中全部 _m.<attr> 引用
     必须在 AppEngine 中可解析（AST 静态分析，含 try/except 条件
     导入块）—— 防阶段 4 迁移遗漏导致运行时 AttributeError
     （实测：_m.read_zxg_stocks / _m._float_mc_loaded /
     _m._STOCK_NAMES_CACHE_FILE）

运行：python Test/test_phase4_guards.py          # 校验（run_all 组件 11）
      python Test/test_phase4_guards.py --update  # 保留参数（本守护无冻结基线，等价校验）
"""
import argparse
import ast
import io
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    try:
        import typing_extensions
        typing.Self = typing_extensions.Self
    except ImportError:
        pass


def read_src(rel):
    with io.open(os.path.join(REPO_ROOT, rel), encoding="utf-8") as f:
        return f.read()


# ═══════════════════════════════════════════════════════════════════════
# ① 兼容壳纯委托（AST）
# ═══════════════════════════════════════════════════════════════════════
# 12 个 DATA 族函数：AppEngine 同名实现必须退化为兼容壳。
# 允许的函数体形态：docstring + （from App.AppData import ...）+ 单条 return
# （阶段 8：_get_stock_name 已下沉 App/utils.py，不再属于 AppEngine 壳面；
#  阶段 8 瘦身：选点/标注/上次代码等 13 个兼容壳已随功能域迁移删除，
#  仅保留引擎内部仍消费的 _save_point_time）
SHELL_FUNCS = [
    # 名称 / PE / 市值
    "_load_stock_names_from_cache_file", "_safe_write_json_file",
    "_load_pe_ttm_cache", "_get_pe_ttm", "_get_index_belong",
    "_load_float_mc_cache", "_update_float_mc_cache", "_get_float_mc_from_cache",
    # 统一缓存三件套
    "_cache_put", "_cache_get", "_cache_remove",
    # 选点持久化（引擎内部手动选点仍调用）
    "_save_point_time",
]

# 委托目标允许的受调者（app_data 属性 / AppData 模块级函数）
_ALLOWED_CALLEES = {"app_data"}


def _is_shell(fn_node):
    """判定函数体是否为纯委托壳：仅 docstring / import / 单条 return Call"""
    body = list(fn_node.body)
    # 剥离 docstring
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    # 允许前置局部 import（from App.AppData import ...）
    while body and isinstance(body[0], (ast.Import, ast.ImportFrom)):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return False, "函数体非「单条 return」"
    ret = body[0].value
    if not isinstance(ret, ast.Call):
        return False, "return 非 Call"
    func = ret.func
    # 形态一：app_data.xxx(...)
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) \
            and func.value.id in _ALLOWED_CALLEES:
        return True, ""
    # 形态二：from App.AppData import safe_write_json_file / get_annotation_key 后直调
    if isinstance(func, ast.Name):
        return True, ""
    return False, "return 调用目标非 app_data.* / AppData 模块函数"


def _shell_callee(fn_node):
    """提取纯委托壳的调用目标名（app_data.<attr> 或模块级函数名）"""
    body = list(fn_node.body)
    if (body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant)
            and isinstance(body[0].value.value, str)):
        body = body[1:]
    while body and isinstance(body[0], (ast.Import, ast.ImportFrom)):
        body = body[1:]
    if len(body) != 1 or not isinstance(body[0], ast.Return):
        return None
    ret = body[0].value
    if not isinstance(ret, ast.Call):
        return None
    func = ret.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return None


def test_shell_purity(failures):
    src = read_src(os.path.join("App", "AppEngine.py"))
    tree = ast.parse(src)
    funcs = {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}
    bad = []
    callees = []  # (壳名, 委托目标)
    for name in SHELL_FUNCS:
        node = funcs.get(name)
        if node is None:
            bad.append(f"{name} 不存在于 AppEngine（兼容壳被删）")
            continue
        ok, why = _is_shell(node)
        if not ok:
            bad.append(f"{name} 非纯委托壳：{why}")
            continue
        callee = _shell_callee(node)
        if callee:
            callees.append((name, callee))
    # 委托目标存在性：壳指向的 app_data.<attr> / AppData 模块函数必须真实存在
    # （吸收外部评审 inspect.getsource 手法：防「壳委托到不存在的目标」薄壳假绿）
    from App.AppData import app_data as _ad
    from App import AppData as _adm
    for shell_name, callee in callees:
        if not (hasattr(_ad, callee) or hasattr(_adm, callee)):
            bad.append(f"{shell_name} 委托目标 app_data.{callee} 不存在（薄壳假绿）")
    if bad:
        failures.extend(f"兼容壳纯委托: {b}" for b in bad)
        for b in bad[:6]:
            print(f"[FAIL] ① 兼容壳: {b}")
        if len(bad) > 6:
            print(f"        …共 {len(bad)} 项")
    else:
        print(f"[PASS] ① 兼容壳: {len(SHELL_FUNCS)} 个 DATA 族函数全部为纯委托壳"
              f"（实现单源 App/AppData.py；{len(callees)} 个委托目标真实存在）")


# ═══════════════════════════════════════════════════════════════════════
# ② 状态别名同对象（运行时 identity）
# ═══════════════════════════════════════════════════════════════════════
ALIAS_PAIRS = [
    # (AppEngine 属性, app_data 内部字段属性)
    ("_stocks_analysis_cache", "stocks_analysis_cache"),
    ("_cache_lock",            "cache_lock"),
    ("_futures_analysis_cache", "futures_analysis_cache"),
    ("_stock_names_cache",     "names_cache"),
    ("_pe_ttm_cache",          "pe_cache"),
    ("_index_belong_cache",    "belong_cache"),
    ("_saved_point_times",     "saved_point_times"),
]


def test_alias_identity(failures):
    from App import AppEngine as m
    from App.AppData import app_data
    bad = []
    for m_name, d_prop in ALIAS_PAIRS:
        m_val = getattr(m, m_name, None)
        d_val = getattr(app_data, d_prop, None)
        if m_val is None:
            bad.append(f"AppEngine.{m_name} 缺失")
        elif d_val is None:
            bad.append(f"app_data.{d_prop} 缺失")
        elif m_val is not d_val:
            bad.append(f"{m_name} 与 app_data.{d_prop} 非同一对象（双态漂移）")
    if bad:
        failures.extend(f"状态别名: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ② 状态别名: {b}")
    else:
        print(f"[PASS] ② 状态别名: {len(ALIAS_PAIRS)} 组别名与 app_data 字段共享同一对象")


# ═══════════════════════════════════════════════════════════════════════
# ②b 状态别名零绕行（P0-2 守护：服务层不得再绕行 AppEngine 状态别名）
# ═══════════════════════════════════════════════════════════════════════
# P0-2 把 AppChart/AppScan/BSPointList 对 _m._cache_get/_saved_point_times/
# _stocks_analysis_cache/_futures_analysis_cache/_cache_remove 等的绕行调用
# 全部改为 app_data.* 公共 API。本守护封禁该模式复活（防止回归到双写路径）。
_BYPASS_PATTERNS = [
    "_m._cache_get", "_m._cache_put", "_m._cache_remove",
    "_m._save_point_time", "_m._saved_point_times",
    "_m._stocks_analysis_cache", "_m._futures_analysis_cache",
    "_m._cache_lock", "_m.FREQ_TO_COL",
]
_BYPASS_IMPORTS = [
    "from App.AppEngine import _cache_",
    "from App.AppEngine import _saved_point_times",
    "from App.AppEngine import _stocks_analysis_cache",
    "from App.AppEngine import _futures_analysis_cache",
]
_BYPASS_FILES = [
    os.path.join("App", "AppChart.py"),
    os.path.join("App", "AppScan.py"),
    os.path.join("BuySellPoint", "BSPointList.py"),
]


def test_no_state_bypass(failures):
    """服务层不得绕行 AppEngine 状态别名（P0-2 成果守护）"""
    bad = []
    for rel in _BYPASS_FILES:
        src = read_src(rel)
        for i, line in enumerate(src.splitlines(), 1):
            s = line.strip()
            if s.startswith("#"):
                continue
            for pat in _BYPASS_PATTERNS:
                if pat in s:
                    bad.append(f"{rel}:{i} 绕行 {pat}（应改走 app_data.* 公共 API）")
            for imp in _BYPASS_IMPORTS:
                if imp in s:
                    bad.append(f"{rel}:{i} 绕行导入 {imp}（应改走 app_data.* 公共 API）")
    if bad:
        failures.extend(f"状态别名零绕行: {b}" for b in bad)
        for b in bad[:8]:
            print(f"[FAIL] ②b 状态别名零绕行: {b}")
        if len(bad) > 8:
            print(f"        …共 {len(bad)} 处")
    else:
        print(f"[PASS] ②b 状态别名零绕行: {len(_BYPASS_FILES)} 个服务模块零绕行 AppEngine 状态别名"
              f"（P0-2 已全部改走 app_data.*）")


# ═══════════════════════════════════════════════════════════════════════
# ③ 配置别名清零（不得复活）
# ═══════════════════════════════════════════════════════════════════════
RETIRED_ALIASES = [
    "TDX_INSTALL_DIR", "VIPDOC_DIR", "DOWNLOAD_DIR", "TDX_HQ_CACHE",
    "CHAN_PATH", "OUTPUT_DIR", "LAST_CODE_FREQ_FILE",
    "_STOCK_NAMES_CACHE_FILE", "_STOCK_PE_TTM_FILE", "_FLOAT_MC_CACHE_FILE",
    "SAVED_POINT_FILE", "ANNOTATIONS_FILE", "_MAX_CACHE_SIZE",
]


def test_no_revived_aliases(failures):
    from App import AppEngine as m
    revived = [n for n in RETIRED_ALIASES if hasattr(m, n)]
    if revived:
        failures.append("配置别名复活: " + ", ".join(revived))
        print(f"[FAIL] ③ 别名清零: {len(revived)} 个已删别名复活: {revived}")
    else:
        print(f"[PASS] ③ 别名清零: {len(RETIRED_ALIASES)} 个路径/容量别名保持清零"
              f"（配置单源 = app_config 直读）")


# ═══════════════════════════════════════════════════════════════════════
# ④ 自选股收敛（源码级）
# ═══════════════════════════════════════════════════════════════════════
def test_zxg_convergence(failures):
    bad = []

    tdx_src = read_src(os.path.join("DataAPI", "TdxAPI.py"))
    tree = ast.parse(tdx_src)
    top_defs = {n.name for n in tree.body
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    top_assigns = {t.id for n in tree.body if isinstance(n, ast.Assign)
                   for t in n.targets if isinstance(t, ast.Name)}
    for gone in ("read_zxg_stocks", "save_to_zxg_blk"):
        if gone in top_defs:
            bad.append(f"DataAPI/TdxAPI.py 仍定义 {gone}()（应已迁出）")
    for gone in ("_ZXG_HK_INDEX_MAP", "_ZXG_US_INDEX_MAP"):
        if gone in top_assigns:
            bad.append(f"DataAPI/TdxAPI.py 仍定义 {gone}（应已迁出）")

    # ths_sync_to_tdx 改从 AppData 导入
    ths_src = read_src(os.path.join("App", "ths_sync_to_tdx.py"))
    if "from App.AppData import app_data" not in ths_src:
        bad.append("App/ths_sync_to_tdx.py 未从 App.AppData 导入 app_data")
    if "from DataAPI.TdxAPI import save_to_zxg_blk" in ths_src:
        bad.append("App/ths_sync_to_tdx.py 仍从 TdxAPI 导入 save_to_zxg_blk")

    # AppEngine 不再从 TdxAPI 导入自选股入口
    m_src = read_src(os.path.join("App", "AppEngine.py"))
    for i, line in enumerate(m_src.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        if ("read_zxg_stocks" in s or "save_to_zxg_blk" in s) and "import" in s:
            bad.append(f"App/AppEngine.py:{i} 仍 import 自选股入口")

    # AppOrch 不再残留 _m.read_zxg_stocks 引用（阶段 4 迁移遗漏防护：
    # read_zxg_stocks 已迁至 AppOrch 模块级，AppEngine 中已无此属性，
    # 残留引用会在 /api/scan_stock_list?source=zxg 时抛 AttributeError）
    orch_src = read_src(os.path.join("App", "AppOrch.py"))
    for i, line in enumerate(orch_src.splitlines(), 1):
        if "_m.read_zxg_stocks" in line:
            bad.append(f"App/AppOrch.py:{i} 残留 _m.read_zxg_stocks（应使用模块级 read_zxg_stocks）")

    if bad:
        failures.extend(f"自选股收敛: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ④ 自选股收敛: {b}")
    else:
        print("[PASS] ④ 自选股收敛: TdxAPI 墓碑化（实现迁 AppData），"
              "ths_sync / AppEngine 改道 AppData")


# ═══════════════════════════════════════════════════════════════════════
# ⑤ 分层方向（import 静态分析）
# ═══════════════════════════════════════════════════════════════════════
def _module_imports(rel):
    """返回**模块级** import 的目标模块名集合（函数体内的惰性 import 不算）"""
    tree = ast.parse(read_src(rel))
    mods = set()
    for n in tree.body:  # 仅顶层（模块级）节点
        if isinstance(n, ast.Import):
            for a in n.names:
                mods.add(a.name.split(".")[0])
        elif isinstance(n, ast.ImportFrom) and n.module and n.level == 0:
            mods.add(n.module.split(".")[0])
    return mods


def test_layering_direction(failures):
    bad = []
    # AppData：只准依赖标准库/第三方 + App.AppConfig（禁 FrontAPI/AppOrch/AppEngine/DataAPI 模块级）
    data_mods = _module_imports(os.path.join("App", "AppData.py"))
    for forbidden in ("FrontAPI", "api_server", "AppOrch", "AppEngine", "DataAPI"):
        if forbidden in data_mods:
            bad.append(f"App/AppData.py 模块级 import 了 {forbidden}（数据层不得反向/跨层依赖）")
    # AppOrch：禁 FrontAPI / api_server
    orch_mods = _module_imports(os.path.join("App", "AppOrch.py"))
    for forbidden in ("FrontAPI", "api_server"):
        if forbidden in orch_mods:
            bad.append(f"App/AppOrch.py import 了 {forbidden}（服务层不得依赖 API 层）")
    # FrontAPI：不得直连 AppData（必须经 AppOrch 漏斗）
    fe_src = read_src("FrontAPI.py")
    for i, line in enumerate(fe_src.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        if ("from App.AppData import" in s) or ("import App.AppData" in s):
            bad.append(f"FrontAPI.py:{i} 直连 AppData（应经 App.AppOrch 漏斗）")
        # 直连 AppEngine 数据层状态（阶段 4 已收敛的 7 个别名）
        for alias in ("_saved_point_times",
                      "_stocks_analysis_cache", "_futures_analysis_cache"):
            if f"m.{alias}" in s:
                bad.append(f"FrontAPI.py:{i} 直连 m.{alias}（数据状态须经 AppOrch/AppData）")

    if bad:
        failures.extend(f"分层方向: {b}" for b in bad)
        for b in bad[:8]:
            print(f"[FAIL] ⑤ 分层方向: {b}")
        if len(bad) > 8:
            print(f"        …共 {len(bad)} 处")
    else:
        print("[PASS] ⑤ 分层方向: FrontAPI → AppOrch → AppData → AppConfig 单向无违反")


# ═══════════════════════════════════════════════════════════════════════
# ⑥ 启动加载零漂移 + LRU 语义（运行时轻量行为网）
# ═══════════════════════════════════════════════════════════════════════
def test_startup_and_lru(failures):
    from App.AppData import app_data, AppData
    bad = []

    # 单例唯一：两种导入路径同一对象
    from App import AppData as _ad_mod
    if _ad_mod.app_data is not app_data:
        bad.append("AppData 单例漂移（app_data 存在多实例）")

    # 启动加载：实例化即完成选点/标注加载（与原 import 期行为一致）
    from App import AppEngine as m
    if not isinstance(app_data.saved_point_times, dict):
        bad.append("saved_point_times 启动加载缺失（非 dict）")
    if not app_data._annotations_loaded:
        bad.append("annotations 启动加载缺失（_annotations_loaded=False）")
    if m._saved_point_times is not app_data.saved_point_times:
        bad.append("AppEngine._saved_point_times 与 app_data 漂移（② 的运行时复核）")

    # LRU 语义：超 50 淘汰最旧；get 命中移尾（隔离验证，不污染全局单例）
    from Test.snapshot_runner import isolate_side_effects
    restore = isolate_side_effects()
    try:
        probe = AppData()
        for i in range(55):
            probe.cache_put(f"__p4_probe_{i}", i)
        size = len(probe._stocks_analysis_cache)
        if size != AppData.MAX_CACHE_SIZE:
            bad.append(f"LRU 容量失守: 55 put 后 {size} 条（上限 {AppData.MAX_CACHE_SIZE}）")
        if "__p4_probe_0" in probe._stocks_analysis_cache:
            bad.append("LRU 未淘汰最旧条目（probe_0 仍在）")
        if "__p4_probe_54" not in probe._stocks_analysis_cache:
            bad.append("LRU 误删最新条目（probe_54 缺失）")
        # get 命中移尾：访问 probe_5 后再 put 一条，被淘汰的应是 probe_6
        probe.cache_get("__p4_probe_5")
        probe.cache_put("__p4_probe_x", "x")
        if "__p4_probe_5" not in probe._stocks_analysis_cache:
            bad.append("cache_get 未把命中条目移尾（probe_5 被误淘汰）")
        if "__p4_probe_6" in probe._stocks_analysis_cache:
            bad.append("cache_get 后 LRU 顺序未更新（probe_6 应被淘汰）")
    finally:
        restore()

    if bad:
        failures.extend(f"启动/LRU: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ⑥ 启动/LRU: {b}")
    else:
        print(f"[PASS] ⑥ 启动/LRU: 单例唯一 + 启动加载完成 + "
              f"LRU {AppData.MAX_CACHE_SIZE} 上限淘汰/移尾语义保持")


# ═══════════════════════════════════════════════════════════════════════
# ⑦ 语义化子窗接口（吸收外部评审：key 规则内聚数据层）
# ═══════════════════════════════════════════════════════════════════════
def test_semantic_subchan(failures):
    from App.AppData import app_data, AppData
    from App import AppOrch as orch
    bad = []

    # 接口齐备
    for fn in ("set_futures_sub_chan", "get_futures_sub_chan", "pop_futures_sub_chan"):
        if not hasattr(app_data, fn):
            bad.append(f"app_data.{fn} 缺失（语义化子窗接口不完整）")
    for fn in ("futures_set_sub_chan", "futures_get_sub_chan", "futures_pop_sub_chan"):
        if not hasattr(orch, fn):
            bad.append(f"orch.{fn} 漏斗缺失")
    if bad:
        failures.extend(f"语义化子窗: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ⑦ 语义化子窗: {b}")
        return

    # 运行时语义：大小写不敏感 + 与泛型视图同储 + pop 释放（隔离验证）
    from Test.snapshot_runner import isolate_side_effects
    restore = isolate_side_effects()
    try:
        probe = AppData()
        sentinel = object()
        probe.set_futures_sub_chan("kq.m@shfe.rb", "1m", sentinel)      # 小写写入
        if probe.get_futures_sub_chan("KQ.m@SHFE.rb", "1m") is not sentinel:
            bad.append("大小写不敏感失效（小写写 → 大写读未命中）")
        if probe.futures_cache_get("KQ.M@SHFE.RB:1m") is not sentinel:
            bad.append("语义接口与泛型视图不同储（key 规则分叉）")
        if probe.pop_futures_sub_chan("kq.m@shfe.rb", "1m") is not sentinel:
            bad.append("pop_futures_sub_chan 未返回被释放对象")
        if probe.get_futures_sub_chan("KQ.m@SHFE.rb", "1m") is not None:
            bad.append("pop 后仍可读（子窗 chan 泄漏）")
    finally:
        restore()

    # 源码级：FrontAPI 不再手工拼 "{SYMBOL}:{sub_freq}" key
    fe_src = read_src("FrontAPI.py")
    for i, line in enumerate(fe_src.splitlines(), 1):
        s = line.strip()
        if s.startswith("#"):
            continue
        if ":{sub_freq}\"" in line and ("upper()" in line or ":" in line.split("f\"")[0]):
            if "orch.futures_set_sub_chan" not in line and "orch.futures_pop_sub_chan" not in line \
                    and "f\"" in line and "upper()" in line:
                bad.append(f"FrontAPI.py:{i} 仍手工拼子窗 key（应由语义化漏斗内聚）")

    if bad:
        failures.extend(f"语义化子窗: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ⑦ 语义化子窗: {b}")
    else:
        print("[PASS] ⑦ 语义化子窗: set/get/pop 三件套齐备，key 规则内聚数据层，"
              "大小写不敏感，FrontAPI 零手工拼 key")


# ═══════════════════════════════════════════════════════════════════════
# ⑧ 数据层反向依赖禁令（防「影子数据层」双源结构）
# ═══════════════════════════════════════════════════════════════════════
_FORBIDDEN_DATA_DEPS = ("AppEngine", "FrontAPI", "api_server", "AppOrch", "DataAPI")


def test_data_layer_purity(failures):
    """App/AppData.py 任何层级（含函数内惰性 import）都不得反向依赖上层/引擎。

    外部评审实现的 AppData 在 14 处函数体内 import AppEngine、并委托
    DataAPI.TdxAPI 读写自选股 —— 数据层反向引用引擎形成「双源影子层」。
    本守护把该模式封禁为结构性红线。"""
    tree = ast.parse(read_src(os.path.join("App", "AppData.py")))
    bad = []
    for node in ast.walk(tree):  # 全层级扫描（模块级 + 函数体内）
        if isinstance(node, ast.Import):
            for a in node.names:
                root = a.name.split(".")[0]
                if root in _FORBIDDEN_DATA_DEPS:
                    bad.append(f"AppData.py import {a.name}（反向/跨层依赖）")
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            root = node.module.split(".")[0]
            if root in _FORBIDDEN_DATA_DEPS:
                bad.append(f"AppData.py from {node.module} import（反向/跨层依赖）")

    if bad:
        failures.extend(f"数据层纯度: {b}" for b in bad)
        for b in bad[:8]:
            print(f"[FAIL] ⑧ 数据层纯度: {b}")
        if len(bad) > 8:
            print(f"        …共 {len(bad)} 处")
    else:
        print("[PASS] ⑧ 数据层纯度: AppData 全层级零反向依赖"
              f"（禁 {len(_FORBIDDEN_DATA_DEPS)} 类上层/引擎模块，防影子层双源）")


# ═══════════════════════════════════════════════════════════════════════
# ⑨ 引擎引用有效性（迁移遗漏防护）
# ═══════════════════════════════════════════════════════════════════════
def test_engine_refs_valid(failures):
    """AppOrch 中所有 _m.<attr> 引用必须能在 AppEngine 中解析。

    阶段 4 起 AppEngine 的 DATA 族实现/常量逐步迁往 AppData/AppConfig，
    本守护用 AST 静态分析（不 import 引擎，避免环境副作用）收集
    AppEngine 全部可解析名字（含 try/except 条件导入块），
    校验 AppOrch 的 _m. 引用均在其中。
    """
    bad = []

    def collect_names(src):
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):  # 遍历所有层级（含 try/except 内条件导入）
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for t in targets:
                    if isinstance(t, ast.Name):
                        names.add(t.id)
                    elif isinstance(t, (ast.Tuple, ast.List)):
                        for elt in t.elts:
                            if isinstance(elt, ast.Name):
                                names.add(elt.id)
            elif isinstance(node, ast.Import):
                for a in node.names:
                    names.add(a.asname or a.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom):
                for a in node.names:
                    names.add(a.asname or a.name)
        return names

    m_names = collect_names(read_src(os.path.join("App", "AppEngine.py")))
    orch_tree = ast.parse(read_src(os.path.join("App", "AppOrch.py")))
    refs = set()
    for node in ast.walk(orch_tree):
        if isinstance(node, ast.Attribute):
            v = node.value
            if isinstance(v, ast.Name) and v.id == "_m":
                refs.add(node.attr)

    missing = sorted(r for r in refs if r not in m_names)
    for m in missing:
        bad.append(f"App/AppOrch.py 引用 _m.{m}，但 AppEngine 中已不存在（迁移遗漏，运行时 AttributeError）")

    if bad:
        failures.extend(f"引擎引用有效性: {b}" for b in bad)
        for b in bad:
            print(f"[FAIL] ⑨ 引擎引用有效性: {b}")
    else:
        print(f"[PASS] ⑨ 引擎引用有效性: AppOrch 全部 {len(refs)} 个 _m.<attr> 引用在 AppEngine 中可解析")


# ═══════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════
def main():
    ap = argparse.ArgumentParser(description="阶段 4 数据层收敛 · 成果防护")
    ap.add_argument("--update", action="store_true",
                    help="兼容 run_all --update（本守护无冻结基线，等价校验）")
    args = ap.parse_args()

    failures = []
    print("=" * 64)
    print("阶段 4 成果防护：数据层收敛（设计 8.7 / 4.4 / 6.3）")
    print("=" * 64)
    test_shell_purity(failures)
    test_alias_identity(failures)
    test_no_state_bypass(failures)
    test_no_revived_aliases(failures)
    test_zxg_convergence(failures)
    test_layering_direction(failures)
    test_startup_and_lru(failures)
    test_semantic_subchan(failures)
    test_data_layer_purity(failures)
    test_engine_refs_valid(failures)
    print("-" * 64)
    if failures:
        print(f"===== 阶段 4 成果防护: 失败 {len(failures)} 项 =====")
        for f in failures:
            print(" -", f)
        return False
    print("===== 阶段 4 成果防护: 全部通过（9 类守护） =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
