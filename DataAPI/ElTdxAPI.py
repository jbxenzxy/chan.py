"""
eltdx 数据源适配器（通达信网络行情客户端封装）。

定位：本模块封装 eltdx 客户端这一个通达信网络数据源，对外暴露 eltdx 的
五类取数能力（均**仅覆盖 A 股**）：
  1. 除权除息（XDXR）——7709 协议 0x000f 事件表，供 TdxAPI 前复权流水线消费；
  2. PE-TTM（滚动市盈率）——7615 F10/TQLEX HTTP 网关 valuation 估值表
     （ReqId=200191），**按代码单只查询**（类似股东增减持；不再走 0x06B9
     全市场统计文件）；
  3. 流通市值——0x0010 财务批量（流通股本）× 0x054c 快照（最新价）；
  4. 股东增减持计划——7615 F10/TQLEX HTTP 网关（按代码查询）；
  5. 板块文件下载 / 刷新——0x06B9 通用文件读取。
PE 单位：倍；流通市值单位：亿元。「按市场 / 标的类型选源」是业务编排规则，
收口在 App 层（App/AppEngine.py 的 _fetch_pe_ttm_live），不在 DataAPI 层。

职责：为使用方提供统一、标准化的 eltdx 取数入口。App 层不直接依赖本模块的
统计取数；本模块是 TdxAPI 前复权流水线与 App 层 PE-TTM 取数共同依赖的
下游数据源。

依赖方向：TdxAPI / App 层 → 本模块（单向）。本模块不反向 import
二者，避免 import 环。

数据源：
  - eltdx（7709 协议）——唯一数据源。
  - 单一数据源是刻意设计：失败即显著报错（见 get_xdxr_data 的 log.error），
    不做静默降级。

**港股不支持**：eltdx 无 hk 通道（codes.all("hk") 抛 ValueError、0x054c
快照对 hk 不可用），故本模块三个入口一律只服务 sh / sz / bj。港股 PE-TTM
仍由 DataAPI/TxAPI.py 提供。
"""
import inspect
import threading
import logging
import time
import warnings

import pandas as pd
from datetime import datetime

log = logging.getLogger(__name__)

_xdxr_lock = threading.Lock()

# xdxr 独立缓存：key=(market, code)，同一股票跨周期不重复拉取
_xdxr_cache = {}

# 失败退避：key=(market, code) → 上次失败时刻(epoch)。
# eltdx 接口失败时不写负缓存（保可重试），但用短 TTL 抑制高频重试与刷屏：
# 同股票的失败在 XDXR_RETRY_TTL 秒内直接快速返回 None，逾期后再重试并重新报错。
XDXR_RETRY_TTL = 60
_xdxr_fail_ttl = {}


# ============================================================
# 列名标准化
# ============================================================
# eltdx 返回的字段本就按标准名构造，这里再做一层标准化兜底；
# col_map 同时保留一批历史别名，使上游字段名变动时无需改调用方。
def _normalize_xdxr_df(df):
    """
    将传入的 DataFrame 列名统一为标准列名。

    date 可由 (year, month, day) 三列合成，也可直接给 date / 除权日 等别名。
    songzhuangu = 送股+转增 合计（每10股），随后拆分到 songgu。
    最终保证输出含 date, category, fenhong, songgu, zhuanzeng, peigu, peigujia。
    """
    if df is None or len(df) == 0:
        return df

    # ── 处理拆分日期 (year, month, day → date) ──
    if 'year' in df.columns and 'month' in df.columns and 'day' in df.columns:
        def _make_date(row):
            try:
                y = int(row['year']); m = int(row['month']); d = int(row['day'])
                return datetime(y, m, d)
            except Exception:
                return None
        df['date'] = df.apply(_make_date, axis=1)
        # 日期列已创建，不再需要 day->date 映射

    # ── 列名映射 ──
    col_map = {
        # 日期列（仅在无 year/month/day 时生效）
        'date': 'date', 'ex_date': 'date', 'datetime': 'date', 'time': 'date',
        'td': 'date', '除权除息日': 'date', '除权日': 'date',
        'ex_dividend_date': 'date', 'trade_date': 'date',
        # 事件类别
        'category': 'category', 'type': 'category', '类别': 'category',
        'event_type': 'category', 'event': 'category',
        # 分红（每10股）
        'fenhong': 'fenhong', 'cash_div': 'fenhong', 'cash': 'fenhong',
        '分红': 'fenhong', 'dividend': 'fenhong', 'div': 'fenhong',
        # 送转股合计（每10股）— eltdx c3_value 即映射到此处
        'songzhuangu': 'songzhuangu',
        # 送股（每10股）
        'songgu': 'songgu', 'bonus_share': 'songgu', '送股': 'songgu',
        'bonus': 'songgu', 'stock_div': 'songgu', 'sg': 'songgu', 'song': 'songgu',
        # 转增（每10股）
        'zhuanzeng': 'zhuanzeng', 'transfer': 'zhuanzeng', '转增': 'zhuanzeng',
        'zhuan': 'zhuanzeng', 'zz': 'zhuanzeng', 'trans': 'zhuanzeng',
        # 配股（每10股）
        'peigu': 'peigu', 'rights_issue': 'peigu', '配股': 'peigu',
        'allotment': 'peigu', 'rights': 'peigu', 'pg': 'peigu',
        # 配股价
        'peigujia': 'peigujia', 'rights_price': 'peigujia', '配股价': 'peigujia',
        'allotment_price': 'peigujia', 'pgj': 'peigujia',
        # 股票代码
        'code': 'code', 'symbol': 'code', '股票代码': 'code',
    }

    rename = {}
    for col in df.columns:
        col_lower = col.lower().strip().replace('_', '').replace(' ', '')
        if col_lower in col_map:
            rename[col] = col_map[col_lower]
        elif col in col_map:
            rename[col] = col_map[col]
        else:
            for key, target in col_map.items():
                if len(key) >= 3 and key in col_lower:
                    rename[col] = target
                    break

    if rename:
        df = df.rename(columns=rename)
        pass  # 列名映射完成

    # ── 统一 songgu/zhuanzeng: songzhuangu 是送转合计，拆到 songgu ──
    if 'songzhuangu' in df.columns:
        # 将 songzhuangu 的值作为 songgu（送转合计），zhuanzeng 留 0
        if 'songgu' not in df.columns:
            df['songgu'] = df['songzhuangu'].fillna(0)
        else:
            df['songgu'] = df['songgu'].fillna(0) + df['songzhuangu'].fillna(0)
        df.drop(columns=['songzhuangu'], inplace=True)

    # 确保必要的列存在
    required_cols = ['date', 'category', 'fenhong', 'songgu', 'zhuanzeng', 'peigu', 'peigujia']
    for col in required_cols:
        if col not in df.columns:
            df[col] = 0

    return df


