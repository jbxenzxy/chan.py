# -*- coding: utf-8 -*-
"""
阶段 2.5：回归测试基线 —— 统一入口
=====================================================================
一条命令跑完整个基线，产出汇总报告。各组件独立子进程执行
（monkeypatch 互不干扰），任一失败即整体退出码非 0（可直接接入
CI / 迁移每阶段的验收门禁）。

组件（按依赖顺序；1~34 为历史阶段组件，其后的分组见 COMPONENTS 内联注释）：
  1. fixtures 完整性   gen_fixtures.py --check      冻结输入未被手改
  2. 核心快照回归      snapshot_runner.py --all     笔/段/中枢/买卖点 7 维度（股票+期货）
  3. trigger_step 回放 test_trigger_step_replay.py  逐步回放收敛一致性
  4. 阶段 2 成果防护   test_phase2_guards.py        配置一致性/异常链路/引擎边界/日期契约
  5. 确定性测试        test_determinism.py          重复调用/跨路径污染/双窗口语义
  6. 行业映射完整性    test_industry_mapping.py     双路径加载不静默降级 + 条目质量
  7. SSE 事件序列      test_sse_sequence.py         首事件/序列/正常关闭（native 生成器）
  8. 函数映射同步      func_map_check.py            阶段 2.6：74 函数/57 状态归属
                                                     完备·无幽灵·行号无漂移
  9. 阶段 3 成果防护   test_phase3_guards.py        锁分类/直连清零/路由收敛/墓碑/
                                                     SSE 单实现/分层方向
 10. 阶段 4 成果防护   test_phase4_guards.py        委托壳+目标存在/状态别名同一性/
                                                     配置别名清零/自选股收敛/语义子窗/
                                                     分层方向/LRU 语义/数据源 import 门禁
 11. 市场量能行为测试  test_app_amo.py               真行为测试（合成 .day 合成数据）
 12. 双窗公式纯函数    test_stocks_dual_algo.py      P0 4 向公式单测（方向×边界）
 13. .blk 解析与自选股  test_blk_parsing.py          黄金行为（对齐 DoubleOptimize）/
                                                     双解析器一致性/缺失文件/自选股链路/
                                                     扫描消费兼容/防 tdx_blk 回归/
                                                     成分股/板块指数2·3/多来源合并
 14. SSE 增量快照 test_sse_incremental.py：增量 klines/MACD ≡ 全量 /
                                                     快照同构/状态缺失回退
 15. SSE 灰度比对      test_sse_gray.py              3b-1：native vs 冻结基线
                                                     （①类型序列 ②剥离时间戳结构 ③总数）
 16. 阶段 5 成果防护   test_phase5_guards.py         获取侧抽象完善：tdxhy 迁 App/统一加载/
                                                     元数据接口提升/
                                                     AppOrch 委托/fetch_kline 抽象
 17. 阶段 6 成果防护   test_phase6_guards.py         前端组件化：组件区块/KLineChart 契约/
                                                     window API 面冻结/事件引用/零构建/
                                                     注册表一致/缓存击穿/合并层完整
 18. 阶段 7 成果防护   test_phase7_guards.py         批量扫描异步化：ScanStore 分层缓存/
                                                     ScanPool ProcessPool 编排/AppOrch 薄封装/
                                                     双路径 API/前端三模式接入/依赖方向
 19. API 集成测试 test_api_integration.py ①：TestClient 起 app 打核心端点/
                                                     健康检查/搜索/扫描守卫/领域异常映射
 20. 代码输入链路守护  test_code_resolution_guards.py 沪深重名消歧/大小写契约/搜索双市场候选/
                                                     search 与引擎同源兜底
 21. SSE 多连接并发 test_sse_concurrent.py ②：8 连接并发隔离/事件序列一致
 22. 扫描池失败收敛 test_scanpool_fallback.py ③：装配/派发失败收敛为任务
                                                     error + 坏池自愈（无线程降级）
 23. 前端 JS 冒烟 test_frontend_smoke.py ④：HTML 骨架/JS 语法/组件注册/事件引用
 24. 锁 v5 守护        test_lock_v5_guards.py        2026-08 锁收敛：线程局部注入隔离 /
                                                     复盘标志线程局部 / AppData 锁覆盖 /
                                                     原子写 / 已删符号防回潮
 25. CChan 数据隔离    test_chan_data_isolation.py   8 线程×3 轮并发建链 ≡ 串行基线
 26. 数据隔离对照      test_chan_data_isolation_control.py
                                                     确定性交错：证明类变量注入串数据、
                                                     线程局部注入隔离（非空测试的自检）
 27. 锁覆盖完整性      test_lock_completeness.py      三形态静态扫描：实例字段 / 模块级
                                                     别名 / 跨模块守卫方法 + 扫描器自证
29. 扫描候选归一化 test_scan_pageindex_normalize.py 候选路径 page_index 板块
                                                     代码须在进入成分取数层前归一化
                                                     （spy + 会话 + 静态回潮三防）
 30. 成交额/量类MACD test_vol_macd_mode.py 设置项入抽屉/数值 ≡ 后端
                                                     calculate_macd/预览bar继承/
                                                     真渲染像素对照（浏览器不在位降级 SKIP）
 31. 统计面板字段     test_stats_panel_labels.py 期货品种只显品种键/平均每笔盈利-亏损
                                                     （标签后半截不重复「平均每笔」）/
                                                     期望值带「元/笔」/最大单笔
                                                     盈-亏合一行不带时间/删出场
                                                     原因与曲线口径/行序/金额不
                                                     带正号/总净盈亏过万→万元、
                                                     过百万→百万元（四档真渲染）
 32. 盈亏曲线坐标轴   test_stats_curve_axis.py  纵轴 1/2/5×10^k 刻度（0 只出现一次）
                                                     +**每根刻度带单位**（不过万→元、
                                                     过万→万元、过百万→百万元，
                                                     整条轴只用一档；与面板「总净
                                                     盈亏」共用同一份缩位）/
                                                     网格+纵轴线+横轴固定 3 个
                                                     首/中/尾日期标签（与「市场
                                                     量能」同源，1000 笔也不变）/
                                                     文字不越出画布/坏值不静默
                                                     （node 桩 + 真渲染两层）
 33. 统计指标口径     Trading/Test/test_trade_stats_formulas.py
                                                     胜率分母含平手/期望值恒等式/
                                                     盈亏比与盈利因子可区分/
                                                     一侧为空→最大单笔报 0/
                                                     金额口径 = net_cash（含双边
                                                     手续费，毛盈净亏算亏损笔）
 34. 底部指标区双槽+RSI test_bottom_slots.py 槽位几何不变式（双窗零回归/四段
                                                     铺满/每槽高与槽数无关）/
                                                     RSI 两份后端实现逐点对齐/
                                                     chip 单击循环+双击拦截/
                                                     翻转下 rsiToY 镜像不变式
                                                     （中间档恒 50，非零线 0）
每组件独立子进程执行，超时 300s 按失败终止（防死循环挂死）。

登记在 `CLEAN_CREDENTIALS_COMPONENTS` 的组件（当前 p20 / p60）额外**清空凭据
环境变量后执行** —— 它们只需构造 broker 注入 FakeApi，凭据齐全时却会真的去连
CTP（慢 2.5~6 倍、带真实登录、偶发网络尖峰）。原因与边界见该常量处注释。

用法（在仓库根目录）：
    python Test/run_all.py             # 全量回归（比对冻结基线）
    python Test/run_all.py --update    # 重新冻结全部基线（迁移改动确认后）
    python Test/run_all.py --report out.json   # 额外落盘机器可读报告
"""
import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime

TEST_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(TEST_DIR)


def _safe_relpath(path, base):
    """os.path.relpath 在 Windows 跨盘（venv 在 D:、仓库在 C:）时抛 ValueError。

    门禁报告里只是「显示命令」用，跨盘时退回绝对路径即可，不让整个门禁崩在
    排版阶段（此前 venv 与仓库不同盘时 run_all 直接崩溃）。
    """
    try:
        return os.path.relpath(path, base)
    except ValueError:
        return os.path.abspath(path)


