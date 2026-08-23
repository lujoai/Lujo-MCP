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
"""
import os

# M13：阻断宿主环境 API_KEY 泄漏进测试进程，确保测试运行在已知（无鉴权）状态。
# 单测若需鉴权场景，应通过 monkeypatch 显式设置。
# ⚠️ 此 env 哨兵不是唯一防线（见上方 M13 说明）：tests/__init__.py 的导入链可能
#    早于 conftest 就创建了 settings 单例；Windows 上空串 env 又等效 unset，不可靠。
#    真正的兜底在下方「直接重置单例」。
os.environ["API_KEY"] = ""
# SEC-03：默认 host=0.0.0.0 + 空 api_key 会拒绝启动；测试用本地回环避开。
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
