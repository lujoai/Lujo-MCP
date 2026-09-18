"""U06：外部 ASGI 宿主禁用 lifespan 时，认证不得静默放开。

复现的缺陷（DEV_PLAN §U06 / CODE_REVIEW §4.3 #6）：
`validate_startup_configuration()` 只在 `app/main.py` 的 lifespan 内被调用。当宿主以
`lifespan="off"` 挂载 app（`uvicorn --lifespan off`、部分嵌入式/反代部署）时该校验根本不
执行，而 `AuthMiddleware` 因 `auth_enabled() is False` 直接 `call_next` 放行 —— 于是
「通配监听（0.0.0.0 / ::）+ 未配置任何 API Key」这一本应被拒绝的危险组合，会以完全无鉴权
的状态对外提供服务。

验收口径（docs/internal/DEV_PLAN.md U06）：
    未授权请求被接受为 P1；若部署方式不受支持，也必须明确拒绝而非静默放开认证。

因此该安全不变量必须由**中间件层按请求独立成立**，不能只依赖生命周期是否运行。

测试手法（对应 AGENTS.md「认证保持 fail-closed」「测试默认不得连接真实数据库」）：
- 一律用裸 `TestClient(app)`（不进 `with`）—— 这恰好**不执行** lifespan，即缺陷场景本身，
  而不是用 mock 绕开真实的 middleware / lifespan 行为。
- lifespan=on 的对照组用 `asyncio.run` 进入真实 `lifespan()`，观察其启动即抛。
- 只改写 `settings` 单例属性（tests/conftest.py 已把 host 重置为 127.0.0.1 并清空 Key），
  不读真实 .env、不碰 SQLite/.arts、不绑定任何网卡、不出网。
"""

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.auth.startup_guard import is_unspecified_bind, unauthenticated_public_bind
from app.config import settings
from app.main import app as real_app

WILDCARD_HOSTS = ("0.0.0.0", "::")

# 缺陷复现清单里逐点名的受保护接口（方法与其真实注册方式一致）
PROTECTED_ENDPOINTS = (
    ("post", "/ingest/error"),
    ("get", "/api/dashboard/traces"),
    ("post", "/mcp"),
    ("get", "/internal/health"),
)

# 中间件 fail-closed 响应的机器可读标记。
# /internal/health 等端点自身也有 403 守卫，只比状态码无法区分「谁拒的」。
DENY_MARKER = "auth_not_configured"


def _configure(monkeypatch, *, host, api_key=None, api_keys=""):
    """把 settings 单例置成指定组合；Key 留空即「未配置鉴权」。"""
    monkeypatch.setattr(settings, "host", host)
    monkeypatch.setattr(settings, "api_key", api_key)
    monkeypatch.setattr(settings, "api_keys", api_keys)
    monkeypatch.setattr(settings, "rbac_enabled", False)


def _fresh_real_client() -> TestClient:
    """真实 app 的客户端，且强制重建中间件栈。

    Starlette 首次请求后缓存 `middleware_stack`，而 `AuthMiddleware.enabled` 是 **构造期快照**
    （middleware.py:__init__ 调一次 auth_enabled()）。不改这里、只改 settings 的话，
    同一进程内谁先跑就决定了鉴权开关 —— 那是测试基建的顺序耦合，不是被测行为。
    置 None 让 Starlette 在下一次请求按当前 settings 重新构建。
    """
    real_app.middleware_stack = None
    return TestClient(real_app)  # 裸构造：不进入 with → lifespan 不执行


def _build_auth_only_app():
    """全新 app + 真实 AuthMiddleware + 一个受保护路由。

    用于「已配置 Key」类用例：每例一个新 app，鉴权快照天然干净，不依赖上一条的 None 技巧。
    """
    import app.middleware as mw

    fresh = FastAPI()

    @fresh.get("/api/dashboard/traces")
    def _traces():
        return {"ok": True}

    fresh.add_middleware(mw.AuthMiddleware)
    return fresh


def _request(client: TestClient, method: str, path: str, headers=None):
    if method == "post":
        return client.post(path, headers=headers or {}, json={})
    return client.get(path, headers=headers or {})


def _error_code(resp) -> str | None:
    """安全取出响应体的 error_code（非 JSON 响应返回 None）。"""
    try:
        body = resp.json()
    except ValueError:
        return None
    return body.get("error_code") if isinstance(body, dict) else None


# ---------------------------------------------------------------------------
# 1. 缺陷本体：通配监听 + 无凭据 + lifespan 未运行 → 必须 fail-closed
# ---------------------------------------------------------------------------


class TestWildcardNoKeyMustDenyWithoutLifespan:
    """lifespan 不跑时，受保护接口绝不能被无凭据访问。"""

    @pytest.mark.parametrize("host", WILDCARD_HOSTS)
    @pytest.mark.parametrize(("method", "path"), PROTECTED_ENDPOINTS)
    def test_not_open_to_unauthenticated_request(self, monkeypatch, method, path, host):
        _configure(monkeypatch, host=host)
        client = _fresh_real_client()

        resp = _request(client, method, path)

        assert _error_code(resp) == DENY_MARKER, (
            f"{method.upper()} {path} 在 host={host} + 无 Key + lifespan 未运行时未fail-closed"
            f"（实际 {resp.status_code} / {resp.text[:120]!r}）—— 认证静默失效"
        )
        assert resp.status_code == 403

    def test_denial_is_attributable(self, monkeypatch):
        """拒绝必须可归因：告诉运维是「通配监听未配 Key」，而非笼统 Invalid API key。"""
        _configure(monkeypatch, host="0.0.0.0")
        client = _fresh_real_client()

        resp = client.get("/api/dashboard/traces")

        detail = str(resp.json().get("detail", ""))
        assert resp.status_code == 403
        assert "API_KEY" in detail or "API_KEYS" in detail

    def test_public_paths_stay_public(self, monkeypatch):
        """PUBLIC_PATHS 的免鉴权契约不变（健康探针仍需可用）。"""
        _configure(monkeypatch, host="0.0.0.0")
        client = _fresh_real_client()

        assert client.get("/health").status_code == 200

    def test_supplying_a_key_restores_normal_auth(self, monkeypatch):
        """同一危险组合一旦配上 Key，就回到常规 401/200 语义，而不是仍被 403 挡住。"""
        _configure(monkeypatch, host="0.0.0.0", api_key="s3cret")
        client = TestClient(_build_auth_only_app())

        assert client.get("/api/dashboard/traces").status_code == 401
        assert (
            client.get("/api/dashboard/traces", headers={"X-API-Key": "s3cret"}).status_code
            == 200
        )


