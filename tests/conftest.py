"""测试根 conftest —— 在任何 app 模块被导入前调整 pydantic-settings 默认行为。

M9 环境阻断 workaround
=====================
背景：pydantic-settings 自 2.5.0 起将默认 `extra` 改为 `"forbid"`（破坏性变更）。
当前 .env 含 `POSTGRES_PASSWORD`、`DATABASE_URL` 等供 docker-compose 使用的字段，
这些字段未在 `app/config.py::Settings` 中声明，导致 `Settings()` 实例化即崩，
所有测试无法收集。这是 v0.3.0 Release Audit 清单中 M9（`.env` 出现未知键启动即崩）
在测试环境下的表现，本身是待处理项（P2），不在本任务修复范围。

处置：仅作用于测试环境，在 `app.config` 被导入前把 `BaseSettings.model_config["extra"]`
改为 `"ignore"`，让未声明的 .env 字段被静默忽略。生产/运行时仍走 `extra="forbid"`
默认值，不受此 workaround 影响。

此文件不修改任何业务代码（`app/**/*.py`），仅是测试基础设施。

M13 宿主 API_KEY 泄漏阻断（方案 B：直接重置单例）
=============================================
背景（已确认）：环境变量哨兵 `os.environ["API_KEY"]=""` 无法作为唯一防线。
原因有二：
1. `tests/__init__.py` 顶层 `from app.runtime.context.builder import build_context`
   的导入链会传导导入 `app.config`，在 conftest 执行之前就把全局 `settings`
   单例创建出来（此时已读取 .env 中的真实 API_KEY）。等 conftest 里的 env 赋值生效，
   `Settings()` 早已实例化为带鉴权状态。
2. Windows 上空串环境变量等效于 unset（pydantic 视同未设置），会回落 .env，
   同样读到真实 API_KEY。

处置（方案 B）：不走 `tests/__init__.py` 重构（本轮明确禁止），改为在导入链已创建
了 settings 单例的前提下，**直接重置单例到无鉴权状态**——`api_key=None; api_keys=""`
（app/ 顶层即可触达，见 `app/config.py` 全局单例赋值；key_rotation.get_valid_keys()
每次运行时实时读单例属性，重置后鉴权即时判定关闭）。pydantic 2.13 默认不在赋值时
校验（model_config 未开 validate_assignment），此直接赋值可被接受且立即生效。

注意：导入顺序必须严格——先设 `extra="ignore"`，再 `from app.config import settings`，
不要在改 extra 之前导入 app.config（否则 .env 未知字段会使 Settings 初始化失败）。

- 需要鉴权的测试必须用 monkeypatch 显式设置 key（不要依赖本处兜底）。
- 本修改只影响 pytest 进程，不影响生产运行时。

FIX: e2e uvicorn 启动被 SEC-03 误杀（host 哨兵时序失效）
==========================================================
背景：下方第 49 行的 `os.environ.setdefault("HOST", "127.0.0.1")` 哨兵与 M13 的
API_KEY 哨兵踩同一个坑——`tests/__init__.py` 顶层导入链在 conftest 执行之前就把
settings 单例创建出来（此时读入默认 `host="0.0.0.0"`），env 哨兵来晚不再被读取。
结果：e2e/conftest.py 用 `uvicorn.Config(host="127.0.0.1")` 启动测试服务器，但 app
lifespan 里的 SEC-03 守卫 `validate_startup_configuration()` 无参调用读的是
`settings.host`（仍为 0.0.0.0）+ `auth_enabled()=False`（M13 已重置）→ RuntimeError
"Refusing to start" → e2e 全部 10 个用例 ERROR（uvicorn 起不来）。

处置：与 M13 同法兜底——在下方「直接重置单例」处一并重置 `settings.host` 为
"127.0.0.1"（与 e2e 测试服务器的实际 bind 地址一致，回环地址安全）。

FIX: Windows 11 24H2+ 损坏 pytest-current junction 防崩补丁
==============================================================
背景：旧版 pytest 在 `%TEMP%\\pytest-of-<user>\\` 创建的 `pytest-current` symlink
被 Windows 11 24H2+ 标记为不受信任挂载点（WinError 5/448），任何 stat/resolve 均
被拒。pytest 8.3.x `_pytest.pathlib.cleanup_dead_symlinks` 遍历该目录时
`left_dir.resolve().exists()` 抛 PermissionError 未捕获 → 整个测试会话在创建
basetemp 时崩溃（0 tests ran，退出码 1）。损坏 junction 本身需管理员权限才能删除。

处置：conftest 加载早于 basetemp 创建，此处把 `cleanup_dead_symlinks` 替换为异常
安全版本——单条目清理失败仅跳过（该清理只是 best-effort 回收旧临时目录，失败无碍）。
"""
import os

