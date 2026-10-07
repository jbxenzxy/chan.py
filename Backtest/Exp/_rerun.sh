#!/usr/bin/env bash
# 在**页面同源数据**上重跑两条流水线的全部核心脚本，并把产物归档到
# `Backtest/Exp/参照日志/`（stdout 原文）与 `Backtest/Exp/参照结果/`（聚合 json）。
# 两份文档里所有数字都出自这两处 —— **数据口径一改就必须整套重跑本脚本**。
#
# 用法:  bash Backtest/Exp/_rerun.sh              （默认不含联网段）
#        WITH_NET=1 bash Backtest/Exp/_rerun.sh   （额外跑 data_parity / xdxr_parity，需要联网）
#
# 2026-10-07 第 3 版：
#   · 第 2 版补齐了「页面加载窗口」第二刀（选点 + STOCKS_LOOKBACK_CONFIG 根数上限）。
#     窗口化后分钟周期便宜了 12~24 倍，故五个周期**统一 800 只股票池**（原分钟只有 300 只）。
#   · 第 3 版把输出改成**直接落进 参照日志/**，并新增归档与目录自检，
#     避免"跑完还要手工搬文件"导致 参照日志/ 与文档口径脱节（见 README 交付边界）。
# 2026-10-07 第 4 版：
#   · 质量门（旧称"预热"）在 entry/exit 两处都从**线程池**改成**进程池**：
#     它和扫描一样是纯 CPU（读 vipdoc + 前复权 + 重采样），线程池被 GIL 串行化
#     （实测 30m × 60 只：--workers 1 = 12s / --workers 12 = 14s，零收益）。
#     30m × 800 只：串行等效 ~160s → 48s。
#   · `exit/atr_scale.py` 的串行取数循环改成进程池：五周期合计 10 分钟 → 2m39s
#     （输出与改前逐字相同）。
#   · 取值入口收敛到 `tdx_source.recs_src()`（原先 entry/exit 各抄一份）；
#     `signal_quality.py` / `exit_fate.py` 的 `--workers` 参数已删除。
#   整套预计 ~86 分钟 → ~48 分钟。
# 2026-10-07 第 5 版：
#   · 本脚本**从仓库根挪到 `Backtest/Exp/`**（用户要求：根目录少放东西）。
#     因此不再假设自己放在哪一层：`$0` 所在目录就是本实验台，仓库根 = 它的上两级。
#     于是**放在仓库内任何位置都能直接跑**，也不再硬编码 `/d/CChan`。
#   · 仍可用 `CHAN_REPO=` 指定仓库根、`PY=` 指定解释器（用于把本目录整份拷到仓库外跑）。
set -u

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"     # <仓库根>/Backtest/Exp
EXP="$HERE"
REPO="${CHAN_REPO:-$(cd "$HERE/../.." && pwd)}"
PY="${PY:-$REPO/.venv/Scripts/python.exe}"
LOGDIR="$EXP/参照日志"
RESDIR="$EXP/参照结果"

if [ ! -x "$PY" ]; then
    echo "找不到解释器: $PY" >&2
    echo "本脚本按自身位置推仓库根（当前推得 REPO=$REPO）。" >&2
    echo "若把 Backtest/Exp 整份拷到了仓库外，请设 CHAN_REPO=<仓库根> 或直接设 PY=<python>。" >&2
    exit 2
fi

cd "$EXP" || exit 1
mkdir -p "$LOGDIR" "$RESDIR"
export PYTHONIOENCODING=utf-8
export CHAN_REPO="$REPO"

hr() { echo; echo "════════════════ $* ════════════════"; }

hr "入场 · 信号质量 五周期（统一 800 只）"
for f in w d 30m 15m 5m; do
  $PY entry/signal_quality.py --freq $f --limit 800 --horizon 30 --procs 8 \
      --out sq_${f}.json > "$LOGDIR/log_sq_${f}.txt" 2>&1
  echo "  $f rc=$?"
done

hr "入场 · 交叉表 / 被扫率 / 层级触发 / 1类样本外"
$PY entry/tab_cycle.py        > "$LOGDIR/log_tab_cycle.txt" 2>&1;        echo "  tab_cycle rc=$?"
$PY entry/sweep_regret.py     > "$LOGDIR/log_sweep_regret.txt" 2>&1;     echo "  sweep_regret rc=$?"
$PY entry/layer_activation.py > "$LOGDIR/log_layer_activation.txt" 2>&1; echo "  layer_activation rc=$?"
$PY entry/type1_oos.py        > "$LOGDIR/log_type1_oos.txt" 2>&1;        echo "  type1_oos rc=$?"

hr "出场 · 两族分解 五周期（统一 800 只）"
for f in w d 30m 15m 5m; do
  $PY exit/exit_fate.py --freq $f --limit 800 --procs 8 --verify 20 \
      --out fate_${f}.json > "$LOGDIR/log_fate_${f}.txt" 2>&1
  echo "  $f rc=$?"
done

hr "出场 · 保本门槛 A/B"
$PY exit/be_ab.py --freq w --n 800 --limit 800 --procs 8 > "$LOGDIR/log_be_ab.txt" 2>&1
echo "  be_ab rc=$?"

hr "出场 · 跨周期波动标定"
# 期货段要联网（新浪），离线时用 --no-fut。取数循环已改进程池（8 进程，见下）。
$PY exit/atr_scale.py --no-fut > "$LOGDIR/log_atr_scale.txt" 2>&1
echo "  atr_scale rc=$?"

hr "出场 · 固定% vs k×ATR（周K / 日K）"
# 内置跑 w、d 两个周期、5 个方案
$PY exit/ab_atr.py --limit 800 --n 800 --workers 8 > "$LOGDIR/log_ab_atr.txt" 2>&1
echo "  ab_atr rc=$?"

EXPECT=17
if [ -n "${WITH_NET:-}" ]; then
  hr "对照 · 数据同源核验 + xdxr 快照对拍（需要联网）"
  $PY common/data_parity.py > "$LOGDIR/log_data_parity.txt" 2>&1;  echo "  data_parity rc=$?"
  $PY common/xdxr_parity.py > "$LOGDIR/log_xdxr_parity.txt" 2>&1;  echo "  xdxr_parity rc=$?"
  EXPECT=19
else
  echo
  echo "（跳过联网段 data_parity / xdxr_parity —— 需要时用 WITH_NET=1 重跑）"
fi

hr "归档聚合结果 → 参照结果/"
cp -f entry/layer_activation.json "$RESDIR/layer_activation.json"
cp -f entry/sweep_regret.json     "$RESDIR/sweep_regret.json"
cp -f exit/be_ab.json             "$RESDIR/be_ab.json"
cp -f exit/atr_scale.json         "$RESDIR/atr_scale.json"
ls -1 "$RESDIR"

hr "自检：参照日志 应有 $EXPECT 个"
n=$(ls -1 "$LOGDIR" | wc -l)
echo "  实测 $n 个（期望 $EXPECT）"
ls -1 "$LOGDIR"
[ "$n" -eq "$EXPECT" ] || echo "  ⚠ 数量不符 —— 别拿这些日志当结论依据"

hr "全部完成"
echo "留档  : $LOGDIR/ 与 $RESDIR/（这两个目录入库）"
echo "中间产物: entry/sq_*.json、exit/fate_*.json 等按脚本自身目录落盘；"
echo "          建议在 .gitignore 里忽略它们（清单见 README「交付边界」）。"