# ============================================================
# eltdx 除权除息（活动数据源，基于 7709 协议 0x000f 命令）
# ============================================================
# eltdx TdxClient 单例复用：每次新建 TdxClient 都会重新解析 hosts 并可能触发
# 服务器探测（probe_hosts），探测后写排名缓存（persist=True）在 Windows 下常因
# 文件被占用抛 OSError → RuntimeWarning（"unable to persist eltdx server ranking"）。
# 扫描每票都走 xdxr，若每次新建会反复触发该警告。改为模块级单例 + probe_hosts=False：
#   - 单例：连接复用，避免重复探测/重复解析 hosts；
#   - probe_hosts=False：关闭启动探测（探测仅用于选最快服务器，非必需；
#     连接失败时 eltdx 内部仍会按 hosts 顺序重连），从根上消除该警告。
_eltdx_client = None
_eltdx_client_ready = False
# 能力探测 / 告警抑制的一次性开关（见 _supports_on_missing、_quiet_eltdx_missing_warnings）
_ON_MISSING_SUPPORTED = None      # None=未探测，True/False=探测结果
_MISSING_WARN_QUIET = False


def _check_eltdx_api_compat(client):
    """校验当前 eltdx 具备本模块所需的最小接口集（缺一即抛升级指引）。

    两代接口都查：
      - `corporate.capital_changes` —— 前复权必需，3.0.0 起提供；
      - 批量取数方法的 `on_missing` 关键字 —— 缺票跳过必需，**3.3.2 起提供**。
        缺它时 `finance_batch(..., on_missing="skip")` 会落成
        `TypeError: unexpected keyword argument`，提示完全不可读（调用点在
        lambda 里，栈上根本看不出是版本问题），故在入口提前拦。
    签名探测结果进程内缓存，热路径（逐票 xdxr）只多一次字典查表级开销。
    """
    corporate = getattr(client, "corporate", None)
    if corporate is None or not hasattr(corporate, "capital_changes"):
        raise RuntimeError(
            "[eltdx 接口不兼容] 前复权所需的 client.corporate.capital_changes 不存在："
            "当前 eltdx 版本过旧，请升级：pip install -U 'eltdx>=3.3.2'。"
        )
    if not _supports_on_missing(client):
        raise RuntimeError(
            "[eltdx 接口不兼容] 批量取数方法缺少 on_missing 关键字（eltdx 3.3.2 起提供）："
            "旧版本遇到退市 / 停牌票会让整批 80 只一起取数失败。"
            "请升级：pip install -U 'eltdx>=3.3.2'。"
        )
    return client


def _supports_on_missing(client):
    """**两条**批量取数方法是否都支持 on_missing（eltdx 3.3.2+）。结果进程内缓存。

    必须逐条探，不能只探一条：
      · 两条命令分别属于 quotes / corporate 两个 API 对象，签名各自演进；
      · 只探 `get_snapshots` 时，若上游某版本只给其中一条加了参数，另一条会在
        `finance_batch(..., on_missing="skip")` 处抛
        `TypeError: unexpected keyword argument` —— 调用点在 lambda 里，栈上
        完全看不出是版本问题，用户只会看到「取数异常」。
    任一条不支持即判 False，由 `_check_eltdx_api_compat` 统一抛升级指引。
    """
    global _ON_MISSING_SUPPORTED
    if _ON_MISSING_SUPPORTED is None:
        try:
            probes = (client.quotes.get_snapshots, client.corporate.finance_batch)
            _ON_MISSING_SUPPORTED = all(
                "on_missing" in inspect.signature(fn).parameters for fn in probes)
        except (TypeError, ValueError, AttributeError):
            _ON_MISSING_SUPPORTED = False
    return _ON_MISSING_SUPPORTED


def _quiet_eltdx_missing_warnings():
    """把 eltdx 的 `MissingCodesWarning` 压掉，改由 `_log_missing` 记账。

    `on_missing="skip"` 每次命中缺票都 `warnings.warn` 一条，全市场 63 批会刷
    几十行，内容与 `_log_missing` 完全重复。这里装一条**进程级一次性**过滤器。

    这不是「把问题藏起来」：缺票明细由 `_log_missing` 以 log.info（含代码清单、
    条数）与 log.warning（整批一只都没回来）打出，信息量不低于原始 warning。
    用 `filterwarnings` 而不是 `catch_warnings` 是因为后者会在 with 块期间替换
    全局 filters，多线程下不安全；本函数可能在扫描线程中被调用。
    """
    global _MISSING_WARN_QUIET
    if _MISSING_WARN_QUIET:
        return
    try:
        from eltdx.exceptions import MissingCodesWarning
    except Exception:
        return                       # 旧版 eltdx 没有该告警类，无需处理
    warnings.filterwarnings("ignore", category=MissingCodesWarning)
    _MISSING_WARN_QUIET = True


def _ensure_eltdx_client():
    """确保 eltdx TdxClient 单例已创建，返回 client 或 None。线程安全。"""
    global _eltdx_client, _eltdx_client_ready
    if _eltdx_client_ready and _eltdx_client is not None:
        return _eltdx_client
    try:
        from eltdx import TdxClient
        # probe_hosts=False：关闭启动服务器探测（探测会写排名缓存，
        # Windows 下文件占用会抛 OSError → RuntimeWarning，且非必需）
        _eltdx_client = TdxClient(timeout=10, probe_hosts=False)
        _eltdx_client_ready = True
        return _eltdx_client
    except Exception:
        _eltdx_client_ready = False
        _eltdx_client = None
        return None


def _get_xdxr_eltdx(market, code):
    """通过 eltdx 获取除权除息数据。在锁内调用。
    返回与 _normalize_xdxr_df 兼容的 DataFrame，失败返回 None。
    使用 client.corporate.capital_changes(code)，其中 0x000f 标签 1 即除权除息事件。字段映射：
      CapitalChangeRecord.c1_value=分红(每10股) · c2_value=配股价 ·
      c3_value=送转(每10股) · c4_value=配股数量(每10股)。
    """
    # 注意：此处不包 try/except——网络 / 接口（含 _check_eltdx_api_compat 的
    # RuntimeError 升级指引）异常一律上抛，由 get_xdxr_data 显著上报，不吞成「无数据」。
    client = _ensure_eltdx_client()
    if client is None:
        return None
    _check_eltdx_api_compat(client)   # 接口失效即抛错，避免静默降级
    market_code = f"{market.lower()}{code}"
    with client:
        block = client.corporate.capital_changes(market_code)
    records = getattr(block, "records", ()) or ()
    rows = []
    for r in records:
        # 仅保留除权除息（标签 1）事件
        if int(getattr(r, "category_raw", 0)) != 1:
            continue
        d = getattr(r, "date", None)
        if d is not None and not isinstance(d, datetime):
            d = datetime(d.year, d.month, d.day)
        rows.append({
            'code': getattr(r, 'code', code),
            'date': d,
            'category': int(getattr(r, 'category_raw', 1)),
            'fenhong': float(getattr(r, 'c1_value', 0) or 0),
            'peigujia': float(getattr(r, 'c2_value', 0) or 0),
            'songzhuangu': float(getattr(r, 'c3_value', 0) or 0),
            'peigu': float(getattr(r, 'c4_value', 0) or 0),
        })
    df = pd.DataFrame(rows, columns=['code', 'date', 'category',
                                     'fenhong', 'peigujia', 'songzhuangu', 'peigu'])
    if len(df) == 0:
        # 该股票历史上无除权除息事件：属「正常空结果」，返回 None（区别于异常）
        return None
    return _normalize_xdxr_df(df)


