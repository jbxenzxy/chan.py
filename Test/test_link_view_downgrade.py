"""测试 _link_view 在配置损坏时的降级路径（P2 修复验证）。

背景：原本 `_load_cfg()` 在 `try` 外，配置损坏（.env 未知键 / 类型错 → pydantic
拒绝 → AppError）时异常会穿透 `_link_view`，导致状态轮询每 5s 抛一次，且注释
承诺的 `options=[] + error` 降级不可达。修复后 `_load_cfg()` 落在 `try` 内，
异常走降级路径。

这是"断言修复生效"的对照实验：把 `_load_cfg` 钉死成抛错，调 `_link_view`，
断言 ① 不抛异常、② 返回 `options=[]`、③ 带 `error` 文案。反向再测正常配置
仍能拿到 options（确保修复没把正常路径弄坏）。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from App.AppTrader import AppTrader
from App.AppErrors import AppError

_fails = []


def check(name, cond):
    print(("PASS" if cond else "FAIL") + " " + name)
    if not cond:
        _fails.append(name)


def _broken_load_cfg():
    raise AppError("读取交易网关配置失败（Trading/Config.py）: "
                   "ValidationError: 未知键 TRADING_BROKER_X")


# 把 _load_cfg 钉成抛 AppError（模拟 .env 配置损坏）
AppTrader._load_cfg = staticmethod(_broken_load_cfg)

inst = AppTrader.__new__(AppTrader)
try:
    view = inst._link_view(None, None)
    check("配置损坏时 _link_view 不抛异常（降级而非穿透）", True)
except Exception as e:  # pragma: no cover —— 本就不该到这
    check("配置损坏时 _link_view 不抛异常（降级而非穿透）", False)
    print("   实际抛:", type(e).__name__, e)
else:
    check("降级返回 options=[]", view.get("options") == [])
    check("降级带 error 文案", bool(view.get("error")))
    print("   view=", {k: v for k, v in view.items() if k != "options"})

    # 反向对照：_load_cfg 正常时仍能拿到 options（确保修复没把正常路径弄坏）
    class _Params:
        tq_market = "simnow"
        confirm_live_trading = False

    class _Cfg:
        broker = "simnow"
        broker_params = _Params()

    AppTrader._load_cfg = staticmethod(lambda: _Cfg())
    view2 = inst._link_view(None, None)
    check("配置正常时仍返回 options（非降级）",
          isinstance(view2.get("options"), list) and len(view2["options"]) == 2)
    check("配置正常时无 error", not view2.get("error"))

sys.exit(1 if _fails else 0)
