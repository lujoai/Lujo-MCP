"""运行时信息收集器 —— 使用 psutil 采集系统/进程状态"""

import os
import sys
import json
import time
from typing import Any

try:
    import psutil
    HAS_PSUTIL = True
except ImportError:
    HAS_PSUTIL = False


def _safe_get(obj: Any, method: str, default: Any = None):
    """安全调用 psutil 方法，避免在某些平台上报错"""
    try:
        fn = getattr(obj, method, None)
        if fn is None:
            return default
        result = fn()
        # namedtuple（如 psutil 的 pmem）保留原对象，调用方依赖命名属性 getattr(rss/vms)
        if isinstance(result, tuple) and hasattr(result, "_fields"):
            return result
        # 处理普通 tuple/list 等不可序列化容器
        if isinstance(result, (tuple, list)):
            return list(result)
        try:
            json.dumps(result)
            return result
        except (TypeError, ValueError):
            return str(result)
    except Exception:
        return default


def collect_process_info() -> dict:
    """收集当前进程信息"""
    if not HAS_PSUTIL:
        return {"error": "psutil 未安装，无法采集进程信息"}

    proc = psutil.Process(os.getpid())
    mem = _safe_get(proc, "memory_info", {})
    if isinstance(mem, object) and not isinstance(mem, dict):
        mem = {
            "rss": getattr(mem, "rss", 0),
            "vms": getattr(mem, "vms", 0),
        }

    return {
        "pid": proc.pid,
        "name": proc.name(),
        "cmdline": proc.cmdline(),
        "cpu_percent": _safe_get(proc, "cpu_percent", 0.0),
        "memory_rss_mb": round(mem.get("rss", 0) / (1024 * 1024), 2),
        "memory_vms_mb": round(mem.get("vms", 0) / (1024 * 1024), 2),
        "num_threads": proc.num_threads(),
        "create_time": proc.create_time(),
        "status": proc.status(),
    }


def collect_system_info() -> dict:
    """收集系统级信息"""
    if not HAS_PSUTIL:
        return {"error": "psutil 未安装，无法采集系统信息"}

    mem = psutil.virtual_memory()
    disk = psutil.disk_usage("/")
    return {
        "cpu_count": psutil.cpu_count(),
        "cpu_percent": psutil.cpu_percent(interval=0.1),
        "memory_total_mb": round(mem.total / (1024 * 1024), 2),
        "memory_available_mb": round(mem.available / (1024 * 1024), 2),
        "memory_percent": mem.percent,
        "disk_total_gb": round(disk.total / (1024 ** 3), 2),
        "disk_free_gb": round(disk.free / (1024 ** 3), 2),
        "disk_percent": disk.percent,
    }


def collect_python_info() -> dict:
    """收集 Python 解释器信息"""
    try:
        python_path = sys.executable
    except Exception:
        python_path = sys.prefix

    return {
        "version": sys.version,
        "executable": python_path,
        "platform": sys.platform,
        "cwd": os.getcwd(),
    }


def collect_runtime_snapshot() -> dict:
    """采集完整的运行时快照"""
    return {
        "timestamp": time.time(),
        "python": collect_python_info(),
        "system": collect_system_info(),
        "process": collect_process_info(),
    }
