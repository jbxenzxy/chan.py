# -*- coding: utf-8 -*-
"""
同花顺自选股 -> 通达信自选股 同步工具

用法：
  python App/ths_sync_to_tdx.py              # 默认替换模式：清空通达信自选股后写入
  python App/ths_sync_to_tdx.py --preview    # 仅预览，不实际写入
  python App/ths_sync_to_tdx.py --append     # 追加模式：在现有基础上追加

依赖：
  - App/ths_cloud_api.py（同目录，阶段 2 随本工具一并迁入 App/）
  - App/AppData.py（自选股写入原语，阶段 4 自 DataAPI/TdxAPI.py 收敛至此）
  - App/AppConfig.py（vipdoc 目录发现，阶段 2 起替代源码解析）
"""
import sys
import os

# 阶段 2：本脚本位于 App/，仓库根 = 父目录；DataAPI/ 与 App/ 包均自仓库根导入
script_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.dirname(script_dir)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
from DataAPI.TdxAPI import set_tdx_config  # 数据源配置注入（板块文件原语仍在 DataAPI）
from App.AppLog import get_logger
log = get_logger(__name__)


CONFIG_FILE = os.path.join(script_dir, ".sync_tdx_config")

# =========================================================
# 同花顺板块编码规则（88xxxx 开头为同花顺私有指数，同步时跳过）
# =========================================================
# 号段         分类                  示例
# ----------  --------------------  -----------------------
# 881xxx      行业板块               881121 半导体
# 882xxx      地域板块               882007 深圳、882010 贵州
# 884xxx      行业板块（细分）        884068 专业工程
# 885xxx      概念板块（老号段）      885312 物联网
# 886xxx      概念板块（新号段）      886078 商业航天
#
# ===============================================================================
# 通达信板块编码规则（88xxxx 开头为通达信私有指数，写入时保留不覆盖）
# ===============================================================================
# 号段               分类               说明
# ----------        ----------------   ------------------------------------------
# 8802xx            地域板块            如 880207 北京板块
# 8803xx / 8804xx   行业板块(旧)        已被 881xxx 替代
# 8805xx            概念板块            如 880545 云计算
# 8807xx            概念/风格板块（扩展） 如 880754 培育钻石(概念)、880751 昨日跌停(风格)
# 8808xx            风格板块            如 880866 近期新低
# 8809xx            概念板块（扩展）     如 880904 机器人概念、880917 央企改革
# 881xxx            研究行业            替代了 8803xx/8804xx
#
# =============================================================================================
# 两地映射关系
# =============================================================================================
# 板块类型     同花顺               通达信                      映射难度
# ----------  ------------------  ------------------         ----------------------------------
# 地域         882xxx              8802xx                     号段不同但名称一一对应，可按名称自动匹配
# 行业         881xxx / 884xxx     881xxx                     号段相同但代码不同，需静态映射
# 概念         885xxx / 886xxx     8805xx / 8807xx / 8809xx   号段完全不同，需静态映射
# 风格         无                  8807xx / 8808xx            同花顺无此分类，无需处理
#
# ==================================================================
# 处理策略
#   同花顺侧：88xxxx 开头私有指数、标准指数均跳过，不同步
#   通达信侧：88xxxx 开头私有指数、标准指数均保留不覆盖，且保持在自选股列表开头
# ==================================================================
#
# 标准指数（两边代码一致，不同步，各自保留）
# 000001 上证指数    399001 深证成指    000300 沪深300
# 000905 中证500    000852 中证1000    399006 创业板指    000688 科创50
STANDARD_INDEX_CODES = {"000001", "000300", "000905", "000852", "000688", "399001", "399006"}


