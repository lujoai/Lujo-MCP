"""单元测试：app.auth.key_rotation —— 多 key 轮换 + 单 key 向后兼容。"""

from app.auth.key_rotation import auth_enabled, get_valid_keys, verify_api_key
from app.config import settings


class TestGetValidKeys:
    """get_valid_keys: 读取有效 key 列表。"""

    def test_single_api_key_backward_compat(self, monkeypatch):
        """仅 settings.api_key 配置时，回退到单 key 列表。"""
        monkeypatch.setattr(settings, "api_keys", "")
        monkeypatch.setattr(settings, "api_key", "legacy-secret")
        assert get_valid_keys() == ["legacy-secret"]

    def test_multiple_api_keys_preferred_over_single(self, monkeypatch):
        """settings.api_keys 多 key 优先于 settings.api_key。"""
        monkeypatch.setattr(settings, "api_keys", "key1,key2,key3")
        monkeypatch.setattr(settings, "api_key", "legacy-secret")
        assert get_valid_keys() == ["key1", "key2", "key3"]

    def test_both_empty_returns_empty_list(self, monkeypatch):
        """api_keys 与 api_key 均空 → []（鉴权关闭）。"""
        monkeypatch.setattr(settings, "api_keys", "")
        monkeypatch.setattr(settings, "api_key", None)
        assert get_valid_keys() == []

    def test_whitespace_and_empty_entries_stripped(self, monkeypatch):
        """api_keys 含空白/空条目应被剔除。"""
        monkeypatch.setattr(settings, "api_keys", "  key1  , , key2 ,,")
        monkeypatch.setattr(settings, "api_key", None)
        assert get_valid_keys() == ["key1", "key2"]

    def test_api_keys_none_falls_back_to_api_key(self, monkeypatch):
        """api_keys 为 None 时回退到 api_key（防御性）。"""
        monkeypatch.setattr(settings, "api_keys", None)
        monkeypatch.setattr(settings, "api_key", "fallback")
        assert get_valid_keys() == ["fallback"]


class TestVerifyApiKey:
    """verify_api_key: 恒定时间校验，fail-closed。"""

    def test_valid_key_passes(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "secret-1")
        monkeypatch.setattr(settings, "api_key", None)
        assert verify_api_key("secret-1") is True

    def test_invalid_key_fails(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "secret-1")
        monkeypatch.setattr(settings, "api_key", None)
        assert verify_api_key("wrong") is False

    def test_empty_candidate_fails(self, monkeypatch):
        """空 candidate → False（fail-closed）。"""
        monkeypatch.setattr(settings, "api_keys", "secret-1")
        monkeypatch.setattr(settings, "api_key", None)
        assert verify_api_key("") is False

    def test_rotation_new_and_old_both_pass(self, monkeypatch):
        """轮换期：新旧 key 同时有效。"""
        monkeypatch.setattr(settings, "api_keys", "old-key,new-key")
        monkeypatch.setattr(settings, "api_key", None)
        assert verify_api_key("old-key") is True
        assert verify_api_key("new-key") is True

    def test_no_keys_configured_returns_false(self, monkeypatch):
        """无 key 配置时 verify_api_key 始终 False（由 middleware 通过 enabled 放行）。"""
        monkeypatch.setattr(settings, "api_keys", "")
        monkeypatch.setattr(settings, "api_key", None)
        assert verify_api_key("anything") is False

    def test_backward_compat_single_api_key(self, monkeypatch):
        """单 api_key（无 api_keys）仍可校验通过。"""
        monkeypatch.setattr(settings, "api_keys", "")
        monkeypatch.setattr(settings, "api_key", "legacy-secret")
        assert verify_api_key("legacy-secret") is True
        assert verify_api_key("not-legacy") is False


class TestAuthEnabled:
    """auth_enabled: 是否启用鉴权。"""

    def test_enabled_when_multiple_keys(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "k1,k2")
        monkeypatch.setattr(settings, "api_key", None)
        assert auth_enabled() is True

    def test_enabled_when_single_api_key(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "")
        monkeypatch.setattr(settings, "api_key", "legacy")
        assert auth_enabled() is True

    def test_disabled_when_no_keys(self, monkeypatch):
        monkeypatch.setattr(settings, "api_keys", "")
        monkeypatch.setattr(settings, "api_key", None)
        assert auth_enabled() is False
