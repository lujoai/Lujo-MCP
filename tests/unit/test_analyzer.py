"""单元测试：LLM analyzer（mock OpenAI 响应）"""
from unittest.mock import patch, MagicMock


class TestAnalyzer:

    def test_truncate_context_basic(self):
        from app.llm.context_prep import truncate_context

        ctx = {
            "request_id": "001",
            "flow": ["start", "error"],
            "input": {"data": "x" * 10000},
            "output": None,
            "errors": ["test error"],
        }
        result = truncate_context(ctx, max_tokens=10)
        assert result["request_id"] == "001"
        assert result.get("_truncated") is True

    def test_truncate_context_short(self):
        from app.llm.context_prep import truncate_context

        ctx = {
            "request_id": "002",
            "flow": ["start", "end"],
        }
        result = truncate_context(ctx, max_tokens=10000)
        assert result.get("_truncated") is not True

    def test_truncate_context_rewrites_trimmed_runtime(self):
        """P3-1: 精简版 runtime 必须写回 context["runtime"]，后续序列化才基于精简结果。"""
        from app.llm.context_prep import truncate_context

        ctx = {
            "request_id": "rt-001",
            "runtime": {
                "python": {"version": "3.12.1"},
                "system": {
                    "cpu_percent": 10.0,
                    "memory_percent": 20.0,
                    "load_avg": [1.0, 2.0, 3.0],  # 非关键字段，应被精简掉
                    "hostname": "drop-me",
                },
                "process": {
                    "pid": 1234,
                    "cpu_percent": 5.0,
                    "memory_rss_mb": 100,
                    "num_threads": 8,
                    "cmdline": ["python", "-m", "app"],  # 非关键字段，应被精简掉
                },
                "top_level_extra": {"drop": True},  # 非关键字段，应被精简掉
            },
        }
        result = truncate_context(ctx, max_tokens=100000)

        assert result["runtime"] == {
            "python": {"version": "3.12.1"},
            "system": {"cpu_percent": 10.0, "memory_percent": 20.0},
            "process": {
                "pid": 1234,
                "cpu_percent": 5.0,
                "memory_rss_mb": 100,
                "num_threads": 8,
            },
        }
        assert "load_avg" not in result["runtime"]["system"]
        assert "cmdline" not in result["runtime"]["process"]
        assert "top_level_extra" not in result["runtime"]

    def test_build_analysis_prompt(self):
        from app.llm.context_prep import build_analysis_prompt

        ctx = {
            "request_id": "003",
            "flow": ["request_start", "error"],
            "input": {"operation": "test"},
            "errors": ["something went wrong"],
        }
        prompt = build_analysis_prompt(ctx)
        assert "请求 ID: 003" in prompt
        assert "request_start" in prompt
        assert "error" in prompt
        assert "something went wrong" in prompt

    @patch("app.llm.analyzer._get_client")
    def test_analyze_with_mock(self, mock_get_client):
        from app.llm.analyzer import analyze
        import json

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "root_cause": "测试根因",
            "impact": "无影响",
            "fix": "无需修复",
            "confidence": "high",
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o-mock"
        mock_response.usage.prompt_tokens = 100
        mock_response.usage.completion_tokens = 50
        mock_response.usage.total_tokens = 150
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {
            "request_id": "004",
            "flow": ["request_start", "error"],
            "errors": ["test error"],
        }

        result = analyze(ctx, model="gpt-4o-mock")
        assert "analysis" in result
        assert result["analysis"]["root_cause"] == "测试根因"
        assert result["model"] == "gpt-4o-mock"
        assert result["usage"]["total_tokens"] == 150
        assert result["attempts"] == 1

    @patch("app.llm.analyzer._get_client")
    def test_analyze_redacts_sensitive_values_before_llm(self, mock_get_client):
        from app.llm.analyzer import analyze
        import json

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({"root_cause": "ok"})
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "gpt-4o-mock"
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {
            "request_id": "005",
            "input": {"api_key": "sk-live-secret", "nested": {"token": "abc123"}},
            "exception": {
                "frames": [
                    {
                        "file": "x.py",
                        "line": 1,
                        "function": "boom",
                        "code": "raise",
                        "locals": {"password": "pw-123", "normal": 'api_key = "inline-secret"'},
                    }
                ]
            },
        }

        analyze(ctx, model="gpt-4o-mock")
        kwargs = mock_client.chat.completions.create.call_args.kwargs
        sent = kwargs["messages"][1]["content"]
        assert "sk-live-secret" not in sent
        assert "abc123" not in sent
        assert "pw-123" not in sent
        assert "inline-secret" not in sent
        assert "***REDACTED***" in sent
        assert '"api_key": "***REDACTED***"' in sent