# 如需新增数据源：在 get_xdxr_data 的来源元组中追加 (名称, 取数函数) 即可，
# 返回值需能被 _normalize_xdxr_df 标准化；**不要**恢复静默 continue 式降级。


def get_xdxr_data(market, code):
    """
    获取指定股票的除权除息数据。
    线程安全：多线程并发时，网络请求串行化，避免 socket 竞争。

    优先级：
      1. 缓存（内存命中，跳过网络请求）
      2. eltdx（唯一数据源，基于 7709 协议、0x000f 命令；失败即报错而非静默降级）

    返回 pandas DataFrame，统一列名：
      date, category, fenhong, peigu, peigujia, songgu, zhuanzeng
    其中 fenhong/songgu/zhuanzeng/peigu 均为"每10股"单位。
    返回 None 表示无除权除息数据或 eltdx 获取失败。
    """
    if market.lower() not in ('sh', 'sz'):
        return None

    cache_key = (market, code)
    with _xdxr_lock:
        if cache_key in _xdxr_cache:
            return _xdxr_cache[cache_key]

        # 失败退避：eltdx 刚失败过（TTL 内）不再重试，快速返回 None，避免反复触发
        # 10s 超时 + 重复报错；逾期后自然重试并重新显著报错。
        _last_fail = _xdxr_fail_ttl.get(cache_key)
        if _last_fail is not None and time.time() - _last_fail < XDXR_RETRY_TTL:
            return None

        _failed = False
        for _src_name, _src_fn in (
            ("eltdx", _get_xdxr_eltdx),
        ):
            try:
                df = _src_fn(market, code)
            except Exception as _e:
                # 网络 / 接口（含 _check_eltdx_api_compat 的 RuntimeError 升级指引）异常：显著上报
                _failed = True
                log.error("[xdxr] %s 取数失败(market=%s, code=%s): %s",
                          _src_name, market, code, _e)
                # 尝试下一数据源；当前仅 eltdx 一个，continue 后即进入 _failed 分支
                continue
            if df is not None and len(df) > 0:
                _xdxr_fail_ttl.pop(cache_key, None)   # 取数成功：清除失败退避记录
                _xdxr_cache[cache_key] = df
                return df
            # df is None：该源无记录，继续尝试下一数据源
            continue

        if _failed:
            # 至少一个源异常且无成功数据：记录失败时刻用于退避、不写负缓存（保留可重试）
            _xdxr_fail_ttl[cache_key] = time.time()
            return None
        # 所有数据源均空结果 = 该股历史上无除权除息（正常空结果）：走 info，避免全市场
        # 扫描被「历史上无除权」刷满 ERROR；缓存 None 避免重复查询。
        log.info("[xdxr] %s: %s 无除权除息记录（正常空结果；网络/接口异常会另行上报 error）",
                 market, code)
        _xdxr_cache[cache_key] = None
        return None


# ============================================================
# 板块文件下载 / 刷新（基于 eltdx 0x06B9 通用文件读取）
# ============================================================
# 注意：spblock.dat 服务器不经文件下载通道服务（实测返回 0 字节），
# 刷新时保留旧文件（该文件由通达信客户端「盘后下载」单独写入）。
_MAX_BLOCK_FILE_BYTES = 8_000_000


def download_block_file_via_eltdx(file_name, hosts=None):
    """通过 eltdx 0x06B9 下载单个板块文件，返回原始字节；不可用/失败返回 None。

    hosts: 'host:port' 字符串列表；为 None 时使用 eltdx 默认服务器列表。
    """
    try:
        from eltdx import TdxClient
    except Exception:
        return None
    try:
        client = TdxClient(timeout=10, probe_hosts=False, hosts=hosts)
        with client:
            data = client.resources.download_file(
                file_name, max_bytes=_MAX_BLOCK_FILE_BYTES, chunk_size=0x4000
            )
        if not data:
            return None
        return data
    except Exception:
        return None


def download_block_files_via_eltdx(file_names, hosts=None, on_file=None):
    """批量下载板块文件，返回 {file_name: bytes}（失败/不可用的不计入）。

    单次连接内顺序下载，避免每文件重建连接；任一文件失败不影响其余。

    on_file: 可选回调 on_file(file_name, ok, nbytes)，每下完一个文件调用一次。
    刷新链路用它把「正在下载第 i/N 个文件」实时报到前端 —— 批量下载本身耗时
    可观（见 TdxAPI.refresh_block_files），没有逐文件回调时 UI 的 step 文字会
    在整个下载期间静止不动，用户只会感觉到"卡住了"。
    """
    result = {}
    try:
        from eltdx import TdxClient
    except Exception:
        return result
    try:
        client = TdxClient(timeout=10, probe_hosts=False, hosts=hosts)
        with client:
            for file_name in file_names:
                data = None
                try:
                    data = client.resources.download_file(
                        file_name, max_bytes=_MAX_BLOCK_FILE_BYTES, chunk_size=0x4000
                    )
                except Exception:
                    data = None
                if data:
                    result[file_name] = data
                if on_file is not None:
                    try:
                        on_file(file_name, bool(data), len(data) if data else 0)
                    except Exception:
                        pass
    except Exception:
        pass
    return result


# ============================================================
# 行情统计：PE-TTM / 流通市值（仅 A 股；eltdx 无港股通道）
# ============================================================
# 两个取数函数与 DataAPI/TxAPI.py 的对应函数**契约一致**，使
# App 层（AppEngine._fetch_pe_ttm_live）能按标的类型选源后互换调用：
#   - fetch_pe_ttm_single(market, code) → {mkt+code: PE-TTM(倍)}（7615 HTTP 单只）
#   - fetch_float_mc(stock_list)        → {code: 流通市值(亿元)}
#
# 选源规则（唯一一份）在 App/AppEngine.py：A 股个股 → 本模块；指数 / 港股 → TxAPI。
# 港股不在本模块服务范围：eltdx 的 codes.all("hk") / 0x054c 对 hk 均不可用。
ELTDX_MARKETS = ("sh", "sz", "bj")

# 逐批请求上限。实测 0x0010 财务批量与 0x054c 快照在 **80 只/批** 稳定；
# 单批放大到 200 只时服务端会截断（返回 100 条）或给出错位响应，
# 故取 80（与 eltdx helpers 内部同值）。
STATS_BATCH = 80

# 批次整体失败后的补救拆分粒度（见 _salvage_fetch）。取 8 的依据是 eltdx
# 3.3.2 的库内恢复预算 `_RECOVERY_REQUESTS = 32`（eltdx/api/_batch.py）：库内
# 定位「哪几只缺失」的代价约 2k·log2(n/k)，8 只小段即使**全缺**也只要 ~16 次，
# 稳稳落在 32 之内；而 80 只批在缺失过半时必然打满 32 次抛 budget exhausted。
_SALVAGE_SUBCHUNK = 8


