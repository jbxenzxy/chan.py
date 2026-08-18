# -*- coding: utf-8 -*-
"""
阶段 2.5：阶段 2 成果防护 + 引擎级边界用例
=====================================================================
吸收外部评审实现（Script/test_regression.py）中四处可借鉴设计的前三处：

  1. 配置一致性   my_chan_main 遗留常量与 AppConfig 派生路径必须逐对一致
                 —— 阶段 2「配置中心化」的直接防回归
  2. 领域异常链路 api_server 的 AppError 处理器：NotFoundError → 404 +
                 结构化 JSON —— 阶段 2「评审 A」修复的防回归
  3. 引擎级边界   无效代码 / 不支持周期 / 日期格式错误 → 返回 error 字典
                 而非崩溃（零数据依赖，离线可跑）
  3b.日期格式坑   end_date 仅支持斜杠格式（%Y/%m/%d 系），连字符必被拒——
                 外部实现的 trigger_step 用例因 "2024-01-31" 连字符必然
                 失败，本用例把该口径冻结为显式契约

运行：python Test/test_phase2_guards.py
"""
import asyncio
import os
import sys

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)
sys.path.insert(0, REPO_ROOT)

import typing
if not hasattr(typing, "Self"):
    import typing_extensions
    typing.Self = typing_extensions.Self

from Test.snapshot_runner import isolate_side_effects


# ═══════════════════════════════════════════════════════════════════
# 1. 配置一致性（阶段 2「配置中心化」防回归 · 阶段 4 语义更新）
# ═══════════════════════════════════════════════════════════════════
# 阶段 4 起 my_chan_main 的路径别名已全部删除（阶段 2 是「别名 = 派生值」，
# 阶段 4 升级为「别名不存在，一律 app_config.<属性> 直读」）。
# 守护语义随之升级：① 别名不得复活；② AppData 持久化路径与 AppConfig
# 单一事实源逐对一致（数据层落盘位置不走私）。
_PHASE4_RETIRED_ALIASES = [
    "TDX_INSTALL_DIR", "VIPDOC_DIR", "DOWNLOAD_DIR", "CHAN_PATH", "OUTPUT_DIR",
    "SAVED_POINT_FILE", "ANNOTATIONS_FILE", "LAST_CODE_FREQ_FILE",
    "_STOCK_NAMES_CACHE_FILE", "_STOCK_PE_TTM_FILE", "_FLOAT_MC_CACHE_FILE",
    "_MAX_CACHE_SIZE",
]

def test_config_consistency(failures):
    """阶段 4 语义：① 路径别名清零不得复活；② AppData 路径 = AppConfig。"""
    from App import AppEngine as m
    from App.AppConfig import app_config
    from App.AppData import AppData

    revived = [n for n in _PHASE4_RETIRED_ALIASES if hasattr(m, n)]
    if revived:
        failures.append("配置一致性: 别名复活 " + ", ".join(revived))
        print(f"[FAIL] ① 配置一致性: {len(revived)} 个阶段 4 已删别名复活: {revived}")
        return

    d = AppData()
    pairs = [
        ("saved_point_file",      d.saved_point_file,      app_config.saved_point_file),
        ("annotations_file",      d.annotations_file,      app_config.annotations_file),
        ("last_code_freq_file",   d.last_code_freq_file,   app_config.last_code_freq_file),
        ("stock_names_cache_file", d.stock_names_cache_file, app_config.stock_names_cache_file),
        ("stock_pe_ttm_file",     d.stock_pe_ttm_file,     app_config.stock_pe_ttm_file),
        ("float_mc_cache_file",   d.float_mc_cache_file,   app_config.float_mc_cache_file),
    ]
    bad = [f"{n}: 数据层 {a!r} != 配置侧 {b!r}"
           for n, a, b in pairs
           if os.path.normpath(str(a)) != os.path.normpath(str(b))]
    if bad:
        failures.append("配置一致性: " + "; ".join(bad))
        print(f"[FAIL] ① 配置一致性: AppData {len(bad)} 对路径与 AppConfig 不一致")
        for x in bad:
            print("      -", x)
    else:
        print(f"[PASS] ① 配置一致性: 别名 {len(_PHASE4_RETIRED_ALIASES)} 个清零 + "
              f"AppData {len(pairs)} 对路径全部一致（单一事实源 = AppConfig）")