def setup_tdx_config():
    """发现 vipdoc_dir 并注入 TdxAPI 配置

    阶段 2（V10 方案 8.3 ④）：vipdoc 发现统一读 App/AppConfig.py
    （环境变量 / .env / 默认值），彻底移除对 my_chan_main.py 的正则源码解析
    ——该机制随配置搬家/改写即静默失效，是本工具最脆弱的一环。

    发现顺序：
      1. AppConfig.vipdoc_dir（TDX_INSTALL_DIR 环境变量 / .env / 默认值推导）
      2. 环境变量 TDX_VIPDOC_DIR（历史直配入口，保留兼容）
      3. 本地缓存 .sync_tdx_config（旧版本工具遗留）
    """
    from App.AppConfig import app_config

    # 1. AppConfig（单一配置源）
    vipdoc_dir = app_config.vipdoc_dir
    if vipdoc_dir and os.path.isdir(vipdoc_dir):
        set_tdx_config(vipdoc_dir=vipdoc_dir)
        log.info(f"[配置] 从 AppConfig 读取: {vipdoc_dir}")
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(vipdoc_dir)
        return True

    # 2. 环境变量 TDX_VIPDOC_DIR（历史直配入口）
    vipdoc_dir = os.environ.get("TDX_VIPDOC_DIR")
    if vipdoc_dir:
        set_tdx_config(vipdoc_dir=vipdoc_dir)
        log.info(f"[配置] 从环境变量读取: {vipdoc_dir}")
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(vipdoc_dir)
        return True

    # 3. 本地缓存（旧版本工具遗留的 .sync_tdx_config）
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                vipdoc_dir = f.read().strip()
            if vipdoc_dir and os.path.isdir(vipdoc_dir):
                set_tdx_config(vipdoc_dir=vipdoc_dir)
                return True
        except Exception:
            pass

    log.warning("[警告] 未找到通达信 vipdoc 目录。请在仓库根 .env 设置 TDX_INSTALL_DIR，"
          "或设置环境变量 TDX_VIPDOC_DIR / TDX_INSTALL_DIR（详见 .env.example）")
    return False


# 标准指数在 TDX zxg.blk 中的格式（SH=1前缀, SZ=0前缀）
TDX_STANDARD_INDICES = {"1000001", "0399001", "1000300", "1000905", "1000852", "0399006", "1000688"}

# =============================================================================================
# 通达信扩展市场 zxg.blk 格式说明
# =============================================================================================
# 通达信自选股文件 zxg.blk 中，不同市场的代码格式：
#   A 股：     {市场前缀}{6位代码}        如 1600519(沪)、0000001(深)、2830067(北)
#   港股个股：  31#{5位代码}               如 31#00700
#   港股指数：  27#HZ{4位数字}             如 27#HZ5489
#   美股个股：  74#{代码}                  如 74#AAPL、74#XBI
#   美股指数：  12#A_{代码}                如 12#A_NBI
#
# 市场编号参考：通达信量化平台常量枚举
#   31=香港交易所  74=美国股票  27=港股指数  12=美股指数(桌面客户端)
# =============================================================================================
# 港股指数 同花顺代码 → 通达信内部代码映射
# 注意：同花顺 API 返回的是 HS+数字 代码（如 HS2083），不是显示名（如 HSTECH）
#       通达信港股指数使用 HZ+数字 的内部代码，两者完全不同，无法算法推导
# 如需新增映射，请在通达信中手工加入该指数后导出 zxg.blk 查看实际代码
HK_INDEX_MAP = {
    "HS2198": "HZ5489",  # 恒生港股通可投资指数（显示名 HSIDI）
    "HS2083": "HZ5017",  # 恒生科技指数（显示名 HSTECH）
}

# 美股指数（非个股） 同花顺代码 → 通达信内部代码映射
# 通达信美股指数在 zxg.blk 中带 A_ 前缀
US_INDEX_MAP = {
    "NBI": "A_NBI",      # 纳斯达克生物科技指数
}