def _is_protocol_error(err):
    """异常是否属于「协议解析失败」——即批次载荷层面出问题的信号。

    eltdx 分层：ProtocolError（编码 / 解码失败，数据层面 ⇒ 拆段重取有意义）
    vs TransportError（超时 / 断连 / 池忙，网络层面 ⇒ 拆分与重试都无意义，
    逐只重试只会把 1 次超时放大成 80 次）。
    `MissingCodesError` 继承 `ProtocolError`（eltdx/exceptions.py），但本模块
    一律以 `on_missing="skip"` 调用，缺票不会走到这里；真走上来（例如未来有人
    改回 `raise`）也会被当作可拆段处理，行为安全。
    eltdx 未安装或异常层次变动时按类名兜底，避免把网络故障误判成坏代码。
    """
    try:
        from eltdx.exceptions import ProtocolError
        return isinstance(err, ProtocolError)
    except Exception:
        return "ProtocolError" in type(err).__name__


def _salvage_fetch(codes, batch_fn):
    """整批取数失败后的补救：先按 _SALVAGE_SUBCHUNK 拆段，再对仍失败的段逐只。

    返回 (rows, bad)，bad 为 [(code, 原因)]。
    **成功的段直接复用本次响应**，不重复请求 —— 数据在补救过程中就已拿全。

    为什么不再递归二分（2026-10-31，eltdx 3.3.2 起）：
      eltdx 3.3.2 把「批次里有票没返回 ⇒ 整批报错」改成了 on_missing 策略。
      本模块一律传 on_missing="skip"（见 fetch_float_mc 的两个 lambda），缺票由
      库内**在 32 次请求的预算内**自行定位并返回其余记录，不再抛异常上浮。
      能逃出库的 ProtocolError 只剩两类（eltdx/api/_batch.py recover_batch）：
        ① `... recovery request budget exhausted (32)` —— 缺失过密，库内预算不足；
        ② 载荷异常（invalid record / unexpected or duplicate record /
           count exceeds request）—— 库内已判为不可自动恢复，直接上抛。
      旧实现在应用层再递归二分一次，等于给**每个子批重新发放一份 32 次预算**：
      实测 80 只批的整批故障从 ~90 次请求放大到上千次，单颗坏码也要 100+ 次。
      改为「拆段 + 逐只」后请求数由结构本身封顶，没有放大路径。

    请求上限：ceil(n/8) + n 次。且**只要有一段成功**，就证明载荷本身可解析、
    失败是代码相关，才继续逐只；若所有段都失败**且段数 > 1**，判定为与代码
    无关的整批故障（帧级错位 / 服务端整批不可解析），立刻止损、不逐只 ——
    实测整批故障 10 次请求即止（旧实现 76 次应用层调用、wire 上千次）。
    n <= _SALVAGE_SUBCHUNK 时整批只有一段，「所有段都失败」无判别力（小股池
    会整批丢），故照常逐只，代价最多 n(≤8) 次请求。

    非协议类异常（超时 / 断连）**不拆不逐只**：批一级已重试过 1 次，到这一层再
    放大只会把 1 次断连变成 n 次等待 —— 与 _chunked_stats_fetch 的批一级口径
    一致，只把「尚未取到」的代码记 bad 后立即返回。
    """
    rows, bad = [], []

    def _transport_bad(exc):
        """非协议类异常（超时 / 断连）时，把「尚未取到」的代码记 bad。

        两个排除项缺一不可：
          · `done` —— 已经从 rows 拿到的，不算失败；
          · `recorded` —— **已经因协议错记过 bad 的**，不得再记一次。
        逐只阶段「某只协议错、紧接着另一只传输错」时，那只协议错的票不在 `done`
        里（失败的不进 rows），只按 `done` 过滤就会让同一 code 在日志里出现两条、
        把失败只数报虚（`_salvage_or_drop` 的计数与预览直接读这个列表）。
        """
        done = {_row_code(r) for r in rows}
        recorded = {c for c, _ in bad}
        return [(c, f"{type(exc).__name__}: {exc}")
                for c in codes if c not in done and c not in recorded]

    pieces = [codes[i:i + _SALVAGE_SUBCHUNK]
              for i in range(0, len(codes), _SALVAGE_SUBCHUNK)]
    failed = []
    for piece in pieces:
        try:
            rows.extend(batch_fn(piece))
        except Exception as _e:
            if not _is_protocol_error(_e):
                bad.extend(_transport_bad(_e))
                return rows, bad
            failed.append(piece)
    if not failed:
        return rows, bad
    if len(failed) == len(pieces) and len(pieces) > 1:
        # 每一段都失败**且段数 > 1** ⇒ 故障与具体代码无关。逐只重试只会把 1 次
        # 故障放大成 n 次。
        # 段数 == 1（n <= _SALVAGE_SUBCHUNK：自选股 / 小板块这类小股池）不走这个
        # 止损 —— 整批只有一段，「所有段都失败」没有判别力，逐只最多再打 n(≤8)
        # 次请求就能救回毒丸旁边的票；按段数止损会把这几只全部记 bad（市值列
        # 整列 `--`），而请求数省下的只是个位数。
        bad.extend((c, "拆段后所有段均失败，判整批故障，未细分") for c in codes)
        return rows, bad
    for piece in failed:
        for code in piece:
            try:
                rows.extend(batch_fn([code]))
            except Exception as _e:
                if not _is_protocol_error(_e):
                    bad.extend(_transport_bad(_e))
                    return rows, bad
                bad.append((code, f"{type(_e).__name__}: {_e}"))
    return rows, bad


def _salvage_or_drop(start, chunk, batch_fn, reason, tag="[eltdx 统计]"):
    """对整段失败批次做拆段补救，补救结果打日志后返回可用 rows。

    首轮协议失败与「重试后转为协议失败」共用此入口，保证两条路径的分类与
    日志口径一致。
    """
    rows, bad = _salvage_fetch(chunk, batch_fn)
    if bad:
        preview = "、".join(f"{c}（{why}）" for c, why in bad[:5])
        tail = " …" if len(bad) > 5 else ""
        log.warning("%s 批次 %d-%d %s，拆段后仍 %d 只取数失败"
                    "（无数据 / 载荷异常 / 整批故障，原因附后）：%s%s",
                    tag, start, start + len(chunk), reason, len(bad), preview, tail)
    return rows


def _row_code(row):
    """记录对应的 full_code（`sh600000` 这种 8 位形态）。

    两条批量命令的记录都自带 `full_code`（eltdx 的 `recover_batch` 就靠它按请求
    顺序重组，见 eltdx/api/_batch.py）；`exchange` + `code` 是兜底拼法，防止上游
    字段改名后 _log_missing 把**整批**误报成「均未返回」。
    """
    full_code = getattr(row, "full_code", None)
    if isinstance(full_code, str):
        return full_code
    exchange = getattr(row, "exchange", None)
    code = getattr(row, "code", None)
    if isinstance(exchange, str) and isinstance(code, str):
        return exchange + code
    return None


def _log_missing(tag, start, chunk, rows):
    """把「请求了但没返回」的代码显式记一笔。

    eltdx 3.3.2 起 `on_missing="skip"` 把缺票从「异常」降级为「少几条记录」，
    代价是静默丢数据——差集必须由调用方自己还原。缺票本身是常态（退市 /
    长期停牌 / 当日无行情），记 log.info；**整批一只都没回来**才是服务端异常，
    记 log.warning。

    tag 带命令标识（0x0010 / 0x054c）：两条命令跑同一批代码，不标就出现两行
    一模一样的「未返回」，看起来像重复执行，实际是一条命令丢一次。
    """
    returned = {_row_code(r) for r in rows}
    missing = [c for c in chunk if c not in returned]
    if not missing:
        return
    preview = "、".join(missing[:8])
    tail = f" …（共 {len(missing)} 只）" if len(missing) > 8 else ""
    if len(missing) == len(chunk):
        log.warning("%s 批次 %d-%d 整批 %d 只均未返回（服务端异常？）：%s%s",
                    tag, start, start + len(chunk), len(chunk), preview, tail)
    else:
        log.info("%s 批次 %d-%d 服务端未返回 %d/%d 只（退市 / 长期停牌）：%s%s",
                 tag, start, start + len(chunk), len(missing), len(chunk), preview, tail)


