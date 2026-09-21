"""分发链 smoke 校验（TST-3）。

覆盖 packaging/（PyInstaller 打包入口 + spec）与 npm/（元包 + 三平台包）的结构完整性，
防止「代码演进但分发资产失联」—— 此前的 SDK e2e 曾因 CI 不监控而长期失联。

仅做静态结构/契约校验（不启动进程、不调 PyInstaller/Node），保证在任何干净环境可跑。
版本一致性动态读取 app/__init__.py 的 __version__，避免硬编码造成版本漂移。
"""
import json
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
PACKAGING = ROOT / "packaging"
NPM = ROOT / "npm"
NPM_PACKAGES = NPM / "packages"
BROWSER_SDK = ROOT / "browser-sdk"


def _version() -> str:
    """从 app/__init__.py 读取版本单一来源。"""
    import app  # noqa: F401  # 仅用于读 __version__

    return app.__version__


def _load_pkg_json(pkg_dir: str) -> dict:
    with open(NPM_PACKAGES / pkg_dir / "package.json", encoding="utf-8") as f:
        return json.load(f)


# ── packaging/ ──────────────────────────────────────────────────────


def test_packaging_artifacts_exist():
    assert (PACKAGING / "entry_stdio.py").is_file()
    assert (PACKAGING / "lujo-mcp-server.spec").is_file()


def test_entry_stdio_exposes_main_entry():
    """entry_stdio 是 PyInstaller 入口，必须能从 app.mcp_server 导入 main。"""
    import importlib.util

    spec = importlib.util.spec_from_file_location("entry_stdio", PACKAGING / "entry_stdio.py")
    assert spec is not None, "entry_stdio.py 无法作为模块加载"
    module = importlib.util.module_from_spec(spec)
    assert hasattr(module, "__name__")
    # 核心契约：entry 引用的入口函数在源码中存在
    import app.mcp_server

    assert callable(getattr(app.mcp_server, "main", None))


def test_spec_has_required_datas_and_hiddenimports():
    """spec 必须携带 Web 演示页 / SDK，且排除 playwright（可选依赖）。

    WP6（Step 3）：migrations datas 与 psycopg2/asyncpg hiddenimports 已随
    PostgreSQL 移除一并删除，此处反向断言防止复活。
    """
    text = (PACKAGING / "lujo-mcp-server.spec").read_text(encoding="utf-8")
    # datas：内置 Web / SDK（PostgreSQL migrations 已归档，不再打包）
    assert '"app", "web"' in text
    assert '"browser-sdk"' in text
    assert '"migrations"' not in text, "migrations datas 应在 WP6 被移除"
    # 关键运行时库（MCP / FastAPI / LLM provider）
    for mod in ("mcp.server.stdio", "fastapi", "openai"):
        assert f'"{mod}"' in text, f"hiddenimports 缺少 {mod}"
    # PG 驱动不得再作为 hiddenimports 复活
    for mod in ("psycopg2", "asyncpg"):
        assert f'"{mod}"' not in text, f"{mod} hiddenimport 应在 WP6 被移除"
    # 可选依赖必须被排除（不打包进二进制）
    assert '"playwright"' in text
    # stdio MCP Server 需要控制台
    assert "console=True" in text
    assert 'name="lujo-mcp-server"' in text


# ── npm/ 元包 ───────────────────────────────────────────────────────


def test_npm_meta_package_structure():
    meta = _load_pkg_json("lujo-mcp")
    assert meta["name"] == "@lujoai/lujo-mcp"
    assert meta["version"] == _version()
    assert meta["bin"] == {"lujo-mcp-server": "bin/cli.js"}
    assert meta["files"] == ["bin", "browser-sdk"]
    # optionalDependencies 必须覆盖三平台且版本一致
    od = meta.get("optionalDependencies", {})
    assert set(od) == {
        "@lujoai/lujo-mcp-win32-x64",
        "@lujoai/lujo-mcp-linux-x64",
        "@lujoai/lujo-mcp-osx-arm64",
    }
    assert all(v == _version() for v in od.values())


