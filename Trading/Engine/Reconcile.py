# -*- coding: utf-8 -*-
"""
对账（ReconcileMixin）
================================
从 Engine.py 拆出的运维职能，以 Mixin 形式挂回 TradingEngine（方法仍通过 self 调用）：

    _reconcile_position / _reconcile_positions / _reconcile_side
        持仓对账（增强 B）：账本 vs 真实持仓逐边比对，发现漂移时落事件并修正。

设计约束：本文件只依赖 Infra 数据结构与 self 注入的引擎上下文
（positions/broker/store/ev/cfg 等），不反向 import 引擎主体，维持单向依赖。
"""
from __future__ import annotations

from typing import List, Optional

from ..Infra.Records import AccountState, Bar, Order, Position, Side, Trade
from ..Infra.Clock import now_cn

class ReconcileMixin:
    # ---------------- 持仓对账（增强 B） ----------------
    def _reconcile_position(self) -> None:
        """【E3.3 兼容壳】单仓对账。E3.3 起实际逻辑在 _reconcile_positions。

        on_bar 已直接调 _reconcile_positions；保留本函数以防外部旧测试直接调用。
        """
        self._reconcile_positions()

    def _reconcile_positions(self, source: str = "on_bar") -> None:
        """【E3.3 + 】多仓对账——每侧（LONG/SHORT）独立与券商真实持仓比对。

        触发场景：
          · on_bar（默认）：用户在快期3等外部终端手工平仓 / 幽灵持仓 / 账户被改
          · restore（新增）：引擎启动 _restore 后立刻拉一次真实持仓，
            防止"本地 store 有持仓但真实账户已平"造成重启后第一根 bar 误判

        对账策略（每侧独立）：
          · real_vol < 0 或 None → skip（broker 不支持对账，如 dry_run）
          · real_vol == 0 → 清空同侧全部仓位（每笔一事件 position_externally_closed）
          · real_vol < engine_vol → FIFO 部分平最早仓位（按 entry_bar_seq ASC），
            生成 trade.reason='reconcile_external_partial'，但不实际下单（broker 不动）
          · real_vol > engine_vol → 仅告警 position_mismatch（不自动接管未知持仓，
            避免误判用户手动加仓为引擎应跟踪仓位）
          · 全部清空后 → state IDLE；否则保持 EXITING

        入场当根 K 线跳过（仅 on_bar 路径生效，restore 路径不跳）：
          开仓后 tqsdk 持仓同步需要时间，避免误判刚开仓为「已平」。

        source 参数：写入事件 source 字段，便于审计区分触发源。
        """
        fn = getattr(self.broker, "real_position", None)
        if fn is None:
            return

        # 收集每侧仓位（FIFO 排序，便于部分平）
        longs = sorted(self.positions.same_side_positions(Side.LONG),
                       key=lambda p: p.entry_bar_seq)
        shorts = sorted(self.positions.same_side_positions(Side.SHORT),
                       key=lambda p: p.entry_bar_seq)

        # ══════════════════════════════════════════════════════════════
        # 盲区补（2026-09-18）：账本该侧为空 ≠ 柜台该侧无仓。
        #   旧实现 `continue` 整侧跳过 —— 「柜台有量、账本无仓」这个方向
        #   （典型成因：回报滞后被误判拒单、柜台已成交）既不告警也不留痕，
        #   是当日 IF 孤儿仓无人过问的直接原因。现对空侧也拉 real_vol 比对：
        #   real_vol > 0 → severe 告警不接管（延续 2026-09-17 拍板口径）。
        # ══════════════════════════════════════════════════════════════
        all_cleared = True
        for side, side_positions in ((Side.LONG, longs), (Side.SHORT, shorts)):
            if not side_positions:
                self._reconcile_empty_side(side, source, fn)
                continue
            engine_vol = sum(p.volume for p in side_positions)

            # 入场当根 K 线跳过（仅 on_bar 路径生效）：
            # 同侧最早仓位若 bars_held < 1 → 整侧 skip，避免误判刚开仓为「已平」
            # restore 路径：bars_seen 已被 _restore 末尾置 0（首次启动时），
            # 不应拦截首拉对账
            if source == "on_bar":
                min_bars_held = min(self.bars_seen - p.entry_bar_seq
                                    for p in side_positions)
                if min_bars_held < 1:
                    all_cleared = False
                    continue

            try:
                real_vol = fn(side)
            except Exception as e:
                # source="restore" 时 broker.real_position 异常 → 写告警事件
                # 让 _restore 的外层 try/except 也能感知（便于审计/告警）
                if source == "restore":
                    self.ev.write("restore_reconcile_failed",
                                  reason="{}: {}".format(type(e).__name__, e),
                                  side=str(side),
                                  note="broker.real_position 抛异常，按本地 store 启动")
                all_cleared = False
                continue
            if real_vol is None:
                continue

            side_all_cleared = self._reconcile_side(
                side, side_positions, engine_vol, real_vol, source)
            if not side_all_cleared:
                all_cleared = False

        # state：所有仓位都清完 → IDLE
        if not self.positions.is_empty():
            all_cleared = False
        if all_cleared:
            self._persist()
            self._sync_state()
        # ══════════════════════════════════════════════════════════════
        # G4：对账把净敞口判成 0 时，同步结束 run。
        #   典型场景：用户在快期3手工平掉一侧 / 幽灵仓被清除 → 引擎簿被清空
        #   → 净敞口归 0，但 `_run_plan` 还挂在进程里。不收口有两个后果：
        #     ① L1-L3 继续拿一个"没有对应敞口"的风控锚判定，并在 `_run_view`
        #        里合成出一笔虚拟仓单；
        #     ② `_persist` 把这段孤儿 run 写回 state.db → 下次启动
        #        `_restore_run` 读到它 → 与净敞口矛盾 → G2 直接拒绝启动。
        #   即：**这里的漏收口，会变成下一次的启动失败**。
        # ══════════════════════════════════════════════════════════════
        if ((self._run_plan is not None or self._run_side is not None)
                and self.account_state() is not AccountState.RUNNING):
            self.ev.write("run_ended_by_reconcile",
                          net_volume=self.positions.net_volume(),
                          run_side=str(self._run_side), source=source,
                          note="对账后净敞口归零，同步结束 run，避免孤儿风控锚被持久化")
            # 按文档「配套改动」改用 `_run_reset`（清字段、不写
            # 事件）—— 对账清仓没有"一段 run 正常结束"的语义，再写一条 `run_end`
            # 会让运维侧误以为真发生了一次离场（原实现是 `run_ended_by_reconcile`
            # + `run_end` 双事件）。上面那条事件已足够表达"被对账收口"。
            self._run_reset()
            self._persist()
            self._sync_state()
        # ══════════════════════════════════════════════════════════════
        # G4 的**反向边**（补）：对账只清掉**一侧**时，
        # 净敞口会从 0 变成非 0（另一侧留下来变成裸奔敞口）。这一段必须在
        # 上面的 if 块**之外**无条件执行 —— 上面的块只在"净敞口归零"时成立。
        #   不补这条边的后果（实测）：`_run_start` 只在 `_execute` 成交时调用，
        #   对账改簿不经过它 → 净敞口非 0 却没有风控锚 → `_settle_positions`
        #   在 `_run_view() is None` 时静默 return → **L1-L3 失效且无任何告警**。
        # ══════════════════════════════════════════════════════════════
        self._check_run_anchor("持仓对账")

    def _reconcile_empty_side(self, side: Side, source: str, fn) -> None:
        """账本该侧无仓时的对账（盲区补）：柜台该侧有量 → severe 告警不接管。

        P61 撤销「刚成交 120s 时间宽限」（2026-09-18）：该宽限要防的场景
        （自家平仓后持仓回报滞后被误报）拿不出代码/日志证据 —— 平仓路径本就
        有 _verify_yesterday/today_delta 泵到持仓增量确认为止（成功即新鲜），
        real_position 读数前又有 _channel_unstable 的 0.3s 泵兜底；而误报的
        代价只是一次弹窗核对（告警不接管），漏报的代价是孤儿仓无人过问。
        按「不空想防护」口径删除时间宽限：宁可偶尔误报，不可静默漏报。"""
        try:
            real_vol = fn(side)
        except Exception as e:
            if source == "restore":
                self.ev.write("restore_reconcile_failed",
                              reason="{}: {}".format(type(e).__name__, e),
                              side=str(side),
                              note="broker.real_position 抛异常，按本地 store 启动")
            return
        if not real_vol:
            return
        self.alert(
            self.ALERT_SEVERE, "position_mismatch",
            "对账发现不一致：柜台 {side} 持仓 {rv} 手，账本该侧无仓。"
            "多出的持仓不是交易引擎开的（常见成因：报单回报延迟被误判拒单、"
            "柜台已成交），引擎不接管，请人工核对处理。".format(
                side=str(side), rv=real_vol),
            side=str(side), engine_vol=0, real_vol=real_vol, source=source)
        self.ev.write("position_mismatch", side=str(side),
                      engine_vol=0, real_vol=real_vol,
                      reason="real_gt_engine_empty_side", source=source)

    def _reconcile_side(self, side: Side, side_positions: List[Position],
                        engine_vol: int, real_vol: int, source: str) -> bool:
        """【】单侧对账（与 _reconcile_positions 解耦）。

        返回 True 表示该侧已全部清空（real_vol == 0）。
        返回 False 表示：一致 / 部分平后仍有残留 / 告警不接管。
        调用方汇总两侧返回值决定是否 state→IDLE。
        """
        if real_vol > engine_vol:
            # 真实持仓 > 引擎：告警不接管（用户可能在外部手动加仓）。
            # 【2026-09-17 拍板】发现账实不一致 → 弹窗说清、由用户干预；
            # 引擎不接管多出的持仓，只告知事实。
            self.alert(
                self.ALERT_SEVERE, "position_mismatch",
                "对账发现不一致：柜台 {side} 持仓 {rv} 手，账本只有 {ev} 手"
                "（多 {diff} 手）。多出的持仓不是交易引擎开的，引擎不接管，"
                "请人工核对处理。".format(
                    side=str(side), rv=real_vol, ev=engine_vol,
                    diff=real_vol - engine_vol),
                side=str(side), engine_vol=engine_vol, real_vol=real_vol,
                source=source)
            self.ev.write("position_mismatch", side=str(side),
                          engine_vol=engine_vol, real_vol=real_vol,
                          n_engine_positions=len(side_positions),
                          reason="real_gt_engine", source=source)
            return False

        if real_vol == engine_vol:
            # 一致：无需操作
            return False

        # real_vol < engine_vol：部分平或全平（FIFO 顺序）
        n_to_close = engine_vol - real_vol  # 手数差
        close_list: List[Position] = []
        closed_vol = 0
        for pos in side_positions:
            if closed_vol >= n_to_close:
                break
            if pos.volume <= (n_to_close - closed_vol):
                # 整笔平
                close_list.append(pos)
                closed_vol += pos.volume
            else:
                # 部分平（仅取差额手数）：当前 E3.3 不支持仓位内部分平，
                # 保守策略：整笔平（生成 trade.reason='reconcile_external_partial_overflow'）
                # 后续 E3.4 可考虑把单 Position 拆分为多笔（如 entry split）
                # 这里直接整笔平，溢出的 closed_vol 写 warning
                close_list.append(pos)
                closed_vol += pos.volume
                self.ev.write("reconcile_partial_overflow",
                              side=str(side),
                              pos_signal_key=pos.signal_key,
                              pos_volume=pos.volume,
                              closed_so_far=closed_vol,
                              needed=n_to_close,
                              source=source,
                              note="E3.3 不支持仓位内拆分，整笔平代替")

        # 生成 trade + 从 book remove（不实际下单）
        # 【2026-09-17 拍板】柜台持仓比账本少 = 账实不一致（多为手工平仓）。
        # 弹窗先入队（内容如实写"已同步"），删除与补记盈亏随即执行 —— 同轮完成，
        # 不做"等用户确认才同步"的挂起机制。明细见事件流 position_externally_closed。
        self.alert(
            self.ALERT_SEVERE, "reconcile_externally_closed",
            "对账发现不一致：柜台 {side} 持仓 {rv} 手，账本记 {ev} 手（少 {n} 手，"
            "多为柜台手工平仓）。引擎已按 FIFO 从账本删除 {k} 笔、按参考价补记平仓盈亏，"
            "账本已同步为与柜台一致；请知悉，如有异议请人工核对柜台。".format(
                side=str(side), rv=real_vol, ev=engine_vol,
                n=n_to_close, k=len(close_list)),
            side=str(side), engine_vol=engine_vol, real_vol=real_vol,
            n_positions_closed=len(close_list), source=source)
        # 账单同步 toast（需求 ⑷(5)）：与上面的 severe 告警并存 —— 告警是
        # 「需人工核对」的持久提醒，这里是「账本已按柜台修正」的即时播报。
        self.notify(
            "账单已同步：柜台 {side} 持仓 {rv} 手，账本已按柜台修正"
            "（删 {k} 笔，多为柜台手工平仓）".format(
                side=str(side), rv=real_vol, k=len(close_list)),
            code="reconcile_sync")
        for idx, pos in enumerate(close_list):
            # 用最新 bar.close 作为参考 exit_price（无真实成交，仅供 trade 记账）
            ref_price = (self.last_bar.close if self.last_bar else pos.entry_price)
            gross = pos.pnl_points(ref_price)
            # 成本口径与 Engine 的 hard-exit 路径对齐（规则 ⑸）。
            #   原写法直接传全局开关 self.state.closetoday_first（默认 True）→ 恒按
            #   "平今"费率（0.0345%）计，对**跨日单**高估 15 倍；而 Engine.py 那边
            #   是按 entry_date 动态判定 —— 两处成本口径不一致。现改为与 Engine 同源。
            #   二次修正：today 也统一走 engine._current_trading_day()
            #   （交易日口径，含夜盘归属次日），不再自行解析 last_bar.date 自然日 ——
            #   否则夜盘品种上会和 Engine 的判定差一天，成本口径再次分叉。
            _today = self._current_trading_day(self.last_bar)
            _is_today_pos = pos.entry_date >= _today
            # 成本改读**品种档案 Fee 两档**（元口径，state 提供
            #   有效乘数），与 Engine._book_close 同源；closetoday_first 留 spec。
            #   净值 = 毛利（点）× 有效乘数 × 手数 − 成本（元），全程元口径。
            #
            # 档案来源 = `self.state.product`（唯一运行时
            #   对象），不再读 `cfg.product_profile`（**实时**按 cfg 的 symbol 查表）
            #   —— 与 `Engine._book_close` 同源；且 `cost_cash` 已去掉 product 入参，
            #   结构上不可能出现"按 A 品种决策、按 B 品种记账"。
            _closetoday = bool(_is_today_pos and self.state.closetoday_first)
            cost = self.state.cost_cash(pos.entry_price, ref_price,
                                        closetoday=_closetoday, volume=pos.volume)
            net_cash = (gross * self.state.multiplier * pos.volume) - cost
            bars_held = max(0, self.bars_seen - pos.entry_bar_seq)

            self._trade_seq += 1
            t = Trade(
                trade_id="T{:05d}".format(self._trade_seq), symbol=pos.symbol,
                side=pos.side, volume=pos.volume, entry_price=pos.entry_price,
                exit_price=ref_price, entry_at=pos.entry_at, exit_at=now_cn(),
                reason="reconcile_external_partial", gross_points=round(gross, 4),
                cost_cash=round(cost, 4), net_cash=round(net_cash, 2),
                bars_held=bars_held,
                signal_key=pos.signal_key, exit_plan_name=pos.exit_plan.name,
                exit_plan_params=pos.exit_plan.params)
            self.store.save_trade(t)
            # （原 RiskGate.on_trade_closed 当日统计已随五道硬闸门删除。）

            self.positions.remove(pos)
            self.ev.write("position_externally_closed",
                          reason="reconcile_external_partial",
                          side=str(pos.side), symbol=pos.symbol,
                          signal_key=pos.signal_key,
                          exit_price=ref_price,
                          gross_points=t.gross_points,
                          net_cash=t.net_cash,
                          fifo_index=idx, pos_count=len(close_list),
                          source=source)

        # 全部清空（real_vol == 0）：写一笔总结事件
        if real_vol == 0:
            self.ev.write("position_externally_closed_summary",
                          reason="reconcile_real_zero",
                          side=str(side),
                          engine_vol=engine_vol,
                          n_positions=len(side_positions),
                          source=source)
        elif n_to_close > 0:
            self.ev.write("position_externally_closed_summary",
                          reason="reconcile_partial",
                          side=str(side),
                          engine_vol=engine_vol, real_vol=real_vol,
                          closed_vol=closed_vol,
                          n_positions_closed=len(close_list),
                          source=source)

        # 全部清空判定：real_vol==0 ⇒ 该侧 0 持仓 ⇒ True（让 state 走 IDLE）
        return real_vol == 0