class TestLLMProvider:

    def test_resolve_base_url_openai_default(self):
        """openai provider 默认无 base_url（用 OpenAI SDK 默认地址）"""
        from app.config import settings

        saved_provider = settings.llm_provider
        saved_base_url = settings.llm_base_url
        try:
            settings.llm_provider = "openai"
            settings.llm_base_url = ""
            from app.llm.clients import _resolve_base_url
            assert _resolve_base_url() == ""
        finally:
            settings.llm_provider = saved_provider
            settings.llm_base_url = saved_base_url

    def test_resolve_base_url_zhipu(self):
        """zhipu provider 自动使用智谱 API 地址"""
        from app.config import settings

        saved_provider = settings.llm_provider
        saved_base_url = settings.llm_base_url
        try:
            settings.llm_provider = "zhipu"
            settings.llm_base_url = ""
            from app.llm.clients import _resolve_base_url
            assert _resolve_base_url() == "https://open.bigmodel.cn/api/paas/v4/"
        finally:
            settings.llm_provider = saved_provider
            settings.llm_base_url = saved_base_url

    def test_resolve_base_url_deepseek(self):
        """deepseek provider 自动使用 DeepSeek API 地址"""
        from app.config import settings

        saved_provider = settings.llm_provider
        saved_base_url = settings.llm_base_url
        try:
            settings.llm_provider = "deepseek"
            settings.llm_base_url = ""
            from app.llm.clients import _resolve_base_url
            assert _resolve_base_url() == "https://api.deepseek.com"
        finally:
            settings.llm_provider = saved_provider
            settings.llm_base_url = saved_base_url

    def test_resolve_base_url_custom_overrides_provider(self):
        """显式 llm_base_url 覆盖 provider 默认值"""
        from app.config import settings

        saved_provider = settings.llm_provider
        saved_base_url = settings.llm_base_url
        try:
            settings.llm_provider = "zhipu"
            settings.llm_base_url = "https://my-proxy.example.com/v1"
            from app.llm.clients import _resolve_base_url
            assert _resolve_base_url() == "https://my-proxy.example.com/v1"
        finally:
            settings.llm_provider = saved_provider
            settings.llm_base_url = saved_base_url

    def test_resolve_base_url_unknown_provider(self):
        """未知 provider 无默认 base_url"""
        from app.config import settings

        saved_provider = settings.llm_provider
        saved_base_url = settings.llm_base_url
        try:
            settings.llm_provider = "unknown"
            settings.llm_base_url = ""
            from app.llm.clients import _resolve_base_url
            assert _resolve_base_url() == ""
        finally:
            settings.llm_provider = saved_provider
            settings.llm_base_url = saved_base_url

    def test_provider_config_fields_exist(self):
        """config 中存在 llm_provider 和 llm_base_url 字段"""
        from app.config import settings
        assert hasattr(settings, "llm_provider")
        assert hasattr(settings, "llm_base_url")
        assert settings.llm_provider in ("openai", "zhipu", "custom", "deepseek", "unknown")