def _chunked_stats_fetch(full_codes, batch_fn, extract, label=""):
    """按 STATS_BATCH 分批取数，返回 {full_code: 值}。

    label: 日志用的命令标识（如 "0x0010 股本"）。两条批量命令共用本函数，
           不标则同批缺票会打出两行完全相同的日志、无法区分是哪条命令丢的。

    缺票语义（2026-10-31，eltdx 3.3.2 起）：batch_fn 一律以 `on_missing="skip"`
    调用（见 fetch_float_mc 的两个 lambda），服务端没给的代码**不再抛异常**，
    而是落在返回记录之外。差集由 _log_missing 还原并记账 —— 少了这一步，
    「跳过」就等于静默丢数据。

    单批异常时按异常性质分流，不再一律逐只重试 80 次：
      - ProtocolError（载荷层面）⇒ 拆段补救（_salvage_fetch），其余照常取数。
        3.3.2 之后能上浮的只剩「缺失过密打满库内 32 次预算」与「载荷异常」两类。
      - 其他异常（超时 / 断连 / 池忙）⇒ 整批重试 1 次：仍抛协议错（错位帧）
        则转入拆段，仍是传输错才整批跳过。这类错误逐只重试只会把 1 次超时
        放大成 80 次；瞬时抖动重试即可成功（上层已有「本地缓存兜底」，且缓存
        是合并写、缺失即放行，不会误杀）。
    """
    tag = f"[eltdx 统计][{label}]" if label else "[eltdx 统计]"
    out = {}
    for start in range(0, len(full_codes), STATS_BATCH):
        chunk = full_codes[start:start + STATS_BATCH]
        try:
            rows = list(batch_fn(chunk))
        except Exception as _e:
            if not _is_protocol_error(_e):
                try:
                    rows = list(batch_fn(chunk))
                except Exception as _retry_err:
                    if not _is_protocol_error(_retry_err):
                        log.warning("%s 批次 %d-%d 传输失败（重试 1 次仍失败），整批跳过：%s: %s",
                                    tag, start, start + len(chunk),
                                    type(_retry_err).__name__, _retry_err)
                        continue
                    log.warning("%s 批次 %d-%d 重试后转为协议失败（首轮 %s），转入拆段补救",
                                tag, start, start + len(chunk), type(_e).__name__)
                    rows = _salvage_or_drop(start, chunk, batch_fn,
                                            f"重试后转为协议失败（首轮 {type(_e).__name__}）",
                                            tag=tag)
                else:
                    log.info("%s 批次 %d-%d 传输抖动，重试 1 次成功",
                             tag, start, start + len(chunk))
            else:
                log.warning("%s 批次 %d-%d 取数失败，拆段补救：%s: %s",
                            tag, start, start + len(chunk), type(_e).__name__, _e)
                rows = _salvage_or_drop(start, chunk, batch_fn, "取数失败", tag=tag)
        _log_missing(tag, start, chunk, rows)
        out.update(extract(rows))
    return out


def _is_code6(value):
    """是否 6 位数字代码。

    服务端在批次里含无行情代码时会返回错位载荷：实测有些批次**不报错**，
    而是把 HTML / CSS 片段当成 code 解出来（如 'tyle["'、'n()">'、'olor: '）。
    这类脏记录必须挡在映射键之外 —— 否则它以随机键写进结果，还会顶掉同批
    真实票的位置（整批 80 只的数据随之丢失，且不产生任何异常）。
    """
    return isinstance(value, str) and len(value) == 6 and value.isdigit()


def _finance_shares_map(records):
    """0x0010 财务批量记录 → {full_code: 流通股本(股)}。

    流通股本为 0/None 的代码不计入（如部分北交所标的无流通股本，
    市值本就无从计算）——不写 0，交由调用方按「未获取到」统计。

    脏 code 过滤（`_is_code6`）是**廉价兜底，不是主防线**：eltdx 3.3.2 已在
    协议层校验「市场号 + 六位 ASCII 数字 + 记录必须属于本次请求的代码集合」，
    正常情况永不命中。一旦命中说明上游校验退化，必须**响**——否则脏记录会以
    随机键写进结果并顶掉同批真实票的位置（整批数据丢失且无异常）。
    """
    out = {}
    dirty = []
    for r in records:
        exchange = getattr(r, "exchange", None) or ""
        code = getattr(r, "code", None)
        shares = getattr(r, "circulating_shares", None)
        if exchange and _is_code6(code):
            if shares:
                out[f"{exchange}{code}"] = float(shares)
        else:
            dirty.append(repr(code))
    if dirty:
        log.warning("[eltdx 统计] 0x0010 出现 %d 条非法代码记录（eltdx 协议校验退化？"
                    "已验证 3.3.2 应永不出现），示例：%s", len(dirty), "、".join(dirty[:5]))
    return out


def _snapshot_price_map(rows):
    """0x054c 快照 → {full_code: 最新价(元)}

    脏 code 过滤口径同 `_finance_shares_map`：3.3.2 协议层已保证「六位数字 +
    属于请求集合」，此处的 `_is_code6` 只是兜底；命中即告警。
    """
    out = {}
    dirty = []
    for r in rows:
        full_code = getattr(r, "full_code", None)
        price = getattr(r, "last_price", None)
        if isinstance(full_code, str) and len(full_code) == 8 and _is_code6(full_code[2:]):
            if price:
                out[full_code] = float(price)
        else:
            dirty.append(repr(full_code))
    if dirty:
        log.warning("[eltdx 统计] 0x054c 出现 %d 条非法代码记录（eltdx 协议校验退化？"
                    "已验证 3.3.2 应永不出现），示例：%s", len(dirty), "、".join(dirty[:5]))
    return out


# 7615 F10/TQLEX HTTP 网关超时。eltdx 3.2.0 起 F10 默认 IPv4-first（直连实测
# 113~133ms），3s 余量已很大；走代理的环境约 310~360ms 也在余量内。
_PE_SINGLE_TIMEOUT = 3.0
# 瞬时抖动重试：单请求超时后再试 1 次（成功即返回），打开 K 线页面时可扛住偶发抖动。
_PE_SINGLE_RETRIES = 1
# 熔断器：连续失败达阈值（网关整体不可达）后冷却期内直接返回 {}，
# 防止反复点开股票时每只都白等 超时×(1+重试)。
_PE_CB_THRESHOLD = 3      # 连续失败次数阈值
_PE_CB_COOLDOWN = 600     # 熔断冷却 10 分钟
_PE_CB_FAILS = 0          # 连续失败计数（受 _PE_CB_LOCK 保护）
_PE_CB_OPEN_UNTIL = 0.0   # 熔断解除时刻（epoch）；0=未熔断
_PE_CB_LOCK = threading.Lock()