def test_npm_meta_bin_scripts_exist():
    meta_dir = NPM_PACKAGES / "lujo-mcp"
    assert (meta_dir / "bin" / "cli.js").is_file()
    assert (meta_dir / "bin" / "check.js").is_file()
    assert (meta_dir / "scripts" / "check-clean-bin.js").is_file()
    # browser-sdk 随主包分发，供 CDN 直接引用（v0.7.2）
    # 分发副本必须与仓库源逐字节一致——漂移会让 CDN 用户拿到旧版 SDK
    src = BROWSER_SDK / "ai-debug.js"
    copy = meta_dir / "browser-sdk" / "ai-debug.js"
    assert src.is_file(), "仓库源 browser-sdk/ai-debug.js 缺失"
    assert copy.is_file(), "主包分发副本 browser-sdk/ai-debug.js 缺失"
    assert src.read_bytes() == copy.read_bytes(), (
        "browser-sdk/ai-debug.js 分发副本与仓库源不一致，"
        "请执行: cp browser-sdk/ai-debug.js npm/packages/lujo-mcp/browser-sdk/"
    )
    # cli.js 必须引用平台二进制（开箱即用核心）
    cli = (meta_dir / "bin" / "cli.js").read_text(encoding="utf-8")
    assert "lujo-mcp-server" in cli
    assert "lujoai" in cli
    # npm 主入口默认启用统一本地模式；--no-http 保留纯 stdio 回退。
    assert "--no-http" in cli
    assert "childArgs.push('--http')" in cli


# ── npm/ 平台包 ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pkg_dir", "name_suffix", "os_", "cpu"),
    [
        ("lujo-mcp-win32-x64", "lujo-mcp-win32-x64", "win32", "x64"),
        ("lujo-mcp-linux-x64", "lujo-mcp-linux-x64", "linux", "x64"),
        ("lujo-mcp-osx-arm64", "lujo-mcp-osx-arm64", "darwin", "arm64"),
    ],
)
def test_npm_platform_package_structure(pkg_dir, name_suffix, os_, cpu):
    pkg = _load_pkg_json(pkg_dir)
    assert pkg["name"] == f"@lujoai/{name_suffix}"
    assert pkg["version"] == _version()
    assert pkg["os"] == [os_]
    assert pkg["cpu"] == [cpu]
    # 平台包不得声明 bin：与元包同名（lujo-mcp-server）会让 npm 在同一安装树
    # 跳过全部同名 bin 链接，项目内安装后 node_modules/.bin 为空。二进制由
    # 元包 cli.js 按固定路径 bin/lujo-mcp-server(.exe) 定位，不需要 bin 字段。
    assert "bin" not in pkg
    assert pkg["files"] == ["bin"]
    # 平台包骨架脚本必须能重新生成该包的 package.json（避免手改漂移）
    assert (NPM / "scripts" / "gen-platform-packages.js").is_file()


# ── browser-sdk/ ────────────────────────────────────────────────────


def test_sdk_package_and_tests_exist():
    pkg = json.loads((BROWSER_SDK / "package.json").read_text(encoding="utf-8"))
    assert pkg["main"] == "ai-debug.js"
    assert (BROWSER_SDK / "ai-debug.js").is_file()
    # TST-3：SDK 契约单测必须存在（Node，CI 守护）
    assert (BROWSER_SDK / "test" / "sdk-core.test.js").is_file()
    # W15 / P4-SDK：包名与已发布的 @lujoai/lujo-mcp-node-sdk 同口径（此前仍叫
    # v0.5.2 品牌统一前的 ai-debug-sdk）。browser-sdk/ 只是开发与测试载体：真正
    # 对外分发的是元包内的 browser-sdk/ai-debug.js 副本，因此必须 private —— 否则
    # 在该目录里误跑一次 npm publish 就会去公网抢注这个 scoped 名。
    assert pkg["name"] == "@lujoai/lujo-mcp-browser-sdk"
    assert pkg.get("private") is True, "browser-sdk 是开发载体，必须声明 private 防误发布"


def test_browser_sdk_test_files_are_all_registered_in_ci():
    """W15：browser-sdk 的测试文件必须同时登记在 CI 清单里。

    ci.yml 显式列文件（不用目录形式：`node --test <dir>` 在部分 Node 版本下按模块
    解析导致 MODULE_NOT_FOUND），所以**新增测试文件不会自动进 CI** —— 该 workflow
    自己的注释就记录了 v0.6.7 传输修复测试因此漏出门禁、v0.6.9 漏登记
    sdk-destroy.test.js。这里把「package.json 的 test script」当作单一来源，
    与 ci.yml 清单做集合比较，两处任一漏登记即判红。
    """
    ci_text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    manifest = json.loads((BROWSER_SDK / "package.json").read_text(encoding="utf-8"))
    script_files = set(re.findall(r"test/([\w.-]+\.test\.js)", manifest["scripts"]["test"]))
    ci_files = set(re.findall(r"browser-sdk/test/([\w.-]+\.test\.js)", ci_text))

    assert script_files, "browser-sdk/package.json 的 test script 未列出任何测试文件"
    assert ci_files == script_files, (
        f"CI 清单与 npm test 清单不一致：仅 CI 有 {sorted(ci_files - script_files)}，"
        f"仅 npm test 有 {sorted(script_files - ci_files)}"
    )
    for name in sorted(script_files):
        assert (BROWSER_SDK / "test" / name).is_file(), f"清单登记的 {name} 在磁盘上不存在"