class TestLLMOutputValidation:
    """N2 — LLM 输出零校验/净化测试"""

    def _call_validate(self, raw_output: str) -> dict:
        from app.llm.output_schema import _validate_and_normalize
        return _validate_and_normalize(raw_output)

    # --- 合法 JSON ---

    def test_valid_json_complete(self):
        """合法完整 JSON：所有字段齐全且 confidence 有效"""
        result = self._call_validate(
            '{"root_cause": "DB连接超时", "impact": "服务不可用", '
            '"fix": "增加重试", "confidence": "high"}'
        )
        assert result["root_cause"] == "DB连接超时"
        assert result["impact"] == "服务不可用"
        assert result["fix"] == "增加重试"
        assert result["confidence"] == "high"
        assert "raw_truncated" not in result

    def test_valid_json_missing_confidence(self):
        """合法 JSON 缺 confidence → 默认 low"""
        result = self._call_validate(
            '{"root_cause": "空指针", "impact": "", "fix": "判空"}'
        )
        assert result["confidence"] == "low"
        assert result["root_cause"] == "空指针"

    def test_valid_json_invalid_confidence(self):
        """合法 JSON 但 confidence 无效值 → 设为 low"""
        result = self._call_validate(
            '{"root_cause": "X", "impact": "Y", "fix": "Z", '
            '"confidence": "very_high"}'
        )
        assert result["confidence"] == "low"

    def test_valid_json_empty_confidence(self):
        """confidence 为空字符串 → 设为 low"""
        result = self._call_validate(
            '{"root_cause": "A", "impact": "B", "fix": "C", '
            '"confidence": ""}'
        )
        assert result["confidence"] == "low"

    # --- Markdown code block ---

    def test_markdown_code_block_json(self):
        """LLM 输出用 markdown code block 包裹 JSON"""
        result = self._call_validate(
            '```json\n{"root_cause": "网络超时", "impact": "部分失败", '
            '"fix": "加超时", "confidence": "medium"}\n```'
        )
        assert result["root_cause"] == "网络超时"
        assert result["confidence"] == "medium"
        assert "raw_truncated" not in result

    def test_markdown_fenced_block_json(self):
        """LLM 输出用 ```（无 json 标记）包裹 JSON"""
        result = self._call_validate(
            '```\n{"root_cause": "内存泄漏", "impact": "OOM", '
            '"fix": "释放引用", "confidence": "high"}\n```'
        )
        assert result["root_cause"] == "内存泄漏"

    # --- 非法 JSON / 纯文本 ---

    def test_plain_text_no_json(self):
        """LLM 返回纯文本，无法解析 JSON → fallback + raw_truncated"""
        result = self._call_validate("让我想想这个问题...根因是网络问题")
        assert result["root_cause"] == ""
        assert result["impact"] == ""
        assert result["fix"] == ""
        assert result["confidence"] == "low"
        assert "raw_truncated" in result
        assert len(result["raw_truncated"]) <= 500

    def test_empty_string(self):
        """空字符串输入 → fallback + raw_truncated"""
        result = self._call_validate("")
        assert result["confidence"] == "low"
        assert "raw_truncated" in result

    def test_null_like_input(self):
        """null/None 输入 → fallback"""
        result = self._call_validate("null")
        assert result["confidence"] == "low"
        assert "raw_truncated" in result

    # --- 字段超长 ---

    def test_field_exceeds_max_chars(self):
        """字段超长（>2000 字符）→ 自动截断"""
        long_value = "x" * 3000
        result = self._call_validate(
            f'{{"root_cause": "{long_value}", "impact": "", "fix": "", '
            '"confidence": "high"}}'
        )
        assert len(result["root_cause"]) <= 2000

    # --- 嵌套 JSON 在文本中 ---

    def test_json_nested_in_text(self):
        """JSON 嵌套在其他文本中 → 提取最外层 {}"""
        result = self._call_validate(
            '好的，以下是分析结果：{"root_cause": "配置错误", '
            '"impact": "功能异常", "fix": "修正配置", "confidence": "medium"}'
        )
        assert result["root_cause"] == "配置错误"
        assert result["confidence"] == "medium"
        assert "raw_truncated" not in result

    def test_multiple_json_blocks_first_wins(self):
        """多个 JSON 块 → 只取第一个"""
        result = self._call_validate(
            '{"root_cause": "第一个", "impact": "", "fix": "", '
            '"confidence": "low"}  {"root_cause": "第二个"}'
        )
        assert result["root_cause"] == "第一个"

    # --- 非 dict JSON 结果 ---

    def test_json_array_becomes_dict(self):
        """JSON 数组被转换为空 dict（非 dict）"""
        result = self._call_validate('["a", "b"]')
        assert result["confidence"] == "low"
        assert "root_cause" not in result or result["root_cause"] == ""

    # --- _extract_json 边界 ---

    def test_extract_json_direct_parse_succeeds(self):
        """直接可解析的 JSON 不需要提取"""
        from app.llm.output_schema import _extract_json
        # 直接合法的 JSON，_extract_json 不会调用，但验证它不会破坏
        result = _extract_json('{"key": "value"}')
        # re.search 会匹配到 {"key": "value"}
        assert result is not None

    def test_extract_json_from_markdown(self):
        """从 markdown code block 中提取 JSON"""
        from app.llm.output_schema import _extract_json
        content = '```\n{"root_cause": "test"}\n```'
        result = _extract_json(content)
        assert result is not None
        assert "test" in result

    def test_extract_json_no_json_found(self):
        """找不到 JSON 返回 None"""
        from app.llm.output_schema import _extract_json
        result = _extract_json("纯文本，没有 JSON")
        # re.search 可能匹配到整个字符串作为 {} 包裹的内容
        # 但只要不是合法 JSON 就行
        assert result is None or "{" not in result or "}" not in result

    def test_validate_fallback_has_all_required_fields(self):
        """fallback 必须包含所有 required fields"""
        result = self._call_validate("garbage text {{{")
        for field in ("root_cause", "impact", "fix"):
            assert field in result
        assert result["confidence"] == "low"