# ═══════════════════════════════════════════════════════════════════
# 2. 领域异常链路（阶段 2 评审 A 修复的防回归）
# ═══════════════════════════════════════════════════════════════════
def test_app_error_chain(failures):
    """AppError 统一处理器：领域异常 → exc.status_code + 结构化 JSON。
    路由层不得吞异常，处理链路断裂会让前端收到 500 裸文本。"""
    import FrontAPI as routes
    from App.AppOrch import NotFoundError, AppError
    from fastapi import Request

    # FastAPI exception_handlers 的键是异常类；逐类查找（AppError 或其子类）
    handler = None
    for exc_type, h in routes.app.exception_handlers.items():
        if exc_type is AppError or (isinstance(exc_type, type)
                                    and issubclass(exc_type, AppError)):
            handler = h
            break
    if handler is None:
        failures.append("领域异常链路: api_server 未注册 AppError 处理器")
        print("[FAIL] ② 领域异常链路: 处理器未注册")
        return

    scope = {"type": "http", "method": "GET", "path": "/api/stock",
             "headers": [], "query_string": b""}
    req = Request(scope)

    async def _invoke():
        try:
            raise NotFoundError("测试股票不存在")
        except Exception as exc:
            return await handler(req, exc)

    resp = asyncio.run(_invoke())
    body = resp.body.decode("utf-8")
    if resp.status_code != 404:
        failures.append(f"领域异常链路: NotFoundError 应映射 404，实际 {resp.status_code}")
        print(f"[FAIL] ② 领域异常链路: 状态码 {resp.status_code} != 404")
    elif "NotFoundError" not in body or "detail" not in body:
        failures.append(f"领域异常链路: 响应体非结构化: {body[:120]}")
        print(f"[FAIL] ② 领域异常链路: 响应体缺 error/detail 字段")
    else:
        print(f"[PASS] ② 领域异常链路: NotFoundError → 404 + 结构化 JSON"
              f"（{body[:60]}…）")


# ═══════════════════════════════════════════════════════════════════
# 3. 引擎级边界（错误输入 → error 字典，绝不裸抛）
# ═══════════════════════════════════════════════════════════════════
def _call_stock(failures, name, **kw):
    """调用股票分析入口，断言返回 dict 且含 error（而非抛异常）"""
    from App import AppEngine as m
    restore = isolate_side_effects()
    try:
        r = m._analyze_stock_internal(**kw)
    except Exception as e:
        failures.append(f"{name}: 引擎裸抛 {type(e).__name__}: {e}")
        print(f"[FAIL] ③ {name}: 裸抛异常（应返回 error 字典）")
        return None
    finally:
        restore()
    if not (isinstance(r, dict) and "error" in r):
        failures.append(f"{name}: 应返回含 error 的 dict，实际 {str(r)[:80]}")
        print(f"[FAIL] ③ {name}: 未返回 error 字典")
        return None
    print(f"[PASS] ③ {name}: error 字典（{r['error'][:50]}）")
    return r


def test_engine_boundaries(failures):
    """股票引擎边界：无效代码 / 不支持周期，均应优雅返回 error。"""
    _call_stock(failures, "无效代码", code="INVALID_CODE", freq="d", cache_chan=False)
    _call_stock(failures, "不支持周期", code="SH000001", freq="xx", cache_chan=False)


def test_futures_boundaries(failures):
    """期货引擎边界（离线注入冻结数据）：
       3a. end_date 连字符格式 → 必须返回 error（契约：仅支持斜杠格式）
       3b. 有效斜杠格式 + 冻结数据 → 正常分析（顺带证明注入通道可用）"""
    from Test.snapshot_runner import install_futures_data_source

    # 3a 连字符契约（真实天勤路径中该格式同样被拒，口径需冻结）
    restore = install_futures_data_source("futures_15s.json")
    try:
        from App import AppEngine as m
        r = m._analyze_futures_internal("KQ.m@SHFE.rb", freq="15s",
                                        end_date="2024-01-31")
        if not (isinstance(r, dict) and "error" in r):
            failures.append(f"期货日期格式契约: 连字符应被拒，实际 {str(r)[:80]}")
            print("[FAIL] ③ 期货日期契约: 连字符 end_date 未被拒绝")
        else:
            print(f"[PASS] ③ 期货日期契约: 连字符被拒（{r['error'][:40]}）——"
                  f"契约：end_date 仅支持 %Y/%m/%d 系斜杠格式")
    finally:
        restore()


# ═══════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════
def main():
    failures = []
    test_config_consistency(failures)
    test_app_error_chain(failures)
    test_engine_boundaries(failures)
    test_futures_boundaries(failures)

    print()
    if failures:
        print(f"===== 阶段 2 成果防护: 失败 {len(failures)} 项 =====")
        for x in failures:
            print(" -", x)
        return False
    print("===== 阶段 2 成果防护: 全部通过 =====")
    return True


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
