"""
缠论分析 - chan.py 版本
基于 https://github.com/Vespa314/chan.py 实现
功能：读取通达信本地K线数据，进行缠论分析，生成K线图网页
"""

import sys
import os
import json
import time
import struct
import re
import threading
import multiprocessing
from datetime import datetime, timedelta
from chinese_calendar import is_holiday
from http.server import HTTPServer, SimpleHTTPRequestHandler
from socketserver import ThreadingMixIn


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True
from urllib.parse import urlparse, parse_qs

# 区间套辅助函数（已搬迁至 BSPointList.py，红框功能复用）
from BuySellPoint.BSPointList import _get_main_bi_time_range, _stocks_red_range, _futures_red_range, _find_sub_bi_sequence, _find_sub_zs

# ============================================================
# 内存监控工具
# ============================================================
def get_memory_info():
    """获取当前进程内存占用（跨平台）"""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        mem_info = process.memory_info()
        return mem_info.rss / (1024 * 1024)  # 转换为 MB
    except ImportError:
        try:
            import resource
            rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            # macOS 返回的是字节，Linux 返回的是 KB
            if sys.platform == "darwin":
                return rss / (1024 * 1024)
            else:
                return rss / 1024
        except Exception:
            return None


_memory_print_count = 0
_memory_baseline = None
_freq_order = {'w': 0, 'd': 1, '30m': 2, '5m': 3}


def print_memory(label="当前"):
    """打印内存占用信息（带递增计数器、相对基线增量、缓存统计）"""
    pass  # 调试阶段已结束，关闭内存监控输出

# ============================================================
# 配置区域 - 请根据你的实际环境修改
# ============================================================
VIPDOC_DIR = r"C:\new_tdx_test\vipdoc"  # 通达信vipdoc目录
TDX_HQ_CACHE = r"C:\new_tdx_test\T0002\hq_cache"  # 通达信hq_cache目录（shm.tnf/szm.tnf）
OUTPUT_DIR = os.path.dirname(os.path.abspath(__file__))  # 输出目录（脚本所在目录）
SYMBOL_CODE = "SH000001"  # 默认股票代码（上证指数）
SYMBOL_DISPLAY = "上证指数"
CHAN_PATH = r"C:\my_chan_project"  # chan.py 仓库解压目录
THS_DIR = r"C:\同花顺软件\同花顺"  # 同花顺安装目录，留空则不启用同花顺自选股同步（如 r"D:\同花顺软件\同花顺"）
_LAST_STOCK_FILE = os.path.join(VIPDOC_DIR, "last_stock.json")  # 持久化上次查看的股票代码

# ============================================================
# 天勤期货/期指行情配置
# ============================================================
# 账户和密码从 C:\new_tdx_test\vipdoc\tq_account.json 文件读取
# 文件格式: {"account": "手机号或用户名", "password": "密码"}
TQ_ENABLED  = True          # 是否启用期货实时行情（设为 False 则只保留股票功能）
_SSE_DEBUG  = False         # SSE 推送详细调试日志开关（设为 True 可恢复调试输出）

# 将 chan.py 和当前脚本目录都添加到搜索路径
if CHAN_PATH not in sys.path:
    sys.path.insert(0, CHAN_PATH)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# ============================================================
# 导入 chan.py 核心模块
# ============================================================
try:
    from Chan import CChan
    from ChanConfig import CChanConfig
    from Common.CEnum import AUTYPE, DATA_SRC, KL_TYPE, BI_DIR, FX_TYPE, BSP_TYPE
    from Common.CTime import CTime
    from KLine.KLine_Unit import CKLine_Unit
    from KLine.KLine_List import CKLine_List
    from DataAPI.CommonStockAPI import CCommonStockApi
    CHAN_AVAILABLE = True
    #print("\n[stock][信息] https://github.com/Vespa314/chan.py 导入成功！！")
except ImportError as e:
    CHAN_AVAILABLE = False
    print(f"\n[错误] chan.py 导入失败: {e}")
    print(f"[提示] 请确保 CHAN_PATH = r'{CHAN_PATH}' 指向正确的 chan.py 仓库目录")
    sys.exit(1)

# 导入通达信数据源适配器（从 chan.py 的 DataAPI 目录）
# 包含：K线读取、前复权、流通股本
from DataAPI.TdxAPI import CTdxAPI, set_tdx_config, read_tdx_day_file, read_tdx_min_file, \
    _resample_5m_to_30m, _resample_day_to_week, find_day_file, \
    read_main_level_records, read_sub_level_records, \
    _forward_adjust, get_float_shares_from_xdxr, \
    read_zxg_stocks, read_zz1000_stocks, save_to_zxg_blk, \
    read_sz50_stocks, read_hs300_stocks, read_zz500_stocks

# 前复权开关：True=开启前复权（消除分红送股的跳空缺口），False=关闭（不复权，原样输出）
FORWARD_ADJUST_ENABLED = True

# 调试模式：冷启动只从指定日期开始加载(所有周期有效)，None表示不开启。如果该日期前无通达信数据，则有多少加载多少
DEBUG_COLD_START_START_DATE = None # "2024-09-10"

# 调试模式：用于解决冷启动起不来的问题；冷启动加载到此日期(仅日K生效)，None表示不开启
DEBUG_COLD_START_END_DATE = None # 示例: "2026-06-29" 北方国际

# 注入通达信数据源配置到 TdxAPI 模块
from DataAPI.TdxAPI import set_tdx_config as _set_tdx_config
_set_tdx_config(
    vipdoc_dir=VIPDOC_DIR,
    forward_adjust_enabled=FORWARD_ADJUST_ENABLED,
)

# 全量数据模式：True=加载全部K线不做时间截断；False=默认模式
FULL_DATA_MODE = False

# 时间截断配置（仅 FULL_DATA_MODE=False 时生效）：{周期: (天数, 显示文本)}
TIME_TRUNCATE_CONFIG = {
    '30m': (180, "6个月"),
    '5m': (90, "3个月"),
}

# 导入天勤数据源适配器（期货/期指）
try:
    from DataAPI.TqSdkAPI import CTqSdkAPI, fetch_futures_kline, FREQ_SEC_MAP, FUTURES_ALIASES, \
        _get_futures_code, _get_futures_name, load_tq_account
    load_tq_account(VIPDOC_DIR)
    TQ_AVAILABLE = True
except ImportError as e:
    CTqSdkAPI = None
    FUTURES_ALIASES = {}
    TQ_AVAILABLE = False
    print(f"[stock][警告] 天勤数据源未安装: {e}，期货功能不可用。pip install tqsdk")


# ============================================================
# 同花顺自选股同步
# ============================================================

def _find_ths_user_dir(ths_dir):
    """在 THS_DIR 下查找包含 同花顺方案/hexin.ini 的用户目录，跳过游客"""
    if not ths_dir or not os.path.isdir(ths_dir):
        return None
    candidates = []
    for root, dirs, files in os.walk(ths_dir):
        depth = root[len(ths_dir):].count(os.sep)
        if depth > 2:
            continue
        dir_name = os.path.basename(root)
        if 'guest' in dir_name.lower() or 'demo' in dir_name.lower() or 'shared' in dir_name.lower():
            continue
        hexin_path = os.path.join(root, "同花顺方案", "hexin.ini")
        if os.path.exists(hexin_path):
            try:
                with open(hexin_path, 'r', encoding='gbk') as f:
                    content = f.read()
                idx = content.find('ADDTIME=')
                if idx >= 0:
                    end = content.find('\n', idx)
                    line = content[idx:end] if end >= 0 else content[idx:]
                    count = line.count('|')
                    candidates.append((root, count))
                    print(f"[THS] 候选目录: {dir_name} ({count}只)")
                else:
                    candidates.append((root, 0))
                    print(f"[THS] 候选目录: {dir_name} (0只)")
            except Exception:
                candidates.append((root, 0))
                print(f"[THS] 候选目录: {dir_name} (读取失败)")
    if not candidates:
        return None
    candidates.sort(key=lambda x: x[1], reverse=True)
    best = candidates[0][0]
    print(f"[THS] 选定目录: {os.path.basename(best)} ({candidates[0][1]}只)")
    return best


def _strip_market_suffix(code):
    """去掉 .SH/.SZ/.BJ 后缀，返回纯6位代码"""
    return code.split('.')[0] if '.' in code else code


def _get_ths_market_id(code):
    """根据股票代码获取同花顺市场代码 M：沪市17、深市33、北交所151"""
    if not code or len(code) < 6:
        return "17"
    c = code.strip()
    if c.startswith('6') or c.startswith('688') or c.startswith('689') or c.startswith('510') or c.startswith('511') or c.startswith('512') or c.startswith('513') or c.startswith('515') or c.startswith('518') or c.startswith('560') or c.startswith('561') or c.startswith('563') or c.startswith('564') or c.startswith('580') or c.startswith('582'):
        return "17"   # 沪市（含主板、科创板、沪市ETF等）
    elif c.startswith('0') or c.startswith('3') or c.startswith('1') or c.startswith('159') or c.startswith('16'):
        return "33"   # 深市（含主板、创业板、深市ETF/LOF等）
    elif c.startswith('8') or c.startswith('4') or c.startswith('43'):
        return "151"  # 北交所/新三板
    else:
        return "17"   # 默认沪市


def _write_file_with_retry(file_path, write_func, max_retries=5):
    """带重试的文件写入，解决同花顺运行时文件锁定问题"""
    import time as _time
    import os as _os
    tmp_path = file_path + '.tmp'
    for attempt in range(max_retries):
        try:
            write_func(tmp_path)
            _os.replace(tmp_path, file_path)
            return True, ""
        except PermissionError as e:
            if attempt < max_retries - 1:
                wait_sec = 0.5 * (attempt + 1)
                print(f"[THS] 文件被锁定，{wait_sec}s 后重试 ({attempt+1}/{max_retries})...")
                _time.sleep(wait_sec)
            else:
                msg = f"文件被同花顺锁定，无法写入。请关闭同花顺后重试。错误: {e}"
                try:
                    _os.remove(tmp_path)
                except Exception:
                    pass
                return False, msg
        except Exception as e:
            msg = f"写入失败: {e}"
            try:
                _os.remove(tmp_path)
            except Exception:
                pass
            return False, msg
    return False, ""


def save_to_ths_zxg(codes):
    """保存股票代码到同花顺自选股。
    核心逻辑：同时写入 stockblock.ini 21=自选股（页面显示的关键）、
    SelfStockInfo.json 和 hexin.ini，确保各文件同步。
    """
    import shutil
    import json as _json

    print(f"[THS] 保存自选股，输入代码: {codes}")
    if not THS_DIR:
        print(f"[THS] THS_DIR 未配置，跳过")
        return 0, "THS_DIR 未配置"
    print(f"[THS] THS_DIR={THS_DIR}")
    user_dir = _find_ths_user_dir(THS_DIR)
    if not user_dir:
        msg = f"未找到同花顺用户目录，请检查 THS_DIR={THS_DIR}"
        print(f"[THS] {msg}")
        return 0, msg
    print(f"[THS] 用户目录: {user_dir}")

    # 统一处理代码：去市场后缀、去重
    unique_codes = []
    seen = set()
    for code in codes:
        c = _strip_market_suffix(code.strip())
        if c and c not in seen:
            unique_codes.append(c)
            seen.add(c)

    block_added = 0
    json_added = 0
    hexin_added = 0

    # ========== 1. stockblock.ini（页面显示的关键）==========
    block_path = os.path.join(user_dir, "stockblock.ini")
    if not os.path.exists(block_path):
        block_path = os.path.join(user_dir, "同花顺方案", "stockblock.ini")
    if os.path.exists(block_path):
        block_added, _ = _save_to_ths_stockblock(unique_codes, user_dir)
    else:
        print(f"[THS] stockblock.ini 不存在，跳过")

    # ========== 2. SelfStockInfo.json ==========
    json_candidates = ["SelfStockInfo.json", "SelfStockCache.json"]
    for name in json_candidates:
        p = os.path.join(user_dir, name)
        if os.path.exists(p):
            json_added, _ = _save_to_ths_json(unique_codes, p)
            break

    # ========== 3. hexin.ini（兼容旧版）==========
    hexin_path = os.path.join(user_dir, "同花顺方案", "hexin.ini")
    if os.path.exists(hexin_path):
        hexin_added, _ = _save_to_ths_hexin(unique_codes, user_dir)

    # 以 SelfStockInfo.json 的结果为准（真正数据源），stockblock.ini 仅作辅助尝试
    total_added = json_added
    details = []
    if block_added > 0:
        details.append(f"block+{block_added}")
    if json_added > 0:
        details.append(f"json+{json_added}")
    if hexin_added > 0:
        details.append(f"hexin+{hexin_added}")

    if total_added > 0:
        print(f"[THS] 保存结果: 共新增 {total_added} 只 ({', '.join(details)})")
        return total_added, "ok"
    else:
        print(f"[THS] 保存结果: 无新增 ({', '.join(details) if details else '全部已存在'})")
        return 0, "ok"


def _save_to_ths_json(codes, json_path):
    """写入同花顺新版 SelfStockInfo.json，同时修复已有条目的格式"""
    import json as _json
    import time as _time

    # 读取现有 JSON
    data = []
    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            data = _json.load(f)
        if not isinstance(data, list):
            data = []
    except Exception as e:
        print(f"[THS] 读取 JSON 警告: {e}，将创建新列表")
        data = []

    # 获取已有代码集合 (C+M 组合去重)，同时修复已有条目的格式
    existing = set()
    fixed = 0
    today = __import__('datetime').datetime.now().strftime('%Y%m%d')
    for item in data:
        if isinstance(item, dict):
            c = item.get('C', '')
            m = item.get('M', '')
            if c:
                existing.add(f"{c}:{m}")
                # 修复格式：补充缺失的 P 和 T 字段
                if 'P' not in item or item.get('P', '') == '':
                    item['P'] = "0.00"
                    fixed += 1
                if 'T' not in item or item.get('T', '') == '':
                    item['T'] = today
                    fixed += 1

    # 添加新代码（格式必须和同花顺原生一致：紧凑JSON，含P和T字段）
    added = 0
    for code in codes:
        code = _strip_market_suffix(code.strip())
        if not code:
            continue
        market = _get_ths_market_id(code)
        key = f"{code}:{market}"
        if key in existing:
            print(f"[THS] 跳过 {code}（已存在）")
            continue
        # 同花顺原生格式: {"C":"600519","M":"17","P":"0.00","T":"20260705"}
        # P字段不能留空，否则同花顺可能跳过该条目
        new_item = {"C": code, "M": market, "P": "0.00", "T": today}
        data.append(new_item)
        existing.add(key)
        print(f"[THS] 新增 {code} (M={market}, T={today})")
        added += 1

    if added == 0 and fixed == 0:
        print(f"[THS] 无新增股票，无需修复格式")
        return 0, "ok"

    def do_write(tmp_path):
        # 必须使用紧凑格式（无空格、无缩进），和原生文件一致
        with open(tmp_path, 'w', encoding='utf-8') as f:
            _json.dump(data, f, ensure_ascii=False, separators=(',', ':'))

    ok, msg = _write_file_with_retry(json_path, do_write)
    if ok:
        print(f"[THS] SelfStockInfo.json 写入成功，新增 {added} 只，修复 {fixed} 条格式，共 {len(data)} 只")
        return added, "ok"
    else:
        print(f"[THS] {msg}")
        return 0, msg


def _save_to_ths_stockblock(codes, user_dir):
    """写入同花顺 stockblock.ini [BLOCK_STOCK_CONTEXT] 21=自选股"""
    block_path = os.path.join(user_dir, "stockblock.ini")
    if not os.path.exists(block_path):
        block_path = os.path.join(user_dir, "同花顺方案", "stockblock.ini")
    print(f"[THS-DEBUG] stockblock.ini 路径: {block_path}")
    if not os.path.exists(block_path):
        msg = "stockblock.ini 不存在"
        print(f"[THS] {msg}")
        return 0, msg

    # 尝试多种编码读取
    content = None
    used_encoding = None
    for enc in ('gbk', 'utf-8', 'utf-8-sig', 'cp936'):
        try:
            with open(block_path, 'r', encoding=enc) as f:
                content = f.read()
            used_encoding = enc
            print(f"[THS-DEBUG] 使用编码 {enc} 读取 stockblock.ini，大小={len(content)} 字节")
            break
        except Exception as e:
            print(f"[THS-DEBUG] 编码 {enc} 读取失败: {e}")
            continue
    if content is None:
        msg = "无法读取 stockblock.ini，所有编码均失败"
        print(f"[THS] {msg}")
        return 0, msg

    section_name = '[BLOCK_STOCK_CONTEXT]'
    section_start = content.find(section_name)
    print(f"[THS-DEBUG] {section_name} 位置: {section_start}")
    if section_start < 0:
        msg = f"stockblock.ini 中没有 {section_name}"
        print(f"[THS] {msg}")
        return 0, msg

    section_body_start = section_start + len(section_name)
    next_section = content.find('\n[', section_body_start)
    section_end = next_section if next_section >= 0 else len(content)
    print(f"[THS-DEBUG] section 范围: {section_body_start} ~ {section_end}")

    line_prefix = '21='
    line_start = content.find('\n' + line_prefix, section_body_start, section_end)
    if line_start < 0:
        line_start = content.find(line_prefix, section_body_start, section_end)
    print(f"[THS-DEBUG] 21= 位置: {line_start}")

    if line_start < 0:
        print(f"[THS] stockblock.ini 中 21= 不存在，将自动创建")
        new_entries = []
        existing_codes = set()
        for code in codes:
            code = _strip_market_suffix(code.strip())
            if not code or code in existing_codes:
                continue
            market = _get_ths_market_id(code)
            new_entries.append(f"{market}:{code}")
            existing_codes.add(code)
        added = len(new_entries)
        if added == 0:
            return 0, "ok"
        new_line = '\n' + line_prefix + ','.join(new_entries) + ','
        insert_pos = section_end
        new_content = content[:insert_pos] + new_line + content[insert_pos:]
        print(f"[THS-DEBUG] 新建 21= 行: {repr(new_line[:200])}")
    else:
        if content[line_start] == '\n':
            line_start += 1
        line_end = content.find('\n', line_start)
        if line_end < 0:
            line_end = len(content)
        old_line = content[line_start:line_end]
        print(f"[THS-DEBUG] 原始 21= 行: {repr(old_line[:300])}")

        existing_codes = set()
        data_part = old_line.split('=', 1)[1] if '=' in old_line else ''
        for entry in data_part.strip().split(','):
            if ':' in entry:
                existing_codes.add(entry.split(':')[1])
            elif entry.strip():
                existing_codes.add(entry.strip())

        print(f"[THS] stockblock.ini 自选股已有 {len(existing_codes)} 只")

        added = 0
        new_entries = []
        for code in codes:
            code = _strip_market_suffix(code.strip())
            if not code or code in existing_codes:
                print(f"[THS] 跳过 {code}（已存在或无效）")
                continue
            market = _get_ths_market_id(code)
            new_entries.append(f"{market}:{code}")
            existing_codes.add(code)
            print(f"[THS] 新增 {code}")
            added += 1

        if added == 0:
            print(f"[THS] 无新增股票")
            return 0, "ok"

        old_data = data_part.strip()
        if old_data and not old_data.endswith(','):
            old_data += ','
        new_data = old_data + ','.join(new_entries) + ','
        new_line = line_prefix + new_data
        new_content = content[:line_start] + new_line + content[line_end:]
        print(f"[THS-DEBUG] 修改后 21= 行: {repr(new_line[:300])}")
        print(f"[THS-DEBUG] new_content 总长度变化: {len(content)} -> {len(new_content)}")

    def do_write(tmp_path):
        with open(tmp_path, 'w', encoding=used_encoding) as f:
            f.write(new_content)

    ok, msg = _write_file_with_retry(block_path, do_write)
    if ok:
        print(f"[THS] stockblock.ini 写入成功，新增 {added} 只，共 {len(existing_codes)} 只")
        # 写入后验证
        try:
            with open(block_path, 'r', encoding=used_encoding) as f:
                verify_content = f.read()
            verify_line_start = verify_content.find('21=')
            if verify_line_start >= 0:
                verify_line_end = verify_content.find('\n', verify_line_start)
                if verify_line_end < 0:
                    verify_line_end = len(verify_content)
                verify_line = verify_content[verify_line_start:verify_line_end]
                print(f"[THS-DEBUG] 验证读取 21= 行: {repr(verify_line[:300])}")
            else:
                print(f"[THS-DEBUG] 验证失败: 21= 行未找到")
        except Exception as e:
            print(f"[THS-DEBUG] 验证读取失败: {e}")
        return added, "ok"
    else:
        print(f"[THS] {msg}")
        return 0, msg


def _save_to_ths_hexin(codes, user_dir):
    """回退：写入同花顺旧版 hexin.ini [SELF_CODE_ADDTIME]"""
    hexin_path = os.path.join(user_dir, "同花顺方案", "hexin.ini")
    if not os.path.exists(hexin_path):
        msg = f"hexin.ini 不存在: {hexin_path}"
        print(f"[THS] {msg}")
        return 0, msg

    # 智能编码探测
    content = None
    used_encoding = None
    try:
        with open(hexin_path, 'rb') as f:
            raw = f.read()
    except Exception as e:
        msg = f"读取 hexin.ini 失败: {e}"
        print(f"[THS] {msg}")
        return 0, msg

    if raw.startswith(b'\xef\xbb\xbf'):
        try:
            content = raw.decode('utf-8-sig')
            used_encoding = 'utf-8-sig'
            print(f"[THS] 检测到 UTF-8 BOM")
        except Exception:
            pass
    else:
        for enc in ('utf-8', 'gbk', 'gb2312', 'cp936'):
            try:
                content = raw.decode(enc)
                used_encoding = enc
                print(f"[THS] 使用编码 {enc}")
                break
            except UnicodeDecodeError:
                continue
    if content is None or used_encoding is None:
        msg = "无法解码 hexin.ini"
        print(f"[THS] {msg}")
        return 0, msg

    # 精准定位 [SELF_CODE_ADDTIME]
    section_name = '[SELF_CODE_ADDTIME]'
    section_start = content.find(section_name)

    if section_start < 0:
        print(f"[THS] {section_name} 不存在，将自动创建")
        today = __import__('datetime').datetime.now().strftime('%Y%m%d')
        new_entries = []
        existing_codes = set()
        for code in codes:
            code = _strip_market_suffix(code.strip())
            if not code or code in existing_codes:
                continue
            new_entries.append(f"{code}:{today}")
            existing_codes.add(code)
        added = len(new_entries)
        if added == 0:
            return 0, "ok"
        sep = '\n' if content.endswith('\n') else '\n\n'
        new_section = f"{sep}{section_name}\nADDTIME=" + '|'.join(new_entries) + '|\n'
        new_content = content + new_section
    else:
        section_body_start = section_start + len(section_name)
        next_section = content.find('\n[', section_body_start)
        section_end = next_section if next_section >= 0 else len(content)

        addtime_start = content.find('ADDTIME=', section_body_start, section_end)
        if addtime_start < 0:
            msg = f"{section_name} 中没有 ADDTIME= 字段"
            print(f"[THS] {msg}")
            return 0, msg

        line_end = content.find('\n', addtime_start)
        if line_end < 0:
            line_end = len(content)
        old_line = content[addtime_start:line_end]

        existing_codes = set()
        data_part = old_line.split('=', 1)[1] if '=' in old_line else ''
        for entry in data_part.strip().split('|'):
            if ':' in entry:
                existing_codes.add(entry.split(':')[0])
            elif entry.strip():
                existing_codes.add(entry.strip())

        print(f"[THS] 自选股已有 {len(existing_codes)} 只")

        added = 0
        new_entries = []
        today = __import__('datetime').datetime.now().strftime('%Y%m%d')
        for code in codes:
            code = _strip_market_suffix(code.strip())
            if not code or code in existing_codes:
                print(f"[THS] 跳过 {code}（已存在或无效）")
                continue
            new_entries.append(f"{code}:{today}")
            existing_codes.add(code)
            print(f"[THS] 新增 {code}")
            added += 1

        if added == 0:
            return 0, "ok"

        old_data = data_part.strip()
        if old_data and not old_data.endswith('|'):
            old_data += '|'
        new_data = old_data + '|'.join(new_entries) + '|'
        new_line = 'ADDTIME=' + new_data
        new_content = content[:addtime_start] + new_line + content[line_end:]

    def do_write(tmp_path):
        with open(tmp_path, 'w', encoding=used_encoding) as f:
            f.write(new_content)

    ok, msg = _write_file_with_retry(hexin_path, do_write)
    if ok:
        print(f"[THS] hexin.ini 写入成功，新增 {added} 只，共 {len(existing_codes)} 只")
        return added, "ok"
    else:
        print(f"[THS] {msg}")
        return 0, msg


# ============================================================
# 通达信数据读取
# ============================================================





# ============================================================
# MACD 计算
# ============================================================
def ema(data, period):
    """计算EMA"""
    result = []
    k = 2.0 / (period + 1)
    for i, val in enumerate(data):
        if i == 0:
            result.append(val)
        else:
            result.append(val * k + result[-1] * (1 - k))
    return result


def calculate_macd(closes, fast=12, slow=26, signal=9):
    """计算MACD"""
    ema_fast = ema(closes, fast)
    ema_slow = ema(closes, slow)
    dif = [f - s for f, s in zip(ema_fast, ema_slow)]
    dea = ema(dif, signal)
    macd = [2 * (d - a) for d, a in zip(dif, dea)]
    return [{"dif": dif[i], "dea": dea[i], "macd": macd[i]} for i in range(len(closes))]


# ============================================================
# 获取股票名称
# ============================================================
def _get_stock_name(market, code):
    """获取股票名称。从本地缓存文件读取，缓存不存在则返回None。
    港股5位代码（如00700）和A股6位代码（如000700）是不同证券，绝不互相回退。
    """
    _load_stock_names_from_cache_file()
    compound_key = market + code
    info = _stock_names_cache.get(compound_key)
    if info and isinstance(info, dict):
        name = info.get("name", "")
        if name:
            return name
    if info and isinstance(info, str) and info:
        return info
    return None


# 流通股本缓存：通过 xdxr 网络接口获取（与除权除息数据复用同一数据源）
# key: (market, code), value: 流通股本(股)
# 由 forward_adjust.get_float_shares_from_xdxr() 负责读取和缓存
# 注意：xdxr 数据的 key 是 (market, code)，不会出现 000001.SH/000001.SZ 冲突

# 股票名称缓存：从通达信行情服务器批量获取后保存到本地JSON
# key: 股票代码(6位), value: {"name": "股票名称", "pinyin": "拼音首字母"}
_stock_names_cache = {}
_stock_names_loaded = False
_STOCK_NAMES_CACHE_FILE = os.path.join(VIPDOC_DIR, "stock_names.json")

# 刷新状态（股票名称刷新用）
_refresh_status = {"running": False, "progress": 0, "total": 0, "loaded": 0, "error": None, "step": ""}


def _load_stock_names_from_cache_file():
    """
    从 stock_names.json 缓存文件加载股票名称到内存。
    返回加载的记录数，文件不存在则返回0。
    版本迁移：自动将旧版纯数字键转换为 market+code 复合键。
    """
    global _stock_names_loaded
    if _stock_names_loaded:
        return len(_stock_names_cache)
    if not os.path.exists(_STOCK_NAMES_CACHE_FILE):
        _inject_known_indices()
        return 0
    try:
        with open(_STOCK_NAMES_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            # 迁移：将旧版纯数字键（如 "000001"）转换为复合键（如 "sh000001"）
            migrated = {}
            for key, info in data.items():
                if isinstance(info, dict) and "market" in info and info["market"]:
                    mkt = info["market"]
                    # 纯数字键（旧格式）→ 复合键
                    if key.isdigit():
                        new_key = mkt + key
                    else:
                        new_key = key
                    migrated[new_key] = info
                else:
                    migrated[key] = info
            _stock_names_cache.update(migrated)
            _stock_names_loaded = True
            print(f"[stock][信息] 从缓存文件加载股票名称: {len(_stock_names_cache)} 只")
            _inject_known_indices()
            return len(_stock_names_cache)
    except Exception as e:
        print(f"[stock][警告] 读取股票名称缓存失败: {e}")
    _inject_known_indices()
    return 0


def _inject_known_indices():
    """将常用指数注入缓存，确保中文名称和拼音首字母始终正确。"""
    _KNOWN_INDICES = [
        ("399001", "深证成指", "sz"),
        ("999999", "上证指数", "sh"),
        ("000001", "上证指数", "sh"),
        ("399006", "创业板指", "sz"),
        ("399005", "中小板指", "sz"),
        ("000016", "上证50", "sh"),
        ("000300", "沪深300", "sh"),
        ("399300", "沪深300", "sz"),
        ("000688", "科创50", "sh"),
        ("000852", "中证1000", "sh"),
        ("000905", "中证500", "sh"),
        ("399905", "中证500", "sz"),
        ("399330", "深证100", "sz"),
        ("399673", "创业板50", "sz"),
        ("HSTECH", "恒生科技指数", "hk"),
        ("HSI", "恒生指数", "hk"),
        ("HSCEI", "恒生中国企业指数", "hk"),
        ("HSCCI", "恒生香港中资企业指数", "hk"),
    ]
    try:
        from pypinyin import lazy_pinyin
        for code, name, market in _KNOWN_INDICES:
            py = "".join([p[0].upper() for p in lazy_pinyin(name) if p])
            compound_key = market + code
            _stock_names_cache[compound_key] = {"name": name, "pinyin": py, "market": market}
    except ImportError:
        for code, name, market in _KNOWN_INDICES:
            compound_key = market + code
            _stock_names_cache[compound_key] = {"name": name, "pinyin": "", "market": market}


def _collect_codes_from_vipdoc():
    """
    从 vipdoc 目录下的 .day 文件名收集所有股票代码。
    适用于没有 shm.tnf/szm.tnf 的通达信普通版。
    返回 {code: {"name": "", "pinyin": "", "market": "sh/sz/hk"}} 字典。

    包含范围：
      A股: 主板(60xxxx/00xxxx)、创业板(30xxxx)、科创板(68xxxx)、北交所(8xxxxx/4xxxxx)
           指数(000001上证/399001深成指/399006创业板指 等 399xxx/000xxx指数)
           ETF(51xxxx沪市ETF/15xxxx深市ETF/159xxx)
      港股: ds/lday 目录下的 31#XXXXX.day 文件

    排除范围：
      债券(11xxxx/12xxxx/13xxxx沪市债券, 10xxxx/11xxxx/12xxxx深市债券)
      基金(50xxxx沪市封闭基金, 16xxxx/18xxxx深市基金)
      其他(1xxxxx北交所债券, 20xxxx/90xxxx B股, 395xxx通达信内部板块)
    """
    result = {}

    # === A股代码过滤规则 ===
    # 上海市场(sh)：包含
    sh_include_prefixes = ("60", "68")  # 主板60, 科创板68
    # 上海市场(sh)：排除（债券、基金、ETF等）
    sh_exclude_prefixes = ("11", "12", "13", "50", "51", "52", "53", "54", "55", "56", "57", "58", "59", "588", "90", "91", "92", "93", "94", "95", "96", "97", "98", "10", "00", "09")
    # 上海指数：000xxx 和 9xxxxx 是上证系列指数
    sh_index_prefixes = ("000", "9")

    # 深圳市场(sz)：包含
    sz_include_prefixes = ("00", "30", "39")  # 主板00, 创业板30, 指数39
    # 深圳市场(sz)：排除（债券、基金、ETF等）
    sz_exclude_prefixes = ("10", "11", "12", "13", "14", "15", "16", "17", "18", "20", "395")

    # 深圳指数：399xxx 是深市指数（如399001深成指、399006创业板指）
    sz_index_prefixes = ("399",)

    def _is_a_stock_code(code, mkt_dir):
        """判断是否为需要包含的A股代码"""
        if not code.isdigit() or len(code) != 6:
            return False

        if mkt_dir == "sh":
            # 上海指数：000xxx（上证系列指数）、9xxxxx
            if code.startswith(sh_index_prefixes):
                return True
            # 上海包含：主板60、科创板68、ETF 51/56/58/59/588
            if code.startswith(sh_include_prefixes):
                return True
            # 上海排除：债券、基金等
            if code.startswith(sh_exclude_prefixes):
                return False
            # 其他上海代码默认排除
            return False

        elif mkt_dir == "sz":
            # 深圳指数：399xxx
            if code.startswith(sz_index_prefixes):
                return True
            # 深圳包含：主板00、创业板30、ETF 15/16/18
            if code.startswith(sz_include_prefixes):
                return True
            # 深圳排除：债券、基金、通达信内部板块等
            if code.startswith(sz_exclude_prefixes):
                return False
            # 其他深圳代码默认排除
            return False

        return False

    # === 收集A股代码 ===
    for mkt_dir, prefix in [("sh", "sh"), ("sz", "sz")]:
        lday_dir = os.path.join(VIPDOC_DIR, mkt_dir, "lday")
        if not os.path.isdir(lday_dir):
            continue
        for fname in os.listdir(lday_dir):
            if fname.startswith(prefix) and fname.endswith(".day"):
                code = fname[len(prefix):-4]
                if _is_a_stock_code(code, mkt_dir):
                    compound_key = mkt_dir + code
                    if compound_key not in result:
                        result[compound_key] = {"name": "", "pinyin": "", "market": mkt_dir}

    # === 收集港股代码（ds目录）===
    ds_lday_dir = os.path.join(VIPDOC_DIR, "ds", "lday")
    if os.path.isdir(ds_lday_dir):
        for fname in os.listdir(ds_lday_dir):
            if fname.startswith("31#") and fname.endswith(".day"):
                # 港股格式：31#00700.day
                code = fname[3:-4]  # 提取 00700
                if code.isdigit():
                    # 港股代码统一补前导零到5位
                    hk_code = code.zfill(5)
                    compound_key = "hk" + hk_code
                    if compound_key not in result:
                        result[compound_key] = {"name": "", "pinyin": "", "market": "hk"}

    print(f"[stock][调试] 代码收集明细: A股{sum(1 for v in result.values() if v['market'] != 'hk')}只, 港股{sum(1 for v in result.values() if v['market'] == 'hk')}只")
    return result


def _fetch_names_from_sina_once(codes_dict):
    """
    一次性从新浪财经API获取股票名称，用于首次建立缓存。
    参数 codes_dict: {code: {"name": "", ...}} —— 只获取 name 为空的条目。
    返回补充了多少条名称。
    注意：新浪API不支持A股和港股混合请求，必须分开调用。
    """
    import urllib.request
    import time

    # 只获取没有名称的代码
    codes_missing = [compound_key for compound_key, info in codes_dict.items() if not info.get("name")]
    if not codes_missing:
        return 0

    # 按市场分组：A股和港股必须分开请求
    a_stock_codes = []
    hk_codes = []
    compound_key_map = {}  # sh000001 -> 000001
    for compound_key in codes_missing:
        market = codes_dict[compound_key].get("market", "")
        # 从复合键提取纯代码：去掉前缀 sh/sz/hk
        if market and compound_key.startswith(market):
            bare_code = compound_key[len(market):]
        else:
            bare_code = compound_key
        compound_key_map[bare_code] = compound_key
        if market == "hk":
            hk_codes.append(bare_code)
        else:
            a_stock_codes.append((bare_code, market))

    print(f"[stock][信息] 从新浪API获取名称：A股{len(a_stock_codes)}只 + 港股{len(hk_codes)}只")
    filled = 0
    hk_filled = 0
    batch_size = 50

    # === 第一轮：A股 ===
    if a_stock_codes:
        total_batches = (len(a_stock_codes) - 1) // batch_size + 1
        for i in range(0, len(a_stock_codes), batch_size):
            batch = a_stock_codes[i:i+batch_size]
            batch_num = i // batch_size + 1
            codes_str_parts = []
            bare_to_compound = {}
            for bare_code, market in batch:
                codes_str_parts.append(f"{market}{bare_code}")
                bare_to_compound[bare_code] = market + bare_code
            codes_str = ",".join(codes_str_parts)
            url = f"http://hq.sinajs.cn/list={codes_str}"
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.sina.com.cn/"
                })
                resp = urllib.request.urlopen(req, timeout=15)
                content = resp.read().decode("gbk", errors="ignore")
                for line in content.strip().split("\n"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    var_part, val_part = line.split("=", 1)
                    val_part = val_part.strip().strip('"').strip(";").strip('"')
                    if not val_part:
                        continue
                    var_name = var_part.strip().replace("var ", "")
                    for mkt_prefix in ("sh", "sz"):
                        marker = f"hq_str_{mkt_prefix}"
                        if var_name.startswith(marker):
                            bare_code = var_name[len(marker):]
                            compound_key = bare_to_compound.get(bare_code)
                            if not compound_key:
                                continue
                            fields = val_part.split(",")
                            if len(fields) >= 1:
                                name = fields[0].strip()
                                if name and compound_key in codes_dict:
                                    codes_dict[compound_key]["name"] = name
                                    filled += 1
                            break
                if batch_num % 20 == 0 or batch_num == total_batches:
                    print(f"[stock][信息] 新浪API(A股): {batch_num}/{total_batches}, 累计{filled}只")
            except Exception as e:
                print(f"[stock][警告] 新浪API(A股)批次{batch_num}失败: {e}")
            if batch_num < total_batches:
                time.sleep(0.5)

    # === 第二轮：港股（用腾讯财经API，新浪港股接口已失效） ===
    if hk_codes:
        total_batches = (len(hk_codes) - 1) // batch_size + 1
        hk_filled = 0
        for i in range(0, len(hk_codes), batch_size):
            batch = hk_codes[i:i+batch_size]
            batch_num = i // batch_size + 1
            # 腾讯财经API：支持多只股票，用逗号分隔
            codes_str = ",".join([f"hk{code}" for code in batch])
            url = f"https://qt.gtimg.cn/q={codes_str}"
            try:
                req = urllib.request.Request(url, headers={
                    "User-Agent": "Mozilla/5.0",
                    "Referer": "https://finance.qq.com/"
                })
                resp = urllib.request.urlopen(req, timeout=15)
                content = resp.read().decode("gbk", errors="ignore")
                # 解析格式：v_hk00700="1~腾讯控股~00700~...";
                for line in content.strip().split(";"):
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    var_part, val_part = line.split("=", 1)
                    val_part = val_part.strip().strip('"').strip(";")
                    if not val_part:
                        continue
                    # 提取代码：v_hk00700 -> 00700
                    var_name = var_part.strip().replace("v_", "").replace("hk", "")
                    bare_code = var_name.strip()
                    compound_key = "hk" + bare_code
                    fields = val_part.split("~")
                    if len(fields) >= 2:
                        name = fields[1].strip()  # 股票名称在第2个字段
                        if name and compound_key in codes_dict:
                            codes_dict[compound_key]["name"] = name
                            filled += 1
                            hk_filled += 1
                if batch_num % 10 == 0 or batch_num == total_batches:
                    print(f"[stock][信息] 腾讯API(港股): {batch_num}/{total_batches}, 本轮累计{hk_filled}只")
            except Exception as e:
                print(f"[stock][警告] 腾讯API(港股)批次{batch_num}失败: {e}")
            if batch_num < total_batches:
                time.sleep(0.5)

    print(f"[stock][信息] API补全完成: 共{filled}只 (A股{filled-hk_filled}, 港股{hk_filled})")
    return filled


def _refresh_stock_names():
    """
    从本地文件批量获取全市场股票名称，保存到 stock_names.json。
    数据来源优先级：
      1. vipdoc/*.day 文件名（收集所有已下载过数据的股票代码）
      2. 新浪财经API（为无名称的代码批量查询名称）
    """
    global _stock_names_cache, _stock_names_loaded, _refresh_status

    # === 先加载已有缓存，新数据合并进去，不覆盖 ===
    raw_names = {}
    _load_stock_names_from_cache_file()
    if _stock_names_cache:
        for code, info in _stock_names_cache.items():
            if isinstance(info, dict):
                raw_names[code] = info
            else:
                raw_names[code] = {"name": info, "pinyin": ""}
        print(f"[stock][信息] 已加载现有缓存 {len(raw_names)} 只，将在此基础上合并新数据")
    else:
        print("[stock][信息] 无现有缓存，将从本地文件全新读取")

    # === 方案1: vipdoc .day文件名收集代码 ===
    # .day 文件覆盖所有已下载过K线数据的股票
    print("[stock][信息] 从vipdoc文件名收集代码...")
    vipdoc_codes = _collect_codes_from_vipdoc()
    vipdoc_added = 0
    for code, info in vipdoc_codes.items():
        if code not in raw_names:
            raw_names[code] = info
            vipdoc_added += 1
        elif not raw_names[code].get("name"):
            raw_names[code]["name"] = info.get("name", "")
            vipdoc_added += 1
    print(f"[stock][信息] 从vipdoc文件名收集到 {len(vipdoc_codes)} 只，新增 {vipdoc_added} 只")

    # === 方案2: 新浪API补全缺失的名称 ===
    # 即使已有缓存，如果有新发现的代码（如港股）没有名称，也要补全
    codes_without_name = [c for c, info in raw_names.items() if not info.get("name")]
    if codes_without_name:
        print(f"[stock][信息] 有 {len(codes_without_name)} 只代码无名称，尝试从新浪API补全...")
        temp_dict = {c: raw_names[c] for c in codes_without_name}
        filled = _fetch_names_from_sina_once(temp_dict)
        for code, info in temp_dict.items():
            if info.get("name"):
                raw_names[code] = info
        print(f"[stock][信息] 新浪API补全完成: {filled} 只")

    # === 补充通达信板块指数名称（88xxxx系列，如880491半导体）===
    tdxzs_file = os.path.join(TDX_HQ_CACHE, "tdxzs.cfg")
    if os.path.exists(tdxzs_file):
        try:
            with open(tdxzs_file, "r", encoding="gbk", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    parts = line.split("|")
                    if len(parts) >= 2:
                        name = parts[0].strip()
                        code = parts[1].strip()
                        if "." in code:
                            code = code.split(".")[0]
                        if name and code and code not in raw_names:
                            # 通达信板块指数代码（88xxxx），使用 sh 市场前缀
                            compound_key = "sh" + code
                            raw_names[compound_key] = {"name": name, "pinyin": "", "market": "sh"}
        except Exception as e:
            print(f"[stock][警告] 读取tdxzs.cfg失败: {e}")

    # === 统一用pypinyin生成拼音首字母（忽略tnf文件中的拼音，确保格式一致） ===
    try:
        from pypinyin import lazy_pinyin
        all_names = {}
        for code, info in raw_names.items():
            if isinstance(info, dict):
                name = info.get("name", "")
                market = info.get("market", "")  # 保留市场字段
            else:
                name = str(info)
                market = ""
            # 始终用pypinyin生成拼音首字母，确保搜索的一致性
            # 通达信/新浪API中名称可能含空格（如"五 粮 液"）或全角字母（如"鲁泰Ａ"）→ 统一清理
            name_clean = name.replace(" ", "")
            # 全角ASCII → 半角: U+FF01-U+FF5E → U+0021-U+007E
            name_clean = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in name_clean)
            pinyin = ""
            if name_clean:
                try:
                    py_list = lazy_pinyin(name_clean)
                    pinyin = "".join([p[0].upper() for p in py_list if p])
                except Exception:
                    pinyin = ""
            all_names[code] = {"name": name_clean, "pinyin": pinyin, "market": market}
    except ImportError:
        all_names = {}
        for code, info in raw_names.items():
            if isinstance(info, dict):
                name = info.get("name", "")
                market = info.get("market", "")
            else:
                name = str(info)
                market = ""
            all_names[code] = {"name": name, "pinyin": "", "market": market}

    # === 过滤 ST、*ST、退市股票，不写入缓存 ===
    filtered_count = 0
    for code in list(all_names.keys()):
        name = all_names[code].get("name", "")
        if not name or name.startswith("*ST") or name.startswith("ST") or "退" in name:
            del all_names[code]
            filtered_count += 1
    if filtered_count > 0:
        print(f"[stock][信息] 过滤掉 {filtered_count} 只（ST/*ST/退市）")

    if all_names:
        os.makedirs(os.path.dirname(_STOCK_NAMES_CACHE_FILE), exist_ok=True)
        with open(_STOCK_NAMES_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(all_names, f, ensure_ascii=False)
        _stock_names_cache = all_names
        _stock_names_loaded = True
        _inject_known_indices()
        sh_count = sum(1 for c in all_names if all_names[c].get("market") == "sh")
        sz_count = sum(1 for c in all_names if all_names[c].get("market") == "sz")
        print(f"[stock][信息] 股票名称刷新完成: 共{len(all_names)}只 (上海{sh_count}, 深圳{sz_count}), 已保存到 {_STOCK_NAMES_CACHE_FILE}")
    else:
        print("[stock][警告] 股票名称刷新失败: 未获取到任何数据")

    # 全部刷新完成，标记状态
    _refresh_status["running"] = False
    _refresh_status["step"] = ""



def get_stock_float_mv_local(market, code, last_close):
    """
    通过 xdxr 网络接口计算流通市值（单位：亿元）。
    流通市值 = 最新收盘价 × 流通股本
    港股返回 None（跳过市值比较）。
    流通股本由 forward_adjust.get_float_shares_from_xdxr() 从 xdxr 数据中提取，
    与除权除息数据复用同一数据源，无需本地文件解密。
    """
    if market == "hk":
        return None
    if not last_close or last_close <= 0:
        return None
    shares = get_float_shares_from_xdxr(market, code)
    if shares and shares > 0:
        return last_close * shares / 100000000  # 元 -> 亿元
    return None


# ============================================================
# 解析证券代码，判断市场
# ============================================================



def _get_stock_market_code(code):
    """识别股票/指数代码，返回 (market, code)；无法识别返回 (None, code)。"""
    # 港股指数别名映射：将用户输入的指数简称映射到通达信港股数据文件实际代码
    _HK_INDEX_ALIASES = {
        "HSTECH": ("hk", "HSTECH"),   # 恒生科技指数
        "HSI": ("hk", "HSI"),         # 恒生指数
        "HSCEI": ("hk", "HSCEI"),     # 恒生中国企业指数
        "HSCCI": ("hk", "HSCCI"),     # 恒生香港中资企业指数
    }
    if code in _HK_INDEX_ALIASES:
        return _HK_INDEX_ALIASES[code]

    prefix_match = re.match(r'^(SH|SZ|HK)(\d+)$', code)
    if prefix_match:
        return prefix_match.group(1).lower(), prefix_match.group(2)
    # HK前缀 + 非数字代码（如 HKHSTECH、HKHSI）
    prefix_alpha_match = re.match(r'^HK([A-Z]+)$', code)
    if prefix_alpha_match:
        return 'hk', prefix_alpha_match.group(1)
    suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK)$', code)
    if suffix_match:
        return suffix_match.group(2).lower(), suffix_match.group(1)
    # .HK 后缀 + 非数字代码（如 HSTECH.HK）
    suffix_alpha_match = re.match(r'^([A-Z]+)\.HK$', code)
    if suffix_alpha_match:
        return 'hk', suffix_alpha_match.group(1)
    # 自动判断：5位纯数字优先识别为港股（如 00700）
    if len(code) == 5 and code.isdigit():
        return 'hk', code
    if len(code) == 4 and code.isdigit():
        return 'hk', '0' + code
    # 6位代码：先检查是否是港股（在ds目录下有对应文件）
    if len(code) == 6 and code.isdigit():
        hk_file = os.path.join(VIPDOC_DIR, "ds", "lday", f"31#{code}.day")
        if os.path.exists(hk_file):
            return 'hk', code
    # A股判断
    if code.startswith('6'):
        return 'sh', code
    if code.startswith('5'):
        return 'sh', code  # 5xxxxx: 沪市ETF(51/56/58/59/588)、基金(50)等
    if code.startswith('0') or code.startswith('3'):
        return 'sz', code
    if code.startswith('1'):
        return 'sz', code  # 1xxxxx: 深市ETF(15/16/18)、债券等
    # 搜索
    for m in ['sh', 'sz']:
        f = os.path.join(VIPDOC_DIR, m, "lday", f"{m}{code}.day")
        if os.path.exists(f):
            return m, code
    f = os.path.join(VIPDOC_DIR, "ds", "lday", f"31#{code}.day")
    if os.path.exists(f):
        return 'hk', code
    return None, code


def _get_market_code(code):
    """
    解析代码，返回 (market, code)
    market: 'sh' / 'sz' / 'hk' / 'futures'
    """
    code = code.strip().upper()
    futures_code = _get_futures_code(code)
    if futures_code:
        return 'futures', futures_code
    return _get_stock_market_code(code)



def _get_kl_type(freq):
    """根据频率字符串返回对应的 KL_TYPE 枚举值"""
    mapping = {
        '15s': KL_TYPE.K_15S, '1m': KL_TYPE.K_1M, '5m': KL_TYPE.K_5M,
        '30m': KL_TYPE.K_30M, '60m': KL_TYPE.K_60M, 'd': KL_TYPE.K_DAY,
        'w': KL_TYPE.K_WEEK,
    }
    return mapping.get(freq, KL_TYPE.K_DAY)

def _get_freq_label(freq):
    """根据频率字符串返回中文标签"""
    labels = {'15s': '15秒', '1m': '1分钟', '5m': '5分钟', '30m': '30分钟', '60m': '60分钟', 'd': '日线', 'w': '周线'}
    return labels.get(freq, '日线')


def _make_chan_config():
    """统一的缠论配置，股票和期货共用。配置值已迁移到 ChanConfig.CChanConfig 默认值
    """
    from ChanConfig import CChanConfig
    return CChanConfig()


# ============================================================
# 缠论分析（chan.py 版本）
# ============================================================
import collections

_MAX_CACHE_SIZE = 20  # 最多缓存 20 个 (股票, 周期) 组合
_stocks_analysis_cache = collections.OrderedDict()
_cache_lock = threading.RLock()  # 保护 _stocks_analysis_cache 的并发读写（可重入，兼容 _check_memory_and_protect 嵌套调用）

# 扫描跳过记录（收集后统一打印）
_scan_skip_log = []

# 扫描与冷启动共用同一个 _stocks_analysis_cache，由 LRU 20 条 + 内存保护机制统一管理
# 不再需要单独的扫描缓存计数器和上限

# 扫描锁（防止并发扫描导致内存峰值翻倍）
_scan_lock = threading.Lock()

# 扫描终止标志：前端点击中断时设True，后端检查后跳过后续请求
_scan_aborted = False

# 扫描开始时间：用于计算扫描耗时，在通知中显示
_scan_start_time = None

# 股票分析锁（防止并发请求时 CTdxAPI.set_data 被覆盖导致分析结果串数据）
_stock_analysis_lock = threading.Lock()

# 内存保护阈值
_MEMORY_WARN_THRESHOLD_MB = 1500   # 1.5GB 警告
_MEMORY_LIMIT_MB = 2500            # 2.5GB 强制清理


def _cache_put(key, value):
    """写入缓存，超出上限时淘汰最旧的条目（LRU语义）"""
    with _cache_lock:
        if key in _stocks_analysis_cache:
            del _stocks_analysis_cache[key]  # 移到末尾
        elif len(_stocks_analysis_cache) >= _MAX_CACHE_SIZE:
            oldest_key = next(iter(_stocks_analysis_cache))
            _stocks_analysis_cache.pop(oldest_key)
            import gc
            gc.collect()
            print(f"[内存] 缓存已满({_MAX_CACHE_SIZE})，淘汰: {oldest_key}")
        _stocks_analysis_cache[key] = value
        _check_memory_and_protect()


def _cache_get(key):
    """读取缓存，命中时移到末尾（LRU语义）"""
    with _cache_lock:
        if key not in _stocks_analysis_cache:
            return None
        value = _stocks_analysis_cache.pop(key)
        _stocks_analysis_cache[key] = value
    return value


def _check_memory_and_protect():
    """检查内存，超过阈值时自动清理缓存"""
    try:
        import psutil
        process = psutil.Process(os.getpid())
        rss_mb = process.memory_info().rss / (1024 * 1024)
    except Exception:
        return

    import gc
    with _cache_lock:
        if rss_mb > _MEMORY_LIMIT_MB:
            _stocks_analysis_cache.clear()
            gc.collect()
            print(f"[内存保护] 内存 {rss_mb:.0f}MB 超过上限 {_MEMORY_LIMIT_MB}MB，已清空全部缓存")
        elif rss_mb > _MEMORY_WARN_THRESHOLD_MB:
            keys_to_remove = list(_stocks_analysis_cache.keys())[:len(_stocks_analysis_cache) // 2]
            for k in keys_to_remove:
                del _stocks_analysis_cache[k]
            gc.collect()
            print(f"[内存保护] 内存 {rss_mb:.0f}MB 超过警告线 {_MEMORY_WARN_THRESHOLD_MB}MB，已淘汰一半缓存")


def _send_windows_notification(title, message):
    """发送 Windows 10/11 Toast 通知（右下角弹出）。
    winotify 零依赖，仅需 PowerShell（Windows 内置），无需额外安装任何包。
    只需在 venv 中执行一次: pip install winotify
    在新线程中执行，不阻塞主流程。
    """
    def _notify():
        try:
            from winotify import Notification
            toast = Notification(app_id="缠论扫描", title=title, msg=message, duration="short")
            toast.show()
        except ImportError:
            print("[通知] 未安装 winotify，请在 venv 中执行: pip install winotify")
        except Exception as e:
            print(f"[通知] 发送失败: {type(e).__name__}: {e}")

    t = threading.Thread(target=_notify, daemon=True)
    t.start()


# ============================================================
# 手选进入段选点保存/恢复
# ============================================================
SAVED_POINT_FILE = r"C:\new_tdx_test\vipdoc\double_click_dt.csv"
# CSV列：股票代码,股票名,年K选点,季K选点,月K选点,周K选点,日K选点,30分选点,15分选点,5分选点,1分选点
SAVED_POINT_COLUMNS = ["code", "name", "y", "q", "m", "w", "d", "60m", "30m", "15m", "5m", "1m", "15s"]
# freq -> CSV列名 的映射
FREQ_TO_COL = {"y": "y", "q": "q", "m": "m", "w": "w", "d": "d", "60m": "60m", "30m": "30m", "15m": "15m", "5m": "5m", "1m": "1m", "15s": "15s"}
# 日内周期集合：分钟级
INTRADAY_FREQS = {"30m", "5m", "1m"}
# 秒级周期：K线时间含秒
SUBSECOND_FREQS = {"15s"}


def _get_date_fmt(freq):
    """根据周期返回统一日期格式（使用斜杠 / 分隔符，与 CChan 输出格式一致）。

    - 秒级（15s）→ "%Y/%m/%d %H:%M:%S"
    - 分钟级（30m, 5m, 1m）→ "%Y/%m/%d %H:%M"
    - 日线及以上 → "%Y/%m/%d"
    """
    if freq in SUBSECOND_FREQS:
        return "%Y/%m/%d %H:%M:%S"
    if freq in INTRADAY_FREQS:
        return "%Y/%m/%d %H:%M"
    return "%Y/%m/%d"


def _find_left_shoulder_time(kl_list, bi_list, bi_idx, freq):
    """
    找到分型左肩第一根原始K线的时间T。

    用户双击的分型K线是合并K线（分型中间K线），分型由三根合并K线组成：
    左肩 | 中间（分型）| 右肩。左肩合并K线可能由多根原始K线经过包含处理形成，
    需要找到左肩合并K线中最左边（最早）的那根原始K线对应的时间。

    参数:
        kl_list: KLine_List对象
        bi_list: 笔列表
        bi_idx: 前端双击命中的笔索引（该笔的begin_klu就是分型中间K线）
        freq: 周期

    返回:
        str: 格式化的时间字符串，如 "2026-01-09" 或 "2026-01-09 10:00"
        None: 定位失败
    """
    entry_bi = bi_list[bi_idx]
    begin_klu = entry_bi.get_begin_klu()  # 分型中间K线对应的klu

    # 在kl_list.lst中找到包含begin_klu的合并K线索引（分型中间位置）
    mid_idx = None
    for i, klc in enumerate(kl_list.lst):
        if hasattr(klc, 'lst') and klc.lst:
            for klu in klc.lst:
                if klu is begin_klu:
                    mid_idx = i
                    break
        if mid_idx is not None:
            break

    if mid_idx is None or mid_idx <= 0:
        print(f"[stock][警告] 无法定位分型中间K线在kl_list.lst中的位置")
        return None

    # 左肩 = 分型合并K线的前一个合并K线
    left_klc = kl_list.lst[mid_idx - 1]

    # 取左肩原始K线序列的第一根（最左边）
    if hasattr(left_klc, 'lst') and left_klc.lst:
        first_klu = left_klc.lst[0]
    else:
        # 没有包含关系，左肩就是一根原始K线
        first_klu = (left_klc.get_high_peak_klu() or left_klc.get_low_peak_klu())

    if first_klu is None:
        print(f"[stock][警告] 无法获取左肩K线单元")
        return None

    return first_klu.time.toFmtStr(_get_date_fmt(freq))





def _bi_overlap_range(bi, zg, zd):
    """判断笔与中枢区间[zd, zg]是否严格重叠，与 chan.py has_overlap 默认语义一致。"""
    return min(zg, bi._high()) > max(zd, bi._low())


def _calc_zs_confirm_edt_from_bis(zs_obj, all_bi_list, date_fmt):
    """
    计算中枢事实确认结束时间。

    zs.end/edt 表示中枢内部最后一笔的结束时间；confirm_edt 表示第一根
    与中枢区间无重叠、且后面已经有 next 的笔的结束时间。这样可以避免
    用尾部无后继的笔过早确认中枢结束；当 next 是虚笔时，也能符合
    trigger_step=True 的实时语义。
    """
    try:
        end_idx = zs_obj.end_bi.idx
        zg, zd = zs_obj.high, zs_obj.low
    except Exception:
        return ""
    for bi in all_bi_list[end_idx + 1:]:
        if _bi_overlap_range(bi, zg, zd):
            continue
        if getattr(bi, "next", None) is None:
            return ""
        return bi.get_end_klu().time.toFmtStr(date_fmt)
    return ""


def _calc_zs_confirm_edt_from_manual(zs_record, start_i, bis, date_fmt):
    """
    手选/保存选点模式下，根据自建中枢记录和后续笔序列计算确认结束时间。
    start_i 是中枢内部最后一笔之后的扫描位置。
    """
    zg, zd = zs_record["zg"], zs_record["zd"]
    i = start_i
    while i < len(bis):
        bi = bis[i]
        if _bi_overlap_range(bi, zg, zd):
            i += 1
            continue
        if getattr(bi, "next", None) is None:
            return ""
        return bi.get_end_klu().time.toFmtStr(date_fmt)
    return ""


def _load_saved_point_times():
    """从CSV文件加载所有选点记录，返回 {code: {col: value}} 字典"""
    points = {}
    if not os.path.exists(SAVED_POINT_FILE):
        return points
    try:
        import csv
        with open(SAVED_POINT_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            for row in reader:
                code = row.get("code", "").strip()
                if code:
                    points[code] = row
    except Exception as e:
        print(f"[stock][警告] 读取选点文件失败: {e}")
    return points

def _save_point_time(code, name, freq, sdt):
    """保存或更新某只股票某个周期的选点"""
    import csv
    col = FREQ_TO_COL.get(freq)
    if not col:
        return
    # 读取现有数据
    rows = []
    if os.path.exists(SAVED_POINT_FILE):
        try:
            with open(SAVED_POINT_FILE, "r", encoding="utf-8-sig") as f:
                reader = csv.DictReader(f)
                fieldnames = reader.fieldnames
                for row in reader:
                    rows.append(row)
        except:
            fieldnames = SAVED_POINT_COLUMNS
    else:
        fieldnames = SAVED_POINT_COLUMNS

    # 查找是否已有该代码的记录
    found = False
    for row in rows:
        if row.get("code", "").strip() == code:
            row["name"] = name
            row[col] = sdt
            found = True
            break
    if not found:
        new_row = {"code": code, "name": name}
        for c in SAVED_POINT_COLUMNS[2:]:
            new_row[c] = ""
        new_row[col] = sdt
        rows.append(new_row)

    # 写回文件
    try:
        with open(SAVED_POINT_FILE, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[stock][信息] 保存选点成功: {code} {freq} {col}={sdt}")
    except Exception as e:
        print(f"[stock][警告] 保存选点文件失败: {e}")


def _clear_saved_point_time(code, freq):
    """清除某只股票某个周期在CSV中的选点，同时更新内存缓存"""
    import csv
    col = FREQ_TO_COL.get(freq)
    if not col:
        return
    # 先清除内存缓存（无论CSV是否存在都要执行）
    if code in _saved_point_times:
        if col in _saved_point_times[code]:
            _saved_point_times[code][col] = ""
    if not os.path.exists(SAVED_POINT_FILE):
        return
    rows = []
    try:
        with open(SAVED_POINT_FILE, "r", encoding="utf-8-sig") as f:
            reader = csv.DictReader(f)
            fieldnames = reader.fieldnames
            for row in reader:
                rows.append(row)
    except Exception:
        return
    # 清除该代码对应周期的选点
    for row in rows:
        if row.get("code", "").strip() == code:
            row[col] = ""
            break
    try:
        with open(SAVED_POINT_FILE, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        print(f"[stock][信息] 清除选点成功: {code} {freq}")
    except Exception as e:
        print(f"[stock][警告] 清除选点失败: {e}")


def _cleanup_all_futures_data():
    """期货切到股票时彻底清理所有期货数据：K线缓存、分析缓存、选点记录"""
    import gc
    # 1. 清空 CTqSdkAPI 的K线缓存
    if CTqSdkAPI is not None:
        CTqSdkAPI.clear_all_cache()
        print("[清理] 已清空期货K线缓存")

    # 2. 清空选点记录中的期货条目（key以KQ.开头）
    pts_to_del = [k for k in list(_saved_point_times.keys()) if k.startswith("KQ.")]
    for k in pts_to_del:
        del _saved_point_times[k]
    if pts_to_del:
        print(f"[清理] 已清除 {len(pts_to_del)} 条期货选点记录")

    gc.collect()


# 启动时加载一次选点数据
_saved_point_times = _load_saved_point_times()


def _save_last_stock(code, freq="d"):
    """持久化上次查看的股票代码到JSON文件"""
    try:
        data = {"code": code, "freq": freq}
        with open(_LAST_STOCK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
    except Exception as e:
        pass  # 静默失败，不影响主流程


def _load_last_stock():
    """从JSON文件加载上次查看的股票代码，返回 (code, freq) 或 (None, None)"""
    try:
        if not os.path.exists(_LAST_STOCK_FILE):
            return None, None
        with open(_LAST_STOCK_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        code = data.get("code", "").strip()
        freq = data.get("freq", "d")
        if code:
            return code, freq
    except Exception as e:
        print(f"[警告] 异常: {type(e).__name__}: {e}")
    return None, None


# ============================================================
# 文字标注持久化存储
# ============================================================
ANNOTATIONS_FILE = os.path.join(VIPDOC_DIR, "text_annotation.json")
_annotations_cache = {}  # { "code_freq": [ { "date": "2024-01-15", "text": "支撑位", "y_offset": 0 }, ... ] }
_annotations_loaded = False


def _load_annotations():
    """从 text_annotation.json 加载标注数据到内存"""
    global _annotations_cache, _annotations_loaded
    if _annotations_loaded:
        return
    if os.path.exists(ANNOTATIONS_FILE):
        try:
            with open(ANNOTATIONS_FILE, "r", encoding="utf-8") as f:
                _annotations_cache = json.load(f)
            print(f"[stock][信息] 标注数据已加载: {len(_annotations_cache)} 个条目")
        except Exception as e:
            print(f"[stock][警告] 加载标注数据失败: {e}")
            _annotations_cache = {}
    _annotations_loaded = True


def _save_annotations():
    """保存标注数据到 text_annotation.json"""
    try:
        with open(ANNOTATIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(_annotations_cache, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[stock][警告] 保存标注数据失败: {e}")


def _get_annotation_key(code, freq):
    """生成标注缓存的键: {code}_{freq}"""
    return f"{code}_{freq}"


def _get_annotations_for(code, freq):
    """获取某股票某周期的所有标注"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    return _annotations_cache.get(key, [])


def _add_annotation(code, freq, date_str, text, y_offset=0):
    """添加一条标注（自动去重：同日期同文字不重复添加）"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    if key not in _annotations_cache:
        _annotations_cache[key] = []
    # 去重：同日期同文字已存在则不添加
    for ann in _annotations_cache[key]:
        if ann.get("date") == date_str and ann.get("text") == text:
            return False
    _annotations_cache[key].append({
        "date": date_str,
        "text": text,
        "y_offset": y_offset,
    })
    _save_annotations()
    return True


def _delete_annotation(code, freq, date_str, text):
    """删除一条标注"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    if key not in _annotations_cache:
        return False
    before = len(_annotations_cache[key])
    _annotations_cache[key] = [
        ann for ann in _annotations_cache[key]
        if not (ann.get("date") == date_str and ann.get("text") == text)
    ]
    if len(_annotations_cache[key]) < before:
        if not _annotations_cache[key]:
            del _annotations_cache[key]  # 清理空列表
        _save_annotations()
        return True
    return False


def _delete_annotation_by_date(code, freq, date_str):
    """删除某日期下所有标注"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    if key not in _annotations_cache:
        return False
    before = len(_annotations_cache[key])
    _annotations_cache[key] = [
        ann for ann in _annotations_cache[key]
        if ann.get("date") != date_str
    ]
    if len(_annotations_cache[key]) < before:
        if not _annotations_cache[key]:
            del _annotations_cache[key]
        _save_annotations()
        return True
    return False


def _delete_all_annotations(code, freq):
    """删除某股票某周期下全部标注"""
    _load_annotations()
    key = _get_annotation_key(code, freq)
    if key not in _annotations_cache or not _annotations_cache[key]:
        return False
    del _annotations_cache[key]
    _save_annotations()
    return True


def _get_annotated_codes(freq=""):
    """获取所有有标注的股票代码+周期列表，用于自选扫描
    返回 bare_code + market + name，方便前端与自选股列表交叉匹配。
    例如 key "000001.SH_d" → {"code": "000001", "market": "SH", "name": "上证指数", "freq": "d", "count": N}
    期货 key "KQ.m@SHFE.rb_d" → {"code": "KQ.m@SHFE.rb", "market": "", "name": "", "freq": "d", "count": N}
    """
    _load_annotations()
    _load_stock_names_from_cache_file()
    result = []
    for key, anns in _annotations_cache.items():
        if not anns:
            continue
        parts = key.rsplit("_", 1)
        if len(parts) != 2:
            continue
        code_with_suffix, key_freq = parts
        if freq and key_freq != freq:
            continue

        # 解析市场后缀: 000001.SH → bare_code=000001, market=SH
        # 期货代码（如 KQ.m@SHFE.rb）没有市场后缀，保持不变
        market = ""
        bare_code = code_with_suffix
        for suffix in [".SH", ".SZ", ".HK", ".BJ"]:
            if code_with_suffix.upper().endswith(suffix):
                market = suffix[1:]  # 去掉点号
                bare_code = code_with_suffix[:-len(suffix)]
                break

        # 查询股票名称
        name = ""
        if market and bare_code:
            lookup_key = market.lower() + bare_code
            info = _stock_names_cache.get(lookup_key, {})
            if isinstance(info, dict):
                name = info.get("name", "")
            elif info:
                name = str(info)

        result.append({
            "code": bare_code,
            "market": market,
            "name": name,
            "freq": key_freq,
            "count": len(anns),
            "annotations": [{"date": a.get("date", ""), "text": a.get("text", "")} for a in anns if a.get("text")]
        })
    return result


# 启动时加载标注数据
_load_annotations()


def _calc_futures_white_hline(kl_list, freq, date_fmt):
    """计算期货最新笔的白色横虚线数据（与股票逻辑一致）。
    返回 {"price": float, "start_date": str} 或 None。"""
    white_hline = None
    if not kl_list or not kl_list.bi_list:
        return white_hline
    latest_bi = kl_list.bi_list[-1]
    direction = "up" if latest_bi.is_up() else "down"
    end_klc = getattr(latest_bi, 'end_klc', None)
    if end_klc is None:
        return white_hline
    end_fx_idx = None
    for idx, klc in enumerate(kl_list.lst):
        if klc is end_klc:
            end_fx_idx = idx
            break
    if end_fx_idx is None or end_fx_idx <= 0:
        return white_hline
    left_klc = kl_list.lst[end_fx_idx - 1]
    klc_high = left_klc.high
    klc_low = left_klc.low
    tgt_klu = None
    if hasattr(left_klc, 'lst') and left_klc.lst:
        for klu in left_klc.lst:
            if direction == "down" and klu.high == klc_high:
                tgt_klu = klu
                break
            elif direction == "up" and klu.low == klc_low:
                tgt_klu = klu
                break
        if tgt_klu is None:
            tgt_klu = left_klc.lst[0]
    if tgt_klu:
        ls_date = tgt_klu.time.toFmtStr(date_fmt)
    else:
        ls_date = ""
    if direction == "down":
        white_hline = {"price": round(klc_high, 2), "start_date": ls_date}
    elif direction == "up":
        white_hline = {"price": round(klc_low, 2), "start_date": ls_date}
    return white_hline

def init_chan_symbol(api, symbol, name, freq_sec, freq_label, start_time=None):
    """拉取历史K线 + 运行 chan.py 分析，返回 (chan, klines, kl_type, records) 或 None。
    由 SSE handler 调用，每个 SSE 连接自包含。
    start_time: 选点起始时间，有值时只拉取该时间之后的K线"""
    import time as _time
    from Common.CEnum import KL_TYPE, AUTYPE
    from Chan import CChan
    from ChanConfig import CChanConfig
    from DataAPI.TqSdkAPI import FREQ_LABEL_CN

    display_label = FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{display_label}"

    try:
        records = fetch_futures_kline(api, symbol, freq_sec=freq_sec, display_key=display_key, start_time=start_time)
        if len(records) > 1:
            now = datetime.now()
            if (now - records[-1]["dt"]).total_seconds() < freq_sec:
                records = records[:-1]
        CTqSdkAPI.set_data(records, symbol=f"{symbol}:{freq_sec}")

        if len(records) == 0:
            print(f"[{display_key}] ⑵ 无有效数据，跳过")
            return None

        t_chan = _time.time()

        _freq_to_kl = {
            15: KL_TYPE.K_15S, 30: KL_TYPE.K_30S, 60: KL_TYPE.K_1M,
            300: KL_TYPE.K_5M, 900: KL_TYPE.K_15M, 1800: KL_TYPE.K_30M,
            3600: KL_TYPE.K_60M, 86400: KL_TYPE.K_DAY,
            604800: KL_TYPE.K_WEEK, 2592000: KL_TYPE.K_MON,
        }
        kl_type = _freq_to_kl.get(freq_sec, KL_TYPE.K_15S)

        config = CChanConfig()

        chan = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )

        for _snapshot in chan.step_load():
            pass

        klines = api.get_kline_serial(symbol, freq_sec)

        if _SSE_DEBUG:
            print(f"[{display_key}] ⑵ 缠论分析: 消费 {len(records)}根K线, 耗时 {_time.time()-t_chan:.1f}s")
        return (chan, klines, kl_type, records)

    except Exception as e:
        import traceback
        print(f"[{display_key}] ⑵ 失败: {e}")
        traceback.print_exc()
        return None

def _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label, saved_selection_date="", lightweight=False, klines=None):
    """从 CChan 对象中提取缠论结构快照，格式与 /api/stock 一致。
    lightweight=True: 仅返回最后一根K线的OHLC变化（周期内tick更新用），不遍历全量结构。
    klines: 天勤实时K线DataFrame（lightweight=True时优先使用，避免chan框架kl_list滞后）"""
    from Common.CEnum import FX_TYPE
    kl_list = chan[kl_type]
    _date_fmt = _get_date_fmt(freq_label)

    if lightweight:
        # ★ 优先从天勤实时 klines 读取当前形成中K线的OHLC，避免 chan 框架 kl_list 滞后
        if klines is not None and len(klines) > 0:
            last_row = klines.iloc[-1]
            dt_ns = last_row.get("datetime")
            kline_dt = "?"
            if dt_ns is not None:
                try:
                    kline_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime(_date_fmt)
                except Exception as e:
                    print(f"[警告] 异常: {type(e).__name__}: {e}")
            o = float(last_row.get("open", 0) or 0)
            h = float(last_row.get("high", 0) or 0)
            l = float(last_row.get("low", 0) or 0)
            c = float(last_row.get("close", 0) or 0)
            return {
                "type": "tick",
                "kline": {
                    "date": kline_dt,
                    "open": round(o, 3),
                    "high": round(h, 3),
                    "low": round(l, 3),
                    "close": round(c, 3),
                },
                "meta": {
                    "symbol": symbol, "name": name, "freq": freq_label,
                    "generated_at": datetime.now().strftime(_date_fmt),
                    "is_realtime": True, "market": "futures",
                },
            }
        # 回退：无 klines 时从 chan 框架读取
        if len(kl_list.lst) == 0:
            return None
        last_klc = kl_list.lst[-1]
        if len(last_klc.lst) == 0:
            return None
        last_klu = last_klc.lst[-1]
        return {
            "type": "tick",
            "kline": {
                "date": last_klu.time.toFmtStr(_date_fmt),
                "open": round(last_klu.open, 3),
                "high": round(last_klu.high, 3),
                "low": round(last_klu.low, 3),
                "close": round(last_klu.close, 3),
            },
            "meta": {
                "symbol": symbol, "name": name, "freq": freq_label,
                "generated_at": datetime.now().strftime(_date_fmt),
                "is_realtime": True, "market": "futures",
            },
        }

    klines_out = []
    for klc in kl_list.lst:
        for klu in klc.lst:
            t = klu.time
            klines_out.append({
                "date": t.toFmtStr(_date_fmt),
                "timestamp": int(t.ts * 1000),
                "open": round(klu.open, 3),
                "high": round(klu.high, 3),
                "low": round(klu.low, 3),
                "close": round(klu.close, 3),
                "vol": int(klu.trade_info.metric.get("volume", 0) or 0),
                "amount": round(klu.trade_info.metric.get("turnover", 0) or 0, 2),
            })

    closes = [k["close"] for k in klines_out]
    if len(closes) >= 26:
        ema12 = ema(closes, 12)
        ema26 = ema(closes, 26)
        dif = [e12 - e26 for e12, e26 in zip(ema12, ema26)]
        dea = ema(dif, 9)
        macd_vals = [2 * (d - a) for d, a in zip(dif, dea)]
        for i in range(len(klines_out)):
            if i < len(dif):
                klines_out[i]["dif"] = round(dif[i], 4)
                klines_out[i]["dea"] = round(dea[i], 4)
                klines_out[i]["macd"] = round(macd_vals[i], 4)
            else:
                klines_out[i]["dif"] = 0; klines_out[i]["dea"] = 0; klines_out[i]["macd"] = 0
    else:
        for k in klines_out:
            k["dif"] = 0; k["dea"] = 0; k["macd"] = 0

    bis = []
    for bi in kl_list.bi_list:
        try:
            direction = "up" if bi.is_up() else "down"
            begin_klu = bi.get_begin_klu()
            end_klu = bi.get_end_klu()
            begin_fx_idx = None
            if hasattr(bi, 'begin_klc') and bi.begin_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.begin_klc:
                        begin_fx_idx = idx
                        break
            end_fx_idx = None
            if hasattr(bi, 'end_klc') and bi.end_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.end_klc:
                        end_fx_idx = idx
                        break
            # 左肩/右肩原始K线时间（用于双窗口红框定位）
            fx_a_raw_dt = ""
            fx_b_raw_dt = ""
            shoulder_times = _get_main_bi_time_range(bi, _date_fmt)
            if shoulder_times:
                fx_a_raw_dt, fx_b_raw_dt, _, _ = shoulder_times
            bis.append({
                "sdt": begin_klu.time.toFmtStr(_date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(_date_fmt) if end_klu else "",
                "sdt_ts": int(begin_klu.time.ts * 1000) if begin_klu else 0,
                "edt_ts": int(end_klu.time.ts * 1000) if end_klu else 0,
                "direction": direction,
                "fx_a_price": round(bi.get_begin_val(), 2),
                "fx_b_price": round(bi.get_end_val(), 2),
                "high": round(bi._high(), 2),
                "low": round(bi._low(), 2),
                "power": round(abs(bi.get_end_val() - bi.get_begin_val()), 2),
                "is_sure": getattr(bi, 'is_sure', True),
                "end_fx_idx": end_fx_idx,
                "begin_fx_idx": begin_fx_idx,
                "fx_a_raw_dt": fx_a_raw_dt,
                "fx_b_raw_dt": fx_b_raw_dt,
                "fx_a_sub_dt": "",
                "fx_b_sub_dt": "",
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    fxs = []
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            peak_klu = klc.get_high_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(_date_fmt) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu else 0,
                "mark": "G", "price": klc.high, "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            peak_klu = klc.get_low_peak_klu()
            fxs.append({
                "date": peak_klu.time.toFmtStr(_date_fmt) if peak_klu else "",
                "timestamp": int(peak_klu.time.ts * 1000) if peak_klu else 0,
                "mark": "D", "price": klc.low, "high": klc.high, "low": klc.low,
            })

    segs = []
    for seg in kl_list.seg_list:
        try:
            direction = "up" if seg.is_up() else "down"
            begin_klu = seg.get_begin_klu()
            end_klu = seg.get_end_klu()
            if direction == "up":
                begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
                end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
            else:
                begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
                end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
            segs.append({
                "sdt": begin_klu.time.toFmtStr(_date_fmt) if begin_klu else "",
                "edt": end_klu.time.toFmtStr(_date_fmt) if end_klu else "",
                "direction": direction,
                "begin_price": begin_price, "end_price": end_price,
                "high": round(seg._high(), 2), "low": round(seg._low(), 2),
                "amp": round(seg.amp(), 2),
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_list = []
    for zs in kl_list.zs_list:
        try:
            zs_list.append({
                "sdt": zs.begin.time.toFmtStr(_date_fmt) if zs.begin and hasattr(zs.begin, 'time') else "",
                "edt": zs.end.time.toFmtStr(_date_fmt) if zs.end and hasattr(zs.end, 'time') else "",
                "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, _date_fmt),
                "zg": round(zs.high, 2), "zd": round(zs.low, 2),
                "gg": round(zs.peak_high, 2), "dd": round(zs.peak_low, 2),
                "dir": "up" if (zs.bi_in and zs.bi_in.is_up()) else "down",
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    zs_stars = []
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        star_date = begin_klu.time.toFmtStr(_date_fmt)
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "D", "color": "red"})
        else:
            zs_stars.append({"date": star_date, "price": round(star_price, 2), "mark": "G", "color": "green"})

    bsps = []
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            bsps.append({
                "date": bsp.klu.time.toFmtStr(_date_fmt),
                "timestamp": int(bsp.klu.time.ts * 1000),
                "type": bsp.type2str(), "is_buy": bsp.is_buy,
                "price": round(bsp.klu.close, 3),
                "high": round(bsp.klu.high, 3),
                "low": round(bsp.klu.low, 3),
            })
    except Exception as e:
        print(f"[警告] 异常: {type(e).__name__}: {e}")

    return {
        "meta": {
            "symbol": symbol, "name": name, "freq": freq_label,
            "kline_count": len(klines_out), "bi_count": len(bis),
            "fx_count": len(fxs), "zs_count": len(zs_list),
            "seg_count": len(segs), "bsp_count": len(bsps),
            "generated_at": datetime.now().strftime(_date_fmt),
            "is_realtime": True, "market": "futures",
            "saved_selection_date": saved_selection_date,
        },
        "klines": klines_out, "bis": bis, "fxs": fxs, "segs": segs,
        "zs": zs_list, "zs_stars": zs_stars, "bsps": bsps, "white_hline": None,
    }



def _analyze_futures_internal(code, freq="1m", end_date=None, dual=False, existing_chan=None, existing_records=None, step=None):
    """
    使用天勤数据源 + chan.py 进行期货/期指缠论分析（静态模式，HTTP 请求）
    与股票分析输出格式一致，便于前端复用同一套图表渲染逻辑。

    dual=True: 双窗口模式，返回 result 含 sub 字段（两个独立 CChan 对象）。
    existing_chan: 双窗口模式下，复用已有的单窗口 CChan 对象（匹配周期则复用）。
    existing_records: 对应 existing_chan 的 records。
    step: 箭头步进，在 full_records 中从 end_date 位置偏移 step 根K线作为新的截断日期。
    """
    import time
    t_start = time.time()

    if not TQ_AVAILABLE or CTqSdkAPI is None:
        return {"error": "天勤数据源未安装，请执行: pip install tqsdk"}

    # 确定周期秒数
    freq_sec = FREQ_SEC_MAP.get(freq, 86400)

    # 1. 拉取历史K线（每次冷启动重新拉取天勤数据）
    t_fetch = time.time()
    full_records = fetch_futures_kline(code, freq_sec=freq_sec)
    print(f"[拉取] ⑴ 天勤拉取K线: {time.time()-t_fetch:.3f}s, {len(full_records)}条")
    if len(full_records) < 5:
        return {"error": f"K线数据不足: 仅{len(full_records)}条"}

    # 2. 截断（end_date 复盘模式）
    if end_date:
        target_dt = None
        for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
            try:
                target_dt = datetime.strptime(end_date, fmt)
                break
            except ValueError:
                continue
        if target_dt is None:
            return {"error": f"无法解析日期: {end_date}"}
        # === 箭头步进：在 full_records 中从 end_date 位置偏移 step 根K线 ===
        if step is not None:
            step = int(step)
            if step != 0:
                anchor_idx = None
                for i in range(len(full_records) - 1, -1, -1):
                    if full_records[i]["dt"] <= target_dt:
                        anchor_idx = i
                        break
                if anchor_idx is not None:
                    new_idx = anchor_idx + step
                    if 0 <= new_idx < len(full_records):
                        target_dt = full_records[new_idx]["dt"]
                        end_date = target_dt.strftime("%Y-%m-%d %H:%M:%S")
                        print(f"[futures][箭头] step={step}: {full_records[anchor_idx]['dt']} → {target_dt} (idx {anchor_idx} → {new_idx})")
                    else:
                        print(f"[futures][箭头] step={step} 越界: idx {anchor_idx} → {new_idx}, 共{len(full_records)}条")

        records = [r for r in full_records if r["dt"] <= target_dt]
        if len(records) < 5:
            return {"error": f"截断后K线数据不足: 仅{len(records)}条"}
    else:
        records = full_records

    # 3. 注入数据源
    t_set = time.time()
    CTqSdkAPI.set_data(records, symbol=code)
    print(f"[分析] ⑵ 注入数据源: {time.time()-t_set:.3f}s")

    # 4. 获取品种名称
    stock_name = _get_futures_name(code)

    # 5. 创建 CChan 并消费
    import gc
    t0 = time.time()
    config = _make_chan_config()

    # 每次请求重置复盘标记，避免残留前一次状态
    from BuySellPoint.BSPointList import CMyBSPointList
    CMyBSPointList.REPLAY_MODE = False

    try:
        if end_date:
            from BuySellPoint.BSPointList import CMyBSPointList
            CMyBSPointList.REPLAY_MODE = True
        chan = CChan(
            code=code,
            begin_time=None,
            end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[_get_kl_type(freq)],
            config=config,
            autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan.step_load():
            pass
    except Exception as e:
        import traceback
        tb = traceback.format_exc()
        records_info = ""
        if records:
            records_info = f" records={len(records)}条 [{records[0]['dt']} ~ {records[-1]['dt']}]"
        print(f"[期货][错误] chan.py 分析失败: code={code} freq={freq}{records_info}")
        print(f"[期货][错误] 异常类型: {type(e).__name__}, 异常信息: {e}")
        print(f"[期货][错误] 完整堆栈:\n{tb}")
        return {"error": f"chan.py 期货分析失败: {type(e).__name__}: {e}"}
    finally:
        if end_date:
            CMyBSPointList.REPLAY_MODE = False

    kl_list = chan[_get_kl_type(freq)]
    print(f"[分析] ⑶ chan.py分析: {time.time()-t0:.3f}s, 合并K线={len(kl_list.lst)}, 笔={len(kl_list.bi_list)}, 中枢={len(kl_list.zs_list)}")

    # 6. 提取结果（与股票一致的格式，用 records 而非 kl_list）

    t_extract = time.time()
    closes = [r["close"] for r in records]
    macd_list = calculate_macd(closes)
    date_fmt = _get_date_fmt(freq)

    # K线数据（从 records 构建，与股票代码一致）
    kline_data = []
    for i, row in enumerate(records):
        macd = macd_list[i] if i < len(macd_list) else {"dif": 0, "dea": 0, "macd": 0}
        kline_data.append({
            "date": row["dt"].strftime(date_fmt),
            "timestamp": int(row["dt"].timestamp()) * 1000,
            "open": row["open"], "high": row["high"],
            "low": row["low"], "close": row["close"],
            "vol": row["vol"], "amount": row["amount"],
            "dif": round(macd["dif"], 4),
            "dea": round(macd["dea"], 4),
            "macd": round(macd["macd"], 4),
        })

    # 笔、线段、中枢、买卖点提取（与股票逻辑完全一致）
    bi_data, fx_data, seg_data, zs_data, zs_stars, bsp_data, white_hline = [], [], [], [], [], [], None

    # 提取笔（字段与股票代码完全一致）
    for bi in kl_list.bi_list:
        try:
            direction = "up" if bi.is_up() else "down"
            begin_val = bi.get_begin_val()
            end_val = bi.get_end_val()
            power = abs(end_val - begin_val)
            begin_klu = bi.get_begin_klu()
            end_klu = bi.get_end_klu()
            sdt_str = begin_klu.time.toFmtStr(date_fmt) if begin_klu else ""
            edt_str = end_klu.time.toFmtStr(date_fmt) if end_klu else ""
            try:
                sdt_ts = int(begin_klu.time.ts * 1000) if begin_klu else 0
            except:
                sdt_ts = 0
            try:
                edt_ts = int(end_klu.time.ts * 1000) if end_klu else 0
            except:
                edt_ts = 0

            # 分型索引（在 kl_list.lst 中定位）
            begin_fx_idx = None
            if hasattr(bi, 'begin_klc') and bi.begin_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.begin_klc:
                        begin_fx_idx = idx
                        break
            end_fx_idx = None
            if hasattr(bi, 'end_klc') and bi.end_klc:
                for idx, klc in enumerate(kl_list.lst):
                    if klc is bi.end_klc:
                        end_fx_idx = idx
                        break

            # 分型肩部原始K线时间
            # chan.py: begin_klc 是分型中间KLC（2），begin_klc.pre 是左肩KLC（1），begin_klc.next 是右肩KLC（3）
            # 左肩KLC可能合并了多根原始K线，取第一根 = A
            # 右肩KLC可能合并了多根原始K线，取最后一根 = B
            fx_a_raw_dt = ""
            fx_b_raw_dt = ""
            a_klu = None
            b_klu = None
            try:
                begin_klc = bi.begin_klc
                end_klc = bi.end_klc
                # A: 左肩第一根原始K线 = begin_klc.pre.lst[0]
                left_shoulder_klc = begin_klc.pre if begin_klc else None
                if left_shoulder_klc and left_shoulder_klc.lst:
                    a_klu = left_shoulder_klc.lst[0]
                if a_klu:
                    fx_a_raw_dt = a_klu.time.toFmtStr(date_fmt)
                # B: 右肩最后一根原始K线 = end_klc.next.lst[-1]
                right_shoulder_klc = end_klc.next if end_klc else None
                if right_shoulder_klc and right_shoulder_klc.lst:
                    b_klu = right_shoulder_klc.lst[-1]
                if b_klu:
                    fx_b_raw_dt = b_klu.time.toFmtStr(date_fmt)
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

            bi_data.append({
                "sdt": sdt_str, "edt": edt_str,
                "sdt_ts": sdt_ts, "edt_ts": edt_ts,
                "direction": direction,
                "fx_a_price": round(begin_val, 2),
                "fx_b_price": round(end_val, 2),
                "high": round(bi._high(), 2),
                "low": round(bi._low(), 2),
                "power": round(power, 2),
                "is_sure": getattr(bi, 'is_sure', True),
                "end_fx_idx": end_fx_idx,
                "begin_fx_idx": begin_fx_idx,
                "fx_a_raw_dt": fx_a_raw_dt,
                "fx_b_raw_dt": fx_b_raw_dt,
                "fx_a_sub_dt": "",
                "fx_b_sub_dt": "",
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

    # 提取分型（与股票路径一致）
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            mark = "G"
            price = klc.high
            klu = klc.get_high_peak_klu()
            fx_date = klu.time.toFmtStr(date_fmt) if klu else ""
            fx_data.append({
                "date": fx_date,
                "timestamp": int(klu.time.ts * 1000) if klu else 0,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            mark = "D"
            price = klc.low
            klu = klc.get_low_peak_klu()
            fx_date = klu.time.toFmtStr(date_fmt) if klu else ""
            fx_data.append({
                "date": fx_date,
                "timestamp": int(klu.time.ts * 1000) if klu else 0,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })

    # 提取线段（与股票代码完全一致）
    for seg in kl_list.seg_list:
        try:
            direction = "up" if seg.is_up() else "down"
            begin_klu = seg.get_begin_klu()
            end_klu = seg.get_end_klu()
            sdt = begin_klu.time.toFmtStr(date_fmt) if begin_klu else ""
            edt = end_klu.time.toFmtStr(date_fmt) if end_klu else ""
            if direction == "up":
                begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
                end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
            else:
                begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
                end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
            seg_data.append({
                "sdt": sdt, "edt": edt,
                "direction": direction,
                "begin_price": begin_price,
                "end_price": end_price,
                "high": round(seg._high(), 2),
                "low": round(seg._low(), 2),
                "amp": round(seg.amp(), 2),
            })
        except Exception as e:
            print(f"[警告] 异常: {type(e).__name__}: {e}")

        for zs in kl_list.zs_list:
            try:
                zs_data.append({
                    "sdt": zs.begin.time.toFmtStr(date_fmt),
                    "edt": zs.end.time.toFmtStr(date_fmt),
                    "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, date_fmt),
                    "zg": round(zs.high, 2),
                    "zd": round(zs.low, 2),
                    "gg": round(zs.peak_high, 2),
                    "dd": round(zs.peak_low, 2),
                    "dir": "up" if zs.bi_in and zs.bi_in.is_up() else "down",
                })
            except Exception as e:
                print(f"[调试] 中枢提取失败: {type(e).__name__}: {e}")

    # 中枢五角星（与股票代码完全一致）
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        star_date = begin_klu.time.toFmtStr(date_fmt)
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "D",
                "color": "red",
            })
        else:
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "G",
                "color": "green",
            })

    # 提取买卖点（与股票路径一致）
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            klu = bsp.klu
            bsp_date = klu.time.toFmtStr(date_fmt)
            try:
                bsp_ts = int(datetime.strptime(bsp_date, date_fmt).timestamp()) * 1000
            except:
                bsp_ts = 0
            bsp_data.append({
                "date": bsp_date, "timestamp": bsp_ts,
                "type": bsp.type2str(),
                "is_buy": bsp.is_buy,
                "price": klu.close,
                "high": klu.high,
                "low": klu.low,
            })
    except Exception as e:
        print(f"[调试] 期货获取买卖点失败: {e}")

    # 计算白色横虚线（最新笔分型上下沿，K线确认后才有意义）
    white_hline = _calc_futures_white_hline(kl_list, freq, date_fmt)

    # 7. 组装结果
    print(f"[分析] ⑷ 提取结果(K线/笔/分型/线段/中枢/买卖点): {time.time()-t_extract:.3f}s")
    date_range = f"{kline_data[0]['date']} ~ {kline_data[-1]['date']}" if kline_data else ""
    result = {
        "meta": {
            "symbol": code,
            "name": stock_name,
            "freq": _get_freq_label(freq),
            "chan_version": "chan.py",
            "kline_count": len(kline_data),
            "bi_count": len(bi_data),
            "fx_count": len(fx_data),
            "zs_count": len(zs_data),
            "seg_count": len(seg_data),
            "bsp_count": len(bsp_data),
            "date_range": date_range,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "is_replay": bool(end_date),
            "forward_adjust": False,
            "market": "futures",
        },
        "klines": kline_data,
        "bis": bi_data,
        "fxs": fx_data,
        "zs": zs_data,
        "zs_stars": zs_stars,
        "segs": seg_data,
        "bsps": bsp_data,
        "white_hline": white_hline,
    }

    print(f"[信息] 期货查询 {code} 完成({_get_freq_label(freq)}): {len(kline_data)}K线, {len(bi_data)}笔, {len(fx_data)}分型, {len(zs_data)}中枢, {len(seg_data)}线段, {len(bsp_data)}买卖点")
    print(f"[耗时] 总耗时: {time.time()-t_start:.3f}s")

    # 双窗口模式：提取子级别数据（独立 CChan 对象）
    if dual:
        sub_freq = _FUTURES_DUAL_FREQ_MAP.get(freq)
        if not sub_freq:
            result["error"] = f"双窗口不支持当前周期: {freq}"
            return result
        sub_freq_sec = FREQ_SEC_MAP.get(sub_freq, 15)
        print(f"[双窗口] 开始提取子级别({sub_freq})数据...")

        # 检查 existing_chan 是否匹配 sub_freq，匹配则复用
        sub_chan = None
        sub_records = None
        if existing_chan is not None and existing_records is not None and freq == sub_freq:
            # existing_chan 匹配的是主周期，子周期需要新建
            pass
        elif existing_chan is not None and existing_records is not None and freq != sub_freq:
            # 如果 existing_chan 正好匹配 sub_freq，复用它
            # 这个情况发生在上窗周期!=单窗口周期时（暂不涉及，保留接口）
            pass

        # 拉取子级别历史K线
        t_sub_fetch = time.time()
        sub_full_records = fetch_futures_kline(code, freq_sec=sub_freq_sec)
        print(f"[双窗口] 子级别({sub_freq})拉取K线: {time.time()-t_sub_fetch:.3f}s, {len(sub_full_records)}条")

        if len(sub_full_records) < 5:
            print(f"[双窗口] 子级别({sub_freq})数据不足，仅{len(sub_full_records)}条，跳过")
            return result

        # 截断到主级别时间范围（同步）
        if len(sub_full_records) > 0 and records:
            main_start = records[0]["dt"]
            main_end = records[-1]["dt"]
            sub_before = len(sub_full_records)
            sub_full_records = [r for r in sub_full_records
                                if main_start - timedelta(days=1) <= r["dt"] <= main_end + timedelta(days=1)]
            if sub_before != len(sub_full_records):
                print(f"[双窗口] 子级别({sub_freq})同步截断: {sub_before}条 -> {len(sub_full_records)}条")

        sub_records = sub_full_records

        # 注入子级别数据源
        sub_code = f"{code}:{sub_freq_sec}"
        CTqSdkAPI.set_data(sub_records, symbol=sub_code)

        # 创建子级别 CChan
        t_sub_chan = time.time()
        sub_config = _make_chan_config()
        try:
            sub_chan = CChan(
                code=sub_code,
                begin_time=None, end_time=None,
                data_src="custom:TqSdkAPI.CTqSdkAPI",
                lv_list=[_get_kl_type(sub_freq)],
                config=sub_config,
                autype=AUTYPE.NONE,
                market_type="futures",
            )
            for _snapshot in sub_chan.step_load():
                pass
        except Exception as e:
            print(f"[双窗口] 子级别({sub_freq}) chan.py 分析失败: {e}")
            return result

        sub_kl_list = sub_chan[_get_kl_type(sub_freq)]
        print(f"[双窗口] 子级别({sub_freq}) chan.py分析: {time.time()-t_sub_chan:.3f}s, "
              f"合并K线={len(sub_kl_list.lst)}, 笔={len(sub_kl_list.bi_list)}, 中枢={len(sub_kl_list.zs_list)}")

        # 提取子级别结果
        sub_name = _get_futures_name(code)
        sub_result = _extract_realtime_snapshot(
            sub_chan, _get_kl_type(sub_freq), code, sub_name,
            _get_freq_label(sub_freq), klines=None
        )
        result["sub"] = sub_result
        # 将 fx_a_raw_dt/fx_b_raw_dt（天勤K线开始时间）换算为子级别时间
        top_freq_sec = FREQ_SEC_MAP.get(freq, 60)
        _futures_red_range(result, top_freq_sec, sub_freq_sec, sub_freq)
        print(f"[双窗口] 子级别({sub_freq})提取完成: K线={sub_result['meta']['kline_count']}, "
              f"笔={sub_result['meta']['bi_count']}, 中枢={sub_result['meta']['zs_count']}")

    return result


def _analyze_stock_internal(code, freq="d", end_date=None, start_time=None, cache_chan=True, dual=False, step=None):
    """
    使用通达信数据源 + chan.py 进行股票/指数缠论分析（内部实现，不处理期货分流）
    返回与 czsc 版本兼容的 JSON 数据结构
    end_date: 复盘截止日期，有值时以该日期为"最新行情"
    start_time: 选点起始时间，有值时只加载该时间之后的K线（不设数量限制）
    step: 箭头步进，在 full_records 中从 end_date 位置偏移 step 根K线作为新的截断日期
    cache_chan: 是否缓存CChan对象。扫描模式设为False以节省内存。
    """
    import time
    t_start = time.time()

    market, code = _get_stock_market_code(code.strip().upper())
    if not market:
        return {"error": f"无法识别股票代码: {code}"}

    qualified_code = f"{code}.{market.upper()}"  # 区分沪市深市同号股票

    # 调试模式：冷启动注入截止日期（仅日K生效）
    if not end_date and DEBUG_COLD_START_END_DATE and freq == 'd':
        end_date = DEBUG_COLD_START_END_DATE
        print(f"[stock][调试] 冷启动使用截止日期: {end_date}")

    # ===== 双窗口模式：独立缓存系统 =====
    # 双窗口与单窗口完全独立，各自拥有独立的 CChan 对象和缓存 key
    # 双窗口内部主级别和子级别也分开存储
    sub_freq = None  # 单窗口模式下为 None，双窗口模式下由 _SUB_FREQ_MAP 赋值
    if dual:
        sub_freq = _SUB_FREQ_MAP.get(freq)
        if not sub_freq:
            return {"error": f"双窗口不支持当前周期: {freq}"}
        # 缓存 key 约定：
        #   dual_main_{market}_{code}_{freq}_{end_date}  — 主级别缓存（含 CChan 对象）
        #   dual_sub_{market}_{code}_{sub_freq}_{end_date}  — 子级别缓存（独立存储）
        date_suffix = "live"
        main_cache_key = f"dual_main_{market}_{code}_{freq}_{date_suffix}"
        sub_cache_key = f"dual_sub_{market}_{code}_{sub_freq}_{date_suffix}"
        cache_key = None  # 双窗口不使用单窗口的 cache_key，初始化为 None 防止意外引用

        # 查双窗口缓存（主级别和子级别必须同时存在，不会出现一个存在一个不存在）
        # 复盘模式(end_date)不命中缓存，强制重新加载
        main_cached = _cache_get(main_cache_key)
        sub_cached = _cache_get(sub_cache_key)
        if not end_date and main_cached is not None and sub_cached is not None \
                and "result" in main_cached and "result" in sub_cached:
            result = main_cached["result"]
            result["sub"] = sub_cached["result"]
            print(f"[stock][耗时] 命中双窗口缓存(freq={freq}+{sub_freq})，总耗时: 0.001s")
            return result

        # 复盘模式：清除旧的双窗口缓存，强制重新加载主级别和子级别
        if end_date:
            import gc
            with _cache_lock:
                if main_cache_key in _stocks_analysis_cache:
                    del _stocks_analysis_cache[main_cache_key]
                if sub_cache_key in _stocks_analysis_cache:
                    del _stocks_analysis_cache[sub_cache_key]
            gc.collect()
            print(f"[stock][信息] 复盘模式：已清除双窗口缓存，重新加载主级别({freq})和子级别({sub_freq})")

        # 未命中缓存：冷启动从文件加载双级别数据，cached_result=None 强制走文件读取
        cached_result = None
    else:
        # ===== 单窗口模式 =====
        date_suffix = end_date if end_date else "live"
        cache_key = f"single_{market}_{code}_{freq}_{date_suffix}"
        cached_result = _cache_get(cache_key)
        if not end_date and cached_result is not None and "result" in cached_result:
            result = cached_result["result"]
            col = FREQ_TO_COL.get(freq, "")
            if col and qualified_code in _saved_point_times:
                saved_sdt = _saved_point_times[qualified_code].get(col, "").strip() or None
                if saved_sdt:
                    cached_saved = result.get("meta", {}).get("saved_selection_date", "")
                    if cached_saved != saved_sdt:
                        print(f"[stock][信息] 缓存选点({cached_saved})与CSV({saved_sdt})不一致，跳过缓存")
                    else:
                        print(f"[stock][耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                        return result
                else:
                    print(f"[stock][耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                    return result
            else:
                print(f"[stock][耗时] 命中缓存(freq={freq})，总耗时: 0.001s")
                return result

    # ===== 0. 提前解析 end_date（复盘模式需要先知道 target_dt，以便传给前复权） =====
    target_dt = None
    if end_date:
        for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
            try:
                target_dt = datetime.strptime(end_date, fmt)
                break
            except ValueError:
                continue
        if target_dt is None:
            return {"error": f"无法解析日期: {end_date}"}

    # ===== 1. 加载K线数据 =====
    forward_adjust_done = False
    sub_records = None  # 子级别数据，仅双窗口使用

    if dual:
        # ────────────────────────────────────
        # 双窗口分支：独立加载主级别和子级别数据
        # ────────────────────────────────────
        t0 = time.time()
        if freq == '30m' and sub_freq == '5m':
            # 优化：30m+5m 共用同一次5m文件读取和前复权，避免重复读取和二次复权
            full_records, sub_records = read_main_level_records(market, code, freq, return_raw=True, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            print(f"[stock][耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            print(f"[stock][信息] 子级别({sub_freq})数据加载: {len(sub_records)}条 (复用前复权)")
        elif freq == 'w' and sub_freq == 'd':
            # 优化：w+d 共用同一次日线文件读取和前复权，避免重复读取和二次复权
            full_records, sub_records = read_main_level_records(market, code, freq, return_raw=True, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            print(f"[stock][耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            print(f"[stock][信息] 子级别({sub_freq})数据加载: {len(sub_records)}条 (复用前复权)")
        else:
            full_records = read_main_level_records(market, code, freq, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"主级别K线数据不足: 仅{len(full_records)}条"}
            print(f"[stock][耗时] 双窗口-主级别({freq})数据: {time.time()-t0:.3f}s, {len(full_records)}条K线")
            sub_records = read_sub_level_records(market, code, freq, sub_freq, full_records, end_date=target_dt)
        forward_adjust_done = FORWARD_ADJUST_ENABLED
        if sub_records is None or len(sub_records) < 5:
            print(f"[stock][警告] 子级别数据不足，退化为单级别模式")
            sub_freq = None
    else:
        # ────────────────────────────────────
        # 单窗口分支：只加载主级别数据
        # ────────────────────────────────────
        if not end_date and cached_result is not None and "records" in cached_result:
            full_records = cached_result["records"]
            forward_adjust_done = cached_result.get("result", {}).get("meta", {}).get("forward_adjust", False)
            print(f"[stock][耗时] 从缓存获取K线: {len(full_records)}条")
        else:
            t0 = time.time()
            full_records = read_main_level_records(market, code, freq, end_date=target_dt)
            if len(full_records) < 5:
                return {"error": f"K线数据不足: 仅{len(full_records)}条"}
            forward_adjust_done = FORWARD_ADJUST_ENABLED
            print(f"[stock][耗时] 读取数据文件: {time.time()-t0:.3f}s, {len(full_records)}条K线")

    # 调试模式：数据加载后立即截断起始日期（所有周期生效），后续流程对此无感知
    # 等于在数据源层面"只加载了指定日期之后的数据"
    if DEBUG_COLD_START_START_DATE:
        try:
            start_cutoff = datetime.strptime(DEBUG_COLD_START_START_DATE, "%Y-%m-%d")
            before = len(full_records)
            filtered = [r for r in full_records if r["dt"] >= start_cutoff]
            if filtered:
                full_records = filtered
                print(f"[stock][调试] 起始日期截断({DEBUG_COLD_START_START_DATE}): {before}条 -> {len(full_records)}条")
            else:
                print(f"[stock][调试] 起始日期 {DEBUG_COLD_START_START_DATE} 之前无数据，保留全部{before}条")
        except ValueError:
            print(f"[stock][警告] DEBUG_COLD_START_START_DATE 格式错误: {DEBUG_COLD_START_START_DATE}，应为 YYYY-MM-DD")

    # 截断到指定日期（复盘模式：以end_date为"最新行情"）
    # 左边界与冷启动一致（同样按时间范围截取），只有右边界不同
    if end_date:
        # 确定日期格式（用于校正日内周期的时分秒），target_dt 已在前面解析
        matched_fmt = None
        for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
            try:
                datetime.strptime(end_date, fmt)
                matched_fmt = fmt
                break
            except ValueError:
                continue
        if matched_fmt == "%Y-%m-%d" and freq in INTRADAY_FREQS:
            target_dt = target_dt.replace(hour=23, minute=59, second=59)

        # === 箭头步进：在 full_records 中从 end_date 位置偏移 step 根K线 ===
        if step is not None:
            step = int(step)
            if step != 0:
                # 找到 end_date 对应的K线索引（精确匹配或 ≤ target_dt 的最后一根）
                anchor_idx = None
                for i in range(len(full_records) - 1, -1, -1):
                    if full_records[i]["dt"] <= target_dt:
                        anchor_idx = i
                        break
                if anchor_idx is not None:
                    new_idx = anchor_idx + step
                    if 0 <= new_idx < len(full_records):
                        target_dt = full_records[new_idx]["dt"]
                        end_date = target_dt.strftime("%Y-%m-%d %H:%M:%S")
                        print(f"[stock][箭头] step={step}: {full_records[anchor_idx]['dt']} → {target_dt} (idx {anchor_idx} → {new_idx})")
                    else:
                        print(f"[stock][箭头] step={step} 越界: idx {anchor_idx} → {new_idx}, 共{len(full_records)}条")

    if end_date:
        # 复盘模式：左右边界截断
        from datetime import timedelta
        if freq == 'w':
            cutoff = target_dt - timedelta(days=365 * 8)
        elif freq == 'd':
            cutoff = target_dt - timedelta(days=365 * 3)
        elif freq == '30m':
            cutoff = target_dt - timedelta(days=90)
        elif freq == '5m':
            cutoff = target_dt - timedelta(days=21)
        else:
            cutoff = None

        before_count = len(full_records)
        if cutoff is not None:
            records = [r for r in full_records if cutoff <= r["dt"] <= target_dt]
        else:
            records = [r for r in full_records if r["dt"] <= target_dt]
        if len(records) < 5:
            return {"error": f"截断后K线数据不足: 仅{len(records)}条，请选择更晚的日期"}
        print(f"[stock][信息] 复盘范围(freq={freq}) {cutoff.strftime('%Y-%m-%d')} ~ {end_date}, "
              f"全量{before_count}条 -> {len(records)}条")
    else:
        records = full_records
        # 确定起始时间：优先使用传入的start_time，其次使用CSV保存的选点
        if start_time is None:
            col = FREQ_TO_COL.get(freq, "")
            if col and qualified_code in _saved_point_times:
                _saved = _saved_point_times[qualified_code].get(col, "").strip() or None
                if _saved:
                    start_time = _saved

        if start_time is not None:
            # 从选点时间开始过滤，不做数量限制
            from datetime import timedelta
            start_dt = None
            for fmt in ["%Y/%m/%d %H:%M:%S", "%Y/%m/%d %H:%M", "%Y/%m/%d"]:
                try:
                    start_dt = datetime.strptime(start_time, fmt)
                    break
                except ValueError:
                    continue
            if start_dt is not None:
                records = [r for r in records if r["dt"] >= start_dt]
                print(f"[stock][信息] 从选点时间 {start_time} 开始，筛选后 {len(records)} 条K线")
        else:
            # 冷启动无选点：默认模式下对30分和5分做时间截断（全量模式跳过）
            if not FULL_DATA_MODE and len(records) > 0 and freq in TIME_TRUNCATE_CONFIG:
                from datetime import timedelta
                latest_dt = records[-1]["dt"]
                trunc_days, trunc_text = TIME_TRUNCATE_CONFIG[freq]
                cutoff = latest_dt - timedelta(days=trunc_days)
                before_count = len(records)
                records = [r for r in records if r["dt"] >= cutoff]
                if before_count != len(records):
                    print(f"[stock][信息] 按时间范围截取(freq={freq}): 从{latest_dt.strftime('%Y-%m-%d')}往前推{trunc_text}, "
                          f"{before_count}条 -> {len(records)}条")

    # 双窗口：子级别数据同步截断到主级别时间范围
    # 避免 chan.py 分析不必要的全量子级别数据（如 30m+5m 时 5m 有 25152 条）
    if dual and sub_freq and sub_records is not None and len(records) > 0:
        from datetime import timedelta
        main_start = records[0]["dt"]
        main_end = records[-1]["dt"]
        sub_before = len(sub_records)
        sub_records = [r for r in sub_records if main_start - timedelta(days=1) <= r["dt"] <= main_end + timedelta(days=1)]
        if sub_before != len(sub_records):
            print(f"[stock][信息] 子级别({sub_freq})同步截断: {sub_before}条 -> {len(sub_records)}条")

    # 2. 使用 chan.py 进行缠论分析
    import gc
    # 复盘时：清空缓存恢复原始状态，再重新加载（与选点逻辑一致）
    if end_date and cache_key in _stocks_analysis_cache:
        with _cache_lock:
            if cache_key in _stocks_analysis_cache:
                del _stocks_analysis_cache[cache_key]
        gc.collect()

    t0 = time.time()
    with _stock_analysis_lock:
        chan_code = f"{market}.{code}"
        config = _make_chan_config()

        # 每次请求重置复盘标记，避免残留前一次状态
        from BuySellPoint.BSPointList import CMyBSPointList
        CMyBSPointList.REPLAY_MODE = False

        try:
            # CChan 创建：数据加载已在前面完成，此处只做数据注入和缠论分析
            if end_date:
                from BuySellPoint.BSPointList import CMyBSPointList
                CMyBSPointList.REPLAY_MODE = True
            _DUAL_LV_LIST = {
                'w': [KL_TYPE.K_WEEK, KL_TYPE.K_DAY],
                'd': [KL_TYPE.K_DAY, KL_TYPE.K_30M],
                '30m': [KL_TYPE.K_30M, KL_TYPE.K_5M],
            }
            if dual and sub_freq:
                # 双窗口：注入主级别和子级别数据
                lv_list = _DUAL_LV_LIST[freq]
                CTdxAPI.set_data({
                    _get_kl_type(freq): records,
                    _get_kl_type(sub_freq): sub_records,
                })
                config.kl_data_check = False
            else:
                # 单窗口（或双窗口降级）：只注入主级别数据
                lv_list = [_get_kl_type(freq)]
                CTdxAPI.set_data(records)

            chan = CChan(
                code=chan_code,
                begin_time=None,
                end_time=None,
                data_src="custom:TdxAPI.CTdxAPI",
                lv_list=lv_list,
                config=config,
                autype=AUTYPE.NONE,
                market_type="stock",
            )
            for _snapshot in chan.step_load():
                pass
        except Exception as e:
            import traceback
            tb = traceback.format_exc()
            records_info = ""
            if records:
                records_info = f" records={len(records)}条 [{records[0]['dt']} ~ {records[-1]['dt']}]"
            print(f"[stock][错误] chan.py 分析失败: code={chan_code} freq={freq}{records_info}")
            print(f"[stock][错误] 异常类型: {type(e).__name__}, 异常信息: {e}")
            print(f"[stock][错误] 完整堆栈:\n{tb}")
            return {"error": f"chan.py 分析失败: {type(e).__name__}: {e}"}
        finally:
            if end_date:
                CMyBSPointList.REPLAY_MODE = False

    print(f"[stock][耗时] chan.py 缠论分析: {time.time()-t0:.3f}s")

    # 4. 提取主级别结果
    result = _extract_main_level_data(chan, freq, records, market, code, 
                                       dual=dual, sub_freq=sub_freq,
                                       qualified_code=qualified_code, 
                                       end_date=end_date,
                                       forward_adjust_done=forward_adjust_done)

    # 双窗口模式：提取子级别数据
    sub_result = None
    if dual and sub_freq:
        print(f"[stock][调试] 双窗口模式: dual={dual}, sub_freq={sub_freq}, chan类型={type(chan).__name__}")
        try:
            sub_result = _extract_sub_level_data(chan, sub_freq, code, market)
        except Exception as e:
            import traceback
            print(f"[stock][错误] 提取子级别数据失败: {type(e).__name__}: {e}")
            print(f"[stock][错误] 堆栈:\n{traceback.format_exc()}")

    if sub_result:
        result["sub"] = sub_result

    mode_str = f" [复盘到 {end_date}]" if end_date else ""
    print(f"[stock][信息] 查询 {code}.{market.upper()} 完成{mode_str}: {result['meta']['kline_count']}条K线, {result['meta']['bi_count']}笔, {result['meta']['fx_count']}分型, {result['meta']['zs_count']}中枢, {result['meta']['seg_count']}线段, {result['meta']['bsp_count']}买卖点")
    print(f"[stock][耗时] 总耗时: {time.time()-t_start:.3f}s")

    # 缓存策略：
    # - 单窗口：缓存到 single_{market}_{code}_{freq}
    # - 双窗口：主级别缓存到 dual_main_{market}_{code}_{freq}，子级别缓存到 dual_sub_{market}_{code}_{sub_freq}
    # - 冷启动/选点/复盘/扫描：统一缓存 records + result + chan，由 LRU 20 条 + 内存保护管理
    if dual and sub_freq and sub_records is not None:
        # 双窗口：主级别缓存（不含 sub 字段，sub 独立存储）
        main_result = {k: v for k, v in result.items() if k != "sub"}
        main_cached = _cache_get(main_cache_key)
        if main_cached is None:
            main_cached = {}
        if cache_chan:
            main_cached["records"] = full_records
            main_cached["chan"] = chan       # CChan 只存一份在主级别缓存
        main_cached["result"] = main_result
        _cache_put(main_cache_key, main_cached)

        # 双窗口：子级别缓存（独立存储，下次切回双窗口直接命中）
        sub_cached = _cache_get(sub_cache_key)
        if sub_cached is None:
            sub_cached = {}
        if cache_chan:
            sub_cached["records"] = sub_records
        sub_cached["result"] = sub_result
        _cache_put(sub_cache_key, sub_cached)
    elif dual:
        # 双窗口降级为单级别（子级别数据不足）：不缓存，下次重试
        pass
    else:
        # 单窗口
        cached = _cache_get(cache_key)
        if cached is None:
            cached = {}
        if cache_chan:
            cached["records"] = full_records
            cached["chan"] = chan
        cached["result"] = result
        _cache_put(cache_key, cached)

    # 复盘后触发GC，回收旧的CChan对象，避免内存累积导致下次分析变慢
    if end_date:
        t_gc = time.time()
        gc.collect()
        print(f"[stock][耗时]   复盘后GC回收: {time.time()-t_gc:.3f}s")

    return result


# ============================================================
# 提取主级别和子级别数据
# ============================================================
def _extract_main_level_data(chan, freq, records, market, code, dual=False, sub_freq=None,
                              qualified_code="", end_date=None, forward_adjust_done=False):
    """
    从 CChan 中提取主级别的 K线、笔、分型、中枢、线段、买卖点数据。
    返回与 czsc 版本兼容的 JSON 数据结构（不含 sub 字段）。
    """
    t0 = time.time()
    kl_list = chan[_get_kl_type(freq)]

    # 1. K线数据（含MACD）
    closes = [r["close"] for r in records]
    macd_list = calculate_macd(closes)
    date_fmt = _get_date_fmt(freq)
    kline_data = []
    for i, row in enumerate(records):
        macd = macd_list[i] if i < len(macd_list) else {"dif": 0, "dea": 0, "macd": 0}
        kline_data.append({
            "date": row["dt"].strftime(date_fmt),
            "timestamp": int(row["dt"].timestamp()) * 1000,
            "open": row["open"], "high": row["high"],
            "low": row["low"], "close": row["close"],
            "vol": row["vol"], "amount": row["amount"],
            "dif": round(macd["dif"], 4),
            "dea": round(macd["dea"], 4),
            "macd": round(macd["macd"], 4),
        })

    def _parse_klu_dt(klu):
        """将 KLU 的时间转为 datetime 对象，用于范围比较。"""
        return datetime.fromtimestamp(klu.time.ts)

    def _format_klu_dt(klu, out_freq):
        """将 KLU 的时间格式化为目标频率对应的日期字符串。"""
        out_fmt = _get_date_fmt(out_freq)
        return klu.time.toFmtStr(out_fmt)

    def _get_parent_sub_klus(parent_klu, parent_freq):
        """获取一根主级别K线真正覆盖的子级别K线序列。"""
        if not parent_klu or not hasattr(parent_klu, 'sub_kl_list') or not parent_klu.sub_kl_list:
            return []
        parent_dt = _parse_klu_dt(parent_klu)
        if parent_dt is None:
            return []

        if parent_freq == 'w':
            start = parent_dt - timedelta(days=parent_dt.weekday())
            start = start.replace(hour=0, minute=0, second=0, microsecond=0)
            end = start + timedelta(days=6, hours=23, minutes=59, seconds=59, microseconds=999999)
        elif parent_freq == 'd':
            start = parent_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            end = parent_dt.replace(hour=23, minute=59, second=59, microsecond=999999)
        elif parent_freq == '30m':
            end = parent_dt
            start = parent_dt - timedelta(minutes=30) + timedelta(microseconds=1)
        else:
            return list(parent_klu.sub_kl_list)

        valid = []
        for sub_klu in parent_klu.sub_kl_list:
            sub_dt = _parse_klu_dt(sub_klu)
            if sub_dt is not None and start <= sub_dt <= end:
                valid.append(sub_klu)
        return valid

    # 双窗口模式：为每根K线添加 sub_kl_times（灰框定位用）
    if dual and sub_freq:
        date_to_klu = {}
        for klc in kl_list.lst:
            for klu in klc.lst:
                key = klu.time.toFmtStr(date_fmt)
                date_to_klu[key] = klu
        for k in kline_data:
            klu = date_to_klu.get(k["date"])
            if klu and klu.sub_kl_list:
                sub_times = []
                for sub_klu in _get_parent_sub_klus(klu, freq):
                    sub_times.append(_format_klu_dt(sub_klu, sub_freq))
                k["sub_kl_times"] = sub_times
            else:
                k["sub_kl_times"] = []

    # 2. 笔数据
    bi_data = []
    _fx_empty_count = 0
    for bi in kl_list.bi_list:
        direction = "up" if bi.is_up() else "down"
        begin_val = bi.get_begin_val()
        end_val = bi.get_end_val()
        power = abs(end_val - begin_val)
        begin_klu = bi.get_begin_klu()
        end_klu = bi.get_end_klu()
        sdt = begin_klu.time.to_str() if begin_klu else ""
        edt = end_klu.time.to_str() if end_klu else ""
        try:
            sdt_dt = datetime.strptime(sdt, "%Y/%m/%d %H:%M")
            sdt_str = sdt_dt.strftime(date_fmt)
            sdt_ts = int(sdt_dt.timestamp()) * 1000
        except:
            sdt_str = sdt
            sdt_ts = 0
        try:
            edt_dt = datetime.strptime(edt, "%Y/%m/%d %H:%M")
            edt_str = edt_dt.strftime(date_fmt)
            edt_ts = int(edt_dt.timestamp()) * 1000
        except:
            edt_str = edt
            edt_ts = 0
        begin_fx_idx = None
        if hasattr(bi, 'begin_klc') and bi.begin_klc:
            for idx, klc in enumerate(kl_list.lst):
                if klc is bi.begin_klc:
                    begin_fx_idx = idx
                    break
        end_fx_idx = None
        if hasattr(bi, 'end_klc') and bi.end_klc:
            for idx, klc in enumerate(kl_list.lst):
                if klc is bi.end_klc:
                    end_fx_idx = idx
                    break
        fx_a_raw_dt = ""
        fx_b_raw_dt = ""
        a_klu = None
        b_klu = None
        date_fmt = _get_date_fmt(freq)
        shoulder_times = _get_main_bi_time_range(bi, date_fmt)
        if shoulder_times:
            fx_a_raw_dt, fx_b_raw_dt, a_klu, b_klu = shoulder_times

        if not fx_a_raw_dt or not fx_b_raw_dt:
            _fx_empty_count += 1

        fx_a_sub_dt, fx_b_sub_dt = _stocks_red_range(a_klu, b_klu, sub_freq, bi) if dual and sub_freq else ("", "")

        bi_data.append({
            "sdt": sdt_str, "edt": edt_str,
            "sdt_ts": sdt_ts, "edt_ts": edt_ts,
            "direction": direction,
            "fx_a_price": round(begin_val, 2),
            "fx_b_price": round(end_val, 2),
            "high": round(bi._high(), 2),
            "low": round(bi._low(), 2),
            "power": round(power, 2),
            "is_sure": getattr(bi, 'is_sure', True),
            "end_fx_idx": end_fx_idx,
            "begin_fx_idx": begin_fx_idx,
            "fx_a_raw_dt": fx_a_raw_dt,
            "fx_b_raw_dt": fx_b_raw_dt,
            "fx_a_sub_dt": fx_a_sub_dt,
            "fx_b_sub_dt": fx_b_sub_dt,
        })

    if _fx_empty_count > 0:
        print(f"[stock][调试] 笔 fx_a/fx_b 空值总数: {_fx_empty_count}/{len(bi_data)}")

    # 3. 分型数据
    fx_data = []
    for klc in kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            mark = "G"
            price = klc.high
            klu = klc.get_high_peak_klu()
            fx_date = klu.time.toFmtStr(_get_date_fmt(freq))
            try:
                fx_dt = datetime.strptime(fx_date, date_fmt)
                fx_ts = int(fx_dt.timestamp()) * 1000
            except:
                fx_ts = 0
            fx_data.append({
                "date": fx_date, "timestamp": fx_ts,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            mark = "D"
            price = klc.low
            klu = klc.get_low_peak_klu()
            fx_date = klu.time.toFmtStr(_get_date_fmt(freq))
            try:
                fx_dt = datetime.strptime(fx_date, date_fmt)
                fx_ts = int(fx_dt.timestamp()) * 1000
            except:
                fx_ts = 0
            fx_data.append({
                "date": fx_date, "timestamp": fx_ts,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })

    # 4. 中枢数据
    zs_data = []
    for zs in kl_list.zs_list:
        zs_data.append({
            "sdt": zs.begin.time.toFmtStr(date_fmt),
            "edt": zs.end.time.toFmtStr(date_fmt),
            "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, kl_list.bi_list, date_fmt),
            "zg": round(zs.high, 2),
            "zd": round(zs.low, 2),
            "gg": round(zs.peak_high, 2),
            "dd": round(zs.peak_low, 2),
            "dir": "up" if zs.bi_in and zs.bi_in.is_up() else "down",
        })

    zs_stars = []
    for zs in kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        try:
            sdt_raw = begin_klu.time.to_str()
            sdt_dt = datetime.strptime(sdt_raw, "%Y/%m/%d %H:%M")
            star_date = sdt_dt.strftime(date_fmt)
        except:
            star_date = sdt_raw
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "D",
                "color": "red",
            })
        else:
            zs_stars.append({
                "date": star_date,
                "price": round(star_price, 2),
                "mark": "G",
                "color": "green",
            })

    # 5. 买卖点数据
    bsp_data = []
    try:
        bsp_list = chan.get_latest_bsp(idx=0, number=0)
        for bsp in bsp_list:
            klu = bsp.klu
            bsp_date = klu.time.toFmtStr(_get_date_fmt(freq))
            try:
                bsp_dt = datetime.strptime(bsp_date, date_fmt)
                bsp_ts = int(bsp_dt.timestamp()) * 1000
            except:
                bsp_ts = 0
            bsp_data.append({
                "date": bsp_date, "timestamp": bsp_ts,
                "type": bsp.type2str(),
                "is_buy": bsp.is_buy,
                "price": klu.close,
                "high": klu.high,
                "low": klu.low,
            })
    except Exception as e:
        print(f"[stock][调试] 获取买卖点失败: {e}")

    # 6. 线段数据
    seg_data = []
    for seg in kl_list.seg_list:
        direction = "up" if seg.is_up() else "down"
        begin_klu = seg.get_begin_klu()
        end_klu = seg.get_end_klu()
        sdt = (begin_klu.time.toFmtStr(_get_date_fmt(freq))) if begin_klu else ""
        edt = (end_klu.time.toFmtStr(_get_date_fmt(freq))) if end_klu else ""
        begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
        end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
        if direction == "down":
            begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
            end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
        seg_data.append({
            "sdt": sdt, "edt": edt,
            "direction": direction,
            "begin_price": begin_price,
            "end_price": end_price,
            "high": round(seg._high(), 2),
            "low": round(seg._low(), 2),
            "amp": round(seg.amp(), 2),
        })

    print(f"[stock][耗时] 分析结果转JSON(K线/分型/笔/线段/中枢/买卖点）: {time.time()-t0:.3f}s")

    # 获取当前周期的保存选点日期
    _col_meta = FREQ_TO_COL.get(freq, "")
    _saved_sdt_for_meta = ""
    if not end_date and _col_meta and qualified_code in _saved_point_times:
        _saved_sdt_for_meta = _saved_point_times[qualified_code].get(_col_meta, "").strip() or ""

    # 7. 计算最新笔的白色横虚线数据
    white_hline = None
    if bi_data:
        latest_bi = bi_data[-1]
        is_virtual = not latest_bi.get("is_sure", True)
        direction = latest_bi.get("direction", "")
        end_fx_idx = latest_bi.get("end_fx_idx")
        if end_fx_idx is not None and end_fx_idx > 0:
            left_klc = kl_list.lst[end_fx_idx - 1]
            klc_high = left_klc.high
            klc_low = left_klc.low
            tgt_klu = None
            if hasattr(left_klc, 'lst') and left_klc.lst:
                for klu in left_klc.lst:
                    if direction == "down" and klu.high == klc_high:
                        tgt_klu = klu
                        break
                    elif direction == "up" and klu.low == klc_low:
                        tgt_klu = klu
                        break
                if tgt_klu is None:
                    tgt_klu = left_klc.lst[0]
            if tgt_klu:
                ls_date = tgt_klu.time.toFmtStr(date_fmt)
            else:
                ls_date = ""
            if direction == "down":
                white_hline = {"price": round(klc_high, 2), "start_date": ls_date}
            elif direction == "up":
                white_hline = {"price": round(klc_low, 2), "start_date": ls_date}

    # 8. 组装结果
    date_range = f"{kline_data[0]['date']} ~ {kline_data[-1]['date']}" if kline_data else ""

    print(f"[stock][耗时] 主级别提取结果: {time.time()-t0:.3f}s (K线={len(kline_data)} 笔={len(bi_data)} 中枢={len(zs_data)} 线段={len(seg_data)} 买卖点={len(bsp_data)})")

    stock_name = _get_stock_name(market, code)
    if not stock_name:
        stock_name = f"{code}.{market.upper()}"

    result = {
        "meta": {
            "symbol": f"{code}.{market.upper()}",
            "name": stock_name,
            "freq": _get_freq_label(freq),
            "chan_version": "chan.py",
            "kline_count": len(kline_data),
            "bi_count": len(bi_data),
            "fx_count": len(fx_data),
            "zs_count": len(zs_data),
            "seg_count": len(seg_data),
            "bsp_count": len(bsp_data),
            "date_range": date_range,
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "saved_selection_date": _saved_sdt_for_meta,
            "is_replay": bool(end_date),
            "forward_adjust": forward_adjust_done,
        },
        "klines": kline_data,
        "bis": bi_data,
        "fxs": fx_data,
        "zs": zs_data,
        "zs_stars": zs_stars,
        "segs": seg_data,
        "bsps": bsp_data,
        "white_hline": white_hline,
    }
    return result


def _extract_sub_level_data(chan, sub_freq, code, market):
    """
    从多级别 CChan 中提取子级别的 K线、笔、分型、中枢、线段、买卖点数据。
    用于双窗口模式，前端无需再发独立的 API 请求加载下面窗口数据。

    返回格式与主级别 result 一致，前端可直接用作 dualBottomData。
    """
    t_start = time.time()
    sub_kl_type = _get_kl_type(sub_freq)
    sub_kl_list = chan[sub_kl_type]

    date_fmt = _get_date_fmt(sub_freq)

    # 1. 提取子级别原始K线数据（从 KLU 对象中提取，用于 MACD 计算）
    raw_records = []
    for klc in sub_kl_list.lst:
        for klu in klc.lst:
            try:
                dt = datetime.strptime(klu.time.to_str(), "%Y/%m/%d %H:%M")
            except:
                dt = datetime.strptime(klu.time.to_str() + " 00:00", "%Y/%m/%d %H:%M")
            raw_records.append({
                "dt": dt,
                "open": klu.open,
                "high": klu.high,
                "low": klu.low,
                "close": klu.close,
                "vol": getattr(klu, 'vol', 0),
                "amount": getattr(klu, 'amount', 0),
            })

    # 2. 计算 MACD
    closes = [r["close"] for r in raw_records]
    macd_list = calculate_macd(closes)

    # 3. 构建 kline_data
    kline_data = []
    for i, row in enumerate(raw_records):
        macd = macd_list[i] if i < len(macd_list) else {"dif": 0, "dea": 0, "macd": 0}
        kline_data.append({
            "date": row["dt"].strftime(date_fmt),
            "timestamp": int(row["dt"].timestamp()) * 1000,
            "open": row["open"], "high": row["high"],
            "low": row["low"], "close": row["close"],
            "vol": row["vol"], "amount": row["amount"],
            "dif": round(macd["dif"], 4),
            "dea": round(macd["dea"], 4),
            "macd": round(macd["macd"], 4),
        })

    # 4. 构建 bi_data
    bi_data = []
    for bi in sub_kl_list.bi_list:
        direction = "up" if bi.is_up() else "down"
        begin_val = bi.get_begin_val()
        end_val = bi.get_end_val()
        power = abs(end_val - begin_val)
        begin_klu = bi.get_begin_klu()
        end_klu = bi.get_end_klu()
        sdt = begin_klu.time.to_str() if begin_klu else ""
        edt = end_klu.time.to_str() if end_klu else ""
        try:
            sdt_dt = datetime.strptime(sdt, "%Y/%m/%d %H:%M")
            sdt_str = sdt_dt.strftime(date_fmt)
            sdt_ts = int(sdt_dt.timestamp()) * 1000
        except:
            sdt_str = sdt
            sdt_ts = 0
        try:
            edt_dt = datetime.strptime(edt, "%Y/%m/%d %H:%M")
            edt_str = edt_dt.strftime(date_fmt)
            edt_ts = int(edt_dt.timestamp()) * 1000
        except:
            edt_str = edt
            edt_ts = 0

        # 子级别的 fx_a_raw_dt/fx_b_raw_dt（分型肩部时间，用于红框定位）
        # 和主级别共用同一个函数，逻辑完全一致
        begin_fx_idx = getattr(bi, 'begin_fx_idx', -1)
        end_fx_idx = getattr(bi, 'end_fx_idx', -1)
        fx_a_raw_dt = ""
        fx_b_raw_dt = ""
        date_fmt_sub = _get_date_fmt(sub_freq)
        shoulder_times = _get_main_bi_time_range(bi, date_fmt_sub)
        if shoulder_times:
            fx_a_raw_dt, fx_b_raw_dt, _, _ = shoulder_times

        bi_data.append({
            "sdt": sdt_str, "edt": edt_str,
            "sdt_ts": sdt_ts, "edt_ts": edt_ts,
            "direction": direction,
            "fx_a_price": round(begin_val, 2),
            "fx_b_price": round(end_val, 2),
            "high": round(bi._high(), 2),
            "low": round(bi._low(), 2),
            "power": round(power, 2),
            "is_sure": getattr(bi, 'is_sure', True),
            "end_fx_idx": end_fx_idx if end_fx_idx is not None else -1,
            "begin_fx_idx": begin_fx_idx if begin_fx_idx is not None else -1,
            "fx_a_raw_dt": fx_a_raw_dt,
            "fx_b_raw_dt": fx_b_raw_dt,
            "fx_a_sub_dt": "",
            "fx_b_sub_dt": "",
        })

    # 5. 构建 fx_data
    fx_data = []
    for klc in sub_kl_list.lst:
        if klc.fx == FX_TYPE.TOP:
            mark = "G"
            price = klc.high
            klu = klc.get_high_peak_klu()
            fx_date = klu.time.toFmtStr(_get_date_fmt(sub_freq))
            try:
                fx_dt = datetime.strptime(fx_date, date_fmt)
                fx_ts = int(fx_dt.timestamp()) * 1000
            except:
                fx_ts = 0
            fx_data.append({
                "date": fx_date, "timestamp": fx_ts,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })
        elif klc.fx == FX_TYPE.BOTTOM:
            mark = "D"
            price = klc.low
            klu = klc.get_low_peak_klu()
            fx_date = klu.time.toFmtStr(_get_date_fmt(sub_freq))
            try:
                fx_dt = datetime.strptime(fx_date, date_fmt)
                fx_ts = int(fx_dt.timestamp()) * 1000
            except:
                fx_ts = 0
            fx_data.append({
                "date": fx_date, "timestamp": fx_ts,
                "mark": mark, "price": price,
                "high": klc.high, "low": klc.low,
            })

    # 6. 构建 zs_data
    zs_data = []
    for zs in sub_kl_list.zs_list:
        zs_data.append({
            "sdt": zs.begin.time.toFmtStr(date_fmt),
            "edt": zs.end.time.toFmtStr(date_fmt),
            "confirm_edt": _calc_zs_confirm_edt_from_bis(zs, sub_kl_list.bi_list, date_fmt),
            "zg": round(zs.high, 2),
            "zd": round(zs.low, 2),
            "gg": round(zs.peak_high, 2),
            "dd": round(zs.peak_low, 2),
            "dir": "up" if zs.bi_in and zs.bi_in.is_up() else "down",
        })

    # 7. 构建 zs_stars
    zs_stars = []
    for zs in sub_kl_list.zs_list:
        if zs.bi_in is None:
            continue
        entry_bi = zs.bi_in
        begin_klu = entry_bi.get_begin_klu()
        if begin_klu is None:
            continue
        try:
            sdt_raw = begin_klu.time.to_str()
            sdt_dt = datetime.strptime(sdt_raw, "%Y/%m/%d %H:%M")
            star_date = sdt_dt.strftime(date_fmt)
        except:
            star_date = sdt_raw
        star_price = entry_bi.get_begin_val()
        if entry_bi.is_up():
            zs_stars.append({
                "date": star_date, "price": round(star_price, 2),
                "mark": "D", "color": "red",
            })
        else:
            zs_stars.append({
                "date": star_date, "price": round(star_price, 2),
                "mark": "G", "color": "green",
            })

    # 8. 构建 bsp_data（从子级别 bs_point_lst 中提取）
    bsp_data = []
    try:
        for bsp in sub_kl_list.bs_point_lst.bsp_iter():
            klu = bsp.klu
            bsp_date = klu.time.toFmtStr(_get_date_fmt(sub_freq))
            try:
                bsp_dt = datetime.strptime(bsp_date, date_fmt)
                bsp_ts = int(bsp_dt.timestamp()) * 1000
            except:
                bsp_ts = 0
            bsp_data.append({
                "date": bsp_date, "timestamp": bsp_ts,
                "type": bsp.type2str(),
                "is_buy": bsp.is_buy,
                "price": klu.close,
                "high": klu.high,
                "low": klu.low,
            })
    except Exception as e:
        print(f"[stock][调试] 子级别获取买卖点失败: {e}")

    # 9. 构建 seg_data
    seg_data = []
    for seg in sub_kl_list.seg_list:
        direction = "up" if seg.is_up() else "down"
        begin_klu = seg.get_begin_klu()
        end_klu = seg.get_end_klu()
        sdt = (begin_klu.time.toFmtStr(_get_date_fmt(sub_freq))) if begin_klu else ""
        edt = (end_klu.time.toFmtStr(_get_date_fmt(sub_freq))) if end_klu else ""
        begin_price = round(begin_klu.low, 2) if begin_klu else round(seg._low(), 2)
        end_price = round(end_klu.high, 2) if end_klu else round(seg._high(), 2)
        if direction == "down":
            begin_price = round(begin_klu.high, 2) if begin_klu else round(seg._high(), 2)
            end_price = round(end_klu.low, 2) if end_klu else round(seg._low(), 2)
        seg_data.append({
            "sdt": sdt, "edt": edt,
            "direction": direction,
            "begin_price": begin_price,
            "end_price": end_price,
            "high": round(seg._high(), 2),
            "low": round(seg._low(), 2),
            "amp": round(seg.amp(), 2),
        })

    # 10. 组装结果
    sub_result = {
        "meta": {
            "symbol": f"{code}.{market.upper()}",
            "name": "",
            "freq": _get_freq_label(sub_freq),
            "chan_version": "chan.py",
            "kline_count": len(kline_data),
            "bi_count": len(bi_data),
            "fx_count": len(fx_data),
            "zs_count": len(zs_data),
            "seg_count": len(seg_data),
            "bsp_count": len(bsp_data),
            "date_range": f"{kline_data[0]['date']} ~ {kline_data[-1]['date']}" if kline_data else "",
            "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        },
        "klines": kline_data,
        "bis": bi_data,
        "fxs": fx_data,
        "zs": zs_data,
        "zs_stars": zs_stars,
        "segs": seg_data,
        "bsps": bsp_data,
    }

    print(f"[stock][耗时] 子级别({sub_freq})提取: {time.time()-t_start:.3f}s (K线={len(kline_data)} 笔={len(bi_data)} 分型={len(fx_data)} 中枢={len(zs_data)} 线段={len(seg_data)} 买卖点={len(bsp_data)})")
    return sub_result


# ============================================================
# 区间套背驰判断
# ============================================================
# 高级别→低级别周期映射（与双窗口 getDualBottomFreq 一致）
_SUB_FREQ_MAP = {'w': 'd', 'd': '30m', '30m': '5m'}

# 期货双窗口周期映射：上窗周期 → 下窗周期
_FUTURES_DUAL_FREQ_MAP = {
    "30m": "5m",
    "5m": "1m",
    "1m": "15s",
}

# 期货双窗口反向映射：下窗周期 → 上窗周期（用于切换时确定上窗周期）
_FUTURES_DUAL_REVERSE_MAP = {
    "5m": "30m",
    "1m": "5m",
    "15s": "1m",
}

# 期货分析缓存（供 /api/red_range_zs 等访问）
# key: "symbol:freq"  (如 "KQ.m@CFFEX.IM:5m")，当前 value: CChan 对象
# 后续可扩展为 {records, chan, result} 三元组，key可加前缀区分（single_/dual_main_/dual_sub_）
_futures_analysis_cache = {}


def compute_red_range_zs(code, sub_freq='d', left_date='', right_date='', end_date=None):
    """
    双窗口红框中枢计算：前端传来红框的左右边界时间 [left_date, right_date]，
    后端内部调用 _find_sub_bi_sequence 找到被红框完全覆盖的子级别笔，再
    用 _find_sub_zs 重新计算中枢，返回给前端绘制。

    参数:
        code:       股票代码（如 "SH000001" 或 "000001.SH"）
        sub_freq:   子级别周期（如 "30m", "5m"）
        left_date:  红框左边界时间字符串（子级别K线格式，如 "2025-06-15 10:00"）
        right_date: 红框右边界时间字符串
        end_date:   复盘日期（None=实时模式，有值=复盘模式），用于精确匹配缓存 key

    返回: {"zs": [...]} 或 {"error": "..."}
    """
    import re
    normalized_code = code.strip().upper()

    # ── 期货双窗口 ──
    if normalized_code.startswith("KQ."):
        cache_key = f"{normalized_code}:{sub_freq}"
        print(f"[dual_zs][期货] cache_key={cache_key}, left_date={left_date}, right_date={right_date}, sub_freq={sub_freq}")
        cached = _futures_analysis_cache.get(cache_key)
        if cached is None:
            print(f"[dual_zs][期货] 缓存未命中! 可用key: {list(_futures_analysis_cache.keys())}")
            return {"error": "双窗口下窗缓存已过期，请重新打开双窗口"}
        chan = cached
        kl_list = chan[_get_kl_type(sub_freq)]
        bi_list = kl_list.bi_list
        print(f"[dual_zs][期货] bi_list长度={len(bi_list)}, kl_list长度={len(kl_list)}")
        date_fmt = _get_date_fmt(sub_freq)
        print(f"[dual_zs][期货] date_fmt={date_fmt}")
        start_bi, end_bi = _find_sub_bi_sequence(left_date, right_date, bi_list, sub_freq)
        if start_bi is None:
            return {"error": f"红框内无完整笔: [{left_date}, {right_date}]"}
        sliced_bis = bi_list[start_bi:end_bi + 1]
        print(f"[dual_zs][期货] sliced_bis长度={len(sliced_bis)}, start_bi={start_bi}, end_bi={end_bi}")
        for i, bi in enumerate(sliced_bis[:3]):
            bku = bi.get_begin_klu()
            eku = bi.get_end_klu()
            sdt = bku.time.toFmtStr(date_fmt) if bku else "None"
            edt = eku.time.toFmtStr(date_fmt) if eku else "None"
            dir_str = "up" if bi.is_up() else "down"
            print(f"[dual_zs][期货]   bi[{start_bi+i}] sdt={sdt} edt={edt} dir={dir_str}")
        zs_data = _find_sub_zs(sliced_bis, bi_list, date_fmt)
        print(f"[dual_zs][期货] ZS结果: zs_data长度={len(zs_data)}")
        if zs_data:
            for zs in zs_data:
                print(f"[dual_zs][期货]   ZS: sdt={zs.get('sdt')}, edt={zs.get('edt')}, zg={zs.get('zg'):.2f}, zd={zs.get('zd'):.2f}")
        return {"zs": zs_data, "start_bi": start_bi, "end_bi": end_bi}

    # ── 股票双窗口 ──
    market = None
    prefix_match = re.match(r'^(SH|SZ|HK)(\d+)$', normalized_code)
    suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK)$', normalized_code)
    if prefix_match:
        market = prefix_match.group(1).lower()
        normalized_code = prefix_match.group(2)
    elif suffix_match:
        normalized_code = suffix_match.group(1)
        market = suffix_match.group(2).lower()

    if not market:
        return {"error": f"无法识别股票代码: {code}"}

    date_suffix = end_date if end_date else "live"
    cache_key = f"single_{market}_{normalized_code}_{sub_freq}_{date_suffix}"
    cached = _cache_get(cache_key)

    # 双窗口新模式：当前 sub_freq 通常是下面窗口频率，优先从 dual_main 主级别缓存中的多级别 CChan 取子级别笔列表。
    # dual_sub 缓存只存 result/records，不存 chan；真正可用于重算中枢的 CChan 在 dual_main 缓存里。
    if (cached is None or "chan" not in cached) and sub_freq in _SUB_FREQ_MAP.values():
        for main_freq, _sub in _SUB_FREQ_MAP.items():
            if _sub == sub_freq:
                dual_main_cache_key = f"dual_main_{market}_{normalized_code}_{main_freq}_{date_suffix}"
                main_cached = _cache_get(dual_main_cache_key)
                if main_cached and "chan" in main_cached:
                    main_chan = main_cached["chan"]
                    try:
                        _ = main_chan[_get_kl_type(sub_freq)]
                        cached = {"chan": main_chan}
                        break
                    except Exception as e:
                        print(f"[警告] 异常: {type(e).__name__}: {e}")
                if cached is None or "chan" not in cached:
                    single_main_cache_key = f"single_{market}_{normalized_code}_{main_freq}_{date_suffix}"
                    main_cached = _cache_get(single_main_cache_key)
                    if main_cached and "chan" in main_cached:
                        main_chan = main_cached["chan"]
                        try:
                            _ = main_chan[_get_kl_type(sub_freq)]
                            cached = {"chan": main_chan}
                            print(f"[stock][信息] compute_red_range_zs 从单窗口主级别缓存({main_freq})获取子级别({sub_freq})数据")
                            break
                        except Exception as e:
                            print(f"[警告] 异常: {type(e).__name__}: {e}")

    if cached is None:
        return {"error": "请先在该周期下加载K线数据"}
    if "chan" not in cached:
        print(f"[stock][信息] 缓存中无chan对象，重新分析 {normalized_code} {sub_freq}")
        analyze_stock(f"{normalized_code}.{market.upper()}", freq=sub_freq, cache_chan=True)
        cached = _cache_get(cache_key)
        if cached is None or "chan" not in cached:
            return {"error": "缓存中无分析数据，请重新查询"}

    chan = cached["chan"]
    kl_list = chan[_get_kl_type(sub_freq)]
    bi_list = kl_list.bi_list

    date_fmt = _get_date_fmt(sub_freq)

    # ── 步骤③：后端找被红框完全覆盖的笔 ──
    start_bi, end_bi = _find_sub_bi_sequence(left_date, right_date, bi_list, sub_freq)
    if start_bi is None:
        return {"error": f"红框内无完整笔: [{left_date}, {right_date}]"}

    sliced_bis = bi_list[start_bi:end_bi + 1]
    zs_data = _find_sub_zs(sliced_bis, bi_list, date_fmt)
    return {"zs": zs_data, "start_bi": start_bi, "end_bi": end_bi}


def stock_manual_select_point(code, freq='d', bi_idx=-1):
    """
    手选进入段：找到左肩原始K线时间T，销毁旧CChan实例及所有中间状态，
    从T重新加载K线数据并创建全新CChan实例，返回完整chartData给前端渲染。

    流程：
    1. 通过前端传来的笔索引，找到分型左肩第一根原始K线时间T
    2. 保存T到CSV
    3. 销毁旧CChanA及_stocks_analysis_cache中的全部中间状态，回收内存
    4. 从T开始重新读取通达信K线，创建CChanB，返回完整结果
    """
    # 标准化代码
    normalized_code = code.strip().upper()
    market = None
    prefix_match = re.match(r'^(SH|SZ|HK)(\d+)$', normalized_code)
    suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK)$', normalized_code)
    if prefix_match:
        market = prefix_match.group(1).lower()
        normalized_code = prefix_match.group(2)
    elif suffix_match:
        normalized_code = suffix_match.group(1)
        market = suffix_match.group(2).lower()
    date_suffix = "live"
    cache_key = f"single_{market}_{normalized_code}_{freq}_{date_suffix}"
    qualified_code = f"{normalized_code}.{market.upper()}"  # 区分沪市深市同号股票
    cached = _cache_get(cache_key)
    if cached is None:
        return {"error": "请先查询该股票"}

    if "chan" not in cached:
        # 扫描缓存只有result没有chan，重新分析以获取完整数据
        print(f"[stock][信息] 缓存中无chan对象，重新分析 {normalized_code} {freq}")
        analyze_stock(normalized_code, freq=freq, cache_chan=True)
        cached = _cache_get(cache_key)
        if cached is None or "chan" not in cached:
            return {"error": "缓存中无分析数据，请重新查询"}

    chan = cached["chan"]
    kl_list = chan[_get_kl_type(freq)]
    bi_list = kl_list.bi_list

    target_bi_idx = int(bi_idx)
    if target_bi_idx < 0 or target_bi_idx >= len(bi_list):
        return {"error": f"笔索引 {bi_idx} 越界，笔总数 {len(bi_list)}"}

    # 检查：选点之后至少需要4笔才能构建中枢（三笔重叠+确认判断）
    remaining_bis = len(bi_list) - target_bi_idx - 1
    if remaining_bis < 4:
        return {"error": f"选点之后仅剩 {remaining_bis} 笔，至少需要4笔才能构建中枢，请重新选点"}

    # Step 1: 找到左肩原始K线时间T
    start_time = _find_left_shoulder_time(kl_list, bi_list, target_bi_idx, freq)
    if start_time is None:
        return {"error": "无法定位左肩K线时间，请重试"}

    # Step 2: 保存选点到CSV（保存的是左肩第一根原始K线的时间T）
    stock_name = cached.get("result", {}).get("meta", {}).get("name", "")
    _save_point_time(qualified_code, stock_name, freq, start_time)
    if qualified_code not in _saved_point_times:
        _saved_point_times[qualified_code] = {}
    _saved_point_times[qualified_code]["name"] = stock_name
    _saved_point_times[qualified_code][FREQ_TO_COL.get(freq, "")] = start_time

    # Step 3: 销毁旧CChanA及所有中间状态，回到冷启动前的干净状态
    import gc
    if cache_key in _stocks_analysis_cache:
        with _cache_lock:
            if cache_key in _stocks_analysis_cache:
                del _stocks_analysis_cache[cache_key]
    gc.collect()

    # Step 4: 从T开始重新加载K线，创建CChanB，返回完整chartData
    result = _analyze_stock_internal(f"{normalized_code}.{market.upper()}", freq=freq, start_time=start_time)
    return result


def futures_manual_select_point(symbol, freq="15s", bi_idx="0"):
    """
    期货期指手选进入段：与股票 stock_manual_select_point 逻辑一致。
    创建临时 TqApi → 拉取全量历史 → 找到左肩时间T → 保存CSV →
    创建新 TqApi → 从T重新拉取 → 创建新CChan → 返回完整快照。
    """
    import time
    from tqsdk import TqApi, TqAuth
    from DataAPI.TqSdkAPI import (
        FREQ_SEC_MAP, FREQ_LABEL_CN, CTqSdkAPI,
        fetch_futures_kline,
        TQ_ACCOUNT, TQ_PASSWORD, FUTURES_ALIASES,
    )

    # 别名解析
    symbol_upper = symbol.upper()
    if symbol_upper in FUTURES_ALIASES:
        symbol = FUTURES_ALIASES[symbol_upper]

    freq_sec = FREQ_SEC_MAP.get(freq, 15)
    freq_label = freq
    freq_cn = FREQ_LABEL_CN.get(freq_label, freq_label)
    display_key = f"{symbol}:{freq_cn}"
    target_bi_idx = int(bi_idx)

    api = None
    api2 = None
    try:
        t_conn = time.time()
        api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
        print(f"[{display_key}] ⓪ 临时连接天勤(选点): 耗时 {time.time()-t_conn:.1f}s")

        records = fetch_futures_kline(api, symbol, freq_sec=freq_sec, display_key=display_key)
        if len(records) < 5:
            return {"error": f"K线数据不足: 仅{len(records)}条"}

        # 注入数据源 + 创建 CChan
        CTqSdkAPI.set_data(records, symbol=f"{symbol}:{freq_sec}")

        from Common.CEnum import KL_TYPE, AUTYPE
        from Chan import CChan

        _freq_to_kl = {
            15: KL_TYPE.K_15S, 30: KL_TYPE.K_30S, 60: KL_TYPE.K_1M,
            300: KL_TYPE.K_5M, 900: KL_TYPE.K_15M, 1800: KL_TYPE.K_30M,
            3600: KL_TYPE.K_60M, 86400: KL_TYPE.K_DAY,
            604800: KL_TYPE.K_WEEK, 2592000: KL_TYPE.K_MON,
        }
        kl_type = _freq_to_kl.get(freq_sec, KL_TYPE.K_15S)

        config = _make_chan_config()

        chan = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan.step_load():
            pass

        kl_list = chan[kl_type]
        bi_list = kl_list.bi_list

        if target_bi_idx < 0 or target_bi_idx >= len(bi_list):
            return {"error": f"笔索引 {bi_idx} 越界，笔总数 {len(bi_list)}"}

        # 检查选点后至少需要4笔
        remaining_bis = len(bi_list) - target_bi_idx - 1
        if remaining_bis < 4:
            return {"error": f"选点之后仅剩 {remaining_bis} 笔，至少需要4笔才能构建中枢，请重新选点"}

        # Step 2: 找到左肩时间T
        start_time = _find_left_shoulder_time(kl_list, bi_list, target_bi_idx, freq)
        if start_time is None:
            return {"error": "无法定位左肩K线时间，请重试"}

        print(f"[{display_key}] 选点左肩时间: {start_time}")

        # Step 3: 保存选点到CSV
        name = _get_futures_name(symbol)
        _save_point_time(symbol, name, freq, start_time)
        if symbol not in _saved_point_times:
            _saved_point_times[symbol] = {}
        _saved_point_times[symbol]["name"] = name
        _saved_point_times[symbol][FREQ_TO_COL.get(freq, "")] = start_time

        # Step 4: 关闭旧TqApi，创建新TqApi，从T重新拉取
        if api is not None:
            try:
                api.close()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
            api = None

        t_conn2 = time.time()
        api2 = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
        print(f"[{display_key}] ⓪ 重新连接天勤(选点后): 耗时 {time.time()-t_conn2:.1f}s")

        records2 = fetch_futures_kline(api2, symbol, freq_sec=freq_sec,
                                       display_key=display_key, start_time=start_time)
        if len(records2) < 5:
            return {"error": f"选点后K线数据不足: 仅{len(records2)}条"}

        # 注入数据源 + 创建新 CChan
        CTqSdkAPI.set_data(records2, symbol=f"{symbol}:{freq_sec}")

        chan2 = CChan(
            code=f"{symbol}:{freq_sec}", begin_time=None, end_time=None,
            data_src="custom:TqSdkAPI.CTqSdkAPI",
            lv_list=[kl_type], config=config, autype=AUTYPE.NONE,
            market_type="futures",
        )
        for _snapshot in chan2.step_load():
            pass

        # Step 5: 提取快照并返回
        result = _extract_realtime_snapshot(chan2, kl_type, symbol, name, freq_label,
                                            saved_selection_date=start_time)
        # 计算白色横虚线
        _kl_list = chan2[kl_type]
        _date_fmt = _get_date_fmt(freq)
        result['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _date_fmt)
        print(f"[{display_key}] 选点完成: {len(result['klines'])}K线, {result['meta']['bi_count']}笔, {result['meta']['zs_count']}中枢")
        return result

    except Exception as e:
        import traceback
        print(f"[{display_key}] 选点异常: {e}")
        traceback.print_exc()
        return {"error": f"选点失败: {str(e)}"}
    finally:
        if api is not None:
            try:
                api.close()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
        if api2 is not None:
            try:
                api2.close()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")


def analyze_stock(code, freq="d", end_date=None, cache_chan=True, dual=False, step=None):
    """公开分析入口：先识别市场，再分流到股票或期货的并列内部流程。
    股票/指数：走通达信数据源，支持 cache_chan 和 dual 双窗口。
    期货/期指：走天勤数据源，dual=True 时使用两个独立 CChan 对象。
    """
    market, normalized_code = _get_market_code(code)
    if not market:
        return {"error": f"无法识别股票代码: {code}"}
    if market == 'futures':
        return _analyze_futures_internal(normalized_code, freq=freq, end_date=end_date, dual=dual, step=step)
    stock_code = f"{normalized_code}.{market.upper()}"
    return _analyze_stock_internal(stock_code, freq=freq, end_date=end_date, cache_chan=cache_chan, dual=dual, step=step)


# ============================================================
# 扫描预过滤（流通市值 + MA120）
# ============================================================
def _quick_prefilter_pass(market, code, freq):
    """
    快速预过滤：加载K线数据，检查流通市值和MA120条件。
    用于中证1000等大范围扫描时提前跳过不满足条件的股票。
    返回 (pass_filter, float_mv, below_ma120)：
      - pass_filter=True 表示通过过滤，可以继续分析
      - pass_filter=False 表示应跳过
    """
    try:
        records = read_main_level_records(market, code, freq)
        if not records or len(records) < 120:
            return (True, None, None)  # 数据不足，不跳过

        closes = [r["close"] if isinstance(r, dict) else r.close for r in records[-120:]]
        last_close = closes[-1]
        ma120 = sum(closes) / 120
        below_ma120 = last_close < ma120

        shares = get_float_shares_from_xdxr(market, code)
        float_mv = None
        if shares:
            float_mv = (shares * last_close) / 1e8  # 亿元
            if float_mv < 50:
                return (False, float_mv, below_ma120)  # 流通市值<50亿，跳过

        if below_ma120:
            return (False, float_mv, below_ma120)  # 价格在120周期线下方，跳过

        return (True, float_mv, below_ma120)
    except Exception as e:
        import traceback
        print(f"[预过滤] {code} 异常: {type(e).__name__}: {e}")
        traceback.print_exc()
        return (True, None, None)  # 出错时不跳过，让完整分析处理


def scan_zxg_buy_points(freq="d"):
    """
    批量扫描自选股，找出当天（最后一根K线）有买点的股票。
    扫描与冷启动共用同一个 _stocks_analysis_cache，缓存完整的 {records, chan, result}。
    无论有无买卖点都不主动清除缓存，由 LRU 20 条 + 内存保护机制统一管理。
    返回列表，每项包含：code, name, market, buy_points(买点列表)
    """
    import gc

    stocks = read_zxg_stocks()
    if not stocks:
        return {"error": "自选股为空或文件不存在", "total": 0, "results": []}

    results = []
    skipped = 0
    total = len(stocks)

    print(f"[自选扫描] 开始扫描 {total} 只自选股，周期={_get_freq_label(freq)}")

    for idx, stk in enumerate(stocks):
        # 内存保护：每10只检查一次
        if (idx + 1) % 10 == 0:
            _check_memory_and_protect()

        code = stk["code"]
        prefix = stk["prefix"]
        # 转换为市场标识
        if prefix == "1":
            market = "sh"
        elif prefix == "0":
            market = "sz"
        elif prefix == "2":
            market = "bj"  # 北交所，通达信可能没有数据
        else:
            continue

        try:
            # 自选股扫描：用 prefix 拼出带市场前缀的代码（如 SH000852），
            # 让 _get_stock_market_code 通过 SH/SZ 前缀精确识别，不依赖 0xxxxx→sz 的硬编码推断
            _PREFIX_MAP = {"0": "SZ", "1": "SH", "2": "BJ"}
            market_prefix = _PREFIX_MAP.get(prefix)
            if not market_prefix:
                continue
            qualified_code = market_prefix + code  # 如 SH000852

            # 扫描串行化：加锁防止并发创建多个CChan导致内存峰值
            # cache_chan=True：与冷启动一致，缓存完整的 {records, chan, result}
            with _scan_lock:
                result = analyze_stock(qualified_code, freq=freq, cache_chan=True)
            if "error" in result:
                skipped += 1
                continue

            bsps = result.get("bsps", [])
            stock_name = result.get("meta", {}).get("name", f"{code}.{market.upper()}")
            # [DEBUG] 打印扫描中meta信息
            print(f"[DEBUG-名称] scan_zxg_buy_points({code}, {freq}) meta.name='{result.get('meta', {}).get('name')}', meta={result.get('meta', {})}")
            print(f"[DEBUG-名称] scan_zxg_buy_points({code}, {freq}) stock_name='{stock_name}'")

            # 筛选最后一根K线上的买点
            klines = result.get("klines", [])
            if not klines:
                skipped += 1
                continue
            last_kline_date = klines[-1]["date"]

            today_buy_points = []
            for bsp in bsps:
                if bsp.get("is_buy", False) and bsp.get("date", "") == last_kline_date:
                    today_buy_points.append({
                        "type": bsp.get("type", ""),
                        "price": bsp.get("price", 0),
                        "date": bsp.get("date", ""),
                    })

            if today_buy_points:
                # 有买点：缓存已由 analyze_stock 写入（完整三项），后续点击可直接查看
                results.append({
                    "code": code + "." + market.upper(),
                    "name": stock_name,
                    "market": market,
                    "buy_points": today_buy_points,
                    "last_close": klines[-1]["close"],
                })
                print(f"[自选扫描] ({idx+1}/{total}) {stock_name}({code}) 发现 {len(today_buy_points)} 个买点")
            # 无买点也不清除缓存，由 LRU 自然淘汰

            # 进度反馈（每10只打印一次）
            if (idx + 1) % 10 == 0 or idx == total - 1:
                print(f"[自选扫描] 进度: {idx+1}/{total}, 已发现 {len(results)} 只买点股")

        except Exception as e:
            print(f"[自选扫描] ({idx+1}/{total}) {code} 分析异常: {e}")

    # 扫描完毕，触发一次GC回收
    gc.collect()
    print(f"[自选扫描] 扫描完成: 共{total}只, 扫描{total-skipped}只, 跳过{skipped}只, 发现{len(results)}只有买/卖点")
    return {"error": None, "total": total, "skipped": skipped, "results": results}


# ============================================================
# HTTP 服务器
# ============================================================
class ChartHandler(SimpleHTTPRequestHandler):
    """HTTP请求处理器"""
    def handle_one_request(self):
        """静默处理客户端断开连接，避免 ConnectionAbortedError 日志噪音"""
        try:
            super().handle_one_request()
        except ConnectionAbortedError:
            pass
        except ConnectionResetError:
            pass

    def do_GET(self):
        global _scan_aborted, _scan_start_time
        parsed = urlparse(self.path)
        if parsed.path == "/api/stock":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            end_date = params.get("end_date", [""])[0] or None
            step = params.get("step", [None])[0]
            dual = params.get("dual", ["0"])[0] == "1"
            if not code:
                self.send_json_response({"error": "请输入股票代码"}, 400)
                print_memory(f"前端操作(查询股票-{code or '空'})")
                return
            try:
                result = analyze_stock(code, freq=freq, end_date=end_date, dual=dual, step=step)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
                    # 非复盘模式：持久化当前股票代码，下次冷启动自动恢复
                    # 双窗口下面窗口的请求不保存，只保存上面窗口的周期
                    if not end_date and not params.get("dual_bottom", [""])[0]:
                        _save_last_stock(code, freq)
            except Exception as e:
                import traceback
                print(f"[错误] analyze_stock异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
            print_memory(f"前端操作(查询股票-{code})")
        elif parsed.path == "/api/stocks_manual_select_point":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            bi_idx = params.get("bi_idx", ["-1"])[0]
            if not code or bi_idx == "-1":
                self.send_json_response({"error": "缺少必要参数 code 或 bi_idx"}, 400)
                return
            try:
                result = stock_manual_select_point(code, freq=freq, bi_idx=bi_idx)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
            except Exception as e:
                import traceback
                print(f"[错误] stock_manual_select_point异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
        elif parsed.path == "/api/red_range_zs":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            left_date = params.get("left_date", [""])[0]
            right_date = params.get("right_date", [""])[0]
            end_date = params.get("end_date", [""])[0] or None
            print(f"[dual_zs][handler] 收到请求: code={code}, freq={freq}, left={left_date}, right={right_date}, end_date={end_date}")
            if not code or not left_date or not right_date:
                print(f"[dual_zs][handler] 参数错误: code/left_date/right_date 为空")
                self.send_json_response({"error": "参数错误: code/left_date/right_date 不能为空"}, 400)
                return
            try:
                result = compute_red_range_zs(code, sub_freq=freq, left_date=left_date, right_date=right_date, end_date=end_date)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
            except Exception as e:
                import traceback
                print(f"[错误] dual_zs异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
        elif parsed.path == "/api/search":
            params = parse_qs(parsed.query)
            keyword = params.get("q", [""])[0]
            if not keyword:
                self.send_json_response({"error": "请输入搜索关键词"}, 400)
                return
            _load_stock_names_from_cache_file()
            keyword_upper = keyword.upper()
            exact_results = []
            exact_pinyin_results = []  # 拼音或名称完全匹配（如输入"LG"精确匹配"柳工"）
            prefix_results = []
            other_results = []

            for compound_key, info in _stock_names_cache.items():
                if isinstance(info, dict):
                    name = info.get("name", "")
                    pinyin = info.get("pinyin", "")
                    market = info.get("market", "")
                else:
                    name = info
                    pinyin = ""
                    market = ""
                # 归一化：全角ASCII → 半角（兼容旧缓存中"鲁泰Ａ"→"鲁泰A"）
                name = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in name)
                pinyin = "".join(chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E else c for c in pinyin)

                if not name:
                    continue

                # 从复合键中提取纯代码（去掉市场前缀）
                if market and compound_key.startswith(market):
                    bare_code = compound_key[len(market):]
                else:
                    bare_code = compound_key

                # 匹配：用户输入匹配纯代码、名称或拼音
                if not (keyword_upper in bare_code or keyword_upper in name.upper() or keyword_upper in pinyin):
                    continue

                if not market:
                    if len(bare_code) == 5 and bare_code.isdigit():
                        market = "hk"
                    elif bare_code.startswith("6") or bare_code.startswith("5") or bare_code.startswith("9"):
                        market = "sh"
                    elif bare_code.startswith("0") or bare_code.startswith("3") or bare_code.startswith("2"):
                        market = "sz"
                    elif bare_code.startswith("88") or bare_code.startswith("99"):
                        market = "sh"
                    else:
                        market = "sz"

                item = {"code": bare_code, "name": name, "pinyin": pinyin, "market": market, "type": ""}
                if bare_code == keyword_upper:
                    exact_results.append(item)
                elif pinyin == keyword_upper or name.upper() == keyword_upper:
                    exact_pinyin_results.append(item)
                elif bare_code.startswith(keyword_upper):
                    prefix_results.append(item)
                else:
                    other_results.append(item)

            results = exact_results[:10] + exact_pinyin_results[:10] + prefix_results[:10] + other_results[:10]
            results = results[:10]

            # 期货/期指搜索：匹配本地品种名称表
            try:
                from DataAPI.TqSdkAPI import DEFAULT_FUTURES_SYMBOLS
                _futures_search = _get_futures_name  # 使用本地函数
                for sym, name, freq_sec, freq_label in DEFAULT_FUTURES_SYMBOLS:
                    if keyword_upper in sym.upper() or keyword_upper in name.upper():
                        # 避免重复（如果主连代码恰好命中）
                        if not any(r["code"] == sym for r in results):
                            results.append({
                                "code": sym, "name": name, "pinyin": "",
                                "market": "futures", "type": freq_label,
                            })
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

            # 期货别名搜索：支持用户输入短名称搜索（如 PTA、IF、rb 等）
            for alias, full_code in FUTURES_ALIASES.items():
                if keyword_upper in alias.upper():
                    name = _get_futures_name(full_code)
                    # 避免重复
                    if not any(r["code"] == full_code for r in results):
                        results.append({
                            "code": full_code, "name": name, "pinyin": alias,
                            "market": "futures", "type": "",
                        })

            # 本地缓存未找到或不够，再查东方财富API补充（已注释掉，避免频繁请求被拉黑）
            # if len(results) < 10:
            #     try:
            #         import urllib.request
            #         url = f"http://searchapi.eastmoney.com/api/suggest/get?input={urllib.parse.quote(keyword)}&type=14&token=D43BF722C8E33BDC906FB84D85E326E&count=10"
            #         req = urllib.request.Request(url, headers={
            #             "User-Agent": "Mozilla/5.0",
            #             "Referer": "https://quote.eastmoney.com/"
            #         })
            #         with urllib.request.urlopen(req, timeout=5) as resp:
            #             text = resp.read().decode("utf-8", errors="ignore")
            #         data = json.loads(text)
            #         for item in data.get("QuotationCodeTable", {}).get("Data", []):
            #             code = item.get("Code", "")
            #             jys = item.get("JYS", "")
            #             name = item.get("Name", "")
            #             sec_type = item.get("SecurityType", "")
            #             if sec_type not in ("1", "2", "3", "5", "6", "19", "25"):
            #                 continue
            #             if jys not in ("0", "1", "2", "3", "6", "23", "80", "HK"):
            #                 continue
            #             if code in ("002002",):
            #                 continue
            #             if name.startswith("*ST") or name.startswith("ST"):
            #                 continue
            #             if jys == "1" and code == "000001" and "上证" in name:
            #                 code = "999999"
            #             if any(r["code"] == code for r in results):
            #                 continue
            #             if jys == "HK":
            #                 market = "hk"
            #             elif jys in ("1", "2"):
            #                 market = "sh"
            #             elif jys in ("0", "3", "6", "80"):
            #                 market = "sz"
            #             elif jys == "23":
            #                 market = "sh"
            #             else:
            #                 market = "sz"
            #             results.append({
            #                 "code": code,
            #                 "name": name,
            #                 "pinyin": item.get("PinYin", ""),
            #                 "market": market,
            #                 "type": item.get("SecurityTypeName", ""),
            #             })
            #             if len(results) >= 10:
            #                 break
            #     except Exception as e:
            #         import traceback
            #         print(f"[错误] 搜索异常: {e}")
            #         traceback.print_exc()
            self.send_json_response({"results": results}, 200)
        elif parsed.path == "/api/zxg_list":
            # 返回自选股列表（供前端逐只扫描）
            try:
                stocks = read_zxg_stocks()
                self.send_json_response({"stocks": stocks}, 200)
            except Exception as e:
                self.send_json_response({"error": str(e)}, 500)
        elif parsed.path == "/api/scan_stock_list":
            # 返回股票列表（支持逗号分隔多来源：zxg,sz50,hs300,zz500,zz1000 → 合并去重后返回）
            params = parse_qs(parsed.query)
            raw = params.get("source", ["zxg"])[0]
            sources = [s.strip() for s in raw.split(",") if s.strip()]
            _SOURCE_READERS = {
                "zxg": (read_zxg_stocks, "自选股"),
                "sz50": (read_sz50_stocks, "上证50"),
                "hs300": (read_hs300_stocks, "沪深300"),
                "zz500": (read_zz500_stocks, "中证500"),
                "zz1000": (read_zz1000_stocks, "中证1000"),
            }
            # 先读取所有来源的股票列表
            src_stocks = {}  # src → [(code, prefix, stock_dict), ...]
            errors = []
            for src in sources:
                reader = _SOURCE_READERS.get(src)
                if reader is None:
                    errors.append(f"未知来源: {src}")
                    continue
                read_fn, label = reader
                stocks = read_fn()
                if not stocks and src != "zxg":
                    errors.append(f"{label}板块文件不存在，请先在通达信中创建/下载{label}板块")
                    continue
                src_stocks[src] = stocks
            # 合并去重：按出现顺序，非zxg来源优先（zxg先出现 → 后续非zxg覆盖并升级_source）
            merged = []
            seen = {}  # (code, prefix) → index in merged
            for src in sources:
                stocks = src_stocks.get(src)
                if not stocks:
                    continue
                for stk in stocks:
                    key = (stk["code"], stk["prefix"])
                    if key not in seen:
                        stk["_source"] = src
                        seen[key] = len(merged)
                        merged.append(stk)
                    else:
                        exist_idx = seen[key]
                        exist_src = merged[exist_idx].get("_source", "")
                        if exist_src == "zxg" and src != "zxg":
                            # 升级：自选股 → 板块来源，预过滤生效
                            merged[exist_idx]["_source"] = src
            self.send_json_response({
                "stocks": merged,
                "sources": sources,
                "total": len(merged),
                "errors": errors if errors else None
            }, 200)
        elif parsed.path == "/api/scan_one":
            # 扫描单只股票的买卖点（供前端逐只调用，实时显示进度）
            t_scan_start = time.time()
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            prefix = params.get("prefix", [""])[0]
            recent_str = params.get("recent", ["1"])[0]
            source = params.get("source", ["zxg"])[0]  # zxg=自选股, sz50=上证50, hs300=沪深300, zz500=中证500, zz1000=中证1000
            try:
                recent_days = max(1, int(recent_str))
            except ValueError:
                recent_days = 1
            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return
            try:
                _check_memory_and_protect()
                t0 = time.time()
                # 自选股扫描：用 prefix 拼出带市场前缀的代码（如 SH000852）
                # 港股：prefix="hk"，拼出 HK03690，让 _get_stock_market_code 通过 HK 前缀精确识别
                _PREFIX_MAP = {"0": "SZ", "1": "SH", "2": "BJ", "hk": "HK"}
                market_prefix = _PREFIX_MAP.get(prefix, "")
                qualified_code = (market_prefix + code) if market_prefix else code
                market = market_prefix.lower() if market_prefix else ""
                # 检查终止标志：在预过滤之前先检查，避免做无用功
                if _scan_aborted:
                    self.send_json_response({"error": "扫描已终止", "aborted": True}, 200)
                    return
                # 板块扫描（上证50/沪深300/中证500/中证1000）：预过滤（流通市值<50亿 或 价格<120周期线 → 跳过）
                if source != "zxg" and market:
                    t_pre = time.time()
                    pass_filter, pre_mv, pre_below = _quick_prefilter_pass(market, code, freq)
                    t_pre_elapsed = time.time() - t_pre
                    pre_mv_fmt = f"{pre_mv:.2f}" if pre_mv is not None else "?"
                    if not pass_filter:
                        _scan_skip_log.append(f"{code} - 预过滤跳过(流通市值={pre_mv_fmt}亿, 120MA线下={pre_below})")
                        print(f"[预过滤] {code} 跳过 (市值={pre_mv_fmt}亿, 低于120MA={pre_below}), 耗时{t_pre_elapsed:.3f}s")
                        self.send_json_response({"error": "预过滤跳过", "skipped": True, "float_mv": pre_mv, "below_ma120": pre_below}, 200)
                        return
                    print(f"[预过滤] {code} 通过 (市值={pre_mv_fmt}亿, 低于120MA={pre_below}), 耗时{t_pre_elapsed:.3f}s")
                # 检查终止标志：在获取锁之前先检查，避免排队等待
                if _scan_aborted:
                    self.send_json_response({"error": "扫描已终止", "aborted": True}, 200)
                    return
                with _scan_lock:
                    if _scan_aborted:
                        self.send_json_response({"error": "扫描已终止", "aborted": True}, 200)
                        return
                    result = analyze_stock(qualified_code, freq=freq, cache_chan=True)
                t_analyze = time.time() - t0
                if "error" in result:
                    _scan_skip_log.append(f"{code} - {result['error']}")
                    print(f"[耗时-扫描] {code} 分析失败: {result['error']}, 耗时{t_analyze:.3f}s")
                    self.send_json_response({"error": result["error"]}, 200)
                else:
                    t0 = time.time()
                    bsps = result.get("bsps", [])
                    stock_name = result.get("meta", {}).get("name", f"{code}")
                    # [DEBUG] 打印API响应中的名称
                    print(f"[DEBUG-名称] /api/scan_one({code}) meta.name='{result.get('meta', {}).get('name')}', stock_name='{stock_name}'")
                    klines = result.get("klines", [])
                    # 最近N根K线的日期集合
                    recent_dates = set()
                    for k in klines[-recent_days:]:
                        recent_dates.add(k.get("date", ""))
                    buy_points = []
                    sell_points = []
                    for bsp in bsps:
                        if bsp.get("date", "") in recent_dates:
                            point = {
                                "type": bsp.get("type", ""),
                                "price": bsp.get("price", 0),
                                "date": bsp.get("date", ""),
                            }
                            if bsp.get("is_buy", False):
                                buy_points.append(point)
                            else:
                                sell_points.append(point)
                    has_points = buy_points or sell_points
                    t_filter = time.time() - t0
                    if has_points:
                        # 有买/卖点：缓存已由 analyze_stock 写入（完整三项），后续点击可直接查看
                        # 计算流通市值和MA120标记
                        t0 = time.time()
                        market_val, code_val = _get_market_code(code)
                        if not market_val:
                            if prefix == "hk":
                                market_val = "hk"
                            elif prefix in ("0", "1"):
                                market_val = "sh" if prefix == "1" else "sz"
                            else:
                                market_val = prefix
                        float_mv = get_stock_float_mv_local(market_val, code_val, klines[-1]["close"] if klines else 0)
                        t_float = time.time() - t0
                        t0 = time.time()
                        below_ma120 = None  # None表示不比较（K线不足120根）
                        if len(klines) >= 120:
                            ma120_sum = sum(k["close"] for k in klines[-120:])
                            ma120_val = ma120_sum / 120
                            below_ma120 = klines[-1]["close"] < ma120_val
                        t_ma120 = time.time() - t0
                        t_total = time.time() - t_scan_start
                        print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s 流值{t_float:.3f}s MA120{t_ma120:.3f}s) 有买卖点")
                        resp_data = {
                            "code": code + "." + market.upper(), "name": stock_name,
                            "buy_points": buy_points,
                            "sell_points": sell_points,
                            "last_close": klines[-1]["close"] if klines else 0,
                            "float_mv": float_mv,
                            "below_ma120": below_ma120,
                            "freq": freq,
                        }
                        print(f"[DEBUG-名称] /api/scan_one({code}) resp_data.name='{resp_data['name']}'")
                    else:
                        # 无买/卖点：不清除缓存，由 LRU 自然淘汰
                        t_total = time.time() - t_scan_start
                        print(f"[耗时-扫描] {code} 总{t_total:.3f}s(分析{t_analyze:.3f}s 过滤{t_filter:.3f}s) 无买卖点")
                        resp_data = {"code": code, "buy_points": [], "sell_points": []}
                    self.send_json_response(resp_data, 200)
            except Exception as e:
                import traceback
                _scan_skip_log.append(f"{code} - 异常: {e}")
                t_total = time.time() - t_scan_start
                print(f"[耗时-扫描] {code} 异常: {e}, 总耗时{t_total:.3f}s")
                self.send_json_response({"error": str(e)}, 200)
        elif parsed.path == "/api/scan_start":
            # 新一轮扫描开始：清空跳过记录和终止标志，记录开始时间
            # 缓存由 LRU 20 条 + 内存保护机制统一管理，不再主动清除
            _scan_aborted = False
            _scan_skip_log.clear()
            _scan_start_time = time.time()
            try:
                _load_stock_names_from_cache_file()
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/refresh_stock_names":
            # 启动股票名称刷新
            if _refresh_status["running"]:
                self.send_json_response({"status": "already_running", **_refresh_status}, 200)
            else:
                def _do_refresh_names():
                    try:
                        _refresh_stock_names()
                    except Exception as e:
                        import traceback
                        print(f"[错误] refresh_stock_names异常: {e}")
                        traceback.print_exc()
                t = threading.Thread(target=_do_refresh_names, daemon=True)
                t.start()
                self.send_json_response({"status": "started", "msg": "股票名称刷新已启动"}, 200)
        elif parsed.path == "/api/scan_end":
            # 扫描结束：统一打印跳过记录，发送 Windows 通知
            if _scan_skip_log:
                print("\n========== 扫描跳过股票明细 ==========")
                print(f"共跳过 {len(_scan_skip_log)} 只:")
                for i, item in enumerate(_scan_skip_log, 1):
                    print(f"  {i}. {item}")
                print("========================================\n")
            else:
                print("\n[扫描明细] 无跳过股票\n")
            # 发送 Windows 通知
            if _scan_start_time is not None:
                elapsed = time.time() - _scan_start_time
                minutes = int(elapsed // 60)
                seconds = int(elapsed % 60)
                if minutes > 0:
                    time_str = f"{minutes}分{seconds}秒"
                else:
                    time_str = f"{seconds}秒"
                skip_count = len(_scan_skip_log)
                msg = f"耗时 {time_str}"
                if skip_count > 0:
                    msg += f"，跳过 {skip_count} 只"
                _send_windows_notification("扫描完成", msg)
                _scan_start_time = None  # 重置，避免重复通知
            self.send_json_response({"count": len(_scan_skip_log)}, 200)
        elif parsed.path == "/api/scan_clear_cache":
            # 关闭扫描面板：缓存由 LRU 20 条 + 内存保护机制统一管理，不再主动清除
            print("[扫描缓存] 面板关闭，缓存由 LRU 自然淘汰")
            self.send_json_response({"cleared": 0}, 200)
        elif parsed.path == "/api/scan_abort":
            # 前端点击中断扫描：设置后端全局终止标志
            _scan_aborted = True
            print("[扫描] 收到中断请求，设置终止标志")
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/zxg_save":
            # 保存勾选的股票到通达信 && 同花顺自选股
            try:
                params = parse_qs(parsed.query)
                codes_str = params.get("codes", [""])[0]
                codes = codes_str.split(",") if codes_str else []
                if not codes:
                    self.send_json_response({"error": "codes为空"}, 400)
                    return
                # 原始代码（带后缀，如 600150.SH），给通达信用
                codes_raw = [c.strip() for c in codes]
                # 纯6位代码，给同花顺用
                codes_plain = [_strip_market_suffix(c.strip()) for c in codes]
                codes_plain = list(dict.fromkeys(codes_plain))  # 去重保序
                tdx_added = save_to_zxg_blk(codes_raw)
                ths_added, ths_msg = save_to_ths_zxg(codes_plain)
                print(f"[THS] 保存结果: tdx={tdx_added}, ths={ths_added}, msg={ths_msg}")
                self.send_json_response({
                    "ok": True,
                    "tdx_saved": tdx_added,
                    "ths_saved": ths_added,
                    "ths_msg": ths_msg,
                }, 200)
            except Exception as e:
                import traceback
                traceback.print_exc()
                self.send_json_response({"error": str(e)}, 500)
        elif parsed.path == "/api/clear_saved_point":
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return
            normalized_code = code.strip().upper()
            market = None
            prefix_match = re.match(r'^(SH|SZ|HK)(\d+)$', normalized_code)
            suffix_match = re.match(r'^(\d+)\.(SH|SZ|HK)$', normalized_code)
            if prefix_match:
                market = prefix_match.group(1).lower()
                normalized_code = prefix_match.group(2)
            elif suffix_match:
                normalized_code = suffix_match.group(1)
                market = suffix_match.group(2).lower()
            # 用 market-qualified code 区分沪市深市同号股票
            qualified_code = f"{normalized_code}.{market.upper()}" if market else normalized_code
            _clear_saved_point_time(qualified_code, freq)
            cache_key = f"single_{market}_{normalized_code}_{freq}_live"
            with _cache_lock:
                if cache_key in _stocks_analysis_cache:
                    del _stocks_analysis_cache[cache_key]
            import gc
            gc.collect()
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/futures_manual_select_point":
            # 期货期指双击选点
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [""])[0]
            freq = params.get("freq", ["15s"])[0]
            bi_idx = params.get("bi_idx", ["-1"])[0]
            if not symbol or bi_idx == "-1":
                self.send_json_response({"error": "缺少必要参数 symbol 或 bi_idx"}, 400)
                return
            try:
                result = futures_manual_select_point(symbol, freq=freq, bi_idx=bi_idx)
                if "error" in result:
                    self.send_json_response(result, 400)
                else:
                    self.send_json_response(result, 200)
            except Exception as e:
                import traceback
                print(f"[错误] futures_manual_select_point异常: {e}")
                traceback.print_exc()
                self.send_json_response({"error": f"服务器内部错误: {str(e)}"}, 500)
        elif parsed.path == "/api/futures_clear_saved_point":
            # 期货期指清除选点
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [""])[0]
            freq = params.get("freq", ["15s"])[0]
            if not symbol:
                self.send_json_response({"error": "缺少symbol参数"}, 400)
                return
            # 别名解析
            symbol_upper = symbol.upper()
            if symbol_upper in FUTURES_ALIASES:
                symbol = FUTURES_ALIASES[symbol_upper]
            _clear_saved_point_time(symbol, freq)
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/futures_cleanup":
            # 期货切到股票：彻底清理所有期货数据
            _cleanup_all_futures_data()
            self.send_json_response({"ok": True}, 200)
        elif parsed.path == "/api/futures_status":
            # 新架构：每个 SSE 连接自包含，无共享引擎，始终返回 ok
            self.send_json_response({"ok": True, "architecture": "self-contained"}, 200)
        elif parsed.path == "/api/futures_config":
            # 前端查询可用周期列表（用于变灰不可用按钮）
            from DataAPI.TqSdkAPI import SUPPORTED_FREQS, DISABLED_FREQS
            self.send_json_response({
                "supported_freqs": SUPPORTED_FREQS,
                "disabled_freqs": DISABLED_FREQS,
            }, 200)
        elif parsed.path == "/api/futures_stream":
            # SSE 实时推送端点（支持单窗口和双窗口模式）
            params = parse_qs(parsed.query)
            symbol = params.get("symbol", [""])[0]
            freq = params.get("freq", ["15s"])[0]
            start_time = params.get("start_time", [""])[0] or None
            dual = params.get("dual", ["0"])[0] == "1"
            if not symbol:
                self.send_json_response({"error": "缺少symbol参数"}, 400)
                return
            if dual:
                bottom_freq = params.get("bottom_freq", [""])[0] or None
                self._handle_sse_stream_dual(symbol, freq, bottom_freq=bottom_freq, start_time=start_time)
            else:
                self._handle_sse_stream_single(symbol, freq, start_time=start_time)
            return
        elif parsed.path == "/api/annotations":
            # 获取某股票某周期的标注数据
            params = parse_qs(parsed.query)
            code = params.get("code", [""])[0]
            freq = params.get("freq", ["d"])[0]
            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return
            anns = _get_annotations_for(code, freq)
            self.send_json_response({"annotations": anns, "code": code, "freq": freq}, 200)
        elif parsed.path == "/api/annotations_scan":
            # 自选扫描：返回有标注的股票列表
            params = parse_qs(parsed.query)
            freq = params.get("freq", [""])[0]
            codes = _get_annotated_codes(freq)
            self.send_json_response({"codes": codes, "total": len(codes)}, 200)
        else:
            filepath = os.path.join(OUTPUT_DIR, parsed.path.lstrip("/"))
            if os.path.isfile(filepath):
                with open(filepath, "rb") as f:
                    content = f.read()
                self.send_response(200)
                if filepath.endswith(".html"):
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                elif filepath.endswith(".js"):
                    self.send_header("Content-Type", "application/javascript")
                elif filepath.endswith(".css"):
                    self.send_header("Content-Type", "text/css")
                else:
                    self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Pragma", "no-cache")
                self.send_header("Expires", "0")
                self.end_headers()
                self.wfile.write(content)
            else:
                self.send_error(404)
            print_memory(f"前端操作(请求文件-{parsed.path})")

    def _handle_sse_stream_dual(self, symbol, top_freq="1m", bottom_freq=None, start_time=None):
        """Server-Sent Events 双窗口推送端：两个独立 CChan 对象，一次 SSE 连接推送两个周期数据。
        与股票双窗口解耦，使用独立的 TqApi 连接和独立的 CChan 对象。
        """
        if not TQ_AVAILABLE:
            self.send_json_response({"error": "天勤数据源不可用"}, 503)
            return

        from DataAPI.TqSdkAPI import (FREQ_SEC_MAP, FREQ_LABEL_CN, CTqSdkAPI,
                                       TQ_ACCOUNT, TQ_PASSWORD, FUTURES_ALIASES)
        from tqsdk import TqApi, TqAuth
        from datetime import datetime

        # 确定周期
        if not bottom_freq:
            bottom_freq = _FUTURES_DUAL_FREQ_MAP.get(top_freq, "15s")
        top_freq_sec = FREQ_SEC_MAP.get(top_freq, 60)
        bottom_freq_sec = FREQ_SEC_MAP.get(bottom_freq, 15)

        display_key = f"{symbol} 双窗口({top_freq}/{bottom_freq})"
        if _SSE_DEBUG:
            print(f"\n[{display_key}] ═══ SSE双窗口连接建立 ═══")

        # 发送 SSE 头
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        api = None
        top_chan = None
        bottom_chan = None
        try:
            # 别名解析
            symbol_upper = symbol.upper()
            if symbol_upper in FUTURES_ALIASES:
                symbol = FUTURES_ALIASES[symbol_upper]

            api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
            name = _get_futures_name(symbol)
            top_freq_label = top_freq
            bottom_freq_label = bottom_freq

            # 1. 查询选点状态
            saved_selection_date = ""
            try:
                qualified_code = symbol
                col_meta = FREQ_TO_COL.get(top_freq, "")
                if col_meta and qualified_code in _saved_point_times:
                    saved_selection_date = _saved_point_times[qualified_code].get(col_meta, "").strip() or ""
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

            # 2. 拉取上窗历史 + chan分析
            if _SSE_DEBUG:
                print(f"[{display_key}] 拉取上窗({top_freq})历史K线...")
            top_result = init_chan_symbol(api, symbol, name, top_freq_sec, top_freq_label, start_time=start_time)
            top_chan, top_records, top_kl_type, _ = top_result
            top_kl_type = _get_kl_type(top_freq)
            if _SSE_DEBUG:
                print(f"[{display_key}] 上窗({top_freq}) chan.py: 合并K线={len(top_chan[top_kl_type].lst)}, "
                      f"笔={len(top_chan[top_kl_type].bi_list)}, 中枢={len(top_chan[top_kl_type].zs_list)}")

            # 3. 拉取下窗历史 + chan分析
            if _SSE_DEBUG:
                print(f"[{display_key}] 拉取下窗({bottom_freq})历史K线...")
            bottom_result = init_chan_symbol(api, symbol, name, bottom_freq_sec, bottom_freq_label, start_time=start_time)
            bottom_chan, bottom_records, bottom_kl_type, _ = bottom_result
            bottom_kl_type = _get_kl_type(bottom_freq)
            # 缓存下窗 CChan 供 /api/dual_zs 访问（key 统一大写）
            _futures_analysis_cache[f"{symbol.upper()}:{bottom_freq}"] = bottom_chan
            if _SSE_DEBUG:
                print(f"[{display_key}] 下窗({bottom_freq}) chan.py: 合并K线={len(bottom_chan[bottom_kl_type].lst)}, "
                      f"笔={len(bottom_chan[bottom_kl_type].bi_list)}, 中枢={len(bottom_chan[bottom_kl_type].zs_list)}")

            # 7. 提取初始快照
            t_snap = time.time()
            top_snapshot = _extract_realtime_snapshot(
                top_chan, top_kl_type, symbol, name, top_freq_label,
                saved_selection_date=saved_selection_date
            )
            bottom_snapshot = _extract_realtime_snapshot(
                bottom_chan, bottom_kl_type, symbol, name, bottom_freq_label,
                klines=None
            )
            # 期货双窗口：上窗 bis 的 fx_a_raw_dt/fx_b_raw_dt 是上层K线时间，
            # 需要换算成子级别K线时间，前端 calcRedRange 才能正确匹配
            _futures_red_range(top_snapshot, top_freq_sec, bottom_freq_sec, bottom_freq)

            # ★ 追加上下窗当前形成中的K线（与单窗口一致），让前端立即看到，且 tick 更新正确的 K 线
            _top_klines_for_init = api.get_kline_serial(symbol, top_freq_sec)
            _bottom_klines_for_init = api.get_kline_serial(symbol, bottom_freq_sec)
            # 上窗
            if _top_klines_for_init is not None and len(_top_klines_for_init) > 0:
                _lr = _top_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
                if _dns is not None:
                    _bdt = datetime.fromtimestamp(_dns / 1e9)
                    _bds = _bdt.strftime(_get_date_fmt(top_freq))
                    _ex = top_snapshot.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _bds:
                        _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                            'open': round(float(_lr.get('open', 0) or 0), 3),
                            'high': round(float(_lr.get('high', 0) or 0), 3),
                            'low': round(float(_lr.get('low', 0) or 0), 3),
                            'close': round(float(_lr.get('close', 0) or 0), 3),
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        top_snapshot['meta']['kline_count'] = len(_ex)
            # 下窗
            if _bottom_klines_for_init is not None and len(_bottom_klines_for_init) > 0:
                _lr = _bottom_klines_for_init.iloc[-1]; _dns = _lr.get('datetime')
                if _dns is not None:
                    _bdt = datetime.fromtimestamp(_dns / 1e9)
                    _bds = _bdt.strftime(_get_date_fmt(bottom_freq))
                    _ex = bottom_snapshot.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _bds:
                        _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                            'open': round(float(_lr.get('open', 0) or 0), 3),
                            'high': round(float(_lr.get('high', 0) or 0), 3),
                            'low': round(float(_lr.get('low', 0) or 0), 3),
                            'close': round(float(_lr.get('close', 0) or 0), 3),
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        bottom_snapshot['meta']['kline_count'] = len(_ex)
            if _SSE_DEBUG:
                print(f"[{display_key}] 初始快照提取: {time.time()-t_snap:.3f}s")

            # 8. 推送双窗口 init 事件
            init_data = {
                "main": top_snapshot,
                "sub": bottom_snapshot,
            }
            init_str = json.dumps(init_data, ensure_ascii=False, allow_nan=False)
            self.wfile.write(f"event: init\ndata: {init_str}\n\n".encode("utf-8"))
            self.wfile.flush()
            if _SSE_DEBUG:
                print(f"[{display_key}] 推送init")

            # 缓存快照用于 tick 路径
            top_cached_snapshot = top_snapshot
            bottom_cached_snapshot = bottom_snapshot

            # 9. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照
            #
            # 策略：用系统壁钟（datetime.now()）判断K线周期是否结束，不再等天勤的
            # klines 序列推进信号。天勤免费版 klines 推进延迟约 7 秒，但 klines[-1]
            # 的 OHLC 在周期结束后已冻结，可以直接用于缠论计算。
            #
            # 流程：
            #   1. 壁钟确认 N-1 周期结束 → 立即取 klines[-1]/[-2] 做缠论
            #   2. 缠论计算完成后，N 周期的第一笔 tick 已到达 → 快照中直接显示 N
            #
            BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
            t_total = time.time()  # 总耗时起点（用于日志输出）
            if _SSE_DEBUG:
                print(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

            # 保存两个窗口的 klines 引用供实时更新使用
            top_klines = api.get_kline_serial(symbol, top_freq_sec)
            bottom_klines = api.get_kline_serial(symbol, bottom_freq_sec)

            # last_bar_dt_ns: klines[-1] 的时间戳，用于检测 klines 是否推进
            # last_processed_dt_ns: 已处理过的K线时间戳，防止同一根K线被重复处理
            top_last_bar_dt_ns = None
            top_last_processed_dt_ns = None
            bottom_last_bar_dt_ns = None
            bottom_last_processed_dt_ns = None
            last_debug_print = time.time()

            # 性能统计
            t_wait_total = 0.0
            t_tick_total = 0.0
            t_step_total = 0.0
            t_snapshot_total = 0.0
            t_push_total = 0.0
            loop_count = 0
            tick_count = 0
            step_count = 0
            last_perf_print = time.time()

            # ---- 定义单窗口K线处理函数（避免 continue 跳过另一个窗口） ----
            def _process_one_window(klines, chan, kl_type, freq_sec, freq_label,
                                     cached_snapshot, last_bar_dt_ns, last_processed_dt_ns,
                                     is_top, window_label):
                """处理单个窗口的K线检测，返回 (updated, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, need_tick)"""
                nonlocal last_debug_print, last_perf_print, loop_count, tick_count, step_count
                nonlocal t_wait_total, t_tick_total, t_step_total, t_snapshot_total, t_push_total
                if len(klines) == 0:
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
                last_row = klines.iloc[-1]
                dt_ns = last_row.get("datetime")
                if dt_ns is None:
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

                # 诊断
                if loop_count == 1 or (loop_count % 50 == 0):
                    chan_last_klu = None
                    try:
                        chan_kl_list = chan[kl_type]
                        if chan_kl_list.lst:
                            last_klc = chan_kl_list.lst[-1]
                            if last_klc.lst:
                                chan_last_klu = last_klc.lst[-1]
                    except Exception as e:
                        print(f"[警告] 异常: {type(e).__name__}: {e}")
                    tqsdk_last_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime('%H:%M:%S') if dt_ns else "None"
                    chan_last_dt = chan_last_klu.time.to_str()[:16] if chan_last_klu and hasattr(chan_last_klu, 'time') else "None"
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG-{window_label}] 循环#{loop_count} | "
                          f"tqsdk klines[-1]={tqsdk_last_dt} | "
                          f"chan kl_list[-1]={chan_last_dt} | "
                          f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

                # 初始化
                if last_bar_dt_ns is None:
                    last_bar_dt_ns = dt_ns
                    last_debug_print = now_ts
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DEBUG] 初始化 [{window_label}]: "
                          f"klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

                # 每60秒性能统计
                if now_ts - last_perf_print >= 60.0:
                    last_perf_print = now_ts
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [PERF] 循环{loop_count}次 | "
                          f"wait_update总计={t_wait_total:.1f}s | "
                          f"tick推送{tick_count}次总计={t_tick_total:.1f}s | "
                          f"step_load{step_count}次总计={t_step_total:.1f}s | "
                          f"快照提取总计={t_snapshot_total:.1f}s | "
                          f"SSE推送总计={t_push_total:.1f}s")
                    t_wait_total = 0.0; t_tick_total = 0.0; t_step_total = 0.0
                    t_snapshot_total = 0.0; t_push_total = 0.0
                    loop_count = 0; tick_count = 0; step_count = 0

                # 每2秒 DEBUG
                if now_ts - last_debug_print >= 2.0:
                    last_debug_print = now_ts
                    bar_dt = datetime.fromtimestamp(dt_ns / 1e9)
                    lag = now_ts - (dt_ns / 1e9 + freq_sec)
                    pushed = (dt_ns != last_bar_dt_ns)
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DEBUG-{window_label}] 壁钟={now.strftime('%H:%M:%S')} "
                          f"klines[-1]={bar_dt.strftime('%H:%M:%S')} "
                          f"过期={lag:+.1f}s 推进={pushed} "
                          f"O={last_row.get('open'):.1f} H={last_row.get('high'):.1f} "
                          f"L={last_row.get('low'):.1f} C={last_row.get('close'):.1f}")

                # --- K线完成检测 ---
                klines_pushed = (dt_ns != last_bar_dt_ns)

                if klines_pushed:
                    completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row
                    last_bar_dt_ns = dt_ns
                    bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] K线完成(klines推进) [{window_label}] "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")
                else:
                    bar_end_ts = (dt_ns / 1e9) + freq_sec + BAR_COMPLETION_BUFFER
                    if now_ts < bar_end_ts:
                        # 周期未结束 → tick更新（更新快照OHLC，稍后统一推送）
                        if cached_snapshot is not None:
                            try:
                                ex = cached_snapshot.get('klines', [])
                                if ex:
                                    o = round(float(last_row.get('open', 0) or 0), 3)
                                    h = round(float(last_row.get('high', 0) or 0), 3)
                                    l = round(float(last_row.get('low', 0) or 0), 3)
                                    c = round(float(last_row.get('close', 0) or 0), 3)
                                    ex[-1]['open'] = o; ex[-1]['high'] = h
                                    ex[-1]['low'] = l; ex[-1]['close'] = c
                                    closes = [k['close'] for k in ex]
                                    if len(closes) >= 26:
                                        ema12 = ema(closes, 12); ema26 = ema(closes, 26)
                                        for i in range(len(ex)):
                                            if i < len(ema12):
                                                ex[i]['dif'] = round(ema12[i] - ema26[i], 4)
                                        difs = [ex[i]['dif'] for i in range(len(ex))]
                                        dea = ema(difs, 9)
                                        for i in range(len(ex)):
                                            if i < len(dea):
                                                ex[i]['dea'] = round(dea[i], 4)
                                                ex[i]['macd'] = round(2 * (ex[i]['dif'] - dea[i]), 4)
                                    cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                            except Exception as e:
                                print(f"[警告] 异常: {type(e).__name__}: {e}")
                        return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, True
                    # 壁钟到期
                    completed_row = last_row
                    bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] K线完成(壁钟) [{window_label}]: "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")

                completed_dt_ns = completed_row.get("datetime")
                if completed_dt_ns is None:
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
                if completed_dt_ns == last_processed_dt_ns:
                    return False, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False
                last_processed_dt_ns = completed_dt_ns

                # 提取 OHLC
                o = float(completed_row.get("open", 0) or 0)
                h = float(completed_row.get("high", 0) or 0)
                l = float(completed_row.get("low", 0) or 0)
                cl = float(completed_row.get("close", 0) or 0)
                vol = int(completed_row.get("volume", 0) or 0)
                oi = float(completed_row.get("open_oi", 0) or 0)
                h = max(h, o, cl); l = min(l, o, cl)

                dt = datetime.fromtimestamp(completed_dt_ns / 1e9)
                bar_expected_end = (completed_dt_ns / 1e9) + freq_sec
                delay = now_ts - bar_expected_end
                source = "klines推进" if klines_pushed else "壁钟"

                code_key = f"{symbol}:{freq_sec}"
                new_bar = {"dt": dt, "open": round(o, 3), "high": round(h, 3),
                           "low": round(l, 3), "close": round(cl, 3),
                           "vol": vol, "amount": round(oi, 2)}

                last_records = CTqSdkAPI.get_last_n(1, symbol=code_key)
                updated = False
                t_step = 0.0
                if not last_records or last_records[0]["dt"] != dt:
                    CTqSdkAPI.append_bar(new_bar, symbol=code_key)
                    t_step_start = time.time()
                    try:
                        for _snapshot in chan.step_load():
                            pass
                    except Exception as e:
                        print(f"[{display_key}] {window_label} step_load 异常: {e}")
                    t_step = time.time() - t_step_start
                    t_step_total += t_step; step_count += 1
                    updated = True
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 完成新K线[{source}] [{window_label}]: "
                          f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                          f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                          f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                          f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

                # 提取完整快照
                if updated:
                    snapshot = _extract_realtime_snapshot(
                        chan, kl_type, symbol, name, freq_label,
                        saved_selection_date=saved_selection_date
                    )
                    if is_top:
                        _futures_red_range(snapshot, freq_sec, bottom_freq_sec, bottom_freq)
                    _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                    _next_ds = _next_dt.strftime(_get_date_fmt(freq_label))
                    _ex = snapshot.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _next_ds:
                        _next_c = round(cl, 3)
                        _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                            'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        snapshot['meta']['kline_count'] = len(_ex)
                    if is_top:
                        _kl_list = chan[kl_type]
                        _date_fmt = _get_date_fmt(top_freq)
                        snapshot['white_hline'] = _calc_futures_white_hline(_kl_list, top_freq, _date_fmt)
                    cached_snapshot = snapshot

                return updated, cached_snapshot, last_bar_dt_ns, last_processed_dt_ns, False

            # ---- 主循环 ----
            while True:
                try:
                    t_wait_start = time.time()
                    api.wait_update(deadline=time.time() * 1e9 + 100_000_000)
                    t_wait = time.time() - t_wait_start
                    t_wait_total += t_wait
                except Exception as e:
                    print(f"[{display_key}] wait_update 异常: {e}")
                    time.sleep(0.5)
                    continue

                # 心跳检测
                try:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 客户端断开，退出循环")
                    break

                loop_count += 1
                now = datetime.now()
                now_ts = now.timestamp()

                # 处理上窗
                top_updated, top_cached_snapshot, top_last_bar_dt_ns, top_last_processed_dt_ns, top_need_tick = \
                    _process_one_window(top_klines, top_chan, top_kl_type, top_freq_sec, top_freq_label,
                                        top_cached_snapshot, top_last_bar_dt_ns, top_last_processed_dt_ns,
                                        is_top=True, window_label="上窗")

                # 处理下窗
                bottom_updated, bottom_cached_snapshot, bottom_last_bar_dt_ns, bottom_last_processed_dt_ns, bottom_need_tick = \
                    _process_one_window(bottom_klines, bottom_chan, bottom_kl_type, bottom_freq_sec, bottom_freq_label,
                                        bottom_cached_snapshot, bottom_last_bar_dt_ns, bottom_last_processed_dt_ns,
                                        is_top=False, window_label="下窗")

                # 推送：tick模式或K线完成模式
                if top_need_tick or bottom_need_tick:
                    # tick推送：统一发送双窗口数据
                    t_tick_start = time.time()
                    tick_data = {"main": top_cached_snapshot, "sub": bottom_cached_snapshot}
                    tick_str = json.dumps(tick_data, ensure_ascii=False, allow_nan=False)
                    try:
                        self.wfile.write(f"event: update\ndata: {tick_str}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    t_tick_total += time.time() - t_tick_start
                    tick_count += 1
                    if tick_count == 1:
                        if _SSE_DEBUG:
                            print(f"[{display_key}] [DIAG] 首次tick推送: "
                              f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

                if top_updated or bottom_updated:
                    t_snap_start = time.time()
                    update_data = {"main": top_cached_snapshot, "sub": bottom_cached_snapshot}
                    update_str = json.dumps(update_data, ensure_ascii=False, allow_nan=False)
                    t_push_start = time.time()
                    try:
                        self.wfile.write(f"event: update\ndata: {update_str}\n\n".encode("utf-8"))
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                    t_push_total += time.time() - t_push_start
                    t_snapshot_total += time.time() - t_snap_start
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 推送更新: 快照提取={t_snapshot_total:.3f}s JSON序列化={(time.time()-t_snap_start)-t_push_total:.3f}s SSE写入={t_push_total:.3f}s")

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            import traceback
            print(f"[{display_key}] 连接异常: {e}")
            traceback.print_exc()
        finally:
            if api is not None:
                try:
                    api.close()
                except Exception as e:
                    print(f"[警告] 异常: {type(e).__name__}: {e}")
            try:
                CTqSdkAPI._records_by_symbol.pop(f"{symbol}:{top_freq_sec}", None)
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
            try:
                CTqSdkAPI._records_by_symbol.pop(f"{symbol}:{bottom_freq_sec}", None)
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")
            try:
                _futures_analysis_cache.pop(f"{symbol.upper()}:{bottom_freq}", None)
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

    def _handle_sse_stream_single(self, symbol, freq="15s", start_time=None):
        """Server-Sent Events 推送端：自包含——创建TqApi → 拉取历史 → chan分析 → 快照 → 实时循环。
        每个 SSE 连接独立，互不干扰。
        start_time: 选点起始时间，有值时从该时间拉取K线；无值时自动查询CSV保存的选点。"""
        import logging
        import time
        logging.getLogger("tqsdk").setLevel(logging.WARNING)
        logging.getLogger("tqsdk.tqapi").setLevel(logging.WARNING)
        for h in logging.root.handlers:
            h.setLevel(logging.WARNING)

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()

        from tqsdk import TqApi, TqAuth
        from DataAPI.TqSdkAPI import (FREQ_SEC_MAP, FREQ_LABEL_CN, CTqSdkAPI,
                                       TQ_ACCOUNT, TQ_PASSWORD, FUTURES_ALIASES)
        from datetime import datetime

        api = None
        chan = None
        try:
            # 别名解析：支持 PTA→KQ.m@CZCE.TA 等短名称
            symbol_upper = symbol.upper()
            if symbol_upper in FUTURES_ALIASES:
                symbol = FUTURES_ALIASES[symbol_upper]

            freq_sec = FREQ_SEC_MAP.get(freq, 15)
            freq_label = freq
            freq_cn = FREQ_LABEL_CN.get(freq_label, freq_label)
            display_key = f"{symbol}:{freq_cn}"

            # 如果没有传入 start_time，查询CSV中是否有保存的选点
            if start_time is None:
                col = FREQ_TO_COL.get(freq, "")
                if col and symbol in _saved_point_times:
                    _saved = _saved_point_times[symbol].get(col, "").strip() or None
                    if _saved:
                        start_time = _saved
                        print(f"[{display_key}] 检测到保存选点: {start_time}")

            saved_selection_date = start_time or ""

            t_conn = time.time()
            api = TqApi(auth=TqAuth(TQ_ACCOUNT, TQ_PASSWORD))
            print(f"[{display_key}] ⓪ 连接天勤: 耗时 {time.time()-t_conn:.1f}s")

            t_total = time.time()
            name = _get_futures_name(symbol)  # 品种名称

            # === 1. 拉取历史 + chan 分析 ===
            result = init_chan_symbol(api, symbol, name, freq_sec, freq_label, start_time=start_time)
            if result is None:
                err = json.dumps({"error": "初始化失败（无数据或网络异常）", "symbol": symbol})
                self.wfile.write(f"event: init\ndata: {err}\n\n".encode("utf-8"))
                self.wfile.flush()
                return
            chan, klines, kl_type, records = result

            # === 2. 推送初始快照 ===
            t0 = time.time()
            try:
                init_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                       saved_selection_date=saved_selection_date)
                # ★ 追加当前形成中的K线（klines[-1]），让前端立即看到新K线
                if klines is not None and len(klines) > 0:
                    _lr = klines.iloc[-1]; _dns = _lr.get('datetime')
                    if _dns is not None:
                        _bdt = datetime.fromtimestamp(_dns / 1e9)
                        _bds = _bdt.strftime(_get_date_fmt(freq))
                        _ex = init_data.get('klines', [])
                        if not _ex or _ex[-1]['date'] != _bds:
                            _ex.append({'date': _bds, 'timestamp': int(_bdt.timestamp() * 1000),
                                'open': round(float(_lr.get('open', 0) or 0), 3),
                                'high': round(float(_lr.get('high', 0) or 0), 3),
                                'low': round(float(_lr.get('low', 0) or 0), 3),
                                'close': round(float(_lr.get('close', 0) or 0), 3),
                                'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                            init_data['meta']['kline_count'] = len(_ex)
                # 计算白色横虚线（初始快照，K线已确认状态）
                _kl_list = chan[kl_type]
                _date_fmt = _get_date_fmt(freq)
                init_data['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _date_fmt)
                init_str = json.dumps(init_data, ensure_ascii=False, allow_nan=False)
                self.wfile.write(f"event: init\ndata: {init_str}\n\n".encode("utf-8"))
                self.wfile.flush()
                cached_snapshot = init_data  # ★ 缓存完整快照，tick推送时更新最后一根K线OHLC
                if _SSE_DEBUG:
                    print(f"[{display_key}] ⑶ 推送init: "
                          f"K线{init_data['meta']['kline_count']}, "
                          f"笔{init_data['meta']['bi_count']}, "
                          f"中枢{init_data['meta']['zs_count']}, "
                          f"耗时 {time.time()-t0:.1f}s")
            except Exception as e:
                err = json.dumps({"error": f"快照提取失败: {e}", "symbol": symbol})
                self.wfile.write(f"event: init\ndata: {err}\n\n".encode("utf-8"))
                self.wfile.flush()
                return

            # === 3. 实时循环：壁钟检测周期结束 → 处理N-1 → 推送N-1/N快照 ===
            #
            # 策略：用系统壁钟（datetime.now()）判断K线周期是否结束，不再等天勤的
            # klines 序列推进信号。天勤免费版 klines 推进延迟约 7 秒，但 klines[-1]
            # 的 OHLC 在周期结束后已冻结，可以直接用于缠论计算。
            #
            # 流程：
            #   1. 壁钟确认 N-1 周期结束 → 立即取 klines[-1]/[-2] 做缠论
            #   2. 缠论计算完成后，N 周期的第一笔 tick 已到达 → 快照中直接显示 N
            #
            BAR_COMPLETION_BUFFER = 1.0  # 周期结束后等 N 秒（等待最后一笔 tick 到达）
            if _SSE_DEBUG:
                print(f"[{display_key}] ⑷ 实时循环 (总耗时 {time.time()-t_total:.1f}s)")

            # last_bar_dt_ns: klines[-1] 的时间戳，用于检测 klines 是否推进
            # last_processed_dt_ns: 已处理过的K线时间戳，防止同一根K线被重复处理
            last_bar_dt_ns = None
            last_processed_dt_ns = None
            last_debug_print = time.time()

            # 性能统计
            t_wait_total = 0.0
            t_tick_total = 0.0
            t_step_total = 0.0
            t_snapshot_total = 0.0
            t_push_total = 0.0
            loop_count = 0
            tick_count = 0
            step_count = 0
            last_perf_print = time.time()

            while True:
                try:
                    t_wait_start = time.time()
                    api.wait_update(deadline=time.time() * 1e9 + 100_000_000)
                    t_wait = time.time() - t_wait_start
                    t_wait_total += t_wait
                except Exception as e:
                    print(f"[{display_key}] wait_update 异常: {e}")
                    time.sleep(0.5)
                    continue

                # 心跳检测：前端断开后及时退出
                try:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 客户端断开，退出循环")
                    break

                loop_count += 1

                now = datetime.now()
                now_ts = now.timestamp()

                if len(klines) == 0:
                    continue

                last_row = klines.iloc[-1]
                dt_ns = last_row.get("datetime")
                if dt_ns is None:
                    continue

                # ★ 诊断：对比 tqsdk 实时 K 线和 chan 框架内部 K 线的时间差
                if loop_count == 1 or (loop_count % 50 == 0):
                    chan_last_klu = None
                    try:
                        chan_kl_list = chan[kl_type]
                        if chan_kl_list.lst:
                            last_klc = chan_kl_list.lst[-1]
                            if last_klc.lst:
                                chan_last_klu = last_klc.lst[-1]
                    except Exception as e:
                        print(f"[警告] 异常: {type(e).__name__}: {e}")
                    tqsdk_last_dt = datetime.fromtimestamp(dt_ns / 1e9).strftime('%H:%M:%S') if dt_ns else "None"
                    chan_last_dt = chan_last_klu.time.to_str()[:16] if chan_last_klu and hasattr(chan_last_klu, 'time') else "None"
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] 循环#{loop_count} | "
                          f"tqsdk klines[-1]={tqsdk_last_dt} | "
                          f"chan kl_list[-1]={chan_last_dt} | "
                          f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")

                # 初始化
                if last_bar_dt_ns is None:
                    last_bar_dt_ns = dt_ns
                    last_debug_print = now_ts
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DEBUG] 初始化: klines[-1]={datetime.fromtimestamp(dt_ns/1e9).strftime('%H:%M:%S')}, "
                          f"klines行数={len(klines)}, 缓冲={BAR_COMPLETION_BUFFER}s")
                    continue

                # 每60秒打印一次性能统计
                if now_ts - last_perf_print >= 60.0:
                    last_perf_print = now_ts
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [PERF] 循环{loop_count}次 | "
                          f"wait_update总计={t_wait_total:.1f}s | "
                          f"tick推送{tick_count}次总计={t_tick_total:.1f}s | "
                          f"step_load{step_count}次总计={t_step_total:.1f}s | "
                          f"快照提取总计={t_snapshot_total:.1f}s | "
                          f"SSE推送总计={t_push_total:.1f}s")
                    t_wait_total = 0.0
                    t_tick_total = 0.0
                    t_step_total = 0.0
                    t_snapshot_total = 0.0
                    t_push_total = 0.0
                    loop_count = 0
                    tick_count = 0
                    step_count = 0

                # ★ DEBUG: 每2秒打印一次当前状态
                if now_ts - last_debug_print >= 2.0:
                    last_debug_print = now_ts
                    bar_dt = datetime.fromtimestamp(dt_ns / 1e9)
                    lag = now_ts - (dt_ns / 1e9 + freq_sec)
                    pushed = (dt_ns != last_bar_dt_ns)
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DEBUG] 壁钟={now.strftime('%H:%M:%S')} "
                          f"klines[-1]={bar_dt.strftime('%H:%M:%S')} "
                          f"过期={lag:+.1f}s 推进={pushed} "
                          f"O={last_row.get('open'):.1f} H={last_row.get('high'):.1f} "
                          f"L={last_row.get('low'):.1f} C={last_row.get('close'):.1f}")

                # --- 检测上一根K线是否已完成 ---
                klines_pushed = (dt_ns != last_bar_dt_ns)

                if klines_pushed:
                    # klines 已推进 → 上一根K线（klines[-2]）已冻结，立即处理
                    completed_row = klines.iloc[-2] if len(klines) >= 2 else last_row
                    last_bar_dt_ns = dt_ns
                    bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] K线完成(klines推进): "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")
                else:
                    # klines 未推进 → 用壁钟判断当前K线周期是否已结束
                    bar_end_ts = (dt_ns / 1e9) + freq_sec + BAR_COMPLETION_BUFFER
                    if now_ts < bar_end_ts:
                        # 周期未结束，更新缓存快照的最后一根K线OHLC后推送完整格式
                        t_tick_start = time.time()
                        try:
                            if cached_snapshot is not None:
                                ex = cached_snapshot.get('klines', [])
                                if ex:
                                    o = round(float(last_row.get('open', 0) or 0), 3)
                                    h = round(float(last_row.get('high', 0) or 0), 3)
                                    l = round(float(last_row.get('low', 0) or 0), 3)
                                    c = round(float(last_row.get('close', 0) or 0), 3)
                                    ex[-1]['open'] = o
                                    ex[-1]['high'] = h
                                    ex[-1]['low'] = l
                                    ex[-1]['close'] = c
                                    # ★ 实时计算最后一根K线的MACD，避免前端跳变
                                    closes = [k['close'] for k in ex]
                                    if len(closes) >= 26:
                                        ema12 = ema(closes, 12)
                                        ema26 = ema(closes, 26)
                                        for i in range(len(ex)):
                                            if i < len(ema12):
                                                ex[i]['dif'] = round(ema12[i] - ema26[i], 4)
                                        difs = [ex[i]['dif'] for i in range(len(ex))]
                                        dea = ema(difs, 9)
                                        for i in range(len(ex)):
                                            if i < len(dea):
                                                ex[i]['dea'] = round(dea[i], 4)
                                                ex[i]['macd'] = round(2 * (ex[i]['dif'] - ex[i]['dea']), 4)
                                    cached_snapshot['meta']['generated_at'] = now.strftime('%Y-%m-%d %H:%M:%S')
                                    tick_str = json.dumps(cached_snapshot, ensure_ascii=False, allow_nan=False)
                                    self.wfile.write(f"event: update\ndata: {tick_str}\n\n".encode("utf-8"))
                                    self.wfile.flush()
                                    if tick_count == 0:
                                        if _SSE_DEBUG:
                                            print(f"[{display_key}] [DIAG] 首次tick推送: "
                                              f"tqsdk klines[-1]={now.strftime('%H:%M:%S')} | "
                                              f"更新最后一根K线OHLC O={o} H={h} L={l} C={c} | "
                                              f"壁钟={now.strftime('%H:%M:%S.%f')[:-3]}")
                                    t_tick_total += time.time() - t_tick_start
                                    tick_count += 1
                        except (BrokenPipeError, ConnectionResetError, OSError):
                            return
                        except Exception as e:
                            print(f"[警告] 异常: {type(e).__name__}: {e}")
                        continue
                    # 壁钟到期，当前K线（klines[-1]）已冻结
                    completed_row = last_row
                    bar_theoretical_end = (completed_row.get("datetime", 0) / 1e9) + freq_sec
                    if _SSE_DEBUG:
                        print(f"[{display_key}] [DIAG] K线完成(壁钟): "
                          f"bar={datetime.fromtimestamp(completed_row.get('datetime', 0)/1e9).strftime('%H:%M:%S')} "
                          f"理论结束={datetime.fromtimestamp(bar_theoretical_end).strftime('%H:%M:%S')} "
                          f"检测时间={now.strftime('%H:%M:%S.%f')[:-3]} "
                          f"滞后={now_ts - bar_theoretical_end:+.2f}s")

                completed_dt_ns = completed_row.get("datetime")
                if completed_dt_ns is None:
                    continue

                # 防止重复处理同一根K线
                if completed_dt_ns == last_processed_dt_ns:
                    continue
                last_processed_dt_ns = completed_dt_ns

                # 提取 OHLC
                o = float(completed_row.get("open", 0) or 0)
                h = float(completed_row.get("high", 0) or 0)
                l = float(completed_row.get("low", 0) or 0)
                cl = float(completed_row.get("close", 0) or 0)
                vol = int(completed_row.get("volume", 0) or 0)
                oi = float(completed_row.get("open_oi", 0) or 0)
                h = max(h, o, cl)
                l = min(l, o, cl)

                dt = datetime.fromtimestamp(completed_dt_ns / 1e9)
                bar_expected_end = (completed_dt_ns / 1e9) + freq_sec
                delay = now_ts - bar_expected_end
                source = "klines推进" if klines_pushed else "壁钟"

                code_key = f"{symbol}:{freq_sec}"
                new_bar = {
                    "dt": dt, "open": round(o, 3), "high": round(h, 3),
                    "low": round(l, 3), "close": round(cl, 3),
                    "vol": vol, "amount": round(oi, 2),
                }
                t_append = time.time()

                last_records = CTqSdkAPI.get_last_n(1, symbol=code_key)
                t_step = 0.0
                if not last_records or last_records[0]["dt"] != dt:
                    CTqSdkAPI.append_bar(new_bar, symbol=code_key)
                    t_step_start = time.time()
                    try:
                        for _snapshot in chan.step_load():
                            pass
                    except Exception as e:
                        print(f"[{display_key}] step_load 异常: {e}")
                    t_step = time.time() - t_step_start
                    t_step_total += t_step
                    step_count += 1

                if _SSE_DEBUG:
                    print(f"[{display_key}] 完成新K线[{source}]: "
                      f"{dt.strftime('%Y-%m-%d %H:%M:%S')} "
                      f"O={o:.3f} H={h:.3f} L={l:.3f} C={cl:.3f} "
                      f"[壁钟={now.strftime('%H:%M:%S')} 延迟={delay:+.1f}s "
                      f"wait_update={t_wait:.3f}s step_load={t_step:.3f}s]")

                # 推送快照（此时 klines[-1] 已推进到 N 周期，快照中自然包含 N 的实时OHLC）
                t_snap_start = time.time()
                try:
                    update_data = _extract_realtime_snapshot(chan, kl_type, symbol, name, freq_label,
                                                       saved_selection_date=saved_selection_date)
                    # ★ 用 completed_time + freq_sec 计算下一根K线时间（不用klines[-1]，因为壁钟触发时klines未推进）
                    _next_dt = datetime.fromtimestamp(completed_dt_ns / 1e9 + freq_sec)
                    _next_ds = _next_dt.strftime(_get_date_fmt(freq_label))
                    _ex = update_data.get('klines', [])
                    if not _ex or _ex[-1]['date'] != _next_ds:
                        _next_c = round(cl, 3)
                        _ex.append({'date': _next_ds, 'timestamp': int(_next_dt.timestamp() * 1000),
                            'open': _next_c, 'high': _next_c, 'low': _next_c, 'close': _next_c,
                            'vol': 0, 'amount': 0, 'dif': 0, 'dea': 0, 'macd': 0})
                        update_data['meta']['kline_count'] = len(_ex)
                    # K线确认后，计算白色横虚线（不在tick推送路径计算）
                    _kl_list = chan[kl_type]
                    _date_fmt = _get_date_fmt(freq)
                    update_data['white_hline'] = _calc_futures_white_hline(_kl_list, freq, _date_fmt)
                    cached_snapshot = update_data  # ★ 更新缓存
                    t_snap = time.time() - t_snap_start
                    t_snapshot_total += t_snap
                    update_str = json.dumps(update_data, ensure_ascii=False, allow_nan=False)
                    t_serialize = time.time() - t_snap_start - t_snap
                    t_push_start = time.time()
                    self.wfile.write(f"event: update\ndata: {update_str}\n\n".encode("utf-8"))
                    self.wfile.flush()
                    t_push = time.time() - t_push_start
                    t_push_total += t_push
                    if _SSE_DEBUG:
                        print(f"[{display_key}] 推送更新: 快照提取={t_snap:.3f}s "
                          f"JSON序列化={t_serialize:.3f}s SSE写入={t_push:.3f}s "
                          f"(append+step_load={time.time()-t_append:.3f}s)")
                except (BrokenPipeError, ConnectionResetError, OSError):
                    return
                except Exception as e:
                    print(f"[{display_key}] 推送异常: {e}")

        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        except Exception as e:
            print(f"[{display_key}] 连接异常: {e}")
        finally:
            if api is not None:
                try:
                    api.close()
                except Exception as e:
                    print(f"[警告] 异常: {type(e).__name__}: {e}")
            # 清理该连接的K线缓存
            try:
                CTqSdkAPI._records_by_symbol.pop(f"{symbol}:{freq_sec}", None)
            except Exception as e:
                print(f"[警告] 异常: {type(e).__name__}: {e}")

    def do_POST(self):
        """处理 POST 请求（标注增删、扫描等）"""
        parsed = urlparse(self.path)
        content_length = int(self.headers.get("Content-Length", 0))
        body = {}
        if content_length > 0:
            try:
                raw_body = self.rfile.read(content_length)
                body = json.loads(raw_body.decode("utf-8"))
            except Exception:
                body = {}

        if parsed.path == "/api/annotations":
            action = body.get("action", "")
            code = body.get("code", "")
            freq = body.get("freq", "d")
            date_str = body.get("date", "")
            text = body.get("text", "")
            y_offset = body.get("y_offset", 0)

            if not code:
                self.send_json_response({"error": "缺少code参数"}, 400)
                return

            if action == "add":
                if not date_str or not text:
                    self.send_json_response({"error": "缺少date或text参数"}, 400)
                    return
                success = _add_annotation(code, freq, date_str, text, y_offset)
                self.send_json_response({"ok": success, "duplicate": not success}, 200)
            elif action == "delete":
                if not date_str or not text:
                    self.send_json_response({"error": "缺少date或text参数"}, 400)
                    return
                success = _delete_annotation(code, freq, date_str, text)
                self.send_json_response({"ok": success}, 200)
            elif action == "delete_by_date":
                if not date_str:
                    self.send_json_response({"error": "缺少date参数"}, 400)
                    return
                success = _delete_annotation_by_date(code, freq, date_str)
                self.send_json_response({"ok": success}, 200)
            elif action == "delete_all":
                success = _delete_all_annotations(code, freq)
                self.send_json_response({"ok": success}, 200)
            elif action == "update":
                old_text = body.get("old_text", "")
                new_text = body.get("text", "")
                if not date_str or not old_text or not new_text:
                    self.send_json_response({"error": "缺少date/old_text/text参数"}, 400)
                    return
                _delete_annotation(code, freq, date_str, old_text)
                success = _add_annotation(code, freq, date_str, new_text, y_offset)
                self.send_json_response({"ok": success}, 200)
            else:
                self.send_json_response({"error": f"未知action: {action}"}, 400)
        else:
            self.send_json_response({"error": "未知路径"}, 404)

    def send_json_response(self, data, status_code):
        t_json = time.time()
        body = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
        json_time = time.time() - t_json
        body_kb = len(body) / 1024
        # 细粒度计时：序列化 / 发送HTTP头 / 写入socket 三个环节分别计时
        if body_kb > 100 or json_time > 0.1:
            print(f"[stock][耗时] JSON序列化: {json_time:.3f}s ({body_kb:.0f}KB)")
        t_header = time.time()
        self.send_response(status_code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.send_header("Pragma", "no-cache")
        self.send_header("Expires", "0")
        self.end_headers()
        header_time = time.time() - t_header
        t_write = time.time()
        self.wfile.write(body)
        write_time = time.time() - t_write
        # 汇总：任一环节超 50ms 就打印明细，方便定位瓶颈
        if json_time > 0.05 or header_time > 0.05 or write_time > 0.05:
            print(f"[stock][耗时] 响应明细 序列化={json_time:.3f}s 头部={header_time:.3f}s 写入={write_time:.3f}s ({body_kb:.0f}KB)")

    def log_message(self, format, *args):
        pass


# ============================================================
# HTML 模板（完整版，从 czsc 版本适配）
# ============================================================
HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">
    <meta http-equiv="Pragma" content="no-cache">
    <meta http-equiv="Expires" content="0">
    <title>缠论K线分析 - chan.py</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'PingFang SC', 'Hiragino Sans GB',
                         'Microsoft YaHei', 'Helvetica Neue', Helvetica, Arial, sans-serif;
            background: #1a1a2e; color: #e0e0e0;
            overflow: hidden; height: 100vh; user-select: none;
        }
        .header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 8px 20px; background: #16213e;
            border-bottom: 1px solid #0f3460; height: 48px;
        }
        .header-left { display: flex; align-items: center; gap: 16px; }
        .stock-input {
            display: flex; align-items: center; gap: 6px;
            position: relative;
        }
        .stock-input label { color: #8892b0; font-size: 12px; white-space: nowrap; }
        .stock-input input {
            width: 150px; padding: 4px 6px; font-size: 12px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #a8b2d1; outline: none; color-scheme: dark;
        }
        .stock-input input:focus { border-color: #e94560; }
        .stock-input-wrap {
            position: relative; display: inline-block;
        }
        .stock-input-wrap input { padding-right: 22px !important; }
        .stock-input-clear {
            position: absolute; right: 5px; top: 50%; transform: translateY(-50%);
            color: #555; font-size: 13px; cursor: pointer;
            user-select: none; line-height: 1; z-index: 1;
        }
        .stock-input-clear:hover { color: #e94560; }
        .stock-input button {
            padding: 4px 12px; font-size: 12px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #a8b2d1; cursor: pointer; transition: all 0.2s;
        }
        .stock-input button:hover { background: #0f3460; color: #e0e0e0; }
        .stock-history {
            position: absolute; top: 100%; left: 0;
            background: #16213e; border: 1px solid #0f3460; border-radius: 4px;
            max-height: 264px; overflow-y: auto; z-index: 200;
            display: none; min-width: 120px;
        }
        .stock-history.show { display: block; }
        .stock-history-item {
            padding: 4px 10px; font-size: 12px; color: #a8b2d1;
            cursor: pointer; white-space: nowrap;
            display: flex; justify-content: space-between; align-items: center;
        }
        .stock-history-item:hover { background: #0f3460; color: #e0e0e0; }
        .stock-history-del {
            color: #555; font-size: 14px; margin-left: 12px; padding: 0 2px;
            cursor: pointer; flex-shrink: 0;
        }
        .stock-history-del:hover { color: #e94560; }
        .stock-history-clear {
            padding: 4px 10px; font-size: 12px; color: #555;
            cursor: pointer; text-align: center;
            border-top: 1px solid #0f3460;
        }
        .stock-history-clear:hover { color: #e94560; }
        .stock-name { font-size: 18px; font-weight: 700; color: #e94560; }
        .stock-code { font-size: 11px; color: #8892b0; }
        .header-right { display: flex; align-items: center; gap: 12px; }
        .btn-icon {
            display: inline-flex; align-items: center; justify-content: center;
            width: 28px; height: 28px; border-radius: 4px;
            border: 1px solid #0f3460; background: #1a1a2e;
            cursor: pointer; transition: all 0.2s; padding: 0;
            flex-shrink: 0;
        }
        .btn-icon:hover { background: #0f3460; }
        .btn-icon.active { background: #e94560; border-color: #e94560; }
        .btn-icon.active svg { fill: #fff; }
        .btn-icon svg { width: 16px; height: 16px; fill: #a8b2d1; }
        .btn-icon:hover svg { fill: #e0e0e0; }
        .btn {
            padding: 4px 12px; border-radius: 4px; border: 1px solid #0f3460;
            background: #1a1a2e; color: #a8b2d1; font-size: 11px;
            cursor: pointer; transition: all 0.2s;
        }
        .btn:hover { background: #0f3460; color: #e0e0e0; }
        .btn.active { background: #e94560; border-color: #e94560; color: #fff; }
        .btn:disabled { opacity: 0.35; cursor: not-allowed; }
        .btn:disabled:hover { background: #1a1a2e; color: #a8b2d1; }
        .freq-btn {
            padding: 2px 8px; border-radius: 3px; border: 1px solid #0f3460;
            background: #1a1a2e; color: #a8b2d1; font-size: 11px;
            cursor: pointer; transition: all 0.2s; margin-left: 4px;
        }
        .freq-btn:hover { background: #0f3460; color: #e0e0e0; }
        .freq-btn.active { background: #e94560; border-color: #e94560; color: #fff; }
        .freq-btn:disabled { opacity: 0.35; cursor: not-allowed; }
        .freq-btn:disabled:hover { background: #1a1a2e; color: #a8b2d1; }
        .realtime-badge {
            display: none; align-items: center; gap: 4px; padding: 2px 8px;
            border-radius: 10px; background: #27ae60; color: #fff; font-size: 11px;
            font-weight: bold; margin-left: 8px; animation: pulse-badge 2s infinite;
        }
        .realtime-badge.visible { display: flex; }
        .realtime-badge.stopped { background: #7f8c8d; animation: none; }
        @keyframes pulse-badge {
            0%, 100% { opacity: 1; }
            50% { opacity: 0.6; }
        }
        #chart-container {
            width: 100%; height: calc(100vh - 80px);
            position: relative; cursor: crosshair;
        }
        #chart-container canvas { display: block; }
        .crosshair-info {
            position: absolute; top: 8px; left: 20px;
            font-size: 12px; color: #a8b2d1;
            pointer-events: none; z-index: 10; line-height: 1.6;
        }
        .crosshair-info .label { color: #8892b0; }
        .crosshair-info .up { color: #FF4444; }
        .crosshair-info .down { color: #00DD00; }
        .legend {
            position: fixed; bottom: 16px; left: 20px;
            display: flex; gap: 16px; padding: 8px 16px;
            background: rgba(22, 33, 62, 0.9);
            border-radius: 6px; border: 1px solid #0f3460;
            font-size: 12px; z-index: 100;
        }
        .legend-item { display: flex; align-items: center; gap: 6px; }
        .legend-color { width: 20px; height: 3px; border-radius: 2px; }
        .legend-dot { width: 8px; height: 8px; border-radius: 50%; }
        .loading-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: #1a1a2e; display: flex; flex-direction: column;
            align-items: center; justify-content: center;
            z-index: 1000; transition: opacity 0.5s;
        }
        .loading-overlay.hidden { opacity: 0; pointer-events: none; }
        .loading-spinner {
            width: 40px; height: 40px;
            border: 3px solid #0f3460; border-top-color: #e94560;
            border-radius: 50%; animation: spin 0.8s linear infinite;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .loading-text { margin-top: 16px; color: #8892b0; font-size: 14px; }
        .error-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: #1a1a2e; display: flex; flex-direction: column;
            align-items: center; justify-content: center; z-index: 1000;
        }
        .error-overlay.hidden { display: none; }
        .error-icon { font-size: 48px; margin-bottom: 16px; }
        .error-title { font-size: 20px; color: #e94560; margin-bottom: 8px; }
        .error-msg { font-size: 14px; color: #8892b0; max-width: 500px; text-align: center; line-height: 1.6; }
        .stats-panel {
            position: fixed; top: 56px; right: 12px;
            background: rgba(22, 33, 62, 0.95);
            border: 1px solid #0f3460; border-radius: 8px;
            padding: 12px 16px; font-size: 12px;
            z-index: 100; min-width: 180px; display: none;
        }
        .stats-panel.show { display: block; }
        .stats-title { font-size: 13px; font-weight: 600; color: #e94560;
            margin-bottom: 8px; padding-bottom: 6px;
            border-bottom: 1px solid #0f3460;
        }
        .stats-row { display: flex; justify-content: space-between; padding: 3px 0; }
        .stats-label { color: #8892b0; }
        .stats-value { color: #a8b2d1; font-weight: 500; }
        .goto-date {
            display: flex; align-items: center; gap: 6px;
        }
        .goto-date label { color: #8892b0; font-size: 12px; white-space: nowrap; }
        .goto-date input {
            width: 130px; padding: 4px 6px; font-size: 12px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #a8b2d1; outline: none;
            color-scheme: dark;
        }
        .goto-date input:focus { border-color: #e94560; }
        .goto-date button {
            padding: 4px 12px; font-size: 12px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #a8b2d1; cursor: pointer; transition: all 0.2s;
        }
        .goto-date button:hover { background: #0f3460; color: #e0e0e0; }
        .goto-date .date-arrow {
            font-size: 10px; color: #8892b0; cursor: pointer;
            padding: 2px 3px; user-select: none;
            transition: color 0.2s; line-height: 1;
        }
        .goto-date .date-arrow:hover { color: #e94560; }
        .goto-date .date-input-wrap {
            position: relative; display: inline-block;
        }
        .goto-date .date-weekday {
            position: absolute; right: 28px; top: 50%; transform: translateY(-50%);
            font-size: 11px; color: #a8b2d1; white-space: nowrap;
            pointer-events: none; user-select: none;
            padding: 0 2px;
        }
        .scan-panel {
            position: fixed; bottom: 40px; left: 10px;
            width: 420px; max-height: calc(100vh - 120px);
            background: rgba(22, 33, 62, 0.97);
            border: 1px solid #0f3460; border-radius: 8px;
            z-index: 200; display: none;
            flex-direction: column; overflow: hidden;
            box-shadow: 0 4px 24px rgba(0,0,0,0.4);
        }
        .scan-panel.show { display: flex; }
        .scan-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 10px 14px; border-bottom: 1px solid #0f3460;
            background: rgba(15, 52, 96, 0.3);
        }
        .scan-title { font-size: 13px; font-weight: 600; color: #e94560; }
        .scan-status { font-size: 11px; color: #8892b0; }
        .scan-close {
            font-size: 18px; color: #8892b0; cursor: pointer;
            padding: 0 4px; line-height: 1;
        }
        .scan-close:hover { color: #e94560; }
        .scan-save-btn {
            font-size: 11px; color: #8892b0; cursor: pointer;
            padding: 2px 8px; border: 1px solid #0f3460; border-radius: 3px;
            background: transparent; margin-right: 8px;
        }
        .scan-save-btn:hover { color: #e94560; border-color: #e94560; }
        .scan-save-btn:disabled { opacity: 0.4; cursor: not-allowed; }
        .scan-col-chk {
            width: 18px !important; flex-shrink: 0 !important;
            text-align: center !important;
        }
        .scan-col-chk input[type="checkbox"] {
            width: 12px; height: 12px; cursor: pointer;
            accent-color: #e94560; vertical-align: middle;
            filter: grayscale(0.5) brightness(0.8);
        }
        .scan-body {
            flex: 1; overflow-y: auto; padding: 8px;
            font-size: 12px;
        }
        .scan-empty {
            text-align: center; color: #555; padding: 40px 0;
        }
        .scan-loading {
            text-align: center; color: #8892b0; padding: 30px 0;
        }
        .scan-loading .spinner {
            display: inline-block; width: 20px; height: 20px;
            border: 2px solid #0f3460; border-top-color: #e94560;
            border-radius: 50%; animation: spin 0.8s linear infinite;
            margin-bottom: 8px;
        }
        @keyframes spin { to { transform: rotate(360deg); } }
        .scan-summary {
            padding: 6px 10px; margin-bottom: 6px;
            background: rgba(15, 52, 96, 0.3); border-radius: 4px;
            color: #a8b2d1; font-size: 11px;
        }
        .scan-summary b { color: #e94560; }
        .scan-stock-row {
            display: flex !important; align-items: center !important; padding: 4px 8px !important;
            border-radius: 4px !important; cursor: pointer !important;
            border-bottom: 1px solid rgba(15, 52, 96, 0.3) !important;
            flex-wrap: nowrap !important; overflow: hidden !important;
            width: 100% !important; box-sizing: border-box !important;
        }
        .scan-stock-row:hover { background: rgba(233, 69, 96, 0.1); }
        .scan-col-name {
            width: 64px !important; flex-shrink: 0 !important;
            text-align: left !important; overflow: hidden !important;
            text-overflow: ellipsis !important; white-space: nowrap !important;
            color: #8892b0 !important; font-weight: 400 !important;
        }
        .scan-col-freq {
            width: 40px !important; flex-shrink: 0 !important;
            text-align: left !important; color: #e94560 !important;
            font-size: 11px !important; white-space: nowrap !important;
            padding-left: 6px !important;
        }
        .scan-col-code {
            width: 85px !important; flex-shrink: 0 !important;
            text-align: left !important; color: #8892b0 !important;
            overflow: hidden !important; text-overflow: ellipsis !important;
            white-space: nowrap !important; padding-left: 4px !important;
        }
        .scan-col-mv {
            width: 44px !important; flex-shrink: 0 !important;
            text-align: left !important; color: #8892b0 !important;
        }
        .scan-col-ma {
            width: 56px !important; flex-shrink: 0 !important;
            text-align: left !important; color: #8892b0 !important;
        }
        .scan-col-ann {
            flex: 1 1 auto !important; min-width: 0 !important;
            text-align: left !important; color: #a8b2d1 !important;
            overflow: hidden !important; text-overflow: ellipsis !important;
            white-space: nowrap !important; padding: 0 4px 0 4px !important;
        }
        .scan-col-tags {
            flex: 1 1 auto !important; min-width: 0 !important;
            text-align: right !important;
            display: flex !important; justify-content: flex-end !important;
            gap: 2px !important; flex-wrap: nowrap !important;
            overflow: hidden !important;
        }
        .scan-bsp-tags {
            display: inline-flex !important; gap: 2px !important;
            flex-shrink: 0 !important; flex-wrap: wrap !important;
            vertical-align: middle !important;
            max-height: 28px !important; overflow: hidden !important;
        }
        .scan-bsp-tag {
            display: inline-block !important;
            padding: 1px 3px !important; border-radius: 3px !important;
            font-size: 9px !important; font-weight: 600 !important;
            white-space: nowrap !important; flex-shrink: 0 !important;
            line-height: 1.2 !important;
        }
        .scan-bsp-tag.buy { background: rgba(255, 68, 68, 0.25); color: #FF4444; }
        .scan-bsp-tag.sell { background: rgba(0, 221, 0, 0.2); color: #00DD00; }
        .scan-bsp-tag.scan-bsp-more { background: rgba(255,255,255,0.1); color: #8892b0; font-weight: 400; }
        .scan-no-result {
            text-align: center; color: #555; padding: 30px 0; font-size: 12px;
        }
        .range-slider {
            position: fixed; bottom: 0; left: 0; width: 100%; height: 32px;
            background: rgba(22, 33, 62, 0.92); z-index: 100;
            display: flex; flex-direction: column; align-items: center; justify-content: center;
            padding: 0 20px; box-sizing: border-box;
            border-top: 1px solid #0f3460;
        }
        .range-slider-track {
            position: relative; width: 100%; height: 6px;
            background: #2a2a4a; border-radius: 3px; cursor: pointer;
        }
        .range-slider-window {
            position: absolute; top: 0; height: 100%;
            background: rgba(65, 105, 225, 0.4); border-radius: 3px;
            min-width: 10px;
        }
        .range-slider-handle {
            position: absolute; top: -3px; width: 8px; height: 12px;
            background: #4169E1; border-radius: 2px; cursor: ew-resize;
            transition: background 0.15s;
        }
        .range-slider-handle:hover { background: #6495ED; }
        .range-slider-handle.left { left: -4px; }
        .range-slider-handle.right { right: -4px; }
        .range-slider-label {
            font-size: 11px; color: #a8b2d1; font-family: monospace;
            margin-top: 2px; white-space: nowrap; text-align: center;
            line-height: 1.4;
        }
        /* 双窗口模式样式 */
        #chart-top, #chart-bottom {
            width: 100%; position: relative; cursor: crosshair; overflow: hidden;
        }
        #chart-top { height: 50%; border-bottom: 2px solid #0f3460; }
        #chart-bottom { height: 50%; }
        #chart-top canvas, #chart-bottom canvas { display: block; }
        #chart-top.dual-active { outline: 2px solid rgba(233, 69, 96, 0.5); outline-offset: -2px; }
        #chart-bottom.dual-active { outline: 2px solid rgba(233, 69, 96, 0.5); outline-offset: -2px; }
        .dual-separator {
            height: 2px; background: #e94560; width: 100%;
        }
        #btn-dual.active { background: #e94560; border-color: #e94560; color: #fff; }
        /* 文字标注弹出菜单 */
        .annotation-menu {
            position: fixed; z-index: 9999; background: #16213e;
            border: 1px solid #0f3460; border-radius: 6px;
            box-shadow: 0 4px 16px rgba(0,0,0,0.4); padding: 4px 0;
            min-width: 140px; display: none;
        }
        .annotation-menu.show { display: block; }
        .annotation-menu-item {
            padding: 6px 14px; font-size: 12px; color: #a8b2d1;
            cursor: pointer; white-space: nowrap;
        }
        .annotation-menu-item:hover { background: #0f3460; color: #e0e0e0; }
        .annotation-menu-item.danger { color: #e94560; }
        .annotation-menu-item.danger:hover { background: rgba(233,69,96,0.2); }
        .annotation-menu-divider {
            height: 1px; background: #0f3460; margin: 4px 0;
        }
        /* 标注输入对话框 */
        .annotation-dialog {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.5); z-index: 10000;
            display: none; align-items: center; justify-content: center;
        }
        .annotation-dialog.show { display: flex; }
        .annotation-dialog-box {
            background: #16213e; border: 1px solid #0f3460; border-radius: 8px;
            padding: 20px 24px; min-width: 320px; box-shadow: 0 8px 32px rgba(0,0,0,0.5);
        }
        .annotation-dialog-title {
            font-size: 14px; font-weight: 600; color: #e0e0e0; margin-bottom: 12px;
        }
        .annotation-dialog-date {
            font-size: 11px; color: #8892b0; margin-bottom: 12px;
        }
        .annotation-dialog-input {
            width: 100%; padding: 6px 10px; font-size: 13px;
            background: #1a1a2e; border: 1px solid #0f3460; border-radius: 4px;
            color: #e0e0e0; outline: none; margin-bottom: 14px;
            color-scheme: dark;
        }
        .annotation-dialog-input:focus { border-color: #e94560; }
        .annotation-dialog-btns {
            display: flex; justify-content: flex-end; gap: 8px;
        }
        .annotation-dialog-btn {
            padding: 5px 16px; border-radius: 4px; font-size: 12px;
            cursor: pointer; border: 1px solid #0f3460; background: #1a1a2e;
            color: #a8b2d1; transition: all 0.2s;
        }
        .annotation-dialog-btn:hover { background: #0f3460; color: #e0e0e0; }
        .annotation-dialog-btn.primary {
            background: #e94560; border-color: #e94560; color: #fff;
        }
        .annotation-dialog-btn.primary:hover { background: #d63850; }
        /* BSP过滤设置对话框 */
        .bsp-filter-grid {
            display: grid; grid-template-columns: 1fr 1fr; gap: 6px; margin-bottom: 14px;
        }
        .bsp-filter-label {
            display: flex; align-items: center; gap: 8px; cursor: pointer;
            font-size: 13px; color: #a8b2d1; padding: 6px 10px; border-radius: 4px;
            background: #1a1a2e; transition: background 0.2s;
            white-space: nowrap; overflow: visible;
        }
        .bsp-filter-label:hover { background: #0f3460; }
        .bsp-filter-label input[type="checkbox"] { accent-color: #e94560; }
        /* 设置抽屉（右侧滑入） */
        .settings-drawer-overlay {
            position: fixed; top: 0; left: 0; right: 0; bottom: 0;
            background: rgba(0,0,0,0.35); z-index: 9999;
            opacity: 0; visibility: hidden; transition: opacity 0.25s, visibility 0.25s;
        }
        .settings-drawer-overlay.show { opacity: 1; visibility: visible; }
        .settings-drawer {
            position: fixed; top: 0; right: 0; bottom: 0;
            width: 360px; max-width: 92vw;
            background: #16213e; border-left: 1px solid #0f3460;
            box-shadow: -8px 0 32px rgba(0,0,0,0.4);
            z-index: 10000;
            transform: translateX(100%); transition: transform 0.28s ease;
            display: flex; flex-direction: column;
        }
        .settings-drawer.show { transform: translateX(0); }
        .settings-drawer-header {
            display: flex; align-items: center; justify-content: space-between;
            padding: 14px 18px; border-bottom: 1px solid #0f3460;
        }
        .settings-drawer-title { font-size: 14px; font-weight: 600; color: #e0e0e0; }
        .settings-drawer-close {
            font-size: 20px; color: #8892b0; cursor: pointer; line-height: 1;
            padding: 0 4px;
        }
        .settings-drawer-close:hover { color: #e94560; }
        .settings-drawer-body {
            flex: 1; overflow-y: auto; padding: 16px 18px;
        }
        .settings-drawer-footer {
            padding: 12px 18px; border-top: 1px solid #0f3460;
            display: flex; justify-content: flex-end; gap: 8px;
        }
        .annotation-list-item {
            font-size: 11px; color: #8892b0; padding: 2px 0;
            cursor: pointer; display: flex; justify-content: space-between;
            align-items: center;
        }
        .annotation-list-item:hover { color: #e94560; }
        .annotation-list-del {
            color: #555; font-size: 13px; cursor: pointer; padding: 0 4px;
        }
        .annotation-list-del:hover { color: #e94560; }
    </style>
</head>
<body>
    <div class="loading-overlay" id="loading">
        <div class="loading-spinner"></div>
        <div class="loading-text">正在加载K线数据...</div>
    </div>
    <div class="error-overlay hidden" id="error">
        <div class="error-icon">&#9888;</div>
        <div class="error-title">数据加载失败</div>
        <div class="error-msg" id="error-msg">
            数据加载失败，请检查网络连接或稍后重试。
        </div>
    </div>
    <div class="header">
        <div class="header-left">
            <div class="stock-input">
                <label>代码:</label>
                <div class="stock-input-wrap">
                    <input type="text" id="stock-code-input" placeholder="如 SZZS、GZMT、600519" onkeydown="onInputKeydown(event)" onfocus="clearInput();showHistory()" oninput="onInputChange()" />
                    <span class="stock-input-clear" id="input-clear" onclick="clearInput();document.getElementById('stock-code-input').focus()" style="display:none">&times;</span>
                </div>
                <button onclick="loadStock()">查询</button>
                <div class="stock-history" id="stock-history"></div>
            </div>
            <span class="stock-name" id="stock-name">--</span>
            <span class="stock-code" id="stock-code">--</span>
            <span id="freq-selector">
                <button class="freq-btn" id="btn-w" onclick="switchFreq('w')">周K</button>
                <button class="freq-btn" id="btn-d" onclick="switchFreq('d')">日K</button>
                <button class="freq-btn" id="btn-30m" onclick="switchFreq('30m')">30分</button>
                <button class="freq-btn active" id="btn-5m" onclick="switchFreq('5m')">5分</button>
                <button class="freq-btn" id="btn-1m" onclick="switchFreq('1m')">1分</button>
                <button class="freq-btn" id="btn-15s" onclick="switchFreq('15s')">15秒</button>
            </span>
            <span class="realtime-badge" id="realtime-badge" title="实时推送中">● 实时</span>
        </div>
        <div class="header-right">
            <div class="goto-date">
                <span class="date-arrow" id="date-arrow-left" onclick="dateStep(-1)" title="前一天">&#9664;</span>
                <span class="date-input-wrap">
                    <input type="date" id="goto-date-input" onchange="handleDateChange()" onkeydown="handleDateKeydown(event)" onblur="handleDateBlur()" oninput="handleDateInput(event)" />
                    <span class="date-weekday" id="date-weekday"></span>
                </span>
                <span class="date-arrow" id="date-arrow-right" onclick="dateStep(1)" title="后一天">&#9654;</span>
            </div>
            <button class="btn" id="btn-dual" onclick="toggleDualWindow()">双窗口</button>
                <button class="btn" id="btn-fx" onclick="toggleOverlay('fx')">分型</button>
                <button class="btn active" id="btn-bi" onclick="toggleOverlay('bi')">笔</button>
                <button class="btn" id="btn-seg" onclick="toggleOverlay('seg')">线段</button>
                <button class="btn active" id="btn-zs" onclick="toggleOverlay('zs')">中枢</button>
                <button class="btn active" id="btn-bsp" onclick="toggleOverlay('bsp')">买卖点</button>
            <button class="btn" id="btn-scan" onclick="startScanZxg()" title="扫描股票买卖点">股票扫描</button>
            <button class="btn" id="btn-restart" disabled onclick="restartStock()" title="清除选点，按冷启动重新加载">重置</button>
            <button class="btn" id="btn-stats" onclick="toggleStats()">统计</button>
            <button class="btn-icon" id="btn-refresh" title="刷新股票名称" onclick="refreshStockNames()">
                <svg viewBox="0 0 24 24"><path d="M17.65 6.35A7.96 7.96 0 0012 4C7.58 4 4.01 7.58 4.01 12S7.58 20 12 20c3.73 0 6.84-2.55 7.73-6h-2.08A5.99 5.99 0 0112 18c-3.31 0-6-2.69-6-6s2.69-6 6-6c1.66 0 3.14.69 4.22 1.78L13 11h7V4l-2.35 2.35z"/></svg>
            </button>
            <span id="refresh-status" style="color:#a8b2d1;font-size:11px;margin-left:4px;display:none;"></span>
            <button class="btn-icon" id="btn-settings" title="设置" onclick="openBspSettings()">
                <svg viewBox="0 0 24 24"><path d="M19.14 12.94c.04-.3.06-.61.06-.94 0-.32-.02-.64-.07-.94l2.03-1.58a.49.49 0 00.12-.61l-1.92-3.32a.49.49 0 00-.59-.22l-2.39.96c-.5-.38-1.03-.7-1.62-.94l-.36-2.54a.484.484 0 00-.48-.41h-3.84c-.24 0-.43.17-.47.41l-.36 2.54c-.59.24-1.13.57-1.62.94l-2.39-.96a.49.49 0 00-.59.22L2.74 8.87c-.12.21-.08.47.12.61l2.03 1.58c-.05.3-.07.62-.07.94s.02.64.07.94l-2.03 1.58a.49.49 0 00-.12.61l1.92 3.32c.12.22.37.29.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41h3.84c.24 0 .44-.17.47-.41l.36-2.54c.59-.24 1.13-.56 1.62-.94l2.39.96c.22.08.47 0 .59-.22l1.92-3.32c.12-.22.07-.47-.12-.61l-2.01-1.58zM12 15.6A3.6 3.6 0 1115.6 12 3.611 3.611 0 0112 15.6z"/></svg>
            </button>
        </div>
    </div>
    <div id="chart-container">
        <div class="crosshair-info" id="crosshair-info"></div>
    </div>
    <div class="range-slider" id="range-slider">
        <div class="range-slider-track" id="slider-track">
            <div class="range-slider-window" id="slider-window">
                <div class="range-slider-handle left" id="slider-handle-left"></div>
                <div class="range-slider-handle right" id="slider-handle-right"></div>
            </div>
        </div>
        <div class="range-slider-label" id="slider-label"></div>
    </div>

    <div class="stats-panel" id="stats-panel">
        <div class="stats-title">缠论统计</div>
        <div id="stats-content"></div>
    </div>
    <div class="scan-panel" id="scan-panel">
        <div class="scan-header">
            <span class="scan-title" id="scan-title">买卖点扫描</span>
            <button class="scan-save-btn" id="scan-save-btn" onclick="saveScanToZxg()" disabled title="保存勾选到通达信+同花顺自选股">保存到自选</button>
            <span class="scan-status" id="scan-status"></span>
            <span class="scan-close" onclick="closeScanPanel()">&times;</span>
        </div>
        <div class="scan-body" id="scan-body">
            <div class="scan-empty">点击上方"股票扫描"按钮开始</div>
        </div>
    </div>
    <div class="redframe-debug" id="redframe-debug" style="position:fixed;bottom:10px;right:10px;background:#1a1a2e;color:#fff;border:1px solid #e94560;border-radius:4px;padding:6px 10px;font-size:11px;font-family:monospace;z-index:9999;display:none;max-width:320px;pointer-events:none;">
        <b style="color:#e94560;">红框调试</b> | <span id="rfdb-state">--</span>
        <span id="rfdb-detail"></span>
    </div>
    <!-- 文字标注右键菜单 -->
    <div class="annotation-menu" id="annotation-menu">
        <div class="annotation-menu-item" id="annotation-menu-edit-one" onclick="annotationEditAnnotation()" style="display:none;">修改标注</div>
        <div class="annotation-menu-item" id="annotation-menu-delete-one" onclick="annotationDeleteAnnotation()" style="display:none;">删除标注</div>
        <div class="annotation-menu-item" id="annotation-menu-add" onclick="annotationAdd()">添加标注</div>
        <div class="annotation-menu-item danger" id="annotation-menu-del-all" onclick="annotationDeleteAllGlobal()">删除全部</div>
        <div class="annotation-menu-divider" id="annotation-menu-divider"></div>
        <div class="annotation-menu-item" id="annotation-menu-replay" onclick="annotationReplayToHere()">复盘到此</div>
        <div class="annotation-menu-divider" id="annotation-menu-divider2"></div>
        <div class="annotation-menu-item" id="annotation-menu-mirror" onclick="toggleMirrorMode()">反转视图</div>
    </div>
    <!-- 文字标注输入对话框 -->
    <div class="annotation-dialog" id="annotation-dialog">
        <div class="annotation-dialog-box">
            <div class="annotation-dialog-title" id="annotation-dialog-title">添加文字标注</div>
            <div class="annotation-dialog-date" id="annotation-dialog-date"></div>
            <input class="annotation-dialog-input" id="annotation-dialog-input" type="text" placeholder="输入标注文字，如：支撑位、减仓点" maxlength="50" onkeydown="annotationDialogKeydown(event)" />
            <div class="annotation-dialog-btns">
                <button class="annotation-dialog-btn" onclick="annotationDialogCancel()">取消</button>
                <button class="annotation-dialog-btn primary" onclick="annotationDialogConfirm()">确定</button>
            </div>
        </div>
    </div>
    <!-- 股票扫描模式选择对话框 -->
    <div class="annotation-dialog" id="scan-mode-dialog">
        <div class="annotation-dialog-box">
            <div class="annotation-dialog-title">股票扫描</div>
            <div style="margin-bottom:10px;font-size:12px;color:#8892b0;">扫描来源（可多选）</div>
            <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:8px;">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="checkbox" name="scan-source" value="zxg" checked style="accent-color:#e94560;" />
                    自选股
                </label>
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="checkbox" name="scan-source" value="sz50" style="accent-color:#e94560;" />
                    上证50
                </label>
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="checkbox" name="scan-source" value="hs300" style="accent-color:#e94560;" />
                    沪深300
                </label>
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="checkbox" name="scan-source" value="zz500" style="accent-color:#e94560;" />
                    中证500
                </label>
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="checkbox" name="scan-source" value="zz1000" style="accent-color:#e94560;" />
                    中证1000
                </label>
            </div>
            <div style="display:flex;gap:6px;margin-bottom:14px;">
                <button onclick="scanSourceSelectAll()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">全选</button>
                <button onclick="scanSourceSelectNone()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">取消</button>
            </div>
            <div style="margin-bottom:10px;font-size:12px;color:#8892b0;">扫描模式</div>
            <div style="display:flex;flex-direction:column;gap:8px;margin-bottom:14px;">
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="radio" name="scan-mode" value="ann" checked onchange="updateScanRecentDisabled()" style="accent-color:#e94560;" />
                    标注
                </label>
                <label style="display:flex;align-items:center;gap:8px;cursor:pointer;font-size:13px;color:#a8b2d1;padding:6px 10px;border-radius:4px;background:#1a1a2e;" onmouseover="this.style.background='#0f3460'" onmouseout="this.style.background='#1a1a2e'">
                    <input type="radio" name="scan-mode" value="bsp" onchange="updateScanRecentDisabled()" style="accent-color:#e94560;" />
                    买/卖点
                </label>
            </div>
            <div id="scan-recent-row" style="display:flex;align-items:center;gap:8px;margin-bottom:14px;font-size:12px;color:#8892b0;">
                <span>最近</span>
                <input type="number" id="scan-recent-days" value="1" min="1" max="100" style="width:50px;height:24px;background:#0a0a1a;border:1px solid #2a2a3e;color:#e0e0e0;border-radius:4px;text-align:center;font-size:13px;padding:0 4px;" />
                <span>根</span>
            </div>
            <div class="annotation-dialog-btns">
                <button class="annotation-dialog-btn" onclick="scanModeDialogCancel()">取消</button>
                <button class="annotation-dialog-btn primary" onclick="scanModeDialogConfirm()">确认</button>
            </div>
        </div>
    </div>
    <!-- 设置抽屉（买卖点过滤 + 均线周期） -->
    <div class="settings-drawer-overlay" id="bsp-filter-overlay" onclick="closeBspSettings()"></div>
    <aside class="settings-drawer" id="bsp-filter-dialog">
        <div class="settings-drawer-header">
            <span class="settings-drawer-title">显示设置</span>
            <span class="settings-drawer-close" onclick="closeBspSettings()">&times;</span>
        </div>
        <div class="settings-drawer-body">
            <div style="margin-bottom:8px;font-size:12px;color:#8892b0;">买卖点类型（可多选）</div>
            <div class="bsp-filter-grid">
                <label class="bsp-filter-label">
                    <input type="checkbox" name="bsp-filter" value="0" checked onchange="onBspFilterChange(this)" />
                    0类（中枢震荡）
                </label>
                <label class="bsp-filter-label">
                    <input type="checkbox" name="bsp-filter" value="1" checked onchange="onBspFilterChange(this)" />
                    1类（趋势背驰）
                </label>
                <label class="bsp-filter-label">
                    <input type="checkbox" name="bsp-filter" value="2" checked onchange="onBspFilterChange(this)" />
                    2类（二买/二卖）
                </label>
                <label class="bsp-filter-label">
                    <input type="checkbox" name="bsp-filter" value="3" checked onchange="onBspFilterChange(this)" />
                    3类（三买/三卖）
                </label>
            </div>
            <div style="display:flex;gap:6px;margin-bottom:18px;">
                <button onclick="bspFilterSelectAll()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">全选</button>
                <button onclick="bspFilterSelectNone()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">取消</button>
            </div>

            <div style="margin-bottom:8px;font-size:12px;color:#8892b0;">均线周期（可多选，斐波那契数列）</div>
            <div class="bsp-filter-grid" id="ma-periods-grid">
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="5" onchange="onMaPeriodChange(this)" /><span style="color:#FF6B6B">●</span> MA5</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="8" onchange="onMaPeriodChange(this)" /><span style="color:#FFD93D">●</span> MA8</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="13" onchange="onMaPeriodChange(this)" /><span style="color:#6BCB77">●</span> MA13</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="21" onchange="onMaPeriodChange(this)" /><span style="color:#4D96FF">●</span> MA21</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="34" onchange="onMaPeriodChange(this)" /><span style="color:#C780FA">●</span> MA34</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="55" onchange="onMaPeriodChange(this)" /><span style="color:#FF9F40">●</span> MA55</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="89" onchange="onMaPeriodChange(this)" /><span style="color:#00CED1">●</span> MA89</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="144" onchange="onMaPeriodChange(this)" /><span style="color:#FF69B4">●</span> MA144</label>
                <label class="bsp-filter-label"><input type="checkbox" name="ma-period" value="233" onchange="onMaPeriodChange(this)" /><span style="color:#A8A8A8">●</span> MA233</label>
            </div>
            <div style="display:flex;gap:6px;margin-bottom:14px;">
                <button onclick="maPeriodsSelectAll()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">全选</button>
                <button onclick="maPeriodsSelectNone()" style="font-size:11px;padding:2px 8px;background:#1a1a2e;border:1px solid #2a2a3e;color:#8892b0;border-radius:3px;cursor:pointer;">取消</button>
            </div>
        </div>
    </aside>
    <script>
    (function() {
        "use strict";
        let chartData = null, canvas, ctx;
        let showBi = true, showFx = false, showMa = false, showZs = true, showSeg = false, showBsp = true;
        // BSP买卖点类型过滤：默认全部显示（0,1,2,3 对应 bs_type 配置）
        let bspFilter = { '0': true, '1': true, '2': true, '3': true };
        // 均线周期：选中的周期集合，默认空（不显示均线）。showMa 由是否有选中周期决定。
        const MA_PERIODS = [5, 8, 13, 21, 34, 55, 89, 144, 233];
        const MA_COLORS = { 5:'#FF6B6B', 8:'#FFD93D', 13:'#6BCB77', 21:'#4D96FF', 34:'#C780FA', 55:'#FF9F40', 89:'#00CED1', 144:'#FF69B4', 233:'#A8A8A8' };
        let maPeriods = {};  // {5: true, 13: true, ...}
        // 从 localStorage 恢复叠加层开关状态
        function loadOverlaySettings() {
            try {
                const raw = localStorage.getItem('chan_overlay_settings');
                if (!raw) return;
                const s = JSON.parse(raw);
                if (typeof s.showBi === 'boolean') showBi = s.showBi;
                if (typeof s.showFx === 'boolean') showFx = s.showFx;
                if (typeof s.showZs === 'boolean') showZs = s.showZs;
                if (typeof s.showSeg === 'boolean') showSeg = s.showSeg;
                if (typeof s.showBsp === 'boolean') showBsp = s.showBsp;
                if (s.bspFilter && typeof s.bspFilter === 'object') {
                    for (var k in s.bspFilter) { bspFilter[k] = s.bspFilter[k]; }
                }
                // 兼容旧版 showMa 布尔：若为 true 且无 maPeriods，迁移为 {5:true,120:true}
                if (s.maPeriods && typeof s.maPeriods === 'object') {
                    for (var p in s.maPeriods) { maPeriods[p] = s.maPeriods[p]; }
                } else if (s.showMa === true) {
                    maPeriods = { '5': true, '120': true };
                }
            } catch(e) {}
        }
        // 保存叠加层开关状态到 localStorage
        function saveOverlaySettings() {
            try {
                const s = {
                    showBi: showBi, showFx: showFx,
                    showZs: showZs, showSeg: showSeg, showBsp: showBsp,
                    bspFilter: bspFilter,
                    maPeriods: maPeriods
                };
                localStorage.setItem('chan_overlay_settings', JSON.stringify(s));
            } catch(e) {}
        }
        // showMa 由是否有选中均线周期决定
        function getShowMa() { return Object.keys(maPeriods).some(function(p){ return maPeriods[p]; }); }
        // 根据保存的设置更新按钮 UI 状态
        function applyOverlayButtonStates() {
            document.getElementById("btn-bi").classList.toggle("active", showBi);
            document.getElementById("btn-fx").classList.toggle("active", showFx);
            document.getElementById("btn-zs").classList.toggle("active", showZs);
            document.getElementById("btn-seg").classList.toggle("active", showSeg);
            document.getElementById("btn-bsp").classList.toggle("active", showBsp);
        }
        // 启动时加载保存的设置
        loadOverlaySettings();
        // 频率→秒数映射
        const FREQ_SEC_MAP_JS = { 'w': 604800, 'd': 86400, '30m': 1800, '5m': 300, '1m': 60, '15s': 15 };
        const PADDING = { top: 20, right: 22, bottom: 36, left: 10 };
        const VOL_RATIO = 0.2, GAP = 12;
        const MACD_TEXT_HEIGHT = 14;
        let viewOffset = 0, viewCount = 377;
        let isDragging = false, dragStartX = 0, dragStartOffset = 0;
        let mouseX = -1, mouseY = -1;
        let _currentClipText = "";
        let _mouseDownX = 0, _mouseDownY = 0;
        // 区间选择状态机: IDLE(空闲) | SELECTED_A(已选起点)
        let _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
        let _currentGlobalIdx = -1;
        let _overlayData = null;
        let initialized = false;
        let currentFreq = 'd'; // 当前周期: d=日K, 30m=30分钟
        let lastStockFreq = 'd';     // 股票上下文上次使用的周期（同类切换继承）
        let lastFuturesFreq = '1m'; // 期货上下文上次使用的周期（同类切换继承）
        // 双窗口状态
        let isDualWindow = false;
        let dualBottomData = null;
        let dualBottomFreq = '';
        let dualBottomViewOffset = 0, dualBottomViewCount = 377;
        let dualBottomMouseX = -1, dualBottomMouseY = -1;
        let topCanvas, topCtx, bottomCanvas, bottomCtx;
        // 反转视图模式：将上涨行情反转为下跌、下跌反转为上涨（缠论做空视角）
        let _isMirrorMode = false;
        let dualBottomIsDragging = false, dualBottomDragStartX = 0, dualBottomDragStartOffset = 0;
        let dualBottomMouseDownX = 0, dualBottomMouseDownY = 0; // 底部窗口点击坐标
        let _bottomCurrentGlobalIdx = -1; // 底部窗口当前鼠标指向的全局索引
        let _bottomClipText = ""; // 底部窗口当前K线信息文本
        let dualHighlightRange = null; // {startIdx, endIdx} 下面窗口高亮范围（灰框）
        let dualRedRange = null;     // {beforeStart, beforeEnd, afterStart, afterEnd} 下面窗口红框范围
        let dualOffscreenState = false; // 状态A：当前鼠标指向的K线对应区间在下面窗口视口外
        let dualNewZsData = null;       // 双窗口新模式：红框内笔计算的新中枢数据 {zs: [...], zs_stars: [...]}
        let dualShowNewZs = false;      // 双窗口新模式：是否绘制新中枢（替代原线段/中枢/买卖点）
        let dualNewZsLeftDate = "";     // 双窗口新模式：上次请求的红框左边界日期（用于去重）
        let dualNewZsRightDate = "";    // 双窗口新模式：上次请求的红框右边界日期（用于去重）
        let dualNewZsFailedKey = "";    // 双窗口新模式：失败请求去重，避免同一红框反复请求
        let activeDualWindow = 'top';   // 当前激活的窗口：'top' 或 'bottom'，控制底部滚动条作用于哪个窗口
        let _ctrlPressed = false;         // Ctrl键是否按下（用于红框计算优化）

        // 文字标注状态
        let annotations = [];          // 当前标注列表: [{date, text, y_offset}]
        let _annotationTargetDate = ""; // 右键点击的K线日期
        let _annotationTargetY = 0;     // 右键点击的Y坐标（图表内相对坐标，用于标注定位）
        let _annotationTargetX = 0;     // 右键点击的X坐标（用于菜单定位）
        let _annotationClickTarget = null; // 右键点击命中的标注对象 {date, text, y_offset}，null表示未命中
        let _annotationEditOldText = "";   // 编辑模式下被修改的旧文字
        let _annotationDialogMode = "add"; // "add" 或 "edit"

        // ===== 日期输入框：按周期切换 date / datetime-local =====
        const INTRADAY_FREQS_JS = ["30m", "5m", "1m", "15s"];
        function isIntradayFreq(freq) { return INTRADAY_FREQS_JS.indexOf(freq) >= 0; }

        // K线日期 → 输入框格式
        // K线日期: "2026/07/02" / "2026/07/02 10:35" / "2026/07/02 10:35:00"
        // date: "2026-07-02"  /  datetime-local: "2026-07-02T10:35"
        function klineDateToInput(klineDate, freq) {
            if (!klineDate) return "";
            var d = klineDate.replace(/\//g, "-");
            if (isIntradayFreq(freq)) {
                var dt = d.slice(0, 16);       // "YYYY-MM-DD HH:MM"
                return dt.replace(" ", "T");
            }
            return d.slice(0, 10);
        }

        // 输入框值 → 后端API格式
        // date: "2026-07-02" / datetime-local: "2026-07-02T10:35"
        // API: "2026-07-02" / "2026-07-02 10:35"
        function inputDateToApi(inputVal, freq) {
            if (!inputVal) return "";
            if (isIntradayFreq(freq)) return inputVal.replace("T", " ");
            return inputVal.slice(0, 10);
        }

        // 切换输入框 type 属性（date ↔ datetime-local）
        function updateDateInputType() {
            var input = document.getElementById("goto-date-input");
            var weekday = document.getElementById("date-weekday");
            var isIntra = isIntradayFreq(currentFreq);
            var oldVal = input.value;
            if (isIntra) {
                input.type = "datetime-local";
                input.step = (currentFreq === "15s") ? "15" : "60";
                // 股票：限定盘中时间 09:00-15:59；期货：全天
                var isStock = chartData && chartData.meta && chartData.meta.symbol && !isFuturesCode(chartData.meta.symbol);
                if (isStock) {
                    input.min = "1990-01-01T09:00";
                    input.max = "2099-12-31T15:59";
                } else {
                    input.min = "1990-01-01T00:00";
                    input.max = "2099-12-31T23:59";
                }
                input.style.width = "180px";
                if (oldVal && oldVal.indexOf("T") < 0) oldVal = oldVal + "T09:30";
                if (weekday) weekday.style.right = "38px";
            } else {
                input.type = "date";
                input.step = "1";
                input.min = "1990-01-01";
                input.max = "2099-12-31";
                input.style.width = "130px";
                if (oldVal && oldVal.indexOf("T") >= 0) oldVal = oldVal.slice(0, 10);
                if (weekday) weekday.style.right = "28px";
            }
            input.value = oldVal;
            // datetime-local：picker 打开时记录原始值
            if (isIntra) {
                input.onfocus = function() {
                    var v = input.value;
                    if (!v) return;
                    _datePickerInteracted = false;
                    _datePickerInputCount = 0;
                    _dateFocusOriginal = v;
                };
            } else {
                input.onfocus = null;
            }
            // 箭头提示
            var la = document.getElementById("date-arrow-left");
            var ra = document.getElementById("date-arrow-right");
            if (la) la.title = isIntra ? "前一根" : "前一天";
            if (ra) ra.title = isIntra ? "后一根" : "后一天";
        }

        // 实时模式（期货/期指 SSE 推送）
        let isRealtimeMode = false;       // 是否处于实时模式
        let realtimeSymbol = null;        // 实时模式下当前品种代码
        let realtimeFreq = null;          // 实时模式下当前周期
        let realtimeStartTime = null;     // 实时模式下选点起始时间
        let realtimeEventSource = null;   // SSE EventSource 对象
        let reconnectTimer = null;          // 重连定时器（防止 onerror 多次触发导致重复连接）
        let reconnectCount = 0;            // 重连次数计数
        const MAX_RECONNECT = 3;           // 最大重连次数
        let realtimeStopped = false;       // 彻底放弃重连标志（阻止 onerror 死循环）
        let realtimeConnected = false;    // SSE 是否已连接

        // 辅助函数：30分钟K线显示时间
        function getKlineEndTime(dateStr, showSeconds) {
            const parts = dateStr.split(/[-\/\s:]/);
            const yy = parts[0].slice(2);
            const mm = parts[1];
            const dd = parts[2];
            const hh = parts[3];
            const min = parts[4];
            const ss = parts[5];
            if (showSeconds && ss !== undefined) {
                return `${yy}/${mm}/${dd} ${hh}:${min}:${ss}`;
            }
            return `${yy}/${mm}/${dd} ${hh}:${min}`;
        }
        // 双窗口：上面周期 -> 下面周期映射
        function getDualBottomFreq(topFreq) {
            // 股票周期映射
            if (topFreq === 'w') return 'd';
            if (topFreq === 'd') return '30m';
            if (topFreq === '30m') return '5m';
            // 期货周期映射（股票5m无对应，期货5m→1m）
            if (topFreq === '5m') return '1m';
            if (topFreq === '1m') return '15s';
            return null; // 5m(股票)/15s(期货)无对应
        }
        // 双窗口：获取上面窗口某根K线对应的灰框边界（子级别K线时间字符串）
        // 通用方案：利用相邻K线时间，不依赖周期长度假设
        //   期货：K线时间=开始时间。左边界=当前时间X，右边界=(下一根时间Y - bottom_sec)
        //   股票：K线时间=结束时间。左边界=(上一根时间Y + bottom_sec)，右边界=当前时间X
        //         日期型K线（如d/w）无时分秒，解析时视为当日结束时刻(23:59:59)
        // 返回 {start: string|null, end: string|null}，null 表示边界在数据范围外
        function getTopKlineTimeRange(kline, idx, klines, isFutures, bottomFreq) {
            const bottomSec = FREQ_SEC_MAP_JS[bottomFreq];
            if (!bottomSec) return null;
            const dateLen = kline.date.length;  // 19=含秒, 16=含分, 10=仅日期
            function fmt(d) {
                const y = d.getFullYear();
                const mo = String(d.getMonth() + 1).padStart(2, '0');
                const da = String(d.getDate()).padStart(2, '0');
                if (dateLen >= 19) {
                    const h = String(d.getHours()).padStart(2, '0');
                    const mi = String(d.getMinutes()).padStart(2, '0');
                    const s = String(d.getSeconds()).padStart(2, '0');
                    return `${y}/${mo}/${da} ${h}:${mi}:${s}`;
                } else if (dateLen >= 16) {
                    const h = String(d.getHours()).padStart(2, '0');
                    const mi = String(d.getMinutes()).padStart(2, '0');
                    return `${y}/${mo}/${da} ${h}:${mi}`;
                }
                return `${y}/${mo}/${da}`;
            }
            function parse(ds) {
                // 日期型K线（仅日期）→ 视为当日结束时刻 23:59:59.999
                if (ds.length === 10) return new Date(ds.replace(/\//g, "-") + "T23:59:59");
                return new Date(ds.replace(/\//g, "-").replace(" ", "T"));
            }
            if (isFutures) {
                // 期货：左边界 = 当前K线时间X（精确匹配）
                const start = kline.date;
                // 右边界 = (下一根K线时间Y - bottom_sec) 记为Z
                let end = null;
                if (idx + 1 < klines.length) {
                    const nextD = parse(klines[idx + 1].date);
                    const endD = new Date(nextD.getTime() - bottomSec * 1000);
                    end = fmt(endD);
                }
                return { start, end };
            } else {
                // 股票：左边界 = (上一根K线时间Y + bottom_sec) 记为Z
                let start = null;
                if (idx > 0) {
                    const prevD = parse(klines[idx - 1].date);
                    const startD = new Date(prevD.getTime() + bottomSec * 1000);
                    start = fmt(startD);
                }
                // 右边界 = 当前K线时间X（精确匹配）
                const end = kline.date;
                return { start, end };
            }
        }
        // 双窗口：根据上面窗口鼠标位置计算下面窗口高亮范围
        function calcGrayRange(topMouseX) {
            if (!isDualWindow || !dualBottomData || !chartData) return null;
            const area = getChartArea();
            const klines = getVisibleKlines();
            if (!klines.length) return null;
            const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
            const barStep = area.w / effectiveCount;
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            const idx = Math.floor((topMouseX - area.x + subPixelOffset) / barStep);
            if (idx < 0 || idx >= klines.length) return null;
            const topKline = klines[idx];
            const bottomKlines = dualBottomData.klines;
            let startIdx = -1, endIdx = -1;
            // 方案B：优先使用 sub_kl_times（后端多级别CChan返回的子级别K线时间列表）
            if (topKline.sub_kl_times && topKline.sub_kl_times.length > 0) {
                const subTimes = topKline.sub_kl_times;
                const firstTime = subTimes[0];
                const lastTime = subTimes[subTimes.length - 1];
                for (let i = 0; i < bottomKlines.length; i++) {
                    const bk = bottomKlines[i];
                    if (bk.date >= firstTime && startIdx === -1) startIdx = i;
                    if (bk.date <= lastTime) endIdx = i;
                }
            } else {
                // 通用方案：利用相邻K线时间精确计算灰框边界
                const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
                const timeRange = getTopKlineTimeRange(topKline, idx, klines, isFutures, dualBottomFreq);
                if (!timeRange) return null;
                // 左边界：用 >= 匹配（字符串比较对 ISO 日期天然正确）
                if (timeRange.start) {
                    for (let i = 0; i < bottomKlines.length; i++) {
                        if (bottomKlines[i].date >= timeRange.start) { startIdx = i; break; }
                    }
                }
                // 右边界：用前缀匹配（兼容 d→30m 等跨格式场景），回退 <=
                if (timeRange.end) {
                    const endLen = timeRange.end.length;
                    for (let i = bottomKlines.length - 1; i >= 0; i--) {
                        if (bottomKlines[i].date.slice(0, endLen) === timeRange.end) { endIdx = i; break; }
                    }
                    if (endIdx === -1) {
                        for (let i = 0; i < bottomKlines.length; i++) {
                            if (bottomKlines[i].date <= timeRange.end) endIdx = i;
                        }
                    }
                }
                // 边界在数据范围外：用首/尾替代
                if (timeRange.start === null && startIdx === -1) startIdx = 0;
                if (timeRange.end === null && endIdx === -1) endIdx = bottomKlines.length - 1;
            }
            // 下面窗口数据中没有匹配的K线（上面K线日期超出了下面数据范围）
            if (startIdx === -1) {
                // 用上面K线日期与下面数据首尾日期比较来判断方向
                const topDate = new Date(topKline.date.replace(/\//g, "-").replace(" ", "T"));
                const bottomFirstDate = new Date(bottomKlines[0].date.replace(/\//g, "-").replace(" ", "T"));
                const bottomLastDate = new Date(bottomKlines[bottomKlines.length - 1].date.replace(/\//g, "-").replace(" ", "T"));
                if (topDate < bottomFirstDate) {
                    return { startIdx: -1, endIdx: -1, isVisible: false, isLeft: true, isRight: false };
                } else if (topDate > bottomLastDate) {
                    return { startIdx: -1, endIdx: -1, isVisible: false, isLeft: false, isRight: true };
                }
                return null;
            }
            // 判断高亮范围是否在下面窗口当前视口内
            const bottomGlobalStart = Math.max(0, Math.floor(dualBottomViewOffset));
            const bottomGlobalEnd = bottomGlobalStart + dualBottomViewCount;
            const isVisible = (startIdx < bottomGlobalEnd && endIdx >= bottomGlobalStart);
            const isLeft = endIdx < bottomGlobalStart;   // 整个区间在视口左边
            const isRight = startIdx >= bottomGlobalEnd;
            let redRange = null;
            if (_ctrlPressed) {
                try {
                    redRange = calcRedRange(topKline, bottomKlines, startIdx, endIdx);
                } catch (e) {
                    console.error("[红框] calcRedRange异常:", e);
                    window._lastCalcRedRangeError = String(e);
                }
            }
            return { startIdx, endIdx, isVisible, isLeft, isRight, redRange };
        }

        // 双窗口红框：鼠标指向上面K线所属笔的外沿区间（分型左肩→右肩）
        // 注意：使用 chartData.bis（复数），JSON 字段名是 "bis"
        function calcRedRange(topKline, bottomKlines, grayStart, grayEnd) {
            console.log("[红框] === 进入 calcRedRange ===");
            console.log("[红框] topKline.date=" + topKline.date + " grayStart=" + grayStart + " grayEnd=" + grayEnd);
            console.log("[红框] chartData.bis=" + (chartData && chartData.bis ? "长度" + chartData.bis.length : "null"));
            console.log("[红框] bottomKlines.length=" + bottomKlines.length);
            if (!chartData || !chartData.bis || !chartData.bis.length) {
                console.log("[红框] ❌ chartData 或 chartData.bis 为空，返回null");
                window._lastRedFrameStatus = { state: "SKIP", reason: "chartData或bis为空" };
                updateRedFrameDebug();
                return null;
            }
            const d = topKline.date;
            let bi = null;
            // 找到topKline所属的笔（交界处归属右边）
            for (let i = 0; i < chartData.bis.length; i++) {
                const b = chartData.bis[i];
                if (d >= b.sdt && d < b.edt) { bi = b; console.log("[红框] 找到笔(主循环): idx=" + i + " sdt=" + b.sdt + " edt=" + b.edt + " dir=" + b.direction); break; }
            }
            if (!bi) {
                for (let i = chartData.bis.length - 1; i >= 0; i--) {
                    if (d === chartData.bis[i].edt) { bi = chartData.bis[i]; console.log("[红框] 找到笔(edt匹配): idx=" + i + " sdt=" + chartData.bis[i].sdt + " edt=" + chartData.bis[i].edt); break; }
                }
            }
            if (!bi) {
                console.log("[红框] ❌ 未找到所属笔，topKline.date=" + d + " 不在任何笔的[sdt, edt)范围内");
                window._lastRedFrameStatus = { state: "SKIP", reason: "未找到所属笔", topDate: d, biCount: chartData.bis.length };
                updateRedFrameDebug();
                return null;
            }
            const aDt = bi.fx_a_sub_dt || bi.fx_a_raw_dt, bDt = bi.fx_b_sub_dt || bi.fx_b_raw_dt;
            if (!aDt || !bDt) {
                console.log("[红框] ❌ aDt='" + (aDt || "") + "' bDt='" + (bDt || "") + "' 为空");
                console.log("[红框]    bi.sdt=" + bi.sdt + " bi.edt=" + bi.edt + " bi.direction=" + bi.direction);
                window._lastRedFrameStatus = { state: "SKIP", reason: "fx_a或fx_b为空", sdt: bi.sdt, edt: bi.edt };
                updateRedFrameDebug();
                return null;
            }
            // fx_a_sub_dt / fx_b_sub_dt 是后端从分型原始K线对应的次级别序列边界直接算出的
            // 双窗口下直接就是次级别K线时间，>= / <= 精确匹配即可
            const aLen = aDt.length, bLen = bDt.length;
            let aIdx = -1, bIdx = -1;
            const bottomFirstDate = bottomKlines[0].date.slice(0, aLen);
            const bottomLastDate = bottomKlines[bottomKlines.length - 1].date.slice(0, bLen);
            for (let i = 0; i < bottomKlines.length; i++) {
                const bk = bottomKlines[i];
                // A: 红框左边界（次级别第一根）
                if (aIdx === -1 && bk.date.slice(0, aLen) >= aDt) aIdx = i;
                // B: 红框右边界（次级别最后一根）
                if (bk.date.slice(0, bLen) <= bDt) bIdx = i;
            }
            // 参照灰框处理：笔区间完全在底部数据范围之外 → 不显示红框，返回null
            if (aIdx === -1 && bIdx === -1) {
                if (aDt > bottomLastDate) {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间在底部数据右侧", aDt: aDt, bottomLast: bottomLastDate };
                } else if (bDt < bottomFirstDate) {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间在底部数据左侧", bDt: bDt, bottomFirst: bottomFirstDate };
                } else {
                    window._lastRedFrameStatus = { state: "SKIP", reason: "笔区间无匹配", aDt: aDt, bDt: bDt };
                }
                updateRedFrameDebug();
                return null;
            }
            // 部分重叠：aIdx 或 bIdx 为 -1 时，截断到可见范围
            if (aIdx === -1) aIdx = 0;
            if (bIdx === -1) bIdx = bottomKlines.length - 1;
            if (aIdx > bIdx) {
                window._lastRedFrameStatus = { state: "SKIP", reason: "aIdx>bIdx", aIdx: aIdx, bIdx: bIdx };
                updateRedFrameDebug();
                return null;
            }
            // 红框时间：使用下方K线时间（精确到分钟），确保30m/5m图表显示完整时间
            const leftDate = bottomKlines[aIdx].date;
            const rightDate = bottomKlines[bIdx].date;
            // before: 笔区间在灰框之前的部分 [aIdx, grayStart-1]
            const beforeStart = aIdx, beforeEnd = Math.min(grayStart - 1, bIdx);
            // after: 笔区间在灰框之后的部分 [grayEnd+1, bIdx]
            const afterStart = grayEnd + 1, afterEnd = bIdx;
            const result = {
                beforeStart, beforeEnd,
                afterStart, afterEnd,
                hasBefore: (beforeEnd >= beforeStart),
                hasAfter: (afterEnd >= afterStart),
                leftDate: leftDate,    // 红框左边沿K线时间（下方窗口，精确到分钟）
                rightDate: rightDate,  // 红框右边沿K线时间
                aIdx: aIdx,            // 红框整体左边界（下方窗口全局索引）
                bIdx: bIdx,            // 红框整体右边界（下方窗口全局索引）
            };
            console.log("[红框] ✅ 返回红框范围: before=[" + beforeStart + "," + beforeEnd + "] hasBefore=" + result.hasBefore + " after=[" + afterStart + "," + afterEnd + "] hasAfter=" + result.hasAfter);
            window._lastRedFrameStatus = { state: "OK", reason: "calcRedRange成功", before: result.hasBefore, after: result.hasAfter, aIdx: aIdx, bIdx: bIdx, grayStart: grayStart, grayEnd: grayEnd, leftDate: result.leftDate, rightDate: result.rightDate };
            updateRedFrameDebug();
            return result;
        }
        const COLORS = {
            bg: "#1a1a2e", grid: "rgba(255,255,255,0.04)", text: "#8892b0", textLight: "#a8b2d1",
            up: "#FF4444", down: "#00DD00", bi: "#FFD700",
            crosshair: "rgba(255,255,255,0.3)",
            macdUp: "rgba(253,16,80,0.6)", macdDown: "rgba(12,244,155,0.6)", // 原值: macdUp="rgba(255,68,68,0.6)", macdDown="rgba(0,221,0,0.6)"
            dif: "#FFFFFF", dea: "#ffa710", // 原值: dea="#FFD700"
        };

        // 根据市场类型更新频率按钮的启用/禁用状态
        function updateFreqButtonStates(isFutures) {
            // 股票禁用 1m/15s，期货禁用 d/w
            document.getElementById('btn-d').disabled = isFutures;
            document.getElementById('btn-w').disabled = isFutures;
            document.getElementById('btn-1m').disabled = !isFutures;
            document.getElementById('btn-15s').disabled = !isFutures;
            // 共享周期：30m 始终启用
            document.getElementById('btn-30m').disabled = false;
            // 5m: 股票双窗口→禁用(无下级)，期货双窗口→启用(5m→1m)
            if (isDualWindow && isFutures) {
                document.getElementById('btn-5m').disabled = false;
                document.getElementById('btn-15s').disabled = true;  // 期货双窗口：15s永远禁用
            } else if (isDualWindow && !isFutures) {
                document.getElementById('btn-5m').disabled = true;
            } else {
                document.getElementById('btn-5m').disabled = false;
            }
            // 同步 active 状态
            document.querySelectorAll('.freq-btn').forEach(b => b.classList.remove('active'));
            const activeBtn = document.getElementById('btn-' + currentFreq);
            if (activeBtn) activeBtn.classList.add('active');
        }

        // 判断是否为期货/期指代码
        function isFuturesCode(code) {
            return code.includes('KQ.m@') || code.includes('KQ.i@') || /^[A-Z]+\.[A-Z]/.test(code);
        }
        // 保存当前状态到 localStorage（仅股票，期货不保存）
        function saveLastState() {
            if (!chartData || !chartData.meta) return;
            const market = chartData.meta.market;
            if (market === 'futures') return; // 期货不保存
            const state = {
                code: chartData.meta.symbol,
                freq: currentFreq,
                name: chartData.meta.name
            };
            try { localStorage.setItem('chan_last_state', JSON.stringify(state)); } catch(e) {}
        }
        // 从 localStorage 加载上次状态，仅股票有效
        function loadLastState() {
            try {
                const raw = localStorage.getItem('chan_last_state');
                if (!raw) return null;
                const state = JSON.parse(raw);
                if (!state.code || !state.freq) return null;
                if (isFuturesCode(state.code)) return null;
                return state;
            } catch(e) { return null; }
        }

        async function init() {
            try {
                // 先尝试从 localStorage 恢复上次的股票状态
                const savedState = loadLastState();
                if (savedState) {
                    // 有保存的股票状态，用初始数据占位后立即异步加载
                    chartData = %%CHART_DATA%%;
                    document.getElementById("stock-code-input").value = savedState.code;
                    // 设置初始周期
                    if (savedState.freq) {
                        currentFreq = savedState.freq;
                        lastStockFreq = savedState.freq;
                    }
                    initCanvas();
                    updateSlider();
                    updateFreqButtonStates(false);
                    updateRestartBtn();
                    updateDualBtn();
                    // 异步加载保存的股票数据
                    document.getElementById("loading").classList.remove("hidden");
                    fetch("/api/stock?code=" + encodeURIComponent(savedState.code) + "&freq=" + savedState.freq)
                        .then(resp => {
                            if (!resp.ok) throw new Error("恢复失败");
                            return resp.json();
                        })
                        .then(data => {
                            chartData = data;
                            saveHistory(savedState.code, data.meta.name);
                            document.getElementById("stock-name").textContent = chartData.meta.name;
                            document.getElementById("stock-code").textContent = chartData.meta.symbol;
                            document.title = "缠论分析 - " + chartData.meta.name;
                            let returnedFreq;
                            if (data.meta.freq === "5分钟") returnedFreq = "5m";
                            else if (data.meta.freq === "30分钟") returnedFreq = "30m";
                            else if (data.meta.freq === "周线") returnedFreq = "w";
                            else returnedFreq = "d";
                            currentFreq = returnedFreq;
                            lastStockFreq = currentFreq;
                            updateDateInputType();
                            updateFreqButtonStates(false);
                            viewCount = 377;
                            adjustViewForSavedPoint();
                            viewOffset = Math.max(0, chartData.klines.length - viewCount);
                            if (chartData.klines.length < viewCount) viewOffset = 0;
                            applyOverlayButtonStates();
                            initialized = true;
                            updateRestartBtn();
                            updateDualBtn();
                            const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                            document.getElementById("goto-date-input").value = lastDate;
                            updateWeekday();
                            render();
                            document.getElementById("loading").classList.add("hidden");
                            generateStats();
                            loadAnnotations();
                            // 断开期货SSE（如果有）
                            disconnectRealtime();
                        })
                        .catch(err => {
                            console.error("恢复上次状态失败，回退到默认:", err);
                            // 回退到默认上证指数
                            document.getElementById("stock-code-input").value = "";
                            initDefault();
                        });
                    return;
                }
                // 无保存状态，默认加载上证指数
                initDefault();
            } catch (err) {
                console.error("初始化失败:", err);
                document.getElementById("loading").classList.add("hidden");
                document.getElementById("error").classList.remove("hidden");
            }
        }

        function initDefault() {
            chartData = %%CHART_DATA%%;
            document.getElementById("stock-name").textContent = chartData.meta.name;
            document.getElementById("stock-code").textContent = chartData.meta.symbol;
            document.title = "缠论分析 - " + chartData.meta.name;
            initCanvas();
            updateSlider();
            if (chartData.meta.freq === "5分钟") currentFreq = "5m";
            else if (chartData.meta.freq === "30分钟") currentFreq = "30m";
            else if (chartData.meta.freq === "周线") currentFreq = "w";
            else currentFreq = "d";
            updateDateInputType();
            lastStockFreq = currentFreq;
            updateFreqButtonStates(false);
            viewCount = 377;
            adjustViewForSavedPoint();
            applyOverlayButtonStates();
            viewOffset = Math.max(0, chartData.klines.length - viewCount);
            if (chartData.klines.length < viewCount) viewOffset = 0;
            initialized = true;
            updateRestartBtn();
            updateDualBtn();
            const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
            document.getElementById("goto-date-input").value = lastDate;
            updateWeekday();
            render();
            document.getElementById("loading").classList.add("hidden");
            generateStats();
            loadAnnotations();
        }

        function initCanvas() {
            const container = document.getElementById("chart-container");
            canvas = document.createElement("canvas");
            container.appendChild(canvas); ctx = canvas.getContext("2d");
            topCanvas = canvas; topCtx = ctx;
            resizeCanvas();
            window.addEventListener("resize", () => { resizeCanvas(); render(); });
            // 上面窗口事件
            canvas.addEventListener("wheel", onWheel, { passive: false });
            canvas.addEventListener("mousedown", onMouseDown);
            canvas.addEventListener("mousemove", onMouseMove);
            canvas.addEventListener("mouseup", onMouseUp);
            canvas.addEventListener("mouseleave", onMouseLeave);
            canvas.addEventListener("contextmenu", onContextMenu);
            canvas.addEventListener("dblclick", function(e) {
                if (!chartData) return;
                const rect = canvas.getBoundingClientRect();
                const clickX = e.clientX - rect.left;
                const clickY = e.clientY - rect.top;
                const area = getChartArea();
                // 1. 只在K线主图区域内有效
                if (clickX < area.x || clickX > area.x + area.w ||
                    clickY < area.y || clickY > area.y + area.h) {
                    return;
                }
                // 2. 计算当前可见K线和参数
                const klines = getVisibleKlines();
                if (!klines.length) return;
                const priceRange = getPriceRange(klines);
                const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
                const barStep = area.w / effectiveCount;
                const barWidth = Math.max(1, barStep * 0.7);
                const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
                // 3. 检查是否落在任何K线的[high,low]矩形内，同时检查是否是笔交汇点（分型）
                let clickedOnKline = false;
                let clickedBiIdx = -1;
                for (let i = 0; i < klines.length; i++) {
                    const k = klines[i];
                    const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                    const highY = priceToY(k.high, area, priceRange);
                    const lowY = priceToY(k.low, area, priceRange);
                    const halfW = barWidth / 2;
                    if (clickX >= x - halfW && clickX <= x + halfW &&
                        clickY >= highY && clickY <= lowY) {
                        clickedOnKline = true;
                        // 通过笔数据判断交汇点：双击K线日期 == 某笔edt == 下一笔sdt
                        const globalStart = Math.max(0, Math.floor(viewOffset));
                        const globalIdx = globalStart + i;
                        const kline = chartData.klines[globalIdx];
                        if (kline) {
                            let dateStr = kline.date;
                            for (let j = 0; j < chartData.bis.length - 1; j++) {
                                if (chartData.bis[j].edt === dateStr && chartData.bis[j + 1].sdt === dateStr) {
                                    clickedBiIdx = j + 1;
                                    break;
                                }
                            }
                        }
                        break;
                    }
                }
                // 复盘模式下不支持双击选点（在K线检测之后判断，确保只对K线上的双击弹提示）
                if (chartData.meta && chartData.meta.is_replay && clickedOnKline) {
                    showDualToast("复盘模式，不支持选点");
                    return;
                }
                // 4. 如果双击落在分型K线上且找到对应笔，手选进入段
                if (clickedBiIdx >= 0) {
                    const code = chartData.meta.symbol;
                    const freq = currentFreq;
                    const isFutures = chartData.meta.market === 'futures';
                    document.getElementById("loading").classList.remove("hidden");
                    document.querySelector(".loading-text").textContent = "正在手选进入段...";
                    const apiPath = isFutures
                        ? "/api/futures_manual_select_point?symbol=" + encodeURIComponent(code) + "&freq=" + freq + "&bi_idx=" + clickedBiIdx
                : "/api/stocks_manual_select_point?code=" + encodeURIComponent(code) + "&freq=" + freq + "&bi_idx=" + clickedBiIdx;
                    fetch(apiPath)
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "手选失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            // 检查后端返回的错误
                            if (data.error) {
                                throw new Error(data.error);
                            }
                            // 期货：断开旧SSE，从选点时间重新连接
                            if (isFutures) {
                                chartData = data;
                                adjustViewForSavedPoint();
                                document.getElementById("stock-name").textContent = chartData.meta.name;
                                document.getElementById("stock-code").textContent = chartData.meta.symbol;
                                document.title = "缠论分析 - " + chartData.meta.name;
                                if (chartData.klines.length > 0) {
                                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                                    document.getElementById("goto-date-input").value = lastDate;
                                }
                                updateWeekday();
                                document.getElementById("loading").classList.add("hidden");
                                document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                                updateRestartBtn();
                                updateDualBtn();
                                resizeCanvas();
                                render();
                                generateStats();
                                loadAnnotations();
                                // 重连SSE，带上选点时间
                                const savedDate = chartData.meta.saved_selection_date;
                                connectRealtimeInit(code, freq, savedDate);
                                return;
                            }
                            // data 现在是完整的 chartData JSON（CChanB 从T重新计算的结果）
                            // 全文替换 chartData
                            chartData = data;
                            // 根据数据中的 freq 自动识别周期
                            if (chartData.meta.freq === "5分钟") {
                                currentFreq = "5m";
                            } else if (chartData.meta.freq === "30分钟") {
                                currentFreq = "30m";
                            } else if (chartData.meta.freq === "周线") {
                                currentFreq = "w";
                            } else {
                                currentFreq = "d";
                            }
                            updateDateInputType();
                            // 同步按钮状态
                            document.getElementById("btn-d").classList.toggle("active", currentFreq === "d");
                            document.getElementById("btn-w").classList.toggle("active", currentFreq === "w");
                            document.getElementById("btn-30m").classList.toggle("active", currentFreq === "30m");
                            document.getElementById("btn-5m").classList.toggle("active", currentFreq === "5m");
                            // 重置视图：选点后klines只含选点之后的K线，直接全部显示
                            adjustViewForSavedPoint();
                            // 更新DOM
                            document.getElementById("stock-name").textContent = chartData.meta.name;
                            document.getElementById("stock-code").textContent = chartData.meta.symbol;
                            document.title = "缠论分析 - " + chartData.meta.name;
                            const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                            document.getElementById("goto-date-input").value = lastDate;
                            updateWeekday();
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                            updateRestartBtn();
                            updateDualBtn();
                            resizeCanvas();
                            render();
                            generateStats();
                            loadAnnotations();
                        })
                        .catch(err => {
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                            setTimeout(() => {
                                alert(err.message);
                            }, 50);
                        });
                    return;
                }
                // 5. 如果双击落在K线上但不是分型，无效
                if (clickedOnKline) {
                    return;
                }
                // 6. 双击空白处
                if (isDualWindow && dualOffscreenState && dualHighlightRange && dualBottomData) {
                    // 状态A：让下面窗口平移到对应区间
                    const hr = dualHighlightRange;
                    if (hr.startIdx >= 0 && hr.endIdx >= 0) {
                        const centerIdx = (hr.startIdx + hr.endIdx) / 2;
                        const totalKlines = dualBottomData.klines.length;
                        let newOffset = Math.round(centerIdx - dualBottomViewCount / 2);
                        // 左边不够：左对齐
                        if (newOffset < 0) newOffset = 0;
                        // 右边不够：右对齐（最后一根K线贴右边缘）
                        const maxOffset = Math.max(0, totalKlines - dualBottomViewCount);
                        if (newOffset > maxOffset) newOffset = maxOffset;
                        dualBottomViewOffset = newOffset;
                        // 重新计算高亮范围（区间已移入视口，应该变为isVisible=true）
                        dualHighlightRange = calcGrayRange(mouseX);
                        dualRedRange = dualHighlightRange ? dualHighlightRange.redRange : null;
                        dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                        renderBottom();
                    } else {
                        // startIdx === -1（下面窗口无对应K线数据）
                        showDualToast("请加载更多K线...");
                    }
                    return;
                }
                // 7. 默认：恢复全视图
                viewCount = 377;
                viewOffset = Math.max(0, chartData.klines.length - viewCount);
                const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                document.getElementById("goto-date-input").value = lastDate;
                updateWeekday();
                render();
            });
        }

        function resizeCanvas() {
            const container = document.getElementById("chart-container");
            const dpr = window.devicePixelRatio || 1;
            if (isDualWindow) {
                // 双窗口模式：分别调整两个canvas
                const w = container.clientWidth;
                const hTop = container.clientHeight / 2;
                const hBottom = container.clientHeight / 2;
                if (topCanvas) {
                    topCanvas.width = w * dpr; topCanvas.height = hTop * dpr;
                    topCanvas.style.width = w + "px"; topCanvas.style.height = hTop + "px";
                    topCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
                }
                if (bottomCanvas) {
                    bottomCanvas.width = w * dpr; bottomCanvas.height = hBottom * dpr;
                    bottomCanvas.style.width = w + "px"; bottomCanvas.style.height = hBottom + "px";
                    bottomCtx.setTransform(dpr, 0, 0, dpr, 0, 0);
                }
            } else {
                // 单窗口模式
                const w = container.clientWidth, h = container.clientHeight;
                canvas.width = w * dpr; canvas.height = h * dpr;
                canvas.style.width = w + "px"; canvas.style.height = h + "px";
                ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
            }
        }

        function getChartArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top, w: totalW - rightGap, h: chartH };
        }

        function getVolArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalMacdH = (h - PADDING.top - PADDING.bottom - GAP) * VOL_RATIO;
            const macdChartH = totalMacdH - MACD_TEXT_HEIGHT;
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top + chartH + MACD_TEXT_HEIGHT,
                     w: totalW - rightGap, h: macdChartH };
        }

        function getMacdTextArea() {
            const w = canvas.clientWidth, h = canvas.clientHeight;
            const chartH = (h - PADDING.top - PADDING.bottom - GAP) * (1 - VOL_RATIO);
            const totalW = w - PADDING.left - PADDING.right;
            const rightGap = 55;
            return { x: PADDING.left, y: PADDING.top + chartH,
                     w: totalW - rightGap, h: MACD_TEXT_HEIGHT };
        }

        function getVisibleKlines() {
            if (!chartData) return [];
            const start = Math.max(0, Math.floor(viewOffset));
            const end = Math.min(chartData.klines.length, start + viewCount + 2);
            const result = chartData.klines.slice(start, end);
            // 周K：返回全部K线，确保铺满整个画布
            if (currentFreq === 'w' && result.length < viewCount) {
                return result;
            }
            return result;
        }

        function getPriceRange(klines) {
            if (!klines.length) return { min: 0, max: 100 };
            let min = Infinity, max = -Infinity;
            klines.forEach(k => { if (k.low < min) min = k.low; if (k.high > max) max = k.high; });
            const margin = (max - min) * 0.05;
            return { min: min - margin, max: max + margin };
        }

        function getMacdRange(klines) {
            if (!klines.length) return { min: -1, max: 1 };
            let min = Infinity, max = -Infinity;
            klines.forEach(k => {
                if (k.macd < min) min = k.macd;
                if (k.macd > max) max = k.macd;
                if (k.dif < min) min = k.dif;
                if (k.dif > max) max = k.dif;
                if (k.dea < min) min = k.dea;
                if (k.dea > max) max = k.dea;
            });
            const margin = Math.max(Math.abs(max), Math.abs(min)) * 0.1;
            return { min: min - margin, max: max + margin };
        }

        // 反转视图：对数据做镜像变换（价格取负 + high/low互换 + 方向取反）
        // 变换后所有绘制函数自动正确：涨K线变跌K线、MACD红柱变绿柱、笔/中枢方向自动镜像
        function _mirrorChartData(data) {
            if (!data) return data;
            // 浅拷贝顶层，klines/bis等数组深拷贝（不能修改原始缓存）
            var m = Object.assign({}, data);
            // K线: open→-open, close→-close, high↔low互换取负, macd/dif/dea取负
            m.klines = (data.klines || []).map(function(k) {
                var nk = Object.assign({}, k);
                nk.open = -k.open;
                nk.close = -k.close;
                nk.high = -k.low;   // 原low取负 → 新high
                nk.low = -k.high;   // 原high取负 → 新low
                if (k.dif !== undefined) nk.dif = -k.dif;
                if (k.dea !== undefined) nk.dea = -k.dea;
                if (k.macd !== undefined) nk.macd = -k.macd;
                return nk;
            });
            // 笔: direction up↔down, 价格取负, high↔low互换
            m.bis = (data.bis || []).map(function(b) {
                var nb = Object.assign({}, b);
                nb.direction = b.direction === 'up' ? 'down' : 'up';
                nb.fx_a_price = -b.fx_a_price;
                nb.fx_b_price = -b.fx_b_price;
                nb.high = -b.low;
                nb.low = -b.high;
                return nb;
            });
            // 分型: mark G↔D, price取负, high↔low互换
            m.fxs = (data.fxs || []).map(function(f) {
                var nf = Object.assign({}, f);
                nf.mark = f.mark === 'G' ? 'D' : 'G';
                nf.price = -f.price;
                nf.high = -f.low;
                nf.low = -f.high;
                return nf;
            });
            // 中枢: zg↔zd互换取负, gg↔dd互换取负, dir up↔down
            m.zs = (data.zs || []).map(function(z) {
                var nz = Object.assign({}, z);
                nz.zg = -z.zd;
                nz.zd = -z.zg;
                nz.gg = -z.dd;
                nz.dd = -z.gg;
                nz.dir = z.dir === 'up' ? 'down' : 'up';
                return nz;
            });
            // 线段: direction up↔down, 价格取负, high↔low互换
            m.segs = (data.segs || []).map(function(s) {
                var ns = Object.assign({}, s);
                ns.direction = s.direction === 'up' ? 'down' : 'up';
                ns.begin_price = -s.begin_price;
                ns.end_price = -s.end_price;
                ns.high = -s.low;
                ns.low = -s.high;
                return ns;
            });
            // 买卖点: is_buy取反, 价格取负, high↔low互换
            m.bsps = (data.bsps || []).map(function(b) {
                var nb = Object.assign({}, b);
                nb.is_buy = !b.is_buy;
                nb.price = -b.price;
                nb.high = -b.low;
                nb.low = -b.high;
                return nb;
            });
            // 中枢星标: price取负, mark G↔D, color red↔green
            m.zs_stars = (data.zs_stars || []).map(function(s) {
                var ns = Object.assign({}, s);
                ns.price = -s.price;
                ns.mark = s.mark === 'G' ? 'D' : 'G';
                ns.color = s.color === 'red' ? 'green' : 'red';
                return ns;
            });
            // 白色水平线: price取负
            if (data.white_hline) {
                m.white_hline = Object.assign({}, data.white_hline);
                m.white_hline.price = -data.white_hline.price;
            }
            return m;
        }

        // 反转模式下的价格显示：取绝对值
        function _fmtPrice(p) {
            return (_isMirrorMode ? Math.abs(p) : p).toFixed(2);
        }

        function priceToY(price, area, priceRange) {
            return area.y + area.h - (price - priceRange.min) / (priceRange.max - priceRange.min) * area.h;
        }

        function yToPrice(y, area, priceRange) {
            return priceRange.min + (area.y + area.h - y) / area.h * (priceRange.max - priceRange.min);
        }

        /**
         * 构建全局日期→全局索引映射（chartData.klines 级别）。
         * 所有需要通过日期查找K线索引的 draw 函数统一使用此映射，
         * 避免因视口滚动导致局部 klines 子数组中找不到日期而丢失绘制。
         */
        function buildGlobalDateMap() {
            const dateToGlobalIdx = {};
            chartData.klines.forEach((k, i) => { dateToGlobalIdx[k.date] = i; });
            return { dateToGlobalIdx };
        }

        /**
         * 通过日期查找全局索引。
         * @param {string} date - 日期字符串
         * @param {object} map - buildGlobalDateMap() 的返回值
         * @returns {number|undefined} 全局索引
         */
        function dateToGlobalIdx(date, map) {
            const result = map.dateToGlobalIdx[date];
            if (result === undefined && window._dualZsDebugCount === undefined) {
                window._dualZsDebugCount = 0;
            }
            if (result === undefined && window._dualZsDebugCount < 3) {
                console.log("[dateToGlobalIdx] 未匹配日期: '" + date + "', 可用日期样本: " + Object.keys(map.dateToGlobalIdx).slice(0, 3).join(", "));
                window._dualZsDebugCount++;
            }
            return result;
        }

        /**
         * 将全局索引转换为画布上的 X 坐标。
         * @param {number} globalIdx - 在 chartData.klines 中的全局索引
         * @param {number} globalStart - 当前视口起始的全局索引
         * @param {number} areaX - 图表区域左边界
         * @param {number} barStep - 每根K线的像素步长
         * @param {number} subPixelOffset - 亚像素偏移
         * @returns {number} 画布 X 坐标
         */
        function globalIdxToX(globalIdx, globalStart, areaX, barStep, subPixelOffset) {
            const localIdx = globalIdx - globalStart;
            return areaX + barStep * localIdx + barStep / 2 - subPixelOffset;
        }

        function render() {
            if (!chartData) return;
            if (isDualWindow) {
                renderTop(); // renderTop内部会调用updateDualHighlight -> renderBottom
            } else {
                renderSingle();
            }
        }

        function renderSingle() {
            if (!chartData || !ctx) return;
            canvas = topCanvas; ctx = topCtx;
            _renderChart(chartData, currentFreq, viewOffset, viewCount, mouseX, mouseY, null, null);
        }

        function renderTop() {
            if (!chartData || !topCtx) return;
            canvas = topCanvas; ctx = topCtx;
            updateActiveWindowClass();
            _renderChart(chartData, currentFreq, viewOffset, viewCount, mouseX, mouseY, null, null);
            // 上面窗口渲染完后，计算下面窗口高亮并重绘下面窗口
            // 注意：_renderChart 内部会临时覆盖全局变量然后恢复，
            // 所以这里全局变量已恢复为上面窗口的值，calcGrayRange 可以正确使用
            updateDualHighlight();
        }

        function renderBottom() {
            if (!dualBottomData || !bottomCtx) return;
            updateDualNewZs();  // 双窗口新模式：检查红框完整性，决定是否请求新中枢
            updateActiveWindowClass();
            const _savedCanvas = canvas, _savedCtx = ctx;
            canvas = bottomCanvas; ctx = bottomCtx;
            window._isRenderingBottom = true;  // 标记：下面窗口渲染中，drawCrosshair 不更新 OHLC
            _renderChart(dualBottomData, dualBottomFreq, dualBottomViewOffset, dualBottomViewCount, dualBottomMouseX, dualBottomMouseY, dualHighlightRange, dualRedRange);
            window._isRenderingBottom = false;
            canvas = _savedCanvas; ctx = _savedCtx;
        }

        function _renderChart(data, freq, vOffset, vCount, mX, mY, highlightRange, redRange) {
            if (!data || !ctx) return;
            // 反转视图模式：对数据做镜像变换（不修改原始缓存，仅影响渲染）
            if (_isMirrorMode) {
                data = _mirrorChartData(data);
            }
            // 临时覆盖全局变量供绘制函数使用
            const _savedViewOffset = viewOffset, _savedViewCount = viewCount;
            const _savedMouseX = mouseX, _savedMouseY = mouseY;
            const _savedCurrentFreq = currentFreq;
            const _savedChartData = chartData;
            viewOffset = vOffset; viewCount = vCount;
            mouseX = mX; mouseY = mY;
            currentFreq = freq;
            chartData = data;
            const w = canvas.clientWidth, h = canvas.clientHeight;
            ctx.fillStyle = COLORS.bg; ctx.fillRect(0, 0, w, h);
            const klines = getVisibleKlines();
            if (!klines.length) {
                viewOffset = _savedViewOffset; viewCount = _savedViewCount;
                mouseX = _savedMouseX; mouseY = _savedMouseY;
                currentFreq = _savedCurrentFreq;
                chartData = _savedChartData;
                return;
            }
            const area = getChartArea(), volArea = getVolArea();
            const macdTextArea = getMacdTextArea();
            const priceRange = getPriceRange(klines), macdRange = getMacdRange(klines);
            const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
            const barWidth = Math.max(1, (area.w / effectiveCount) * 0.7);
            const barStep = area.w / effectiveCount;
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            // 双窗口红框：笔外沿区间（分型左肩→右肩，跳过中间灰框部分）
            // 调试：记录到全局状态供侧边调试面板读取（保留 calcRedRange 之前设置的原因）
            var _prevReason = window._lastRedFrameStatus ? window._lastRedFrameStatus.reason : undefined;
            window._lastRedFrameStatus = { redRange: !!redRange, highlightRange: !!highlightRange, isVisible: highlightRange ? highlightRange.isVisible : null };
            if (_prevReason) window._lastRedFrameStatus.reason = _prevReason;
            updateRedFrameDebug();
            if (redRange && highlightRange && highlightRange.isVisible) {
                window._lastRedFrameStatus.state = "DRAW";
                window._lastRedFrameStatus.leftDate = redRange.leftDate || "";
                window._lastRedFrameStatus.rightDate = redRange.rightDate || "";
                const globalStart = Math.max(0, Math.floor(viewOffset));
                const rFill = "rgba(220, 50, 50, 0.12)";  // 与红中枢同色
                if (redRange.hasBefore) {
                    const bx1 = globalIdxToX(redRange.beforeStart, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                    const bx2 = globalIdxToX(redRange.beforeEnd, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                    ctx.fillStyle = rFill; ctx.fillRect(bx1, area.y, bx2 - bx1, area.h);
                    window._lastRedFrameStatus.beforeDrawn = true;
                    window._lastRedFrameStatus.beforeRect = [bx1.toFixed(0), bx2.toFixed(0)];
                }
                if (redRange.hasAfter) {
                    const ax1 = globalIdxToX(redRange.afterStart, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                    const ax2 = globalIdxToX(redRange.afterEnd, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                    ctx.fillStyle = rFill; ctx.fillRect(ax1, area.y, ax2 - ax1, area.h);
                    window._lastRedFrameStatus.afterDrawn = true;
                    window._lastRedFrameStatus.afterRect = [ax1.toFixed(0), ax2.toFixed(0)];
                }
                updateRedFrameDebug();
            } else {
                // 保留 calcRedRange 给出的原因（如果有），不覆盖
                if (!window._lastRedFrameStatus || !window._lastRedFrameStatus.reason) {
                    window._lastRedFrameStatus = window._lastRedFrameStatus || {};
                    window._lastRedFrameStatus.reason = "渲染跳过(redRange或visibility)";
                }
                window._lastRedFrameStatus.state = "SKIP";
                window._lastRedFrameStatus.redRange = !!redRange;
                window._lastRedFrameStatus.highlightRange = !!highlightRange;
                window._lastRedFrameStatus.isVisible = highlightRange ? highlightRange.isVisible : null;
                updateRedFrameDebug();
            }
            // 双窗口高亮：在绘制K线之前先画灰色背景
            let offscreenIndicator = null; // {isLeft, isRight} 用于最后画箭头
            let highlightCenterDate = null; // 灰框中间K线的日期
            if (highlightRange && highlightRange.startIdx !== undefined) {
                if (highlightRange.isVisible) {
                    const globalStart = Math.max(0, Math.floor(viewOffset));
                    const hStartX = globalIdxToX(highlightRange.startIdx, globalStart, area.x, barStep, subPixelOffset) - barStep / 2;
                    const hEndX = globalIdxToX(highlightRange.endIdx, globalStart, area.x, barStep, subPixelOffset) + barStep / 2;
                    ctx.fillStyle = "rgba(128, 128, 128, 0.35)";
                    ctx.fillRect(hStartX, area.y, hEndX - hStartX, area.h);
                    // 画灰框中间的白色纵线
                    const centerIdx = Math.round((highlightRange.startIdx + highlightRange.endIdx) / 2);
                    const centerKline = data.klines[centerIdx];
                    if (centerKline) {
                        highlightCenterDate = centerKline.date;
                        const centerX = globalIdxToX(centerIdx, globalStart, area.x, barStep, subPixelOffset);
                        ctx.strokeStyle = "rgba(255, 255, 255, 0.5)";
                        ctx.lineWidth = 1;
                        ctx.setLineDash([4, 3]);
                        ctx.beginPath();
                        ctx.moveTo(centerX, area.y);
                        ctx.lineTo(centerX, area.y + area.h);
                        ctx.stroke();
                        ctx.setLineDash([]);
                    }
                } else if (highlightRange.isLeft || highlightRange.isRight) {
                    offscreenIndicator = { isLeft: highlightRange.isLeft, isRight: highlightRange.isRight };
                }
            }
            drawGrid(area, priceRange);
            ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
            ctx.beginPath();
            ctx.moveTo(area.x + area.w, area.y);
            ctx.lineTo(area.x + area.w, volArea.y + volArea.h);
            ctx.stroke();
            ctx.beginPath();
            ctx.moveTo(area.x, area.y);
            ctx.lineTo(area.x, volArea.y + volArea.h);
            ctx.stroke();
            const klinesToDraw = klines.slice(0, viewCount);
            drawMacdLabel(macdTextArea, klinesToDraw, barStep, subPixelOffset);
            drawMacd(klinesToDraw, volArea, macdRange, barStep, barWidth / 2, subPixelOffset);
            // 区间选择高亮：绘制起点A的金色标记
            if (_rangeSelect.mode === 'SELECTED_A' && _rangeSelect.startFreq === currentFreq && chartData && _rangeSelect.startSymbol === chartData.meta.symbol) {
                const selIdx = _rangeSelect.startIdx;
                const globalStart = Math.max(0, Math.floor(viewOffset));
                const selX = globalIdxToX(selIdx, globalStart, area.x, barStep, subPixelOffset);
                if (selX >= area.x - barStep && selX <= area.x + area.w + barStep) {
                    const selX1 = selX - barStep / 2;
                    const selX2 = selX + barStep / 2;
                    ctx.fillStyle = "rgba(255, 215, 0, 0.22)";
                    ctx.fillRect(selX1, area.y, selX2 - selX1, area.h);
                    ctx.strokeStyle = "rgba(255, 215, 0, 0.7)";
                    ctx.lineWidth = 1.5;
                    ctx.strokeRect(selX1, area.y, selX2 - selX1, area.h);
                    // 顶部标签
                    const selK = data.klines[selIdx];
                    if (selK) {
                        const label = "A";
                        ctx.font = "bold 11px monospace";
                        ctx.fillStyle = "rgba(0,0,0,0.75)";
                        ctx.fillRect(selX - 8, area.y - 18, 16, 16);
                        ctx.fillStyle = "#FFD700";
                        ctx.textAlign = "center";
                        ctx.fillText(label, selX, area.y - 6);
                    }
                }
            }
            drawCandles(klinesToDraw, area, priceRange, barStep, barWidth, subPixelOffset);
            if (getShowMa()) {
                try { drawMaLines(klinesToDraw, area, priceRange, barStep, subPixelOffset); }
                catch (e) { console.error("[drawMaLines错误]", e); }
            }
            if (showBi) drawBiLines(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showFx) drawFxMarkers(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            // 双窗口新模式：红框出现后立即进入新中枢模式。
            // 请求返回前也先隐藏原中枢/线段/买卖点，避免红框出现后仍显示旧结构。
            const isBottomNewZs = (data === dualBottomData && dualShowNewZs);
            if (showZs && !isBottomNewZs) drawZs(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showSeg && !isBottomNewZs) drawSegLines(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (showBsp && !isBottomNewZs) drawBspMarkers(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            if (isBottomNewZs) drawDualNewZs(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawWhiteHLine(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawAnnotations(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            drawViewportHighLow(klinesToDraw, area, priceRange, barStep, subPixelOffset);
            _overlayData = null;
            drawCrosshair(klinesToDraw, area, priceRange, volArea, macdRange, barStep, macdTextArea, subPixelOffset);
            drawPriceAxis(area, priceRange); drawMacdAxis(volArea, macdRange);
            drawDateAxis(klinesToDraw, barStep, subPixelOffset);
            if (_overlayData) {
                if (_overlayData.rightPrice !== undefined) {
                    const labelW = 50;
                    ctx.fillStyle = "#dcdcdc"; ctx.fillRect(area.x + area.w + 2, _overlayData.rightY - 10, labelW, 20);
                    ctx.fillStyle = "#333"; ctx.font = "11px monospace"; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
                    ctx.fillText(_overlayData.rightPrice, area.x + area.w + 6, _overlayData.rightY + 4);
                }
                if (_overlayData.bottomText) {
                    const d = _overlayData;
                    ctx.fillStyle = "#dcdcdc";
                    ctx.fillRect(d.bottomX, d.bottomY, d.bottomW + d.bottomPad * 2, d.bottomH);
                    ctx.fillStyle = "#333"; ctx.textAlign = "left"; ctx.textBaseline = "alphabetic";
                    ctx.fillText(d.bottomText, d.bottomX + d.bottomPad, d.bottomY + 13);
                }
            }
            // 双窗口：在所有绘制完成后，画视口外指示箭头（确保不被覆盖）
            if (offscreenIndicator) {
                const arrowSize = 10;
                const arrowY = area.y + area.h / 2;
                ctx.fillStyle = "rgba(200, 200, 200, 0.6)";
                ctx.beginPath();
                if (offscreenIndicator.isLeft) {
                    ctx.moveTo(area.x + arrowSize + 4, arrowY - arrowSize);
                    ctx.lineTo(area.x + 4, arrowY);
                    ctx.lineTo(area.x + arrowSize + 4, arrowY + arrowSize);
                } else {
                    ctx.moveTo(area.x + area.w - arrowSize - 4, arrowY - arrowSize);
                    ctx.lineTo(area.x + area.w - 4, arrowY);
                    ctx.lineTo(area.x + area.w - arrowSize - 4, arrowY + arrowSize);
                }
                ctx.closePath();
                ctx.fill();
            }
            // 双窗口高亮：在灰框中间白线下方显示日期标签（同drawCrosshair完整信息）
            if (highlightCenterDate && highlightRange && highlightRange.isVisible) {
                const globalStart = Math.max(0, Math.floor(viewOffset));
                const centerIdx = Math.round((highlightRange.startIdx + highlightRange.endIdx) / 2);
                const centerX = globalIdxToX(centerIdx, globalStart, area.x, barStep, subPixelOffset);
                const centerKline = data.klines[centerIdx];
                if (centerKline) {
                    // 格式化日期
                    let shortDate;
                    if (freq === '15s') {
                        shortDate = getKlineEndTime(highlightCenterDate, true);
                    } else if (freq === '1m' || freq === '30m' || freq === '5m') {
                        shortDate = getKlineEndTime(highlightCenterDate);
                    } else if (freq === 'w') {
                        const dateParts = highlightCenterDate.split(/[-\/]/);
                        shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                    } else {
                        const dateParts = highlightCenterDate.split(/[-\/]/);
                        shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                    }
                    const d = new Date(highlightCenterDate.replace(/\//g, "-").replace(" ", "T"));
                    const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                    const weekDay = "周" + weekDays[d.getDay()];
                    // barsToRight: 从centerIdx到最右边可见K线
                    const rightGlobalIdx = globalStart + klines.length - 1;
                    const barsToRight = Math.max(1, rightGlobalIdx - centerIdx + 1);
                    // 涨跌幅: 从centerIdx到最右边可见K线
                    const prevKLine = centerIdx > 0 ? data.klines[centerIdx - 1] : null;
                    const startPrice = prevKLine ? prevKLine.close : centerKline.open;
                    const rightVisibleK = klines[klines.length - 1];
                    const totalChange = rightVisibleK.close - startPrice;
                    const totalChangePct = startPrice !== 0 ? (totalChange / startPrice * 100).toFixed(2) : "0.00";
                    const tcSign = totalChange >= 0 ? "+" : "";
                    const extraText = ` ${barsToRight}根 ${tcSign}${totalChange.toFixed(2)}(${tcSign}${totalChangePct}%)`;
                    const dateText = shortDate + " " + weekDay + extraText;
                    ctx.font = "11px monospace";
                    const textW = ctx.measureText(dateText).width;
                    const labelH = 18;
                    const labelPad = 4;
                    let labelX = centerX - textW / 2 - labelPad;
                    if (labelX < area.x) labelX = area.x;
                    if (labelX + textW + labelPad * 2 > area.x + area.w) labelX = area.x + area.w - textW - labelPad * 2;
                    const labelY = area.y + area.h - labelH;
                    ctx.fillStyle = "#dcdcdc";
                    ctx.fillRect(labelX, labelY, textW + labelPad * 2, labelH);
                    ctx.fillStyle = "#333"; ctx.textAlign = "left";
                    ctx.fillText(dateText, labelX + labelPad, labelY + 13);
                }
            }
            // 恢复全局变量
            viewOffset = _savedViewOffset; viewCount = _savedViewCount;
            mouseX = _savedMouseX; mouseY = _savedMouseY;
            currentFreq = _savedCurrentFreq;
            chartData = _savedChartData;
            // 只在主窗口（上面窗口或单窗口）更新统计
            if (data === _savedChartData || !isDualWindow) {
                generateStats();
            }
            // 始终更新slider（双窗口下根据激活窗口显示对应数据范围）
            updateSlider();
        }

        // 红框调试面板更新（不依赖console.log，即使F12过滤也能在页面上看到）
        function updateRedFrameDebug() {
            var dbg = document.getElementById("redframe-debug");
            if (!dbg || !isDualWindow) return;
            var st = window._lastRedFrameStatus;
            if (!st) return;
            dbg.style.display = "block";
            var stateEl = document.getElementById("rfdb-state");
            var detailEl = document.getElementById("rfdb-detail");
            // 显示灰色框状态
            var gs = window._lastGrayStatus;
            var grayInfo = "";
            if (gs && gs.startIdx !== undefined) {
                grayInfo = " 灰[" + gs.startIdx + "-" + gs.endIdx + (gs.isVisible ? "✓" : "✗") + "]";
            }
            if (st.state === "SKIP") {
                stateEl.textContent = "跳过";
                stateEl.style.color = "#ffa710";
                var extra = "";
                if (window._lastCalcRedRangeError) extra += " ERR:" + window._lastCalcRedRangeError;
                if (st.aDt) extra += " aDt=" + st.aDt;
                if (st.bDt) extra += " bDt=" + st.bDt;
                if (st.bottomFirst) extra += " btm1st=" + st.bottomFirst;
                if (st.bottomLast) extra += " btmLast=" + st.bottomLast;
                detailEl.textContent = (st.reason||"") + grayInfo + extra + " redRange=" + st.redRange + " hl=" + st.highlightRange + " vis=" + st.isVisible;
            } else if (st.state === "DRAW") {
                stateEl.textContent = "已绘制";
                stateEl.style.color = "#4caf50";
                // 格式化日期：30分钟数据去掉秒和前面多余的
                function fmtDate(d) {
                    if (!d) return "?";
                    if (d.length >= 16) return d.slice(5, 16);  // "MM-DD HH:MM"
                    return d.slice(5, 10);  // "MM-DD"
                }
                detailEl.textContent = "[" + fmtDate(st.leftDate) + ", " + fmtDate(st.rightDate) + "]";
            } else if (st.state === "OK") {
                stateEl.textContent = "计算OK";
                stateEl.style.color = "#2196f3";
                detailEl.textContent = "A/B[" + st.aIdx + "," + st.bIdx + "] 灰[" + st.grayStart + "," + st.grayEnd + "] before=" + st.before + " after=" + st.after + grayInfo;
            } else {
                stateEl.textContent = st.state || "--";
                stateEl.style.color = "#fff";
                detailEl.textContent = "";
            }
        }

        // 上面窗口鼠标移动时更新下面窗口高亮并重绘下面窗口
        function updateDualHighlight() {
            if (!isDualWindow || !dualBottomData) return;
            if (mouseX >= 0) {
                dualHighlightRange = calcGrayRange(mouseX);
                // 只有按住 Ctrl 键时才计算红框（耗资源操作），否则只显示灰框
                dualRedRange = (_ctrlPressed && dualHighlightRange) ? dualHighlightRange.redRange : null;
                // 更新状态A：区间是否在视口外
                dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                // 更新调试面板：显示灰框状态
                if (dualHighlightRange && dualHighlightRange.startIdx !== undefined) {
                    window._lastGrayStatus = {
                        startIdx: dualHighlightRange.startIdx,
                        endIdx: dualHighlightRange.endIdx,
                        isVisible: dualHighlightRange.isVisible,
                        redRange: !!dualRedRange
                    };
                } else {
                    window._lastGrayStatus = { noMatch: true };
                }
            }
            renderBottom();
        }

        // 双窗口新模式：红框出现后，请求用红框内笔计算新中枢
        function updateDualNewZs() {
            console.log("[updateDualNewZs] 进入: isDualWindow=" + isDualWindow + " dualBottomData=" + !!dualBottomData + " dualHighlightRange=" + !!dualHighlightRange);
            if (!isDualWindow || !dualBottomData || !dualHighlightRange) {
                console.log("[updateDualNewZs] 条件1失败: isDualWindow/dualBottomData/dualHighlightRange 为空");
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            const rr = dualHighlightRange.redRange;
            console.log("[updateDualNewZs] redRange=" + !!rr + " highlightRange keys=" + Object.keys(dualHighlightRange).join(","));
            if (!rr) {
                console.log("[updateDualNewZs] 条件2失败: redRange为空");
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            const aIdx = rr.aIdx;
            const bIdx = rr.bIdx;
            console.log("[updateDualNewZs] aIdx=" + aIdx + " bIdx=" + bIdx);
            if (aIdx === undefined || bIdx === undefined) {
                console.log("[updateDualNewZs] 条件3失败: aIdx或bIdx为undefined");
                return;
            }
            // 红框对应的灰框区间不在当前下面窗口视口内时，不切换新中枢
            console.log("[updateDualNewZs] isVisible=" + dualHighlightRange.isVisible);
            if (!dualHighlightRange.isVisible) {
                console.log("[updateDualNewZs] 条件4失败: 不在视口内");
                if (dualShowNewZs) {
                    dualShowNewZs = false;
                    dualNewZsData = null;
                }
                return;
            }
            // 红框左右边界时间（子级别K线格式，传给后端由 _find_sub_bi_sequence 找笔）
            const bottomKlines = dualBottomData.klines;
            const leftDate = bottomKlines[aIdx].date;
            const rightDate = bottomKlines[bIdx].date;
            console.log("[updateDualNewZs] leftDate=" + leftDate + " rightDate=" + rightDate + " freq=" + dualBottomFreq);
            const requestKey = dualBottomFreq + ":" + leftDate + ":" + rightDate;
            if (dualNewZsFailedKey === requestKey) {
                console.log("[updateDualNewZs] 条件5失败: 请求已失败过, key=" + requestKey);
                return;
            }
            if (dualShowNewZs && dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                console.log("[updateDualNewZs] 条件6: 已缓存相同请求, 跳过");
                return;
            }
            console.log("[updateDualNewZs] >>> 发送fetch请求到 /api/red_range_zs");
            dualNewZsLeftDate = leftDate;
            dualNewZsRightDate = rightDate;
            dualShowNewZs = true;
            dualNewZsData = null;
            const code = dualBottomData.meta.symbol;
            const isReplay = dualBottomData.meta && dualBottomData.meta.is_replay;
            let url = "/api/red_range_zs?code=" + encodeURIComponent(code) + "&freq=" + dualBottomFreq + "&left_date=" + encodeURIComponent(leftDate) + "&right_date=" + encodeURIComponent(rightDate);
            if (isReplay) {
                const endDate = document.getElementById("goto-date-input").value;
                url += "&end_date=" + encodeURIComponent(endDate);
            }
            fetch(url)
                .then(resp => resp.json())
                .then(data => {
                    if (data.error) {
                        console.error("[dual_zs] 后端错误:", data.error);
                        if (dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                            dualNewZsFailedKey = requestKey;
                            dualShowNewZs = false;
                            dualNewZsData = null;
                            renderBottom();
                        }
                        return;
                    }
                    if (dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                        dualNewZsFailedKey = "";
                        dualNewZsData = data;
                        dualShowNewZs = true;
                        renderBottom();
                    }
                })
                .catch(err => {
                    console.error("[dual_zs] 请求失败:", err);
                    if (dualNewZsLeftDate === leftDate && dualNewZsRightDate === rightDate) {
                        dualNewZsFailedKey = requestKey;
                        dualShowNewZs = false;
                        dualNewZsData = null;
                        renderBottom();
                    }
                });
        }

        function drawGrid(area, range) {
            ctx.strokeStyle = COLORS.grid; ctx.lineWidth = 1;
            for (let i = 0; i <= 5; i++) {
                const y = area.y + (area.h / 5) * i;
                ctx.beginPath(); ctx.moveTo(area.x, y); ctx.lineTo(area.x + area.w, y); ctx.stroke();
            }
        }

        function drawCandles(klines, area, priceRange, barStep, barWidth, subPixelOffset) {
            klines.forEach((k, i) => {
                const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                const openY = priceToY(k.open, area, priceRange);
                const closeY = priceToY(k.close, area, priceRange);
                const highY = priceToY(k.high, area, priceRange);
                const lowY = priceToY(k.low, area, priceRange);
                const bodyTop = Math.min(openY, closeY);
                const bodyH = Math.max(1, Math.abs(closeY - openY));

                if (k.close === k.open) {
                    // 收盘价等于开盘价，画十字线（竖线+横线，宽度一致）
                    ctx.fillStyle = "#FFFFFF";
                    ctx.fillRect(x - 0.5, highY, 1, lowY - highY);          // 竖线：上影线到下影线
                    ctx.fillRect(x - barWidth / 2, closeY - 0.5, barWidth, 1); // 横线：在收盘价位置，与竖线同宽
                } else if (k.close > k.open) {
                    ctx.fillStyle = "#FF4444";
                    if (highY < bodyTop) {
                        ctx.fillRect(x - 0.5, highY, 1, bodyTop - highY);
                    }
                    if (bodyTop + bodyH < lowY) {
                        ctx.fillRect(x - 0.5, bodyTop + bodyH, 1, lowY - bodyTop - bodyH);
                    }
                    ctx.strokeStyle = "#FF4444"; ctx.lineWidth = 1;
                    ctx.strokeRect(x - barWidth / 2, bodyTop, barWidth, bodyH);
                } else {
                    ctx.fillStyle = "#54fcfc";
                    ctx.fillRect(x - 0.5, highY, 1, lowY - highY);
                    ctx.fillRect(x - barWidth / 2, bodyTop, barWidth, bodyH);
                    ctx.fillRect(x - 0.5, bodyTop + bodyH, 1, lowY - bodyTop - bodyH);
                }
            });
        }

        function drawMacd(klines, macdArea, macdRange, barStep, barWidth, subPixelOffset) {
            const zeroY = macdArea.y + macdArea.h * (macdRange.max / (macdRange.max - macdRange.min));
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const isUp = k.macd >= 0;
                ctx.fillStyle = isUp ? COLORS.macdUp : COLORS.macdDown;
                const macdH = Math.abs(k.macd) / (macdRange.max - macdRange.min) * macdArea.h;
                const y = k.macd >= 0 ? zeroY - macdH : zeroY;
                ctx.fillRect(x - barWidth / 2, y, barWidth, macdH);
            });
            ctx.strokeStyle = COLORS.dif; ctx.lineWidth = 1;
            ctx.beginPath();
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const y = macdArea.y + macdArea.h - (k.dif - macdRange.min) / (macdRange.max - macdRange.min) * macdArea.h;
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.strokeStyle = COLORS.dea; ctx.lineWidth = 1;
            ctx.beginPath();
            klines.forEach((k, i) => {
                const x = macdArea.x + barStep * i + barStep / 2 - subPixelOffset;
                const y = macdArea.y + macdArea.h - (k.dea - macdRange.min) / (macdRange.max - macdRange.min) * macdArea.h;
                if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
            });
            ctx.stroke();
            ctx.strokeStyle = "rgba(255,255,255,0.2)"; ctx.lineWidth = 1;
            ctx.beginPath(); ctx.moveTo(macdArea.x, zeroY); ctx.lineTo(macdArea.x + macdArea.w, zeroY); ctx.stroke();
        }

        function drawBiLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.bis.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            ctx.lineWidth = 1;
            chartData.bis.forEach(bi => {
                let s = dateToGlobalIdx(bi.sdt, map), e = dateToGlobalIdx(bi.edt, map);
                if (s === undefined || e === undefined) return;
                // 笔的两端都必须在视口内才显示（确保成笔条件在当前视口内成立）
                if (s < globalStart || s >= globalEnd || e < globalStart || e >= globalEnd) return;
                let x1 = globalIdxToX(s, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(e, globalStart, area.x, barStep, subPixelOffset);
                // 裁剪到图表主区域内
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(bi.fx_a_price, area, priceRange);
                const y2 = priceToY(bi.fx_b_price, area, priceRange);
                // 未确定的笔用虚线绘制，确定的笔用实线
                // 原值: 确定笔="#FFFFFF", 未确定笔="rgba(255, 255, 255, 0.4)"
                if (bi.is_sure === false) {
                    ctx.strokeStyle = "rgba(253, 221, 96, 0.4)";
                    ctx.setLineDash([4, 4]);
                } else {
                    ctx.strokeStyle = "#fddd60";
                    ctx.setLineDash([]);
                }
                ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
            });
            ctx.setLineDash([]);
        }

        function drawMacdLabel(textArea, klines, barStep, subPixelOffset) {
            let targetK = null;
            if (mouseX >= textArea.x && mouseX <= textArea.x + textArea.w) {
                const idx = Math.floor((mouseX - textArea.x + subPixelOffset) / barStep);
                targetK = klines[Math.min(idx, klines.length - 1)];
            }
            if (!targetK) targetK = klines[klines.length - 1];
            if (targetK) {
                ctx.font = "11px monospace"; ctx.textAlign = "left";
                const lineY = textArea.y + 11;
                ctx.fillStyle = COLORS.textLight;
                ctx.fillText("MACD(12,26,9)", textArea.x + 4, lineY);
                let xPos = textArea.x + 4 + ctx.measureText("MACD(12,26,9) ").width;
                ctx.fillStyle = COLORS.dif;
                ctx.fillText("DIF:" + targetK.dif.toFixed(2), xPos, lineY);
                xPos += ctx.measureText("DIF:" + targetK.dif.toFixed(2) + " ").width;
                ctx.fillStyle = COLORS.dea;
                ctx.fillText("DEA:" + targetK.dea.toFixed(2), xPos, lineY);
                xPos += ctx.measureText("DEA:" + targetK.dea.toFixed(2) + " ").width;
                ctx.fillStyle = targetK.macd >= 0 ? "#FF4444" : "#00DD00";
                ctx.fillText("BAR:" + targetK.macd.toFixed(2), xPos, lineY);
            }
        }

        function drawFxMarkers(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.fxs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            let fxNum = 0;
            ctx.font = "10px monospace"; ctx.textAlign = "center";
            chartData.fxs.forEach(fx => {
                let idx = dateToGlobalIdx(fx.date, map);
                if (idx === undefined) return;
                if (idx < globalStart || idx >= globalEnd) return;
                fxNum++;
                const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
                const y = priceToY(fx.price, area, priceRange);
                const isTop = fx.mark === "G";
                const color = isTop ? COLORS.up : COLORS.down;
                ctx.fillStyle = color;
                ctx.fillText(String(fxNum), x, isTop ? y - 4 : y + 10);
            });
        }

        function drawZs(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.zs || !chartData.zs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;

            chartData.zs.forEach(zs => {
                let sIdx = dateToGlobalIdx(zs.sdt, map);
                let eIdx = zs.confirm_edt ? dateToGlobalIdx(zs.confirm_edt, map) : undefined;
                if (sIdx === undefined) return;
                if (eIdx === undefined) {
                    // 未确认结束的中枢延伸到当前数据最后一根K线，而不是使用 zs.end/edt 过早收口
                    eIdx = chartData.klines.length - 1;
                }
                // 只绘制与当前视口有交集的中枢
                if (eIdx < globalStart || sIdx >= globalEnd) return;

                // 右边框使用后端给出的“中枢结束事实被确认”的时点；未确认则延伸到最新K线
                let finalEndIdx = eIdx;

                let x1 = globalIdxToX(sIdx, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(finalEndIdx, globalStart, area.x, barStep, subPixelOffset);
                // 裁剪到图表主区域内
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(zs.zg, area, priceRange);
                const y2 = priceToY(zs.zd, area, priceRange);

                const isUp = zs.dir === "up";
                const fillColor = isUp ? "rgba(220, 50, 50, 0.10)" : "rgba(50, 180, 50, 0.10)";
                const strokeColor = isUp ? "rgba(220, 50, 50, 0.6)" : "rgba(50, 180, 50, 0.6)";
                const textColor = isUp ? "rgba(220, 50, 50, 0.8)" : "rgba(50, 180, 50, 0.8)";

                ctx.fillStyle = fillColor;
                ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
                ctx.setLineDash([]);

                ctx.font = "10px monospace";
                ctx.fillStyle = textColor;
                ctx.textAlign = "right";
                ctx.fillText(_fmtPrice(zs.zg), x1 - 2, y1 - 2);
                ctx.fillText(_fmtPrice(zs.zd), x1 - 2, y2 + 10);
                // 中枢高度，标在上下沿中间位置
                const zsHeight = zs.zg - zs.zd;
                ctx.fillText(_fmtPrice(zsHeight), x1 - 2, (y1 + y2) / 2 + 3);
            });

            // 进入段和离开段红绿五角星 - 已注释掉
            // if (!chartData.zs_stars || !chartData.zs_stars.length) return;
            // chartData.zs_stars.forEach(star => {
            //     let idx = dateToGlobalIdx(star.date, map);
            //     if (idx === undefined) return;
            //     if (idx < globalStart || idx >= globalEnd) return;
            //     const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
            //     const y = priceToY(star.price, area, priceRange);
            //     const isTop = star.mark === "G";
            //     const starY = isTop ? y - 16 : y + 22;
            //     drawStar(ctx, x, starY, 5, 6, 3, star.color);
            // });
        }

        function drawStar(ctx, cx, cy, spikes, outerR, innerR, color) {
            ctx.fillStyle = color;
            ctx.beginPath();
            for (let i = 0; i < spikes * 2; i++) {
                const r = i % 2 === 0 ? outerR : innerR;
                const angle = (Math.PI / spikes) * i - Math.PI / 2;
                const x = cx + r * Math.cos(angle);
                const y = cy + r * Math.sin(angle);
                if (i === 0) ctx.moveTo(x, y);
                else ctx.lineTo(x, y);
            }
            ctx.closePath();
            ctx.fill();
        }

        // 画线段（与笔同粗细，区分方向颜色）
        function drawSegLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.segs || !chartData.segs.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            ctx.lineWidth = 1; ctx.setLineDash([]);
            chartData.segs.forEach(seg => {
                let s = dateToGlobalIdx(seg.sdt, map), e = dateToGlobalIdx(seg.edt, map);
                if (s === undefined || e === undefined) return;
                // 只绘制与当前视口有交集的线段
                if (e < globalStart || s >= globalEnd) return;
                let x1 = globalIdxToX(s, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(e, globalStart, area.x, barStep, subPixelOffset);
                // 裁剪到图表主区域内
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(seg.begin_price, area, priceRange);
                const y2 = priceToY(seg.end_price, area, priceRange);
                ctx.strokeStyle = "#ffa710"; // 原值: up="#FF6666", down="#66FF66"
                ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(x2, y2); ctx.stroke();
            });
        }

        // 画买卖点标记（买点▲红色，卖点▼绿色——绿色与MACD绿柱子同色）
        // 标记画在K线外侧：买点在最低价下方，卖点在最高价上方，与分型标号/五角星错开
        function drawBspMarkers(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.bsps || !chartData.bsps.length) return;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            chartData.bsps.forEach(bsp => {
                let idx = dateToGlobalIdx(bsp.date, map);
                if (idx === undefined) return;
                if (idx < globalStart || idx >= globalEnd) return;
                if (bspFilter && !bspFilter[bsp.type]) return;  // 按用户设置过滤类型
                const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
                const isBuy = bsp.is_buy;
                // 用K线外侧价格定位：买点用low，卖点用high
                const anchorPrice = isBuy ? bsp.low : bsp.high;
                const y = priceToY(anchorPrice, area, priceRange);
                const color = isBuy ? COLORS.up : COLORS.down;
                ctx.fillStyle = color;
                ctx.textAlign = "center";
                ctx.textBaseline = "middle";
                ctx.font = "bold 14px monospace";
                // 错开偏移：买点往下放（远离五角星/分型），卖点往上放
                const markerY = isBuy ? y + 22 : y - 22;
                ctx.fillText(isBuy ? "▲" : "▼", x, markerY);
                // 买卖点类型标签再往外错开一点（与三角形同色，fillStyle已设置）
                ctx.font = "11px sans-serif";
                const labelY = isBuy ? markerY + 18 : markerY - 18;
                ctx.fillText(bsp.type, x, labelY);
                ctx.textBaseline = "alphabetic";
            });
        }

        // 双窗口新模式：绘制红框内笔计算的新中枢（替代原中枢/线段/买卖点）
        function drawDualNewZs(klines, area, priceRange, barStep, subPixelOffset) {
            if (!dualNewZsData || !dualNewZsData.zs || !dualNewZsData.zs.length) {
                console.log("[drawDualNewZs] 跳过: dualNewZsData=", dualNewZsData);
                return;
            }
            const map = buildGlobalDateMap();
            console.log("[drawDualNewZs] ZS数量=" + dualNewZsData.zs.length + ", start_bi=" + dualNewZsData.start_bi + ", end_bi=" + dualNewZsData.end_bi);
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;

            dualNewZsData.zs.forEach((zs, zsIdx) => {
                let sIdx = dateToGlobalIdx(zs.sdt, map);
                if (sIdx === undefined) return;
                let eIdx = undefined;
                if (zs.confirm_edt) {
                    // 中枢被确认的笔打破 → 右边界在打破笔的末端
                    eIdx = dateToGlobalIdx(zs.confirm_edt, map);
                }
                if (eIdx === undefined) {
                    // 未被确认打破 → 用 edt（最后重叠笔的末端）
                    eIdx = zs.edt ? dateToGlobalIdx(zs.edt, map) : undefined;
                }
                if (eIdx === undefined) {
                    eIdx = dualBottomData.klines.length - 1;
                }
                // 最后一个中枢，未被确认打破 → 延伸到红框右边界
                if (zsIdx === dualNewZsData.zs.length - 1 && !zs.confirm_edt && dualRedRange && dualRedRange.bIdx !== undefined) {
                    if (dualRedRange.bIdx > eIdx) {
                        eIdx = dualRedRange.bIdx;
                    }
                }
                if (eIdx < globalStart || sIdx >= globalEnd) return;
                let finalEndIdx = eIdx;
                let x1 = globalIdxToX(sIdx, globalStart, area.x, barStep, subPixelOffset);
                let x2 = globalIdxToX(finalEndIdx, globalStart, area.x, barStep, subPixelOffset);
                if (x2 < area.x || x1 > rightBound) return;
                x1 = Math.max(area.x, x1);
                x2 = Math.min(rightBound, x2);
                const y1 = priceToY(zs.zg, area, priceRange);
                const y2 = priceToY(zs.zd, area, priceRange);

                const isUp = zs.dir === "up";
                // 新中枢使用更醒目的颜色区分
                const fillColor = isUp ? "rgba(220, 50, 50, 0.10)" : "rgba(50, 180, 50, 0.10)";
                const strokeColor = isUp ? "rgba(255, 80, 80, 0.85)" : "rgba(80, 255, 80, 0.85)";
                const textColor = isUp ? "rgba(220, 50, 50, 0.8)" : "rgba(50, 180, 50, 0.8)";

                ctx.fillStyle = fillColor;
                ctx.fillRect(x1, y1, x2 - x1, y2 - y1);
                ctx.strokeStyle = strokeColor;
                ctx.lineWidth = 1;
                ctx.setLineDash([4, 3]);
                ctx.strokeRect(x1, y1, x2 - x1, y2 - y1);
                ctx.setLineDash([]);

                ctx.font = "10px monospace";
                ctx.fillStyle = textColor;
                ctx.textAlign = "right";
                ctx.fillText(_fmtPrice(zs.zg), x1 - 2, y1 - 2);
                ctx.fillText(_fmtPrice(zs.zd), x1 - 2, y2 + 10);
                // 中枢高度，标在上下沿中间位置
                const zsHeight = zs.zg - zs.zd;
                ctx.fillText(_fmtPrice(zsHeight), x1 - 2, (y1 + y2) / 2 + 3);
            });

            // 进入段和离开段红绿五角星 - 已注释掉
            // if (!dualNewZsData.zs_stars || !dualNewZsData.zs_stars.length) return;
            // dualNewZsData.zs_stars.forEach(star => {
            //     let idx = dateToGlobalIdx(star.date, map);
            //     if (idx === undefined) return;
            //     if (idx < globalStart || idx >= globalEnd) return;
            //     const x = globalIdxToX(idx, globalStart, area.x, barStep, subPixelOffset);
            //     const y = priceToY(star.price, area, priceRange);
            //     const isTop = star.mark === "G";
            //     const starY = isTop ? y - 16 : y + 22;
            //     drawStar(ctx, x, starY, 5, 6, 3, star.color);
            // });
        }

        function drawMaLines(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || klines.length < 2) return;
            const start = Math.max(0, Math.floor(viewOffset));
            const allKlines = chartData.klines;
            const n = allKlines.length;
            // 收集已选中的均线周期（按周期升序，短周期画在上层）
            const periods = [];
            for (var p in maPeriods) {
                if (maPeriods[p]) periods.push(parseInt(p, 10));
            }
            periods.sort(function(a, b) { return a - b; });
            if (periods.length === 0) return;
            ctx.lineWidth = 1;
            for (let pi = 0; pi < periods.length; pi++) {
                const period = periods[pi];
                if (period <= 0 || period > n) continue;
                // 滑动窗口计算该周期均线
                const ma = new Array(n).fill(null);
                let sum = 0;
                for (let i = 0; i < n; i++) {
                    sum += allKlines[i].close;
                    if (i >= period) sum -= allKlines[i - period].close;
                    if (i >= period - 1) ma[i] = sum / period;
                }
                ctx.strokeStyle = MA_COLORS[period] || "#FFFFFF";
                ctx.beginPath();
                let started = false;
                for (let i = 0; i < klines.length; i++) {
                    const globalIdx = start + i;
                    if (globalIdx < n && ma[globalIdx] !== null && !isNaN(ma[globalIdx])) {
                        const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                        const y = priceToY(ma[globalIdx], area, priceRange);
                        if (!started) { ctx.moveTo(x, y); started = true; }
                        else ctx.lineTo(x, y);
                    }
                }
                ctx.stroke();
            }
        }

        // 画最新笔的白色横虚线（同一时间只有一根）
        function drawWhiteHLine(klines, area, priceRange, barStep, subPixelOffset) {
            if (!chartData || !chartData.white_hline) return;
            const hline = chartData.white_hline;
            const map = buildGlobalDateMap();
            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalEnd = globalStart + viewCount;
            const rightBound = area.x + area.w;
            // 找到起始日期对应的全局索引
            let startIdx = dateToGlobalIdx(hline.start_date, map);
            if (startIdx === undefined) return;
            // 如果起始点在视口右边之外，不绘制
            if (startIdx >= globalEnd) return;
            // 计算起始X坐标（如果起始点在视口左边之外，则从area.x开始）
            let x1;
            if (startIdx < globalStart) {
                x1 = area.x;
            } else {
                x1 = globalIdxToX(startIdx, globalStart, area.x, barStep, subPixelOffset);
            }
            // 向右延伸到页面最右边
            const x2 = rightBound;
            const y = priceToY(hline.price, area, priceRange);
            // 白色横虚线
            ctx.strokeStyle = "#FFFFFF";
            ctx.lineWidth = 1;
            ctx.setLineDash([4, 3]);
            ctx.beginPath();
            ctx.moveTo(x1, y);
            ctx.lineTo(x2, y);
            ctx.stroke();
            ctx.setLineDash([]);
            // 在右端显示价格标签
            ctx.fillStyle = "#FFFFFF";
            ctx.font = "11px monospace";
            ctx.textAlign = "left";
            ctx.fillText(_fmtPrice(hline.price), x2 + 4, y + 4);
        }

        /**
         * 同花顺风格：在视口内标注最高价和最低价的极值K线
         * - 数值和箭头纯白色 #FFFFFF
         * - 高点：下边沿贴合极值线，数值 ↘；低点：上边沿贴合极值线，数值 ↗
         * - 左侧空间不足时：高点 ↙ 数值，低点 ↖ 数值（数值显示在右侧）
         */
        function drawViewportHighLow(klines, area, priceRange, barStep, subPixelOffset) {
            if (!klines.length) return;

            // 找到视口内最高价和最低价的K线
            let maxHigh = -Infinity, minLow = Infinity;
            let maxHighIdx = -1, minLowIdx = -1;

            for (let i = 0; i < klines.length; i++) {
                const k = klines[i];
                if (k.high > maxHigh) { maxHigh = k.high; maxHighIdx = i; }
                if (k.low < minLow) { minLow = k.low; minLowIdx = i; }
            }

            if (maxHighIdx === -1 || minLowIdx === -1) return;

            const gap = 4; // 数值与箭头间距

            ctx.font = "11px monospace";
            ctx.fillStyle = "#FFFFFF";

            const arrowR = "\u2192"; // →  用于计算箭头宽度（所有箭头等宽）
            const arrowW = ctx.measureText(arrowR).width;

            /**
             * 绘制单个极值标注
             * @param {number} price - 极值价格
             * @param {number} klineIdx - K线在视口内的索引
             * @param {boolean} isHigh - 是否为高点（true=下边沿贴合, false=上边沿贴合）
             */
            function drawOne(price, klineIdx, isHigh) {
                const kx = area.x + barStep * klineIdx + barStep / 2 - subPixelOffset;
                const ky = priceToY(price, area, priceRange);
                const text = _fmtPrice(price);
                const textW = ctx.measureText(text).width;

                const needLeft = textW + gap + arrowW;
                const canLeft = (kx - needLeft) >= area.x;

                const textHeight = 11 * 1.2;  // fontSize * 行高系数
                const a = isHigh ? (canLeft ? "\u2198" : "\u2199") : (canLeft ? "\u2197" : "\u2196");

                ctx.textAlign = "left";

                if (canLeft) {
                    // 数值 ↘(高) / ↗(低)
                    const arrowX = kx - arrowW;
                    const textX = arrowX - gap - textW;
                    // 箭头：保持原位置（高点bottom贴合ky，低点top贴合ky）
                    ctx.textBaseline = isHigh ? "bottom" : "top";
                    ctx.fillText(a, textX + textW + gap, ky);
                    // 数值：中间对齐箭头尾端，高点往上移，低点往下移
                    ctx.textBaseline = "middle";
                    ctx.fillText(text, textX, isHigh ? ky - textHeight / 2 : ky + textHeight / 2);
                } else {
                    // ↙(高) / ↖(低) 数值
                    const arrowX = kx;
                    const textX = arrowX + arrowW + gap;
                    if (textX + textW > area.x + area.w) return;
                    // 箭头：保持原位置
                    ctx.textBaseline = isHigh ? "bottom" : "top";
                    ctx.fillText(a, arrowX, ky);
                    // 数值：中间对齐箭头尾端
                    ctx.textBaseline = "middle";
                    ctx.fillText(text, textX, isHigh ? ky - textHeight / 2 : ky + textHeight / 2);
                }
            }

            drawOne(maxHigh, maxHighIdx, true);   // 高点：下边沿贴合
            drawOne(minLow, minLowIdx, false);    // 低点：上边沿贴合
        }

        function drawCrosshair(klines, area, priceRange, volArea, volRange, barStep, macdTextArea, subPixelOffset) {
            let idx, k, cx;
            if (mouseX < area.x || mouseX > area.x + area.w) {
                idx = klines.length - 1;
                k = klines[idx];
                if (!k) return;
                cx = area.x + barStep * idx + barStep / 2 - subPixelOffset;
                // K线不足一屏时右对齐
                if (currentFreq === 'w') {
                    cx = area.x + area.w - barStep / 2;
                }
            } else {
                idx = Math.floor((mouseX - area.x + subPixelOffset) / barStep);
                k = klines[Math.min(idx, klines.length - 1)];
                if (!k) return;
                cx = area.x + barStep * idx + barStep / 2 - subPixelOffset;
                const crosshairEndY = volArea.y + volArea.h;
                ctx.strokeStyle = COLORS.crosshair; ctx.lineWidth = 1; ctx.setLineDash([4, 4]);
                ctx.beginPath(); ctx.moveTo(cx, area.y); ctx.lineTo(cx, crosshairEndY); ctx.stroke();
                if (mouseY >= area.y && mouseY <= crosshairEndY) {
                    ctx.beginPath(); ctx.moveTo(area.x, mouseY); ctx.lineTo(area.x + area.w, mouseY); ctx.stroke();
                }
                ctx.setLineDash([]);
                if (mouseY >= area.y && mouseY <= area.y + area.h) {
                    const price = yToPrice(mouseY, area, priceRange);
                    _overlayData = _overlayData || {};
                    _overlayData.rightPrice = _fmtPrice(price);
                    _overlayData.rightY = mouseY;
                }
            }

            const globalStart = Math.max(0, Math.floor(viewOffset));
            const globalIdx = globalStart + idx;
            if (!window._isRenderingBottom) {
                _currentGlobalIdx = globalIdx;
            }
            const prevK = globalIdx > 0 ? chartData.klines[globalIdx - 1] : null;
            const prevClose = prevK ? prevK.close : k.open;
            const changeVal = k.close - prevClose;
            const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
            const cls = changeVal >= 0 ? "up" : "down";
            const sign = changeVal >= 0 ? "+" : "";

            if (mouseX >= area.x && mouseX <= area.x + area.w) {
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                let shortDate;
                if (currentFreq === '15s') {
                    shortDate = getKlineEndTime(k.date, true);  // 含秒
                } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m') {
                    shortDate = getKlineEndTime(k.date);
                } else if (currentFreq === 'w') {
                    const dateParts = k.date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                } else {
                    const dateParts = k.date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                }
                const d = new Date(k.date.replace(/\//g, "-").replace(" ", "T"));
                const weekDay = "周" + weekDays[d.getDay()];

                const rightVisibleK = klines[klines.length - 1];
                const rightGlobalIdx = globalStart + klines.length - 1;
                const barsToRight = Math.max(1, rightGlobalIdx - globalIdx + 1);
                const prevKLine = globalIdx > 0 ? chartData.klines[globalIdx - 1] : null;
                const startPrice = prevKLine ? prevKLine.close : k.open;
                const totalChange = rightVisibleK.close - startPrice;
                const totalChangePct = startPrice !== 0 ? (totalChange / startPrice * 100).toFixed(2) : "0.00";
                const tcSign = totalChange >= 0 ? "+" : "";

                const extraText = ` ${barsToRight}根 ${tcSign}${totalChange.toFixed(2)}(${tcSign}${totalChangePct}%)`;
                const dateText = shortDate + " " + weekDay + extraText;

                ctx.font = "11px monospace";
                const textW = ctx.measureText(dateText).width;
                const labelH = 18;
                const labelPad = 4;
                let labelX = cx - textW / 2 - labelPad;
                if (labelX < area.x) labelX = area.x;
                if (labelX + textW + labelPad * 2 > area.x + area.w) labelX = area.x + area.w - textW - labelPad * 2;
                const labelY = area.y + area.h - labelH;
                _overlayData = _overlayData || {};
                _overlayData.bottomText = dateText;
                _overlayData.bottomX = labelX;
                _overlayData.bottomY = labelY;
                _overlayData.bottomW = textW;
                _overlayData.bottomH = labelH;
                _overlayData.bottomPad = labelPad;
            }

            // 双窗口：下面窗口渲染时，仅当鼠标不在下面窗口上（mouseX<0）才跳过 OHLC 更新
            // 避免"鼠标在上面窗口时，下面窗口的最后一根K线数据覆盖上面窗口的 OHLC"
            if (!(window._isRenderingBottom && mouseX < 0)) {
                // 反转模式下显示原始价格：open/close取绝对值，high/low互换后取绝对值
                const dispOpen = _isMirrorMode ? Math.abs(k.open) : k.open;
                const dispHigh = _isMirrorMode ? Math.abs(k.low) : k.high;   // k.low=-orig_high → |k.low|=orig_high
                const dispLow = _isMirrorMode ? Math.abs(k.high) : k.low;    // k.high=-orig_low → |k.high|=orig_low
                const dispClose = _isMirrorMode ? Math.abs(k.close) : k.close;
                document.getElementById("crosshair-info").innerHTML =
                    `<span class="label">开:</span> <span class="${cls}">${dispOpen.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">高:</span> <span class="${cls}">${dispHigh.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">低:</span> <span class="${cls}">${dispLow.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">收:</span> <span class="${cls}">${dispClose.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">涨跌:</span> <span class="${cls}">${sign}${changeVal.toFixed(2)}</span> &nbsp; ` +
                    `<span class="label">涨幅:</span> <span class="${cls}">${sign}${changePct}%</span> &nbsp; ` +
                    `<span class="label">复权:</span> <span class="label">${chartData.meta.forward_adjust ? "前复权" : "不复权"}</span>`;
            }

            const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
            const weekDayStr = "周" + weekDays[new Date(k.date.replace(/\//g, "-").replace(" ", "T")).getDay()];
            // 剪贴板文本同样显示原始价格
            const clipOpen = _isMirrorMode ? Math.abs(k.open) : k.open;
            const clipHigh = _isMirrorMode ? Math.abs(k.low) : k.high;
            const clipLow = _isMirrorMode ? Math.abs(k.high) : k.low;
            const clipClose = _isMirrorMode ? Math.abs(k.close) : k.close;
            const clipText = `${k.date} ${weekDayStr} 开:${clipOpen.toFixed(2)} 高:${clipHigh.toFixed(2)} 低:${clipLow.toFixed(2)} 收:${clipClose.toFixed(2)} 涨跌:${sign}${changeVal.toFixed(2)} 涨幅:${sign}${changePct}% 复权:${chartData.meta.forward_adjust ? "前复权" : "不复权"}`;
            if (window._isRenderingBottom) {
                // 底部窗口：记录底部窗口的全局索引和剪贴板文本
                _bottomCurrentGlobalIdx = globalIdx;
                _bottomClipText = clipText;
            } else {
                // 上面窗口
                _currentGlobalIdx = globalIdx;
                _currentClipText = clipText;
            }
        }

        function drawPriceAxis(area, priceRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
            for (let i = 0; i <= 5; i++) {
                const price = priceRange.min + (priceRange.max - priceRange.min) * (1 - i / 5);
                const y = area.y + (area.h / 5) * i;
                ctx.fillText(_fmtPrice(price), area.x + area.w + 6, y + 4);
            }
        }

        function drawMacdAxis(macdArea, macdRange) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace"; ctx.textBaseline = "alphabetic"; ctx.textAlign = "left";
            ctx.fillText(macdRange.max.toFixed(2), macdArea.x + macdArea.w + 6, macdArea.y + 12);
            const zeroY = macdArea.y + macdArea.h * (macdRange.max / (macdRange.max - macdRange.min));
            ctx.fillText("0", macdArea.x + macdArea.w + 6, zeroY + 4);
            ctx.fillText(macdRange.min.toFixed(2), macdArea.x + macdArea.w + 6, macdArea.y + macdArea.h - 4);
        }

        function drawDateAxis(klines, barStep, subPixelOffset) {
            ctx.fillStyle = COLORS.text; ctx.font = "11px monospace";
            const area = getChartArea(), volArea = getVolArea();
            const dateY = volArea.y + volArea.h + 28;
            // 用 measureText 动态计算日期标签实际宽度，避免缩放时写死像素值导致重叠
            let sampleDate;
            if (currentFreq === '15s') {
                sampleDate = getKlineEndTime(klines[0].date, true);
            } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m' || currentFreq === '60m') {
                sampleDate = getKlineEndTime(klines[0].date);
            } else {
                const dateParts = klines[0].date.split(/[-\/]/);
                sampleDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
            }
            const textWidth = ctx.measureText(sampleDate).width;
            const minPixelGap = textWidth + 10;  // 文本宽度 + 10px 间距
            const interval = Math.max(1, Math.ceil(minPixelGap / barStep));
            const indices = [];
            indices.push(0);
            for (let i = interval; i < klines.length - 1; i += interval) {
                indices.push(i);
            }
            indices.push(klines.length - 1);
            for (let j = indices.length - 1; j > 0; j--) {
                const x1 = area.x + barStep * indices[j - 1] + barStep / 2 - subPixelOffset;
                const x2 = area.x + barStep * indices[j] + barStep / 2 - subPixelOffset;
                if (x2 - x1 < minPixelGap) {
                    if (indices[j - 1] === 0) {
                        // 保护首标签：移除第二个
                        indices.splice(j, 1);
                    } else if (indices[j] === klines.length - 1) {
                        // 保护尾标签：移除倒数第二个
                        indices.splice(j - 1, 1);
                    } else {
                        indices.splice(j, 1);
                    }
                }
            }
            indices.forEach(i => {
                let shortDate;
                if (currentFreq === '15s') {
                    // 15秒：显示日期+时间（含秒）
                    shortDate = getKlineEndTime(klines[i].date, true);
                } else if (currentFreq === '1m' || currentFreq === '30m' || currentFreq === '5m') {
                    shortDate = getKlineEndTime(klines[i].date);
                } else if (currentFreq === 'w') {
                    const dateParts = klines[i].date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                } else {
                    // 日线
                    const dateParts = klines[i].date.split(/[-\/]/);
                    shortDate = dateParts[0].slice(2) + "/" + dateParts[1] + "/" + dateParts[2];
                }
                if (i === 0) {
                    ctx.textAlign = "left";
                    ctx.fillText(shortDate, area.x, dateY);
                } else if (i === klines.length - 1) {
                    ctx.textAlign = "right";
                    // K线不足一屏时：日期标签也右对齐
                    const dateX = currentFreq === 'w' ? area.x + area.w : area.x + area.w;
                    ctx.fillText(shortDate, dateX, dateY);
                } else {
                    ctx.textAlign = "center";
                    const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                    ctx.fillText(shortDate, x, dateY);
                }
            });
        }

        function onWheel(e) {
            e.preventDefault();
            if (isDualWindow) { activeDualWindow = 'top'; updateActiveWindowClass(); updateSlider(); }
            const area = getChartArea();
            const klines = chartData.klines;
            const barStep = area.w / viewCount;
            const ratio = Math.max(0, Math.min(1, (mouseX - area.x) / area.w));
            const mouseKIdx = ratio * viewCount;

            const zoomFactor = 1.15;
            const newViewCount = e.deltaY > 0
                ? Math.min(klines.length, Math.ceil(viewCount * zoomFactor))
                : Math.max(3, Math.round(viewCount / zoomFactor));
            if (newViewCount === viewCount) return;

            const maxOffset = klines.length - newViewCount;

            if (mouseKIdx >= viewCount - 1) {
                const rightGlobalIdx = viewOffset + viewCount - 1;
                viewCount = newViewCount;
                viewOffset = Math.max(0, Math.min(maxOffset, rightGlobalIdx - newViewCount + 1));
                if (isDualWindow) { renderTop(); } else { render(); }
                return;
            }

            const anchorGlobalIdx = viewOffset + mouseKIdx;
            let newViewOffset = anchorGlobalIdx - ratio * newViewCount;
            newViewOffset = Math.max(0, newViewOffset);
            if (newViewOffset > maxOffset) newViewOffset = maxOffset;

            viewCount = newViewCount;
            viewOffset = newViewOffset;
            if (isDualWindow) { renderTop(); } else { render(); }
        }

        function onMouseDown(e) {
            isDragging = true; dragStartX = e.clientX; dragStartOffset = viewOffset; canvas.style.cursor = "grabbing";
            _mouseDownX = e.clientX; _mouseDownY = e.clientY;
            if (isDualWindow) { activeDualWindow = 'top'; updateActiveWindowClass(); updateSlider(); }
        }
        function onMouseMove(e) {
            const rect = canvas.getBoundingClientRect();
            mouseX = e.clientX - rect.left; mouseY = e.clientY - rect.top;
            if (isDragging) { viewOffset = dragStartOffset - (e.clientX - dragStartX) / (getChartArea().w / viewCount); viewOffset = Math.max(0, Math.min(chartData.klines.length - viewCount, viewOffset)); }
            // 双窗口红框：直接用 MouseEvent.ctrlKey 检测，比 keydown/keyup 跟踪更可靠
            if (isDualWindow) {
                const prevCtrl = _ctrlPressed;
                _ctrlPressed = e.ctrlKey;
                // Ctrl 状态变化时强制重绘（松开Ctrl立即清除红框/新中枢）
                if (_ctrlPressed !== prevCtrl) {
                    if (!_ctrlPressed) {
                        dualRedRange = null;
                        dualShowNewZs = false;
                        dualNewZsData = null;
                    }
                }
                renderTop();
            } else {
                render();
            }
        }
        function onMouseUp(e) {
            isDragging = false; canvas.style.cursor = "crosshair";
            // 只处理左键点击（非拖拽）
            if (e.button !== 0 || Math.abs(e.clientX - _mouseDownX) >= 5 || Math.abs(e.clientY - _mouseDownY) >= 5) return;
            if (_currentGlobalIdx < 0 || !chartData) return;

            // === Ctrl+点击：区间选择模式切换 ===
            if (e.ctrlKey) {
                if (_rangeSelect.mode === 'IDLE') {
                    // 进入选择模式，记录起点A
                    _rangeSelect = {
                        mode: 'SELECTED_A',
                        startIdx: _currentGlobalIdx,
                        startFreq: currentFreq,
                        startSymbol: chartData.meta.symbol
                    };
                    const startDate = chartData.klines[_currentGlobalIdx].date.split(' ')[0];
                    showDualToast("区间起点: " + startDate + "，点击另一根K线完成选择");
                    render();
                } else {
                    // Ctrl+再次点击：取消选择
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("区间选择已取消");
                    render();
                }
                return;
            }

            // === 普通点击：如果在选择模式中，完成区间选择 ===
            if (_rangeSelect.mode === 'SELECTED_A') {
                // 验证：同一股票、同一周期
                if (_rangeSelect.startFreq !== currentFreq || _rangeSelect.startSymbol !== chartData.meta.symbol) {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("股票或周期已变更，区间选择已取消");
                    return;
                }
                const a = Math.min(_rangeSelect.startIdx, _currentGlobalIdx);
                const b = Math.max(_rangeSelect.startIdx, _currentGlobalIdx);
                const klines = chartData.klines;
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                const lines = [];
                for (let i = a; i <= b; i++) {
                    const k = klines[i];
                    const prevK = i > 0 ? klines[i - 1] : null;
                    const prevClose = prevK ? prevK.close : k.open;
                    const changeVal = k.close - prevClose;
                    const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
                    const sign = changeVal >= 0 ? "+" : "";
                    const wd = "周" + weekDays[new Date(k.date.replace(/\//g, "-").replace(" ", "T")).getDay()];
                    lines.push(`${k.date} ${wd} 开:${k.open.toFixed(2)} 高:${k.high.toFixed(2)} 低:${k.low.toFixed(2)} 收:${k.close.toFixed(2)} 涨跌:${sign}${changeVal.toFixed(2)} 涨幅:${sign}${changePct}% 复权:${chartData.meta.forward_adjust ? "前复权" : "不复权"}`);
                }
                navigator.clipboard.writeText(lines.join("\n")).catch(() => {});
                showDualToast("已复制 " + (b - a + 1) + " 根K线数据到剪贴板");
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                render();
                return;
            }

            // === 普通模式：复制当前K线信息 ===
            if (_currentClipText) {
                navigator.clipboard.writeText(_currentClipText).catch(() => {});
            }
        }
        function onMouseLeave() { isDragging = false; mouseX = -1; mouseY = -1; canvas.style.cursor = "crosshair"; if (isDualWindow) { dualOffscreenState = false; dualHighlightRange = null; dualRedRange = null; dualNewZsData = null; dualShowNewZs = false; renderTop(); } else { render(); } }

        // 双窗口toast提示
        function showDualToast(msg) {
            let toast = document.getElementById("dual-toast");
            if (!toast) {
                toast = document.createElement("div");
                toast.id = "dual-toast";
                toast.style.cssText = "position:fixed;top:50%;left:50%;transform:translate(-50%,-50%);background:rgba(0,0,0,0.85);color:#fff;padding:12px 28px;border-radius:8px;font-size:14px;z-index:9999;pointer-events:none;opacity:0;transition:opacity 0.3s;";
                document.body.appendChild(toast);
            }
            toast.textContent = msg;
            toast.style.opacity = "1";
            clearTimeout(toast._timer);
            toast._timer = setTimeout(() => { toast.style.opacity = "0"; }, 1000);
        }

        // Esc键取消区间选择 / 关闭设置抽屉
        document.addEventListener('keydown', function(e) {
            if (e.key === 'Escape') {
                var drawer = document.getElementById("bsp-filter-dialog");
                if (drawer.classList.contains("show")) {
                    closeBspSettings();
                    return;
                }
                if (_rangeSelect.mode === 'SELECTED_A') {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("区间选择已取消");
                    render();
                }
            }
        });
        document.addEventListener('keyup', function(e) {
            // 兜底：松开Ctrl时清除红框（onMouseMove用e.ctrlKey是主要检测路径）
            if (e.key === 'Control' && isDualWindow) {
                _ctrlPressed = false;
                dualRedRange = null;
                dualShowNewZs = false;
                dualNewZsData = null;
                renderTop();
            }
        });

        // 更新双窗口激活状态视觉提示
        function updateActiveWindowClass() {
            const topDiv = document.getElementById("chart-top");
            const bottomDiv = document.getElementById("chart-bottom");
            if (topDiv) topDiv.classList.toggle("dual-active", activeDualWindow === 'top');
            if (bottomDiv) bottomDiv.classList.toggle("dual-active", activeDualWindow === 'bottom');
        }

        // 双窗口切换
        window.toggleDualWindow = function() {
            if (!chartData) return;
            const btn = document.getElementById("btn-dual");
            if (isDualWindow) {
                // 关闭双窗口
                isDualWindow = false;
                activeDualWindow = 'top';
                dualBottomData = null;
                dualBottomFreq = '';
                dualHighlightRange = null;
                dualRedRange = null;
                dualNewZsData = null;
                dualShowNewZs = false;
                dualNewZsLeftDate = "";
                dualNewZsRightDate = "";
                btn.classList.remove("active");
                const isFuturesClose = chartData && chartData.meta && chartData.meta.market === 'futures';
                updateFreqButtonStates(isFuturesClose);
                // 恢复单canvas布局
                const container = document.getElementById("chart-container");
                const topDiv = document.getElementById("chart-top");
                const bottomDiv = document.getElementById("chart-bottom");
                if (topDiv) topDiv.remove();
                if (bottomDiv) bottomDiv.remove();
                canvas = topCanvas; ctx = topCtx;
                container.appendChild(canvas);
                resizeCanvas();
                // 期货：关闭双窗口后重连单SSE
                if (isFuturesClose) {
                    disconnectRealtime();
                    connectRealtimeInit(chartData.meta.symbol, currentFreq);
                } else {
                    render();
                }
            } else {
                // 开启双窗口
                const bottomFreq = getDualBottomFreq(currentFreq);
                if (!bottomFreq) {
                    // 5分周期无对应，提示
                    return;
                }
                isDualWindow = true;
                dualBottomFreq = bottomFreq;
                btn.classList.add("active");
                // 创建双窗口布局
                const container = document.getElementById("chart-container");
                // 保存原始canvas引用
                const origCanvas = topCanvas;
                // 清空容器
                container.innerHTML = '';
                // 创建上面窗口
                const topDiv = document.createElement("div");
                topDiv.id = "chart-top";
                topDiv.appendChild(origCanvas);
                container.appendChild(topDiv);
                // 创建下面窗口
                const bottomDiv = document.createElement("div");
                bottomDiv.id = "chart-bottom";
                bottomCanvas = document.createElement("canvas");
                bottomCtx = bottomCanvas.getContext("2d");
                bottomDiv.appendChild(bottomCanvas);
                container.appendChild(bottomDiv);
                // 添加下面窗口事件
                bottomCanvas.addEventListener("wheel", onBottomWheel, { passive: false });
                bottomCanvas.addEventListener("mousedown", onBottomMouseDown);
                bottomCanvas.addEventListener("mousemove", onBottomMouseMove);
                bottomCanvas.addEventListener("mouseup", onBottomMouseUp);
                bottomCanvas.addEventListener("mouseleave", onBottomMouseLeave);
                bottomCanvas.addEventListener("dblclick", function(e) {
                    if (!dualBottomData) return;
                    const rect = bottomCanvas.getBoundingClientRect();
                    const clickX = e.clientX - rect.left;
                    const clickY = e.clientY - rect.top;
                    // 临时切换全局变量以使用 getChartArea 等函数
                    const _savedCanvas = canvas, _savedCtx = ctx;
                    const _savedViewOffset = viewOffset, _savedViewCount = viewCount;
                    const _savedChartData = chartData, _savedFreq = currentFreq;
                    canvas = bottomCanvas; ctx = bottomCtx;
                    viewOffset = dualBottomViewOffset; viewCount = dualBottomViewCount;
                    chartData = dualBottomData; currentFreq = dualBottomFreq;
                    const area = getChartArea();
                    const klines = getVisibleKlines();
                    if (!klines.length) { canvas = _savedCanvas; ctx = _savedCtx; viewOffset = _savedViewOffset; viewCount = _savedViewCount; chartData = _savedChartData; currentFreq = _savedFreq; return; }
                    const priceRange = getPriceRange(klines);
                    const effectiveCount = klines.length < viewCount ? klines.length : viewCount;
                    const barStep = area.w / effectiveCount;
                    const barWidth = Math.max(1, barStep * 0.7);
                    const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
                    // 检查是否落在K线上
                    let clickedOnKline = false;
                    for (let i = 0; i < klines.length; i++) {
                        const k = klines[i];
                        const x = area.x + barStep * i + barStep / 2 - subPixelOffset;
                        const highY = priceToY(k.high, area, priceRange);
                        const lowY = priceToY(k.low, area, priceRange);
                        const halfW = barWidth / 2;
                        if (clickX >= x - halfW && clickX <= x + halfW &&
                            clickY >= highY && clickY <= lowY) {
                            clickedOnKline = true;
                            break;
                        }
                    }
                    // 恢复全局变量
                    canvas = _savedCanvas; ctx = _savedCtx;
                    viewOffset = _savedViewOffset; viewCount = _savedViewCount;
                    chartData = _savedChartData; currentFreq = _savedFreq;
                    // 双击K线上无效，双击空白处
                    if (clickedOnKline) return;
                    // 状态A：让下面窗口平移到对应区间
                    if (dualOffscreenState && dualHighlightRange && dualBottomData) {
                        const hr = dualHighlightRange;
                        if (hr.startIdx >= 0 && hr.endIdx >= 0) {
                            const centerIdx = (hr.startIdx + hr.endIdx) / 2;
                            const totalKlines = dualBottomData.klines.length;
                            let newOffset = Math.round(centerIdx - dualBottomViewCount / 2);
                            if (newOffset < 0) newOffset = 0;
                            const maxOffset = Math.max(0, totalKlines - dualBottomViewCount);
                            if (newOffset > maxOffset) newOffset = maxOffset;
                            dualBottomViewOffset = newOffset;
                            dualHighlightRange = calcGrayRange(mouseX);
                            dualRedRange = dualHighlightRange ? dualHighlightRange.redRange : null;
                            dualOffscreenState = dualHighlightRange && !dualHighlightRange.isVisible;
                        } else {
                            showDualToast("请加载更多K线...");
                        }
                        renderBottom();
                        return;
                    }
                    // 默认：恢复下面窗口全视图
                    dualBottomViewCount = 377;
                    dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                    if (dualBottomData.klines.length < dualBottomViewCount) {
                        dualBottomViewOffset = 0;
                    }
                    renderBottom();
                });
                // 恢复crosshair-info
                const crosshairInfo = document.createElement("div");
                crosshairInfo.className = "crosshair-info";
                crosshairInfo.id = "crosshair-info";
                container.appendChild(crosshairInfo);
                resizeCanvas();
                const code = chartData.meta.symbol;
                const isFutures = chartData.meta.market === 'futures';
                document.getElementById("loading").classList.remove("hidden");
                document.querySelector(".loading-text").textContent = "正在加载双窗口数据...";

                if (isFutures) {
                    // 期货双窗口：使用 connectRealtimeDual，自带完整的 init/update/error 处理与自动跟随逻辑
                    connectRealtimeDual(code, currentFreq, bottomFreq);
                } else {
                    // 股票双窗口：HTTP 请求
                    fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + currentFreq + "&dual=1")
                        .then(resp => {
                            if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                            return resp.json();
                        })
                        .then(data => {
                            if (data.sub) {
                                chartData = data;
                                dualBottomData = data.sub;
                                dualBottomViewCount = 377;
                                dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                                if (dualBottomData.klines.length < dualBottomViewCount) {
                                    dualBottomViewOffset = 0;
                                }
                                document.getElementById("loading").classList.add("hidden");
                                document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                                updateFreqButtonStates(false);
                                render();
                            } else {
                                throw new Error("服务端未返回子级别数据");
                            }
                        })
                        .catch(err => {
                            alert("加载下面窗口数据失败: " + err.message);
                            isDualWindow = false;
                            activeDualWindow = 'top';
                            dualBottomData = null;
                            dualBottomFreq = '';
                            btn.classList.remove("active");
                            updateFreqButtonStates(false);
                            const container2 = document.getElementById("chart-container");
                            container2.innerHTML = '';
                            const ci2 = document.createElement("div");
                            ci2.className = "crosshair-info";
                            ci2.id = "crosshair-info";
                            container2.appendChild(ci2);
                            container2.appendChild(origCanvas);
                            canvas = topCanvas; ctx = topCtx;
                            resizeCanvas();
                            render();
                            document.getElementById("loading").classList.add("hidden");
                            document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        });
                }
            }
        };

        // 下面窗口的事件处理
        function onBottomWheel(e) {
            e.preventDefault();
            if (!dualBottomData) return;
            activeDualWindow = 'bottom';
            updateActiveWindowClass();
            updateSlider();
            const savedCanvas = canvas; const savedCtx = ctx;
            const savedViewOffset = viewOffset; const savedViewCount = viewCount;
            canvas = bottomCanvas; ctx = bottomCtx;
            viewOffset = dualBottomViewOffset; viewCount = dualBottomViewCount;
            const rect = bottomCanvas.getBoundingClientRect();
            const bMouseX = e.clientX - rect.left;
            const area = getChartArea();
            const klines = dualBottomData.klines;
            const barStep = area.w / viewCount;
            const ratio = Math.max(0, Math.min(1, (bMouseX - area.x) / area.w));
            const mouseKIdx = ratio * viewCount;
            const zoomFactor = 1.15;
            const newViewCount = e.deltaY > 0
                ? Math.min(klines.length, Math.ceil(viewCount * zoomFactor))
                : Math.max(3, Math.round(viewCount / zoomFactor));
            if (newViewCount === viewCount) { canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount; return; }
            const maxOffset = klines.length - newViewCount;
            if (mouseKIdx >= viewCount - 1) {
                const rightGlobalIdx = viewOffset + viewCount - 1;
                dualBottomViewCount = newViewCount;
                dualBottomViewOffset = Math.max(0, Math.min(maxOffset, rightGlobalIdx - newViewCount + 1));
            } else {
                const anchorGlobalIdx = viewOffset + mouseKIdx;
                let newViewOffset = anchorGlobalIdx - ratio * newViewCount;
                newViewOffset = Math.max(0, newViewOffset);
                if (newViewOffset > maxOffset) newViewOffset = maxOffset;
                dualBottomViewCount = newViewCount;
                dualBottomViewOffset = newViewOffset;
            }
            canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
            renderBottom();
        }

        function onBottomMouseDown(e) {
            dualBottomIsDragging = true;
            dualBottomDragStartX = e.clientX;
            dualBottomDragStartOffset = dualBottomViewOffset;
            dualBottomMouseDownX = e.clientX;
            dualBottomMouseDownY = e.clientY;
            bottomCanvas.style.cursor = "grabbing";
            activeDualWindow = 'bottom';
            updateActiveWindowClass();
            updateSlider();
        }

        function onBottomMouseMove(e) {
            const rect = bottomCanvas.getBoundingClientRect();
            dualBottomMouseX = e.clientX - rect.left;
            dualBottomMouseY = e.clientY - rect.top;
            if (dualBottomIsDragging && dualBottomData) {
                const savedCanvas = canvas; const savedCtx = ctx;
                const savedViewOffset = viewOffset; const savedViewCount = viewCount;
                canvas = bottomCanvas; ctx = bottomCtx;
                viewOffset = dualBottomViewOffset; viewCount = dualBottomViewCount;
                dualBottomViewOffset = dualBottomDragStartOffset - (e.clientX - dualBottomDragStartX) / (getChartArea().w / viewCount);
                dualBottomViewOffset = Math.max(0, Math.min(dualBottomData.klines.length - dualBottomViewCount, dualBottomViewOffset));
                canvas = savedCanvas; ctx = savedCtx; viewOffset = savedViewOffset; viewCount = savedViewCount;
            }
            renderBottom();
        }

        function onBottomMouseUp(e) {
            dualBottomIsDragging = false;
            bottomCanvas.style.cursor = "crosshair";
            // 只处理左键点击（非拖拽）
            if (e.button !== 0 || Math.abs(e.clientX - dualBottomMouseDownX) >= 5 || Math.abs(e.clientY - dualBottomMouseDownY) >= 5) return;
            if (_bottomCurrentGlobalIdx < 0 || !dualBottomData) return;

            // === Ctrl+点击：区间选择模式切换（底部窗口）===
            if (e.ctrlKey) {
                if (_rangeSelect.mode === 'IDLE') {
                    _rangeSelect = {
                        mode: 'SELECTED_A',
                        startIdx: _bottomCurrentGlobalIdx,
                        startFreq: dualBottomFreq,
                        startSymbol: dualBottomData.meta.symbol
                    };
                    const startDate = dualBottomData.klines[_bottomCurrentGlobalIdx].date.split(' ')[0];
                    showDualToast("区间起点: " + startDate + "，点击另一根K线完成选择");
                    renderBottom();
                } else {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("区间选择已取消");
                    renderBottom();
                }
                return;
            }

            // === 普通点击：如果在选择模式中，完成区间选择（底部窗口）===
            if (_rangeSelect.mode === 'SELECTED_A') {
                // 验证：同一股票、同一周期
                if (_rangeSelect.startFreq !== dualBottomFreq || _rangeSelect.startSymbol !== dualBottomData.meta.symbol) {
                    _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                    showDualToast("股票或周期已变更，区间选择已取消");
                    return;
                }
                const a = Math.min(_rangeSelect.startIdx, _bottomCurrentGlobalIdx);
                const b = Math.max(_rangeSelect.startIdx, _bottomCurrentGlobalIdx);
                const klines = dualBottomData.klines;
                const weekDays = ["日", "一", "二", "三", "四", "五", "六"];
                const lines = [];
                for (let i = a; i <= b; i++) {
                    const k = klines[i];
                    const prevK = i > 0 ? klines[i - 1] : null;
                    const prevClose = prevK ? prevK.close : k.open;
                    const changeVal = k.close - prevClose;
                    const changePct = prevClose !== 0 ? (changeVal / prevClose * 100).toFixed(2) : "0.00";
                    const sign = changeVal >= 0 ? "+" : "";
                    const wd = "周" + weekDays[new Date(k.date.replace(/\//g, "-").replace(" ", "T")).getDay()];
                    lines.push(`${k.date} ${wd} 开:${k.open.toFixed(2)} 高:${k.high.toFixed(2)} 低:${k.low.toFixed(2)} 收:${k.close.toFixed(2)} 涨跌:${sign}${changeVal.toFixed(2)} 涨幅:${sign}${changePct}% 复权:${chartData.meta.forward_adjust ? "前复权" : "不复权"}`);
                }
                navigator.clipboard.writeText(lines.join("\n")).catch(() => {});
                showDualToast("已复制 " + (b - a + 1) + " 根K线数据到剪贴板");
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
                renderBottom();
                return;
            }

            // === 普通模式：复制当前K线信息 ===
            if (_bottomClipText) {
                navigator.clipboard.writeText(_bottomClipText).catch(() => {});
            }
        }

        function onBottomMouseLeave() {
            dualBottomIsDragging = false;
            dualBottomMouseX = -1; dualBottomMouseY = -1;
            bottomCanvas.style.cursor = "crosshair";
            renderBottom();
        }

        window.toggleOverlay = function(type) {
            if (type === "bi") { showBi = !showBi; document.getElementById("btn-bi").classList.toggle("active", showBi); }
            else if (type === "fx") { showFx = !showFx; document.getElementById("btn-fx").classList.toggle("active", showFx); }
            else if (type === "zs") { showZs = !showZs; document.getElementById("btn-zs").classList.toggle("active", showZs); }
            else if (type === "seg") { showSeg = !showSeg; document.getElementById("btn-seg").classList.toggle("active", showSeg); }
            else if (type === "bsp") { showBsp = !showBsp; document.getElementById("btn-bsp").classList.toggle("active", showBsp); }
            saveOverlaySettings();
            render();
        };

        window.toggleStats = function() { document.getElementById("stats-panel").classList.toggle("show"); };

        // ── BSP买卖点类型过滤 + 均线周期设置 ──
        window.openBspSettings = function() {
            // 打开前同步当前过滤状态到复选框
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = bspFilter[cbs[i].value];
            }
            // 同步均线周期复选框
            var macbs = document.querySelectorAll('#bsp-filter-dialog input[name="ma-period"]');
            for (var i = 0; i < macbs.length; i++) {
                macbs[i].checked = !!maPeriods[macbs[i].value];
            }
            document.getElementById("bsp-filter-dialog").classList.add("show");
            document.getElementById("bsp-filter-overlay").classList.add("show");
        };
        window.closeBspSettings = function() {
            document.getElementById("bsp-filter-dialog").classList.remove("show");
            document.getElementById("bsp-filter-overlay").classList.remove("show");
        };
        // 即时生效：单个买卖点复选框变化
        window.onBspFilterChange = function(cb) {
            bspFilter[cb.value] = cb.checked;
            saveOverlaySettings();
            render();
        };
        // 即时生效：单个均线周期复选框变化
        window.onMaPeriodChange = function(cb) {
            if (cb.checked) maPeriods[cb.value] = true;
            else delete maPeriods[cb.value];
            showMa = getShowMa();
            saveOverlaySettings();
            render();
        };
        window.bspFilterSelectAll = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = true;
                bspFilter[cbs[i].value] = true;
            }
            saveOverlaySettings();
            render();
        };
        window.bspFilterSelectNone = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="bsp-filter"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = false;
                bspFilter[cbs[i].value] = false;
            }
            saveOverlaySettings();
            render();
        };
        window.maPeriodsSelectAll = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="ma-period"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = true;
                maPeriods[cbs[i].value] = true;
            }
            showMa = getShowMa();
            saveOverlaySettings();
            render();
        };
        window.maPeriodsSelectNone = function() {
            var cbs = document.querySelectorAll('#bsp-filter-dialog input[name="ma-period"]');
            for (var i = 0; i < cbs.length; i++) {
                cbs[i].checked = false;
            }
            maPeriods = {};
            showMa = getShowMa();
            saveOverlaySettings();
            render();
        };

        // 辅助：根据chartData中的saved_selection_date恢复重启按钮状态
        function updateRestartBtn() {
            var hasPoint = chartData && chartData.meta && chartData.meta.saved_selection_date;
            document.getElementById("btn-restart").disabled = !hasPoint;
        }

        function updateDualBtn() {
            const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            if (isFutures) {
                // 期货：30m/5m/1m 可双窗口，15s 不可
                document.getElementById("btn-dual").disabled = (currentFreq === '15s');
            } else {
                // 股票：w/d/30m 可双窗口，5m 不可
                document.getElementById("btn-dual").disabled = (currentFreq === '5m');
            }
        };

        // ============================================================
        // 重启：清除选点，按冷启动重新加载
        // ============================================================
        window.restartStock = function() {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            const isFutures = chartData.meta.market === 'futures';
            document.getElementById("btn-restart").disabled = true;
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在重置...";

            // 期货：清除选点 + 冷启动重连SSE（无start_time）
            if (isFutures) {
                fetch("/api/futures_clear_saved_point?symbol=" + encodeURIComponent(code) + "&freq=" + freq)
                    .then(resp => resp.json())
                    .then(() => {
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        connectRealtimeInit(code, freq);  // 冷启动，不带start_time
                    })
                    .catch(err => {
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        document.getElementById("btn-restart").disabled = false;
                        alert("重置失败: " + err.message);
                    });
                return;
            }

            // 股票：清除选点 + 冷启动HTTP
            // Step 1: 调用后端清除CSV中该周期选点
            fetch("/api/clear_saved_point?code=" + encodeURIComponent(code) + "&freq=" + freq)
                .then(resp => resp.json())
                .then(() => {
                    // Step 2: 冷启动重新加载
                    return fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq + (isDualWindow && getDualBottomFreq(freq) ? "&dual=1" : ""));
                })
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "重置失败"); });
                    return resp.json();
                })
                .then(data => {
                    // 全文替换 chartData
                    chartData = data;
                    if (chartData.meta.freq === "5分钟") {
                        currentFreq = "5m";
                    } else if (chartData.meta.freq === "30分钟") {
                        currentFreq = "30m";
                    } else if (chartData.meta.freq === "周线") {
                        currentFreq = "w";
                    } else {
                        currentFreq = "d";
                    }
                    updateDateInputType();
                    document.getElementById("btn-d").classList.toggle("active", currentFreq === "d");
                    document.getElementById("btn-w").classList.toggle("active", currentFreq === "w");
                    document.getElementById("btn-30m").classList.toggle("active", currentFreq === "30m");
                    document.getElementById("btn-5m").classList.toggle("active", currentFreq === "5m");
                    viewCount = 377;
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    resizeCanvas();
                    render();
                    generateStats();
                    updateRestartBtn();
                    updateDualBtn();
                    // 双窗口模式：从 data.sub 恢复子级别数据
                    if (isDualWindow && data.sub) {
                        dualBottomData = data.sub;
                        dualBottomViewCount = 377;
                        dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                        if (dualBottomData.klines.length < dualBottomViewCount) {
                            dualBottomViewOffset = 0;
                        }
                    }
                })
                .catch(err => {
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    document.getElementById("btn-restart").disabled = false;
                    alert("重置失败: " + err.message);
                });
        };

        // ============================================================
        // 股票买卖点扫描（逐只扫描，实时进度，可中断）
        // ============================================================
        let _scanRunning = false;
        let _scanAborted = false;
        let _scanMode = "ann"; // "ann" = 标注扫描, "bsp" = 买卖点扫描
        let _scanRecentDays = 1; // 最近N根K线，默认1
        let _scanSources = ["zxg"]; // 多选：["zxg", "sz50", "hs300", "zz500", "zz1000"]

        // 扫描模式切换时，控制"最近N根"输入框的灰化状态
        // 标注扫描：只要有标注就命中，与日期无关，输入框置灰
        // 买卖点扫描：需要按最近N根K线过滤，输入框可用
        function updateScanRecentDisabled() {
            var row = document.getElementById("scan-recent-row");
            var input = document.getElementById("scan-recent-days");
            if (!row || !input) return;
            var selected = document.querySelector('input[name="scan-mode"]:checked');
            var isAnn = selected && selected.value === "ann";
            if (isAnn) {
                row.style.opacity = "0.35";
                row.style.pointerEvents = "none";
                input.disabled = true;
            } else {
                row.style.opacity = "1";
                row.style.pointerEvents = "";
                input.disabled = false;
            }
        }
        window.updateScanRecentDisabled = updateScanRecentDisabled;

        // 扫描来源→中文标签（多选时用顿号连接）
        function _scanSourceLabel() {
            var map = {"zxg": "自选股", "sz50": "上证50", "hs300": "沪深300", "zz500": "中证500", "zz1000": "中证1000"};
            var labels = [];
            for (var i = 0; i < _scanSources.length; i++) {
                labels.push(map[_scanSources[i]] || _scanSources[i]);
            }
            return labels.join("、");
        }

        // 全选 / 取消 扫描来源
        window.scanSourceSelectAll = function() {
            var cbs = document.querySelectorAll('input[name="scan-source"]');
            for (var i = 0; i < cbs.length; i++) { cbs[i].checked = true; }
        };
        window.scanSourceSelectNone = function() {
            var cbs = document.querySelectorAll('input[name="scan-source"]');
            for (var i = 0; i < cbs.length; i++) { cbs[i].checked = false; }
        };

        console.log("[扫描模块] v2-多选版 已加载 OK");

        // 生成买卖点标签HTML（最多显示6个，超出显示+N）
        function buildBspTagsHtml(buyPoints, sellPoints) {
            var MAX_TAGS = 6;
            var allTags = [];
            (buyPoints || []).forEach(function(bp) {
                var tp = bp.type.replace(/\s/g, "");
                if (tp === "0" || tp === "1" || tp === "2" || tp === "3") {
                    allTags.push('<span class="scan-bsp-tag buy">' + bp.type + '</span>');
                }
            });
            (sellPoints || []).forEach(function(sp) {
                var tp = sp.type.replace(/\s/g, "");
                if (tp === "0" || tp === "1" || tp === "2" || tp === "3") {
                    allTags.push('<span class="scan-bsp-tag sell">' + sp.type + '</span>');
                }
            });
            var html = '<div class="scan-bsp-tags">';
            if (allTags.length <= MAX_TAGS) {
                html += allTags.join('');
            } else {
                html += allTags.slice(0, MAX_TAGS).join('');
                html += '<span class="scan-bsp-tag scan-bsp-more">+' + (allTags.length - MAX_TAGS) + '</span>';
            }
            html += '</div>';
            return html;
        }

        // 扫描模式对话框：取消
        window.scanModeDialogCancel = function() {
            document.getElementById("scan-mode-dialog").classList.remove("show");
        };

        // 扫描模式对话框：确认
        window.scanModeDialogConfirm = function() {
            var selected = document.querySelector('input[name="scan-mode"]:checked');
            if (!selected) return;
            _scanMode = selected.value;
            // 多选：读取所有勾选的 checkbox
            var sourceCbs = document.querySelectorAll('input[name="scan-source"]:checked');
            _scanSources = [];
            for (var i = 0; i < sourceCbs.length; i++) {
                _scanSources.push(sourceCbs[i].value);
            }
            if (_scanSources.length === 0) {
                _scanSources = ["zxg"];
                document.querySelector('input[name="scan-source"][value="zxg"]').checked = true;
            }
            var daysInput = document.getElementById("scan-recent-days");
            _scanRecentDays = parseInt(daysInput.value) || 1;
            if (_scanRecentDays < 1) _scanRecentDays = 1;
            // 持久化到 localStorage，下次打开保持上次选择
            try {
                localStorage.setItem("scan_mode", _scanMode);
                localStorage.setItem("scan_recent_days", String(_scanRecentDays));
                localStorage.setItem("scan_sources", _scanSources.join(","));
            } catch(e) {}
            console.log("[扫描对话框] 用户选择来源: " + _scanSources.join(",") + " 模式: " + _scanMode + " 最近: " + _scanRecentDays);
            document.getElementById("scan-mode-dialog").classList.remove("show");
            // 执行实际扫描
            doStartScan();
        };

        function updateScanTitle() {
            var freq = currentFreq;
            var freqLabels = {"d": "日K", "w": "周K", "30m": "30分", "5m": "5分"};
            var freqLabel = freqLabels[freq] || freq;
            if (_scanMode === "bsp") {
                document.getElementById("scan-title").innerHTML = freqLabel + ' <span style="font-size:11px;font-weight:400;color:#a8b2d1">[最近</span><b style="font-size:11px;color:#e94560"> ' + _scanRecentDays + ' </b><span style="font-size:11px;font-weight:400;color:#a8b2d1">根]</span>';
            } else {
                // 标注扫描：显示全周期，不再显示当前周期
                document.getElementById("scan-title").textContent = "扫描全周期";
            }
        }

        window.startScanZxg = function() {
            if (_scanRunning) {
                // 正在扫描中，再次点击 = 中断扫描
                _scanAborted = true;
                var btn = document.getElementById("btn-scan");
                btn.textContent = "正在中断...";
                btn.disabled = true;
                // 通知后端立即终止
                fetch("/api/scan_abort").catch(function(){});
                return;
            }
            // 弹出模式选择对话框
            // 从 localStorage 恢复上次的选择
            try {
                var savedMode = localStorage.getItem("scan_mode");
                if (savedMode === "bsp" || savedMode === "ann") {
                    _scanMode = savedMode;
                    var radio = document.querySelector('input[name="scan-mode"][value="' + savedMode + '"]');
                    if (radio) radio.checked = true;
                }
                var savedDays = localStorage.getItem("scan_recent_days");
                if (savedDays) {
                    _scanRecentDays = parseInt(savedDays) || 1;
                    document.getElementById("scan-recent-days").value = _scanRecentDays;
                }
                var savedSources = localStorage.getItem("scan_sources");
                if (savedSources) {
                    var arr = savedSources.split(",");
                    var valid = [];
                    for (var i = 0; i < arr.length; i++) {
                        var v = arr[i].trim();
                        if (v === "zxg" || v === "sz50" || v === "hs300" || v === "zz500" || v === "zz1000") {
                            valid.push(v);
                        }
                    }
                    if (valid.length > 0) {
                        _scanSources = valid;
                        // 先全部取消，再勾选保存的
                        var allCbs = document.querySelectorAll('input[name="scan-source"]');
                        for (var i = 0; i < allCbs.length; i++) { allCbs[i].checked = false; }
                        for (var i = 0; i < valid.length; i++) {
                            var cb = document.querySelector('input[name="scan-source"][value="' + valid[i] + '"]');
                            if (cb) cb.checked = true;
                        }
                    }
                }
            } catch(e) {}
            // 根据当前模式设置"最近N根"输入框的灰化状态
            updateScanRecentDisabled();
            document.getElementById("scan-mode-dialog").classList.add("show");
        };

        // 多来源合并：后端统一合并去重，前端只需传逗号分隔的来源列表
        function _fetchMergedStocks(sources) {
            return fetch("/api/scan_stock_list?source=" + sources.join(","))
                .then(function(r) { return r.json(); })
                .then(function(data) {
                    if (data.errors && data.errors.length > 0) {
                        console.warn("[扫描] 后端合并警告:", data.errors.join("; "));
                    }
                    return data.stocks || [];
                });
        }

        // 实际执行扫描（由对话框确认后调用）
        function doStartScan() {
            var panel = document.getElementById("scan-panel");
            var body = document.getElementById("scan-body");
            var status = document.getElementById("scan-status");
            var btn = document.getElementById("btn-scan");

            panel.classList.add("show");
            btn.classList.add("active");
            _scanRunning = true;
            _scanAborted = false;

            var freq = currentFreq;
            updateScanTitle();
            status.textContent = "";

            // 标注扫描模式：直接查询标注缓存（全周期，不按 freq 过滤）
            if (_scanMode === "ann") {
                var sourceLabel = _scanSourceLabel();
                body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在查询' + sourceLabel + '标注数据...</div>';
                // 合并多来源股票列表 + 标注缓存（不传 freq，返回全部周期的标注）
                Promise.all([
                    _fetchMergedStocks(_scanSources),
                    fetch("/api/annotations_scan")
                ])
                .then(function(resps) {
                    return Promise.all([Promise.resolve(resps[0]), resps[1].json()]);
                })
                .then(function(dataArr) {
                    var stocks = dataArr[0];
                    var annData = dataArr[1];
                    console.log("[标注扫描] 合并后股票总数: " + (stocks ? stocks.length : 0) + " 只, 来源: " + _scanSources.join(","));
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "股票扫描";

                    if (!stocks || stocks.length === 0) {
                        body.innerHTML = '<div class="scan-no-result">' + sourceLabel + '列表为空或文件不存在</div>';
                        return;
                    }

                    // 构建标注记录数组（按 "code.market" 分组，保留同一股票多个周期的记录）
                    var annotatedByCode = {};
                    (annData.codes || []).forEach(function(c) {
                        var k = c.code + "." + c.market;
                        if (!annotatedByCode[k]) annotatedByCode[k] = [];
                        annotatedByCode[k].push(c);
                    });

                    // 周期→中文标签
                    var freqLabelMap = {"d": "日K", "w": "周K", "30m": "30分", "5m": "5分", "60m": "60分", "1m": "1分", "15s": "15秒"};

                    // 交叉匹配：股票列表中有标注的（用复合key匹配，避免000001.SH/000001.SZ冲突）
                    var results = [];
                    stocks.forEach(function(stk) {
                        var market = stk.prefix === "1" ? "SH" : stk.prefix === "0" ? "SZ" : stk.prefix === "2" ? "BJ" : stk.prefix.toUpperCase();
                        var lookupKey = stk.code + "." + market;
                        var annList = annotatedByCode[lookupKey];
                        if (annList) {
                            // 同一股票可能有多个周期的标注，每个周期一条记录
                            annList.forEach(function(ann) {
                                results.push({
                                    code: stk.code + "." + market,
                                    name: ann.name || (stk.code + "." + market),
                                    freq: ann.freq,
                                    freqLabel: freqLabelMap[ann.freq] || ann.freq,
                                    count: ann.count,
                                    annotations: ann.annotations || []
                                });
                            });
                        }
                    });

                    var html = '<div class="scan-summary">' + sourceLabel + '（合并后<b>' + stocks.length + '</b>只），有标注 <b>' + results.length + '</b> 条</div>';
                    if (results.length === 0) {
                        html += '<div class="scan-no-result">未发现标注股票</div>';
                    } else {
                        results.forEach(function(r) {
                            // 取日期最靠近当前日期的标注文字，最多8字
                            var closestText = "";
                            if (r.annotations && r.annotations.length > 0) {
                                var today = new Date();
                                today.setHours(0, 0, 0, 0);
                                var closest = null;
                                var closestDiff = Infinity;
                                r.annotations.forEach(function(a) {
                                    var d = new Date(a.date.replace(/\//g, "-"));
                                    if (isNaN(d.getTime())) return;
                                    var diff = Math.abs(d - today);
                                    if (diff < closestDiff) {
                                        closestDiff = diff;
                                        closest = a.text;
                                    }
                                });
                                if (closest) {
                                    closestText = closest.length > 11 ? closest.substring(0, 11) + "..." : closest;
                                }
                            }
                            html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + r.freq + '\')" title="点击查看K线图">';
                            html += chkBox(r.code, isLatestBspBuy(r));
                            html += '<span class="scan-col-name">' + r.name + '</span>';
                            html += '<span class="scan-col-code">' + r.code + '</span>';
                            html += '<span class="scan-col-freq">' + r.freqLabel + '</span>';
                            html += '<span class="scan-col-ann">' + closestText + '</span>';
                            html += '<span class="scan-col-tags"><span class="scan-bsp-tag buy">' + r.count + '条</span></span>';
                            html += '</div>';
                        });
                    }
                    body.innerHTML = html;
                    updateScanSaveBtn();
                })
                .catch(function(err) {
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "股票扫描";
                    body.innerHTML = '<div class="scan-no-result">查询失败: ' + err.message + '</div>';
                });
                return;
            }

            // 买卖点扫描模式（原有逻辑）
            // 第一步：通知后端开始新扫描 + 合并多来源股票列表
            var sourceLabel = _scanSourceLabel();
            body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在读取' + sourceLabel + '列表...</div>';
            Promise.all([
                fetch("/api/scan_start"),
                _fetchMergedStocks(_scanSources)
            ])
                .then(function(resps) {
                    // 先检查 scan_start 的响应
                    return resps[0].json().then(function(scanStartData) {
                        if (scanStartData.need_refresh) {
                            // 需要刷新缓存数据
                            _scanRunning = false;
                            btn.classList.remove("active");
                            body.innerHTML = '<div class="scan-no-result" style="text-align:center;padding:20px;">' +
                                '<div style="font-size:14px;color:#e94560;margin-bottom:12px;">&#9888; ' + scanStartData.msg + '</div>' +
                                '<button class="btn" onclick="refreshStockNames();closeScanPanel();" style="margin-top:8px;">立即刷新</button>' +
                                '</div>';
                            return null;
                        }
                        // scan_start 正常，继续处理股票列表（_fetchMergedStocks 已返回去重数组）
                        return resps[1];
                    });
                })
                .then(function(data) {
                    if (data === null) return; // 已处理 need_refresh
                    if (!data || data.length === 0) {
                        _scanRunning = false;
                        btn.classList.remove("active");
                        body.innerHTML = '<div class="scan-no-result">' + sourceLabel + '列表为空或文件不存在</div>';
                        return;
                    }
                    var stocks = data;
                    var total = stocks.length;
                    console.log("[买卖点扫描] 合并后股票总数: " + total + " 只, 来源: " + _scanSources.join(","));
                    var results = [];
                    var skipped = 0;
                    var currentIdx = 0;
                    var completed = 0;
                    var hasRenderedAny = false;

                    // 立即更新面板为扫描进度，不等第一批请求返回
                    body.innerHTML = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 0/' + total + '，已发现 0 只股票</div>';

                    // 扫描结束统一通知后端打印
                    function finishScan(interrupted) {
                        fetch("/api/scan_end").then(function() {
                            renderScanResults(results, interrupted ? completed : total, skipped, interrupted);
                        });
                    }

                    // 实时更新面板：显示进度 + 已找到的买卖点股票
                    var _updateTimer = null;
                    var _pendingUpdate = false;
                    function updatePanel() {
                        // 节流：最多500ms更新一次，避免阻塞主线程导致"中断扫描"按钮无响应
                        if (_updateTimer) {
                            _pendingUpdate = true;
                            return;
                        }
                        _doUpdatePanel();
                        _updateTimer = setInterval(function() {
                            if (_pendingUpdate) {
                                _pendingUpdate = false;
                                _doUpdatePanel();
                            } else {
                                clearInterval(_updateTimer);
                                _updateTimer = null;
                            }
                        }, 500);
                    }
                    function _doUpdatePanel() {
                        var progress = completed + "/" + total;
                        var found = results.length;
                        var html = '<div class="scan-loading"><div class="spinner"></div><br>正在扫描 ' + progress + '，已发现 ' + found + ' 只股票</div>';
                        // 如果已经找到一些结果，实时显示出来
                        if (results.length > 0) {
                            hasRenderedAny = true;
                            html += '<div class="scan-summary" style="margin-top:8px;">已发现 <b>' + found + '</b> 只有买/卖点</div>';
                            for (var i = 0; i < results.length; i++) {
                                var r = results[i];
                                var tagsHtml = buildBspTagsHtml(r.buy_points, r.sell_points);
                                var mvText = '';
                                if (r.float_mv !== undefined && r.float_mv !== null && r.float_mv < 50) {
                                    mvText = r.float_mv.toFixed(1) + '亿';
                                }
                                var maText = '';
                                if (r.below_ma120 === true) {
                                    maText = '牛熊线下';
                                }
                                html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + currentFreq + '\')" title="点击查看K线图">';
                                html += chkBox(r.code, isLatestBspBuy(r));
                                html += '<span class="scan-col-name">' + r.name + '</span>';
                                html += '<span class="scan-col-code">' + r.code + '</span>';
                                html += '<span class="scan-col-mv">' + mvText + '</span>';
                                html += '<span class="scan-col-ma">' + maText + '</span>';
                                html += '<span class="scan-col-tags">' + tagsHtml + '</span>';
                                html += '</div>';
                            }
                        }
                        body.innerHTML = html;
                        updateScanSaveBtn();
                    }

                    function checkDone() {
                        if (_updateTimer) { clearInterval(_updateTimer); _updateTimer = null; }
                        if (_scanAborted) {
                            _scanRunning = false;
                            _scanAborted = false;
                            btn.classList.remove("active");
                            btn.disabled = false;
                            btn.textContent = "股票扫描";
                            finishScan(true);
                            return;
                        }
                        if (completed >= total) {
                            _scanRunning = false;
                            btn.classList.remove("active");
                            btn.disabled = false;
                            btn.textContent = "股票扫描";
                            finishScan(false);
                            return;
                        }
                    }

                    // 第二步：并发扫描（同时发送多个请求）
                    var CONCURRENCY = 5;  // 同时扫描5只
                    btn.textContent = "中断扫描";

                    function launchBatch() {
                        if (_scanAborted) return;
                        var batch = [];
                        while (currentIdx < total && batch.length < CONCURRENCY) {
                            batch.push(stocks[currentIdx]);
                            currentIdx++;
                        }
                        if (batch.length === 0) return;
                        var batchDone = 0;
                        var batchSize = batch.length;
                        batch.forEach(function(stk) {
                            var code = stk.code;
                            var prefix = stk.prefix;
                            fetch("/api/scan_one?code=" + code + "&freq=" + freq + "&prefix=" + prefix + "&recent=" + _scanRecentDays + "&source=" + (stk._source || "zxg") + "&_t=" + Date.now())
                                .then(function(resp) { return resp.json(); })
                                .then(function(data) {
                                    completed++;
                                    if (data.skipped) {
                                        // 预过滤跳过，不计入 error
                                        skipped++;
                                    } else if (data.error) {
                                        skipped++;
                                    } else if ((data.buy_points && data.buy_points.length > 0) || (data.sell_points && data.sell_points.length > 0)) {
                                        results.push(data);
                                    }
                                    batchDone++;
                                    if (batchDone >= batchSize) {
                                        // 整批完成后只产生一个 setTimeout，点击事件有机会插入
                                        setTimeout(function() {
                                            updatePanel();
                                            if (currentIdx < total) {
                                                launchBatch();
                                                // launchBatch 可能因 _scanAborted 提前返回，此时需手动触发 checkDone
                                                if (_scanAborted) { checkDone(); }
                                            } else {
                                                checkDone();
                                            }
                                        }, 0);
                                    }
                                })
                                .catch(function(err) {
                                    completed++;
                                    skipped++;
                                    batchDone++;
                                    if (batchDone >= batchSize) {
                                        setTimeout(function() {
                                            updatePanel();
                                            if (currentIdx < total) {
                                                launchBatch();
                                                if (_scanAborted) { checkDone(); }
                                            } else {
                                                checkDone();
                                            }
                                        }, 0);
                                    }
                                });
                        });
                    }

                    // 启动初始批次（单链递归，每次发5个请求，完成后通过setTimeout推迟下一批，
                    // 让浏览器有机会处理点击"中断扫描"按钮）
                    launchBatch();
                })
                .catch(function(err) {
                    _scanRunning = false;
                    btn.classList.remove("active");
                    btn.textContent = "股票扫描";
                    body.innerHTML = '<div class="scan-no-result">读取' + sourceLabel + '失败: ' + err.message + '</div>';
                });
        };

        // ============================================================
        // 股票名称刷新（原 GBBQ 刷新，现在仅刷新股票名称缓存）
        // ============================================================
        window.refreshStockNames = function() {
            var btn = document.getElementById("btn-refresh");
            var status = document.getElementById("refresh-status");
            if (btn.disabled) return;
            btn.disabled = true;
            btn.classList.add("active");
            btn.querySelector("svg").style.animation = "spin 1s linear infinite";
            status.style.display = "inline";
            status.textContent = "正在刷新股票名称...";

            fetch("/api/refresh_stock_names")
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.status === "already_running") {
                        pollRefreshStatus(btn, status);
                    } else {
                        pollRefreshStatus(btn, status);
                    }
                })
                .catch(function(err) {
                    btn.disabled = false;
                    btn.classList.remove("active");
                    btn.querySelector("svg").style.animation = "";
                    status.style.display = "none";
                    alert("启动刷新失败: " + err.message);
                });
        };

        function pollRefreshStatus(btn, status) {
            fetch("/api/refresh_status")
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.running) {
                        status.textContent = data.step || "刷新中...";
                        setTimeout(function() { pollRefreshStatus(btn, status); }, 500);
                    } else {
                        btn.disabled = false;
                        btn.classList.remove("active");
                        btn.querySelector("svg").style.animation = "";
                        if (data.error) {
                            status.textContent = "刷新失败";
                            alert("刷新失败: " + data.error);
                        } else {
                            status.textContent = "刷新完成";
                            setTimeout(function() { status.style.display = "none"; }, 2000);
                        }
                    }
                })
                .catch(function() {
                    btn.disabled = false;
                    btn.classList.remove("active");
                    btn.querySelector("svg").style.animation = "";
                    status.style.display = "none";
                });
        }



        function renderScanResults(results, total, skipped, interrupted) {
            var body = document.getElementById("scan-body");
            var label = interrupted ? "（已中断）" : "";
            var sourceLabel = _scanSourceLabel();
            var html = '<div class="scan-summary">' + sourceLabel + '（合并后<b>' + total + '</b>只），扫描 <b>' + (total - skipped) + '</b> 只，跳过 <b>' + skipped + '</b> 只，发现 <b>' + results.length + '</b> 只有买/卖点' + label + '</div>';
            if (results.length === 0) {
                html += '<div class="scan-no-result">当前周期下未发现买卖点股票</div>';
            } else {
                for (var i = 0; i < results.length; i++) {
                    var r = results[i];
                    var tagsHtml = buildBspTagsHtml(r.buy_points, r.sell_points);
                    // 构建各列内容
                    var mvText2 = '';
                    if (r.float_mv !== undefined && r.float_mv !== null && r.float_mv < 50) {
                        mvText2 = r.float_mv.toFixed(1) + '亿';
                    }
                    var maText2 = '';
                    if (r.below_ma120 === true) {
                        maText2 = '牛熊线下';
                    }
                    html += '<div class="scan-stock-row" onclick="loadScanResult(\'' + r.code + '\', \'' + currentFreq + '\')" title="点击查看K线图">';
                    html += chkBox(r.code, isLatestBspBuy(r));
                    html += '<span class="scan-col-name">' + r.name + '</span>';
                    html += '<span class="scan-col-code">' + r.code + '</span>';
                    html += '<span class="scan-col-mv">' + mvText2 + '</span>';
                    html += '<span class="scan-col-ma">' + maText2 + '</span>';
                    html += '<span class="scan-col-tags">' + tagsHtml + '</span>';
                    html += '</div>';
                }
            }
            body.innerHTML = html;
            updateScanSaveBtn();
        }

        // 生成复选框HTML
        function isLatestBspBuy(r) {
            var buyPoints = r.buy_points || [];
            var sellPoints = r.sell_points || [];
            if (buyPoints.length === 0 && sellPoints.length === 0) return false;
            var lastBuyDate = buyPoints.length > 0 ? buyPoints[buyPoints.length - 1].date : "";
            var lastSellDate = sellPoints.length > 0 ? sellPoints[sellPoints.length - 1].date : "";
            // 最近的是买点
            if (!lastBuyDate && !lastSellDate) return false;
            if (!lastSellDate) return true;
            if (!lastBuyDate) return false;
            return lastBuyDate >= lastSellDate;
        }

        function chkBox(code, checked) {
            return '<span class="scan-col-chk" onclick="event.stopPropagation()"><input type="checkbox" value="' + code + '" onchange="updateScanSaveBtn()" ' + (checked ? 'checked' : '') + '/></span>';
        }

        // 收集勾选的代码并更新按钮状态
        window.updateScanSaveBtn = function() {
            var checks = document.querySelectorAll("#scan-body .scan-col-chk input[type=checkbox]:checked");
            var allCbs = document.querySelectorAll("#scan-body .scan-col-chk input[type=checkbox]");
            var btn = document.getElementById("scan-save-btn");
            btn.disabled = allCbs.length === 0;
            btn.textContent = checks.length > 0 ? "保存到自选(" + checks.length + ")" : "保存到自选";
        };

        // 保存勾选到自选股（通达信+同花顺）
        window.saveScanToZxg = function() {
            var checks = document.querySelectorAll("#scan-body .scan-col-chk input[type=checkbox]:checked");
            if (checks.length === 0) return;
            var codes = [];
            checks.forEach(function(cb) { codes.push(cb.value); });
            var btn = document.getElementById("scan-save-btn");
            btn.disabled = true;
            btn.textContent = "保存中...";
            fetch("/api/zxg_save?codes=" + encodeURIComponent(codes.join(",")))
            .then(function(r) { return r.json(); })
            .then(function(data) {
                console.log("[THS] 保存响应:", data);
                // 保存结果用与扫描面板汇总行一致的亮度：普通文字 #a8b2d1，高亮数字/状态 #e94560
                btn.style.opacity = "1";
                var parts = [];
                if (data.tdx_saved > 0) {
                    parts.push("<span style='color:#a8b2d1'>通达信：</span><span style='color:#e94560'>" + data.tdx_saved + "</span><span style='color:#a8b2d1'> 只</span>");
                } else {
                    parts.push("<span style='color:#a8b2d1'>通达信：</span><span style='color:#e94560'> 已保存</span>");
                }
                // 同花顺状态：区分成功/已存在/失败/未配置
                if (data.ths_saved > 0) {
                    parts.push("<span style='color:#a8b2d1'>同花顺：</span><span style='color:#e94560'>" + data.ths_saved + "</span><span style='color:#a8b2d1'> 只</span>");
                } else if (!data.ths_msg || data.ths_msg === "THS_DIR 未配置") {
                    // 未配置同花顺目录，静默不显示
                } else if (data.ths_msg === "ok") {
                    parts.push("<span style='color:#a8b2d1'>同花顺：</span><span style='color:#e94560'> 已保存</span>");
                } else {
                    parts.push("<span style='color:#a8b2d1'>同花顺：失败</span>");
                    console.warn("[THS] 保存失败:", data.ths_msg);
                }
                btn.innerHTML = parts.join("&nbsp;&nbsp;&nbsp;");
                // 如果同花顺保存成功且数量>0，提示可能需要重启同花顺才能看到
                if (data.ths_saved > 0) {
                    setTimeout(function() {
                        alert("同花顺自选股已保存，如界面未显示请重启同花顺。");
                    }, 100);
                } else if (data.ths_msg && data.ths_msg !== "ok" && data.ths_msg !== "THS_DIR 未配置") {
                    setTimeout(function() {
                        alert("同花顺保存失败: " + data.ths_msg);
                    }, 100);
                }
                setTimeout(function() {
                    btn.textContent = "保存到自选";
                    btn.disabled = false;
                    btn.style.opacity = "";
                    updateScanSaveBtn();
                }, 2000);
            })
            .catch(function() {
                btn.textContent = "保存失败";
                btn.disabled = false;
                btn.style.opacity = "";
            });
        };

        window.closeScanPanel = function() {
            // 扫描中不允许关闭面板，用户需通过"中断扫描"按钮停止
            if (_scanRunning) return;
            document.getElementById("scan-panel").classList.remove("show");
            // 关闭面板时清除扫描缓存，释放内存
            fetch("/api/scan_clear_cache").catch(function() {});
        };

        window.loadScanResult = function(code, freq) {
            // 加载该股票到当前页面，不关闭面板
            // 传入 freq（标注所在周期），确保用正确的周期加载K线，避免标注因周期不匹配而不显示
            document.getElementById("stock-code-input").value = code;
            if (freq) {
                lastStockFreq = freq; // 让 loadStock 使用标注所在的周期
            }
            loadStock();
        };

        window.switchFreq = function(freq) {
            if (!chartData || currentFreq === freq) return;
            // 切换周期时取消区间选择
            if (_rangeSelect.mode === 'SELECTED_A') {
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
            }
            currentFreq = freq;
            updateDateInputType();
            updateDualBtn();
            const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            if (isFutures) {
                lastFuturesFreq = freq; // 期货上下文切换周期，记录
            } else {
                lastStockFreq = freq;   // 股票上下文切换周期，记录
            }
            updateFreqButtonStates(isFutures);
            // 切换周期后重新加载数据
            const code = document.getElementById("stock-code-input").value.trim() || chartData.meta.symbol;
            if (code) {
                document.getElementById("loading").classList.remove("hidden");
                // 期货：跳过HTTP，直接重连SSE（初始快照+增量合一）
                const isFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
                if (isFutures) {
                    disconnectRealtime();
                    if (isDualWindow) {
                        // 双窗口模式：用双窗口SSE
                        const newBottomFreq = getDualBottomFreq(freq);
                        dualBottomFreq = newBottomFreq;
                        connectRealtimeDual(code, freq, newBottomFreq);
                    } else {
                        connectRealtimeInit(code, freq);
                    }
                    return;
                }
                fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq + (isDualWindow && getDualBottomFreq(freq) ? "&dual=1" : ""))
                    .then(resp => {
                        if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                        return resp.json();
                    })
                    .then(data => {
                        chartData = data;
                        updateRestartBtn();
                        updateDualBtn();
                        viewCount = 377;
                        adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                        viewOffset = Math.max(0, chartData.klines.length - viewCount);
                        // K线不足一屏时右对齐
                        if (chartData.klines.length < viewCount) {
                            viewOffset = 0;
                        }
                        document.getElementById("stock-name").textContent = chartData.meta.name;
                        document.getElementById("stock-code").textContent = chartData.meta.symbol;
                        document.title = "缠论分析 - " + chartData.meta.name;
                        const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, freq);
                        document.getElementById("goto-date-input").value = lastDate;
                        updateWeekday();
                        // 双窗口模式：从 data.sub 获取子级别数据（方案B）
                        if (isDualWindow) {
                            const newBottomFreq = getDualBottomFreq(freq);
                            if (newBottomFreq) {
                                dualBottomFreq = newBottomFreq;
                                if (data.sub) {
                                    dualBottomData = data.sub;
                                    dualBottomViewCount = 377;
                                    dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                                    if (dualBottomData.klines.length < dualBottomViewCount) {
                                        dualBottomViewOffset = 0;
                                    }
                                }
                            } else {
                                // 新周期是5m，双窗口已关闭
                            }
                        }
                        document.getElementById("loading").classList.add("hidden");
                        render();
                        generateStats();
                        loadAnnotations();
                        saveLastState(); // 保存股票状态
                        startRealtimeIfFutures(data);
                    })
                    .catch(err => {
                        alert("切换周期失败: " + err.message);
                        document.getElementById("loading").classList.add("hidden");
                    });
            }
        };

        // 根据保存的选点日期，动态调整 viewCount 和 viewOffset
        // 选点后后端已过滤，klines只包含选点之后的K线，直接全部显示
        function adjustViewForSavedPoint() {
            if (!chartData || !chartData.meta) return;
            if (!chartData.meta.saved_selection_date) return;
            if (!chartData.klines || chartData.klines.length === 0) return;
            viewCount = chartData.klines.length;
            viewOffset = 0;
        }

        window.gotoDate = function() {
            // 重置所有日期输入标志位，避免上次手动输入/键盘操作阻塞后续日历点击
            _dateKeyEnter = false;
            _dateKeyArrow = false;
            _dateManualTyping = false;
            _datePickerInteracted = false;
            _datePickerInputCount = 0;
            if (!chartData) return;
            // 复盘模式下断开实时连接
            disconnectRealtime();
            const dateStr = document.getElementById("goto-date-input").value.trim();
            if (!dateStr) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            const apiDate = inputDateToApi(dateStr, freq);
            const url = "/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq + "&end_date=" + encodeURIComponent(apiDate) + (isDualWindow && getDualBottomFreq(freq) ? "&dual=1" : "");
            document.getElementById("goto-date-input").disabled = true;
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在复盘计算，请稍候...";
            fetch(url)
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "跳转失败"); });
                    return resp.json();
                })
                .then(data => {
                    chartData = data;
                    updateRestartBtn();
                    updateDualBtn();
                    // 双窗口模式：从 data.sub 恢复子级别数据
                    if (isDualWindow && data.sub) {
                        dualBottomData = data.sub;
                        dualBottomViewCount = 377;
                        dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                        if (dualBottomData.klines.length < dualBottomViewCount) {
                            dualBottomViewOffset = 0;
                        }
                    }
                    viewCount = 377;
                    adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    // 复盘后输入框显示实际最后一根K线日期
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    resizeCanvas();
                    render();
                    loadAnnotations();
                })
                .catch(err => {
                    alert("跳转失败: " + err.message);
                })
                .finally(() => {
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    document.getElementById("goto-date-input").disabled = false;
                });
        };

        let _dateKeyArrow = false, _dateKeyEnter = false, _dateManualTyping = false, _dateStepIgnore = false;
        let _dateInputTriggered = false;   // input 已触发 gotoDate，change 跳过
        let _dateFocusOriginal = "";       // onfocus 保存的原始值，用于 blur 恢复
        let _datePickerInteracted = false; // datetime-local picker 中用户有过交互，blur 时不恢复原始值
        let _datePickerInputCount = 0;     // datetime-local picker 打开后真实交互次数
        window.handleDateKeydown = function(e) {
            if (e.key === 'Enter') { _dateKeyEnter = true; gotoDate(); return; }
            if (e.key.startsWith('Arrow')) { _dateKeyArrow = true; return; }
            if (e.key !== 'Tab' && e.key !== 'Escape') { _dateManualTyping = true; }
        };
        window.handleDateChange = function() {
            if (_dateStepIgnore) return;
            updateWeekday();
            // 键盘/手动输入 → 不触发（Enter 已在 handleDateKeydown 中处理）
            if (_dateKeyEnter) { _dateKeyEnter = false; return; }
            if (_dateKeyArrow) { _dateKeyArrow = false; return; }
            if (_dateManualTyping) { _dateManualTyping = false; return; }
            // input 已处理（datetime-local "今天"），change 跳过避免重复
            if (_dateInputTriggered) { _dateInputTriggered = false; return; }
            // datetime-local 正常完成（用户选完日期+小时+分钟，picker关闭）→ 触发
            _dateFocusOriginal = "";
            _datePickerInteracted = false;
            _datePickerInputCount = 0;
            gotoDate();
        };
        window.handleDateBlur = function() {
            const input = document.getElementById("goto-date-input");
            var v = input.value;
            // picker 打开后用户未交互 → 恢复原始值
            if (_dateFocusOriginal && !_datePickerInteracted) {
                input.value = _dateFocusOriginal;
                _dateFocusOriginal = "";
            }
            _dateFocusOriginal = "";
            _datePickerInteracted = false;
            _datePickerInputCount = 0;
            v = input.value;
            if (!v) return;
            const parts = v.split('-');
            if (parts.length === 3) {
                const d = parseInt(parts[2], 10);
                if (!isNaN(d) && d > 31) {
                    input.value = parts[0] + '-' + parts[1] + '-31';
                }
            }
            // 股票 datetime-local：小时超出盘中范围(09-15)则自动修正
            if (input.type === "datetime-local" && chartData && chartData.meta && !isFuturesCode(chartData.meta.symbol)) {
                var p = input.value.split('T');
                if (p.length === 2) {
                    var tp = p[1].split(':');
                    var hh = parseInt(tp[0], 10);
                    if (hh < 9) input.value = p[0] + 'T09:' + tp[1];
                    else if (hh > 15) input.value = p[0] + 'T15:' + tp[1];
                }
            }
            updateWeekday();
        };
        window.handleDateInput = function(e) {
            const input = e.target;
            const val = input.value;
            if (!val) return;
            // 年份部分超过4位时截断到4位
            const firstDash = val.indexOf('-');
            if (firstDash === -1) {
                if (val.length > 4) {
                    input.value = val.substring(0, 4);
                    setTimeout(() => { try { input.setSelectionRange(5, 5); } catch(_) {} }, 10);
                }
            } else {
                const yearStr = val.substring(0, firstDash);
                if (yearStr.length > 4) {
                    const rest = val.substring(firstDash);
                    input.value = yearStr.substring(0, 4) + rest;
                    setTimeout(() => { try { input.setSelectionRange(5, 5); } catch(_) {} }, 10);
                }
            }
            updateWeekday();
            // 键盘输入 → 不在此处理（等待 Enter 或 change）
            if (_dateManualTyping || _dateKeyEnter || _dateKeyArrow) return;
            // datetime-local 日历交互：
            // - 用户选了日期/时间 → 标记 _datePickerInteracted，blur 时不再恢复原始值
            // - 第1次交互，日期=今天 且 时间≠原始时间 → "今天"按钮，立即触发
            // - 其他情况：不触发，等 change（正常完成选日期+小时+分钟后触发）
            if (input.type === "datetime-local" && _dateFocusOriginal) {
                _datePickerInteracted = true;
                _datePickerInputCount++;
                if (_datePickerInputCount === 1) {
                    var curParts = val.split('T');
                    var origParts = _dateFocusOriginal.split('T');
                    var todayStr = new Date().toISOString().slice(0, 10);
                    if (curParts.length === 2 && origParts.length === 2 && curParts[0] === todayStr && curParts[1] !== origParts[1]) {
                        // "今天"按钮：日期=今天 且 时间变了 → 设为 23:59，立即触发
                        input.value = curParts[0] + 'T23:59';
                        _dateFocusOriginal = "";
                        _datePickerInteracted = false;
                        _datePickerInputCount = 0;
                        _dateInputTriggered = true;
                        gotoDate();
                        return;
                    }
                }
            }
        };

        window.dateStep = function(delta) {
            if (!chartData || !chartData.klines || chartData.klines.length === 0) return;
            var input = document.getElementById("goto-date-input");
            var currentEndDate = inputDateToApi(input.value.trim(), currentFreq);
            if (!currentEndDate) return;
            _dateStepIgnore = true;
            fetchStep(currentEndDate, delta);
            _dateStepIgnore = false;
        };

        // 发送 step 请求，后端在 full_records 中偏移定位，全自动处理节假日/调休/跨周
        function fetchStep(endDate, delta) {
            var code = chartData.meta.symbol;
            var freq = currentFreq;
            var url = "/api/stock?code=" + encodeURIComponent(code) + "&freq=" + freq
                + "&end_date=" + encodeURIComponent(endDate) + "&step=" + delta
                + (isDualWindow && getDualBottomFreq(freq) ? "&dual=1" : "");
            document.getElementById("goto-date-input").disabled = true;
            document.getElementById("loading").classList.remove("hidden");
            document.querySelector(".loading-text").textContent = "正在复盘计算，请稍候...";
            fetch(url)
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "跳转失败"); });
                    return resp.json();
                })
                .then(data => {
                    chartData = data;
                    updateRestartBtn();
                    updateDualBtn();
                    if (isDualWindow && data.sub) {
                        dualBottomData = data.sub;
                        dualBottomViewCount = 377;
                        dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                        if (dualBottomData.klines.length < dualBottomViewCount) dualBottomViewOffset = 0;
                    }
                    viewCount = 377;
                    adjustViewForSavedPoint();
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    if (chartData.klines.length < viewCount) viewOffset = 0;
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    var lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    resizeCanvas();
                    render();
                    loadAnnotations();
                })
                .catch(err => { alert("箭头跳转失败: " + err.message); })
                .finally(() => {
                    document.getElementById("loading").classList.add("hidden");
                    document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                    document.getElementById("goto-date-input").disabled = false;
                });
        }

        window.updateWeekday = function() {
            var input = document.getElementById("goto-date-input");
            var span = document.getElementById("date-weekday");
            var v = input.value.trim();
            if (!v) { span.textContent = ""; return; }
            // 提取日期部分（兼容 datetime-local 的 T 分隔符）
            var datePart = v.split("T")[0];
            var parts = datePart.split('-');
            if (parts.length !== 3) { span.textContent = ""; return; }
            var d = new Date(parseInt(parts[0], 10), parseInt(parts[1], 10) - 1, parseInt(parts[2], 10));
            var weekNames = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
            span.textContent = weekNames[d.getDay()];
        };

        const HISTORY_KEY = "chan_stock_history";
        const MAX_HISTORY = 10;
        function getHistory() {
            try {
                let list = JSON.parse(localStorage.getItem(HISTORY_KEY)) || [];
                // 兼容旧格式（纯字符串）-> 转换为新格式（{code, name}）
                return list.map(c => typeof c === 'string' ? {code: c, name: ""} : c);
            } catch(e) { return []; }
        }
        function saveHistory(code, name) {
            let list = getHistory();
            list = list.filter(c => c.code !== code);
            list.unshift({code: code, name: name || ""});
            if (list.length > MAX_HISTORY) list = list.slice(0, MAX_HISTORY);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
        }
        function removeHistory(code) {
            let list = getHistory().filter(c => c.code !== code);
            localStorage.setItem(HISTORY_KEY, JSON.stringify(list));
            showHistory();
        }
        window.clearHistory = function() {
            localStorage.removeItem(HISTORY_KEY);
            document.getElementById("stock-history").classList.remove("show");
        };
        window.clearInput = function() {
            const input = document.getElementById("stock-code-input");
            input.value = "";
            document.getElementById("input-clear").style.display = "none";
        };
        let searchTimer = null;
        let searchResults = [];
        let selectedIndex = -1;
        window.onInputChange = function() {
            const input = document.getElementById("stock-code-input");
            document.getElementById("input-clear").style.display = input.value ? "" : "none";
            selectedIndex = -1;
            const val = input.value.trim();
            if (!val) {
                document.getElementById("stock-history").classList.remove("show");
                return;
            }
            // 带市场前缀+数字（如 sh600519），不搜索；纯拼音（如 SZCZ）正常搜索
            if (/^(sh|sz|bj|hk)\d/i.test(val)) {
                document.getElementById("stock-history").classList.remove("show");
                return;
            }
            // 纯数字（6位）也搜索，可能有同名（如000001=平安银行/上证指数）
            // 拼音或中文，延迟搜索
            clearTimeout(searchTimer);
            searchTimer = setTimeout(() => doSearch(val), 300);
        };
        window.onInputKeydown = function(e) {
            const el = document.getElementById("stock-history");
            if (!el.classList.contains("show") || !searchResults.length) {
                if (e.key === "Enter") loadStock();
                return;
            }
            const items = el.querySelectorAll(".stock-history-item");
            if (e.key === "ArrowDown") {
                e.preventDefault();
                selectedIndex = (selectedIndex + 1) % items.length;
                updateSearchSelection(items);
            } else if (e.key === "ArrowUp") {
                e.preventDefault();
                selectedIndex = (selectedIndex - 1 + items.length) % items.length;
                updateSearchSelection(items);
            } else if (e.key === "Enter") {
                e.preventDefault();
                if (selectedIndex >= 0 && selectedIndex < searchResults.length) {
                    const item = searchResults[selectedIndex];
                    selectHistory(item.market === 'futures' ? item.code : item.market + item.code);
                } else {
                    loadStock();
                }
            } else if (e.key === "Escape") {
                el.classList.remove("show");
            }
        };
        window.updateSearchSelection = function(items) {
            items.forEach((item, i) => {
                item.style.background = i === selectedIndex ? "#0f3460" : "";
                item.style.color = i === selectedIndex ? "#e0e0e0" : "";
            });
        };
        window.doSearch = function(keyword) {
            fetch("/api/search?q=" + encodeURIComponent(keyword))
                .then(r => r.json())
                .then(data => {
                    const el = document.getElementById("stock-history");
                    searchResults = data.results || [];
                    selectedIndex = -1;
                    if (!searchResults.length) {
                        el.classList.remove("show");
                        return;
                    }
                    el.innerHTML = searchResults.map((item, idx) => {
                        const safeCode = item.code.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                        const safeMarket = item.market.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                        const fullCode = item.market === 'futures' ? safeCode : safeMarket + safeCode;
                        const displayCode = item.market === 'futures' ? item.code : item.market + item.code;
                        const typeMap = {"深A":"深A","沪A":"沪A","深B":"深B","沪B":"沪B","指数":"指数","基金":"基金","场外基金":"场外基金","港股":"港股"};
                        const typeLabel = typeMap[item.type] || item.type;
                        return `<div class="stock-history-item" data-idx="${idx}"><span onclick="selectHistory('${fullCode}')" style="flex:1;display:block">${displayCode} - ${item.name} (${item.pinyin}) <span style="color:#888;font-size:11px;margin-left:8px">${typeLabel}</span></span></div>`;
                    }).join("");
                    el.classList.add("show");
                    // 焦点自动移到第一个候选
                    selectedIndex = 0;
                    updateSearchSelection(el.querySelectorAll(".stock-history-item"));
                })
                .catch(() => {});
        };
        window.toggleInputClear = function() {
            const input = document.getElementById("stock-code-input");
            document.getElementById("input-clear").style.display = input.value ? "" : "none";
        };
        window.removeHistory = removeHistory;
        window.showHistory = function() {
            const list = getHistory();
            const el = document.getElementById("stock-history");
            if (!list.length) { el.classList.remove("show"); return; }
            el.innerHTML = list.map(c => {
                const safe = c.code.replace(/'/g, "\\'").replace(/\\/g, "\\\\");
                const label = c.name ? c.code + " - " + c.name : c.code;
                return `<div class="stock-history-item"><span onclick="selectHistory('${safe}')" style="flex:1;display:block">${label}</span><span class="stock-history-del" onclick="event.stopPropagation();removeHistory('${safe}')">&times;</span></div>`;
            }).join("");
            el.innerHTML += `<div class="stock-history-clear" onclick="event.stopPropagation();clearHistory()">清除全部</div>`;
            el.classList.add("show");
        };
        document.addEventListener("click", function(e) {
            if (!e.target.closest(".stock-input")) {
                document.getElementById("stock-history").classList.remove("show");
            }
        });

        window.loadStock = function() {
            const code = document.getElementById("stock-code-input").value.trim();
            if (!code) return;
            // 切换股票时取消区间选择
            if (_rangeSelect.mode === 'SELECTED_A') {
                _rangeSelect = { mode: 'IDLE', startIdx: null, startFreq: null, startSymbol: null };
            }
            document.getElementById("stock-history").classList.remove("show");
            document.getElementById("loading").classList.remove("hidden");
            const FUTURES_ALIAS_KEYS = new Set(["IF","IH","IC","IM","T","TF","TL","TS","CU","AL","ZN","PB","NI","SN","AO","AU","AG","RB","WR","HC","SS","BU","RU","FU","SP","BR","M","Y","A","B","P","J","JM","I","C","CS","L","V","PP","EG","EB","PG","FB","BB","RR","LH","JD","TA","PTA","MA","FG","SA","SR","CF","CY","OI","RM","ZC","UR","PF","PK","AP","CJ","SM","SF","SH","PX","LR","RI","JR","WH","PM","RS","SC","LU","NR","BC","EC","SI","LC","PS"]);
            const isFuturesCode = code.includes('KQ.m@') || code.includes('KQ.i@') || /^[A-Z]+\.[A-Z]/.test(code) || FUTURES_ALIAS_KEYS.has(code.toUpperCase());
            // 判断切换前是否为期指
            const wasFutures = chartData && chartData.meta && chartData.meta.market === 'futures';
            // 同类继承上一周期，异类使用默认周期
            let fetchFreq;
            if (wasFutures && isFuturesCode) {
                // 期指C → 期指D：保持C的周期
                fetchFreq = lastFuturesFreq;
            } else if (!wasFutures && !isFuturesCode) {
                // 股票A → 股票B：保持A的周期
                fetchFreq = lastStockFreq;
            } else if (wasFutures && !isFuturesCode) {
                // 期指 → 股票：默认日K，同时彻底清理所有期货数据
                disconnectRealtime();
                fetch("/api/futures_cleanup").catch(() => {});
                fetchFreq = 'd';
            } else {
                // 股票 → 期指：默认1分钟
                fetchFreq = '1m';
            }
            currentFreq = fetchFreq;
            updateDateInputType();
            if (isFuturesCode) {
                updateFreqButtonStates(true); // 期货：禁用 d/w，启用 1m/15s
                connectRealtimeInit(code, fetchFreq);
                return;
            }
            updateFreqButtonStates(false); // 股票：禁用 1m/15s，启用 d/w
            fetch("/api/stock?code=" + encodeURIComponent(code) + "&freq=" + fetchFreq + (isDualWindow && getDualBottomFreq(fetchFreq) ? "&dual=1" : ""))
                .then(resp => {
                    if (!resp.ok) return resp.json().then(e => { throw new Error(e.error || "查询失败"); });
                    return resp.json();
                })
                .then(data => {
                    saveHistory(code, data.meta.name);
                    chartData = data;
                    updateRestartBtn();
                    updateDualBtn();
                    // 根据返回数据的周期同步按钮状态
                    let returnedFreq;
                    if (data.meta.freq === "5分钟") {
                        returnedFreq = "5m";
                    } else if (data.meta.freq === "30分钟") {
                        returnedFreq = "30m";
                    } else if (data.meta.freq === "周线") {
                        returnedFreq = "w";
                    } else {
                        returnedFreq = "d";
                    }
                    currentFreq = returnedFreq;
                    lastStockFreq = currentFreq; // 更新股票周期记忆
                    updateDateInputType();
                    updateFreqButtonStates(false);
                    viewCount = 377;
                    adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                    viewOffset = Math.max(0, chartData.klines.length - viewCount);
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    // K线不足一屏时右对齐
                    if (chartData.klines.length < viewCount) {
                        viewOffset = 0;
                    }
                    // 保持当前开关状态，不重置（切换个股时继承）
                    document.getElementById("btn-fx").classList.toggle("active", showFx);
                    document.getElementById("btn-bi").classList.toggle("active", showBi);
                    document.getElementById("btn-zs").classList.toggle("active", showZs);
                    document.getElementById("btn-seg").classList.toggle("active", showSeg);
                    document.getElementById("btn-bsp").classList.toggle("active", showBsp);
                    document.getElementById("stock-name").textContent = chartData.meta.name;
                    document.getElementById("stock-code").textContent = chartData.meta.symbol;
                    document.title = "缠论分析 - " + chartData.meta.name;
                    const lastDate = klineDateToInput(chartData.klines[chartData.klines.length - 1].date, currentFreq);
                    document.getElementById("goto-date-input").value = lastDate;
                    updateWeekday();
                    resizeCanvas();
                    // 双窗口模式下同时加载下面窗口数据
                    if (isDualWindow) {
                        const bottomFreq = getDualBottomFreq(currentFreq);
                        if (bottomFreq) {
                            dualBottomFreq = bottomFreq;
                            if (data.sub) {
                                dualBottomData = data.sub;
                                dualBottomViewCount = 377;
                                dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                                if (dualBottomData.klines.length < dualBottomViewCount) {
                                    dualBottomViewOffset = 0;
                                }
                            }
                        }
                    }
                    document.getElementById("loading").classList.add("hidden");
                    render();
                    generateStats();
                    loadAnnotations();
                    saveLastState(); // 保存股票状态
                    // 期货/期指：切换到实时模式
                    startRealtimeIfFutures(data);
                })
                .catch(err => {
                    alert("查询失败: " + err.message);
                    document.getElementById("loading").classList.add("hidden");
                });
        };

        window.selectHistory = function(code) {
            document.getElementById("stock-code-input").value = code;
            document.getElementById("stock-history").classList.remove("show");
            window.loadStock();
        };

        // ========== 期货实时模式 ==========
        function startRealtimeIfFutures(data) {
            // 检查是否是期货/期指品种（股票路径中用于断开SSE，期货路径中用于连SSE）
            const isFutures = data.meta.market === 'futures';
            const badge = document.getElementById('realtime-badge');

            if (isFutures) {
                const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d','周线':'w'};
                if (data.meta.freq) {
                    currentFreq = freqMap[data.meta.freq] || currentFreq;
                }
                lastFuturesFreq = currentFreq; // 记录期货周期
                updateFreqButtonStates(true);
                connectRealtime(data.meta.symbol);
            } else {
                disconnectRealtime();
                updateFreqButtonStates(false);
            }
        }

        // ========== SSE 初始化连接（初始快照 + 增量合一） ==========
        function tryReconnect(callback, delayMs) {
            if (reconnectCount >= MAX_RECONNECT) {
                console.warn('重连已达上限(' + MAX_RECONNECT + '次)，放弃重连');
                realtimeStopped = true;
                isRealtimeMode = false;
                if (realtimeEventSource) {
                    realtimeEventSource.close();
                    realtimeEventSource = null;
                }
                const badge = document.getElementById('realtime-badge');
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
                return;
            }
            reconnectCount++;
            if (reconnectTimer) clearTimeout(reconnectTimer);
            reconnectTimer = setTimeout(() => {
                reconnectTimer = null;
                callback();
            }, delayMs);
        }

        function connectRealtimeInit(symbol, freq, startTime) {
            disconnectRealtime();
            realtimeStopped = false;  // 用户主动操作，允许重连
            reconnectCount = 0; // 用户主动操作，重置重连计数
            realtimeSymbol = symbol;
            realtimeFreq = freq;
            realtimeStartTime = startTime || null;
            isRealtimeMode = true;
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';
            // loading 由调用方（loadStock/switchFreq）已设置

            try {
                let sseUrl = '/api/futures_stream?symbol=' + encodeURIComponent(symbol) + '&freq=' + encodeURIComponent(freq || '1m');
                if (startTime) {
                    sseUrl += '&start_time=' + encodeURIComponent(startTime);
                }
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                // init 事件：初始全量快照
                realtimeEventSource.addEventListener('init', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.error) {
                            console.warn('引擎未就绪，2秒后重试:', data.error);
                            disconnectRealtime();
                            tryReconnect(() => {
                                if (realtimeSymbol === symbol) {
                                    connectRealtimeInit(symbol, freq, realtimeStartTime);
                                }
                            }, 2000);
                            return;
                        }
                        // 全量初始数据
                        chartData = data;
                        // 用后端解析后的完整代码保存历史，避免别名导致历史记录不一致
                        const resolvedSymbol = data.meta.symbol || symbol;
                        saveHistory(resolvedSymbol, data.meta.name);
                        // 同步 realtimeSymbol 为解析后的完整代码
                        realtimeSymbol = resolvedSymbol;
                        // 更新输入框为解析后的完整代码
                        document.getElementById("stock-code-input").value = resolvedSymbol;
                        updateRestartBtn();
                        updateDualBtn();
                        // 同步周期
                        const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d','周线':'w'};
                        currentFreq = freqMap[data.meta.freq] || freq;
                        lastFuturesFreq = currentFreq; // 更新期货周期记忆
                        updateFreqButtonStates(true);
                        viewCount = 377;
                        adjustViewForSavedPoint(); // 有选点时动态调整，显示全部K线
                        viewOffset = Math.max(0, data.klines.length - viewCount);
                        if (data.klines.length < viewCount) viewOffset = 0;
                        document.getElementById("stock-name").textContent = data.meta.name;
                        document.getElementById("stock-code").textContent = data.meta.symbol;
                        document.title = "缠论分析 - " + data.meta.name;
                        if (data.klines.length > 0) {
                            const lastDate = klineDateToInput(data.klines[data.klines.length - 1].date, currentFreq);
                            document.getElementById("goto-date-input").value = lastDate;
                        }
                        updateWeekday();
                        document.getElementById("loading").classList.add("hidden");
                        resizeCanvas();
                        render();
                        generateStats();
                    } catch(e) {
                        console.error('初始数据解析失败:', e);
                        document.getElementById("loading").classList.add("hidden");
                    }
                });

                // update 事件：增量更新
                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeDataSingle(data);
                    } catch(e) {
                        console.error('实时数据解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    if (realtimeStopped) return; // 已放弃重连，忽略后续事件
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
                    tryReconnect(() => {
                        if (isRealtimeMode && realtimeSymbol) {
                            connectRealtime(realtimeSymbol, realtimeFreq, realtimeStartTime);
                        }
                    }, 5000);
                };

                realtimeEventSource.onopen = function() {
                    realtimeConnected = true;
                    badge.classList.remove('stopped');
                    badge.textContent = '● 实时';
                };
            } catch(e) {
                console.error('SSE连接失败:', e);
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
                document.getElementById("loading").classList.add("hidden");
                // 3秒后重试
                tryReconnect(() => {
                    if (realtimeSymbol === symbol) {
                        document.getElementById("loading").classList.remove("hidden");
                        connectRealtimeInit(symbol, freq, realtimeStartTime);
                    }
                }, 3000);
            }
        }

        // 期货双窗口SSE连接（独立于 connectRealtimeInit，与股票双窗口解耦）
        function connectRealtimeDual(symbol, topFreq, bottomFreq) {
            disconnectRealtime();
            realtimeSymbol = symbol;
            realtimeFreq = topFreq;
            dualBottomFreq = bottomFreq;
            isRealtimeMode = true;
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';

            try {
                let sseUrl = '/api/futures_stream?symbol=' + encodeURIComponent(symbol)
                    + '&freq=' + topFreq + '&dual=1&bottom_freq=' + bottomFreq;
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                realtimeEventSource.addEventListener('init', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        if (data.main) {
                            chartData = data.main;
                            viewOffset = Math.max(0, chartData.klines.length - 377);
                            viewCount = 377;
                            if (chartData.klines.length < 377) { viewOffset = 0; viewCount = chartData.klines.length; }
                        }
                        if (data.sub) {
                            dualBottomData = data.sub;
                            dualBottomViewCount = 377;
                            dualBottomViewOffset = Math.max(0, dualBottomData.klines.length - dualBottomViewCount);
                            if (dualBottomData.klines.length < dualBottomViewCount) {
                                dualBottomViewOffset = 0;
                            }
                        }
                        document.getElementById("loading").classList.add("hidden");
                        document.querySelector(".loading-text").textContent = "正在加载K线数据...";
                        updateFreqButtonStates(true);
                        render();
                    } catch (e) {
                        console.error('双窗口init解析失败:', e);
                    }
                });

                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeDataDual(data);
                    } catch (e) {
                        console.error('双窗口update解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    if (realtimeStopped) return;
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
                    tryReconnect(() => {
                        if (isRealtimeMode && realtimeSymbol && isDualWindow) {
                            connectRealtimeDual(realtimeSymbol, realtimeFreq, dualBottomFreq);
                        }
                    }, 5000);
                };

                realtimeEventSource.onopen = function() {
                    realtimeConnected = true;
                    badge.classList.remove('stopped');
                    badge.textContent = '● 实时';
                };
            } catch (e) {
                console.error('双窗口SSE连接失败:', e);
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
                document.getElementById("loading").classList.add("hidden");
                tryReconnect(() => {
                    if (realtimeSymbol === symbol && isDualWindow) {
                        document.getElementById("loading").classList.remove("hidden");
                        connectRealtimeDual(symbol, topFreq, bottomFreq);
                    }
                }, 3000);
            }
        }

        function connectRealtime(symbol, freq, startTime) {
            freq = freq || currentFreq || '1m';
            // 断开旧连接
            disconnectRealtime();
            realtimeSymbol = symbol;
            realtimeFreq = freq;
            realtimeStartTime = startTime || null;
            isRealtimeMode = true;
            const badge = document.getElementById('realtime-badge');
            badge.classList.add('visible');
            badge.classList.remove('stopped');
            badge.textContent = '● 实时';

            try {
                let sseUrl = '/api/futures_stream?symbol=' + encodeURIComponent(symbol) + '&freq=' + encodeURIComponent(freq);
                if (startTime) {
                    sseUrl += '&start_time=' + encodeURIComponent(startTime);
                }
                realtimeEventSource = new EventSource(sseUrl);
                realtimeConnected = true;

                // 只监听 update 事件（重连不处理 init，避免覆盖已有数据）
                realtimeEventSource.addEventListener('update', function(event) {
                    try {
                        const data = JSON.parse(event.data);
                        handleRealtimeDataSingle(data);
                    } catch(e) {
                        console.error('实时数据解析失败:', e);
                    }
                });

                realtimeEventSource.onerror = function() {
                    if (realtimeStopped) return; // 已放弃重连，忽略后续事件
                    realtimeConnected = false;
                    badge.classList.add('stopped');
                    badge.textContent = '● 断开';
                    // 5秒后尝试重连
                    tryReconnect(() => {
                        if (isRealtimeMode && realtimeSymbol) {
                            connectRealtime(realtimeSymbol, realtimeFreq, realtimeStartTime);
                        }
                    }, 5000);
                };

                realtimeEventSource.onopen = function() {
                    realtimeConnected = true;
                    badge.classList.remove('stopped');
                    badge.textContent = '● 实时';
                };
            } catch(e) {
                console.error('SSE连接失败:', e);
                badge.classList.add('stopped');
                badge.textContent = '● 离线';
            }
        }

        function disconnectRealtime() {
            isRealtimeMode = false;
            realtimeSymbol = null;
            realtimeFreq = null;
            realtimeStartTime = null;
            if (realtimeEventSource) {
                realtimeEventSource.close();
                realtimeEventSource = null;
            }
            realtimeStopped = false; // 断开时重置标志
            if (reconnectTimer) {
                clearTimeout(reconnectTimer);
                reconnectTimer = null;
            }
            realtimeConnected = false;
            const badge = document.getElementById('realtime-badge');
            badge.classList.remove('visible', 'stopped');
        }

        function handleRealtimeDataSingle(data) {
            if (!isRealtimeMode || !data || !data.klines) return;
            // 保存当前开关状态
            const savedShowBi = showBi, savedShowFx = showFx, savedShowMa = showMa;
            const savedShowZs = showZs, savedShowSeg = showSeg, savedShowBsp = showBsp;

            // 保存用户当前的缩放和位置
            const oldKlinesCount = chartData && chartData.klines ? chartData.klines.length : 0;
            const savedViewCount = viewCount;
            const savedViewOffset = viewOffset;
            const wasAtRightEdge = (savedViewOffset + savedViewCount >= oldKlinesCount);

            // 更新图表数据
            chartData = data;

            // 更新元信息
            document.getElementById('stock-name').textContent = data.meta.name;
            document.getElementById('stock-code').textContent = data.meta.symbol;
            document.title = "缠论分析 - " + data.meta.name;
            if (data.meta.freq) {
                const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d'};
                currentFreq = freqMap[data.meta.freq] || currentFreq;
            }

            // 同步 freq 按钮状态
            updateFreqButtonStates(true);

            // 保持用户缩放不变：如果在最右端，左减一右加一；否则原地不动
            const newKlinesCount = data.klines.length;
            const delta = newKlinesCount - oldKlinesCount;
            viewCount = savedViewCount;
            if (wasAtRightEdge && delta > 0) {
                viewOffset = Math.max(0, savedViewOffset + delta);
            } else {
                viewOffset = savedViewOffset;
            }

            // 重绘
            updateSlider();
            resizeCanvas();
            render();
            updateRestartBtn();
            updateDualBtn();
        }

        function handleRealtimeDataDual(data) {
            if (!isRealtimeMode || !data) return;
            // 保存当前开关状态
            const savedShowBi = showBi, savedShowFx = showFx, savedShowMa = showMa;
            const savedShowZs = showZs, savedShowSeg = showSeg, savedShowBsp = showBsp;

            if (data.main) {
                // 保存用户当前的缩放和位置
                const oldMainCount = chartData && chartData.klines ? chartData.klines.length : 0;
                const savedViewCount = viewCount;
                const savedViewOffset = viewOffset;
                const wasAtRightEdge = (savedViewOffset + savedViewCount >= oldMainCount);

                chartData = data.main;

                // 更新元信息
                if (data.main.meta) {
                    document.getElementById('stock-name').textContent = data.main.meta.name || '';
                    document.getElementById('stock-code').textContent = data.main.meta.symbol || '';
                    if (data.main.meta.freq) {
                        const freqMap = {'15秒':'15s','1分钟':'1m','5分钟':'5m','30分钟':'30m','日线':'d'};
                        currentFreq = freqMap[data.main.meta.freq] || currentFreq;
                    }
                }

                // 保持用户缩放不变：如果在最右端，左减一右加一
                const newMainCount = data.main.klines ? data.main.klines.length : 0;
                const delta = newMainCount - oldMainCount;
                viewCount = savedViewCount;
                if (wasAtRightEdge && delta > 0) {
                    viewOffset = Math.max(0, savedViewOffset + delta);
                } else {
                    viewOffset = savedViewOffset;
                }
            }
            if (data.sub) {
                // 保存子窗口的缩放和位置
                const oldSubCount = dualBottomData && dualBottomData.klines ? dualBottomData.klines.length : 0;
                const savedSubCount = dualBottomViewCount || 377;
                const savedSubOffset = dualBottomViewOffset || 0;
                const wasSubAtRightEdge = (savedSubOffset + savedSubCount >= oldSubCount);

                dualBottomData = data.sub;

                const newSubCount = data.sub.klines ? data.sub.klines.length : 0;
                const subDelta = newSubCount - oldSubCount;
                dualBottomViewCount = savedSubCount;
                if (wasSubAtRightEdge && subDelta > 0) {
                    dualBottomViewOffset = Math.max(0, savedSubOffset + subDelta);
                } else {
                    dualBottomViewOffset = savedSubOffset;
                }
            }
            updateSlider();
            resizeCanvas();
            render();
        }

        // 页面关闭时清理 SSE
        window.addEventListener('beforeunload', function() {
            disconnectRealtime();
        });

        function generateStats() {
            if (!chartData) return;
            const klines = getVisibleKlines();
            if (!klines.length) return;
            const startDate = klines[0].date, endDate = klines[klines.length - 1].date;
            const visBis = chartData.bis.filter(bi => bi.sdt >= startDate && bi.edt <= endDate);
            const visFxs = chartData.fxs.filter(fx => fx.date >= startDate && fx.date <= endDate);
            const allBis = chartData.bis, allFxs = chartData.fxs;
            let visUp = 0, visDown = 0, totalPower = 0, maxPower = 0;
            visBis.forEach(bi => { if (bi.direction === "up") visUp++; else visDown++; totalPower += bi.power; if (bi.power > maxPower) maxPower = bi.power; });
            const avgPower = visBis.length > 0 ? (totalPower / visBis.length).toFixed(2) : 0;
            let allUp = 0, allDown = 0;
            allBis.forEach(bi => { if (bi.direction === "up") allUp++; else allDown++; });
            document.getElementById("stats-content").innerHTML = `
                <div class="stats-row"><span class="stats-label">可见笔数</span><span class="stats-value">${visBis.length} / ${allBis.length}</span></div>
                <div class="stats-row"><span class="stats-label">向上笔</span><span class="stats-value" style="color:#FF4444">${visUp} / ${allUp}</span></div>
                <div class="stats-row"><span class="stats-label">向下笔</span><span class="stats-value" style="color:#00DD00">${visDown} / ${allDown}</span></div>
                <div class="stats-row"><span class="stats-label">平均力度</span><span class="stats-value">${avgPower}</span></div>
                <div class="stats-row"><span class="stats-label">最大力度</span><span class="stats-value" style="color:#FFD700">${maxPower.toFixed(2)}</span></div>
                <div class="stats-row"><span class="stats-label">顶分型</span><span class="stats-value" style="color:#FF4444">${visFxs.filter(f=>f.mark==="G").length} / ${allFxs.filter(f=>f.mark==="G").length}</span></div>
                <div class="stats-row"><span class="stats-label">底分型</span><span class="stats-value" style="color:#00DD00">${visFxs.filter(f=>f.mark==="D").length} / ${allFxs.filter(f=>f.mark==="D").length}</span></div>`;
        }

        function updateSlider() {
            // 双窗口模式下，使用激活窗口的数据
            const data = (isDualWindow && activeDualWindow === 'bottom' && dualBottomData) ? dualBottomData : chartData;
            const vo = (isDualWindow && activeDualWindow === 'bottom') ? dualBottomViewOffset : viewOffset;
            const vc = (isDualWindow && activeDualWindow === 'bottom') ? dualBottomViewCount : viewCount;
            if (!data || !data.klines.length) return;
            const track = document.getElementById("slider-track");
            const win = document.getElementById("slider-window");
            const label = document.getElementById("slider-label");
            const totalKlines = data.klines.length;
            const trackWidth = track.clientWidth;
            if (trackWidth <= 0) return;

            const windowWidth = Math.max(10, (vc / totalKlines) * trackWidth);
            const maxOffset = Math.max(0, totalKlines - vc);
            const windowLeft = (vo / totalKlines) * trackWidth;

            win.style.width = windowWidth + "px";
            win.style.left = Math.max(0, Math.min(windowLeft, trackWidth - windowWidth)) + "px";

            const displayCount = Math.round(vc);
            const displayOffset = Math.round(vo);
            const startIdx = Math.max(0, displayOffset);
            const endIdx = Math.min(totalKlines - 1, startIdx + displayCount - 1);
            const startDate = data.klines[startIdx].date.slice(0, 10);
            const endDate = data.klines[endIdx].date.slice(0, 10);
            const globalStart = Math.max(0, Math.floor(vo));
            const globalEnd = Math.min(totalKlines, globalStart + vc);
            const visBis = data.bis.filter(bi => {
                const si = data.klines.findIndex(k => k.date === bi.sdt);
                return si >= globalStart && si < globalEnd;
            });
            const visFxs = data.fxs.filter(fx => {
                const fi = data.klines.findIndex(k => k.date === fx.date);
                return fi >= globalStart && fi < globalEnd;
            });
            const visZs = data.zs.filter(zs => {
                const si = data.klines.findIndex(k => k.date === zs.sdt);
                return si >= globalStart && si < globalEnd;
            });
            const winLabel = isDualWindow ? (activeDualWindow === 'bottom' ? '[下窗] ' : '[上窗] ') : '';
            label.textContent = winLabel + startDate + " - " + endDate + "   [K线]: " + displayCount + "/" + totalKlines + "   [分型]: " + visFxs.length + "/" + data.fxs.length + "   [笔]: " + visBis.length + "/" + data.bis.length + "   [中枢]: " + visZs.length + "/" + data.zs.length;
        }

        (function() {
            const slider = document.getElementById("range-slider");
            const track = document.getElementById("slider-track");
            const win = document.getElementById("slider-window");
            const handleLeft = document.getElementById("slider-handle-left");
            const handleRight = document.getElementById("slider-handle-right");
            let sliderDragging = false;
            let dragType = null;
            let dragStartX = 0, dragStartOffset = 0, dragStartCount = 0, dragStartRightEdge = 0;

            // 获取当前激活窗口的 data
            function getActiveData() {
                if (isDualWindow && activeDualWindow === 'bottom' && dualBottomData) {
                    return dualBottomData;
                }
                return chartData;
            }
            // 获取当前激活窗口的 viewOffset
            function getActiveViewOffset() {
                if (isDualWindow && activeDualWindow === 'bottom') {
                    return dualBottomViewOffset;
                }
                return viewOffset;
            }
            // 设置当前激活窗口的 viewOffset
            function setActiveViewOffset(v) {
                if (isDualWindow && activeDualWindow === 'bottom') {
                    dualBottomViewOffset = v;
                } else {
                    viewOffset = v;
                }
            }
            // 获取当前激活窗口的 viewCount
            function getActiveViewCount() {
                if (isDualWindow && activeDualWindow === 'bottom') {
                    return dualBottomViewCount;
                }
                return viewCount;
            }
            // 设置当前激活窗口的 viewCount
            function setActiveViewCount(v) {
                if (isDualWindow && activeDualWindow === 'bottom') {
                    dualBottomViewCount = v;
                } else {
                    viewCount = v;
                }
            }
            // 渲染当前激活窗口
            function renderActive() {
                updateActiveWindowClass();
                if (isDualWindow && activeDualWindow === 'bottom') {
                    // 直接渲染下面窗口，跳过 updateDualNewZs() 避免滑块操作时误清除红框新中枢
                    if (!dualBottomData || !bottomCtx) return;
                    const _savedCanvas = canvas, _savedCtx = ctx;
                    canvas = bottomCanvas; ctx = bottomCtx;
                    window._isRenderingBottom = true;
                    _renderChart(dualBottomData, dualBottomFreq, dualBottomViewOffset, dualBottomViewCount,
                        dualBottomMouseX, dualBottomMouseY, dualHighlightRange, dualRedRange);
                    window._isRenderingBottom = false;
                    canvas = _savedCanvas; ctx = _savedCtx;
                } else if (isDualWindow) {
                    renderTop();
                } else {
                    render();
                }
            }

            function getSliderInfo() {
                const data = getActiveData();
                const totalKlines = data ? data.klines.length : 1;
                const trackWidth = track.clientWidth;
                return { totalKlines, trackWidth };
            }

            handleLeft.addEventListener("mousedown", function(e) {
                e.preventDefault(); e.stopPropagation();
                sliderDragging = true; dragType = "left";
                dragStartX = e.clientX; dragStartCount = getActiveViewCount();
                dragStartRightEdge = getActiveViewOffset() + getActiveViewCount();
            });
            handleRight.addEventListener("mousedown", function(e) {
                e.preventDefault(); e.stopPropagation();
                sliderDragging = true; dragType = "right";
                dragStartX = e.clientX; dragStartCount = getActiveViewCount();
                dragStartOffset = getActiveViewOffset();
            });
            win.addEventListener("mousedown", function(e) {
                e.preventDefault(); e.stopPropagation();
                sliderDragging = true; dragType = "window";
                dragStartX = e.clientX; dragStartOffset = getActiveViewOffset();
            });
            track.addEventListener("mousedown", function(e) {
                const data = getActiveData();
                if (!data) return;
                e.preventDefault();
                const rect = track.getBoundingClientRect();
                const ratio = (e.clientX - rect.left) / rect.width;
                const totalKlines = data.klines.length;
                const vc = getActiveViewCount();
                const newOffset = ratio * totalKlines - vc / 2;
                setActiveViewOffset(Math.max(0, Math.min(totalKlines - vc, newOffset)));
                renderActive();
            });

            document.addEventListener("mousemove", function(e) {
                if (!sliderDragging || !getActiveData()) return;
                const { totalKlines, trackWidth } = getSliderInfo();
                if (trackWidth <= 0) return;
                const dx = e.clientX - dragStartX;
                const dk = (dx / trackWidth) * totalKlines;

                let vc = getActiveViewCount();
                let vo = getActiveViewOffset();
                if (dragType === "left") {
                    const newCount = Math.round(Math.max(3, Math.min(totalKlines, dragStartCount - dk)));
                    vc = newCount;
                    vo = Math.max(0, Math.round(dragStartRightEdge - vc));
                } else if (dragType === "right") {
                    const newCount = Math.round(Math.max(3, Math.min(totalKlines, dragStartCount + dk)));
                    const maxOffset = totalKlines - newCount;
                    vc = newCount;
                    vo = Math.min(vo, Math.max(0, maxOffset));
                } else if (dragType === "window") {
                    const newOffset = dragStartOffset + dk;
                    const maxOffset = totalKlines - vc;
                    vo = Math.max(0, Math.min(newOffset, maxOffset));
                }
                vc = Math.round(vc);
                vo = Math.round(vo);
                setActiveViewCount(vc);
                setActiveViewOffset(vo);
                renderActive();
            });

            document.addEventListener("mouseup", function() {
                sliderDragging = false; dragType = null;
            });
        })();

        // ============================================================
        // 文字标注功能
        // ============================================================

        // 加载标注数据
        function loadAnnotations() {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/annotations?code=" + encodeURIComponent(code) + "&freq=" + freq)
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    annotations = data.annotations || [];
                    render();
                })
                .catch(function() { annotations = []; });
        }

        // 保存标注到后端
        function saveAnnotationToServer(dateStr, text, yOffset) {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/annotations", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action: "add",
                    code: code,
                    freq: freq,
                    date: dateStr,
                    text: text,
                    y_offset: yOffset || 0
                })
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.ok) {
                    // 添加到本地缓存
                    annotations.push({ date: dateStr, text: text, y_offset: yOffset || 0 });
                    render();
                }
            })
            .catch(function(err) { console.error("保存标注失败:", err); });
        }

        // 删除标注
        function deleteAnnotationFromServer(dateStr, text) {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/annotations", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action: "delete",
                    code: code,
                    freq: freq,
                    date: dateStr,
                    text: text
                })
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.ok) {
                    // 先本地移除，立即刷新界面
                    annotations = annotations.filter(function(a) {
                        return !(a.date === dateStr && a.text === text);
                    });
                    render();
                    // 再从服务器重新加载，确保与后端真实状态完全一致
                    loadAnnotations();
                } else {
                    // 后端未找到匹配标注：标注可能存在于其他周期下
                    // 重新加载以同步本地状态，并提示用户
                    console.warn("[标注] 后端未找到匹配标注(code=" + code + ", freq=" + freq + ")，重新加载标注数据");
                    loadAnnotations();
                    alert("未找到该标注，可能标注存在于其他周期下。\n当前周期: " + freq + "\n请切换到添加标注时使用的周期再试。");
                }
            })
            .catch(function(err) { console.error("删除标注失败:", err); });
        }

        // 右键菜单处理
        function onContextMenu(e) {
            e.preventDefault();
            if (!chartData) return;

            // 双窗口模式下，只在上面窗口支持标注
            if (isDualWindow && window._isRenderingBottom) return;

            const rect = canvas.getBoundingClientRect();
            const clickX = e.clientX - rect.left;
            const clickY = e.clientY - rect.top;

            // 确定点击的K线
            const area = getChartArea();
            const klines = getVisibleKlines();
            if (!klines.length) return;
            const barStep = area.w / (klines.length < viewCount ? klines.length : viewCount);
            const subPixelOffset = (viewOffset - Math.floor(viewOffset)) * barStep;
            const idx = Math.floor((clickX - area.x + subPixelOffset) / barStep);
            if (idx < 0 || idx >= klines.length) return;
            const k = klines[idx];
            if (!k) return;
            // 检查是否在K线主图区域内
            if (clickY < area.y || clickY > area.y + area.h) return;

            _annotationTargetDate = k.date;
            _annotationTargetX = e.clientX;
            _annotationTargetY = clickY;
            _annotationClickTarget = null;

            // 检测点击是否在某个标注的方框区域内
            const priceRange = getPriceRange(klines);
            const dateToIdx = {};
            for (let i = 0; i < klines.length; i++) { dateToIdx[klines[i].date] = i; }
            for (let i = 0; i < annotations.length; i++) {
                const ann = annotations[i];
                const annIdx = dateToIdx[ann.date];
                if (annIdx === undefined) continue;
                const annK = klines[annIdx];
                const annX = area.x + barStep * annIdx + barStep / 2 - subPixelOffset;
                const annY = ann.y_offset || (priceToY(annK.high, area, priceRange) - 8);
                const layout = getAnnotationLayout(ann, annX, annY, area);
                if (clickX >= layout.boxX && clickX <= layout.boxX + layout.boxW &&
                    clickY >= layout.boxY && clickY <= layout.boxY + layout.boxH) {
                    _annotationClickTarget = ann;
                    break;
                }
            }

            // 显示菜单
            const menu = document.getElementById("annotation-menu");
            const menuDeleteOne = document.getElementById("annotation-menu-delete-one");
            const menuEditOne = document.getElementById("annotation-menu-edit-one");
            const menuAdd = document.getElementById("annotation-menu-add");
            const menuReplay = document.getElementById("annotation-menu-replay");
            const menuDivider = document.getElementById("annotation-menu-divider");
            const menuDelAll = document.getElementById("annotation-menu-del-all");
            const menuDivider2 = document.getElementById("annotation-menu-divider2");
            const menuMirror = document.getElementById("annotation-menu-mirror");
            // 更新反转视图菜单项文字（显示当前状态）
            menuMirror.textContent = _isMirrorMode ? "取消反转" : "反转视图";
            if (_annotationClickTarget) {
                menuDeleteOne.style.display = "block";
                menuEditOne.style.display = "block";
                menuAdd.style.display = "none";
                menuReplay.style.display = "none";
                menuDivider.style.display = "none";
                menuDelAll.style.display = "none";
            } else {
                menuDeleteOne.style.display = "none";
                menuEditOne.style.display = "none";
                menuAdd.style.display = "block";
                menuReplay.style.display = "block";
                menuDivider.style.display = "block";
                menuDelAll.style.display = "block";
            }
            // 反转视图始终显示（与标注操作无关，是全局视图模式）
            menuDivider2.style.display = "block";
            menuMirror.style.display = "block";

            menu.style.left = e.clientX + "px";
            menu.style.top = e.clientY + "px";
            menu.classList.add("show");
        }

        // 关闭右键菜单（点击其他地方）
        document.addEventListener("click", function(e) {
            const menu = document.getElementById("annotation-menu");
            if (!menu.contains(e.target)) {
                menu.classList.remove("show");
            }
        });

        // 添加标注
        window.annotationAdd = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            _annotationDialogMode = "add";
            document.getElementById("annotation-dialog-title").textContent = "添加文字标注";
            document.getElementById("annotation-dialog-date").textContent = "K线日期: " + _annotationTargetDate;
            document.getElementById("annotation-dialog-input").value = "";
            document.getElementById("annotation-dialog").classList.add("show");
            setTimeout(function() {
                document.getElementById("annotation-dialog-input").focus();
            }, 100);
        };

        // 复盘到右键点击的K线日期（等价于在复盘日期输入框中输入该日期）
        window.annotationReplayToHere = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!_annotationTargetDate) return;
            var input = document.getElementById("goto-date-input");
            var dateStr;
            if (isIntradayFreq(currentFreq)) {
                // 日内周期：保留完整时间，转成 datetime-local 格式
                dateStr = klineDateToInput(_annotationTargetDate, currentFreq);
            } else {
                // 日K/周K：只取日期部分
                dateStr = _annotationTargetDate.slice(0, 10).replace(/\//g, "-");
                if (!/^\d{4}-\d{2}-\d{2}$/.test(dateStr)) {
                    alert("无法识别该K线日期: " + _annotationTargetDate);
                    return;
                }
            }
            // 若与当前日期相同，仍强制重新复盘（避免用户改了其他条件后无响应）
            input.value = dateStr;
            if (typeof updateWeekday === "function") updateWeekday();
            gotoDate();
        };

        // 切换反转视图模式（K线涨跌互换、MACD红绿互换、缠论结构镜像）
        // 保底策略：如果反图渲染出错，自动切回正图并从后端重新加载，确保正图永远正确
        window.toggleMirrorMode = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            var prevMode = _isMirrorMode;
            _isMirrorMode = !_isMirrorMode;
            try {
                render();
            } catch(e) {
                console.error("[反转视图] 渲染出错，自动恢复正图:", e);
                _isMirrorMode = false;
                // chartData 可能被镜像数据污染（_renderChart 中途异常未恢复），
                // 从后端重新加载（命中缓存仅 0.001s），彻底恢复正图
                try {
                    loadStock();
                } catch(e2) {
                    console.error("[反转视图] 恢复失败:", e2);
                }
            }
        };

        // 删除右键点击命中的标注
        window.annotationDeleteAnnotation = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!_annotationClickTarget) return;
            deleteAnnotationFromServer(_annotationClickTarget.date, _annotationClickTarget.text);
        };

        // 修改右键点击命中的标注
        window.annotationEditAnnotation = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!_annotationClickTarget) return;
            _annotationDialogMode = "edit";
            _annotationEditOldText = _annotationClickTarget.text;
            _annotationTargetDate = _annotationClickTarget.date;
            _annotationTargetY = _annotationClickTarget.y_offset || 0;
            document.getElementById("annotation-dialog-title").textContent = "修改文字标注";
            document.getElementById("annotation-dialog-date").textContent = "K线日期: " + _annotationClickTarget.date;
            document.getElementById("annotation-dialog-input").value = _annotationClickTarget.text;
            document.getElementById("annotation-dialog").classList.add("show");
            setTimeout(function() {
                var inp = document.getElementById("annotation-dialog-input");
                inp.focus();
                inp.setSelectionRange(inp.value.length, inp.value.length);
            }, 100);
        };

        // 删除当前股票/周期全部标注
        window.annotationDeleteAllGlobal = function() {
            document.getElementById("annotation-menu").classList.remove("show");
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            if (confirm("确定删除当前股票 (" + code + ") " + freq + " 周期下的全部标注吗？")) {
                fetch("/api/annotations", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({
                        action: "delete_all",
                        code: code,
                        freq: freq
                    })
                })
                .then(function(resp) { return resp.json(); })
                .then(function(data) {
                    if (data.ok) {
                        annotations = [];
                        render();
                    }
                })
                .catch(function(err) { console.error("删除全部标注失败:", err); });
            }
        };

        // 标注对话框键盘事件
        window.annotationDialogKeydown = function(e) {
            if (e.key === "Enter") {
                annotationDialogConfirm();
            } else if (e.key === "Escape") {
                annotationDialogCancel();
            }
        };

        // 标注对话框确认
        window.annotationDialogConfirm = function() {
            const text = document.getElementById("annotation-dialog-input").value.trim();
            if (!text) {
                alert("请输入标注文字");
                return;
            }
            document.getElementById("annotation-dialog").classList.remove("show");
            if (_annotationDialogMode === "edit" && _annotationEditOldText) {
                updateAnnotationOnServer(_annotationTargetDate, _annotationEditOldText, text, _annotationTargetY);
            } else {
                saveAnnotationToServer(_annotationTargetDate, text, _annotationTargetY);
            }
        };

        // 更新标注（修改模式：删除旧标注+添加新标注）
        function updateAnnotationOnServer(dateStr, oldText, newText, yOffset) {
            if (!chartData || !chartData.meta) return;
            const code = chartData.meta.symbol;
            const freq = currentFreq;
            fetch("/api/annotations", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({
                    action: "update",
                    code: code,
                    freq: freq,
                    date: dateStr,
                    old_text: oldText,
                    text: newText,
                    y_offset: yOffset || 0
                })
            })
            .then(function(resp) { return resp.json(); })
            .then(function(data) {
                if (data.ok) {
                    annotations = annotations.filter(function(a) {
                        return !(a.date === dateStr && a.text === oldText);
                    });
                    annotations.push({ date: dateStr, text: newText, y_offset: yOffset || 0 });
                    render();
                }
            })
            .catch(function(err) { console.error("更新标注失败:", err); });
        }

        // 标注对话框取消
        window.annotationDialogCancel = function() {
            document.getElementById("annotation-dialog").classList.remove("show");
        };

        // 计算标注框的布局信息（供绘制和命中检测共用）
        function getAnnotationLayout(ann, klineX, klineY, area) {
            const font = "bold 12px 'PingFang SC', 'Microsoft YaHei', sans-serif";
            ctx.font = font;
            const lineHeight = 16;
            const padX = 6, padY = 3;
            const maxCharsPerLine = 11;

            // 按每行最多11个字折行
            const lines = [];
            let remaining = ann.text;
            while (remaining.length > 0) {
                lines.push(remaining.substring(0, maxCharsPerLine));
                remaining = remaining.substring(maxCharsPerLine);
            }

            // 计算每行宽度，取最大
            let maxTextW = 0;
            const lineWidths = lines.map(function(line) {
                const w = ctx.measureText(line).width;
                if (w > maxTextW) maxTextW = w;
                return w;
            });

            const boxW = maxTextW + padX * 2;
            const boxH = lines.length * lineHeight + padY * 2;
            const boxY = klineY - boxH; // 框底对齐klineY

            // 居中：以K线X为中心
            let boxX = klineX - boxW / 2;

            // 边界修正：不超出视口
            if (boxX < area.x) {
                boxX = area.x;
            }
            if (boxX + boxW > area.x + area.w) {
                boxX = area.x + area.w - boxW;
            }

            return { lines: lines, lineWidths: lineWidths, maxTextW: maxTextW,
                     boxW: boxW, boxH: boxH, boxX: boxX, boxY: boxY,
                     lineHeight: lineHeight, padX: padX, padY: padY };
        }

        // 绘制标注文字
        function drawAnnotations(klines, area, priceRange, barStep, subPixelOffset) {
            if (!annotations || !annotations.length) return;
            const dateToIdx = {};
            for (let i = 0; i < klines.length; i++) {
                dateToIdx[klines[i].date] = i;
            }

            annotations.forEach(function(ann) {
                const idx = dateToIdx[ann.date];
                if (idx === undefined) return;
                const k = klines[idx];
                const kx = area.x + barStep * idx + barStep / 2 - subPixelOffset;
                const ky = ann.y_offset || (priceToY(k.high, area, priceRange) - 8);

                const layout = getAnnotationLayout(ann, kx, ky, area);

                // 绘制每行文字（无背景框，文字左对齐，白色加阴影）
                ctx.fillStyle = "#ffffff";
                ctx.textAlign = "left";
                ctx.textBaseline = "middle";
                ctx.font = "bold 12px 'PingFang SC', 'Microsoft YaHei', sans-serif";
                ctx.shadowColor = "rgba(0, 0, 0, 0.85)";
                ctx.shadowBlur = 3;
                for (let li = 0; li < layout.lines.length; li++) {
                    const lineX = layout.boxX + layout.padX;
                    const lineY = layout.boxY + layout.padY + layout.lineHeight * li + layout.lineHeight / 2;
                    ctx.fillText(layout.lines[li], lineX, lineY);
                }
                ctx.shadowColor = "transparent";
                ctx.shadowBlur = 0;
            });
            ctx.textBaseline = "alphabetic"; // 恢复默认基线
        }

        init();

        // 关闭/刷新页面时保存状态（仅股票，期货不保存）
        window.addEventListener('beforeunload', function() { saveLastState(); });
    })();
    </script>
</body>
</html>"""


# ============================================================
# 主函数
# ============================================================
def main():
    print("=" * 60)
    print("  缠论分析 - chan.py 版本")
    print("=" * 60)

    # 1. 加载上次查看的股票代码（持久化恢复），若不存在则使用默认值
    last_code, last_freq = _load_last_stock()
    if last_code:
        start_code = last_code
        start_freq = last_freq
        print(f"[stock][信息] 恢复上次股票: {last_code} (周期: {last_freq})")
    else:
        start_code = SYMBOL_CODE
        start_freq = "d"
        print(f"[stock][信息] 使用默认股票: {start_code}")

    result = analyze_stock(start_code, freq=start_freq)
    if "error" in result:
        print(f"[错误] {result['error']}")
        return

    # 2. 生成 HTML
    html_path = os.path.join(OUTPUT_DIR, "chan_chart.html")
    chart_data_json = json.dumps(result, ensure_ascii=False, allow_nan=False)
    chart_data_json = chart_data_json.replace("</script>", "<\\/script>")
    html_content = HTML_TEMPLATE.replace("%%CHART_DATA%%", chart_data_json)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    html_size = os.path.getsize(html_path)
    print(f"[stock][信息] HTML页面已生成: {html_path} ({html_size/1024/1024:.1f}MB)")

    # 3. 启动HTTP服务器（流通股本在扫描时按需加载，只加载自选股列表中的股票）
    port = 18081  # 使用18081，避免与czsc版本的18080冲突
    server = ThreadingHTTPServer(("127.0.0.1", port), ChartHandler)
    server_thread = threading.Thread(target=server.serve_forever, daemon=False)
    server_thread.start()
    url = f"http://127.0.0.1:{port}/chan_chart.html"
    print(f"[stock][信息] HTTP服务器已启动: {url}\n")
    print_memory("程序启动后")

    try:
        server_thread.join()
    except KeyboardInterrupt:
        server.shutdown()
        print("\n[stock][信息] 服务器已停止")

    print("=" * 60)


if __name__ == "__main__":
    multiprocessing.freeze_support()
    main()