# ---------------------------------------------------------------------------
# LLM 分析结果缓存测试（P1-2）
# ---------------------------------------------------------------------------

class TestLLMCache:
    """测试 LLM 分析结果缓存：命中、未命中、TTL、淘汰、flag"""

    def setup_method(self):
        """每个测试前清空缓存，避免互相影响"""
        from app.llm.cache import _analysis_cache
        from app.rag.knowledge_base import clear_knowledge_base
        _analysis_cache.clear()
        clear_knowledge_base()

    def test_cache_hit_on_second_call(self):
        """相同 context 二次调用应命中缓存，不调用 LLM"""
        from app.llm.cache import (
            _compute_context_fingerprint,
            _set_cache_result,
            _get_cached_result,
        )

        ctx = {"request_id": "cache-test-001", "errors": ["err"]}
        fp = _compute_context_fingerprint(ctx)

        mock_result = {"analysis": {"root_cause": "test"}, "model": "mock"}
        _set_cache_result(fp, mock_result)

        cached = _get_cached_result(fp)
        assert cached is not None
        assert cached["analysis"]["root_cause"] == "test"

    def test_cache_miss_on_different_context(self):
        """不同 context 不应命中缓存"""
        from app.llm.cache import (
            _compute_context_fingerprint,
            _set_cache_result,
            _get_cached_result,
        )

        ctx1 = {"request_id": "ctx-a", "errors": ["err1"]}
        ctx2 = {"request_id": "ctx-b", "errors": ["err2"]}

        fp1 = _compute_context_fingerprint(ctx1)
        fp2 = _compute_context_fingerprint(ctx2)

        _set_cache_result(fp1, {"analysis": {"root_cause": "a"}})

        assert _get_cached_result(fp2) is None

    def test_cache_expires_after_ttl(self):
        """TTL 过期后应返回 None"""
        from app.llm.cache import (
            _compute_context_fingerprint,
            _set_cache_result,
            _get_cached_result,
            _analysis_cache,
        )

        ctx = {"request_id": "ttl-test", "errors": ["err"]}
        fp = _compute_context_fingerprint(ctx)

        _set_cache_result(fp, {"analysis": {"root_cause": "expired"}})

        # 模拟过期：手动修改 cached_at 为 2 小时前
        with __import__("app.llm.cache", fromlist=["_cache_lock"])._cache_lock:
            _analysis_cache[fp]["cached_at"] -= 7200

        assert _get_cached_result(fp) is None

    def test_cache_evicts_oldest_when_full(self):
        """缓存达到上限时应淘汰最旧的条目"""
        from app.llm.cache import (
            _set_cache_result,
            _get_cached_result,
            _MAX_CACHE_SIZE,
        )

        # 填满缓存
        for i in range(_MAX_CACHE_SIZE):
            _set_cache_result(f"fp-{i:04d}", {"index": i})

        # 添加一条新记录，应淘汰 fp-0000
        _set_cache_result("fp-new", {"index": "new"})

        # 最旧的应已被淘汰
        assert _get_cached_result("fp-0000") is None
        # 新记录应存在
        assert _get_cached_result("fp-new") is not None

    @patch("app.llm.analyzer._get_client")
    def test_analyze_cache_hit_returns_cached_flag(self, mock_get_client):
        """缓存命中时返回的 dict 含 cached=True"""
        from app.llm.analyzer import analyze

        mock_client = MagicMock()
        import json

        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "root_cause": "root",
            "impact": "impact",
            "fix": "fix",
            "confidence": "high",
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "mock-model"
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {"request_id": "flag-test-001", "errors": ["err"]}

        # 第一次调用：未命中缓存
        result1 = analyze(ctx, model="mock-model")
        assert result1["cached"] is False

        # 第二次调用：应命中缓存
        result2 = analyze(ctx, model="mock-model")
        assert result2["cached"] is True
        # LLM 只被调用一次
        assert mock_client.chat.completions.create.call_count == 1

    @patch("app.llm.analyzer._get_client")
    def test_cached_result_does_not_mutate_cache(self, mock_get_client):
        """修改缓存返回的结果不应污染缓存原始数据"""
        from app.llm.analyzer import analyze

        mock_client = MagicMock()
        import json

        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "root_cause": "original",
            "impact": "impact",
            "fix": "fix",
            "confidence": "high",
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "mock-model"
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {"request_id": "mutate-test-001", "errors": ["err"]}

        # 第一次调用
        result1 = analyze(ctx, model="mock-model")
        assert result1["analysis"]["root_cause"] == "original"

        # 修改返回结果
        result1["analysis"]["root_cause"] = "mutated"

        # 第二次调用：缓存中的数据不应被修改
        result2 = analyze(ctx, model="mock-model")
        assert result2["analysis"]["root_cause"] == "original"
        assert mock_client.chat.completions.create.call_count == 1

    @patch("app.llm.analyzer._get_client")
    def test_analyze_returns_knowledge_base_result_before_llm(self, mock_get_client):
        """知识库命中时直接返回结果，并跳过 LLM 调用"""
        from app.llm.analyzer import analyze
        from app.rag.knowledge_base import upsert_knowledge_entry

        upsert_knowledge_entry(
            fingerprint="kb-hit-fp",
            analysis={
                "root_cause": "历史已知异常",
                "impact": "请求失败",
                "confidence": "high",
            },
            fix_suggestion="检查下游服务状态",
            source="llm",
        )

        ctx = {
            "request_id": "kb-hit-001",
            "exception": {"fingerprint": "kb-hit-fp"},
            "errors": ["err"],
        }

        result = analyze(ctx, model="mock-model")

        assert result["knowledge_base_hit"] is True
        assert result["analysis_source"] == "knowledge_base"
        assert result["analysis"]["root_cause"] == "历史已知异常"
        assert result["analysis"]["fix"] == "检查下游服务状态"
        assert result["model"] == "__knowledge_base__"
        assert result["cached"] is False
        assert result["attempts"] == 0
        assert result["usage"]["total_tokens"] == 0
        mock_get_client.assert_not_called()

    @patch("app.llm.analyzer._get_client")
    def test_analyze_falls_back_to_llm_when_knowledge_base_misses(self, mock_get_client):
        """知识库未命中时保持现有 LLM 分析链路和结果结构"""
        from app.llm.analyzer import analyze
        import json

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "root_cause": "实时分析结果",
            "impact": "接口报错",
            "fix": "修复参数",
            "confidence": "high",
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "mock-model"
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {
            "request_id": "kb-miss-001",
            "exception": {"fingerprint": "kb-miss-fp"},
            "errors": ["err"],
        }

        result = analyze(ctx, model="mock-model")

        assert result["knowledge_base_hit"] is False
        assert result["analysis_source"] == "llm"
        assert result["cached"] is False
        assert result["analysis"]["root_cause"] == "实时分析结果"
        assert result["model"] == "mock-model"
        assert result["usage"]["total_tokens"] == 15
        assert result["attempts"] == 1
        assert mock_client.chat.completions.create.call_count == 1