# (组件名, 命令) —— 顺序即执行顺序
COMPONENTS = [
    ("fixtures_integrity",
     [sys.executable, os.path.join("Test", "gen_fixtures.py"), "--check"]),
    # 换行卫生（2026-10-01）：仓库内 .py 不得出现 \r\r\n（读 CRLF 用 newline=""
    #   保留 + 默认文本写回，会让已是 CRLF 的内容再翻一次 → 孤立 \r 被 Python 当
    #   行终止符 → import 时 SyntaxError: expected ':'）；不得 LF/CRLF 混用；
    #   端到端兜底每个 .py 都能 compile 通过。零网络、秒级。
    ("newline_hygiene",
     [sys.executable, os.path.join("Test", "test_newline_hygiene.py")]),
    ("dotenv_secrets_guard",
     [sys.executable, os.path.join("Test", "test_dotenv_secrets_guard.py")]),
    # TDX 安装目录「单一事实源」护栏（零网络、秒级）：路径字面量只许出现在
    #   App/AppConfig.py 的 _default_tdx_install_dir()，消费方一律现读 SSOT，
    #   注释/文档不许复制路径值 —— 防"改目录只改一处"被悄悄破坏成第二个源。
    ("tdx_dir_ssot_guard",
     [sys.executable, os.path.join("Test", "test_tdx_dir_ssot_guard.py")]),
    ("snapshot_regression",
     [sys.executable, os.path.join("Test", "snapshot_runner.py")]),
    ("trigger_step_replay",
     [sys.executable, os.path.join("Test", "test_trigger_step_replay.py")]),
    ("phase2_guards",
     [sys.executable, os.path.join("Test", "test_phase2_guards.py")]),
    ("determinism",
     [sys.executable, os.path.join("Test", "test_determinism.py")]),
    ("industry_mapping",
     [sys.executable, os.path.join("Test", "test_industry_mapping.py")]),
    ("sse_sequence",
     [sys.executable, os.path.join("Test", "test_sse_sequence.py")]),
    ("func_map_sync",
     [sys.executable, os.path.join("Test", "func_map_check.py")]),
    ("phase3_guards",
     [sys.executable, os.path.join("Test", "test_phase3_guards.py")]),
    ("phase4_guards",
     [sys.executable, os.path.join("Test", "test_phase4_guards.py")]),
    ("app_amo_behavior",
     [sys.executable, os.path.join("Test", "test_app_amo.py")]),
    ("stocks_dual_algo",
     [sys.executable, os.path.join("Test", "test_stocks_dual_algo.py")]),
    ("blk_parsing",
     [sys.executable, os.path.join("Test", "test_blk_parsing.py")]),
    ("sse_incremental",
     [sys.executable, os.path.join("Test", "test_sse_incremental.py")]),
    ("phase5_guards",
     [sys.executable, os.path.join("Test", "test_phase5_guards.py")]),
    ("phase6_guards",
     [sys.executable, os.path.join("Test", "test_phase6_guards.py")]),
    ("phase7_guards",
     [sys.executable, os.path.join("Test", "test_phase7_guards.py")]),
    ("sse_gray",
     [sys.executable, os.path.join("Test", "test_sse_gray.py")]),
    ("api_integration",
     [sys.executable, os.path.join("Test", "test_api_integration.py")]),
    ("code_resolution_guards",
     [sys.executable, os.path.join("Test", "test_code_resolution_guards.py")]),
    ("scan_pageindex_normalize",
     [sys.executable, os.path.join("Test", "test_scan_pageindex_normalize.py")]),
    ("lock_v5_guards",
     [sys.executable, os.path.join("Test", "test_lock_v5_guards.py")]),
    ("chan_data_isolation",
     [sys.executable, os.path.join("Test", "test_chan_data_isolation.py")]),
    ("chan_data_isolation_control",
     [sys.executable, os.path.join("Test", "test_chan_data_isolation_control.py")]),
    ("sse_concurrent",
     [sys.executable, os.path.join("Test", "test_sse_concurrent.py")]),
    ("futures_sub_key",
     [sys.executable, os.path.join("Test", "test_futures_sub_key.py")]),
    ("futures_session_binding",
     [sys.executable, os.path.join("Test", "test_futures_session_binding.py")]),
    ("scanpool_fallback",
     [sys.executable, os.path.join("Test", "test_scanpool_fallback.py")]),
    ("frontend_smoke",
     [sys.executable, os.path.join("Test", "test_frontend_smoke.py")]),
    # 补注册：此前 lock_completeness **从未进入过回归套件**——
    # 它扫得出问题，但没人跑它，等于没有（审计发现的覆盖面盲区之一）。
    ("lock_completeness",
     [sys.executable, os.path.join("Test", "test_lock_completeness.py")]),
    # ── 补注册（守护覆盖面补齐）──────────────
    # 这三个用例此前**写完却没进门禁**——与 lock_completeness 同款盲区：
    # 扫得出问题，但没人跑它，等于没有。三者当前均为「通过」态，可直接
    # 接入 CI；语义与门禁一致（失败非 0 退出）。
    ("scan_session_isolation",
     [sys.executable, os.path.join("Test", "test_scan_session_isolation.py")]),
    ("user_store_rmw",
     [sys.executable, os.path.join("Test", "test_user_store_rmw.py")]),
    ("repro_n3_scan_leak",
     [sys.executable, os.path.join("Test", "repro_n3_scan_session_leak.py")]),
    # ── 补注册 · P1 优先级三条守护（缺口）────
    # 这三条都经「变异测试」验证过有效性：把各自防范的缺陷人为塞回去后
    # 均会变红（G11 摘守卫→5 红、G5 丢文件锁→红、G1 去 LRU 上限→2 红），
    # 不是「跑得绿但拦不住」的摆设用例。
    #   G11 期货清理作用域 ——（P0 历史缺陷）此前**零回归拦截**，
    #       守卫一旦被删，一页期转股掐断所有期货页而 CI 全绿。
    #   G5  zxg.blk 并发写盘 —— 登记表写的「进程锁 + OS 文件锁叠加」
    #       从未被验证过；其中**跨进程**维度更是零测试。
    #   G1  股票分析缓存并发 —— 矩阵上那个 ✅ 是推导出来的，
    #       同页快速连点的 miss/hit 混合编排此前无任何用例覆盖。
    ("futures_cleanup_scope",
     [sys.executable, os.path.join("Test", "test_futures_cleanup_scope.py")]),
    ("zxg_write_concurrency",
     [sys.executable, os.path.join("Test", "test_zxg_write_concurrency.py")]),
    ("analyze_cache_concurrency",
     [sys.executable, os.path.join("Test", "test_analyze_cache_concurrency.py")]),
    # ── P2 守护（审计矩阵 G4 / G7 / G9）──────────────────────────────
    ("futures_selectpoint_concurrency",
     [sys.executable, os.path.join("Test", "test_futures_selectpoint_concurrency.py")]),
    ("scan_session_intra_concurrency",
     [sys.executable, os.path.join("Test", "test_scan_session_intra_concurrency.py")]),
    ("refresh_interleave_concurrency",
     [sys.executable, os.path.join("Test", "test_refresh_interleave_concurrency.py")]),
    # ── 2026-09-01 P3 守护（审计矩阵 G2 / G3 / G6 / G8 / G10）──────────
    # 五个此前**零用例覆盖**的并发缺口，均经「变异测试」验证有效性：把各自
    # 防范的缺陷人为塞回后均会变红（g2-nocap / g2-rmw / g10-noguard /
    # g10-nopoplock / g6-nosnap / g8-isolate / g3-notomic / g3-filelock
    # 八种回归形态全部被对应守卫抓回，无假绿）。
    #   G2  股票双窗口缓存并发（双窗键限额 + cache_update 原子 RMW + 混合并发）
    #   G10 期货双窗口 SSE 子缠论对象图锁（撕裂读 + 登记表随 pop 回收）
    #   G6  搜索×刷新流交织（names_snapshot 拷贝隔离，防静默串表）
    #   G8  旧客户端扫描会话参数跨号污染（token 隔离 + 回退 legacy）
    #   G3  标注跨进程文件锁（进程内合并无丢失 + 读者永不读撕裂 JSON +
    #       跨进程 OS 文件锁真正串行化 RMW）
    ("dual_cache_concurrency",
     [sys.executable, os.path.join("Test", "test_dual_cache_concurrency.py")]),
    ("futures_subchan_concurrency",
     [sys.executable, os.path.join("Test", "test_futures_subchan_concurrency.py")]),
    ("search_refresh_interleave",
     [sys.executable, os.path.join("Test", "test_search_refresh_interleave.py")]),
    ("legacy_scan_session",
     [sys.executable, os.path.join("Test", "test_legacy_scan_session.py")]),
    ("annotation_filelock",
     [sys.executable, os.path.join("Test", "test_annotation_filelock.py")]),
    # P3 变异门禁：把「守卫能抓回退」这一性质本身纳入 CI——每次回归先验证
    # 上述守卫不是「跑得绿但拦不住」的假绿（八种回归形态须全部使守卫变红）。
    ("p3_mutation_gate",
     [sys.executable, os.path.join("Test", "_mutate_p3.py")]),
    # 15m 周期 + 30m 分桶回归：覆盖 15m 合成、港股30m 锚点分桶、单事实源、
    # 双窗口一致性等。原先漏登记，等于没有该回归（详见 15m 对比评审）。
    ("15m_period",
     [sys.executable, os.path.join("Test", "test_15m_period.py")]),
    # 股票 K 线回看窗口截断：`_analyze_stock_internal` 的两条同型分支（复盘 /
    # 冷启动）此前在门禁内零执行、零断言——快照与回放入口为隔离宿主机配置把
    # STOCKS_LOOKBACK_CONFIG 置空（= 不截断），15m 用例只看 keys 不看条数。
    # 本用例自带窗口值，断言「末 N 根」的条数与左右边界，不依赖 AppConfig 默认值。
    ("lookback_truncation",
     [sys.executable, os.path.join("Test", "test_lookback_truncation.py")]),
    # 股票复盘窗口 [L,R] start_time 语义（2026-10-03）：复盘截 [start_time,
    # end_date] 不做根数截断（短窗口不补 / 长区间不截与 lookback_truncation
    # 用例 7 互补）；start>target 与不可解析报错（兜底改严）；复盘不回写
    # 选点 meta、非复盘回写（B 操作现状保护）。
    ("replay_window_start",
     [sys.executable, os.path.join("Test", "test_replay_window_start.py")]),
    # 期货单窗复盘窗口 [L,R]（2026-10-03 二期）：_futures_window_fetch_bars
    # 四分支（默认/选点/复盘/组合=复盘继承选点不做根数截断）；_sse_single_gen
    # start_time 继承（显式/CSV 恢复/倒挂回退）与 meta 恒回显 CSV 真值。
    # MockSource + 业务桩驱动（参考 test_sse_concurrent），全程离线。
    ("futures_replay_window",
     [sys.executable, os.path.join("Test", "test_futures_replay_window.py")]),
    # 股票双窗选点语义（2026-10-03 三期）：配对严格大于校验、sub_start_time
    # 透传链完整性（文本断言）、meta 双字段（sub_saved_selection_date）、
    # isolate 三件套重定向回归（选点写入落临时目录，生产 App/ 零残留）。
    ("stock_dual_window",
     [sys.executable, os.path.join("Test", "test_stock_dual_window.py")]),
    # 漏斗壳形参一致性护栏（2026-10-03 评审 P0-1 防回潮）：AppChart 的
    # call_*/RAW 壳形参 ⊆ 委托实现形参（期货选点 RAW 壳漏 end_date 曾致
    # 期货手动选点 100% 500）；RAW 壳转发断言 + 检测器自证。
    ("funnel_signature",
     [sys.executable, os.path.join("Test", "test_funnel_signature.py")]),
    # 双窗选点/复盘一致性静态护栏（2026-10-04 评审 #2/#4/#11 防回潮）：
    # 期货双窗取消选点豁免已删、上窗选点重连带复盘点 end、路由层删死参数
    # step（引擎内部 step 保留——trigger_step_replay 快照回放依赖）。
    ("dual_window_point_guards",
     [sys.executable, os.path.join("Test", "test_dual_window_point_guards.py")]),
    # PE-TTM 实时层（2026-09 改造）：打开 K 线页面即取数 / single-flight /
    # 失败降级 / 冻结态不联网 / json 只存指数归属 / 旧文件迁移 / 分流单一源。
    # 全程打桩，**不联网**。
    ("pe_ttm_live",
     [sys.executable, os.path.join("Test", "test_pe_ttm_live.py")]),
    ("sina_name_pairing",
     [sys.executable, os.path.join("Test", "test_sina_name_pairing.py")]),
    # 成交额/量「类MACD」显示模式（2026-09-18）：设置项入抽屉 + 数值与后端
    # calculate_macd 逐点对齐（node 抽真实代码段跑真实快照）+ 预览bar继承口径
    # + 真渲染对照（无头 Chrome 截图量像素，浏览器不在位时降级 SKIP）。
    ("vol_macd_mode",
     [sys.executable, os.path.join("Test", "test_vol_macd_mode.py")]),
    # 底部指标区「单窗双槽位 + RSI」（2026-10-09）：槽位几何不变式（双窗零回归 /
    # 四段铺满 / 每槽高与槽数无关）+ RSI 两份**后端**实现逐点对齐 + chip 单击循环与
    # 双击拦截（不落到底下的"恢复全视图"）+ 翻转视图下 rsiToY 的镜像 / 不动点 /
    # 70·30 互换，以及「纵轴中间档恒 50（不是 MACD 的零线 0）」。
    ("bottom_slots",
     [sys.executable, os.path.join("Test", "test_bottom_slots.py")]),
    # 统计面板字段：期货品种只显品种键 / 平均每笔盈利-亏损（后半截不重复）/
    # 期望值数值带「 元/笔」/
    # 最大单笔盈-亏合成一行且不显示时间 / 删「按出场原因」与「曲线口径」/
    # 明细行行序 / 金额一律不带正号 / 总净盈亏过万→万元、过百万→百万元。
    ("stats_panel_labels",
     [sys.executable, os.path.join("Test", "test_stats_panel_labels.py")]),
    # 盈亏曲线坐标轴（2026-09-19）：纵轴 1/2/5×10^k 刻度（0 只出现一次、
    # 刻度互不重复、首尾把数据包住）+ 网格/纵轴线 + 横轴**固定 3 个**首/中/尾
    # 日期标签（取该笔 exit_at，与「市场量能」fmtAxisDate 交叉比对同源；
    # 1000 笔也不变，修「笔数一多变数就变」）+ 文字不许越出画布（修「7 位数负号被裁」）+
    # 坏值不静默。node + canvas 桩跑真实代码段，另有真渲染层。
    ("stats_curve_axis",
     [sys.executable, os.path.join("Test", "test_stats_curve_axis.py")]),
    # 成交统计四项指标口径 + 最大单笔同侧取值（2026-09-19 复核）：
    # 胜率分母含平手 / 期望值 ≡ 总净盈亏÷总笔数 / 盈亏比与盈利因子两口径
    # 可区分 / 无亏损→None / 某侧为空→最大单笔报 0（不再报负数）。
    # [H] 金额口径 = net_cash（**含双边手续费**）：造 gross 与 net 符号相反的
    # 样本，钉「毛盈净亏算亏损笔」；含 AST 契约「_net() 只读 net_cash」。
    ("trade_stats_formulas",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_trade_stats_formulas.py")]),
    # 账本面板展示契约（2026-09-22）：两节标题「持仓」「成交」、
    # 持仓行去止损列、时间 YY/MM/DD HH:MM:SS、成交列表最新排在最后。
    ("docs_anchor_refs",
     [sys.executable, os.path.join("Test", "test_docs_anchor_refs.py")]),
    ("aol_ledger_display",
     [sys.executable, os.path.join("Test", "test_aol_ledger_display.py")]),
    # 报单成交后自动打开账本（2026-10-08 需求 ⑴⑵）：开仓成交 open_filled /
    # 离场成交 close_filled → 幂等打开账本面板，并在面板顶挂一行「刚刚：…」
    # 一次性提示（平仓后面板只剩"空仓"，光看持仓说不出发生了什么）。
    # 护栏要点：只认成交 code（阶段跃迁 / 账单同步不触发）、首次拉取不回放、
    # 面板已开时不被 toggle 关掉、提示行随下次账本刷新失效。5 条变异全拦下。
    ("aol_auto_open_on_fill",
     [sys.executable, os.path.join("Test", "test_aol_auto_open_on_fill.py")]),
    # 0 类买卖点「第5/7笔」㈠ 闪电走势判据（2026-10-04 放宽）：由「相邻回调笔
    # 逐级收窄」改为「各回调笔只与笔1比较」。该分支在 8 个快照回归样本（合成 K 线）
    # 里**零触及** ⇒ 快照全绿证不了这次改动，故补两条护栏：
    #   ① 逻辑层（本组件）：从真实源码抽段 + stub 笔枚举 640 组，钉「宽松 ⊇ 严格」
    #      超集不变量 + 放宽面非空；两种回退形态均已变异自证会被拦下。
    #   ② 端到端（下一条 bs0_ozs57_realdata）：真实行情冻结切片，钉「可达 + 产出
    #      基线 + 换回严格即归零」。
    ("bs0_ozs57_lightning",
     [sys.executable, os.path.join("Test", "test_bs0_ozs57_lightning.py")]),
    # 上一条的端到端补位（2026-10-04 同日）：全 A 股日线两次全量扫描（宽松/严格
    # 各跑一遍 5224 只）证实 57th 在真实行情里被进入 102,854 次、覆盖 5,176 只，
    # 放宽后 496 只多产出 T0，「仅严格有」的 T0 = 0。取判别力最强的 sz002190
    # （57th 产出 T0 宽松 6 / 严格 0）冻成 Test/fixtures_real/ 离线切片，
    # 钉「端到端可达 + 产出与冻结基线一致 + 换回严格口径即归零」。
    ("bs0_ozs57_realdata",
     [sys.executable, os.path.join("Test", "test_bs0_ozs57_realdata.py")]),
    # Trading/README.md 锚点护栏（2026-09-29）：174 处行号引用全量改成稳定锚点，
    #   钉住「零行号 / 锚点可 grep / 反引号配对 / 表格结构」四项，防行号回潮。
    ("readme_anchor_refs",
     [sys.executable, os.path.join("Test", "test_readme_anchor_refs.py")]),
    ("docs_line_refs",
     [sys.executable, os.path.join("Test", "test_docs_line_refs.py")]),
    # 区间套判据改名护栏（2026-10-01）：`check_nested_diver` → `check_nesting_divergence`
    #   （34 处纯标识符替换、零行数漂移）。钉四类契约 —— 类上有新名且形参不变、
    #   定义与调用点同用新名、全仓两个旧拼写零残留（`Docs/` 带版本号的历史快照
    #   刻意豁免但计数可见）、扫描范围没被改窄。写回旧名即红。
    ("nested_divergence_rename",
     [sys.executable, os.path.join("Test", "test_nested_divergence_rename.py")]),
    # 登录链路选择（2026-09-28）：开关先弹「SimNow / 实盘」选择框，选定值以
    #   环境变量注入子进程（三个键名逐键钉死并对 TradingConfig 做真消费验证）；
    #   实盘两个前置（期货公司名 / confirm_live_trading）缺任一即拒绝；
    #   选 SimNow 时闸门判据取**选定值**、不得按配置里的期货公司名误拦；
    #   选项持久化在 state.db（kv ao_link）；点框外 / Esc / 取消 = 不启动。
    ("trade_link_choice",
     [sys.executable, os.path.join("Test", "test_trade_link_choice.py")]),
    # _link_view 配置损坏降级路径（P2 修复：_load_cfg 移入 try，异常走
    # options=[] + error 而非穿透状态轮询）。对照实验：钉死 _load_cfg 抛错，
    # 断言不抛异常且返回降级视图。
    ("link_view_downgrade",
     [sys.executable, os.path.join("Test", "test_link_view_downgrade.py")]),
    # 收尾硬退出看护（2026-09-28）：盘后关闭自动下单时收尾链任一步卡住
    # （主循环退出 / 连接关闭）不再滞留到父进程 150s 强杀 —— 子进程按账户态
    # 分档兜底硬退（RUNNING 长宽限覆盖最坏柜台追价；空仓/锁仓收尾零柜台
    # 交互，主循环退出后短宽限）。钉死分档顺序（在途委托安全）与门控接线。
    ("shutdown_watchdog",
     [sys.executable, os.path.join("Test", "test_shutdown_watchdog.py")]),
    # 出场判定只读收盘价（2026-09-22 口径，p61）：
    #   触发判据不再读本根 high/low（AST 钉死 check() 函数体内不得出现
    #   .high / .low）；"是否达标"只读根内有利极值且必经 `_fav_extreme()`；
    #   止损侧边界一律严格不等（收盘价 == 保护价 → 不离场）；同根内**先抬保护价
    #   再判触发**（故达标根可当根离场）；`only_update` 路径不得带 fill_price。
    ("p61_exit_close_only",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p61_exit_close_only.py")]),
    # 运行态保护价：后端投影 + 前端 K线画线（2026-09-23 p62 / 2026-09-24 改版）：
    #   `auto_order_status()["run"]` 除实时 stop 外给出它的解释（phase / r / tp，
    #   取值来源钉死 `_run_plan.params`）；前端 app.js 的 calcRunSegments 消费
    #   run.segments 构造分段画线状态（2026-09-28 单线升级为分段阶梯线），
    #   drawRunSegments 在主图画分段橙虚线，无运行段不画；移动止盈 toast 与
    #   保本 toast 一样带出保护价。
    #   起因：2026-09-23 实盘 2.6R 浮盈回撤到 0.77R，保护价全程只有悬停 tooltip 一个出口。
    ("p62_ao_protection_price",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p62_ao_protection_price.py")]),
    # 运行态保护价分段历史（2026-09-28）：引擎把本段 run 的每次保护价生效
    # 区间记成一段（初始止损 → 保本 → 初始跟踪 → 每根新高 bar 的移动跟踪），
    # 随 run kv 持久化、投影 run.segments/wlr 下发前端画分段阶梯线 —— 数据源
    # 是引擎实际抬价（入场价=实际成交价），非右键推演的假设口径。
    ("p71_run_segments",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p71_run_segments.py")]),
    # 盈亏比「文档不复述取值」护栏（2026-09-23，p63）：
    #   改某品种盈亏比时不该被迫同步改一堆注释 / README / 测试描述 —— 具体倍数只许写在
    #   档案条目上。判据 = 注释与字符串常量里「语境词（win_loss_ratio / 盈亏比 / L3）
    #   + 档位形态」同行同现；判据是**形态**而非绑死数值 ⇒ 改档位不必改该测试。
    ("p63_wlr_doc_guard",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p63_wlr_doc_guard.py")]),
    # p64：自动下单后台系统通知（页面不在前台 → Win11 右下角 Notification）。
    #   钉三件事：判据函数四纪律（授权态 / hidden||!focused / 点击回前台 /
    #   try/catch）、三通道接线（warn / severe / 成交 toast）+ 权限请求挂在
    #   开启手势上、行为矩阵抽 node 跑真函数（前台+已授权必须 False）。
    ("p64_ao_bg_notify",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p64_ao_bg_notify.py")]),
    # p65：SSE 源停止中断（盘后静默流卡死修复，2026-09-23）。
    #   钉三件事：stop 必须真正调 close（行为级，删 close 段即红）、close 打断
    #   后 read1 的两条收敛路径（返回 EOF / 抛 OSError）都让 events() 秒级退出、
    #   _running=False 结构性不重连。用户态 FakeResp 实现（环境注记见测试 docstring）。
    ("p65_sse_stop_interrupt",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p65_sse_stop_interrupt.py")]),
    # p66：自动下单轮询移 Web Worker（后台通知延迟修复，2026-09-23）。
    #   钉三件事：Worker 源码协议（5s 周期唯一来源 / start 启动即拉一次 /
    #   stop 清定时器 / postMessage 形状）、app.js 接线（Worker + onmessage
    #   → applyAutoOrderStatus + onerror 回退）、行为矩阵抽 node 跑真
    #   Worker 代码（start 即拉 / 周期 5000 / 每 fetch 一条 status / no-store）。
    ("p66_ao_poll_worker",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p66_ao_poll_worker.py")]),
    # p69：托管模式停止 flag 竞态（盘后关闭 150s 强杀修复，2026-09-23）。
    #   钉三件事：托管模式（--managed）子进程跳过清残留 .stop_request
    #   （build_runtime 登录慢窗口内父进程写的是真停止请求）、CLI 直启
    #   保留清残留（防残留 flag 秒退）、AppTrader 命令行传 --managed +
    #   父进程 Popen 前清点不回退。行为级跑真函数 + 源码判据防回潮。
    ("p69_stop_flag_managed",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p69_stop_flag_managed.py")]),
    # p70：停止分档宽限（启动链未就绪短宽限快杀，2026-09-23）。
    #   钉五件事：main.py 完成 build_runtime 原子写 .ready 就绪标志、
    #   AppTrader.stop 未就绪（启动链卡在 tqsdk 同步登录，盘后实测 59s+
    #   无横幅）用短宽限 15s 快杀（启动链中无成交能力，无锁仓风险）、
    #   就绪用完整宽限 150s（覆盖最坏锁仓）、等待中就绪标志出现自动
    #   切换完整宽限、start Popen 前清上一轮遗留 .ready（不清会让
    #   stop 误判已就绪，短宽限失效回 150s）。行为级真 Popen + 源码判据。
    ("p70_stop_ready_flex",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p70_stop_ready_flex.py")]),
    # p6-wiring：P6 成交裁决的**接线级**护栏（2026-09-30 IM 实盘事故根修）。
    #   事故：状态回报流（orders）已宣告全部成交（FINISHED + volume_left=0 +
    #   last_msg=全部成交报单已提交），成交明细流（trades）2 秒后才到，引擎的
    #   同步终判恰好落在两包之间 → 旧 _finalize 把"还没到"读成"没有"，判成
    #   rejected、不落账本，留下无风控锚的孤儿仓（快期3 里却是成交的）。
    #   与 test_p6_fix.py 的分工：那个测**判据函数**，不经过 _finalize；
    #   本组件直接调真实 _finalize，钉住三件事 ——
    #     ① 成交只由**正向证据**宣告（终态文案宣告成交，或明细手数足够），
    #        绝不因**证据缺失**被否证；
    #     ② 判成 filled 的单子 filled_price 必为有限正数（否则引擎
    #        `Engine._book_order` 按 `o.filled_price is None` 又把它当 rejected）；
    #     ③ 09-04 幻影防护不降级（拒单文案 + 残留余量 0 → 仍判未成交）。
    #   真 import 生产代码、零网络、无需凭据。
    ("p6_finalize_wiring",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p6_finalize_wiring.py")]),
    # p6-fallback-consumer：成交价回落的**引擎侧消费点**护栏（2026-10-01 补）。
    #   起因：`meta["price_source"]` 在 2026-09-30 版全仓零消费点 —— 明细晚到时
    #   `_finalize` 用委托限价先落成成交，这笔"限价回填的入场价"无人回头纠正。
    #   补的写入当时**没有任何测试盯着**：把 `if _psrc == "limit":` 改成
    #   `if False:` 跑全套门禁，结果项与基线一字不差（137/139 全同）。
    #   本组件用 dry_run 引擎 + 伪造 Order 走真实 `_execute`，回读 events.jsonl，
    #   钉五件事：① 判成交 + 回落来源 ⇒ 恰一条且字段可对账；② 明细真实价 /
    #   未标来源 ⇒ 零条；③ 拒单 ⇒ 零条（消费点在 rejected 早退之后）；
    #   ④ 历史来源值 `"ref_price"` 已停收；⑤ 事件字典有中文标签。
    #   零网络、无需凭据。
    ("p6_fallback_consumer",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p6_fallback_consumer.py")]),
    # ── 交易域用例（Trading/Test）：引擎 / 品种 / 周期 / 出场 / 统计 ────────
    #    p5~p60 全套 + 引擎与数据源契约。注册前的实测口径见各条目自身
    #    docstring（全部为「0=通过 / 非 0=真坏了」，打桩为主、不联网）。
    ("apptrader_instances",
     [sys.executable, os.path.join("Test", "test_apptrader_instances.py")]),
    # 前端「关闭自动下单」请求契约（静态）：不带 symbol 会退化成停全部
    ("frontend_ao_off_symbol",
     [sys.executable, os.path.join("Test", "test_frontend_ao_off_symbol.py")]),
    # 前端多实例契约（源码抽取 + node 真执行；2026-09-30 评审 P2-1/P2-3/P3-5）：
    #   ① 本页绑定按**品种键**（另一标签页把同品种写成 IF2609/小写主连时不再
    #      丢绑定 → 切合约/切周期守卫不放行）；② 告警与轻提示的水位/冷却
    #      **per 实例**（低 ts 新事件不被高 ts 掩盖后又被 ack 清库）；
    #   ③ 退出弹窗取本页实例的逐实例 log_tail/exit_rc。
    #   不做子串断言（这些词在注释里也有，会恒绿）；node 不在位时自动 SKIP。
    ("ao_multi_instance_frontend",
     [sys.executable, os.path.join("Test",
                                   "test_ao_multi_instance_frontend.py")]),
    # 倒计时（K线区右上角「流逝时间」）显示判据：只有「末根K线是否正在走」才画。
    #   选点 = 改 L 不改 R（需求⑹）：选点态 R 仍是最新、末根就是当前正在走的
    #   K线，倒计时必须照常显示；旧判据用 realtimeStartTime（= 是否设了 start）
    #   代替它，把选点态一并隐藏（2026-10-09「流逝时间不见了」）。
    #   源码抽取 + node 真执行 + 判别力自证（判据改回旧写法必须转红）。
    ("countdown_display",
     [sys.executable, os.path.join("Test", "test_countdown_display.py")]),
    ("apptrader_pid_alive_guards",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_apptrader_pid_alive_guards.py")]),
    ("channel_timing",
     [sys.executable, os.path.join("Trading", "Test", "test_channel_timing.py")]),
    ("engine_config",
     [sys.executable, os.path.join("Trading", "Test", "test_engine_config.py")]),
    ("instrument_spec_ssot",
     [sys.executable, os.path.join("Trading", "Test", "test_instrument_spec_ssot.py")]),
    ("p5_fix",
     [sys.executable, os.path.join("Trading", "Test", "test_p5_fix.py")]),
    ("p6_fix",
     [sys.executable, os.path.join("Trading", "Test", "test_p6_fix.py")]),
    ("p7_pricing",
     [sys.executable, os.path.join("Trading", "Test", "test_p7_pricing.py")]),
    ("p8_layered_exit",
     [sys.executable, os.path.join("Trading", "Test", "test_p8_layered_exit.py")]),
    ("p9_czce_fak",
     [sys.executable, os.path.join("Trading", "Test", "test_p9_czce_fak.py")]),
    ("p10_state_machine",
     [sys.executable, os.path.join("Trading", "Test", "test_p10_state_machine.py")]),
    ("p12_position_book",
     [sys.executable, os.path.join("Trading", "Test", "test_p12_position_book.py")]),
    ("p14a_posbook_cfg",
     [sys.executable, os.path.join("Trading", "Test", "test_p14a_posbook_cfg.py")]),
    ("p15a_open_lots",
     [sys.executable, os.path.join("Trading", "Test", "test_p15a_open_lots.py")]),
    ("p15b_e33_fifo_close",
     [sys.executable, os.path.join("Trading", "Test", "test_p15b_e33_fifo_close.py")]),
    ("p16_phase_f",
     [sys.executable, os.path.join("Trading", "Test", "test_p16_phase_f.py")]),
    ("p17_phase_g",
     [sys.executable, os.path.join("Trading", "Test", "test_p17_phase_g.py")]),
    ("p20_phase_i1",
     [sys.executable, os.path.join("Trading", "Test", "test_p20_phase_i1.py")]),
    ("p21_stop_e2e",
     [sys.executable, os.path.join("Trading", "Test", "test_p21_stop_e2e.py")]),
    ("p23_fok",
     [sys.executable, os.path.join("Trading", "Test", "test_p23_fok.py")]),
    ("p24_close_offset",
     [sys.executable, os.path.join("Trading", "Test", "test_p24_close_offset.py")]),
    ("p26_terminology_guard",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p26_terminology_guard.py")]),
    ("p27_account_state_ssot",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p27_account_state_ssot.py")]),
    ("p28_entry_date_invariant",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p28_entry_date_invariant.py")]),
    ("p29_id_uniqueness",
     [sys.executable, os.path.join("Trading", "Test", "test_p29_id_uniqueness.py")]),
    ("p30_shutdown_exit_mode",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p30_shutdown_exit_mode.py")]),
    ("p32_net_exposure_ssot",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p32_net_exposure_ssot.py")]),
    ("p33_state_machine_table",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p33_state_machine_table.py")]),
    ("p34_scene_x_y_equiv",
     [sys.executable, os.path.join("Trading", "Test", "test_p34_scene_x_y_equiv.py")]),
    ("p35_today_never_close",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p35_today_never_close.py")]),
    ("p36_state_db_no_legacy",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p36_state_db_no_legacy.py")]),
    ("p37_fok_fullcancel_partial",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p37_fok_fullcancel_partial.py")]),
    ("p38_close_hits_yesterday_only",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p38_close_hits_yesterday_only.py")]),
    ("p39_alert_queue_ack",
     [sys.executable, os.path.join("Trading", "Test", "test_p39_alert_queue_ack.py")]),
    ("p41_intent_offset_two",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p41_intent_offset_two.py")]),
    ("p43_audit_fixes",
     [sys.executable, os.path.join("Trading", "Test", "test_p43_audit_fixes.py")]),
    ("p44_no_signal_filter",
     [sys.executable, os.path.join("Trading", "Test", "test_p44_no_signal_filter.py")]),
    ("p44_reconcile_alert",
     [sys.executable, os.path.join("Trading", "Test", "test_p44_reconcile_alert.py")]),
    ("p45_metal_products",
     [sys.executable, os.path.join("Trading", "Test", "test_p45_metal_products.py")]),
    ("p46_pta_product",
     [sys.executable, os.path.join("Trading", "Test", "test_p46_pta_product.py")]),
    ("p47_product_case_whitelist",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p47_product_case_whitelist.py")]),
    ("p49_spec_drift",
     [sys.executable, os.path.join("Trading", "Test", "test_p49_spec_drift.py")]),
    ("p50_review_fixes",
     [sys.executable, os.path.join("Trading", "Test", "test_p50_review_fixes.py")]),
    ("p51_closetoday_switch",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p51_closetoday_switch.py")]),
    ("p52_run_accounting",
     [sys.executable, os.path.join("Trading", "Test", "test_p52_run_accounting.py")]),
    ("p53_delivery_guard",
     [sys.executable, os.path.join("Trading", "Test", "test_p53_delivery_guard.py")]),
    ("p54_trade_stats_merge",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p54_trade_stats_merge.py")]),
    ("p55_product_ssot",
     [sys.executable, os.path.join("Trading", "Test", "test_p55_product_ssot.py")]),
    ("p56_exchange_today_exit",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p56_exchange_today_exit.py")]),
    ("p57_trades_product_key",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p57_trades_product_key.py")]),
    ("p58_handover_items",
     [sys.executable, os.path.join("Trading", "Test", "test_p58_handover_items.py")]),
    ("p59_bsp_type_filter",
     [sys.executable, os.path.join("Trading", "Test", "test_p59_bsp_type_filter.py")]),
    ("p60_trade_toasts_reconcile_gap",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p60_trade_toasts_reconcile_gap.py")]),
    ("period_consistency",
     [sys.executable, os.path.join("Trading", "Test", "test_period_consistency.py")]),
    ("period_matrix",
     [sys.executable, os.path.join("Trading", "Test", "test_period_matrix.py")]),
    ("period_profile",
     [sys.executable, os.path.join("Trading", "Test", "test_period_profile.py")]),
    ("product_fee_table",
     [sys.executable, os.path.join("Trading", "Test", "test_product_fee_table.py")]),
    ("simnow_guards",
     [sys.executable, os.path.join("Trading", "Test", "test_simnow_guards.py")]),
    ("source_reconnect",
     [sys.executable, os.path.join("Trading", "Test", "test_source_reconnect.py")]),
    ("step2_config_ssot",
     [sys.executable, os.path.join("Trading", "Test", "test_step2_config_ssot.py")]),
    ("step2_smoke_freq",
     [sys.executable, os.path.join("Trading", "Test", "test_step2_smoke_freq.py")]),
    ("trade_stats_ratio_naming",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_trade_stats_ratio_naming.py")]),

    # ── Test/ 下的补充用例 ──────────────────────────────────────────
    ("repro_n2_bare_property",
     [sys.executable, os.path.join("Test", "repro_n2_bare_property.py")]),
    ("lock_v6_fixes",
     [sys.executable, os.path.join("Test", "test_lock_v6_fixes.py")]),
    # 守护本入口自身的「按组件清凭据」机制：两份 CREDENTIAL_ENV_KEYS 不许漂移、
    # 登记表里的名字必须真实存在于 COMPONENTS、清凭据只作用于登记组件。
    ("gate_credential_isolation",
     [sys.executable, os.path.join("Test",
                                   "test_gate_credential_isolation.py")]),
    # 守护收割线程的「结果缺口补齐」：worker 落库失败被它自己吞掉 → future 不抛
    # 异常 → 只挂 future 异常的兜底不会触发，completed 停在 total 之下、该票
    # 连跳过汇总都不进（静默少一只）。故障注入驱动（假 future + 真实临时库）。
    ("scanpool_result_gap",
     [sys.executable, os.path.join("Test", "test_scanpool_result_gap.py")]),
    ("stock_tpsl",
     [sys.executable, os.path.join("Test", "test_stock_tpsl.py")]),
    # 全A股扫描来源 + 流通市值过滤阈值随请求传入（2026-10-02）：
    # vipdoc 个股段过滤 / 阈值 0 关闭取数 / 自选股纳入过滤，全程打桩不联网。
    ("scan_all_a_source",
     [sys.executable, os.path.join("Test", "test_scan_all_a_source.py")]),
    # 成交统计「买卖点类型过滤联动」契约（2026-09-26 需求③）：纯单元/集成，
    # 无凭据无网络，此前漏登记（discover 能扫到、门禁没跑）——补登，消除孤儿。
    ("trade_stats_bsp_filter",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_trade_stats_bsp_filter.py")]),
    # Gate ⑦ (2026-10-02)：两测试入口覆盖一致性钉死（无孤儿、无悬空登记）。
    ("gate_component_count",
     [sys.executable, os.path.join("Test", "test_gate_component_count.py")]),

    # ── Backtest/ 回测包（P0）──────────────────────────────────────
    # 裸眼可读的因果：下面是「回测内核的端到端冻结快照」
    #   → 「它依赖的两个口径源（缠论配置 / 出场参数）与页面不等」
    #   → 「它不许依赖上层（App / Frontend / Test/）」
    #   → 「它与页面在 bsp 组装上的投影必须同键同值」。
    # 顺序即依赖顺序：先证内核自己稳，再证它与页面的接缝。
    #
    # 端到端冻结快照（P0 判据①②③）：sz002190 日线 1393 根，逐笔 + 汇总
    # + 类型过滤两场景（only-0 / only-3）与 Backtest/Test/snapshots/ 基线一致。
    ("bt01_p0_fixture",
     [sys.executable, os.path.join("Backtest", "Test", "test_bt01_p0_fixture.py")]),
    # 缠论配置契约（P0-2 方案 B）：default_chan_config() ≡ App.AppUtils._make_chan_config()
    # 逐字段，外加 kl_type_of / date_fmt_of 与 Common 侧 SSOT 同源。
    ("bt02_config_contract",
     [sys.executable, os.path.join("Backtest", "Test", "test_bt02_config_contract.py")]),
    # 迁移期双源护栏（§5.9 #1，v1.15 裁定"护栏式"）：
    # App.AppTPSL._STOCK_EXIT_OVERRIDES ≡ Backtest.ExitParams.STOCK_EXIT_PARAMS。
    # TPSL 删除后该用例自动 SKIP 退役（模块不在 = 预期终态；符号被改名 = 判红）。
    ("bt03_exit_params_contract",
     [sys.executable, os.path.join("Backtest", "Test",
                                   "test_bt03_exit_params_contract.py")]),
    # 分层单向依赖（§5.1 / §5.9 R31）：Backtest/ **生产代码**零 App / Frontend /
    # Test/ 依赖 + 全部本地依赖 ∈ 层表白名单（AST 扫描，Backtest/Test/ 豁免）。
    ("bt04_no_app_import",
     [sys.executable, os.path.join("Backtest", "Test",
                                   "test_bt04_no_app_import.py")]),
    # bsp 组装常驻护栏（§4.5 / §5.9 P1-3）：App 侧 3 处投影（AppEngine ×2 /
    # AppSSE ×1）键集 ≡ Backtest.bsp_to_dict；股票侧两处**当场 eval 页面表达式**
    # 做逐键值比对（改页面表达式护栏自动跟着变，不需要人同步）。
    ("bt05_bsp_dict",
     [sys.executable, os.path.join("Backtest", "Test", "test_bt05_bsp_dict.py")]),
    # 逐周期冻结快照（P0 判据④）：Test/fixtures_real/ 五份切片（d/w/30m/15m/5m）
    # 各跑一遍，钉「每份切片的 sha256 ≡ manifest」+「逐周期 bars/信号/逐笔/汇总
    # ≡ 基线」+「放行信号数 == 开仓笔数」+「signals_seen == 引擎侧 bsp_iter() 总数」
    # （独立交叉验证）+「周线 0 笔的判别力自证」。切片重冻用 `--freeze`。
    ("bt06_periods_fixture",
     [sys.executable, os.path.join("Backtest", "Test",
                                   "test_bt06_periods_fixture.py")]),
    # P4 漂移巡检常驻化（设计文档 §2.6 / §6 P4）：两个一次性探针（`sb/probe_repaint.py`
    # / `sb/probe_r_drift.py`）转正 —— 逐帧推进 d/30m 两份切片，钉「零重绘（信号出现
    # 后不消失/不复活）」「笔指纹改写清单」「R 漂移清单（ΔA 单向 >0）」「回测 signals_seen
    # == 引擎侧 ever_seen」「漂移笔与回测笔同日同类型对齐」+ 判别力自证（0 < 漂移数 < 总数）。
    # 基线 `Backtest/Test/snapshots/p4_repaint_drift.json`，重冻用 `--freeze`。
    ("bt07_repaint_drift",
     [sys.executable, os.path.join("Backtest", "Test",
                                   "test_bt07_repaint_drift.py")]),
    # P1 A/B 对照（设计文档 §2.7 / §6 P1）：0 类点 57th ㈠ 段「严格闸（相邻回调笔
    # 逐级收窄）vs 放宽（各回调笔极值不越过笔1）」在**交易层**（状态机去重后）的差异
    # —— 比**交易笔数与期望 R**，不是比信号数（§2.7：8 信号只对应 3 笔，按信号数会高估）。
    # 含判别力对照臂（只放行 3 类时两臂逐笔全等 ⇒ 改动边界只落 0 类）。
    # 基线 `Backtest/Test/snapshots/p1_ab_57th.json`，重冻用 `--freeze`。
    ("bt08_ab_57th",
     [sys.executable, os.path.join("Backtest", "Test",
                                   "test_bt08_ab_57th.py")]),
    # 出场原因显示文案 + 未平仓浮动估值（v1.18，设计文档 §6.1 ⑥）：文案**只有一份来源**
    # （`Backtest/Report.py::exit_reason_labels`，控制台 / App 响应 / 前端共用），
    # 三条名称是**静态词**（止损 / 保本 / 跟踪止盈，2026-10-06 起名称里不带数字），
    # 带配置数字的解释归 `exit_reason_legend`；未平仓笔的
    # `unrealized_*` 与已实现字段**槽位隔离**（填错槽位等于把没平的仓位算进胜率分母）。
    # 基线 `Backtest/Test/snapshots/p0_display_labels.json`，重冻用 `--freeze`。
    ("bt09_display_labels",
     [sys.executable, os.path.join("Backtest", "Test",
                                   "test_bt09_display_labels.py")]),
    # 两态状态机 + 「唯一改写点」护栏（v1.19）：`State.next_state()` 的真值表穷举
    # （4 合法 + 4 非法）、`Runner.py` 的状态**只能**经它改（AST 断言 `state = State.X`
    # 仅 1 处、`next_state(` 恰 2 处）+ 判别力自证。起因见 v1.18 审核结论 §4.2：
    # docstring 写"唯一改写点"而实际零调用 = 三个 fail-fast 分支从没跑过。
    ("bt10_state_machine",
     [sys.executable, os.path.join("Backtest", "Test",
                                   "test_bt10_state_machine.py")]),
    # `[L, R]` 区间裁切的**粒度**（v1.20）：端点只给日期 ⇒ 含整天（L 补 00:00:00 /
    #   R 补 23:59:59）；给时刻 ⇒ 精确到该时刻。`_slice_records` 旧实现两端都 `[:10]`
    #   截到日 ⇒ `--to 2026-09-29 10:00:00` 与 `--to 2026-09-29` 同样本，而报告
    #   口径行照传入值印 ⇒ 自述区间 ≠ 真实样本（静默）。含变异自证（换回按日截断
    #   的旧写法，判据必须转红且含「裁切」类失败）。
    ("bt11_slice_boundary",
     [sys.executable, os.path.join("Backtest", "Test",
                                   "test_bt11_slice_boundary.py")]),
    # 股票页「回测」端到端契约（设计文档 §4）：**App 边界**能不能把页面格式的
    # klines（斜杠日期、无 dt）还原成 records —— 与 bt06 共用同一批冻结切片，
    # 故引擎侧与页面侧是**同一份样本**。覆盖响应形状 / 口径披露三项自洽 /
    # bsp_types 四态（None 全放行 vs "" 全过滤）/ *_pct ×100 / signals 三分解
    # 不变量 / 400 穷举 / AST「不落盘不联网」(Q8) / 前端抽真函数（按钮文案三态、
    # 双窗禁用、市场态切换收面板、首同步不关、toggleStats 分流）。
    ("stock_backtest",
     [sys.executable, os.path.join("Test", "test_stock_backtest.py")]),
    # 回测扫描（扫描模式 "backtest"）：对「扫描来源 × 扫描周期」逐票跑一遍与
    # 单页回测**同一套内核**，结果按期望值(%/笔)降序。三段守护 ——
    #   ① worker 层 `scan_one(mode="backtest")` 与 `compute_stock_backtest`
    #      用**同一份 klines** 得逐位相等的笔数/期望值（防"另写一套算法"）；
    #   ② bsp_types 三态（None 全放行 / "" 零成交 / 全选 ≡ None）+ 空 klines
    #      收敛为"跳过"而非炸整批；③ 前端真函数（node 抽段）：期望值降序 +
    #      null 沉底 / 列序 名·码·笔数·期望值 / 仅正期望默认勾选 / 涨红跌绿，
    #      以及**弹窗置灰契约**（回测：最近N根灰，来源与周期**可用**；并附
    #      标注模式全灰作判别力对照）+ bsp_types 全链签名与派发位置。
    ("scan_backtest",
     [sys.executable, os.path.join("Test", "test_scan_backtest_mode.py")]),
    # 连涨扫描（扫描模式 "lianzhang"）：最近 N 根 K 线**逐根收红**（口径 = K 线图
    # 红色 `close > open`，平盘白线不算）—— 不是通达信 UPNDAY（逐根高于前收），
    # 两者会选出不同的票。四段守护 ——
    #   ① 后端判据（合成样本 9 组：全红 / 平盘 / 阴线 / 数据不足 / N=1 / 只看最后
    #      N 根 / 跳空下跌仍命中且涨幅为负 / 零价保护）；② 真实冻结切片（具体窗口
    #      的日期与涨幅期望 + 24 截断点 × N=1..5 与独立复算逐组一致 + 非空转自证）；
    #   ③ 前端真函数（node 抽段）：置灰契约（连涨「最近N根」可用）、切模式自动填 3
    #      （附买卖点模式对照证非恒真）、涨幅降序 /「N连涨」标签 / 涨红跌绿 /
    #      终态口径披露；④ 静态契约（HTML 位置紧跟「放量」、选项数 =7、版本号
    #      v=73、localStorage 白名单、后端判据严格 `>`）。
    #   涨幅基期 = 窗口首根**前收** → 末根收盘（与 K 线图底部十字白框同口径，
    #      非首根开盘；见 test ①「判别力」两条与 ④ 白框同源锚点）。
    ("scan_lianzhang",
     [sys.executable, os.path.join("Test", "test_scan_lianzhang_mode.py")]),
    # 放量扫描（扫描模式 "fangliang"）：最近 N 根内**成交额最大的那一根**严格
    # 大于其前 W 根的最高成交额（W = app_config.SCAN_FANGLIANG_WINDOW_BARS，
    # 默认 120）—— 即「成交额创 W 根新高」。**不是**「最近 N 根天天放量」：
    # N 根里只有最猛的那一根参与比较。四段守护 ——
    #   ① 后端判据（合成样本：命中 / 平量不命中 + 一丝超出即命中的判别力自证 /
    #      巨量落在比较窗口内不命中 / 数据不足 / 最猛那根成交额为 0 / N=1 / 最猛那根收阴）；
    #   ② 真实冻结切片（截断点 × N 与独立复算逐组一致 + 命中项 amount_a/peak_prev
    #      逐组一致 + 非空转自证 + 样本内确有命中窗口）；
    #   ③ 前端真函数（node 抽段）：口径披露行的存在 / 措辞 / 窗口根数取自配置
    #      （SSOT：/api/health 下发；未拉到回落中性措辞、**不硬编码 120**）/
    #      进度期与终态共用一个披露函数 / 空结果态仍给披露；
    #   ④ 静态契约（披露单一来源 / 后端严格 `>` / 配置 SSOT / 版本号 v=73）。
    ("scan_fangliang",
     [sys.executable, os.path.join("Test", "test_scan_fangliang_mode.py")]),
    # 未定义全局名静态护栏（2026-10-08 期货选点 NameError 事故防回潮）：
    #   db6f886 把「定位」段的 `config = _make_chan_config()` 换成
    #   `init_chan_symbol(...)`，下方 Step 4 的 `config=config` 遂成孤立引用
    #   → 选中点即 `NameError: name 'config' is not defined`，选点 100% 失败。
    #   门禁此前**没有「未定义全局名」这一维度**：NameError 只在真跑到那行才抛，
    #   桩驱动/单测都进不到 Step 4；`config` 又是合法标识符，语法检查一律放过。
    #   判据：全仓逐 code object 扫 `LOAD_GLOBAL` ∉（模块级绑定名 ∪ 内建名），空即绿；
    #   另含事故点锚点断言 + 两条检测器自证（含「同名局部不得掩盖」的收集范围棘轮）。
    ("undefined_global_guard",
     [sys.executable, os.path.join("Test", "test_undefined_global_guard.py")]),
    # 死参数防回潮护栏（2026-10-09 移除 cal_macd_metric 的 is_reverse 形参后防复活）：
    #   is_reverse 的唯一消费者 AREA_HALF 家族已于 2026-10-08 移除，Bi.Bi / Seg.Seg
    #   两处函数体再无任何读取点 ⇒ 形参成为死参数；而 17 处调用点仍机械地传
    #   True/False，读者会误以为它影响计算（实际两者结果完全相同）。判据 ——
    #   ① 两处形参集合**恰好** {self, macd_algo}：既防 is_reverse 复活，也防把
    #      macd_algo 一起删过头（反向锚点）；② 全仓 cal_macd_metric 调用点无
    #      is_reverse 关键字传参；③ 纯文本兜底拦「照抄旧签名」的新定义
    #      （AST 只看调用点，管不住新写的 def）；④ 函数体仍消费 macd_algo，
    #      证明判据入口存活；⑤⑥ 检测器双向自证（旧片段必命中 / 干净片段与
    #      说明性注释不误报）。变异实证：复活传参命中 ②、复活形参命中 ①、
    #      删成 (self) 命中 ① 反向锚点。
    ("dead_param_guard",
     [sys.executable, os.path.join("Test", "test_dead_param_guard.py")]),
    # ── 暂不注册（注册即恒红 / 无拦截力，注册了门禁形同虚设）──────────
    #   Test/repro_n4_cleanup_race.py          N4 未修，且脚本只有 return 0
    #                                          （恒通过、无拦截力，须先改成
    #                                          「命中即非 0」再注册）
    #   Test/smoke_phase7.py                   脚本自述「不注册进 run_all」：
    #                                          端到端冒烟已内置于
    #                                          test_phase7_guards ⑧，本工具供
    #                                          交付后手工复核（ProcessPool 端到端）
    #   Trading/Test/smoke_simnow_phase_g.py   需要真实 SimNow 连接，凭据隔离后必红
    # 注：Test/repro_n2_bare_property.py 已于 N2 收口（方案 b）后退出码转 0，
    #     与已注册的 repro_n3 同语义，本次一并注册。
]

# 单组件超时（秒）：防阶段 3 重构引入死循环/长阻塞挂死整个 CI
COMPONENT_TIMEOUT_S = 300

# 会被 Trading/Broker/SimNow.py 当作登录凭据读走的环境变量。
# 与发现式 run_all_tests.py 的同名常量是**同源的两份定义**（两入口互不 import），
# 改动时须两份同步；Test/test_gate_credential_isolation.py 有断言钉住一致性。
CREDENTIAL_ENV_KEYS = (
    "SN_ACCOUNT", "SN_PASSWORD",
    "TQ_ACCOUNT", "TQ_PASSWORD",
    "LIVE_ACCOUNT", "LIVE_PASSWORD",
)

# 「跑本组件前先清空凭据」的组件名集合。
#
# 为什么需要：SimNow.py 的 __init__ 在凭据齐全时会真的 _connect() 登录 CTP。
# 下面这两个用例只想构造 broker 注入 FakeApi、本不需要凭据，但在本机凭据齐全
# 的环境里会走联机分支 —— 实测 (2026-09-23)：p60 4.1s → 21.7s、p20 6.7s → 16.5s，
# 输出尾部多出 tqsdk 的 "task: <Task cancelling ...>"，偶发网络抖动还会把它顶到 173s。
# 清空后走 SimNow.py 自带的「缺少凭据」快路径（不联网、秒返回）。
#
# 与发现式的差别：发现式是**全局**清空（面向全量盘点的默认策略，另有
# --keep-credentials 可关）；门禁只对本表登记的组件清，其余组件保留原环境
# （有些用例要读本机配置）。需要真实凭据的用例不要登记 ——
# 如 Trading/Test/smoke_simnow_phase_g.py，凭据被清必红，它本就不在门禁内。
CLEAN_CREDENTIALS_COMPONENTS = {
    "p20_phase_i1",
    "p60_trade_toasts_reconcile_gap",
}


def run_component(name, cmd, update=False, env=None):
    """执行单个组件，返回记录 dict。超时按失败处理（不无限等待）。

    `name` 在 CLEAN_CREDENTIALS_COMPONENTS 里时，用一份**剔除了凭据键的环境副本**
    执行该组件；调用方那份 env 不做就地修改，后续组件不受影响。
    """
    real_cmd = list(cmd)
    if env is not None and name in CLEAN_CREDENTIALS_COMPONENTS:
        env = {k: v for k, v in env.items() if k not in CREDENTIAL_ENV_KEYS}
    if update and name in ("snapshot_regression", "trigger_step_replay",
                           "phase2_guards", "industry_mapping", "sse_sequence",
                           "func_map_sync", "phase3_guards", "phase4_guards",
                           "phase5_guards", "sse_gray"):
        real_cmd.append("--update")
    t0 = time.time()
    try:
        proc = subprocess.run(
            real_cmd, cwd=REPO_ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace",
            timeout=COMPONENT_TIMEOUT_S)
        ok, exit_code, timed_out = proc.returncode == 0, proc.returncode, False
        out = proc.stdout
    except subprocess.TimeoutExpired as e:
        ok, exit_code, timed_out = False, None, True
        out = (e.stdout or "") + f"\n[TIMEOUT] 组件 {name} 超过 {COMPONENT_TIMEOUT_S}s 被终止"
    elapsed = time.time() - t0
    # 失败组件保留完整输出（上限 2000 行防失控），成功组件仍只留尾部 12 行。
    # ✗ 断言行常出现在输出中部而非结尾，只留尾部会把关键证据截掉
    # （product_fee [3g]、p59 [13e] 都为此多花一轮才定位）。
    _lines = (out or "").strip().splitlines()
    rec = {
        "name": name,
        "cmd": " ".join(_safe_relpath(c, REPO_ROOT) if os.path.isabs(c) else c
                        for c in real_cmd),
        "ok": ok,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_s": round(elapsed, 2),
        "output_tail": _lines[-12:] if ok else _lines[-2000:],
    }
    return rec


def main():
    ap = argparse.ArgumentParser(description="阶段 2.5 回归测试基线统一入口")
    ap.add_argument("--update", action="store_true",
                    help="重新冻结全部基线（迁移改动经人工确认后使用）")
    ap.add_argument("--report", metavar="PATH",
                    help="额外写入机器可读 JSON 报告（默认 Test/report.json）")
    args = ap.parse_args()

    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env.setdefault("PYTHONDONTWRITEBYTECODE", "1")

    print("=" * 64)
    print("阶段 2.5 回归测试基线" + ("（重新冻结模式）" if args.update else ""))
    print(f"仓库: {REPO_ROOT}")
    print(f"时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 64)

    records = []
    for name, cmd in COMPONENTS:
        print(f"\n──── [{len(records) + 1}/{len(COMPONENTS)}] {name} ────")
        if name in CLEAN_CREDENTIALS_COMPONENTS:
            print("(本组件已清空 " + "/".join(CREDENTIAL_ENV_KEYS) + "，走离线快路径)")
        rec = run_component(name, cmd, update=args.update, env=env)
        records.append(rec)
        print("\n".join(rec["output_tail"]))
        print(f"──── {'PASS' if rec['ok'] else 'FAIL'} ({rec['elapsed_s']}s) ────")

    n_ok = sum(1 for r in records if r["ok"])
    n_all = len(records)
    summary = {
        "phase": "7",
        "mode": "update" if args.update else "verify",
        "ran_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "python": sys.version.split()[0],
        "total": n_all,
        "passed": n_ok,
        "failed": n_all - n_ok,
        "components": records,
    }

    report_path = args.report or os.path.join(TEST_DIR, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(summary, f, ensure_ascii=False, indent=1)

    print("\n" + "=" * 64)
    for r in records:
        print(f"  [{'PASS' if r['ok'] else 'FAIL'}] {r['name']:<24} {r['elapsed_s']:>6}s")
    print("=" * 64)
    print(f"结果: {n_ok}/{n_all} 通过 | 报告: {_safe_relpath(report_path, REPO_ROOT)}")
    if args.update:
        print("注意: 基线已重新冻结，请 git diff Test/snapshots/ 逐项审查后提交。")
    return n_ok == n_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
