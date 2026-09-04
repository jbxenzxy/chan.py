# -*- coding: utf-8 -*-
"""执行层。接真实账户时新增一个 Broker 子类即可（同目录自动扫描注册）。"""
import importlib
import os
import pkgutil

from .base import BROKERS, Broker, build_broker, register_broker

_EXCLUDE = {"base", "__init__"}
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
for _m in pkgutil.iter_modules([_pkg_dir]):
    if _m.name in _EXCLUDE:
        continue
    try:
        importlib.import_module("." + _m.name, __name__)
    except Exception as _e:
        print("[brokers] 跳过 {}: {}".format(_m.name, _e))

__all__ = ["Broker", "build_broker", "register_broker", "BROKERS"]
