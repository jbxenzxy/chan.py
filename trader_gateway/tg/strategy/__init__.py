# -*- coding: utf-8 -*-
"""策略层。新增策略请继承 base 里的接口并用 @register 装饰。

即插即用：本目录（含子目录）下的 .py 文件会在包导入时自动 import，
所以"新增一个策略"= 往这个目录丢一个 py 文件，不需要改任何注册代码。
参考 example_trailing.py。
"""
import importlib
import os
import pkgutil

from .base import (
    ENTRY_POLICIES, EXIT_POLICIES, EntryPolicy, ExitCheck, ExitPolicy,
    build_entry_policy, build_exit_policy, register_entry, register_exit,
)

_EXCLUDE = {"base", "__init__"}
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
for _m in pkgutil.iter_modules([_pkg_dir]):
    if _m.name in _EXCLUDE:
        continue
    try:
        importlib.import_module("." + _m.name, __name__)
    except Exception as _e:      # 单个策略文件写错不应拖垮整个网关
        print("[strategy] 跳过 {}: {}".format(_m.name, _e))

__all__ = [
    "EntryPolicy", "ExitPolicy", "ExitCheck",
    "build_entry_policy", "build_exit_policy",
    "register_entry", "register_exit",
    "ENTRY_POLICIES", "EXIT_POLICIES",
]
