# -*- coding: utf-8 -*-
"""
DataAPI/TqSdkCSSESource.py —— SSE 数据源抽象
=====================================================================
SSE 流数据源层：tqsdk 仅在本模块（DataAPI 数据源抽象层）可见，
下游（FrontAPI / App/AppSSE）经本模块消费，不直接 import tqsdk。

【职责边界】本模块只承载「数据源面」方法：
  connect / get_kline_serial / wait_update / last_records / append_bar /
  close / close_api / cleanup_records
业务方法（init_chan / extract_snapshot / white_hline / step_load）由
服务层 App/AppSSE.py 的模块级纯业务函数承担：
  _sse.init_chan_symbol / _sse._extract_realtime_snapshot /
  _sse._calc_futures_white_hline / _sse._drain_chan
生成器与业务层按「src 协议 + 服务层业务函数」消费，不触碰 tqsdk。

CSSESource 继承 DataAPI 统一基类 CCommonStockApi，使「面向生成器的
流协议」成为抽象层的合法扩展。流数据源无传统 K 线初始化参数，
故子类以类属性（FREQ_SEC_MAP 等）提供元数据，不用
CCommonStockApi 的 __init__ 既约。

CTqSdkSession 生命周期（天勤约束，必须遵守）：
  - TqApi.close() 必须在 wait_update 返回后、由 wait_update 的调用线程
    调用（close() 检查 _loop.is_running()，运行中抛「不能在协程中调用
    close」）。因此 api.close() 永远只在生成器线程的 finally（close_api
    → api.close()）中执行；
  - 应用级回收（/api/futures_cleanup 与 lifespan 关闭钩子）只设置
    _closed 旗，生成器下一次 wait_update 抛 CSSESourceClosed 干净退出，
    finally 中由生成器线程完成 api.close()；
  - close_all() 作为应用级统一回收入口（设置旗并等待 _api_closed）。

依赖方向：DataAPI ⊆ 数据源抽象层（仅此处可见 tqsdk）；下游（FrontAPI /
App/AppSSE）经本模块与业务函数消费，不再直接 import tqsdk。
锁分类：SELF_CONTAINED（每连接独立 TqApi + CChan，不加引擎锁）。
"""
import threading
import logging

from DataAPI.CommonStockAPI import CCommonStockApi

log = logging.getLogger(__name__)


class CSSESourceClosed(Exception):
    """数据源正常关闭信号（Mock/回放源自然结束；生产 CTqSdkSession 被
    应用级回收（futures_cleanup / lifespan 关闭钩子）时也抛出本信号，
    让 SSE 生成器在 wait_update 处干净退出，而非 error-loop）"""


# ── CTqSdkSession 活跃注册表（应用级 TqApi 生命周期管理，根因修复）──────
# 天勤约束：TqApi.close() 必须在 wait_update 返回后、由 wait_update 的
# 调用线程调用（close() 检查 _loop.is_running()，运行中则抛
# 「不能在协程中调用 close」）。因此 api.close() 永远只在生成器线程
# （wait_update 调用者）的 finally 中执行：
#   - 客户端断开 → GeneratorExit → finally（close → close_api → cleanup）
#     可靠执行，close_api 内 api.close() 串行安全；
#   - 外部主动回收（futures_cleanup / lifespan）只设置 _closed 旗，生成器
#     下一次 wait_update 抛 CSSESourceClosed 干净退出，finally 中由生成器
#     线程完成 api.close()。
# 注册表用于主动回收入口：/api/futures_cleanup 与 lifespan 关闭钩子调用
# close_all() 设置旗并等待 _api_closed。
_ACTIVE_SOURCES = set()
_ACTIVE_SOURCES_LOCK = threading.Lock()


def close_all():
    """关闭所有活跃 CTqSdkSession（幂等；供 futures_cleanup 与 lifespan 关闭钩子调用）

    只设置 _closed 旗通知各生成器线程退出，然后等待各生成器线程在
    finally 中完成 api.close()（_api_closed 置位）。api.close() 由生成器
    线程调用——wait_update 已返回、_loop 已停止，从机制上保证不触发
    「不能在协程中调用 close」。生成器最迟一个 wait_update deadline
    （0.1s）内退出，等待是确定性的。
    """
    with _ACTIVE_SOURCES_LOCK:
        sources = list(_ACTIVE_SOURCES)
    for src in sources:
        try:
            src.close()
        except Exception as exc:  # noqa: BLE001 —— 单个源关闭失败不阻断其余
            log.warning("关闭 CTqSdkSession 异常: %s: %s", type(exc).__name__, exc)
    # 等待各生成器线程完成 api.close()（机制保证 0.1s 内完成）
    for src in sources:
        try:
            src._api_closed.wait()
        except Exception as exc:  # noqa: BLE001
            log.warning("等待 CTqSdkSession 关闭异常: %s: %s", type(exc).__name__, exc)


