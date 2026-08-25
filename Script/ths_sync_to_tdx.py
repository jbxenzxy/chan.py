# -*- coding: utf-8 -*-
"""
同花顺自选股 -> 通达信自选股 同步工具

用法：
  python Script/ths_sync_to_tdx.py              # 默认替换模式：清空通达信自选股后写入
  python Script/ths_sync_to_tdx.py --preview    # 仅预览，不实际写入
  python Script/ths_sync_to_tdx.py --append     # 追加模式：在现有基础上追加

依赖：
  - DataAPI/ThsCloudZxgAPI.py（同花顺云端自选股 API，DataAPI 层，自仓库根以包形式导入）
  - App/AppData.py（app_data 单一写入源：zxg_blk_path 路径推导 + sync_zxg_blk 全量同步）
"""
import sys
import os

# P2-2：本脚本位于 Script/，仓库根 = 父目录；DataAPI/ 与 App/ 包均自仓库根导入
script_dir = os.path.dirname(os.path.abspath(__file__))
repo_root = os.path.dirname(script_dir)
if repo_root not in sys.path:
    sys.path.insert(0, repo_root)
from App.AppLog import get_logger
log = get_logger(__name__)
from App.AppData import app_data  # 自选股读写单一写入源（收敛：不再在本脚本内重复实现）

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


# =============================================================================
# 通达信自选股 zxg.blk 格式说明（写盘格式知识已在 App/AppData.py 收敛为单一源）
# =============================================================================
# 通达信自选股文件 zxg.blk 中，不同市场的代码格式：
#   A 股：     {市场前缀}{6位代码}        如 1600519(沪)、0000001(深)、2830067(北)
#   港股个股：  31#{5位代码}               如 31#00700
#   港股指数：  27#HZ{4位数字}             如 27#HZ5489
#   美股个股：  74#{代码}                  如 74#AAPL
#   美股指数：  12#A_{代码}                如 12#A_NBI
#
# 市场编号参考：通达信量化平台常量枚举
#   31=香港交易所  74=美国股票  27=港股指数  12=美股指数(桌面客户端)
#
# 港股指数（HS→HZ）与美股指数（A_ 前缀）的同花顺→通达信映射、以及标准指数
# 保留（TDX_STANDARD_INDICES），统一由 App/AppData.py 持有并用于写入。
# =============================================================================


def main():
    import argparse

    parser = argparse.ArgumentParser(description="同花顺自选股 -> 通达信自选股 同步")
    parser.add_argument("--preview", action="store_true", help="仅预览，不实际写入")
    parser.add_argument("--append", action="store_true", help="追加模式（不清空，在现有基础上追加）")
    parser.add_argument("--tdx-path", type=str, default=None, help="通达信自选股目录路径（自动检测）")
    parser.add_argument("--cookie-file", type=str, default=None, help="同花顺Cookie文件路径")
    args = parser.parse_args()

    # 1. 导入同花顺 API（DataAPI/ThsCloudZxgAPI.py，自仓库根以包形式导入）
    try:
        from DataAPI.ThsCloudZxgAPI import CThsCloudZxg
    except ImportError:
        log.error("[错误] 找不到 DataAPI/ThsCloudZxgAPI.py，请确保 DataAPI/ 目录完整")
        sys.exit(1)

    # 2. 连接同花顺
    log.info("=" * 50)
    log.info("  同花顺 -> 通达信 自选股同步")
    log.info("=" * 50)

    api = CThsCloudZxg(cookie_file=args.cookie_file)
    if not api.check_login():
        log.error("[错误] 同花顺登录状态失效，请更新 Cookie")
        sys.exit(1)
    log.info("[OK] 同花顺登录有效")

    # 3. 获取同花顺自选股
    stocks = api.get_self_stocks()

    if not stocks:
        log.info("同花顺自选股为空，无需同步")
        return

    # 权威写入目标：非专业版通达信自选股固定位于 <安装目录>/T0002/blocknew/zxg.blk
    # （普通版不存在 T0002/hq_block 目录；路径由 app_data.zxg_blk_path 单一事实源推导）
    auto_blk = app_data.zxg_blk_path

    # --tdx-path 显式覆盖（两种传法均兼容：指向 zxg.blk 文件，或含它的目录）
    if args.tdx_path:
        p = args.tdx_path.rstrip("\\/")
        blk_path = p if p.lower().endswith(".blk") else os.path.join(p, "zxg.blk")
    else:
        blk_path = auto_blk

    if not blk_path:
        log.error("[错误] 未找到通达信安装目录，无法推导自选股路径；请用 --tdx-path 指定 zxg.blk")
        sys.exit(1)
    log.info(f"[检测] 通达信自选股文件: {blk_path}")

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

    # 读取通达信侧保留代码（标准指数 + 88xxxx 私有指数），来自权威 blk 文件
    preserved = app_data.read_preserved_zxg_lines(blk_path)

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

    log.info(f"合计：可同步{len(codes_tdx)}只 到 通达信自选股{blk_path}")

    if preserved:
        log.info(f"[保留] 通达信保留{len(preserved)}只：标准指数 + 私有指数（88xxxx）")

    if args.preview:
        log.info("[预览模式] 不写入文件")
        return

    # 5. 写入通达信（单一写入源 app_data.sync_zxg_blk）
    #    replace（默认）：清空重写但保留标准指数/私有指数；--append 时在现有基础上追加
    #    （去重 / 代码格式转换 / 指数保留均收敛于 AppData）
    valid_suffix = ("SH", "SZ", "BJ", "HK", "US")
    codes_to_write = [c for c in codes_tdx if "." not in c or c.rsplit(".", 1)[1] in valid_suffix]

    app_data.sync_zxg_blk(codes_to_write, path=blk_path, append=args.append)


if __name__ == "__main__":
    main()