def _read_preserved_codes(blk_path):
    """
    读取通达信 zxg.blk 中已有的保留代码（标准指数 + 88xxxx 私有指数）
    替换模式时保留，不被覆盖，并保持在文件开头
    注意：通达信 zxg.blk 中 88xxxx 代码带 SH 前缀 1，格式为 188xxxx
    :param blk_path: zxg.blk 完整路径
    :return: [保留代码列表]
    """
    if not os.path.exists(blk_path):
        return []
    codes = []
    try:
        with open(blk_path, "r", encoding="gbk") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                # 匹配: 88xxxx（无前缀）或 188xxxx（SH前缀）或 标准指数（SH/SZ前缀）
                if line.startswith("88") or line.startswith("188") or line in TDX_STANDARD_INDICES:
                    codes.append(line)
    except Exception:
        pass
    return codes


def find_tdx_zxg_path():
    """自动查找通达信自选股文件路径

    P2-8 硬编码配置化：优先读配置中心 app_config.tdx_install_dir
    （Windows 默认 C:\\new_tdx_hd_test，可由 .env / 环境变量 TDX_INSTALL_DIR 覆盖），
    不再把安装路径硬编码在候选列表首位；候选列表仅作兜底探测。
    """
    candidates = []

    # P2-8：配置中心单一事实源（Windows 默认 C:\new_tdx_hd_test）
    try:
        from App.AppConfig import app_config
        cfg_root = app_config.tdx_install_dir
        if cfg_root:
            candidates.append(cfg_root)
    except Exception:
        pass

    # 常见通达信安装路径（兜底探测，配置中心未命中时使用）
    tdx_roots = [
        r"C:\new_tdx_hd_test",
        r"C:\new_tdx_hd",
        r"D:\new_tdx_hd_test",
        r"D:\new_tdx_hd",
        r"C:\同花顺",
        r"C:\同花顺软件",
        r"D:\同花顺",
        r"D:\同花顺软件",
    ]

    for root in tdx_roots:
        if os.path.isdir(root):
            candidates.append(root)

    # 如果以上都不存在，尝试搜索注册表或常见位置
    # 这里简单处理：返回第一个存在的路径
    for root in candidates:
        zxg_dir = os.path.join(root, r"T0002\hq_block")
        if os.path.isdir(zxg_dir):
            return zxg_dir

    return None


def find_save_to_zxg_blk():
    """尝试导入 save_to_zxg_blk 函数（阶段 4 起实现收敛 App/AppData.py，
    仓库根已在模块头注入 sys.path，App 包自仓库根导入）"""
    try:
        from App.AppData import app_data   # 业务数据层：自选股存哪里、怎么存
        return app_data.save_to_zxg_blk
    except ImportError:
        return None


def _convert_codes_to_tdx_lines(codes_list):
    """
    将标准格式代码列表转换为通达信 zxg.blk 行格式。

    各市场输出格式：
      A 股：     {前缀}{6位代码}    如 1600519
      港股个股：  31#{5位代码}       如 31#00700
      港股指数：  27#{HZ代码}       如 27#HZ5489  （需 HK_INDEX_MAP 映射）
      美股个股：  74#{代码}         如 74#XBI
      美股指数：  12#A_{代码}       如 12#A_NBI   （需 US_INDEX_MAP 映射）

    :param codes_list: ["600519.SH", "000001.SZ", "00700.HK", "NBI.US", "1A0001", ...]
    :return: [通达信格式行列表]
    """
    prefix_map = {"SH": "1", "SZ": "0", "BJ": "2"}
    lines = []
    for code_full in codes_list:
        code_full = code_full.strip()
        if not code_full:
            continue
        if "." not in code_full:
            # 无后缀（如指数原始代码 1A0001、188xxxx 等），原样保留
            lines.append(code_full)
            continue
        parts = code_full.rsplit(".", 1)
        if len(parts) != 2:
            continue
        code, suffix = parts
        suffix = suffix.upper()
        code_upper = code.upper()
        if suffix == "HK":
            if code_upper in HK_INDEX_MAP:
                # 港股指数：27# + 通达信内部代码（如 27#HZ5489）
                lines.append("27#" + HK_INDEX_MAP[code_upper])
            else:
                # 港股个股：31# + 5位代码（如 31#00700）
                lines.append("31#" + code.zfill(5))
        elif suffix == "US":
            if code_upper in US_INDEX_MAP:
                # 美股指数：12# + 通达信内部代码（如 12#A_NBI）
                lines.append("12#" + US_INDEX_MAP[code_upper])
            else:
                # 美股个股：74# + 原始代码（如 74#XBI）
                lines.append("74#" + code)
        elif suffix in prefix_map:
            lines.append(f"{prefix_map[suffix]}{code}")
    return lines


