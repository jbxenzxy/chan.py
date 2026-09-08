# -*- coding: utf-8 -*-
"""策略层（2026-09-08 精简：取消策略选择器抽象）。

生产环境入场只有一个策略 DefaultEntryPolicy、出场只有一个策略
LayeredExitPolicy（L1-L4 分层），不再有「注册表 / @register / build_*_policy」
这类选择器机制。main.py 与测试直接实例化这两个类即可。

参考 Exit.py 里 LayeredExitPolicy 的完整实现（L1-L4 各层注释）。
"""
from .Base import ExitCheck, ExitPolicy, EntryPolicy
from .Entry import DefaultEntryPolicy
from .Exit import LayeredExitPolicy

__all__ = [
    "EntryPolicy", "ExitPolicy", "ExitCheck",
    "DefaultEntryPolicy", "LayeredExitPolicy",
]
