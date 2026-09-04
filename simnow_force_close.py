# -*- coding: utf-8 -*-
"""
simnow_force_close.py — 不依赖 gateway 引擎的独立强制平仓工具（M2b+ 配套）

历史背景：
    M2b 实测发现，gateway 跑过 SSE 实时后，账户可能因 P0/P3/P4 各种原因残留
    非零仓位（比如上轮的 1 手 IF2609 多 @4564.4）。即使 simnow_diag.py 显示
    "账户无持仓"，gateway v5 启动后 tqsdk 重建连接，CTP 重发上轮成交通知，
    position 又变回 1 手。

    之前用 simnow_diag.py --close-all 走市价单，但中金所对市价单限制多
    （实际常被跌停板拒）。本工具改用：
        1. 启动时 wait_update 5 秒让 CTP 持仓同步完毕
        2. 读所有非零持仓（多/空分别处理）
        3. 对每个非零持仓，按对手价让 1 tick 下限价单
        4. 等 30 秒，若未成交则撤单 + 重新让 2 tick + 重试
        5. 最多 3 次重试都失败 → 报错并保留在账户里

用法：
    python simnow_force_close.py                 # 只查询 + 报告
    python simnow_force_close.py --close-all     # 强制平掉所有非零持仓
    python simnow_force_close.py --symbol CFFEX.IF2609  # 只平指定合约

凭据：环境变量 SN_ACCOUNT/SN_PASSWORD/TQ_ACCOUNT/TQ_PASSWORD（GUI 设置）
"""
from __future__ import annotations

import argparse
import logging
import math
import os
import sys
import time

# 沙盒运行：把当前脚本所在目录加进 path，便于本地直接 python simnow_force_close.py
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def _setup_logger() -> logging.Logger:
    log = logging.getLogger("force_close")
    if not log.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter("%(asctime)s - %(levelname)5s - %(message)s",
                                         datefmt="%Y-%m-%d %H:%M:%S"))
        log.addHandler(h)
    log.setLevel(logging.INFO)
    return log


def _env_creds():
    return {
        "sn_account": os.environ.get("SN_ACCOUNT", "").strip(),
        "sn_password": os.environ.get("SN_PASSWORD", "").strip(),
        "tq_account": os.environ.get("TQ_ACCOUNT", "").strip(),
        "tq_password": os.environ.get("TQ_PASSWORD", "").strip(),
    }


def _read_position(api, symbol):
    """读指定合约的多/空总持仓（今+昨），返回 (long_total, short_total)。"""
    try:
        pos = api.get_position(symbol)
    except Exception:
        return None, None
    if pos is None:
        return 0, 0
    long_total = (getattr(pos, "pos_long_today", 0) or 0) \
               + (getattr(pos, "pos_long_his", 0) or 0)
    short_total = (getattr(pos, "pos_short_today", 0) or 0) \
                + (getattr(pos, "pos_short_his", 0) or 0)
    return long_total, short_total


def _read_quote(api, symbol):
    """读合约最新行情，返回 (bid_price, ask_price)，失败返回 (None, None)。"""
    try:
        q = api.get_quote(symbol)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            api.wait_update(deadline=deadline)
            bid = getattr(q, "bid_price1", None)
            ask = getattr(q, "ask_price1", None)
            if bid and ask and bid > 0 and ask > 0:
                return float(bid), float(ask)
            time.sleep(0.1)
    except Exception:
        pass
    return None, None


def _align_tick(price, tick, side):
    """按 tick 对齐价格（防止限价单因价格精度被拒）。"""
    if price is None or tick is None or tick <= 0:
        return price
    return round(round(price / tick) * tick, 4)


def _force_close_one(api, symbol, side_to_close, volume, tick, log, max_retry=3):
    """对 (symbol, side_to_close, volume) 强制平仓。

    side_to_close: "LONG" 表示要平掉多头（用 SELL CLOSE），反之亦然。
    返回 (success: bool, filled_volume: int, message: str)。
    """
    if volume <= 0:
        return True, 0, "无需平仓"

    direction = "SELL" if side_to_close == "LONG" else "BUY"

    for attempt in range(1, max_retry + 1):
        bid, ask = _read_quote(api, symbol)
        if bid is None or ask is None:
            log.warning("[%s] attempt %d: 读行情失败（bid=%s ask=%s），重试",
                        symbol, attempt, bid, ask)
            time.sleep(1.0)
            continue

        # 平多 → 用对手价 ask 让 1 tick；平空 → 用对手价 bid 让 1 tick
        if direction == "SELL":
            ref_price = ask
        else:
            ref_price = bid
        if tick and tick > 0:
            # 让 1 tick 加速成交
            ref_price = ref_price + tick if direction == "BUY" else ref_price - tick
        limit_price = _align_tick(ref_price, tick, direction)

        log.info("[%s] attempt %d/%d: 下限价单 %s CLOSE %d 手 @%s",
                 symbol, attempt, max_retry, direction, volume, limit_price)

        try:
            order = api.insert_order(symbol=symbol, direction=direction,
                                     offset="CLOSE", volume=volume,
                                     limit_price=limit_price)
        except Exception as e:
            log.error("[%s] attempt %d: 下单失败: %s: %s", symbol, attempt,
                      type(e).__name__, e)
            time.sleep(1.0)
            continue

        # 等 30 秒看是否成交
        deadline = time.time() + 30.0
        filled = False
        while time.time() < deadline:
            api.wait_update(deadline=deadline)
            status = getattr(order, "status", "")
            if status == "FINISHED":
                if getattr(order, "volume_left", None) == 0:
                    tp = getattr(order, "trade_price", None)
                    log.info("[%s] attempt %d: 成交! trade_price=%s",
                             symbol, attempt, tp)
                    filled = True
                else:
                    log.warning("[%s] attempt %d: FINISHED 但 volume_left=%s，撤单重试",
                                symbol, attempt, getattr(order, "volume_left", None))
                break
            time.sleep(0.1)

        if not filled:
            log.warning("[%s] attempt %d: 30s 内未成交，撤单重试", symbol, attempt)
            try:
                api.cancel_order(order.order_id)
                api.wait_update(deadline=time.time() + 5)
            except Exception as e:
                log.warning("[%s] 撤单异常: %s", symbol, e)

            # 重试前再等 1 秒
            time.sleep(1.0)
            continue

        # 校验持仓：等 5 秒让 position 同步
        deadline = time.time() + 5.0
        while time.time() < deadline:
            api.wait_update(deadline=deadline)
            long_total, short_total = _read_position(api, symbol)
            cur = long_total if side_to_close == "LONG" else short_total
            if cur == 0 or (cur is not None and cur <= 0):
                return True, volume, "成交+持仓校验通过"
            time.sleep(0.1)
        # 成交但持仓没归零（不应该发生），记录
        log.warning("[%s] attempt %d: 成交但 position=%s 未归零",
                    symbol, attempt,
                    long_total if side_to_close == "LONG" else short_total)
        return True, volume, "成交但 position 校验延迟"

    return False, 0, "3 次重试均未成交"