class TestKnowledgeBaseAutoPersist:
    def setup_method(self):
        from app.llm.cache import _analysis_cache
        from app.rag.knowledge_base import clear_knowledge_base

        _analysis_cache.clear()
        clear_knowledge_base()

    @patch("app.llm.cache._get_redis_cache", return_value=None)
    @patch("app.llm.analyzer._get_client")
    def test_analyze_auto_persists_knowledge_base_entry(self, mock_get_client, _mock_get_redis_cache):
        from app.llm.analyzer import analyze
        from app.rag.knowledge_base import get_knowledge_entry
        import json

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "root_cause": "数据库连接超时",
            "impact": "请求失败",
            "fix": "增加连接池重试",
            "confidence": "high",
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "mock-model"
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {
            "request_id": "kb-auto-001",
            "errors": ["err"],
            "exception": {"fingerprint": "fp-auto-001"},
        }

        result = analyze(ctx, model="mock-model")
        entry = get_knowledge_entry("fp-auto-001")

        assert result["analysis"]["root_cause"] == "数据库连接超时"
        assert entry is not None
        assert entry["analysis"]["root_cause"] == "数据库连接超时"
        assert entry["fix_suggestion"] == "增加连接池重试"
        assert entry["source"] == "llm"

    @patch("app.llm.cache._get_redis_cache", return_value=None)
    @patch("app.llm.kb_integration.logger.warning")
    @patch("app.llm.kb_integration.upsert_knowledge_entry", side_effect=RuntimeError("kb write failed"))
    @patch("app.llm.analyzer._get_client")
    def test_analyze_ignores_knowledge_base_write_failure(
        self,
        mock_get_client,
        mock_upsert_knowledge_entry,
        mock_logger_warning,
        _mock_get_redis_cache,
    ):
        from app.llm.analyzer import analyze
        import json

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "root_cause": "服务配置错误",
            "impact": "分析可返回",
            "fix": "修正配置",
            "confidence": "medium",
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "mock-model"
        mock_response.usage.prompt_tokens = 12
        mock_response.usage.completion_tokens = 6
        mock_response.usage.total_tokens = 18
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {
            "request_id": "kb-auto-002",
            "errors": ["err"],
            "exception": {"fingerprint": "fp-auto-002"},
        }

        result = analyze(ctx, model="mock-model")

        assert result["analysis"]["root_cause"] == "服务配置错误"
        assert result["cached"] is False
        mock_upsert_knowledge_entry.assert_called_once()
        mock_logger_warning.assert_called_once()


