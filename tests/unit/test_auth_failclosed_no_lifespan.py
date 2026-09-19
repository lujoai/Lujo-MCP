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
    @pytest.mark.parametrize("host", [*WILDCARD_HOSTS, ""])
    def test_unspecified_addresses(self, host):
        # S2-F3：空串 host 在 syscall 层等价 INADDR_ANY（CPython bind(("",port))；
        # uvicorn 0.49 直接透传 sock.bind((host, port))），归入通配组。
        # 此前 "" 被断言为 False —— 该断言写的正是缺陷本身，现移入本组（详见 S2 报告）。
        assert is_unspecified_bind(host) is True

    @pytest.mark.parametrize(
        "host",
        ["127.0.0.1", "::1", "10.0.0.0", "100.0.0.0", "localhost", "192.168.1.5"],
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


# ---------------------------------------------------------------------------
# 6. 监听地址来源与暴露证据：
#    uvicorn 的 scope["server"] 是"已建立连接的本地端点"（绑 0.0.0.0 经回环进来
#    也是 127.0.0.1:ephemeral），不能当真实 bind；但它作为**权威暴露证据**可用 ——
#    配置声明回环本地而连接落在可路由 NIC 地址 = 外部宿主擅自改绑，必须
#    fail-closed；显式 LAN 配置（HOST=10.x）维持既有 WARNING-only 契约。
# ---------------------------------------------------------------------------


def _wrap_real_bind(inner_app, server_host: str):
    """ASGI3 包装器：强制注入 scope["server"]=(server_host, 9)，模拟连接本地端点。"""

    async def outer(scope, receive, send):
        if scope.get("type") in {"http", "websocket"}:
            scope = dict(scope)
            scope["server"] = [server_host, 9]
        await inner_app(scope, receive, send)

    return outer


def _client_with_real_bind(server_host: str) -> TestClient:
    real_app.middleware_stack = None
    return TestClient(_wrap_real_bind(real_app, server_host))


class TestRealBindAddressPreferred:
    @pytest.mark.parametrize("real_host", WILDCARD_HOSTS)
    def test_wildcard_local_endpoint_denied_even_if_host_config_says_loopback(
        self, monkeypatch, real_host
    ):
        """服务器填报本地端点即通配（规范允许的实现）→ 即便 HOST 说回环也 403。"""
        _configure(monkeypatch, host="127.0.0.1")
        client = _client_with_real_bind(real_host)

        resp = client.get("/api/dashboard/traces")
        assert _error_code(resp) == DENY_MARKER, (
            f"连接本地端点 {real_host} 但 HOST={settings.host} 时未 fail-closed"
            f"（{resp.status_code}）—— guard 只信 settings 的缺口未修复"
        )
        assert resp.status_code == 403

    def test_loopback_real_bind_serves(self, monkeypatch):
        _configure(monkeypatch, host="127.0.0.1")
        resp = _client_with_real_bind("127.0.0.1").get("/api/dashboard/traces")
        assert _error_code(resp) != DENY_MARKER
        assert resp.status_code == 200

    def test_routable_exposure_with_loopback_config_denied(self, monkeypatch):
        """配置声明本地，但连接实际落在可路由 NIC 地址 → 从未授权对外 → 拒绝。"""
        _configure(monkeypatch, host="127.0.0.1")
        resp = _client_with_real_bind("10.0.0.5").get("/api/dashboard/traces")
        assert _error_code(resp) == DENY_MARKER, "擅自改绑 NIC 地址的宿主未被拦截"
        assert resp.status_code == 403

    def test_explicit_lan_bind_keeps_warning_only_contract(self, monkeypatch):
        """显式 HOST=10.0.0.5 的 LAN 部署：既有 WARNING-only 契约不得收紧为拒绝。"""
        _configure(monkeypatch, host="10.0.0.5")
        resp = _client_with_real_bind("10.0.0.5").get("/api/dashboard/traces")
        assert _error_code(resp) != DENY_MARKER
        assert resp.status_code == 200

    @pytest.mark.parametrize(
        ("host_setting", "real_host"),
        [
            ("0.0.0.0", "192.168.1.50"),  # S2-F1 主格：通配配置 + LAN 端点
            ("0.0.0.0", "8.8.8.8"),       # 通配配置 + 公网端点
            ("::", "2001:db8::5"),        # S2-F2：IPv6 同形
            ("::", "fc00::1"),            # S2-F2：IPv6 ULA
            ("", "192.168.1.50"),         # S2-F3 请求层：空串 host + LAN 端点
        ],
    )
    def test_wildcard_settings_with_routable_real_bind_denied(
        self, monkeypatch, host_setting, real_host
    ):
        """S2-F1/F2/F3：配置口径为通配（含空串）时，可路由连接端点必须 fail-closed。

        修复前缺陷：resolve_bind_host 让 real_host 顶掉通配配置（F-1/F-2）；
        空串 host 则让两条证据同时沉默（F-3 请求层）—— 全部 200 放行。
        证据路径 1 必须始终查询配置口径的 settings.host，不得被连接端点顶掉。
        """
        _configure(monkeypatch, host=host_setting)
        resp = _client_with_real_bind(real_host).get("/api/dashboard/traces")
        assert _error_code(resp) == DENY_MARKER, (
            f"host={host_setting!r} + 连接端点 {real_host!r} 时未 fail-closed"
            f"（{resp.status_code} / {resp.text[:100]!r}）—— 通配配置被连接端点顶掉"
        )
        assert resp.status_code == 403

    def test_empty_host_with_loopback_real_bind_serves(self, monkeypatch):
        """S2-F3 定界：HOST='' 的拦截责任在启动期，请求层只对非回环端点收紧。

        空串 host 在 syscall 层等价 INADDR_ANY（C1 已把 is_unspecified_bind('')
        归为通配），但按单用户本地定位，回环连接端点仍放行（与下方
        test_settings_wildcard_but_real_bind_loopback_serves 同一取舍）。
        本用例锁定该分工：空串 host 的硬拒绝由启动期守卫承担
        （test_main.py::test_validate_rejects_empty_host_without_api_key）。
        """
        _configure(monkeypatch, host="")
        resp = _client_with_real_bind("127.0.0.1").get("/api/dashboard/traces")
        assert _error_code(resp) != DENY_MARKER
        assert resp.status_code == 200

    def test_settings_wildcard_but_real_bind_loopback_serves(self, monkeypatch):
        """反向不误伤：HOST=0.0.0.0 配置但连接本地端点是回环（CLI 覆盖）→ 放行。"""
        _configure(monkeypatch, host="0.0.0.0")
        resp = _client_with_real_bind("127.0.0.1").get("/api/dashboard/traces")
        assert _error_code(resp) != DENY_MARKER
        assert resp.status_code == 200

    @pytest.mark.parametrize("scope_host", ["testserver", "not-an-ip"])
    def test_unparseable_scope_falls_back_to_settings(self, monkeypatch, scope_host):
        """ASGI 服务器给了不可解析值（测试栈/定制宿主）→ 回落 settings.host，不猜测。"""
        _configure(monkeypatch, host="0.0.0.0")
        resp = _client_with_real_bind(scope_host).get("/api/dashboard/traces")
        assert _error_code(resp) == DENY_MARKER and resp.status_code == 403

        _configure(monkeypatch, host="127.0.0.1")
        resp = _client_with_real_bind(scope_host).get("/api/dashboard/traces")
        assert _error_code(resp) != DENY_MARKER
        assert resp.status_code == 200

    def test_real_wildcard_bind_with_key_normal_auth(self, monkeypatch):
        """通配 + 已配 Key：回到常规 401/200，不因证据来源改变认证语义。"""
        from fastapi import FastAPI

        from app.middleware import AuthMiddleware

        _configure(monkeypatch, host="127.0.0.1", api_key="real-bind-key")
        fresh = FastAPI()

        @fresh.get("/internal/health")
        def _ok():
            return {"ok": True}

        fresh.add_middleware(AuthMiddleware)
        client = TestClient(_wrap_real_bind(fresh, "0.0.0.0"))
        assert client.get("/internal/health").status_code == 401
        assert (
            client.get("/internal/health", headers={"X-API-Key": "wrong"}).status_code == 401
        )
        assert (
            client.get("/internal/health", headers={"X-API-Key": "real-bind-key"}).status_code
            == 200
        )
