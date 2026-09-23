# -*- coding: utf-8 -*-
"""
阶段 2.5：回归测试基线 —— 统一入口
=====================================================================
一条命令跑完整个基线，产出汇总报告。各组件独立子进程执行
（monkeypatch 互不干扰），任一失败即整体退出码非 0（可直接接入
CI / 迁移每阶段的验收门禁）。

组件（按依赖顺序；1~33 为历史阶段组件，其后的分组见 COMPONENTS 内联注释）：
  1. fixtures 完整性   gen_fixtures.py --check      冻结输入未被手改
  2. 核心快照回归      snapshot_runner.py --all     笔/段/中枢/买卖点 7 维度（股票+期货）
  3. trigger_step 回放 test_trigger_step_replay.py  逐步回放收敛一致性
  4. 阶段 2 成果防护   test_phase2_guards.py        配置一致性/异常链路/引擎边界/日期契约
  5. 确定性测试        test_determinism.py          重复调用/跨路径污染/双窗口语义
  6. 行业映射完整性    test_industry_mapping.py     双路径加载不静默降级 + 条目质量
  7. SSE 事件序列      test_sse_sequence.py         首事件/序列/正常关闭（legacy 桥接）
  8. 函数映射同步      func_map_check.py            阶段 2.6：74 函数/57 状态归属
                                                     完备·无幽灵·行号无漂移
  9. 阶段 3 成果防护   test_phase3_guards.py        锁分类/直连清零/路由收敛/墓碑/
                                                     SSE 双实现/分层方向
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

# (组件名, 命令) —— 顺序即执行顺序
COMPONENTS = [
    ("fixtures_integrity",
     [sys.executable, os.path.join("Test", "gen_fixtures.py"), "--check"]),
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
    ("aol_ledger_display",
     [sys.executable, os.path.join("Test", "test_aol_ledger_display.py")]),
    # 出场判定只读收盘价（2026-09-22 口径，p61）：
    #   触发判据不再读本根 high/low（AST 钉死 check() 函数体内不得出现
    #   .high / .low）；"是否达标"只读根内有利极值且必经 `_fav_extreme()`；
    #   止损侧边界一律严格不等（收盘价 == 保护价 → 不离场）；同根内**先抬保护价
    #   再判触发**（故达标根可当根离场）；`only_update` 路径不得带 fill_price。
    ("p61_exit_close_only",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p61_exit_close_only.py")]),
    # 运行态保护价：后端投影 + 前端常驻显示（2026-09-23，p62）：
    #   `auto_order_status()["run"]` 除实时 stop 外给出它的解释（phase / r / tp，
    #   取值来源钉死 `_run_plan.params`）；前端 index.html 有常驻元素、app.js 消费
    #   三项、无运行段时隐藏；移动止盈 toast 与保本 toast 一样带出保护价。
    #   起因：2026-09-23 实盘 2.6R 浮盈回撤到 0.77R，保护价全程只有悬停 tooltip 一个出口。
    ("p62_ao_protection_price",
     [sys.executable, os.path.join("Trading", "Test",
                                   "test_p62_ao_protection_price.py")]),
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
    # ── 交易域用例（Trading/Test）：引擎 / 品种 / 周期 / 出场 / 统计 ────────
    #    p5~p60 全套 + 引擎与数据源契约。注册前的实测口径见各条目自身
    #    docstring（全部为「0=通过 / 非 0=真坏了」，打桩为主、不联网）。
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
    rec = {
        "name": name,
        "cmd": " ".join(os.path.relpath(c, REPO_ROOT) if os.path.isabs(c) else c
                        for c in real_cmd),
        "ok": ok,
        "exit_code": exit_code,
        "timed_out": timed_out,
        "elapsed_s": round(elapsed, 2),
        "output_tail": (out or "").strip().splitlines()[-12:],
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
    print(f"结果: {n_ok}/{n_all} 通过 | 报告: {os.path.relpath(report_path, REPO_ROOT)}")
    if args.update:
        print("注意: 基线已重新冻结，请 git diff Test/snapshots/ 逐项审查后提交。")
    return n_ok == n_all


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
