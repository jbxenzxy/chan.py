# -*- coding: utf-8 -*-
"""并发用例的**自包含**依赖兜底（缺三方库时用最小 stub 顶替）

背景
----
仓库内的并发守护用例（test_lock_v5_guards / test_chan_data_isolation /
test_chan_data_isolation_control）原本这样取 stub::

    _STUBS = os.path.join(os.path.dirname(_REPO_ROOT), "stubs")
    if os.path.isdir(_STUBS):
        sys.path.insert(0, _STUBS)

问题在于 ``stubs/`` 位于仓库**外部**且不在版本库中。CI 或任何纯净
环境一旦没有 pandas / numpy / chinese_calendar，这些用例会在 import
阶段直接抛 ModuleNotFoundError —— 断言一行都没执行，守护静默失效
（外层若吞异常，报告上甚至看不出它"没跑"）。这是审计 v1.3 §四没有
覆盖到的一类覆盖面盲区：**守护自身的可执行性**没有守护。

本模块把兜底改为自包含：不依赖任何仓库外目录，缺什么补什么，
且**仅在真的缺失时**注入（装了真库就完全不介入，零副作用）。

用法（必须在 import 任何 App / DataAPI 模块之前调用）::

    import _stub_env
    _stub_env.install()

原理
----
用 PEP 562 的模块级 ``__getattr__`` 造"惰性模块"：任何属性访问都返回
``_Any`` 替身，可调用、可迭代（空）。这覆盖了模块级形如
``import pandas as pd`` 的导入需求——只要不在 import 阶段真的做数值
计算，就不会碰壁。本用例集只用 AppScan / AppSSE 的**状态与锁语义**，
不碰真实行情计算，故该兜底等价于真库。
"""
import sys
import types

# 可能出现在导入链上的第三方依赖（缺哪个补哪个，不预设全缺）
_GUARDED = ("pandas", "numpy", "chinese_calendar")


class _Any:
    """万能替身：属性访问/调用均返回自身，迭代为空。

    用于顶替 pandas.DataFrame / numpy.ndarray 等只在模块级被引用、
    在本用例的代码路径上不会被真正求值(算)的名字。
    """

    def __init__(self, *a, **k):
        pass

    def __call__(self, *a, **k):
        return _Any()

    def __getattr__(self, _name):
        return _Any()

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return False


def _lazy_module(name):
    """构造惰性模块：任意属性 → _Any（PEP 562）"""
    mod = types.ModuleType(name)
    mod.__getattr__ = lambda _name: _Any()          # noqa: E731
    return mod


def install(extra=()):
    """注入缺失依赖的 stub。已安装的真库一律不覆盖。

    :param extra: 额外需要兜底的模块名（元组/列表）
    :return: 本次实际注入的模块名列表（便于用例打印说明）
    """
    injected = []
    for dep in tuple(_GUARDED) + tuple(extra):
        if dep in sys.modules:
            continue
        try:
            __import__(dep)
            continue                                 # 真库可用 → 不介入
        except ImportError:
            pass
        sys.modules[dep] = _lazy_module(dep)
        injected.append(dep)
    return injected


if __name__ == "__main__":
    print("注入:", install() or "（依赖齐全，未注入）")
