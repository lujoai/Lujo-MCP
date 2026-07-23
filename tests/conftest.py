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
"""
import os

# M13：阻断宿主环境 API_KEY 泄漏进测试进程，确保测试运行在已知
# （无鉴权）状态。单测若需鉴权场景，应通过 monkeypatch 显式设置。
# 必须在任何 `from app...` / pydantic-settings 实例化前执行。
# env var 优先级高于 .env 文件：显式置空 → config.py M7 归一化为 None → 鉴权关闭。
# （原 os.environ.pop 无效：pop 后 pydantic 仍从 .env 读到 API_KEY=test_secret_key_456）
os.environ["API_KEY"] = ""
# SEC-03：默认 host=0.0.0.0 + 空 api_key 会拒绝启动；测试用本地回环避开。
os.environ.setdefault("HOST", "127.0.0.1")

import pydantic_settings

# 必须在任何 `from app...` 之前执行：pytest 加载 conftest.py 的顺序是
# 外层先于内层，所以 tests/conftest.py 会先于 tests/unit/conftest.py 被加载。
pydantic_settings.BaseSettings.model_config["extra"] = "ignore"
