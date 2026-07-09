# -*- coding: utf-8 -*-
"""
同花顺自选股 -> 通达信自选股 同步工具

用法：
  python sync_ths_to_tdx.py              # 默认替换模式：清空通达信自选股后写入
  python sync_ths_to_tdx.py --preview    # 仅预览，不实际写入
  python sync_ths_to_tdx.py --append     # 追加模式：在现有基础上追加

依赖：
  - ths_cloud_api.py（同目录）
  - DataAPI/TdxAPI.py
"""
import sys
import os
import re

# 导入通达信 API 设置配置
script_dir = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, script_dir)
from DataAPI.TdxAPI import set_tdx_config

CONFIG_FILE = os.path.join(script_dir, ".sync_tdx_config")


def setup_tdx_config():
    """从多个来源读取 vipdoc_dir 配置，找到后缓存到本地文件"""
    vipdoc_dir = None

    # 1. 尝试从本地缓存文件读取（上次保存的）
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                vipdoc_dir = f.read().strip()
            if vipdoc_dir and os.path.isdir(vipdoc_dir):
                set_tdx_config(vipdoc_dir=vipdoc_dir)
                print(f"[配置] 从缓存读取: {vipdoc_dir}")
                return True
        except Exception:
            pass

    # 2. 尝试从环境变量读取
    vipdoc_dir = os.environ.get("TDX_VIPDOC_DIR")
    if vipdoc_dir:
        set_tdx_config(vipdoc_dir=vipdoc_dir)
        print(f"[配置] 从环境变量读取: {vipdoc_dir}")
        # 缓存到本地
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(vipdoc_dir)
        return True

    # 3. 从 my_chan_main.py 解析 VIPDOC_DIR
    main_file = os.path.join(script_dir, "my_chan_main.py")
    if os.path.exists(main_file):
        try:
            with open(main_file, "r", encoding="utf-8") as f:
                for line in f:
                    m = re.search(r'VIPDOC_DIR\s*=\s*.*?["\'](.+?)["\']', line)
                    if m:
                        vipdoc_dir = m.group(1)
                        set_tdx_config(vipdoc_dir=vipdoc_dir)
                        print(f"[配置] 从 my_chan_main.py 读取: {vipdoc_dir}")
                        # 缓存到本地
                        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                            f.write(vipdoc_dir)
                        return True
        except Exception:
            pass

    print("[警告] 未找到通达信配置，请设置环境变量: $env:TDX_VIPDOC_DIR = \"C:\\new_tdx_test\\vipdoc\"")
    return False


