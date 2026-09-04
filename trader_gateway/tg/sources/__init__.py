# -*- coding: utf-8 -*-
"""信号源层。实时 SSE 与离线回放对外产出完全相同的事件流。"""
import importlib
import os
import pkgutil

from .base import SOURCES, Source, build_source, register_source

_EXCLUDE = {"base", "__init__"}
_pkg_dir = os.path.dirname(os.path.abspath(__file__))
for _m in pkgutil.iter_modules([_pkg_dir]):
    if _m.name in _EXCLUDE:
        continue
    try:
        importlib.import_module("." + _m.name, __name__)
    except Exception as _e:
        print("[sources] 跳过 {}: {}".format(_m.name, _e))

__all__ = ["Source", "build_source", "register_source", "SOURCES"]