class CSSESource(CCommonStockApi):
    """SSE 流数据源抽象 · 锁分类 SELF_CONTAINED · 仅「数据源面」

    生产实现 CTqSdkSession：真实天勤连接，每 SSE 连接独立 TqApi + CChan，
    不触碰共享分析缓存（_futures_analysis_cache 仅按下窗键存放供
    /api/dual_zs 读取，启动写入/收尾弹出，无跨连接竞争）。
    测试注入 MockSource：脚本化 K 线序列驱动确定性事件流（见 Test/test_sse_gray.py）。

    只承载数据源面协议（连接 / 行情序列 / wait_update / 生命周期）；
    业务（历史拉取 + chan 分析 / 快照提取 / 白线 / 引擎消耗）由服务层
    App/AppSSE.py 模块级业务函数承担（彻底解耦业务）。

    全部方法为同步阻塞调用——SSE 生成器为同步生成器，由
    StreamingResponse 在线程池中迭代，阻塞调用天然发生在线程内，
    不占事件循环（每连接一条常驻线程）。

    继承 CCommonStockApi 但不用其 __init__ 共约（流会话无传统 K 线
    初始化参数）；其元数据接口以类属性提供，由 CTqSdkSession 代理
    CTqSdkAPI 取值。为规避 abc 抽象实例化约束，四个抽象方法给出
    默认实现（流方法语义上仍以 NotImplementedError 声明数据源接口）。
    """

    def __init__(self):
        """流数据源无传统 K 线初始化参数；不调用 CCommonStockApi.__init__"""
        pass

    def connect(self):
        """建立数据源连接（每 SSE 连接一次）"""
        raise NotImplementedError

    def get_kline_serial(self, symbol, freq_sec):
        """获取实时 K 线序列引用（同 (symbol, freq_sec) 返回同一对象，天勤语义）"""
        raise NotImplementedError

    def wait_update(self, deadline_ns):
        """阻塞等待行情更新（deadline 纳秒壁钟）"""
        raise NotImplementedError

    def last_records(self, code_key):
        """读取最近 N 条已注入记录（去重判断用）"""
        raise NotImplementedError

    def append_bar(self, bar, code_key):
        """向引擎注入一根已完成 K 线"""
        raise NotImplementedError

    # ── 记录缓存操作（统一经 Session 协议访问）──────────────────
    # records↔K线转换统一经 Session 协议访问，CTqSdkAPI 为 Session
    # 内部实现细节，服务层不直调其 set_data / get_data / get_last_n /
    # clear_all_cache。
    def set_data(self, records, symbol=None):
        """注入整段K线记录到共享缓存（供 CChan 数据源读取）"""
        raise NotImplementedError

    def get_data(self, symbol=None, **kwargs):
        """读取已注入的K线记录（副本）"""
        raise NotImplementedError

    def get_last_n(self, n=1, symbol=None):
        """读取最近 N 条已注入记录（去重判断用）"""
        raise NotImplementedError

    def clear_all_cache(self):
        """清空全部K线缓存（期货切股票时调用）"""
        raise NotImplementedError

    def close(self):
        """设置关闭旗，通知生成器线程退出（不直接关闭底层连接）"""
        raise NotImplementedError

    def close_api(self):
        """由生成器线程调用，关闭底层连接（天勤 TqApi）。

        基类默认 no-op（Mock/回放源无真实 api）。CTqSdkSession 覆写为
        api.close()。关键约束：api.close() 必须在 wait_update 返回后、
        由 wait_update 的调用线程调用（天勤 _loop.is_running() 检查），
        因此只能在生成器 finally（wait_update 已返回）中调用。
        """
        pass

    def cleanup_records(self, code_key):
        """清理该连接的 K 线注入缓存（异常自吞并打印）"""
        raise NotImplementedError

    # ── CCommonStockApi 抽象方法默认实现（流会话不需要传统 K 线形态）──
    def get_kl_data(self):
        raise NotImplementedError

    def SetBasciInfo(self):
        pass

    @classmethod
    def do_init(cls):
        pass

    @classmethod
    def do_close(cls):
        pass