def find_tdx_zxg_path():
    """自动查找通达信自选股文件路径"""
    candidates = []

    # 常见通达信安装路径
    tdx_roots = [
        r"C:\new_tdx",
        r"C:\tdx",
        r"D:\new_tdx",
        r"D:\tdx",
        r"C:\Program Files\new_tdx",
        r"C:\Program Files (x86)\new_tdx",
        r"C:\同花顺",
        r"D:\同花顺",
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
    """尝试导入 save_to_zxg_blk 函数"""
    # 方法1：从 DataAPI.TdxAPI 导入（chan 项目结构）
    try:
        # 将脚本所在目录加入 sys.path
        script_dir = os.path.dirname(os.path.abspath(__file__))
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        from DataAPI.TdxAPI import save_to_zxg_blk
        return save_to_zxg_blk
    except ImportError:
        pass

    # 方法2：如果 ths_cloud_api.py 和 my_chan_main.py 在同目录，从那里找
    try:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        parent_dir = os.path.dirname(script_dir)
        if parent_dir not in sys.path:
            sys.path.insert(0, parent_dir)
        from DataAPI.TdxAPI import save_to_zxg_blk
        return save_to_zxg_blk
    except ImportError:
        pass

    return None


def _convert_codes_to_tdx_lines(codes_list):
    """
    将标准格式代码列表转换为通达信 zxg.blk 行格式
    :param codes_list: ["600519.SH", "000001.SZ", "00700.HK", "1A0001", ...]
    :return: [通达信格式行列表]
    """
    prefix_map = {"SH": "1", "SZ": "0", "BJ": "2"}
    lines = []
    for code_full in codes_list:
        code_full = code_full.strip()
        if not code_full:
            continue
        if "." not in code_full:
            lines.append(code_full)
            continue
        parts = code_full.rsplit(".", 1)
        if len(parts) != 2:
            continue
        code, suffix = parts
        suffix = suffix.upper()
        if suffix == "HK":
            lines.append(f"31#{code.zfill(5)}")  # 通达信原生格式：31#00700
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
        print("[警告] 没有可写入的股票代码")
        return 0

    with open(blk_path, "w", encoding="gbk") as f:
        f.write("\n".join(lines) + "\n")

    print(f"[保存] 全量写入 {len(lines)} 只到 {blk_path}")
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
        print(f"[错误] 通达信自选股目录不存在: {zxg_dir}")
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

    # 1. 导入同花顺 API
    script_dir = os.path.dirname(os.path.abspath(__file__))
    if script_dir not in sys.path:
        sys.path.insert(0, script_dir)

    try:
        from ths_cloud_api import THSCloudAPI
    except ImportError:
        print("[错误] 找不到 ths_cloud_api.py，请确保与脚本在同一目录")
        sys.exit(1)

    # 2. 连接同花顺
    print("=" * 50)
    print("  同花顺 -> 通达信 自选股同步")
    print("=" * 50)

    # 初始化通达信配置（关键：必须设置 vipdoc_dir 才能写入）
    setup_tdx_config()

    api = THSCloudAPI(cookie_file=args.cookie_file)
    if not api.check_login():
        print("[错误] 同花顺登录状态失效，请更新 Cookie")
        sys.exit(1)
    print("[OK] 同花顺登录有效\n")

    # 3. 获取同花顺自选股
    stocks = api.get_self_stocks()
    print(f"[获取] 同花顺自选股: {len(stocks)} 只")

    if not stocks:
        print("同花顺自选股为空，无需同步")
        return

    # 检测通达信路径和写入方式（预览时就显示，方便用户确认）
    save_fn = find_save_to_zxg_blk()
    zxg_dir = args.tdx_path or find_tdx_zxg_path()

    # 如果用了 DataAPI，从 TdxAPI 内部获取实际写入路径（用于清空操作）
    zxg_blk_path = None
    if save_fn:
        try:
            from DataAPI.TdxAPI import get_blk_path
            zxg_blk_path = get_blk_path("zxg")
        except Exception:
            pass

    print()
    if save_fn:
        print(f"[检测] 找到 DataAPI.save_to_zxg_blk（与股票扫描共用同一个写入函数）")
        if zxg_blk_path:
            print(f"[检测] 通达信自选股文件: {zxg_blk_path}")
    elif zxg_dir:
        print(f"[检测] 通达信自选股目录: {zxg_dir}")
    else:
        print(f"[检测] 未找到通达信自选股目录")
        print(f"       如需指定路径: --tdx-path C:\\new_tdx\\T0002\\hq_block")

    # 4. 按市场分类并转换代码格式
    # marketid -> (suffix, is_index)
    #   suffix=None → 指数（写入 blk 时用原始代码，不加后缀）
    #   suffix="HK" → 港股
    #   suffix="SH"/"SZ"/"BJ" → A股
    #   suffix="US" → 美股（不支持，跳过）
    marketid_info = {
        "17": ("SH", False),   # 沪市
        "18": ("SH", False),   # 沪市(其他)
        "20": ("SH", False),   # 沪市(ETF等)
        "23": ("SH", False),   # 沪市(其他)
        "16": (None, True),    # 沪市指数 → 原始代码 1A0001
        "33": ("SZ", False),   # 深市
        "34": ("SZ", False),   # 深市(其他)
        "35": ("SZ", False),   # 深市(其他)
        "39": ("SZ", False),   # 深市(其他)
        "32": (None, True),    # 深市指数 → 原始代码 399001
        "151": ("BJ", False),  # 北交所
        "87": ("BJ", False),   # 北交所(旧)
        "177": ("HK", False),  # 港股
        "178": ("HK", False),  # 港股
        "179": ("HK", False),  # 港股
        "180": ("HK", False),  # 港股
        "181": ("HK", False),  # 港股
        "182": ("HK", False),  # 港股
        "176": ("HK", False),  # 港股(指数)
        "169": ("US", False),  # 美股（不支持）
    }

    codes_tdx = []  # 通达信格式（带后缀的 A/港股，或原始代码的指数）
    skipped = []
    by_market = {}

    for s in stocks:
        mid = s.get("marketid", "")
        code = s.get("code", "")
        info = marketid_info.get(mid)

        if not info:
            skipped.append(f"{code}(marketid={mid}, 未知)")
            continue

        suffix, is_index = info

        # 跳过板块/概念（marketid=48）
        if mid == "48":
            skipped.append(f"{code}(板块/概念)")
            continue

        # 美股不支持
        if suffix == "US":
            skipped.append(f"{code}(美股)")
            continue

        # 港股：跳过港股指数（HS* 开头），清理旧 K 前缀
        if suffix == "HK" and mid == "176":
            if code.startswith("HS") or code.startswith("HSI") or code.startswith("HSC") or code.startswith("HSH"):
                skipped.append(f"{code}(港股指数)")
                continue
        if suffix == "HK" and code.startswith("K"):
            code = code[1:]
            print(f"[修复] 港股代码: 旧格式 K -> {code}")
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

        codes_tdx.append(full_code)
        market_label = api.MARKET_ID.get(mid, mid)
        by_market.setdefault(market_label, []).append(full_code)

    # 显示预览
    print()
    for label in sorted(by_market.keys()):
        items = by_market[label]
        print(f"  [{label}] {len(items)} 只: {', '.join(items)}")
    if skipped:
        print(f"\n  [跳过] {len(skipped)} 只（指数/板块/不支持的）: {', '.join(skipped[:10])}")
        if len(skipped) > 10:
            print(f"         ... 等共 {len(skipped)} 只")

    print(f"\n  合计: 可同步 {len(codes_tdx)} 只, 跳过 {len(skipped)} 只")

    if args.preview:
        print("\n[预览模式] 不写入文件")
        return

    # 5. 写入通达信（默认替换模式：清空后再写入）
    codes_tdx = list(dict.fromkeys(codes_tdx))
    replace = not args.append  # 默认替换，--append 时不清空

    if save_fn:
        # 过滤：A股(.SH/.SZ/.BJ)、港股(.HK)、指数(原始代码，无后缀)
        codes_to_write = [c for c in codes_tdx if "." not in c or c.rsplit(".", 1)[1] in ("SH", "SZ", "BJ", "HK")]
        if replace and zxg_blk_path:
            # 替换模式：直接全量写入，绕过 save_to_zxg_blk 的追加+去重逻辑
            # save_to_zxg_blk 内部用追加模式打开文件，如果清空操作和它之间
            # 有任何时序问题，或者它读到的路径和我们清空的不一致，就会导致
            # 旧数据残留。直接写入可以完全避免这个问题。
            print(f"[写入] 替换模式：直接全量写入 {zxg_blk_path}")
            os.makedirs(os.path.dirname(zxg_blk_path), exist_ok=True)
            written = write_tdx_zxg_direct_to_file(zxg_blk_path, codes_to_write)
        else:
            print(f"[写入] 使用 DataAPI.save_to_zxg_blk（追加模式）")
            written = save_fn(codes_to_write)
        print(f"[完成] 写入 {written} 只到通达信自选股")
    else:
        if not zxg_dir:
            print("[错误] 未找到通达信自选股目录")
            print("  请用 --tdx-path 指定")
            sys.exit(1)
        if replace:
            block_file = os.path.join(zxg_dir, "zxg.blk")
            if os.path.exists(block_file):
                with open(block_file, "w", encoding="gbk") as f:
                    f.write("")
                print(f"[写入] 已清空 {block_file}")
        written = write_tdx_zxg_direct(zxg_dir, codes_to_write)
        print(f"[完成] 写入 {written} 只到通达信自选股")

    print("\n[提示] 请在通达信中刷新自选股查看")


if __name__ == "__main__":
    main()