def _fetch_valuation_rows(code):
    """取单只股票的 7615 估值表原始行（list[dict]，键含 PETTM / DATE 等）。

    走 eltdx >= 3.2.0 官方 F10Client（IPv4-first，与股东增减持 _fetch_plan_rows
    同策略）。取数失败抛异常，由调用方的重试/熔断处理。
    """
    from eltdx.f10 import F10Client
    resp = F10Client(timeout=_PE_SINGLE_TIMEOUT).valuation(code)
    if not getattr(resp, "ok", False):
        raise RuntimeError("网关返回 ok=False")
    rows = []
    for table in getattr(resp, "tables", ()) or ():
        rows.extend(list(getattr(table, "rows", ()) or ()))
    return rows


def _pe_value_from_valuation_rows(key, rows):
    """从估值表行中取 PETTM 字段 → {key: float}；无值 / 非法返回 {}。

    PETTM 为服务端返回的字符串（如 "49.31"）；行内 DATE 最大者即最新估值。
    无 PETTM / 为 0 / 非法数值 → 该票无 PE（正常空结果），返回 {}；
    **负值保留**（亏损股口径，与 0x06B9 全量表过滤规则一致）。
    """
    best = None
    best_date = None
    for r in rows:
        v = r.get("PETTM")
        if v is None or str(v).strip() == "":
            continue
        try:
            val = float(v)
        except (TypeError, ValueError):
            continue
        if val == 0:
            continue
        d = r.get("DATE")
        if best is None or (d is not None and (best_date is None or d > best_date)):
            best, best_date = val, d
    return {key: best} if best is not None else {}