# M13：阻断宿主环境 API_KEY 泄漏进测试进程，确保测试运行在已知（无鉴权）状态。
# 单测若需鉴权场景，应通过 monkeypatch 显式设置。
# ⚠️ 此 env 哨兵不是唯一防线（见上方 M13 说明）：tests/__init__.py 的导入链可能
#    早于 conftest 就创建了 settings 单例；Windows 上空串 env 又等效 unset，不可靠。
#    真正的兜底在下方「直接重置单例」。
os.environ["API_KEY"] = ""
# SEC-03：默认 host=0.0.0.0 + 空 api_key 会拒绝启动；测试用本地回环避开。
# ⚠️ 与 API_KEY 哨兵同样存在时序失效（见上方 FIX 说明），真正兜底在下方单例重置。
os.environ.setdefault("HOST", "127.0.0.1")

import pydantic_settings

# 必须在任何 `from app...` 之前执行：pytest 加载 conftest.py 的顺序是
# 外层先于内层，所以 tests/conftest.py 会先于 tests/unit/conftest.py 被加载。
# 但 tests/__init__.py 顶层导入链会在两者之前就触达 app.config（创建 settings 单例），
# 因此下面必须紧接着重置该单例（方案 B），不能只依赖 env 或 extra=ignore。
pydantic_settings.BaseSettings.model_config["extra"] = "ignore"

# 方案 B（最终兜底）：导入并重置全局 settings 单例。
# 必须在上方 extra=ignore 设置完成后再导入 app.config，否则 .env 未知字段会使
# Settings() 初始化失败（M9）。导入链（tests/__init__.py）已提前创建该单例并读入
# .env 真实 API_KEY，这里直接改写单例属性到无鉴权状态——这是测试基础设施层的最终防线，
# 覆盖 tests/__init__.py 早于 conftest 创建 settings、以及 Windows 空串 env 失效两种情形。
# noqa: E402 —— 必须放在上方 extra=ignore 之后导入（命令式时序），故容忍模块级非置顶 import。
from app.config import settings  # noqa: E402

settings.api_key = None
settings.api_keys = ""
# FIX: e2e 误杀——HOST env 哨兵因导入链抢跑失效，此处直接重置单例 host
# 为回环地址（与 e2e/conftest.py 的 uvicorn bind 一致），SEC-03 守卫放行。
settings.host = "127.0.0.1"

# ── FIX: pytest-current 损坏 junction 防崩补丁（Windows 11 24H2+）──
# 替换 pytest 内部的死链清理函数为异常安全版本。conftest 加载早于 tmpdir factory
# 创建 basetemp，补丁一定在崩溃点（cleanup_dead_symlinks）之前生效。
import _pytest.pathlib as _pytest_pathlib  # noqa: E402


def _safe_cleanup_dead_symlinks(root) -> None:
    """cleanup_dead_symlinks 的异常安全版本：单条目 resolve/unlink 失败仅跳过。

    覆盖场景：%TEMP%\\pytest-of-<user>\\pytest-current 被系统标记为不受信任
    挂载点（WinError 5），原实现在 resolve() 处抛 PermissionError 使整个
    测试会话崩溃。清理本身是 best-effort 回收，失败跳过无碍正确性。
    """
    for left_dir in root.iterdir():
        try:
            if left_dir.is_symlink() and not left_dir.resolve().exists():
                left_dir.unlink()
        except OSError:
            # 不可访问的 symlink/junction：跳过该条目（可能需要管理员权限清理）
            continue


_pytest_pathlib.cleanup_dead_symlinks = _safe_cleanup_dead_symlinks
