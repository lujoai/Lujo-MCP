"""单元测试：v0.6.0 LLM 与存储层 Prometheus 业务监控指标"""

import pytest
from app.observability import (
    record_llm_request,
    record_llm_cache_hit,
    record_storage_operation,
    record_pg_retry,
    _render_prometheus,
    _trim_metric_tables_if_needed,
    _MAX_METRIC_KEYS,
)


class TestBusinessMetrics:

    def test_record_llm_request_and_render(self):
        record_llm_request(
            provider="openai",
            model="gpt-4o-mini",
            status="success",
            duration_sec=0.456,
            prompt_tokens=150,
            completion_tokens=50,
        )

        rendered = _render_prometheus()
        assert "llm_requests_total" in rendered
        assert 'provider="openai"' in rendered
        assert 'model="gpt-4o-mini"' in rendered
        assert 'status="success"' in rendered
        assert "llm_request_duration_seconds_sum" in rendered
        assert "llm_tokens_total" in rendered
        assert 'type="prompt"' in rendered
        assert 'type="completion"' in rendered

    def test_record_llm_cache_hit(self):
        record_llm_cache_hit("knowledge_base")
        record_llm_cache_hit("exact_context")

        rendered = _render_prometheus()
        assert "llm_cache_hits_total" in rendered
        assert 'cache_type="knowledge_base"' in rendered
        assert 'cache_type="exact_context"' in rendered

    def test_record_storage_operation_and_pg_retry(self):
        record_storage_operation(
            store="pg",
            operation="query_traces",
            status="ok",
            duration_sec=0.012,
        )
        record_pg_retry("reconnect_execute")

        rendered = _render_prometheus()
        assert "storage_operations_total" in rendered
        assert 'store="pg"' in rendered
        assert 'operation="query_traces"' in rendered
        assert "pg_retries_total" in rendered
        assert 'operation="reconnect_execute"' in rendered

    def test_label_sanitization(self):
        # 包含换行与引号的非法 label 不应破坏 Prometheus 输出格式
        record_llm_request(
            provider="bad\nprovider\"",
            model="model\r\ntest",
            status="ok\t",
            duration_sec=0.1,
        )

        rendered = _render_prometheus()
        assert "\nprovider" not in rendered.splitlines()[-1]
        assert "badprovider" in rendered

    def test_metric_table_trimming_under_load(self):
        # 验证大基数保护逻辑正常
        from app.observability import _counter_lock, _llm_requests_total
        with _counter_lock:
            # 填入伪数据模拟溢出
            for i in range(_MAX_METRIC_KEYS + 10):
                _llm_requests_total[(f"prov_{i}", "mod", "ok")] = 1
            _trim_metric_tables_if_needed()
            assert len(_llm_requests_total) == 0
