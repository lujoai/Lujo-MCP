"""runtime 采集器单测（FIX v0.7.1-b3-2 / b3-10）。

覆盖：
- b3-2: proc.* 裸调用在 zombie/权限失败时降级（不再整段丢失 process 节）
- b3-2: cmdline 敏感参数脱敏
- b3-10: cpu_percent 非阻塞采样（interval=0）
- 缺 psutil 时返回 error dict（既有契约）
"""

from types import SimpleNamespace

import pytest

from app.runtime.collectors import runtime as runtime_module


class _FakePsutil:
    """隔离的 psutil 替身：不触碰真实 psutil 模块。"""

    def __init__(self) -> None:
        self.proc = None
        self.cpu_intervals: list = []

    def Process(self, pid):  # 与 psutil 同构
        return self.proc

    def virtual_memory(self):
        return SimpleNamespace(total=8 * 1024**3, available=4 * 1024**3, percent=50.0)

    def disk_usage(self, _path):
        return SimpleNamespace(total=100 * 1024**3, free=50 * 1024**3, percent=50.0)

    def cpu_count(self):
        return 8

    def cpu_percent(self, interval=None):
        self.cpu_intervals.append(interval)
        return 12.3


@pytest.fixture
def fake_psutil(monkeypatch):
    fake = _FakePsutil()
    monkeypatch.setattr(runtime_module, "psutil", fake)
    monkeypatch.setattr(runtime_module, "HAS_PSUTIL", True)
    return fake


# ── b3-2: zombie / 权限失败降级 ──────────────────────────────────


def test_process_info_degrades_on_zombie(fake_psutil):
    """proc.name()/cmdline() 抛异常（zombie 窗口）→ 字段降级而非整段丢失。"""

    class _ZombieProc:
        pid = 1234

        def memory_info(self):
            return None

        def name(self):
            raise RuntimeError("ZombieProcess")

        def cmdline(self):
            raise RuntimeError("ZombieProcess")

        def num_threads(self):
            return 5

        def cpu_percent(self):
            return 1.0

        def create_time(self):
            return 100.0

        def status(self):
            return "running"

    fake_psutil.proc = _ZombieProc()

    info = runtime_module.collect_process_info()

    assert info["pid"] == 1234
    assert info["name"] == "unknown"  # 降级而非抛错
    assert info["cmdline"] == []
    assert info["num_threads"] == 5  # 正常字段不受影响


def test_process_cmdline_redacted(fake_psutil):
    """cmdline 含敏感参数（--password=xxx）→ 入库前脱敏。"""

    class _Proc:
        pid = 7

        def memory_info(self):
            return None

        def name(self):
            return "myserver"

        def cmdline(self):
            return ["python", "app.py", "--password=secret-abc-123", "--port", "8000"]

        def num_threads(self):
            return 1

        def cpu_percent(self):
            return 0.0

        def create_time(self):
            return 1.0

        def status(self):
            return "running"

    fake_psutil.proc = _Proc()

    info = runtime_module.collect_process_info()
    joined = " ".join(info["cmdline"])
    assert "secret-abc-123" not in joined, "cmdline 敏感值必须被脱敏"
    assert "--password" in joined  # 键名保留（掩码只覆盖值）
    assert "app.py" in joined      # 非敏感内容不受影响


# ── b3-10: cpu_percent 非阻塞采样 ─────────────────────────────────


def test_system_cpu_percent_non_blocking(fake_psutil):
    """cpu_percent 必须以 interval=0 调用（旧 0.1 阻塞 100ms 事件循环）。"""
    snap = runtime_module.collect_system_info()
    assert snap["cpu_count"] == 8
    assert fake_psutil.cpu_intervals == [0], (
        f"cpu_percent 必须非阻塞采样（interval=0），实际 {fake_psutil.cpu_intervals}"
    )


# ── 缺 psutil 契约 ──────────────────────────────────────────────


def test_psutil_missing_returns_error_dict(monkeypatch):
    monkeypatch.setattr(runtime_module, "HAS_PSUTIL", False)
    assert runtime_module.collect_system_info() == {"error": "psutil 未安装，无法采集系统信息"}
    assert runtime_module.collect_process_info() == {"error": "psutil 未安装，无法采集进程信息"}