# ---------------------------------------------------------------------------
# 2. 对照组：lifespan 正常运行时仍然启动即拒（既有 SEC-03 行为不得回归）
# ---------------------------------------------------------------------------


class TestLifespanOnStillRefusesToStart:
    @pytest.mark.parametrize("host", WILDCARD_HOSTS)
    def test_startup_raises(self, monkeypatch, host):
        _configure(monkeypatch, host=host)
        from app.main import lifespan

        async def _enter():
            async with lifespan(FastAPI()):
                return True

        with pytest.raises(RuntimeError, match="0.0.0.0|::"):
            asyncio.run(_enter())


# ---------------------------------------------------------------------------
# 3. 本地模式与既有边界：不得被过度收紧
# ---------------------------------------------------------------------------


class TestLocalModePreserved:
    def test_loopback_without_key_still_serves(self, monkeypatch):
        """127.0.0.1 + 无 Key = 单用户本地自用，必须继续放行（零配置不变量）。"""
        _configure(monkeypatch, host="127.0.0.1")
        client = _fresh_real_client()

        for method, path in PROTECTED_ENDPOINTS:
            resp = _request(client, method, path)
            assert _error_code(resp) != DENY_MARKER, f"本地回环模式被误伤：{method} {path}"
            assert resp.status_code < 500, f"{method} {path} 在本地模式下 5xx"

        # 核心只读接口必须真正可用，而不只是「没被我们的守卫挡住」
        assert client.get("/api/dashboard/traces").status_code == 200

    def test_ipv6_loopback_without_key_still_serves(self, monkeypatch):
        _configure(monkeypatch, host="::1")
        client = _fresh_real_client()

        assert client.get("/api/dashboard/traces").status_code == 200

    def test_specific_non_loopback_bind_is_not_hard_denied(self, monkeypatch):
        """既有口径：非通配的非回环地址只 WARNING、不硬拒（对应 validate_startup_configuration
        对 10.0.0.0 / 100.0.0.0 的语义）。中间件必须与之保持一致，不擅自扩大拒绝面。"""
        _configure(monkeypatch, host="10.0.0.0")
        client = _fresh_real_client()

        assert client.get("/api/dashboard/traces").status_code == 200


# ---------------------------------------------------------------------------
# 4. 已配置凭据的正常认证行为（单 Key / 多 Key）不得回归
# ---------------------------------------------------------------------------


class TestConfiguredKeysUnaffected:
    def test_single_key_missing_wrong_correct(self, monkeypatch):
        _configure(monkeypatch, host="0.0.0.0", api_key="legacy-secret")
        client = TestClient(_build_auth_only_app())

        assert client.get("/api/dashboard/traces").status_code == 401
        assert client.get(
            "/api/dashboard/traces", headers={"X-API-Key": "wrong"}
        ).status_code == 401
        assert client.get(
            "/api/dashboard/traces", headers={"X-API-Key": "legacy-secret"}
        ).status_code == 200

    def test_multi_key_rotation_both_valid(self, monkeypatch):
        _configure(monkeypatch, host="0.0.0.0", api_keys="key-new,key-old")
        client = TestClient(_build_auth_only_app())

        for key in ("key-new", "key-old"):
            assert client.get(
                "/api/dashboard/traces", headers={"X-API-Key": key}
            ).status_code == 200
        assert client.get(
            "/api/dashboard/traces", headers={"X-API-Key": "stale"}
        ).status_code == 401

    def test_bearer_header_still_accepted_on_wildcard(self, monkeypatch):
        _configure(monkeypatch, host="0.0.0.0", api_keys="key1")
        client = TestClient(_build_auth_only_app())

        assert client.get(
            "/api/dashboard/traces", headers={"Authorization": "Bearer key1"}
        ).status_code == 200


# ---------------------------------------------------------------------------
# 5. 判定谓词本身：中间件与启动校验必须共用同一套语义
# ---------------------------------------------------------------------------


class TestBindPredicateSingleSource:
    @pytest.mark.parametrize("host", WILDCARD_HOSTS)
    def test_unspecified_addresses(self, host):
        assert is_unspecified_bind(host) is True

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "::1", "10.0.0.0", "100.0.0.0", "localhost", "", "192.168.1.5"],
    )
    def test_not_unspecified(self, host):
        assert is_unspecified_bind(host) is False

    def test_guard_tracks_live_settings(self, monkeypatch):
        """谓词按请求实时读 settings 单例 —— AuthMiddleware.__init__ 的 enabled 是构造期
        快照，不能指望它覆盖运行期的配置变化。"""
        _configure(monkeypatch, host="0.0.0.0")
        assert unauthenticated_public_bind() is True

        _configure(monkeypatch, host="0.0.0.0", api_keys="k")
        assert unauthenticated_public_bind() is False

        _configure(monkeypatch, host="127.0.0.1")
        assert unauthenticated_public_bind() is False
