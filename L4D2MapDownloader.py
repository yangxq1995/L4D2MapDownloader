#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
L4D2 地图下载工具 (GUI版) - 改进版
功能：搜索地图、下载Steam工坊Mod、自动重命名
由 [lwyxb]来玩游戏吧 提供，51青年 提供支持

改进点（相比原版）:
1. 添加可视化进度条
2. 添加全选/取消全选按钮
3. 改进错误提示（关键错误弹窗）
4. 动态下载超时（根据地图大小）
5. 日志级别过滤
"""

import os
import re
import sys
import subprocess
import shutil
import threading
import queue
import time
import urllib.request
import webbrowser
import zipfile
from datetime import datetime
from typing import Optional, Dict, List, Callable

# ============================================================
# tkinter 依赖检查
# ============================================================
try:
    import tkinter as tk
    from tkinter import ttk, messagebox, filedialog
except ImportError:
    print("tkinter 未安装，无法启动GUI")
    sys.exit(1)

# ============================================================
# 全局路径
# ============================================================
if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
    SCRIPT_DIR = os.path.dirname(sys.executable)
else:
    SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

BASE_DIR = SCRIPT_DIR
STEAMCMD_DIR = os.path.join(BASE_DIR, "steamcmd")
STEAMCMD_EXE = os.path.join(STEAMCMD_DIR,
    "steamcmd.exe" if os.name == 'nt' else "steamcmd")
LOG_DIR = os.path.join(BASE_DIR, "logs")
CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

# ============================================================
# 数据库配置
# ============================================================
DB_CONFIG = {
    "host": "yangxq.cc",
    "port": 3306,
    "user": "lwyxb",
    "password": "lwyxb",
    "database": "lwyxb",
    "charset": "utf8mb4"
}

# ============================================================
# SteamCMD 后台下载状态（线程安全用锁保护）
# ============================================================
_steamcmd_lock = threading.Lock()
_steamcmd_downloading = False
_steamcmd_error = None
_steamcmd_ready = threading.Event()

# ============================================================
# 全局日志和 GUI 实例
# ============================================================
logger = None
app = None

# ============================================================
# 依赖检查
# ============================================================
try:
    import pymysql
except ImportError:
    pymysql = None

try:
    import requests
except ImportError:
    requests = None


# ============================================================
# 工具函数
# ============================================================

def decode_line(raw: bytes):
    """安全解码字节流，尝试多种编码"""
    for enc in ('utf-8', 'gbk', 'gb18030', 'latin-1'):
        try:
            return raw.decode(enc).strip()
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', errors='replace').strip()


def format_size(size_bytes: int) -> str:
    """格式化文件大小"""
    if size_bytes >= 1024 ** 3:
        return f"{size_bytes / 1024 ** 3:.1f}GB"
    elif size_bytes >= 1024:
        return f"{size_bytes / 1024 ** 2:.1f}MB"
    return f"{size_bytes}B"


def format_elapsed(delta) -> str:
    """格式化时间间隔"""
    if delta is None:
        return "-"
    total = int(delta.total_seconds())
    if total < 60:
        return f"{total}秒"
    elif total < 3600:
        return f"{total // 60}分{total % 60}秒"
    return f"{total // 3600}时{(total % 3600) // 60}分"


def ensure_dir(path: str) -> None:
    """确保目录存在"""
    if path and not os.path.exists(path):
        os.makedirs(path, exist_ok=True)


def parse_size_to_bytes(size_str: str) -> Optional[int]:
    """将大小字符串解析为字节数"""
    if not size_str or size_str == "未知":
        return None
    m = re.match(r'([0-9.,]+)\s*(KB|MB|GB)?', size_str, re.IGNORECASE)
    if not m:
        return None
    num = float(m.group(1).replace(',', ''))
    unit = (m.group(2) or 'MB').upper()
    multipliers = {'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}
    return int(num * multipliers.get(unit, 1024**2))


# ============================================================
# GUI 日志类（改进版 - 添加日志级别过滤）
# ============================================================

class GUILogger:
    """日志输出到 GUI 文本框 + 文件（支持级别过滤）"""

    def __init__(self, log_file: str, text_widget):
        self.log_file = log_file
        self.text_widget = text_widget
        self.start_time = datetime.now()
        self._lock = threading.Lock()
        
        # 日志级别过滤
        self._filter_level = 'ALL'  # ALL, INFO, WARNING, ERROR
        self._all_logs = []  # 保存所有日志用于过滤
        
        ensure_dir(os.path.dirname(log_file))
        self._write_header()

    def set_filter(self, level: str):
        """设置日志过滤级别"""
        self._filter_level = level
        self._refresh_display()

    def _refresh_display(self):
        """刷新显示（根据过滤级别）"""
        if not self.text_widget:
            return
        try:
            self.text_widget.configure(state='normal')
            self.text_widget.delete('1.0', 'end')
            for msg, tag in self._all_logs:
                if self._should_display(tag):
                    self.text_widget.insert('end', msg + '\n', tag)
            self.text_widget.see('end')
            self.text_widget.configure(state='disabled')
        except Exception:
            pass

    def _should_display(self, tag: str) -> bool:
        """判断是否应该显示该条日志"""
        if self._filter_level == 'ALL':
            return True
        if self._filter_level == 'ERROR':
            return tag in ('error',)
        if self._filter_level == 'WARNING':
            return tag in ('error', 'warning')
        if self._filter_level == 'INFO':
            return tag in ('error', 'warning', 'info', 'success')
        return True

    def _write_header(self):
        header = (
            f"{'=' * 70}\n"
            f"[lwyxb]第三方地图下载工具 - 运行日志\n"
            f"由 [lwyxb]来玩游戏吧-51青年提供数据服务\n"
            f"{'=' * 70}\n"
            f"开始时间: {self.start_time:%Y-%m-%d %H:%M:%S}\n"
            f"程序目录: {SCRIPT_DIR}\n"
            f"系统信息: {sys.platform} | Python {sys.version.split()[0]}\n"
            f"{'=' * 70}"
        )
        self._output(header)

    def _output(self, msg: str, tag: str = 'info'):
        ts = datetime.now().strftime('%H:%M:%S')
        line = f"[{ts}] {msg}"

        # 保存所有日志
        self._all_logs.append((line, tag))

        # 写文件
        try:
            with open(self.log_file, 'a', encoding='utf-8') as f:
                f.write(line + '\n')
        except OSError:
            pass

        # 写 GUI（线程安全）
        if self.text_widget:
            try:
                self.text_widget.after(0, self._append_text, line + '\n', tag)
            except Exception:
                pass

    def _append_text(self, text: str, tag: str = 'info'):
        try:
            if not self._should_display(tag):
                return
            self.text_widget.configure(state='normal')
            self.text_widget.insert('end', text, tag)
            self.text_widget.see('end')
            self.text_widget.configure(state='disabled')
        except Exception:
            pass

    def step(self, n: int, total: int, msg: str):
        self._output(f"[步骤 {n}/{total}] {msg}")

    def info(self, msg: str):
        self._output(f"[信息] {msg}", tag='info')

    def success(self, msg: str):
        self._output(f"[成功] {msg}", tag='success')

    def error(self, msg: str, show_dialog: bool = False):
        self._output(f"[错误] {msg}", tag='error')
        if show_dialog and app:
            app.root.after(0, lambda: messagebox.showerror("错误", msg))

    def warning(self, msg: str):
        self._output(f"[警告] {msg}", tag='warning')

    def debug(self, msg: str):
        self._output(f"[调试] {msg}")

    def separator(self):
        self._output('-' * 70)

    def close(self):
        elapsed = datetime.now() - self.start_time
        self._output(f"\n{'=' * 70}", tag='info')
        self._output(f"程序结束，总耗时: {elapsed}", tag='info')
        self._output(f"日志已保存至: {self.log_file}", tag='info')
        self._output(f"{'=' * 70}", tag='info')


# ============================================================
# 数据库
# ============================================================

def get_db():
    """获取数据库连接"""
    if pymysql is None:
        raise RuntimeError("pymysql 未安装")
    return pymysql.connect(**DB_CONFIG)


def query_maps_by_name(map_name: str):
    """按名称搜索地图"""
    try:
        conn = get_db()
        try:
            cursor = conn.cursor()
            pattern = f"%{map_name}%"
            cursor.execute(
                """SELECT modID, mapNameCN, mapAuthor, mapSize, updateTime, isUsing, gamemapsID, mapNameEN
                   FROM mapsinfo
                   WHERE (mapNameCN LIKE %s OR mapNameEN LIKE %s)
                     AND isValue != '1'
                   ORDER BY mapNameCN""",
                (pattern, pattern)
            )
            results = []
            for row in cursor.fetchall():
                size = row[3]
                if isinstance(size, (int, float)) and size > 0:
                    size_str = f"{size / 1024:.1f}GB" if size >= 1024 else f"{size:.1f}MB"
                elif isinstance(size, str) and size:
                    size_str = size
                else:
                    size_str = "未知"
                results.append({
                    "mod_id": row[0],
                    "map_name_cn": row[1],
                    "author": row[2] or "未知",
                    "size": size_str,
                    "update_time": str(row[4]) if row[4] else "未知",
                    "is_using": row[5],
                    "gamemaps_id": row[6] or "",
                    "map_name_en": row[7] or ""
                })
            return results
        finally:
            conn.close()
    except Exception as e:
        logger.error(f"数据库查询失败: {e}", show_dialog=True)
        return []


# ============================================================
# 文件检查与远程大小获取
# ============================================================

def get_map_size_from_db(mod_id: str):
    """从数据库查询地图大小（MB）"""
    try:
        conn = get_db()
        try:
            cursor = conn.cursor()
            cursor.execute("SELECT mapSize FROM mapsinfo WHERE modID=%s", (mod_id,))
            row = cursor.fetchone()
            if row and row[0]:
                size = row[0]
                if isinstance(size, (int, float)):
                    return int(size * 1024 * 1024)
                elif isinstance(size, str):
                    return parse_size_to_bytes(size)
        finally:
            conn.close()
    except Exception:
        pass
    return None


def get_steam_workshop_file_size(mod_id: str):
    """从 Steam 工坊页面获取文件大小"""
    if requests is None:
        return None
    try:
        url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod_id}"
        headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'}
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        m = re.search(r'workshopFileSize[^>]*>([0-9.,]+)\s*(KB|MB|GB)', resp.text)
        if m:
            size = float(m.group(1).replace(',', ''))
            unit = m.group(2).upper()
            multipliers = {'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}
            return int(size * multipliers.get(unit, 1))
    except Exception:
        pass
    return None


def check_existing_file(mod_id: str, map_name_cn: str):
    """检查本地是否已有文件，返回 (文件路径, 是否匹配大小, 文件大小)"""
    candidates = [
        os.path.join(BASE_DIR, f"{mod_id}.vpk"),
        os.path.join(BASE_DIR, f"{map_name_cn}.vpk") if map_name_cn else None,
        os.path.join(BASE_DIR, f"[战役]{map_name_cn}.vpk") if map_name_cn else None,
    ]
    existing = next((c for c in candidates if c and os.path.exists(c)), None)
    if not existing:
        return None, False, 0

    local_size = os.path.getsize(existing)
    logger.info(f"本地文件: {os.path.basename(existing)} ({format_size(local_size)})")

    # 尝试获取远程大小
    remote_size = get_map_size_from_db(mod_id)
    if remote_size is None:
        remote_size = get_steam_workshop_file_size(mod_id)
    if remote_size is None:
        # 无法获取远程大小时，假设本地文件可用
        return existing, True, local_size

    diff = abs(local_size - remote_size) / remote_size * 100 if remote_size else 100
    if diff < 5:
        logger.success(f"文件大小匹配（差异 {diff:.1}%），无需下载")
        return existing, True, local_size
    logger.warning(f"文件大小不匹配（差异 {diff:.1}%），需要重新下载")
    return existing, False, local_size


# ============================================================
# SteamCMD 管理
# ============================================================

def download_steamcmd_zip() -> bool:
    """下载 SteamCMD"""
    url = "https://steamcdn-a.akamaihd.net/client/installer/steamcmd.zip"
    zip_path = os.path.join(STEAMCMD_DIR, "steamcmd.zip")
    try:
        ensure_dir(STEAMCMD_DIR)
        logger.info("正在下载 SteamCMD...")
        urllib.request.urlretrieve(url, zip_path)
        logger.info("正在解压...")
        with zipfile.ZipFile(zip_path, 'r') as z:
            z.extractall(STEAMCMD_DIR)
        os.remove(zip_path)
        return os.path.exists(STEAMCMD_EXE)
    except Exception as e:
        logger.error(f"SteamCMD 下载失败: {e}")
        return False


def _download_steamcmd_bg():
    """后台线程：下载 SteamCMD"""
    global _steamcmd_downloading, _steamcmd_error
    try:
        logger.info("[后台] 开始下载 SteamCMD...")
        ok = download_steamcmd_zip()
        with _steamcmd_lock:
            _steamcmd_error = None if ok else "下载失败"
        if ok:
            logger.success("[后台] SteamCMD 安装完成")
        else:
            logger.error("[后台] SteamCMD 安装失败")
    except Exception as e:
        with _steamcmd_lock:
            _steamcmd_error = str(e)
        logger.error(f"[后台] SteamCMD 下载异常: {e}")
    finally:
        with _steamcmd_lock:
            _steamcmd_downloading = False
        _steamcmd_ready.set()


def ensure_steamcmd() -> bool:
    """确保 SteamCMD 可用（已下载则直接返回 True）"""
    global _steamcmd_downloading, _steamcmd_error

    if os.path.exists(STEAMCMD_EXE):
        _steamcmd_ready.set()
        return True

    with _steamcmd_lock:
        if not _steamcmd_downloading:
            _steamcmd_downloading = True
            _steamcmd_error = None
            threading.Thread(target=_download_steamcmd_bg, daemon=True).start()

    if not _steamcmd_ready.wait(timeout=300):
        logger.error("SteamCMD 下载超时")
        return False

    with _steamcmd_lock:
        if _steamcmd_error:
            logger.error(f"SteamCMD 不可用: {_steamcmd_error}")
            return False
    return True


# ============================================================
# 核心下载函数（改进版 - 动态超时）
# ============================================================

def download_mod_with_steamcmd(mod_id: str, progress_callback: Optional[Callable] = None, 
                               map_size_bytes: Optional[int] = None):
    """
    下载单个 Mod，使用独立安装目录以支持多线程并行
    
    Args:
        mod_id: Steam 工坊 ID
        progress_callback: 进度回调函数 (percent) -> None
        map_size_bytes: 地图大小（字节），用于动态调整超时时间
    """
    logger.separator()
    logger.info(f"开始下载 Mod: {mod_id}")

    if not ensure_steamcmd():
        logger.error("SteamCMD 不可用，无法下载")
        return False, None

    # 动态计算超时时间（根据地图大小）
    # 默认：每 100MB 给 5 分钟，最少 10 分钟，最多 60 分钟
    if map_size_bytes and map_size_bytes > 0:
        size_mb = map_size_bytes / (1024 * 1024)
        timeout_seconds = max(600, min(3600, int(size_mb / 100 * 300)))
    else:
        timeout_seconds = 1200  # 默认 20 分钟
    
    logger.info(f"下载超时设置: {timeout_seconds // 60} 分钟")

    # 每个 mod 使用独立安装目录
    install_rel = f"downloads/{mod_id}"
    install_abs = os.path.join(STEAMCMD_DIR, install_rel)
    content_dir = os.path.join(install_abs, "steamapps", "workshop", "content", "550", mod_id)
    download_dir = os.path.join(install_abs, "steamapps", "workshop", "downloads", "550", mod_id)
    logger.info(f"下载目标: {install_rel}")

    cmd = [
        STEAMCMD_EXE,
        "+force_install_dir", install_rel,
        "+login", "anonymous",
        "+workshop_download_item", "550", mod_id,
        "+quit"
    ]
    logger.info("正在执行 SteamCMD（首次运行可能需要更新，请耐心等待...）")

    # 启动进程
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=STEAMCMD_DIR,
        )
    except OSError as e:
        logger.error(f"SteamCMD 启动失败: {e}", show_dialog=True)
        return False, None

    # 独立线程读取 stdout，避免阻塞主循环
    out_q: queue.Queue = queue.Queue()

    def reader():
        while True:
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                break
            out_q.put(chunk)
        # 读完后再取剩余
        try:
            remaining = os.read(proc.stdout.fileno(), 65536)
            if remaining:
                out_q.put(remaining)
        except OSError:
            pass

    rd = threading.Thread(target=reader, daemon=True)
    rd.start()

    stdout_lines = []
    start = datetime.now()
    buf = b''
    timed_out = False

    while True:
        # 超时检测（使用动态超时）
        if (datetime.now() - start).total_seconds() > timeout_seconds:
            try:
                proc.kill()
            except OSError:
                pass
            logger.error(f"SteamCMD 执行超时（{timeout_seconds // 60}分钟）")
            timed_out = True
            break

        # 从队列取数据（非阻塞，最多等0.5秒）
        try:
            chunk = out_q.get(timeout=0.5)
        except queue.Empty:
            if proc.poll() is not None:
                break
            continue

        buf += chunk

        # 按行分割处理
        while b'\n' in buf:
            line_bytes, buf = buf.split(b'\n', 1)
            decoded = decode_line(line_bytes)
            if not decoded:
                continue

            stdout_lines.append(decoded)
            logger.info(f"[SteamCMD] {decoded[:200]}")

            # 进度解析
            percent = None
            for pat in (
                r'Progress:\s*[\d,]+\s*/\s*[\d,]+\s*\((\d+)%\)',     # Progress: x / y (74%)
                r'Progress:\s*([\d,]+)\s*/\s*([\d,]+)',               # Progress: x / y
                r'\[\s*(\d+)%\]',                                     # [ 74%]
            ):
                m = re.search(pat, decoded)
                if m:
                    if len(m.groups()) == 1:
                        percent = int(m.group(1))
                    else:
                        try:
                            a = int(m.group(1).replace(',', ''))
                            b = int(m.group(2).replace(',', ''))
                            if b > 0:
                                percent = int(a / b * 100)
                        except (ValueError, ZeroDivisionError):
                            pass
                    break

            # 里程碑回调
            if 'Downloading item' in decoded and progress_callback:
                progress_callback(0)
            if 'Success' in decoded and 'Downloaded item' in decoded and progress_callback:
                progress_callback(100)
            if percent is not None and progress_callback:
                progress_callback(percent)

    rd.join(timeout=3)

    # 获取进程返回值
    if timed_out:
        returncode = -1
    else:
        try:
            returncode = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            proc.kill()
            returncode = -1

    # 读取 stderr
    stderr = ""
    try:
        raw_err = b''
        while True:
            chunk = os.read(proc.stderr.fileno(), 4096)
            if not chunk:
                break
            raw_err += chunk
        stderr = decode_line(raw_err) or ""
    except OSError:
        pass

    stdout = '\n'.join(stdout_lines)

    # 检查结果
    success_keywords = "Success" in stdout or "Downloaded" in stdout
    has_files = os.path.exists(content_dir) or os.path.exists(download_dir) or os.path.exists(install_abs)

    if returncode != 0 and not success_keywords and not has_files:
        logger.error(f"SteamCMD 执行失败，返回码: {returncode}")
        if stderr:
            logger.error(f"错误信息: {stderr[:300]}")
        return False, None

    if success_keywords and not has_files:
        logger.warning("SteamCMD 报告成功但未找到下载目录，可能文件已存在")

    # 搜索 VPK 或 BIN 文件
    dst_vpk = os.path.join(BASE_DIR, f"{mod_id}.vpk")
    search_dirs = [content_dir, download_dir]
    if not (os.path.exists(content_dir) or os.path.exists(download_dir)):
        if os.path.exists(install_abs):
            logger.warning("标准目录不存在，搜索整个安装目录...")
            search_dirs = [install_abs]
        else:
            logger.error("安装目录不存在，下载可能未完成")
            return False, None

    for search_dir in search_dirs:
        if not os.path.exists(search_dir):
            continue
        for root, dirs, files in os.walk(search_dir):
            for f in files:
                if f.endswith('.vpk') or f.endswith('_legacy.bin'):
                    src = os.path.join(root, f)
                    try:
                        shutil.copy2(src, dst_vpk)
                        os.utime(dst_vpk, None)          # 设置修改时间为当前时间
                        fsize = os.path.getsize(dst_vpk)
                        logger.success(f"文件大小: {fsize:,} bytes ({format_size(fsize)})")
                        return True, dst_vpk
                    except OSError as e:
                        logger.error(f"复制文件失败: {e}")
                        return False, None

    logger.error("未找到 VPK 或 BIN 文件")
    # 列出目录结构用于调试
    for srch in search_dirs:
        if os.path.exists(srch):
            logger.info(f"目录内容: {srch}")
            for root, dirs, files in os.walk(srch):
                level = root.replace(srch, '').count(os.sep)
                for f in files[:5]:
                    logger.info(f"{'  ' * level}{f}")
    return False, None


# ============================================================
# 地图后处理
# ============================================================

def process_one_map(mod_id: str, map_name: str) -> dict:
    """将下载的 VPK 文件重命名为 [战役]xxx.vpk"""
    logger.separator()
    logger.info(f"处理地图: {map_name} (modID: {mod_id})")

    src = os.path.join(BASE_DIR, f"{mod_id}.vpk")
    if not os.path.exists(src):
        logger.error(f"VPK 文件不存在: {src}")
        return {"mod_id": mod_id, "map_name": map_name, "status": "失败", "reason": "VPK 文件不存在"}

    final_name = f"[战役]{map_name}.vpk"
    dst = os.path.join(BASE_DIR, final_name)

    if os.path.exists(dst):
        os.remove(dst)
        logger.info(f"已删除旧文件: {final_name}")

    try:
        shutil.move(src, dst)
        os.utime(dst, None)                          # 设置修改时间
        fsize = os.path.getsize(dst)
        logger.success(f"已重命名: {mod_id}.vpk -> {final_name}")
        logger.success(f"文件大小: {fsize:,} bytes ({format_size(fsize)})")
        # 检查文件大小是否异常（< 10MB）
        if fsize < 10 * 1024 * 1024:
            logger.warning(f"文件大小异常 ({fsize / 1024 / 1024:.1f}MB < 10MB)，删除文件")
            try:
                os.remove(dst)
            except OSError:
                pass
            return {"mod_id": mod_id, "map_name": map_name, "status": "失败", "reason": f"文件大小异常（{fsize / 1024 / 1024:.1f}MB < 10MB）"}
        return {"mod_id": mod_id, "map_name": map_name, "size_mb": fsize / 1024 / 1024, "status": "成功"}
    except OSError as e:
        logger.error(f"重命名文件失败: {e}", show_dialog=True)
        return {"mod_id": mod_id, "map_name": map_name, "status": "失败", "reason": str(e)}


# ============================================================
# 清理
# ============================================================

def cleanup_logs():
    """清理超过 10 个的旧日志文件"""
    if not os.path.exists(LOG_DIR):
        return
    try:
        files = [
            (os.path.join(LOG_DIR, f), os.path.getsize(os.path.join(LOG_DIR, f)), os.path.getmtime(os.path.join(LOG_DIR, f)))
            for f in os.listdir(LOG_DIR) if f.endswith('.txt')
        ]
    except OSError:
        return
    if not files:
        return
    total_mb = sum(s for _, s, _ in files) / 1024 / 1024
    if total_mb <= 1:
        return
    files.sort(key=lambda x: x[2], reverse=True)
    for path, _, _ in files[10:]:
        try:
            os.remove(path)
        except OSError:
            pass


def cleanup_steamcmd_downloads():
    """清理 SteamCMD 下载缓存"""
    path = os.path.join(STEAMCMD_DIR, "downloads")
    if not os.path.exists(path):
        return
    try:
        shutil.rmtree(path)
    except OSError:
        pass


# ============================================================
# 下载管理器（改进版 - 添加进度条支持）
# ============================================================

class DownloadManager:
    """多线程并行下载管理器"""

    def __init__(self, app):
        self.app = app
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._active = 0
        self._max_threads = 3
        self._total_enqueued = 0
        self._total_done = 0
        self._running_maps = []             # 当前下载中的地图名
        self._progress = {}                   # mod_id -> 百分比
        self._running_map_names = {}           # mod_id -> 中文名
        self._map_sizes = {}                   # mod_id -> 大小（字节）

    @property
    def is_running(self) -> bool:
        with self._lock:
            return self._active > 0 or not self._queue.empty()

    @property
    def queue_size(self) -> int:
        return self._queue.qsize()

    @property
    def active_count(self) -> int:
        with self._lock:
            return self._active

    def enqueue(self, maps: list):
        """加入下载队列"""
        with self._lock:
            for m in maps:
                self._queue.put(m)
                # 解析地图大小
                size_bytes = parse_size_to_bytes(m.get('size', ''))
                if m.get('mod_id'):
                    self._map_sizes[m['mod_id']] = size_bytes
            self._total_enqueued += len(maps)
        logger.info(f"已加入下载队列: {len(maps)} 个地图")
        self._spawn()

    def _spawn(self):
        """启动新下载线程（不超过最大并发数）"""
        while True:
            with self._lock:
                if self._active >= self._max_threads:
                    return
                if self._queue.empty():
                    return
                try:
                    m = self._queue.get_nowait()
                except queue.Empty:
                    return
                self._active += 1
                self._running_maps.append(m['map_name_cn'])
                self._running_map_names[m['mod_id']] = m['map_name_cn']
            threading.Thread(target=self._download_one, args=(m,), daemon=True).start()

    def _download_one(self, m: dict):
        """单个下载线程"""
        mod_id = m['mod_id']
        map_name = m['map_name_cn']
        start = datetime.now()

        logger.separator()
        logger.info(f"[下载] 开始: {map_name} (modID: {mod_id})")

        try:
            # 检查是否无工坊版本
            if (not mod_id or mod_id == '0') and m.get('gamemaps_id'):
                logger.warning(f"[下载] {map_name}: 该地图无 Steam 工坊版本，请前往 gameMaps 下载")
                gamemaps_id = m['gamemaps_id']
                url = f"https://www.gamemaps.com/details/{gamemaps_id}"
                logger.info(f"gameMaps 链接: {url}")
                webbrowser.open(url)
            elif (not mod_id or mod_id == '0'):
                logger.warning(f"[下载] {map_name}: 该地图无工坊版本，也没有 gameMaps 链接")
            else:
                existing, matched, _ = check_existing_file(mod_id, map_name)

                if existing and matched:
                    # 跳过下载，但可能需要复制/重命名
                    if existing != os.path.join(BASE_DIR, f"[战役]{map_name}.vpk"):
                        target = os.path.join(BASE_DIR, f"{mod_id}.vpk")
                        if existing != target:
                            try:
                                if os.path.exists(target):
                                    os.remove(target)
                                shutil.copy2(existing, target)
                                os.utime(target, None)
                            except OSError as e:
                                logger.error(f"复制文件失败: {e}", show_dialog=True)
                                raise
                        result = process_one_map(mod_id, map_name)
                    else:
                        # 文件已是正确格式，直接检查结果或检查大小
                        dst = os.path.join(BASE_DIR, f"[战役]{map_name}.vpk")
                        fsize = os.path.getsize(dst) if os.path.exists(dst) else 0
                        if 0 < fsize < 10 * 1024 * 1024:
                            logger.warning(f"[下载] {map_name}: 本地文件大小异常 ({fsize/1024/1024:.1f}MB < 10MB)，删除")
                            try:
                                os.remove(dst)
                            except OSError:
                                pass
                            result = {"mod_id": mod_id, "map_name": map_name, "status": "失败", "reason": f"文件大小异常（{fsize/1024/1024:.1f}MB < 10MB）"}

                            # 如果有 gamemaps_id，打开 gamemaps 网页
                            gamemaps_id = m.get('gamemaps_id', '')
                            if gamemaps_id:
                                url = f"https://www.gamemaps.com/details/{gamemaps_id}"
                                logger.warning(f"[下载] {map_name}: 文件大小异常，已自动打开 gameMaps 网页: {url}")
                                webbrowser.open(url)
                        else:
                            result = {"mod_id": mod_id, "map_name": map_name, "status": "成功"}
                    elapsed = datetime.now() - start
                    # 文件大小异常已在 process_one_map 中记录日志，无需弹窗
                    if result.get('status') == '成功':
                        logger.success(f"[下载] {map_name}: 完成（使用现有文件） {format_elapsed(elapsed)}")
                    else:
                        logger.error(f"[下载] {map_name}: 处理失败 - {result.get('reason', '未知')} {format_elapsed(elapsed)}")
                else:
                    if existing:
                        logger.warning(f"[下载] {map_name}: 文件大小不匹配，重新下载")
                    
                    # 获取地图大小（用于动态超时）
                    map_size_bytes = self._map_sizes.get(mod_id)
                    ok, _ = download_mod_with_steamcmd(
                        mod_id, 
                        progress_callback=lambda p, mid=mod_id: self._update_progress(mid, p),
                        map_size_bytes=map_size_bytes
                    )
                    elapsed = datetime.now() - start
                    if not ok:
                        logger.error(f"[下载] {map_name}: 下载失败 {format_elapsed(elapsed)}")
                    else:
                        result = process_one_map(mod_id, map_name)
                        # 检查是否因文件大小异常而失败
                        # 文件大小异常已在 process_one_map 中记录日志，无需弹窗
                        if result.get('status') == '失败' and '文件大小异常' in result.get('reason', ''):
                            # 如果有 gamemaps_id，打开 gamemaps 网页
                            gamemaps_id = m.get('gamemaps_id', '')
                            if gamemaps_id:
                                url = f"https://www.gamemaps.com/details/{gamemaps_id}"
                                logger.warning(f"[下载] {map_name}: 文件大小异常，已自动打开 gameMaps 网页: {url}")
                                webbrowser.open(url)
                        if result.get('status') == '成功':
                            logger.success(f"[下载] {map_name}: 完成 {format_elapsed(elapsed)}")
                        else:
                            logger.error(f"[下载] {map_name}: 处理失败 - {result.get('reason', '未知')} {format_elapsed(elapsed)}")
        except Exception as e:
            logger.error(f"[下载] {map_name}: 异常 - {e}", show_dialog=True)

        finally:
            with self._lock:
                self._active -= 1
                self._total_done += 1
                self._running_maps = [n for n in self._running_maps if n != map_name]
                self._progress.pop(mod_id, None)
                self._running_map_names.pop(mod_id, None)
            self._spawn()
            if not self.is_running:
                self.app.root.after(0, self.app._on_all_downloads_done)

    def get_status_text(self) -> str:
        """获取状态文本（供 GUI 显示）"""
        with self._lock:
            if self._active == 0 and self._queue.empty():
                return "就绪"
            parts = []
            if self._running_maps:
                entries = []
                for mid, name in list(self._running_map_names.items())[:3]:
                    pct = self._progress.get(mid)
                    entry = f"{name}({pct}%)" if pct is not None else name
                    entries.append(entry)
                if len(self._running_maps) > 3:
                    entries.append(f"+{len(self._running_maps) - 3}")
                parts.append(f"下载中: {', '.join(entries)}")
            q = self._queue.qsize()
            if q > 0:
                parts.append(f"等待中: {q}")
            if self._total_done > 0 or self._active > 0:
                parts.append(f"进度: {self._total_done}/{self._total_enqueued}")
            return " | ".join(parts)

    def _update_progress(self, mod_id: str, percent: int):
        with self._lock:
            self._progress[mod_id] = percent
        # 更新 GUI 进度条
        if self.app:
            self.app.root.after(0, self.app._update_progress_bar, mod_id, percent)


# ============================================================
# GUI 应用（改进版 - 添加进度条、全选按钮、日志过滤）
# ============================================================

class L4D2App:
    # ---- 配色主题 ----
    THEME = {
        # 窗口 - 白底黑字
        'bg': '#ffffff',
        'fg': '#000000',
        'surface': '#f5f5f5',
        'border': '#cccccc',
        # 按钮
        'btn_search_bg': '#22aa44',       # 绿色
        'btn_search_fg': '#ffffff',
        'btn_dl_bg': '#22aa44',           # 绿色
        'btn_dl_fg': '#ffffff',
        'btn_disabled_bg': '#bbbbbb',
        # 日志区 - 白底
        'log_bg': '#ffffff',
        'log_fg': '#000000',
        'log_success': '#008800',         # 绿
        'log_error': '#cc0000',           # 红
        'log_warning': '#cc8800',         # 黄/橙
        'log_info': '#000000',            # 黑
        # 表格 - 白底黑字
        'tree_bg': '#ffffff',
        'tree_fg': '#000000',
        'tree_header_bg': '#e8e8e8',
        'tree_select_bg': '#ff4444',      # 选中红底
        'tree_hover_bg': '#ffcccc',       # 悬浮浅红
    }

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("[lwyxb]三方图下载器")
        self.root.geometry("1300x800")
        self.root.minsize(1000, 600)
        self.root.configure(bg=self.THEME['bg'])

        # 状态
        self.search_results: list = []
        self._searching = False
        self._progress_bars = {}  # mod_id -> progressbar widget

        # 下载管理器
        self.download_mgr = DownloadManager(self)

        # 样式
        self._setup_style()

        # 布局
        self._build_layout()

        # 初始化日志
        self._init_logger()

        # 加载配置
        self._load_config()

        # 定时刷新
        self._status_update_loop()

    # ============================================================
    # 样式
    # ============================================================

    def _setup_style(self):
        s = ttk.Style()
        s.theme_use('clam')

        # 整体背景 - 白色
        s.configure('TFrame', background=self.THEME['bg'])
        s.configure('Surface.TFrame', background=self.THEME['surface'])

        # 标签
        s.configure('TLabel',
            background=self.THEME['bg'],
            foreground=self.THEME['fg'],
            font=("Microsoft YaHei", 10))

        # 表格 - 白底黑字
        s.configure('Treeview',
            background=self.THEME['tree_bg'],
            foreground=self.THEME['tree_fg'],
            fieldbackground=self.THEME['tree_bg'],
            rowheight=30,
            font=("Microsoft YaHei", 9),
            borderwidth=1,
            relief='solid')
        s.configure('Treeview.Heading',
            background=self.THEME['tree_header_bg'],
            foreground='#000000',
            font=("Microsoft YaHei", 9, "bold"),
            relief='flat',
            borderwidth=1)
        s.map('Treeview',
            background=[('selected', self.THEME['tree_select_bg'])])
        s.map('Treeview.Heading',
            background=[('active', '#d0d0d0')])

        # 输入框 - 白底黑字有边框
        s.configure('TEntry',
            fieldbackground='#ffffff',
            foreground='#000000',
            insertcolor='#000000',
            borderwidth=1,
            relief='solid')

        # 进度条
        s.configure('Horizontal.TProgressbar',
            background='#22aa44',
            troughcolor='#e0e0e0',
            borderwidth=0,
            lightcolor='#22aa44',
            darkcolor='#22aa44')

        # 滚动条
        s.configure('Vertical.TScrollbar',
            background='#e0e0e0',
            troughcolor='#f5f5f5',
            arrowcolor='#666666')
        s.configure('Horizontal.TScrollbar',
            background='#e0e0e0',
            troughcolor='#f5f5f5',
            arrowcolor='#666666')

    # ============================================================
    # 布局（改进版 - 添加进度条、全选按钮、日志过滤）
    # ============================================================

    def _build_layout(self):
        # 顶部分隔条 - 绿色
        top_sep = tk.Frame(self.root, height=3, bg='#22aa44')
        top_sep.pack(fill='x')

        # 主面板（左右分栏）
        paned = ttk.PanedWindow(self.root, orient='horizontal')
        paned.pack(fill='both', expand=True, padx=8, pady=(0, 8))
        self.paned = paned

        # ---- 左栏：搜索 + 表格 + 进度条 ----
        left = ttk.Frame(paned)
        paned.add(left, weight=2)

        # 搜索区
        sf = ttk.Frame(left)
        sf.pack(fill='x', padx=8, pady=(10, 6))
        tk.Label(sf, text="🔍", font=("Segoe UI Emoji", 13),
                 bg=self.THEME['bg'], fg='#22aa44').pack(side='left')
        tk.Label(sf, text="地图名称",
                 font=("Microsoft YaHei", 11, "bold"), bg=self.THEME['bg'],
                 fg='#000000').pack(side='left', padx=(4, 8))
        self.search_var = tk.StringVar()
        entry = ttk.Entry(sf, textvariable=self.search_var,
                          font=("Microsoft YaHei", 10))
        entry.pack(side='left', fill='x', expand=True, padx=(0, 6))
        entry.bind('<Return>', lambda e: self.do_search())
        self.search_entry = entry

        # 搜索按钮 - 绿色，带3D质感
        self.search_btn = tk.Button(sf, text="🔍 搜索",
            font=("Microsoft YaHei", 9, "bold"),
            bg='#28a745', fg='white',
            activebackground='#dc3545', activeforeground='white',
            bd=2, relief='raised', padx=10, pady=0, height=1, cursor='hand2',
            highlightbackground='#1e7e34', highlightthickness=1,
            command=self.do_search)
        self.search_btn.pack(side='left')
        # 悬浮/按下效果
        self.search_btn.bind('<Enter>', lambda e: self.search_btn.configure(bg='#34ce57'))
        self.search_btn.bind('<Leave>', lambda e: self.search_btn.configure(bg='#28a745'))
        self.search_btn.bind('<ButtonPress-1>', lambda e: self.search_btn.configure(bg='#c82333', relief='sunken'))
        self.search_btn.bind('<ButtonRelease-1>', lambda e: self.search_btn.configure(bg='#34ce57', relief='raised'))

        # 下载按钮 - 绿色，带3D质感
        self.download_btn = tk.Button(sf, text="⬇ 下载选中",
            font=("Microsoft YaHei", 9, "bold"),
            bg='#28a745', fg='white',
            activebackground='#dc3545', activeforeground='white',
            bd=2, relief='raised', padx=10, pady=0, height=1, cursor='hand2',
            highlightbackground='#1e7e34', highlightthickness=1,
            state='disabled', command=self.do_download)
        self.download_btn.pack(side='left', padx=(4, 0))
        self.download_btn.bind('<Enter>', lambda e: self._btn_hover(self.download_btn, True))
        self.download_btn.bind('<Leave>', lambda e: self._btn_hover(self.download_btn, False))
        self.download_btn.bind('<ButtonPress-1>', lambda e: self._btn_press(self.download_btn, True))
        self.download_btn.bind('<ButtonRelease-1>', lambda e: self._btn_press(self.download_btn, False))

        # 全选/取消全选按钮
        self.select_all_btn = tk.Button(sf, text="☑ 全选",
            font=("Microsoft YaHei", 9),
            bg='#6c757d', fg='white',
            activebackground='#5a6268', activeforeground='white',
            bd=1, relief='raised', padx=8, pady=0, height=1, cursor='hand2',
            command=self._select_all)
        self.select_all_btn.pack(side='left', padx=(4, 0))

        self.deselect_all_btn = tk.Button(sf, text="☐ 取消",
            font=("Microsoft YaHei", 9),
            bg='#6c757d', fg='white',
            activebackground='#5a6268', activeforeground='white',
            bd=1, relief='raised', padx=8, pady=0, height=1, cursor='hand2',
            command=self._deselect_all)
        self.deselect_all_btn.pack(side='left', padx=(2, 0))

        # 进度条区域（新增）
        pf = ttk.Frame(left)
        pf.pack(fill='x', padx=8, pady=(0, 4))
        
        tk.Label(pf, text="总体进度:",
                 font=("Microsoft YaHei", 9),
                 bg=self.THEME['bg'], fg='#000000').pack(side='left', padx=(0, 4))
        
        self.overall_progress = ttk.Progressbar(pf, mode='determinate', maximum=100)
        self.overall_progress.pack(side='left', fill='x', expand=True, padx=(0, 8))
        
        self.progress_label = tk.Label(pf, text="0%",
            font=("Microsoft YaHei", 9),
            bg=self.THEME['bg'], fg='#000000')
        self.progress_label.pack(side='left')

        # 表格区 - 带边框
        tf = tk.Frame(left, bg='#cccccc', bd=1, relief='solid')
        tf.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        cols = ("idx", "mod_id", "name", "author", "size", "update", "status")
        self.tree = ttk.Treeview(tf, columns=cols, show='headings',
                                  selectmode='extended')
        self.tree.tag_configure('hover', background=self.THEME['tree_hover_bg'])
        # 排序状态：记录每列当前排序方向
        self._sort_reverse = {}  # col -> bool (True=降序)
        self.tree.heading("idx", text="#", command=lambda: self._sort_by('idx'))
        self.tree.heading("mod_id", text="Steam ID", command=lambda: self._sort_by('mod_id'))
        self.tree.heading("name", text="地图名", command=lambda: self._sort_by('name'))
        self.tree.heading("author", text="作者", command=lambda: self._sort_by('author'))
        self.tree.heading("size", text="大小", command=lambda: self._sort_by('size'))
        self.tree.heading("update", text="最近更新", command=lambda: self._sort_by('update'))
        self.tree.heading("status", text="状态", command=lambda: self._sort_by('status'))

        # 列宽
        self.tree.column("idx", width=40, minwidth=35, anchor='center')
        self.tree.column("mod_id", width=100, minwidth=80)
        self.tree.column("name", width=180, minwidth=120)
        self.tree.column("author", width=100, minwidth=70)
        self.tree.column("size", width=75, minwidth=55, anchor='center')
        self.tree.column("update", width=100, minwidth=80)
        self.tree.column("status", width=55, minwidth=45, anchor='center')

        # 滚动条
        tv_sy = ttk.Scrollbar(tf, orient='vertical', command=self.tree.yview,
                               style='Vertical.TScrollbar')
        self.tree.configure(yscrollcommand=tv_sy.set)
        tv_sx = ttk.Scrollbar(tf, orient='horizontal', command=self.tree.xview,
                               style='Horizontal.TScrollbar')
        self.tree.configure(xscrollcommand=tv_sx.set)

        self.tree.grid(row=0, column=0, sticky='nsew')
        tv_sy.grid(row=0, column=1, sticky='ns')
        tv_sx.grid(row=1, column=0, sticky='ew')
        tf.grid_rowconfigure(0, weight=1)
        tf.grid_columnconfigure(0, weight=1)

        self.tree.bind('<<TreeviewSelect>>', self._on_select)
        self.tree.bind('<Double-1>', lambda e: self.do_download())
        # 表格行悬浮效果 - 红底
        self.tree.bind('<Motion>', self._tree_hover)
        self.tree.bind('<Leave>', self._tree_leave)
        self._tree_hover_ids = set()
        # 窗口大小变化时重新调整列宽
        self.tree.bind('<Configure>', self._on_tree_configure)

        # 右键菜单
        self._ctx_menu = tk.Menu(self.root, tearoff=0)
        self._ctx_menu.add_command(label="观看实况", command=self._open_bilibili)
        self._ctx_menu.add_command(label="查看创意工坊", command=self._open_workshop)
        self._ctx_menu.add_command(label="Steam下载", command=self.do_download)
        self._ctx_menu.add_command(label="gameMaps下载", command=self._open_gamemaps)
        self.tree.bind('<Button-3>', self._show_context_menu)

        # ---- 右栏：日志 + 日志过滤 ----
        right = ttk.Frame(paned)
        paned.add(right, weight=1)

        # 状态栏（左右循环滚动）
        self._status_text = "就绪"
        self._scroll_offset = 0
        self._scroll_dir = 1  # 1=向左滚, -1=向右滚
        self._scroll_paused = 0  # 到边界时暂停计数
        sh = tk.Frame(right, bg=self.THEME['surface'], bd=1, relief='solid',
                      highlightbackground='#cccccc', highlightthickness=1)
        sh.pack(fill='x', padx=8, pady=(8, 2))
        self.status_label = tk.Label(sh, text="就绪",
                 font=("Microsoft YaHei", 10, "bold"),
                 bg=self.THEME['surface'], fg='#008800',
                 anchor='w')
        self.status_label.pack(fill='x', padx=6, pady=4)

        # 日志过滤区（新增）
        lf_top = ttk.Frame(right)
        lf_top.pack(fill='x', padx=8, pady=(0, 2))
        
        tk.Label(lf_top, text="日志级别:",
                 font=("Microsoft YaHei", 9),
                 bg=self.THEME['bg'], fg='#000000').pack(side='left', padx=(0, 4))
        
        self.log_filter_var = tk.StringVar(value='ALL')
        for text, value in [("全部", "ALL"), ("信息", "INFO"), ("警告", "WARNING"), ("错误", "ERROR")]:
            rb = tk.Radiobutton(lf_top, text=text, value=value,
                                variable=self.log_filter_var,
                                bg=self.THEME['bg'], fg='#000000',
                                activebackground=self.THEME['bg'],
                                font=("Microsoft YaHei", 9),
                                command=self._on_log_filter_change)
            rb.pack(side='left', padx=(0, 8))

        # 日志区 - 白底，带边框
        lf = tk.Frame(right, bg='#cccccc', bd=1, relief='solid')
        lf.pack(fill='both', expand=True, padx=8, pady=(0, 8))

        self.log_text = tk.Text(lf,
            wrap='none',
            font=("Cascadia Mono", 9),
            bg='#ffffff',
            fg='#000000',
            insertbackground='#000000',
            relief='flat',
            state='disabled',
            spacing1=2)
        self.log_text.tag_configure('success', foreground=self.THEME['log_success'])
        self.log_text.tag_configure('error', foreground=self.THEME['log_error'])
        self.log_text.tag_configure('warning', foreground=self.THEME['log_warning'])
        self.log_text.tag_configure('info', foreground=self.THEME['log_info'])

        lsy = ttk.Scrollbar(lf, orient='vertical', command=self.log_text.yview,
                             style='Vertical.TScrollbar')
        self.log_text.configure(yscrollcommand=lsy.set)
        lsx = ttk.Scrollbar(lf, orient='horizontal', command=self.log_text.xview,
                             style='Horizontal.TScrollbar')
        self.log_text.configure(xscrollcommand=lsx.set)

        self.log_text.grid(row=0, column=0, sticky='nsew')
        lsy.grid(row=0, column=1, sticky='ns')
        lsx.grid(row=1, column=0, sticky='ew')
        lf.grid_rowconfigure(0, weight=1)
        lf.grid_columnconfigure(0, weight=1)

        # 窗口关闭事件
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    # ============================================================
    # 日志过滤（新增）
    # ============================================================

    def _on_log_filter_change(self):
        """日志过滤级别变更"""
        level = self.log_filter_var.get()
        if logger:
            logger.set_filter(level)

    # ============================================================
    # 全选/取消全选（新增）
    # ============================================================

    def _select_all(self):
        """全选所有地图"""
        all_items = self.tree.get_children()
        if all_items:
            self.tree.selection_set(all_items)
            self._on_select()

    def _deselect_all(self):
        """取消全选"""
        self.tree.selection_remove(self.tree.selection())
        self._on_select()

    # ============================================================
    # 进度条更新（新增）
    # ============================================================

    def _update_progress_bar(self, mod_id: str, percent: int):
        """更新进度条显示"""
        # 这里可以实现更精细的进度显示
        # 目前先更新总体进度
        if self.download_mgr._total_enqueued > 0:
            overall = int(self.download_mgr._total_done / self.download_mgr._total_enqueued * 100)
            self.overall_progress['value'] = overall
            self.progress_label.configure(text=f"{overall}%")

    # ============================================================
    # 日志初始化
    # ============================================================

    def _init_logger(self):
        global logger
        log_file = os.path.join(LOG_DIR,
            f"运行日志_{datetime.now():%Y%m%d_%H%M%S}.txt")
        logger = GUILogger(log_file, self.log_text)
        logger.info("程序启动")
        logger.info(f"程序目录: {BASE_DIR}")
        logger.info(f"SteamCMD目录: {STEAMCMD_DIR}")
        logger.info("版本: v2.1 (改进版)")

        # 依赖检查
        missing = []
        if pymysql is None:
            missing.append("pymysql")
        if requests is None:
            missing.append("requests")
        if missing:
            logger.warning(f"缺少依赖: {', '.join(missing)}，正在安装...")
            try:
                subprocess.check_call([sys.executable, "-m", "pip", "install"] + missing)
                logger.success("依赖安装成功！请重新运行程序。")
            except Exception as e:
                logger.error(f"依赖安装失败: {e}，请手动执行: pip install {' '.join(missing)}")
            sys.exit(0)
        else:
            logger.success("所有依赖已就绪")

        # SteamCMD 检查
        if os.path.exists(STEAMCMD_EXE):
            logger.success("SteamCMD 已就绪")
        else:
            logger.info("SteamCMD 未找到，将在后台下载...")

    # ============================================================
    # 定时任务
    # ============================================================

    def _status_update_loop(self):
        self._update_status()
        self._scroll_status_text()
        self.root.after(500, self._status_update_loop)

    def _update_status(self):
        status = self.download_mgr.get_status_text()
        if status != self._status_text:
            self._status_text = status
            self._scroll_offset = 0
            self.status_label.configure(text=status)
        # 删除 queue_label 更新代码

    def _scroll_status_text(self):
        """状态文字过长时左右循环滚动显示，尽可能多显示文字"""
        text = self._status_text
        if not text or text == "就绪":
            return
        try:
            label_width = self.status_label.winfo_width()
            if label_width < 20:
                return
            # 估算文字像素宽度
            est_width = sum(14 if ord(c) > 127 else 8 for c in text)
            if est_width <= label_width - 8:
                return  # 不需要滚动
        except Exception:
            return

        # 到边界时暂停几步再反向
        if self._scroll_paused > 0:
            self._scroll_paused -= 1
            return

        # 左右循环滚动
        self._scroll_offset += self._scroll_dir

        # 计算最大偏移：文字总宽 - 可见宽，换算成字符数
        overflow_chars = max(0, len(text) - int((label_width - 8) / 10))

        if self._scroll_offset <= 0:
            self._scroll_offset = 0
            self._scroll_dir = 1  # 到头，改为向左滚
            self._scroll_paused = 4  # 暂停4步
        elif self._scroll_offset >= overflow_chars:
            self._scroll_offset = overflow_chars
            self._scroll_dir = -1  # 到尾，改为向右滚
            self._scroll_paused = 4

        # 尽可能多显示文字
        visible = text[self._scroll_offset:]
        if self._scroll_offset > 0:
            visible = "…" + visible
        self.status_label.configure(text=visible)

    # ============================================================
    # 按钮效果
    # ============================================================

    def _btn_hover(self, btn, enter):
        """鼠标悬浮：变亮绿"""
        if btn.cget('state') == 'disabled':
            return
        btn.configure(bg='#34ce57' if enter else '#28a745', relief='raised')

    def _btn_press(self, btn, pressing):
        """鼠标按下：变红 + 凹陷"""
        if btn.cget('state') == 'disabled':
            return
        if pressing:
            btn.configure(bg='#c82333', relief='sunken')
        else:
            btn.configure(bg='#34ce57', relief='raised')

    # ============================================================
    # 表格悬浮效果
    # ============================================================

    def _tree_hover(self, event):
        """鼠标移到表格行上时高亮红底"""
        item_id = self.tree.identify_row(event.y)
        # 清除上次高亮
        for iid in self._tree_hover_ids:
            try:
                self.tree.item(iid, tags=())
            except Exception:
                pass
        self._tree_hover_ids.clear()
        if item_id:
            self.tree.item(item_id, tags=('hover',))
            self._tree_hover_ids.add(item_id)

    def _tree_leave(self, _=None):
        """鼠标离开表格时清除高亮"""
        for iid in self._tree_hover_ids:
            try:
                self.tree.item(iid, tags=())
            except Exception:
                pass
        self._tree_hover_ids.clear()

    # ============================================================
    # 事件处理
    # ============================================================

    def _on_select(self, _=None):
        has_sel = len(self.tree.selection()) > 0
        if has_sel:
            self.download_btn.configure(state='normal', bg='#28a745')
        else:
            self.download_btn.configure(state='disabled', bg='#bbbbbb')

    def _show_context_menu(self, event):
        """右键菜单：选中所在行后弹出"""
        row_id = self.tree.identify_row(event.y)
        if row_id:
            # 如果右键行未选中，则切换选中到该行
            if row_id not in self.tree.selection():
                self.tree.selection_set(row_id)
            self._ctx_menu.post(event.x_root, event.y_root)


    def _open_workshop(self):
        """在浏览器中打开选中地图的 Steam 创意工坊页面"""
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        idx = self.tree.index(item)
        if idx < len(self.search_results):
            mod_id = self.search_results[idx].get('mod_id', '')
            if mod_id:
                url = f"https://steamcommunity.com/sharedfiles/filedetails/?id={mod_id}"
                webbrowser.open(url)
            else:
                logger.warning("该地图缺少 Steam ID，无法打开工坊页面")

    def _open_gamemaps(self):
        """在浏览器中打开选中地图的 gameMaps 页面"""
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        idx = self.tree.index(item)
        if idx < len(self.search_results):
            gamemaps_id = self.search_results[idx].get('gamemaps_id', '')
            if gamemaps_id:
                url = f"https://www.gamemaps.com/details/{gamemaps_id}"
                webbrowser.open(url)
            else:
                logger.warning("该地图缺少 gameMaps ID，无法打开 gameMaps 页面")

    def _open_bilibili(self):
        """在浏览器中打开 B 站搜索该地图的实况视频"""
        sel = self.tree.selection()
        if not sel:
            return
        item = sel[0]
        idx = self.tree.index(item)
        if idx < len(self.search_results):
            map_name_en = self.search_results[idx].get('map_name_en', '')
            keyword = map_name_en or self.search_results[idx].get('map_name_cn', '')
            if keyword:
                url = f"https://search.bilibili.com/all?keyword={urllib.request.quote(keyword)}"
                webbrowser.open(url)
            else:
                logger.warning("该地图缺少名称，无法搜索实况")

    def _auto_resize_columns(self):
        """搜索完成后自动调整列宽，确保表格始终占满全宽，作者列自适应"""
        w = self.tree.winfo_width()
        if w <= 1:
            self.root.after(100, self._auto_resize_columns)
            return

        cols = list(self.tree['columns'])
        # 除了 author 之外的列：计算内容所需宽度
        fixed_cols = [c for c in cols if c != 'author']
        widths = {}
        for col in fixed_cols:
            heading = self.tree.heading(col)['text']
            mw = len(heading) * 14 + 16
            for item in list(self.tree.get_children())[:100]:
                vals = self.tree.item(item, 'values')
                ci = cols.index(col)
                if ci < len(vals):
                    mw = max(mw, len(str(vals[ci])) * 8 + 12)
            widths[col] = mw

        fixed_total = sum(widths.values())
        # author 列 = 剩余空间（至少 minwidth）
        author_min = self.tree.column('author', 'minwidth')
        author_w = max(w - fixed_total, author_min)
        widths['author'] = author_w

        # 如果总宽度超出（内容太多），等比缩放固定列
        total = sum(widths.values())
        if total > w and w > 0:
            overflow = total - w
            scale = max((fixed_total - overflow) / fixed_total, 0.5)
            for col in fixed_cols:
                widths[col] = max(int(widths[col] * scale), self.tree.column(col, 'minwidth'))
            widths['author'] = max(w - sum(widths[c] for c in fixed_cols), author_min)

        for col, cw in widths.items():
            self.tree.column(col, width=cw)

    def _sort_by(self, col):
        """点击表头排序：第一次正序，再点反序，循环切换"""
        if not self.search_results:
            return

        # 切换排序方向
        reverse = not self._sort_reverse.get(col, False)
        self._sort_reverse[col] = reverse

        # 列名 -> search_results 字段映射
        col_key_map = {
            'idx': None,       # 用行号排
            'mod_id': 'mod_id',
            'name': 'map_name_cn',
            'author': 'author',
            'size': 'size',
            'update': 'update_time',
            'status': 'is_using',
        }

        key = col_key_map.get(col)
        cols = list(self.tree['columns'])

        if col == 'idx':
            # 按原始顺序排（正序=原始，反序=倒序）
            sorted_results = list(self.search_results)
            if reverse:
                sorted_results.reverse()
        else:
            def _sort_key(m):
                v = m.get(key, '') or ''
                # size 列：提取数字+单位转为字节数排序，未知排末尾
                if col == 'size':
                    if v == '未知' or not v:
                        size_val = -1  # 未知排末尾（升序时在最后）
                    else:
                        try:
                            match = re.match(r'([0-9.]+)\s*(KB|MB|GB)', v, re.IGNORECASE)
                            if match:
                                num = float(match.group(1))
                                unit = match.group(2).upper()
                                multipliers = {'KB': 1024, 'MB': 1024**2, 'GB': 1024**3}
                                size_val = num * multipliers.get(unit, 1)
                            else:
                                size_val = float(v)
                        except (ValueError, TypeError):
                            size_val = -1
                    # 同大小的按更新时间排（最近的排前面→字符串降序）
                    update_str = str(m.get('update_time', '') or '')
                    return (size_val, update_str)
                # update 列：日期排序
                if col == 'update':
                    return str(v)
                # status 列：按 is_using 值排
                if col == 'status':
                    val = m.get('is_using', 1)
                    return 0 if val in (0, '0', False) else 1
                # mod_id 列：数字排序
                if col == 'mod_id':
                    try:
                        return int(v)
                    except (ValueError, TypeError):
                        return 0
                # 文本列：按拼音排序
                return str(v)
            sorted_results = sorted(self.search_results, key=_sort_key, reverse=reverse)

        # 更新 search_results 以保持选中对应关系
        self.search_results = sorted_results

        # 重新填充表格
        for item in self.tree.get_children():
            self.tree.delete(item)

        for i, m in enumerate(sorted_results, 1):
            status = "⚠停用" if m.get('is_using') in (0, '0', False) else "✅正常"
            self.tree.insert('', 'end', values=(
                i,
                m['mod_id'],
                m['map_name_cn'] or '未知',
                m['author'],
                m['size'],
                m['update_time'],
                status
            ))

        # 更新表头显示排序方向箭头
        arrow = ' ▼' if reverse else ' ▲'
        for c in cols:
            base_text = {
                'idx': '#', 'mod_id': 'Steam ID', 'name': '地图名',
                'author': '作者', 'size': '大小',
                'update': '最近更新', 'status': '状态'
            }[c]
            self.tree.heading(c, text=base_text + (arrow if c == col else ''))

    def _on_tree_configure(self, _=None):
        """窗口大小变化时重新调整列宽"""
        if self.tree.get_children():  # 有数据时才调整
            self._auto_resize_columns()

    def _on_all_downloads_done(self):
        logger.separator()
        logger.success("🎉 所有下载任务已完成！")
        # 所有下载完成后统一清理 SteamCMD 下载缓存
        cleanup_steamcmd_downloads()
        logger.info("已清理 SteamCMD 下载缓存")
        # 重置进度条
        self.overall_progress['value'] = 100
        self.progress_label.configure(text="100%")

    def _on_close(self):
        if self.download_mgr.is_running:
            if not messagebox.askyesno("确认",
                    "正在下载中，确定要退出吗？\n未完成的下载将丢失。"):
                return
        logger.info("用户退出程序")
        cleanup_logs()
        cleanup_steamcmd_downloads()
        logger.close()
        self._save_config()
        self.root.destroy()

    # ============================================================
    # 配置持久化（新增）
    # ============================================================

    def _save_config(self):
        """保存配置到文件"""
        config = {
            'window_geometry': self.root.geometry(),
            'search_history': getattr(self, '_search_history', [])[:10],  # 保存最近10条
        }
        try:
            import json
            with open(CONFIG_FILE, 'w', encoding='utf-8') as f:
                json.dump(config, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"保存配置失败: {e}")

    def _load_config(self):
        """从文件加载配置"""
        try:
            import json
            if os.path.exists(CONFIG_FILE):
                with open(CONFIG_FILE, 'r', encoding='utf-8') as f:
                    config = json.load(f)
                # 恢复窗口大小和位置
                if 'window_geometry' in config:
                    try:
                        self.root.geometry(config['window_geometry'])
                    except Exception:
                        pass
                # 加载搜索历史
                self._search_history = config.get('search_history', [])
        except Exception as e:
            logger.warning(f"加载配置失败: {e}")
            self._search_history = []

    # ============================================================
    # 搜索（改进版 - 添加搜索历史）
    # ============================================================

    def do_search(self):
        if self._searching:
            return
        name = self.search_var.get().strip()
        if not name:
            messagebox.showwarning("提示", "请输入地图名称")
            return

        # 保存到搜索历史
        if not hasattr(self, '_search_history'):
            self._search_history = []
        if name not in self._search_history:
            self._search_history.insert(0, name)
            self._search_history = self._search_history[:10]  # 最多保存10条

        self._searching = True
        self.search_btn.configure(state='disabled')
        self.status_label.configure(text="搜索中...")

        def _search():
            results = query_maps_by_name(name)
            self.root.after(0, self._on_search_done, results)

        threading.Thread(target=_search, daemon=True).start()

    def _on_search_done(self, results: list):
        self._searching = False
        self.search_results = results
        self.search_btn.configure(state='normal')

        for item in self.tree.get_children():
            self.tree.delete(item)

        if not results:
            self.status_label.configure(text="未找到匹配的地图")
            self.download_btn.configure(state='disabled')
            logger.warning("未找到匹配的地图")
            return

        for i, m in enumerate(results, 1):
            status = "⚠停用" if m.get('is_using') in (0, '0', False) else "✅正常"
            self.tree.insert('', 'end', values=(
                i,
                m['mod_id'],
                m['map_name_cn'] or '未知',
                m['author'],
                m['size'],
                m['update_time'],
                status
            ))

        disabled = [m for m in results if m.get('is_using') in (0, '0', False)]
        if disabled:
            logger.warning(f"⚠ {len(disabled)} 个地图已停用")

        logger.success(f"找到 {len(results)} 个地图")
        self.status_label.configure(text=f"找到 {len(results)} 个地图")
        self._auto_resize_columns()

    # ============================================================
    # 下载
    # ============================================================

    def do_download(self):
        selection = self.tree.selection()
        if not selection:
            messagebox.showwarning("提示", "请先选择要下载的地图")
            return

        selected = []
        for item in selection:
            values = self.tree.item(item, 'values')
            idx = int(values[0]) - 1
            if 0 <= idx < len(self.search_results):
                selected.append(self.search_results[idx])

        if not selected:
            return

        self.download_mgr.enqueue(selected)


# ============================================================
# 主入口
# ============================================================

def main():
    global app
    root = tk.Tk()
    # 尝试加载自定义主题
    try:
        root.tk.call('tk', 'scaling', 1.2)
    except Exception:
        pass
    app = L4D2App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