def test_every_published_npm_package_ships_readme_and_license():
    """W15 / P3-SDK-6：发布产物必须自带 README 与 LICENSE 正文。

    npm 只把**包目录内**的 README/LICENSE 打进 tarball（这两个文件名不受 files
    字段管辖，因此不能靠 files 白名单带出去）。此前元包与三个平台包都没有这两个
    文件 → registry 页面空白、无许可证正文，只剩 manifest 里一句 "license": "MIT"。
    LICENSE 必须与仓库根逐字节一致：改一次许可证正文却漏改分发副本，等于对外
    声明了两套条款。
    """
    root_license = (ROOT / "LICENSE").read_bytes()
    published_dirs = [
        NPM_PACKAGES / "lujo-mcp",
        NPM_PACKAGES / "lujo-mcp-win32-x64",
        NPM_PACKAGES / "lujo-mcp-linux-x64",
        NPM_PACKAGES / "lujo-mcp-osx-arm64",
        ROOT / "node-sdk",
    ]
    for pkg_dir in published_dirs:
        name = pkg_dir.name
        readme = pkg_dir / "README.md"
        license_file = pkg_dir / "LICENSE"
        assert readme.is_file(), f"{name} 缺 README.md（npm 包页面会空白）"
        assert readme.read_text(encoding="utf-8").strip(), f"{name} 的 README.md 是空文件"
        assert license_file.is_file(), f"{name} 缺 LICENSE（tarball 里没有许可证正文）"
        assert license_file.read_bytes() == root_license, f"{name}/LICENSE 与仓库根 LICENSE 不一致"
        manifest = json.loads((pkg_dir / "package.json").read_text(encoding="utf-8"))
        assert manifest.get("license") == "MIT", f"{name}/package.json 未声明 MIT，与 LICENSE 正文矛盾"


def test_builtin_browser_demos_are_zero_config_and_same_origin():
    """内置演示页不能携带失效测试密钥，也不能制造 localhost/127.0.0.1 跨域。"""
    for name in ("network_capture_demo.html", "silent_failure_demo.html"):
        text = (ROOT / "app" / "web" / name).read_text(encoding="utf-8")
        assert "test_secret_key_456" not in text
        assert "endpoint: window.location.origin" in text


def test_launcher_platform_allowlist_matches_published_packages():
    """FIX(v0.7.4 P0)：cli.js/check.js 平台白名单守卫。

    此前白名单写成无前缀后缀（win32-x64），与 platformPackageName() 产出的
    完整包名（lujo-mcp-win32-x64）永不相等 → npm 启动器在所有平台 100% 启动
    失败、postinstall 校验空转，自 v0.6.8 起连发多版本未被 CI 发现（发布冒烟
    只测裸二进制）。白名单必须与实际平台包目录同名且带完整前缀。
    """
    import re

    expected = {"lujo-mcp-win32-x64", "lujo-mcp-linux-x64", "lujo-mcp-osx-arm64"}
    actual_dirs = {
        p.name for p in NPM_PACKAGES.iterdir()
        if p.is_dir() and p.name.startswith("lujo-mcp-")
    }
    assert actual_dirs == expected, f"平台包目录 {actual_dirs} 与预期 {expected} 不一致"

    for script in ("bin/cli.js", "bin/check.js"):
        text = (NPM_PACKAGES / "lujo-mcp" / script).read_text(encoding="utf-8")
        m = re.search(r"supportedPlatforms = new Set\(\[([^\]]+)\]\)", text)
        assert m, f"{script} 缺 supportedPlatforms 定义"
        names = set(re.findall(r"'([^']+)'", m.group(1)))
        assert names == expected, (
            f"{script} 平台白名单 {names} 与实际平台包 {expected} 不一致——"
            "必须用完整包名 lujo-mcp-<platform>-<arch>（与 platformPackageName() 同口径）"
        )