def main():
    parser = argparse.ArgumentParser(description="SimNow 强制平仓工具（独立运行）")
    parser.add_argument("--close-all", action="store_true",
                        help="强制平掉所有非零持仓（不指定 --close-all 只查询）")
    parser.add_argument("--symbol", default="",
                        help="只处理指定合约（如 CFFEX.IF2609）；不指定则扫描整个账户")
    parser.add_argument("--tick", type=float, default=0.2,
                        help="合约 tick（默认 0.2，对应 IF/IH/IC）")
    parser.add_argument("--dry-run", action="store_true",
                        help="只列出要平的单，不真下（用于预览）")
    args = parser.parse_args()

    log = _setup_logger()

    creds = _env_creds()
    missing = [k for k, v in creds.items() if not v]
    if missing:
        log.error("❌ 缺少凭据环境变量: %s", missing)
        log.error("   请在 GUI 环境变量设置中补齐：SN_ACCOUNT / SN_PASSWORD / "
                  "TQ_ACCOUNT / TQ_PASSWORD")
        return 1

    log.info("[1/4] 登录 SimNow...")
    from tqsdk import TqApi, TqAuth, TqAccount
    api = TqApi(TqAccount("simnow", creds["sn_account"], creds["sn_password"]),
                 auth=TqAuth(creds["tq_account"], creds["tq_password"]))
    log.info("     登录成功")

    # ===== P5: 等 5 秒让 CTP 推送所有未确认持仓同步完毕 =====
    log.info("[2/4] 等待 CTP 持仓同步（5s）...")
    api.wait_update(deadline=time.time() + 5.0)
    api.wait_update(deadline=time.time() + 5.0)

    # 读所有非零持仓
    log.info("[3/4] 扫描账户持仓...")
    try:
        all_pos = api.get_position()
    except Exception as e:
        log.error("get_position 失败: %s: %s", type(e).__name__, e)
        api.close()
        return 1

    plan = []  # [(symbol, side_to_close, volume)]
    if isinstance(all_pos, dict):
        items = all_pos.items()
    else:
        items = [(args.symbol or "?", all_pos)]
    for sym, v in items:
        if v is None:
            continue
        if args.symbol and sym != args.symbol:
            continue
        long_total = (getattr(v, "pos_long_today", 0) or 0) \
                   + (getattr(v, "pos_long_his", 0) or 0)
        short_total = (getattr(v, "pos_short_today", 0) or 0) \
                    + (getattr(v, "pos_short_his", 0) or 0)
        if long_total > 0:
            plan.append((sym, "LONG", long_total))
        if short_total > 0:
            plan.append((sym, "SHORT", short_total))

    if not plan:
        log.info("     ✓ 账户无任何非零持仓，无需平仓")
        api.close()
        return 0

    log.info("     待平仓清单（共 %d 项）：", len(plan))
    for sym, side, vol in plan:
        log.info("       %s %s %d 手", side, sym, vol)

    if args.dry_run:
        log.info("[dry-run] 仅列出清单，不实际下单")
        api.close()
        return 0

    if not args.close_all:
        log.info("[4/4] 仅查询模式，未平仓。如要强制平仓请加 --close-all")
        api.close()
        return 0

    # ===== 实际平仓 =====
    log.info("[4/4] 开始强制平仓...")
    results = []
    for sym, side, vol in plan:
        log.info("--- 平仓: %s %s %d 手 ---", side, sym, vol)
        ok, filled, msg = _force_close_one(api, sym, side, vol, args.tick, log)
        results.append((sym, side, vol, ok, filled, msg))
        if not ok:
            log.error("  ❌ %s %s %d 手 平仓失败: %s", side, sym, vol, msg)
        else:
            log.info("  ✓ %s %s %d 手 平仓成功 (%s)", side, sym, vol, msg)

    # 汇总
    log.info("=" * 60)
    log.info("平仓汇总：")
    fail = [r for r in results if not r[3]]
    if fail:
        log.error("  失败 %d 项：", len(fail))
        for sym, side, vol, ok, filled, msg in fail:
            log.error("    %s %s %d 手 - %s", side, sym, vol, msg)
        api.close()
        return 1
    log.info("  ✓ 全部平仓成功（%d 项）", len(results))
    api.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())