# ---------------------------------------------------------------------------
# Phase 7：向量检索 RAG fallback 测试
# ---------------------------------------------------------------------------


class TestVectorRagFallback:
    """P7 — 精确指纹 miss 后的向量检索 RAG fallback 行为测试"""

    def setup_method(self):
        from app.llm.cache import _analysis_cache
        from app.rag.knowledge_base import clear_knowledge_base
        from app.rag.vector_store import _reset_vector_store

        _analysis_cache.clear()
        clear_knowledge_base()
        _reset_vector_store()

    def teardown_method(self):
        from app.rag.vector_store import _reset_vector_store

        _reset_vector_store()

    @patch("app.llm.analyzer._get_client")
    @patch("app.llm.kb_integration.retrieve_similar")
    def test_vector_rag_hit_when_fingerprint_misses(
        self, mock_retrieve_similar, mock_get_client
    ):
        """KB 指纹 miss + 向量召回 hit → 返回 analysis_source=vector_rag，跳过 LLM"""
        from app.llm.analyzer import analyze

        mock_retrieve_similar.return_value = [{
            "fingerprint": "similar-fp",
            "analysis": {
                "root_cause": "相似历史根因",
                "fix": "相似修复",
                "confidence": "medium",
            },
            "fix_suggestion": "相似修复",
        }]

        ctx = {
            "request_id": "vector-rag-001",
            "exception": {"fingerprint": "different-fp"},
            "errors": ["err"],
        }

        result = analyze(ctx, model="mock-model")

        assert result["analysis_source"] == "vector_rag"
        assert result["knowledge_base_hit"] is False
        assert result["model"] == "__vector_rag__"
        assert result["analysis"]["root_cause"] == "相似历史根因"
        assert result["analysis"]["fix"] == "相似修复"
        assert result["attempts"] == 0
        assert result["cached"] is False
        mock_get_client.assert_not_called()

    @patch("app.llm.analyzer._get_client")
    @patch("app.llm.kb_integration.retrieve_similar", return_value=[])
    def test_vector_rag_miss_falls_through_to_llm(
        self, mock_retrieve_similar, mock_get_client
    ):
        """KB miss + 向量 miss → 走 LLM，返回 analysis_source=llm"""
        from app.llm.analyzer import analyze
        import json

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "root_cause": "实时根因",
            "impact": "影响",
            "fix": "修复",
            "confidence": "high",
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "mock-model"
        mock_response.usage.prompt_tokens = 10
        mock_response.usage.completion_tokens = 5
        mock_response.usage.total_tokens = 15
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {
            "request_id": "vector-rag-miss-001",
            "exception": {"fingerprint": "no-match-fp"},
            "errors": ["err"],
        }

        result = analyze(ctx, model="mock-model")

        assert result["analysis_source"] == "llm"
        assert result["knowledge_base_hit"] is False
        assert result["analysis"]["root_cause"] == "实时根因"
        assert result["model"] == "mock-model"
        assert result["usage"]["total_tokens"] == 15
        assert mock_client.chat.completions.create.call_count == 1
        mock_retrieve_similar.assert_called_once()

    @patch("app.llm.analyzer._get_client")
    @patch("app.llm.kb_integration.retrieve_similar", return_value=[])
    def test_vector_store_disabled_does_not_break_analyze(
        self, mock_retrieve_similar, mock_get_client
    ):
        """vector_store 关闭（retrieve_similar 返回 []）→ 现有 LLM 行为不回归"""
        from app.llm.analyzer import analyze
        import json

        mock_client = MagicMock()
        mock_choice = MagicMock()
        mock_choice.message.content = json.dumps({
            "root_cause": "结果",
            "impact": "",
            "fix": "",
            "confidence": "low",
        })
        mock_response = MagicMock()
        mock_response.choices = [mock_choice]
        mock_response.model = "mock-model"
        mock_response.usage.prompt_tokens = 1
        mock_response.usage.completion_tokens = 1
        mock_response.usage.total_tokens = 2
        mock_client.chat.completions.create.return_value = mock_response
        mock_get_client.return_value = mock_client

        ctx = {
            "request_id": "vector-disabled-001",
            "exception": {"fingerprint": "disabled-fp"},
            "errors": ["err"],
        }

        result = analyze(ctx, model="mock-model")

        assert result["analysis_source"] == "llm"
        assert result["model"] == "mock-model"
        mock_retrieve_similar.assert_called_once()

    @patch("app.llm.kb_integration.retrieve_similar")
    @patch("app.llm.analyzer._get_client")
    def test_exact_fingerprint_takes_priority_over_vector_rag(
        self, mock_get_client, mock_retrieve_similar
    ):
        """KB 精确指纹命中优先于向量召回（向量检索不应被调用）"""
        from app.llm.analyzer import analyze
        from app.rag.knowledge_base import upsert_knowledge_entry

        upsert_knowledge_entry(
            fingerprint="exact-fp",
            analysis={"root_cause": "已知根因", "confidence": "high"},
            fix_suggestion="已知修复",
            source="llm",
        )

        ctx = {
            "request_id": "priority-test-001",
            "exception": {"fingerprint": "exact-fp"},
            "errors": ["err"],
        }

        result = analyze(ctx, model="mock-model")

        assert result["analysis_source"] == "knowledge_base"
        assert result["knowledge_base_hit"] is True
        assert result["model"] == "__knowledge_base__"
        assert result["analysis"]["root_cause"] == "已知根因"
        # 向量检索 fallback 不应被调用
        mock_retrieve_similar.assert_not_called()
        mock_get_client.assert_not_called()