def write_tdx_zxg_direct_to_file(blk_path, codes_list):
    """
    直接写入通达信自选股文件（全量覆盖，不依赖 DataAPI）

    :param blk_path: zxg.blk 的完整路径
    :param codes_list: 代码列表
    :return: 写入数量
    """
    lines = _convert_codes_to_tdx_lines(codes_list)
    if not lines:
        log.warning("[警告] 没有可写入的股票代码")
        return 0

    with open(blk_path, "w", encoding="gbk") as f:
        f.write("\n".join(lines) + "\n")

    return len(lines)


def write_tdx_zxg_direct(zxg_dir, codes_list):
    """
    直接写入通达信自选股文件（不依赖 DataAPI，旧版兼容）

    :param zxg_dir: 通达信 block 文件目录
    :param codes_list: 代码列表，支持格式：
        - 600519.SH → 1600519
        - 000001.SZ → 0000001
        - 00700.HK → HK00700
        - 1A0001（指数，无后缀）→ 1A0001
    :return: 写入数量
    """
    block_file = os.path.join(zxg_dir, "zxg.blk")
    if not os.path.isdir(zxg_dir):
        log.error(f"[错误] 通达信自选股目录不存在: {zxg_dir}")
        return 0

    return write_tdx_zxg_direct_to_file(block_file, codes_list)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="同花顺自选股 -> 通达信自选股 同步")
    parser.add_argument("--preview", action="store_true", help="仅预览，不实际写入")
    parser.add_argument("--append", action="store_true", help="追加模式（不清空，在现有基础上追加）")
    parser.add_argument("--tdx-path", type=str, default=None, help="通达信自选股目录路径（自动检测）")
    parser.add_argument("--cookie-file", type=str, default=None, help="同花顺Cookie文件路径")
    args = parser.parse_args()

    # 1. 导入同花顺 API（阶段 2：ths_cloud_api.py 与本脚本同在 App/，自仓库根以包形式导入）
    try:
        from App.ths_cloud_api import THSCloudAPI
    except ImportError:
        log.error("[错误] 找不到 App/ths_cloud_api.py，请确保 App/ 目录完整（阶段 2 已迁入 App/）")
        sys.exit(1)

    # 2. 连接同花顺
    log.info("=" * 50)
    log.info("  同花顺 -> 通达信 自选股同步")
    log.info("=" * 50)

    # 初始化通达信配置（关键：必须设置 vipdoc_dir 才能写入）
    setup_tdx_config()

    api = THSCloudAPI(cookie_file=args.cookie_file)
    if not api.check_login():
        log.error("[错误] 同花顺登录状态失效，请更新 Cookie")
        sys.exit(1)
    log.info("[OK] 同花顺登录有效")

    # 3. 获取同花顺自选股
    stocks = api.get_self_stocks()

    if not stocks:
        log.info("同花顺自选股为空，无需同步")
        return

    # 检测通达信路径和写入方式（预览时就显示，方便用户确认）
    save_fn = find_save_to_zxg_blk()
    zxg_dir = args.tdx_path or find_tdx_zxg_path()

    # 如果用了 DataAPI，从数据层获取实际写入路径（用于清空操作；阶段 4 自含）
    zxg_blk_path = None
    if save_fn:
        try:
            zxg_blk_path = app_data.zxg_blk_path
        except Exception:
            pass

    if zxg_blk_path:
        pass
    elif zxg_dir:
        log.info(f"[检测] 通达信自选股目录: {zxg_dir}")
    else:
        log.info(f"[检测] 未找到通达信自选股目录")
        log.info(f"       如需指定路径: --tdx-path C:\\new_tdx_hd_test\\T0002\\hq_block")

    # 4. 按市场分类并转换代码格式
    # marketid -> (suffix, is_index, is_index_etf)
    #   suffix=None → 指数（写入 blk 时用原始代码，不加后缀）
    #   is_index=True → A股指数（需要代码转换）
    #   is_index_etf=True → 指数或ETF（显示分类用）
    marketid_info = {
        "17": ("SH", False, False),    # 沪市
        "18": ("SH", False, False),    # 沪市(其他)
        "20": ("SH", False, True),     # 沪市(ETF等)
        "23": ("SH", False, False),    # 沪市(其他)
        "16": (None, True, True),      # 沪市指数 → 原始代码 1A0001
        "33": ("SZ", False, False),    # 深市
        "34": ("SZ", False, False),    # 深市(其他)
        "35": ("SZ", False, False),    # 深市(其他)
        "36": ("SZ", False, True),     # 深市(ETF)
        "39": ("SZ", False, False),    # 深市(其他)
        "32": (None, True, True),      # 深市指数 → 原始代码 399001
        "151": ("BJ", False, False),  # 北交所
        "87": ("BJ", False, False),   # 北交所(旧)
        "177": ("HK", False, False),  # 港股
        "178": ("HK", False, False),  # 港股
        "179": ("HK", False, False),  # 港股
        "180": ("HK", False, False),  # 港股
        "181": ("HK", False, False),  # 港股
        "182": ("HK", False, False),  # 港股
        "176": ("HK", False, True),   # 港股(指数)
        "169": ("US", False, False),  # 美股
        "UNSI": ("US", False, True),  # 美股(指数)
    }

    codes_tdx = []           # 通达信格式（带后缀的 A/港股，或原始代码的指数）
    skipped = []              # 不支持的（美股、未知市场等）
    skipped_standard = []     # 跳过的标准指数
    skipped_private = []      # 跳过的私有指数（板块/概念）
    # 按市场+类型统计（显示用）：market_key → {"stock": n, "index_etf": n}
    market_stats = {}

    for s in stocks:
        mid = s.get("marketid", "")
        code = s.get("code", "")
        name = s.get("name", "")
        info = marketid_info.get(mid)

        # 板块/概念（88xxxx 开头为同花顺私有指数），跳过
        # 必须在 info 检查之前，因为 88xxxx 可能不在 marketid_info 中
        if code.startswith("88"):
            if code not in skipped_private:
                skipped_private.append(code)
            continue

        if not info:
            # 83xxxx 开头且无 marketid 映射 → 同花顺私有指数
            if code.startswith("83"):
                if code not in skipped_private:
                    skipped_private.append(code)
                continue
            skipped.append(f"{code}(marketid={mid}, 未知)")
            continue

        suffix, is_index, is_index_etf = info

        # 港股：清理旧 K 前缀
        if suffix == "HK" and code.startswith("K"):
            code = code[1:]
        if suffix == "HK" and code.startswith("HK"):
            code = code[2:]

        if is_index:
            # 指数：同花顺内部代码 → 交易所标准代码
            # 1A0001→000001.SH, 1B0300→000300.SH, 399001→399001.SZ
            if code.startswith("1A") or code.startswith("1B"):
                code = "00" + code[2:]   # 1A0001→000001, 1B0300→000300
                suffix = "SH"
            elif mid == "32":
                suffix = "SZ"            # 399001→399001.SZ
            else:
                suffix = "SH"
            full_code = f"{code}.{suffix}"
        else:
            full_code = f"{code}.{suffix}"

        # 标准指数（上证指数、深证成指、沪深300 等）不同步
        # 注意：仅当是指数(is_index=True)时才跳过，避免误判同名个股（如 000001 平安银行）
        if is_index and code in STANDARD_INDEX_CODES:
            if code not in skipped_standard:
                skipped_standard.append(code)
            continue

        codes_tdx.append(full_code)
        # 统计：按市场+类型（个股/指数等）
        mkt_key = suffix  # is_index block 已将 suffix 转为 SH/SZ
        cat = "index_etf" if is_index_etf else "stock"
        market_stats.setdefault(mkt_key, {"stock": 0, "index_etf": 0})[cat] += 1

    # 显示预览
    _MARKET_DISPLAY = [
        ("SH", "沪市"),
        ("SZ", "深市"),
        ("BJ", "北交所"),
        ("HK", "港股"),
        ("US", "美股"),
    ]
    parts = []
    for mkt_key, mkt_name in _MARKET_DISPLAY:
        stats = market_stats.get(mkt_key)
        if not stats:
            continue
        stock_n = stats["stock"]
        idx_n = stats["index_etf"]
        if stock_n:
            parts.append(f"{mkt_name}：{stock_n}只")
        if idx_n:
            parts.append(f"{mkt_name}(指数等)：{idx_n}只")
    log.info(f"[获取] 同花顺自选股: {len(stocks)} 只（{'   '.join(parts)}）")

    # 读取通达信侧保留代码（标准指数 + 88xxxx 私有指数）
    preserved = []
    if zxg_blk_path:
        preserved = _read_preserved_codes(zxg_blk_path)
    elif zxg_dir:
        preserved = _read_preserved_codes(os.path.join(zxg_dir, "zxg.blk"))

    # 构建跳过信息
    skip_parts = []
    if skipped_standard:
        skip_parts.append(f"{len(skipped_standard)}只（标准指数：{', '.join(skipped_standard)}）")
    if skipped_private:
        skip_parts.append(f"{len(skipped_private)}只（私有指数：{', '.join(skipped_private)}）")
    if skipped:
        unsupported_codes = [s.split("(")[0] for s in skipped]
        skip_parts.append(f"{len(unsupported_codes)}只（不支持：{', '.join(unsupported_codes)}）")
    if skip_parts:
        log.info(f"[跳过] {' + '.join(skip_parts)}")

    # 去重
    codes_tdx = list(dict.fromkeys(codes_tdx))

    log.info(f"合计：可同步{len(codes_tdx)}只 到 通达信自选股{zxg_blk_path or os.path.join(zxg_dir, 'zxg.blk')}")

    if preserved:
        log.info(f"[保留] 通达信保留{len(preserved)}只：标准指数 + 私有指数（88xxxx）")

    if args.preview:
        log.info("[预览模式] 不写入文件")
        return

    # 5. 写入通达信（默认替换模式：清空后再写入，但保留标准指数和 88xxxx 私有指数）
    replace = not args.append  # 默认替换，--append 时不清空

    if save_fn:
        codes_to_write = [c for c in codes_tdx if "." not in c or c.rsplit(".", 1)[1] in ("SH", "SZ", "BJ", "HK", "US")]
        if replace and zxg_blk_path:
            all_codes = preserved + codes_to_write
            all_codes = list(dict.fromkeys(all_codes))
            os.makedirs(os.path.dirname(zxg_blk_path), exist_ok=True)
            write_tdx_zxg_direct_to_file(zxg_blk_path, all_codes)
        else:
            save_fn(codes_to_write)
    else:
        if not zxg_dir:
            log.error("[错误] 未找到通达信自选股目录")
            log.info("  请用 --tdx-path 指定")
            sys.exit(1)
        if replace:
            block_file = os.path.join(zxg_dir, "zxg.blk")
            codes_to_write = [c for c in codes_tdx if "." not in c or c.rsplit(".", 1)[1] in ("SH", "SZ", "BJ", "HK", "US")]
            all_codes = preserved + codes_to_write
            all_codes = list(dict.fromkeys(all_codes))
            write_tdx_zxg_direct_to_file(block_file, all_codes)
        else:
            write_tdx_zxg_direct(zxg_dir, codes_to_write)


if __name__ == "__main__":
    main()
