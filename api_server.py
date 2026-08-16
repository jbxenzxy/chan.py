# -*- coding: utf-8 -*-
"""
api_server.py —— 兼容壳（阶段 3a 退役，见设计文档 V10 方案 8.6）
=========================================================================
本文件已完成历史使命，退役为纯转发壳：

  - 全部 31 条路由（30 REST + 1 兼容重定向）已于阶段 3a 迁入 FrontAPI.py
    （单一路由源）；本文件不再定义任何路由；
  - app / router / SSE Mock 桥接层（_SSEMockWfile / _SSEMockHandler /
    _sse_generator）/ _json_response 均为 FrontAPI 同名对象别名——
    存量部署脚本（uvicorn api_server:app）与测试用例
    （Test/test_sse_sequence.py、test_phase2_guards.py）零改动可用；
  - 引擎调用锁分类（SERIAL / SCAN / SELF_CONTAINED）见 App/AppOrch.py
    LOCK_POLICY 登记表，Test/test_phase3_guards.py 守护。

启动（二选一，行为一致）：
    python FrontAPI.py            # 推荐入口（端口/地址走 App/AppConfig.py）
    python api_server.py          # 兼容入口（同一 app 实例）
    uvicorn api_server:app        # 存量部署脚本

拆除计划：3b-2（SSE 灰度通过后）——届时 SSE Mock 桥接层与
ChartHandler SSE 方法一并拆除，本文件若外部无引用可整体删除。
"""

import sys
import os

# ── 确保仓库根目录可导入（FrontAPI.py 所在目录）──────────────────────
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if _SCRIPT_DIR not in sys.path:
    sys.path.insert(0, _SCRIPT_DIR)

# ── 转发 FrontAPI（单一路由源）──────────────────────────────────────
# import FrontAPI 触发其模块级组装（含 my_chan_main 模块级初始化）。
import FrontAPI as _fe

# 单一 app 实例：两个入口名指向同一对象，避免双注册路由。
app = _fe.app
router = _fe.router

# ── SSE Mock 桥接层（3b-2 拆除）─────────────────────────────────────
# 迁至 FrontAPI 后按原符号名再导出，测试用例零改动。
_SSEMockWfile = _fe._SSEMockWfile
_SSEMockHandler = _fe._SSEMockHandler
_sse_generator = _fe._sse_generator

# ── 工具函数 ────────────────────────────────────────────────────────
_json_response = _fe._json_response


# ═══════════════════════════════════════════════════════════════════════
# 入口（与 FrontAPI.__main__ 等价：同一 app、同一端口策略）
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import socket
    import uvicorn
    from App.AppConfig import app_config

    PORT = app_config.port
    HOST = app_config.host

    # ── 端口占用检测（与 FrontAPI 同策略）──────────────────────────
    try:
        _probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        _probe.bind((HOST, PORT))
        _probe.close()
    except OSError:
        print(f"[错误] 端口 {PORT} 已被占用！")
        print(f"[错误] 可能原因：FrontAPI.py 新入口或其他服务仍在运行。")
        print(f"[解决] Windows 可执行: netstat -ano | findstr {PORT} 查看占用 PID，")
        print(f"[解决] 然后执行: taskkill /PID <PID> /F 结束旧进程。")
        sys.exit(1)

    # ── 恢复上次标的提示（合并自评审对方包的优点，经 orch 服务函数）──
    from App import AppOrch as orch
    last_code, last_freq = orch.load_last_code_freq()
    if last_code:
        print(f"[信息] 恢复上次: {last_code} (周期: {last_freq})")
    else:
        print(f"[信息] 使用默认股票（上次记录为空）")

    print(f"[信息] api_server 兼容入口启动（实际服务: FrontAPI.app）: http://{HOST}:{PORT}")
    print(f"[信息] 推荐改用: python FrontAPI.py（阶段 3a 起的统一入口）")
    uvicorn.run(app, host=HOST, port=PORT, log_level="info")