def fetch_pe_ttm_single(market, code):
    """eltdx **单只查询** **A 股** PE-TTM（滚动市盈率），返回 {mkt+code: float}。

    market: 'sh'/'sz'/'bj'；其余市场（如 hk）直接忽略——返回 {}，选源见
    App/AppEngine.py。
    数据源：7615 F10 / TQLEX HTTP 网关 `F10Client.valuation`（Entry=
    HQServ.hq_nlp_gpsj，ReqId=200191 估值表，**按代码单只查询**）——与股东
    增减持同类：打开一只股票时取一次，**不做全量拉取、不落盘**（2026-09
    用户定版：不再走 0x06B9 zhb.zip / tdxstat.cfg 全市场统计文件，删除原
    fetch_pe_ttm / fetch_pe_ttm_all 批量取数）。
    网络层：eltdx >= 3.2.0 官方 F10Client（IPv4-first）；重试 1 次 + 连续
    失败 3 次熔断 10 分钟（与股东增减持同策略）。
    取数失败**抛异常**（由 AppData 记 error 显著上报，不静默返回 {} ——
    「取数失败」与「该票无 PE」必须可区分）。
    """
    market = (market or "").lower()
    if market not in ELTDX_MARKETS:
        return {}
    # eltdx 仅服务 A 股个股 + 指数；ETF/基金/债券/B股 会被 eltdx 内部
    # infer_market 抛错。取数前预筛，避免无谓的异常与告警（与搜索缓存同一套
    # 前缀判定，搜索框据此不收录 ETF/基金/债券/B股，但指数照常收录并取数）。
    from DataAPI.TdxAPI import is_pe_ttm_target as _is_target
    if not _is_target(market, code):
        log.debug("[PE-TTM] 跳过非个股/指数标的(%s%s): eltdx 不支持", market, code)
        return {}
    global _PE_CB_FAILS, _PE_CB_OPEN_UNTIL
    key = market + code
    now = time.time()
    with _PE_CB_LOCK:
        if now < _PE_CB_OPEN_UNTIL:
            # 熔断中：网关刚被判定整体不可达，直接放弃本次（静默返回 {}，
            # 冷却期结束后的第一次失败会再打一条汇总日志）。
            return {}
    last_err = None
    rows = None
    for _attempt in range(1 + _PE_SINGLE_RETRIES):
        try:
            rows = _fetch_valuation_rows(code)
            last_err = None
            break
        except Exception as _e:              # noqa: BLE001
            last_err = _e
    if last_err is not None:
        with _PE_CB_LOCK:
            _PE_CB_FAILS += 1
            if _PE_CB_FAILS >= _PE_CB_THRESHOLD:
                _PE_CB_OPEN_UNTIL = time.time() + _PE_CB_COOLDOWN
                log.warning("[PE-TTM] 连续 %d 次取数失败（末次: %s%s: %s），"
                            "熔断 %d 分钟内跳过该数据源",
                            _PE_CB_FAILS, market, code, last_err,
                            _PE_CB_COOLDOWN // 60)
            else:
                log.warning("[PE-TTM] 取数失败(%s%s): %s", market, code, last_err)
        raise RuntimeError(f"[PE-TTM] 取数失败({market}{code}): {last_err}")
    with _PE_CB_LOCK:
        _PE_CB_FAILS = 0
        _PE_CB_OPEN_UNTIL = 0.0
    return _pe_value_from_valuation_rows(key, rows)


def fetch_float_mc(stock_list):
    """eltdx 批量获取 **A 股** 流通市值，返回 {code: 流通市值(亿元)}。

    stock_list: list[{"code": "600519", "prefix": "1"}, ...]，prefix
                0→sz / 1→sh / 2→bj；其它前缀（hk / us）一律跳过——与改造前的
                腾讯路径一致：流通市值本就只覆盖 A 股。
    口径：流通市值 = 流通股本(0x0010) × 最新价(0x054c)，与 eltdx
          StockProfile.circulating_market_value 同源同式；单位由「元」→「亿元」
          （腾讯 [44] 返回亿元且只有两位小数，本路径为原值，精度更高）。
    缺失语义：无股本 / 股本为 0 / 无快照的代码不计入结果，不写 0。
    缺票语义：两条批量命令都以 `on_missing="skip"` 调用（eltdx 3.3.2 起）。
          服务端没返回的代码（退市 / 长期停牌 / 当日无行情）**不再让整批报错**，
          由 _chunked_stats_fetch 记录差集后跳过；默认的 `"raise"` 会让一颗缺票
          拖掉整批 80 只。**要求 eltdx >= 3.3.2**（`on_missing` 自 3.3.2 起提供，
          见 `_check_eltdx_api_compat`）。
    """
    _PFX_TO_MKT = {"0": "sz", "1": "sh", "2": "bj"}
    full_codes = []
    for stk in stock_list or ():
        mkt = _PFX_TO_MKT.get(stk.get("prefix", ""), "")
        code = stk.get("code", "")
        if mkt and code:
            full_codes.append(mkt + code)
    if not full_codes:
        return {}
    full_codes = list(dict.fromkeys(full_codes))    # 去重保序：同一票不重复请求

    client = _ensure_eltdx_client()
    if client is None:
        raise RuntimeError(
            "[eltdx 不可用] 流通市值取数需要 eltdx，请安装/升级："
            "pip install -U 'eltdx>=3.3.2'"
        )
    _check_eltdx_api_compat(client)     # 缺 on_missing() 直接抛升级指引，不落成 TypeError
    _quiet_eltdx_missing_warnings()     # 缺票改由 _log_missing 记账，见该函数注释
    with client:
        # 先跑完股本批再取快照批：两批互不依赖，但股本批的失败面更小（财务批
        # 载荷规整），先跑它可让后续快照批的补救日志只覆盖真正的快照故障。
        # 注意：这与「前一个请求失败会让下一个请求读到错位响应」无关 —— eltdx
        # 运行时按 message identity（msg_id + msg_type）配对响应，不匹配的帧归
        # Push 丢弃（crates/eltdx-runtime/src/slot.rs `on_frame`），不会串包。
        shares = _chunked_stats_fetch(
            full_codes,
            lambda ch: list(getattr(client.corporate.finance_batch(ch, on_missing="skip"),
                                    "records", ()) or ()),
            _finance_shares_map,
            label="0x0010 股本",
        )
        prices = _chunked_stats_fetch(
            full_codes,
            lambda ch: list(client.quotes.get_snapshots(ch, on_missing="skip")),
            _snapshot_price_map,
            label="0x054c 快照",
        )

    result = {}
    for full_code in full_codes:
        shares_val = shares.get(full_code)
        price_val = prices.get(full_code)
        if not shares_val or not price_val:
            continue
        result[full_code[2:]] = shares_val * price_val / 1e8
    return result


# ============================================================
# 重要股东买卖：股东增减持计划（通达信 F10 7615 网关，按代码查询）
# ============================================================
# 数据源：eltdx.f10.F10Client.shareholder_change_plans（7615 TQLEX 网关
#   Entry=CWServ.tdxf10_gg_gdyj + section gdzjcjh）。
#   **要求 eltdx >= 3.2.0**：该版本起 F10 默认 IPv4-first（f10/_http.py 的
#   _connect_ipv4_first），修复了旧版裸 urlopen 在「本机 IPv6 出口不通」时
#   每请求卡 8~12s 的地址序问题。
# 这是「按代码查询」的协议命令（与 xdxr 同类），**不是** PE-TTM 那样一次性
# 下载全市场统计文件；故按单只股票取数 + 进程缓存，不落盘全市场文件。
# 列名（实测 7615 网关 ColName 返回 N001..N012，与 eltdx 解析一致）：
#   N001 公告日期  N002 拟减持/拟增持  N003 股东名称  N004 股东身份
#   N005 拟减持股数  N006 占总股本%  N007/N008 拟增持股数(下限/上限)
#   N009 变动起始日期  N010 变动截止日期  N011 进度(完成/进行中/未实施)
_REDUCTION_CACHE = {}          # mkt+code -> (epoch, [plans])
_REDUCTION_CACHE_TTL = 24 * 3600
_REDUCTION_LOCK = threading.Lock()
# 7615 F10 网关 HTTP 超时。eltdx 3.2.0 起 F10 默认 IPv4-first（直连实测
# 113~133ms），3s 余量已很大；走代理的环境约 310~360ms 也在余量内。
_REDUCTION_TIMEOUT = 3.0
# 瞬时抖动重试：单请求超时后再试 1 次（成功即返回），扫描逐票调用时可扛住偶发抖动。
_REDUCTION_RETRIES = 1
# 熔断器：连续失败达阈值（网关整体不可达）后冷却期内直接返回 []，
# 防止扫描 1000 只股票时每只都白等 超时×(1+重试) 把扫描拖爆。
_REDUCTION_CB_THRESHOLD = 3      # 连续失败次数阈值
_REDUCTION_CB_COOLDOWN = 600     # 熔断冷却 10 分钟
_REDUCTION_CB_FAILS = 0          # 连续失败计数（受 _REDUCTION_LOCK 保护）
_REDUCTION_CB_OPEN_UNTIL = 0.0   # 熔断解除时刻（epoch）；0=未熔断


def _fetch_plan_rows(code):
    """取「股东增减持计划」原始行（list[dict-like]，键 N001~N012）。

    走 eltdx >= 3.2.0 官方 F10Client：3.2.0 起 F10 默认 IPv4-first
    （`f10/_http._connect_ipv4_first`：getaddrinfo 后稳定排序 AF_INET 优先、
    逐地址回落，保留代理/Host/SNI/证书校验，不改全局 socket），上游已修复
    「本机 IPv6 黑洞 → 每次卡 8~12s」的问题；IPv6-only 域名也仍可连。
    取数失败抛异常，由调用方的重试/熔断处理。
    """
    from eltdx.f10 import F10Client
    resp = F10Client(timeout=_REDUCTION_TIMEOUT).shareholder_change_plans(code)
    if not getattr(resp, "ok", False):
        raise RuntimeError("网关返回 ok=False")
    return list(getattr(resp, "rows", ()) or ())


def get_shareholder_reduction_plans(market, code):
    """获取某股票的「股东增减持计划」原始列表（按代码查，进程缓存 1 天）。

    返回 list[dict]，每条字段：
        announce_date, direction, holder, identity,
        reduce_shares, reduce_pct, start, end, progress
    非 A 股（指数 / 港股 / 期货等）、取数失败 → 返回 []。

    网络层：eltdx 官方客户端（IPv4-first，见 _fetch_plan_rows）。
    **F10 的 IPv4-first 自 eltdx 3.2.0 起；本模块整体下界是 3.3.2**
    （批量接口需要 on_missing，见 requirements.txt / _check_eltdx_api_compat）。
    旧版裸 urlopen 在本机 IPv6 不通时每次卡 8~12s，不做旧版回退——依赖以
    requirements 明确，避免双路径难排查。
    """
    if market.lower() not in ('sh', 'sz', 'bj'):
        return []
    # 股东增减持仅服务 A 股个股；ETF/基金/债券/B股/指数 都会被 eltdx 内部
    # infer_market 抛错或无意义。取数前预筛，避免无谓的异常与告警（与搜索缓存
    # 同一套前缀判定，且指数段在本路径显式剔除——指数无股东）。
    from DataAPI.TdxAPI import is_shareholder_reduction_target as _is_target
    if not _is_target(market, code):
        return []
    global _REDUCTION_CB_FAILS, _REDUCTION_CB_OPEN_UNTIL
    key = market.lower() + code
    now = time.time()
    with _REDUCTION_LOCK:
        cached = _REDUCTION_CACHE.get(key)
        if cached and now - cached[0] < _REDUCTION_CACHE_TTL:
            return cached[1]
        cb_open = now < _REDUCTION_CB_OPEN_UNTIL
    if cb_open:
        # 熔断中：网关刚被判定整体不可达，直接放弃本次（不占扫描耗时）。
        # 静默返回——冷却期结束后的第一次失败会再打一条汇总日志。
        return []
    last_err = None
    rows = None
    for attempt in range(1 + _REDUCTION_RETRIES):
        try:
            rows = _fetch_plan_rows(code)
            last_err = None
            break
        except Exception as _e:
            last_err = _e
    if last_err is not None:
        with _REDUCTION_LOCK:
            _REDUCTION_CB_FAILS += 1
            if _REDUCTION_CB_FAILS >= _REDUCTION_CB_THRESHOLD:
                _REDUCTION_CB_OPEN_UNTIL = time.time() + _REDUCTION_CB_COOLDOWN
                log.warning("[股东增减持] 连续 %d 次取数失败（末次: %s%s: %s），"
                            "熔断 %d 分钟内跳过该数据源",
                            _REDUCTION_CB_FAILS, market, code, last_err,
                            _REDUCTION_CB_COOLDOWN // 60)
            else:
                log.warning("[股东增减持] 取数失败(%s%s): %s", market, code, last_err)
        return []
    with _REDUCTION_LOCK:
        _REDUCTION_CB_FAILS = 0
        _REDUCTION_CB_OPEN_UNTIL = 0.0
    plans = []
    for item in rows:
        plans.append({
            "announce_date": item.get("N001"),
            "direction": item.get("N002"),
            "holder": item.get("N003"),
            "identity": item.get("N004"),
            "reduce_shares": item.get("N005"),
            "reduce_pct": item.get("N006"),
            "start": item.get("N009"),
            "end": item.get("N010"),
            "progress": item.get("N011"),
        })
    with _REDUCTION_LOCK:
        _REDUCTION_CACHE[key] = (now, plans)
    return plans


def _parse_plan_date(s):
    """把 'YYYY-MM-DD' / 'YYYY/MM/DD' / 'YYYYMMDD'（可带 ' HH:MM[:SS]' 时间尾部，
    来自日内周期 K 线日期）解析为 date；无法解析返回 None。

    注意：应用里 K 线日期用 func_util._get_date_fmt 的 %Y/%m/%d（斜杠）格式，
    而 eltdx 网关返回的减持计划日期是 %Y-%m-%d（短横）；两者都要兼容。
    """
    if not s:
        return None
    s = str(s).strip()
    s = s.split(" ")[0]          # 丢弃时间部分（日内周期日期形如 2026/09/11 15:00）
    s = s.replace("/", "-")      # 兼容 / 与 - 分隔符
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _fmt_mmdd(d):
    """date → 'MM-DD'（徽标里展示的紧凑形式），None 原样返回 None。"""
    if d is None:
        return None
    return d.strftime("%m-%d")


def get_shareholder_reduction_flag(market, code, today):
    """判断「当前交易日 today」是否落在某条「拟减持」计划的 减持窗口（公告日~变动截止日）内。

    today: 'YYYY-MM-DD' / 'YYYY/MM/DD' / 'YYYYMMDD'（建议传 K 线最新一根日期 = 屏幕上当前交易日）。
    命中任一条「进行中」拟减持计划即返回 active=True，并把**所有被命中窗口合并为
    一个最大连续区间**（最早起始 ~ 最晚截止；因命中窗口都包含 today，并集天然
    连续无空洞，整段即潜在抛压区间）；windows 恒为 1 条，另附最大拟减持股比 /
    股东名单；否则返回 {'active': False}。

    进度（N011）过滤：仅「进行中」的计划计入抛压——「完成」（已实施完毕、
    抛压已释放）与「停止实施」（计划取消）不计入；未知状态默认保留。

    窗口 = **公告日(N001) ~ 变动截止日(N010)**。语义：减持计划一经公告，
    潜在抛压即告成立，持续到窗口截止——这正是「公告日到截止日」的业务含义。
    N009（变动起始日）仅作 N001 缺失时的回退，不作为常规下界。
    若源数据错乱导致 起始 > 截止，该行跳过。

    注意：本函数触发 7615 网关 HTTP 调用（eltdx >= 3.2.0 IPv4-first，直连实测
    113~133ms、超时 3s；进程内每代码缓存 1 天；失败重试 1 次 + 连续失败熔断
    10 分钟），**可安全同步调用**——AppEngine 在 K 线主分析路径上直接调用它。
    曾经「每次 8~12s」的卡顿根因是旧版 eltdx 默认 urlopen 走了本机不通的
    IPv6（3.2.0 已在上游修复），与本函数本身的逻辑无关。
    """
    if market.lower() not in ('sh', 'sz', 'bj'):
        return {"active": False}
    today_d = _parse_plan_date(today)
    if today_d is None:
        return {"active": False}
    plans = get_shareholder_reduction_plans(market, code)
    windows = []      # [(end, start_str, end_str, pct, holder), ...] 只保留「今日命中」的窗口
    hit_pct = None
    all_holders = []
    for p in plans:
        direction = p.get("direction") or ""
        if "减持" not in direction:
            continue
        # 进度过滤（N011 实测词表：进行中 / 完成 / 停止实施）：
        # 「完成」= 已实施完毕、抛压已释放；「停止实施」= 计划取消。均不计入抛压区间。
        # 用黑名单匹配而非白名单（未知新状态默认保留，宁可多显示不漏抛压）。
        progress = str(p.get("progress") or "")
        if any(k in progress for k in ("完成", "停止实施", "失效", "终止", "结束", "取消")):
            continue
        # 窗口 = 公告日(N001) ~ 变动截止日(N010)。语义：公告披露后即可视为
        # 潜在抛压开始，直到窗口截止；N009（变动起始日）仅作 N001 缺失时的回退。
        lo = _parse_plan_date(p.get("announce_date")) or _parse_plan_date(p.get("start"))
        hi = _parse_plan_date(p.get("end"))
        if lo is None or hi is None or lo > hi:
            continue
        if lo <= today_d <= hi:
            window = {
                "start": _fmt_mmdd(lo),
                "end": _fmt_mmdd(hi),
                "start_full": lo.strftime("%Y-%m-%d"),
                "end_full": hi.strftime("%Y-%m-%d"),
            }
            try:
                pct = float(p["reduce_pct"])
            except (TypeError, ValueError):
                pct = None
            window["pct"] = pct
            window["holder"] = p.get("holder")
            windows.append(window)
            if pct is not None and (hit_pct is None or pct > hit_pct):
                hit_pct = pct
            holder = p.get("holder")
            if holder and holder not in all_holders:
                all_holders.append(holder)
    if not windows:
        return {"active": False}
    # 合并为**最大连续区间**（最早起始 ~ 最晚截止）：能进入 windows 的窗口都
    # 包含 today，故它们的并集天然连续、无空洞——整段都可视作潜在抛压区间
    # （用户定版口径：不管命中几条计划，徽标只显示一个「06-14~12-16」式大区间）。
    # 股东名「/」串接去重、占比取最大。
    lo_full = min(w["start_full"] for w in windows)   # ISO 日期可直接按字符串比较
    hi_full = max(w["end_full"] for w in windows)
    span_holders = []
    span_pct = None
    for w in windows:
        h = w.get("holder")
        if h and h not in span_holders:
            span_holders.append(h)
        if w.get("pct") is not None and (span_pct is None or w["pct"] > span_pct):
            span_pct = w["pct"]
    windows = [{
        "start": lo_full[5:],                # MM-DD
        "end": hi_full[5:],                  # MM-DD
        "start_full": lo_full,
        "end_full": hi_full,
        "pct": span_pct,
        "holder": "/".join(span_holders),
    }]
    return {
        "active": True,
        "windows": windows,                  # 恒 1 条：{start, end, start_full, end_full, pct, holder}
        "max_pct": hit_pct,
        "holders": all_holders,
        # 兼容旧字段（首个被命中窗口 = 区间截止日）—— 前端徽标默认取第一个
        "end": windows[0]["end_full"],
    }
