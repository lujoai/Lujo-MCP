"""release-npm.yml 版本格式守卫的回归测试。

背景（v1.0.0 发布恢复，2026-09-28）：publish job 的「Verify version matches
app.__version__」步骤用内联正则校验 tag 版本号，旧正则只接受 0.x.x 形态，
导致 1.0.0 在任何 npm publish 之前被拒（run 36389375805）。本测试直接从
workflow 文件提取该正则，锁定其必须接受任意主版本（含预发布/构建元数据后缀）
并拒绝非法形态——防止未来再被改回 0.x 限定。
"""
import re
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "release-npm.yml"
# 与 workflow 内联脚本相同的提取目标：re.fullmatch(r"...", release_ver)
PATTERN_FINDER = re.compile(r're\.fullmatch\(r"([^"]+)",\s*release_ver\)')


def _guard_pattern() -> re.Pattern:
    text = WORKFLOW.read_text(encoding="utf-8")
    m = PATTERN_FINDER.search(text)
    assert m, "release-npm.yml 中未找到版本校验 fullmatch 正则（守卫步骤被改动？）"
    return re.compile(m.group(1))


def test_guard_accepts_valid_versions_including_major_one():
    pat = _guard_pattern()
    for ver in ("0.9.9", "1.0.0", "2.13.4", "0.9.10", "10.20.30"):
        assert pat.fullmatch(ver), f"合法版本 {ver} 应被接受"


def test_guard_accepts_prerelease_and_build_metadata():
    pat = _guard_pattern()
    # 与旧正则一致的后缀契约：单个 -/+ 段（预发布或构建元数据二选一）；
    # 组合形态（-beta+build.5）旧正则同样不接受，不属于被保留的支持范围
    for ver in ("1.0.0-rc.1", "0.9.9-beta", "0.9.9+build.5", "1.0.0+build.20260928"):
        assert pat.fullmatch(ver), f"预发布/构建元数据版本 {ver} 应被接受"


def test_guard_rejects_invalid_versions():
    pat = _guard_pattern()
    for ver in ("1.0", "v1.0.0", "1.0.0.0", "latest", "", "1.0.0-", "1..0", "1.0.x"):
        assert not pat.fullmatch(ver), f"非法版本 {ver!r} 应被拒绝"