class CTqSdkSession(CSSESource):
    """生产数据源：天勤 TqApi 会话（唯一聚焦连接与行情 I/O）

    TqApi 生命周期（应用级管理）：
      - 每 SSE 连接独立 TqApi；实例创建即注册进 _ACTIVE_SOURCES 注册表；
      - close() 只设置 _closed 旗（通知生成器退出），不直接 api.close()；
        api.close() 由生成器线程在 finally 中经 close_api() 调用——天勤
        要求 close 必须在 wait_update 返回后、由 wait_update 的调用线程
        调用（_loop.is_running() 检查），生成器 finally 恰好满足该约束；
      - 回收入口：/api/futures_cleanup（期指切股票）与 lifespan 关闭钩子
        （服务器退出）统一调用 close_all() 设置旗并等待 _api_closed（各
        生成器线程完成 api.close()）。
    """

    def __init__(self):
        self.api = None
        self._serials = {}      # (symbol, freq_sec) → kline serial（天勤同参同对象语义）
        self._closed = False    # 关闭旗：wait_update 检测到后抛 CSSESourceClosed 让生成器干净退出
        self._close_lock = threading.Lock()   # 串行化 api.close()（多源并发关闭场景）
        self._api_closed = threading.Event()  # api.close() 完成（生成器线程置位）
        with _ACTIVE_SOURCES_LOCK:
            _ACTIVE_SOURCES.add(self)

    def connect(self):
        from tqsdk import TqApi, TqAuth
        from DataAPI.TqSdkAPI import TQ_ACCOUNT, TQ_PASSWORD
        self.api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))

    def get_kline_serial(self, symbol, freq_sec):
        key = (symbol, freq_sec)
        if key not in self._serials:
            self._serials[key] = self.api.get_kline_serial(symbol, freq_sec)
        return self._serials[key]

    def fetch_kline(self, symbol, freq_sec=15, num_bars=None,
                    display_key=None, start_time=None):
        """拉取历史 K 线（委托 CTqSdkAPI.fetch_kline，封装底层 api 对象）。
        服务层只消费 src.* 协议，不触碰 src.api 原始对象。"""
        from DataAPI.TqSdkAPI import CTqSdkAPI
        return CTqSdkAPI.fetch_kline(self.api, symbol, freq_sec=freq_sec,
                                     num_bars=num_bars, display_key=display_key,
                                     start_time=start_time)

    def wait_update(self, deadline_ns):
        if self._closed:
            raise CSSESourceClosed()
        return self.api.wait_update(deadline=deadline_ns)

    def last_records(self, code_key):
        from DataAPI.TqSdkAPI import CTqSdkAPI
        return CTqSdkAPI.get_last_n(1, symbol=code_key)

    def append_bar(self, bar, code_key):
        from DataAPI.TqSdkAPI import CTqSdkAPI
        CTqSdkAPI.append_bar(bar, symbol=code_key)

    # ── 记录缓存操作（Session 统一承载 records↔K线转换）──────────
    def set_data(self, records, symbol=None):
        from DataAPI.TqSdkAPI import CTqSdkAPI
        CTqSdkAPI.set_data(records, symbol=symbol)

    def get_data(self, symbol=None, **kwargs):
        from DataAPI.TqSdkAPI import CTqSdkAPI
        return CTqSdkAPI.get_data(symbol=symbol, **kwargs)

    def get_last_n(self, n=1, symbol=None):
        from DataAPI.TqSdkAPI import CTqSdkAPI
        return CTqSdkAPI.get_last_n(n=n, symbol=symbol)

    def clear_all_cache(self):
        from DataAPI.TqSdkAPI import CTqSdkAPI
        CTqSdkAPI.clear_all_cache()

    def close(self):
        """设置关闭旗，通知生成器线程退出（幂等）。

        不在此调用 api.close()：天勤要求 api.close() 必须在 wait_update
        返回后、由 wait_update 的调用线程调用（_loop.is_running() 检查）。
        外部线程（futures_cleanup / lifespan）只设置 _closed 旗，生成器
        下一次 wait_update 抛 CSSESourceClosed 干净退出，finally 中由
        生成器线程调用 close_api() 完成 api.close()——同一线程串行，
        从机制上保证 wait_update 已返回、_loop 已停止。
        """
        self._closed = True
        with _ACTIVE_SOURCES_LOCK:
            _ACTIVE_SOURCES.discard(self)

    def close_api(self):
        """生成器线程调用：关闭天勤 TqApi（wait_update 已返回，_loop 已停止）。

        只在生成器 finally 中调用（生成器线程 = wait_update 调用线程，
        finally 在 wait_update 返回/抛异常后执行，_loop 必然已停止）。
        _close_lock 串行化：多源并发关闭时 api.close() 非线程安全
        （is_closed 检查与关闭之间存在竞态），加锁后第二次调用见
        is_closed() 直接返回。
        """
        try:
            if self.api is not None:
                with self._close_lock:
                    self.api.close()
        except Exception as e:
            log.warning("异常: %s: %s", type(e).__name__, e)
        finally:
            self._api_closed.set()

    def cleanup_records(self, code_key):
        try:
            from DataAPI.TqSdkAPI import CTqSdkAPI
            CTqSdkAPI._records_by_symbol.pop(code_key, None)
        except Exception as e:
            log.warning("异常: %s: %s", type(e).__name__, e)