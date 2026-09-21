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
            # 读数不可信 → 整侧跳过（不采纳、不动账本）：
            #   None = **唯一**的不可信信号（`Base.real_position` 契约：未连接 /
            #          通道不稳 / 读数失败 / 形态或取值不可识别，四个成因同值）。
            #   < 0  = 畸形读数（契约只允许"非负 int 或 None"；负数是实现违约，
            #          与 None 同处置）。本层消费的是 Base 级接口，所以边界上仍
            #          逐条挡：往下走会被 `_reconcile_side` 当成"柜台比账本少"，
            #          算出 n_to_close > engine_vol → 把账本该侧整笔删除并补记
            #          虚构平仓盈亏（与证据门要防的破坏同源，只是入口不同）。
            if real_vol is None or real_vol < 0:
                continue
            self._mirror_note(str(side), real_vol, source)

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

    # ── 证据门 + 测量（P64，2026-09-18 14:46 事故）──────────────────
    #   otg 持仓通道整场未同步时镜像恒为 0，旧「柜台少→采纳」把刚开 9 秒的仓
    #   从账本删除、补记虚构盈亏、收掉风控 run（快期3 实际 4 空 2 多）。
    #   规则：镜像**本会话曾读到过 ≥ 账本量**（见过这笔仓）才有资格说它消失；
    #   从未见过 → 读数不可信 → 只告警不采纳。读数变化时落 mirror_snapshot
    #   事件，为 otg 持仓通道的定量测量留数据（登录初读另见 SimNow）。
    def _mirror_note(self, side_key: str, real_vol: int, source: str) -> None:
        """记录本地柜台镜像读数：维护会话最大值 + 变化留痕（测量用）。

        只接受**可信读数**（非负 int）：None = 不可信、负数 = 畸形，两者都不是
        "读数"（0 才是有效的"该侧无仓"）。放进去会污染会话最大值，进而污染
        证据门（`_mirror_max_seen` 是本会话"镜像见过这笔仓"的唯一凭据）。
        """
        if real_vol is None or real_vol < 0:
            return
        seen = getattr(self, "_mirror_max_seen", None)
        if seen is None:
            seen = {}
            self._mirror_max_seen = seen
            self._mirror_last = {}
        last = self._mirror_last
        if real_vol > seen.get(side_key, -1):
            seen[side_key] = real_vol
        if last.get(side_key) != real_vol:
            last[side_key] = real_vol
            self.ev.write("mirror_snapshot", side=side_key,
                          mirror_vol=real_vol, session_max=seen[side_key],
                          source=source,
                          note="本地柜台镜像持仓读数变化留痕（otg 持仓通道测量）")

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
        # 0 = 该侧柜台确实无仓（空侧对账的正常路径，什么都不用做）；
        # None = 读数不可信（唯一信号）、< 0 = 畸形读数 → 同样直接返回：否则会
        # 弹出一条"本地柜台镜像 None/-1 手、账本该侧无仓"的假告警。
        if real_vol is None or real_vol <= 0:
            return
        self._mirror_note(str(side), real_vol, source)
        self.alert(
            self.ALERT_SEVERE, "position_mismatch",
            "对账发现不一致：本地柜台镜像 {side} 持仓 {rv} 手，"
            "账本该侧无仓。"
            "多出的持仓不是交易引擎开的（常见成因：报单回报延迟被误判拒单、"
            "柜台已成交），引擎不接管，请人工核对处理。".format(
                side=str(side), rv=real_vol),
            side=str(side), engine_vol=0, real_vol=real_vol, source=source)
        self.ev.write("position_mismatch", side=str(side),
                      engine_vol=0, real_vol=real_vol,
                      reason="real_gt_engine_empty_side", source=source)

    def _reconcile_side(self, side: Side, side_positions: List[Position],
                        engine_vol: int, real_vol: int, source: str) -> bool:
        """单侧对账（与 _reconcile_positions 解耦）。

        返回 True 表示该侧已全部清空（real_vol == 0）。
        返回 False 表示：一致 / 部分平后仍有残留 / 告警不接管。
        调用方汇总两侧返回值决定是否 state→IDLE。
        """
        # 自守：本函数会**改账本**（删仓 + 补记盈亏），所以不把"读数可信"
        # 只交给调用方保证 —— 契约是"非负 int，不可信 = None"，负数属实现违约的
        # 畸形读数；一旦漏进来，`engine_vol - real_vol` 会大于 engine_vol，
        # 被当成"柜台比账本少"，整侧仓单被删并补记虚构盈亏。
        # 调用方 `_reconcile_positions` 有同名守卫，这里只是纵深防御。
        if real_vol is None or real_vol < 0:
            return False

        if real_vol > engine_vol:
            # 真实持仓 > 引擎：告警不接管（用户可能在外部手动加仓）。
            # 【2026-09-17 拍板】发现账实不一致 → 弹窗说清、由用户干预；
            # 引擎不接管多出的持仓，只告知事实。
            self.alert(
                self.ALERT_SEVERE, "position_mismatch",
                "对账发现不一致：本地柜台镜像 {side} 持仓 {rv} 手，"
                "账本只有 {ev} 手（多 {diff} 手）。多出的持仓不是交易引擎开的，"
                "引擎不接管，请人工核对处理。".format(
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

        # ════════════════════════════════════════════════════════════
        # 证据门（P64，2026-09-18 14:46 事故）：镜像说"仓少了"之前，
        # 必须先证明它见过这笔仓 —— 本会话该侧镜像最大读数 ≥ 账本量。
        # 事故里 otg 持仓通道整场未同步（镜像恒 0，连下单前账户既有的
        # 2 手空都看不见），旧逻辑把 0 当权威，开仓 9 秒后把刚开的仓从账本
        # 删除、按参考价补记虚构盈亏、收掉风控 run。现在：从未见过 → 读数
        # 不可信 → 只告警不采纳。账本保留，L1-L3 照常按交易引擎账本管理；
        # 镜像哪天同步出真实持仓（读到 ≥ 账本量），本门自动放行，恢复采纳。
        # ════════════════════════════════════════════════════════════
        seen_max = getattr(self, "_mirror_max_seen", {}).get(str(side), -1)
        # restore 豁免：跨会话幽灵仓清理是启动对账的存在目的（见
        # _reconcile_positions docstring）—— 账本仓来自上一会话，本会话
        # 不可能积累"镜像曾见过"的证据；启动镜像读数即真值口径，且有
        # SimNow 登录初读日志可审计。证据门只管**会话进行中**的采纳。
        if source != "restore" and seen_max < engine_vol:
            self.alert(
                self.ALERT_SEVERE, "reconcile_mirror_untrusted",
                "本地柜台镜像读数不可信：镜像 {side} 持仓 {rv} 手，账本记 {ev} 手，"
                "但本会话镜像从未读到过 ≥ {ev} 手（otg 持仓通道可能未同步）。"
                "引擎不改账本（不删仓、不补盈亏），持仓仍按交易引擎账本正常风控；"
                "请以快期3 为准人工核对。".format(
                    side=str(side), rv=real_vol, ev=engine_vol),
                side=str(side), engine_vol=engine_vol, real_vol=real_vol,
                mirror_session_max=seen_max, source=source)
            self.ev.write("reconcile_gate_blocked", side=str(side),
                          engine_vol=engine_vol, real_vol=real_vol,
                          mirror_session_max=seen_max, source=source,
                          note="镜像从未见过该仓，「柜台少」不采纳")
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
            "对账发现不一致：本地柜台镜像 {side} 持仓 {rv} 手，"
            "账本记 {ev} 手（少 {n} 手，多为柜台手工平仓）。"
            "引擎已按 FIFO 从账本删除 {k} 笔、按参考价补记平仓盈亏，"
            "账本已同步为与镜像一致；请知悉，如有异议请人工核对快期3。".format(
                side=str(side), rv=real_vol, ev=engine_vol,
                n=n_to_close, k=len(close_list)),
            side=str(side), engine_vol=engine_vol, real_vol=real_vol,
            n_positions_closed=len(close_list), source=source)
        # 账单同步 toast（需求 ⑷(5)）：与上面的 severe 告警并存 —— 告警是
        # 「需人工核对」的持久提醒，这里是「账本已按柜台修正」的即时播报。
        self.notify(
            "账单已同步：本地柜台镜像 {side} 持仓 {rv} 手，账本已按镜像修正"
            "（删 {k} 笔，多为柜台手工平仓）".format(
                side=str(side), rv=real_vol, k=len(close_list)),
            code="reconcile_sync")
        for idx, pos in enumerate(close_list):
            # 用最新 bar.close 作为参考 exit_price（无真实成交，仅供记账）
            ref_price = (self.last_bar.close if self.last_bar else pos.entry_price)
            # 交易日口径（含夜盘归属次日）：run 结算的离场档判定与
            # 下方仓单级成本的平今判定共用一次取值。
            _today = self._current_trading_day(self.last_bar)
            # run 级结算（设计文档 v3.1 §5.1-5）：被删仓单属于在途 run（与 run
            # 同向）→ 以对账参考价**就地**强制结算该 run —— 触发点 = 删除那一刻，
            # 不是两侧循环收尾（两侧同清时先删的可能是 run 侧仓单，延后结算会错
            # 时点/参考价）。锁仓侧仓单删除不触 run（LOCKED 态 run 已随锁仓 fill
            # 收口）。不再保留独立的仓单级补记行（§7-② 拍板）。
            trade_id = ""
            if (self._run_side is not None and pos.side is self._run_side
                    and self.account_state() is AccountState.RUNNING):
                # 离场档按被删仓单自己的建仓日判（与 hard-exit 的
                # today 判定同源：交易日口径，含夜盘归属次日）。
                _exit_offset = ("CLOSETODAY"
                                if (pos.entry_date >= _today
                                    and self.state.closetoday_first)
                                else "CLOSE")
                _t = self._settle_run(exit_price=ref_price,
                                      exit_offset=_exit_offset,
                                      reason="reconcile_external_partial",
                                      at=now_cn(), volume=pos.volume)
                if _t is not None:
                    trade_id = _t.trade_id
            self.positions.remove(pos)
            # 仓单级盈亏快照（审计底稿，§5.1-4）：与 close 事件同源同口径
            # （Engine._write_close_event：pnl_points + cost_cash 动态平今），
            # 回答"被删的这笔仓单自身"的段盈亏 —— 锁仓侧仓单被删时它是
            # 唯一载体（trades 表无行）。与 trade_id 指向的 run 级 Trade
            # （run 锚口径）分层：事件 = 仓单身份，Trade = run 身份，
            # 两者并存不是矛盾。
            _gross = pos.pnl_points(ref_price)
            _closetoday = bool(self.state.closetoday_first
                               and pos.entry_date >= _today)
            _cost = self.state.cost_cash(pos.entry_price, ref_price,
                                         closetoday=_closetoday,
                                         volume=pos.volume)
            _net_cash = (_gross * self.state.multiplier * pos.volume) - _cost
            self.ev.write("position_externally_closed",
                          reason="reconcile_external_partial",
                          side=str(pos.side), symbol=pos.symbol,
                          signal_key=pos.signal_key,
                          exit_price=ref_price,
                          gross_points=round(_gross, 4),
                          net_cash=round(_net_cash, 2),
                          trade_id=trade_id,
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
