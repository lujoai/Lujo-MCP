"""M2-B2 真实 LLM 成对实验的离线单元测试。

覆盖范围（全部离线：无网络、无 API Key、不读 .env、不连数据库）：
- provider 未配置时明确失败，且绝不静默回落到 fake；
- 注入 fake transport 才能产生调用（fake 只存在于测试注入路径）；
- API Key 不进入 manifest、日志、异常文本；
- timeout / HTTP 错误 / 限流 / 无效 JSON / 空补全 的错误分类；
- without_lujo 不携带 Lujo Context，with_lujo 才携带；
- 两组基础 prompt 与控制变量一致，实际 prompt 必须不同；
- run_count / run_index 正确；
- input_hash 继续通过 canonical 校验；
- 失败不伪造成 metrics=0；
- dry-run 不产生任何测量结果；
- 生成的 manifest 能通过 validate_payload 与 summarize_records；
- summarize 在未评分时给出 no_measurements 横幅。
"""

from __future__ import annotations

import contextlib
import http.server
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import fields as _dc_fields
from pathlib import Path

import pytest

from benchmark import llm_experiment as lx
from benchmark import prompting as pr
from benchmark import runner
from benchmark import experiment as exp
from benchmark import llm_provider as lp
from benchmark.cases import BENCHMARK_CASES, get_case
from benchmark.schemas import BenchmarkCase

REPO_ROOT = Path(__file__).resolve().parents[2]


# ── 测试夹具：注入式 fake transport（生产路径永不含 fake）──


def _ok_body(content: str = '{"root_cause": "stub"}') -> str:
    return json.dumps(
        {
            "choices": [
                {"message": {"role": "assistant", "content": content}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


def _transport_returning(status: int, body: str, headers: dict[str, str] | None = None):
    calls: list[dict] = []

    def _transport(url, request_headers, request_body, timeout_s):
        calls.append(
            {
                "url": url,
                "headers": dict(request_headers),
                "body": json.loads(request_body.decode("utf-8")),
                "timeout_s": timeout_s,
            }
        )
        return status, body, headers or {"x-request-id": "req-1"}

    return _transport, calls


def _config(**overrides) -> lp.LLMConfig:
    base = {
        "base_url": "https://api.example.com/v1",
        "api_key": "sk-test-SECRET-abcdef123456",
        "model": "test-model",
        "timeout_s": 5.0,
        "temperature": 0.0,
        "max_tokens": 128,
        "max_retries": 0,
    }
    base.update(overrides)
    return lp.LLMConfig(**base)


def _manifest_for(case_ids: list[str], *, run_count: int = 1) -> dict:
    cases = [get_case(cid) for cid in case_ids]
    return exp.build_manifest(cases, run_count=run_count, model="test-model", repo_sha="0" * 40)


def _run_manifest(manifest: dict, transport, **kwargs) -> dict:
    """对全部记录槽执行一次（注入 fake transport）。"""
    plan = lx.build_plan(manifest, skip_measured=False)
    provider = lp.LLMProvider(_config(), transport=transport)
    return lx.execute_plan(
        manifest,
        plan,
        provider=provider,
        provider_model="test-model",
        temperature=0.0,
        max_tokens=128,
        timeout_s=5.0,
        endpoint_host="https://api.example.com/v1",
        **kwargs,
    )


# ── provider 配置 ──


class TestProviderConfig:
    def test_missing_env_fails_loudly(self):
        """未配置 provider 必须明确失败，且错误里只出现变量名（无回落）。"""
        with pytest.raises(lp.LLMNotConfiguredError) as ei:
            lp.load_config_from_env(env={})
        message = str(ei.value)
        assert lp.ENV_BASE_URL in message
        assert lp.ENV_API_KEY in message
        assert lp.ENV_MODEL in message

    def test_never_falls_back_to_production_key(self):
        """存在 OPENAI_API_KEY 也不算配置（防误用生产凭据）。"""
        env = {"OPENAI_API_KEY": "sk-production-key-xyz"}
        with pytest.raises(lp.LLMNotConfiguredError):
            lp.load_config_from_env(env=env)

    def test_partial_config_reports_only_missing(self):
        env = {lp.ENV_BASE_URL: "https://x/v1", lp.ENV_API_KEY: "k"}
        with pytest.raises(lp.LLMNotConfiguredError) as ei:
            lp.load_config_from_env(env=env)
        assert lp.ENV_MODEL in str(ei.value)
        assert lp.ENV_API_KEY not in str(ei.value)

    def test_config_from_env_ok(self):
        env = {
            lp.ENV_BASE_URL: "https://x/v1",
            lp.ENV_API_KEY: "k",
            lp.ENV_MODEL: "m",
            lp.ENV_TIMEOUT: "12",
            lp.ENV_TEMPERATURE: "0.3",
        }
        cfg = lp.load_config_from_env(env=env)
        assert cfg.timeout_s == 12.0
        assert cfg.temperature == 0.3

    def test_endpoint_host_strips_query_and_credentials(self):
        cfg = _config(base_url="https://user:pw@api.example.com/v1?api_key=leak")
        host = cfg.endpoint_host()
        assert host == "https://api.example.com/v1"
        assert "leak" not in host
        assert "pw" not in host

    def test_no_implicit_retry_by_default(self):
        transport, calls = _transport_returning(500, "boom")
        provider = lp.LLMProvider(_config(max_retries=0), transport=transport)
        result = provider.call([{"role": "user", "content": "hi"}])
        assert not result.ok
        assert len(calls) == 1
        assert provider.config.max_retries == 0


# ── 错误分类 ──


class TestErrorClassification:
    def _call(self, transport) -> lp.LLMResult:
        provider = lp.LLMProvider(_config(), transport=transport)
        return provider.call([{"role": "user", "content": "hi"}])

    def test_timeout(self):
        def _timeout(url, headers, body, timeout_s):
            raise TimeoutError("timed out")

        result = self._call(_timeout)
        assert result.error_class == lp.ERROR_TIMEOUT
        assert result.text is None

    def test_connection_error(self):
        import urllib.error

        def _conn(url, headers, body, timeout_s):
            raise urllib.error.URLError("dns failure")

        result = self._call(_conn)
        assert result.error_class == lp.ERROR_CONNECTION

    def test_rate_limited_records_retry_after(self):
        transport, _ = _transport_returning(
            429, "slow down", {"retry-after": "7", "x-request-id": "r2"}
        )
        result = self._call(transport)
        assert result.error_class == lp.ERROR_RATE_LIMITED
        assert result.http_status == 429
        assert result.retry_after_s == 7.0

    def test_http_4xx_and_5xx(self):
        assert self._call(_transport_returning(401, "unauthorized")[0]).error_class == lp.ERROR_HTTP_4XX
        assert self._call(_transport_returning(503, "unavailable")[0]).error_class == lp.ERROR_HTTP_5XX

    def test_invalid_json(self):
        result = self._call(_transport_returning(200, "not-json-at-all")[0])
        assert result.error_class == lp.ERROR_INVALID_JSON

    def test_empty_completion(self):
        result = self._call(_transport_returning(200, _ok_body("   "))[0])
        assert result.error_class == lp.ERROR_EMPTY_COMPLETION

    def test_4xx_not_retried_even_with_retries_allowed(self):
        transport, calls = _transport_returning(401, "unauthorized")
        provider = lp.LLMProvider(_config(max_retries=3), transport=transport)
        result = provider.call([{"role": "user", "content": "hi"}])
        assert result.error_class == lp.ERROR_HTTP_4XX
        assert len(calls) == 1


# ── 泄漏防护 ──


class TestSecretRedaction:
    def test_redact_authorization_header(self):
        text = "Authorization: Bearer sk-test-SECRET-abcdef123456"
        out = lp.redact(text)
        assert "abcdef123456" not in out
        assert "<redacted" in out

    def test_redact_api_key_patterns(self):
        for raw in (
            "api_key=sk-live-9999999999",
            "token: abcdef1234567890",
            "?api_key=sk-qwerty12345",
            '{"api_key": "sk-inside-json-123"}',
        ):
            out = lp.redact(raw)
            assert "sk-" not in out or "<redacted" in out
            assert "sk-live-9999999999" not in out
            assert "qwerty12345" not in out
            assert "inside-json-123" not in out

    def test_error_body_redacted_and_truncated(self):
        leaked = "Incorrect API key provided: sk-leaked-abcdef123456. " + "x" * 2000
        result = lp.LLMProvider(_config(), transport=_transport_returning(401, leaked)[0]).call(
            [{"role": "user", "content": "hi"}]
        )
        assert result.error_message is not None
        assert "sk-leaked-abcdef123456" not in result.error_message
        assert len(result.error_message) <= 400

    def test_manifest_never_contains_api_key(self):
        secret = "sk-test-SECRET-abcdef123456"
        manifest = _manifest_for(["api_500_none_attribute"])
        transport, _ = _transport_returning(200, _ok_body())
        result = _run_manifest(manifest, transport)
        blob = json.dumps(result, ensure_ascii=False)
        assert secret not in blob
        assert "Authorization" not in blob

    def test_transport_receives_key_in_header_only(self):
        """凭据只在请求头里；请求体与记录中都不得出现。"""
        transport, calls = _transport_returning(200, _ok_body())
        _run_manifest(_manifest_for(["api_500_none_attribute"]), transport)
        assert calls[0]["headers"]["Authorization"].startswith("Bearer ")
        assert "Authorization" not in json.dumps(calls[0]["body"])
        assert "sk-test-SECRET" not in json.dumps(calls[0]["body"])

    def test_unexpected_transport_exception_redacted(self):
        """transport 抛出非 OSError 异常时，凭据不得出现在错误文本里。"""
        secret = "sk-test-SECRET-abcdef123456"

        def _raising(url, headers, body, timeout_s):
            raise RuntimeError(f"upstream rejected Authorization: Bearer {secret}")

        provider = lp.LLMProvider(_config(), transport=_raising)
        result = provider.call([{"role": "user", "content": "hi"}])
        assert not result.ok
        assert result.error_message is not None
        assert secret not in result.error_message
        assert "<redacted" in result.error_message

    def test_key_never_in_manifest_even_on_failure(self):
        secret = "sk-test-SECRET-abcdef123456"

        def _raising(url, headers, body, timeout_s):
            raise RuntimeError(f"boom with {secret}")

        manifest = _run_manifest(_manifest_for(["api_500_none_attribute"]), _raising)
        assert secret not in json.dumps(manifest, ensure_ascii=False)


# ── Prompt 契约 ──


class TestPromptContract:
    def test_without_message_has_no_context(self):
        case = get_case("api_500_none_attribute")
        text = pr.render_user_message(case.user_description, None)
        assert case.user_description in text
        assert "trace_id" not in text
        assert "exception" not in text.lower()

    def test_with_message_contains_context(self):
        case = get_case("api_500_none_attribute")
        with_ctx = pr.render_user_message(case.user_description, case.lujo_context)
        without_ctx = pr.render_user_message(case.user_description, None)
        assert with_ctx != without_ctx
        assert "trace_id" in with_ctx
        assert with_ctx.startswith(without_ctx[: len(without_ctx) - 2])

    def test_prompt_never_leaks_ground_truth(self):
        """任何组的 prompt 都不得包含标题、类别或标准答案。"""
        for case in BENCHMARK_CASES:
            for context in (None, case.lujo_context):
                text = pr.render_user_message(case.user_description, context)
                assert case.expected_root_cause not in text
                for item in case.expected_evidence:
                    assert item not in text
                assert case.title not in text
                assert case.category not in text

    def test_base_prompt_hash_identical_across_groups(self):
        case = get_case("api_500_none_attribute")
        a = pr.compute_base_prompt_hash(case.case_id, case.user_description, None)
        b = pr.compute_base_prompt_hash(case.case_id, case.user_description, case.lujo_context)
        assert a == b

    def test_prompt_hash_differs_across_groups(self):
        case = get_case("api_500_none_attribute")
        w = pr.compute_prompt_hash(pr.build_messages(case.user_description, None))
        wi = pr.compute_prompt_hash(pr.build_messages(case.user_description, case.lujo_context))
        assert w != wi

    def test_system_prompt_shared_and_stable(self):
        case = get_case("frontend_blank_fetch_error")
        w = pr.build_messages(case.user_description, None)
        wi = pr.build_messages(case.user_description, case.lujo_context)
        assert w[0] == wi[0]
        assert pr.SYSTEM_PROMPT == w[0]["content"]
        assert "root_cause" in pr.SYSTEM_PROMPT
        for field in pr.REQUIRED_RESPONSE_FIELDS:
            assert field in pr.SYSTEM_PROMPT

    def test_prompt_hash_is_stable_across_calls(self):
        case = get_case("db_error_null_column")
        msgs = pr.build_messages(case.user_description, case.lujo_context)
        assert pr.compute_prompt_hash(msgs) == pr.compute_prompt_hash(msgs)


# ── 答案泄露守卫：模型可见输入不得含标准答案（含意译，不只字面）──


def _model_visible_strings(case) -> list[str]:
    """模型可见文本：user_description + with 组渲染出的 context 段落。

    不含 system prompt（固定文本）与 case 元数据。without 组可见文本被
    user_description / system prompt 覆盖（逐字相同），故只取 with 渲染体。
    """
    text = (
        case.user_description
        + "\n"
        + pr.render_user_message(case.user_description, case.lujo_context)
    )
    return [line for line in text.splitlines() if line.strip()]


_LEAK_STOPWORDS = frozenset(
    {
        "的", "了", "是", "在", "和", "与", "或", "把", "被", "而", "非", "均",
        "该", "这", "那", "及", "为", "使", "导致", "问题",
    }
)


def _is_cjk(ch: str) -> bool:
    return "一" <= ch <= "鿿"


def _content_grams(text: str, n: int = 4) -> set[str]:
    """抽取**连续中文片段**内的字符 n-gram。

    只在最长中文连续段内取 gram（长度 ≥ n 的段），从而：
    - 抓到中文件句式复述（如 `且未设默认值` / `循环内逐条查库`）；
    - 忽略 `NOT NULL` / `item_detail` 这类双方共有的技术记号；
    - 忽略 `admin 角色` 这类仅共享 2 个中文字符的合法证据（其连续中文段只有 `角色`）。
    """
    out: set[str] = set()
    run = ""
    for ch in text + "\0":
        if _is_cjk(ch):
            run += ch
            continue
        if len(run) >= n:
            for i in range(len(run) - n + 1):
                gram = run[i : i + n]
                if all(c in _LEAK_STOPWORDS for c in gram):
                    continue
                out.add(gram)
        run = ""
    return out


def _recent_diff_text(case) -> list[str]:
    """recent_diffs 各条目里的字符串值（summary / diff / 其它）。"""
    texts: list[str] = []
    for entry in case.lujo_context.get("recent_diffs") or []:
        for value in entry.values():
            if isinstance(value, str) and value.strip():
                texts.append(value)
    return texts


class TestNoAnswerLeakage:
    """fixture 不得把标准答案以任何形式（含意译复述）交给模型。"""

    def test_guard_catches_historical_leak_and_keeps_derivable_evidence(self):
        """守卫有效性 + 证据保留。

        1) 守卫必须能识别**历史上真实存在**的泄露句（Case 3/5 的旧 summary）——
           把它们与各自 gold 做 n-gram 比对，重叠非空即证明守卫不是空转。
        2) 当前 fixture 的 recent_diffs 必须仍然非空（保留可推理的运行证据），
           且不得与 gold 近似重合。
        """
        historical = {
            "db_error_null_column": "users.phone 改为 NOT NULL 且未设默认值",
            "perf_slow_nplus1": "列表查询改为循环内逐条查库（引入 N+1）",
        }
        for case_id, leak in historical.items():
            case = get_case(case_id)
            gold = _content_grams(case.expected_root_cause)
            assert _content_grams(leak) & gold, (
                f"守卫未能识别 {case_id} 的历史泄露句——n-gram 检测失效"
            )
            texts = _recent_diff_text(case)
            assert texts, f"{case_id} 应保留 recent_diffs 运行证据"
            for text in texts:
                overlap = _content_grams(text) & gold
                assert not overlap, (
                    f"{case_id} 的 recent_diffs 复述了 expected_root_cause："
                    f"重叠片段 {sorted(overlap)}（{text!r}）"
                )

    def test_no_context_field_paraphrases_expected_root_cause(self):
        """6 个 case：任何模型可见字符串都不得与 expected_root_cause 近似重合。"""
        for case in BENCHMARK_CASES:
            gold = _content_grams(case.expected_root_cause)
            for line in _model_visible_strings(case):
                overlap = _content_grams(line) & gold
                assert not overlap, (
                    f"{case.case_id} 模型可见文本复述根因：重叠片段 {sorted(overlap)}（{line!r}）"
                )

    def test_prompt_never_leaks_expected_verification_or_metrics(self):
        """expected_* 与 evaluation_metrics 均不得出现在任何一组的 prompt 中。"""
        case_fields = {f.name for f in _dc_fields(BenchmarkCase)}
        assert {"expected_root_cause", "expected_evidence"} <= case_fields
        # 目前不存在 expected_verification 字段；若日后新增，必须显式纳入泄露守卫。
        assert "expected_verification" not in case_fields
        for case in BENCHMARK_CASES:
            for context in (None, case.lujo_context):
                text = pr.render_user_message(case.user_description, context)
                assert case.expected_root_cause not in text
                for item in case.expected_evidence:
                    assert item not in text
                assert case.title not in text
                assert case.category not in text
                assert "expected_root_cause" not in text
                assert "expected_verification" not in text
                assert "evaluation_metrics" not in text

    def test_evidence_is_derivable_from_context_not_copied(self):
        """每条 expected_evidence 必须指向真实存在的 context 字段（不得凭空。）

        该断言限定为「evidence 描述的字段在 context 中确实存在」，不做语义匹配。
        """
        field_map = {
            "exception": "exception",
            "堆栈": "exception",
            "IntegrityError": "exception",
            "请求": "request",
            "network_trace": "network_trace",
            "ui_events": "ui_events",
            "console": "console",
            "recent_diffs": "recent_diffs",
            "git_blame": "git_blame",
            "runtime": "runtime",
            "spec_diffs": "spec_diffs",
            "related_specs": "related_specs",
            "trace": "trace",
            "auth_context": "auth_context",
            "resolved_frames": "resolved_frames",
            "original": "resolved_frames",
            "code_snippets": "code_snippets",
        }
        for case in BENCHMARK_CASES:
            for item in case.expected_evidence:
                referenced: list[str] = []
                working = item
                # 长键优先匹配并抹除，避免 `trace` 命中 `network_trace` 内部。
                for key in sorted(field_map, key=len, reverse=True):
                    if key in working:
                        referenced.append(field_map[key])
                        working = working.replace(key, "")
                assert referenced, f"{case.case_id} 的 evidence {item!r} 未指向任何 context 字段"
                for field in referenced:
                    assert case.lujo_context.get(field), (
                        f"{case.case_id} 的 evidence {item!r} 指向空字段 {field!r}"
                    )

    def test_recent_diff_summary_stays_observational(self):
        """recent_diffs 的文本只能是可观测的运行证据，不得含结论性措辞。"""
        forbidden = ("根因", "N+1", "n+1", "导致", "引入", "改为", "未设默认值", "循环内", "判定")
        for case in BENCHMARK_CASES:
            for text in _recent_diff_text(case):
                for token in forbidden:
                    assert token not in text, (
                        f"{case.case_id} recent_diffs 含结论性措辞 {token!r}：{text!r}"
                    )

    def test_gold_labels_are_evidence_backed(self):
        """Case 2/6 的 gold label 不得包含 context 无法证明的额外子句。

        这两例的 expected_root_cause 曾分别断言「try/catch 静默吞掉」与
        「后端返回列表含空元素」——context 均无对应证据。
        """
        case2 = get_case("frontend_blank_fetch_error")
        assert "try/catch" not in case2.expected_root_cause
        assert "静默吞掉" not in case2.expected_root_cause
        case6 = get_case("frontend_minified_sourcemap")
        assert "后端" not in case6.expected_root_cause
        assert "为空" not in case6.expected_root_cause


# ── 组语义 ──


class TestGroupSemantics:
    def test_group_context_without_is_always_none(self):
        case = get_case("api_500_none_attribute")
        assert lx.group_context(case, exp.GROUP_WITHOUT) is None
        assert lx.group_context(case, exp.GROUP_WITHOUT, allow_context=False) is None

    def test_group_context_with_returns_fixture(self):
        case = get_case("api_500_none_attribute")
        assert lx.group_context(case, exp.GROUP_WITH) == case.lujo_context

    def test_without_record_has_no_context_hash(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(200, _ok_body())[0]
        )
        for record in manifest["records"]:
            if record["group"] == exp.GROUP_WITHOUT:
                assert record["context_hash"] is None
                assert record["context_mode"] == exp.CONTEXT_MODE_NONE
            else:
                assert record["context_hash"] is not None
                assert record["context_mode"] == exp.CONTEXT_MODE_FIXTURE

    def test_paired_control_vars_and_prompt_invariants(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(200, _ok_body())[0]
        )
        by_pair: dict[tuple, dict] = {}
        for r in manifest["records"]:
            by_pair.setdefault((r["case_id"], r["run_index"]), {})[r["group"]] = r
        for pair in by_pair.values():
            w, wi = pair[exp.GROUP_WITHOUT], pair[exp.GROUP_WITH]
            assert w["base_prompt_hash"] == wi["base_prompt_hash"]
            assert w["prompt_hash"] != wi["prompt_hash"]
            assert w["model"] == wi["model"]
            assert w["temperature"] == wi["temperature"]
            assert w["input_hash"] == wi["input_hash"]

    def test_validate_payload_accepts_executed_manifest(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(200, _ok_body())[0]
        )
        assert exp.validate_payload(manifest) == []

    def test_run_count_and_run_index(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"], run_count=3),
            _transport_returning(200, _ok_body())[0],
        )
        indices = sorted(r["run_index"] for r in manifest["records"])
        assert indices == [1, 1, 2, 2, 3, 3]
        assert exp.validate_payload(manifest) == []

    def test_input_hash_matches_canonical(self):
        manifest = _run_manifest(
            _manifest_for(["frontend_blank_fetch_error"]), _transport_returning(200, _ok_body())[0]
        )
        case = get_case("frontend_blank_fetch_error")
        for record in manifest["records"]:
            assert record["input_hash"] == exp.compute_input_hash(
                case.case_id, case.user_description
            )


# ── metrics 与失败语义 ──


class TestMetricsSemantics:
    def test_metrics_stay_none_on_success(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(200, _ok_body())[0]
        )
        for record in manifest["records"]:
            assert all(v is None for v in record["metrics"].values())

    def test_failure_does_not_forge_zero_metrics(self):
        """请求失败必须留错误证据，且绝不能把失败写成 0 分。"""
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(500, "server error")[0]
        )
        for record in manifest["records"]:
            assert all(v is None for v in record["metrics"].values())
            assert record["execution"]["status"] == "error"
            assert record["execution"]["error_class"] == lp.ERROR_HTTP_5XX

    def test_failure_records_remain_paired_and_valid(self):
        """一侧失败也必须保持配对结构可校验（不得出现单侧孤儿记录）。"""
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(429, "slow down")[0]
        )
        assert exp.validate_payload(manifest) == []
        assert len(manifest["records"]) == 2

    def test_execution_metadata_recorded(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(200, _ok_body())[0]
        )
        execution = manifest["records"][0]["execution"]
        assert execution["status"] == "ok"
        assert execution["max_tokens"] == 128
        assert execution["timeout_s"] == 5.0
        assert execution["endpoint_host"] == "https://api.example.com/v1"
        assert execution["response_sha256"] is not None
        assert execution["attempts"] == 1


# ── 计划 / dry-run / resume ──


class TestPlanning:
    def test_dry_run_produces_no_measurements(self, tmp_path, monkeypatch):
        """CLI dry-run 不得联网、不得写盘、不得产出任何结果。"""
        path = tmp_path / "m.json"
        manifest = _manifest_for(["api_500_none_attribute"])
        path.write_text(json.dumps(manifest), encoding="utf-8")
        before = path.read_text(encoding="utf-8")

        def _boom(*_args, **_kwargs):
            raise AssertionError("dry-run must not construct a provider")

        monkeypatch.setattr(lp, "LLMProvider", _boom)
        rc = runner.main(["run", str(path), "--dry-run"])
        assert rc == 0
        assert path.read_text(encoding="utf-8") == before
        assert all(v is None for v in manifest["records"][0]["metrics"].values())

    def test_dry_run_does_not_require_provider_config(self, tmp_path, monkeypatch):
        monkeypatch.delenv(lp.ENV_API_KEY, raising=False)
        monkeypatch.delenv(lp.ENV_BASE_URL, raising=False)
        monkeypatch.delenv(lp.ENV_MODEL, raising=False)
        path = tmp_path / "m.json"
        path.write_text(json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8")
        assert runner.main(["run", str(path), "--dry-run"]) == 0

    def test_skip_measured_by_default(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(200, _ok_body())[0]
        )
        assert lx.build_plan(manifest) == []

    def test_force_rerun_requeues(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(200, _ok_body())[0]
        )
        plan = lx.build_plan(manifest, skip_measured=False)
        assert len(plan) == 2

    def test_retry_failed_requeues_only_errors(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(503, "down")[0]
        )
        assert len(lx.build_plan(manifest, retry_failed=False)) == 0
        assert len(lx.build_plan(manifest, retry_failed=True)) == 2

    def test_group_filter(self):
        manifest = _manifest_for(["api_500_none_attribute"])
        assert len(lx.build_plan(manifest, groups=(exp.GROUP_WITH,))) == 1
        assert len(lx.build_plan(manifest, groups=(exp.GROUP_WITHOUT,))) == 1

    def test_interrupt_preserves_paired_structure(self, tmp_path):
        """中断（只执行一半）后 manifest 仍可校验、仍保持成对结构。"""
        path = tmp_path / "m.json"
        manifest = _manifest_for(["api_500_none_attribute", "db_error_null_column"])
        path.write_text(json.dumps(manifest), encoding="utf-8")
        loaded = lx.load_manifest(str(path))
        plan = lx.build_plan(loaded)[:2]  # 只跑第一对的一半
        provider = lp.LLMProvider(_config(), transport=_transport_returning(200, _ok_body())[0])
        lx.execute_plan(
            loaded,
            plan,
            provider=provider,
            provider_model="test-model",
            temperature=0.0,
            max_tokens=128,
            timeout_s=5.0,
            endpoint_host="https://api.example.com/v1",
        )
        lx.write_manifest_atomic(str(path), loaded)
        reloaded = lx.load_manifest(str(path))
        assert exp.validate_payload(reloaded) == []
        assert len(reloaded["records"]) == 4

    def test_atomic_write_leaves_no_temp_files(self, tmp_path):
        path = tmp_path / "m.json"
        lx.write_manifest_atomic(str(path), _manifest_for(["api_500_none_attribute"]))
        leftovers = [p.name for p in tmp_path.iterdir() if p.name.startswith(".benchmark-")]
        assert leftovers == []
        assert json.loads(path.read_text(encoding="utf-8"))["records"]


# ── CLI 行为 ──


class TestRunCLI:
    def test_run_without_provider_exits_nonzero(self, tmp_path, monkeypatch, capsys):
        for name in (lp.ENV_BASE_URL, lp.ENV_API_KEY, lp.ENV_MODEL):
            monkeypatch.delenv(name, raising=False)
        path = tmp_path / "m.json"
        path.write_text(json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8")
        rc = runner.main(["run", str(path)])
        assert rc == 1
        err = capsys.readouterr().err
        assert "provider 未配置" in err
        assert lp.ENV_API_KEY in err
        assert json.loads(path.read_text(encoding="utf-8"))["records"][0]["metrics"][
            "root_cause_accuracy"
        ] is None

    def test_run_unknown_arg_exit_nonzero(self, tmp_path):
        path = tmp_path / "m.json"
        path.write_text(json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8")
        assert runner.main(["run", str(path), "--nope"]) == 1

    def test_run_rejects_dirty_manifest(self, tmp_path, capsys):
        manifest = _manifest_for(["api_500_none_attribute"])
        manifest["records"][0]["input_hash"] = "a" * 64  # 非法 provenance
        path = tmp_path / "m.json"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        assert runner.main(["run", str(path), "--dry-run"]) == 1
        assert "canonical" in capsys.readouterr().err

    def test_run_executes_with_injected_transport(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "m.json"
        path.write_text(json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8")
        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-SECRET-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        monkeypatch.setattr(lp, "_urllib_transport", transport)
        rc = runner.main(["run", str(path)])
        assert rc == 0
        assert len(calls) == 2
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert exp.validate_payload(saved) == []
        assert "sk-test-SECRET" not in json.dumps(saved)

    def test_run_writes_raw_response_into_given_dir(self, tmp_path, monkeypatch):
        path = tmp_path / "m.json"
        raw_dir = tmp_path / "raw"
        path.write_text(json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-SECRET-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        monkeypatch.setattr(
            lp, "_urllib_transport", _transport_returning(200, _ok_body("answer-text"))[0]
        )
        assert runner.main(["run", str(path), "--raw-dir", str(raw_dir)]) == 0
        files = sorted(p.name for p in raw_dir.iterdir())
        assert len(files) == 2
        saved = json.loads(path.read_text(encoding="utf-8"))
        for record in saved["records"]:
            ref = record["execution"]["raw_response_ref"]
            assert str(tmp_path) not in ref
            assert ref in files

    def test_rerun_is_idempotent_and_force_reruns(self, tmp_path, monkeypatch, capsys):
        """默认重跑跳过已执行槽位；--force-rerun 才真正重跑。"""
        path = tmp_path / "m.json"
        path.write_text(json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setattr(lp, "_urllib_transport", transport)

        assert runner.main(["run", str(path)]) == 0
        first = len(calls)
        assert first == 2

        # 默认重跑：已执行槽位被跳过，不再发起调用
        assert runner.main(["run", str(path)]) == 0
        assert len(calls) == first
        assert "没有需要执行的记录" in capsys.readouterr().err

        # --force-rerun：显式重跑
        assert runner.main(["run", str(path), "--force-rerun"]) == 0
        assert len(calls) == first + 2


# ── 汇总与汇总横幅 ──


class TestSummarizeIntegration:
    def test_executed_manifest_summarizes_as_no_measurements(self):
        manifest = _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(200, _ok_body())[0]
        )
        summary = exp.summarize_records(manifest["records"])
        assert summary["result_status"] == "no_measurements"
        assert summary["record_count"] == 2
        assert summary["metrics"]["root_cause_accuracy"]["measured"] == 0
        assert summary["metrics"]["root_cause_accuracy"]["mean"] is None

    def test_summarize_banner_printed_on_cli(self, tmp_path, capsys):
        path = tmp_path / "m.json"
        path.write_text(json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8")
        assert runner.main(["summarize", str(path)]) == 0
        captured = capsys.readouterr()
        assert json.loads(captured.out)["result_status"] == "no_measurements"
        assert "不是实验结果" in captured.err

    def test_measured_records_still_report_measured(self):
        manifest = _manifest_for(["api_500_none_attribute"])
        manifest["records"][0]["metrics"]["root_cause_accuracy"] = 0.5
        manifest["records"][0]["created_at"] = "2026-09-17T00:00:00Z"
        summary = exp.summarize_records(manifest["records"])
        assert summary["result_status"] == "measured"


# ── 校验拒绝非法 M2-B2 记录 ──


class TestLiveRecordValidation:
    def _executed(self) -> dict:
        return _run_manifest(
            _manifest_for(["api_500_none_attribute"]), _transport_returning(200, _ok_body())[0]
        )

    def test_dry_run_with_metrics_rejected(self):
        manifest = self._executed()
        manifest["records"][0]["run_mode"] = exp.RUN_MODE_DRY
        manifest["records"][0]["metrics"]["root_cause_accuracy"] = 0.5
        manifest["records"][0]["created_at"] = "2026-09-17T00:00:00Z"
        errors = exp.validate_records(manifest["records"])
        assert any("dry_run" in e for e in errors)

    def test_without_record_with_context_hash_rejected(self):
        manifest = self._executed()
        for record in manifest["records"]:
            if record["group"] == exp.GROUP_WITHOUT:
                record["context_hash"] = exp.stable_hash({"fake": "context"})
                record["context_mode"] = exp.CONTEXT_MODE_FIXTURE
        errors = exp.validate_records(manifest["records"])
        assert any("must not carry a context_hash" in e for e in errors)
        assert any("context_mode='none'" in e for e in errors)

    def test_wrong_context_hash_rejected(self):
        manifest = self._executed()
        for record in manifest["records"]:
            if record["group"] == exp.GROUP_WITH:
                record["context_hash"] = "b" * 64
        errors = exp.validate_records(manifest["records"])
        assert any("does not match canonical lujo_context" in e for e in errors)

    def test_identical_prompt_hash_rejected(self):
        """两侧 prompt 相同说明没有注入 context，必须拒绝。"""
        manifest = self._executed()
        w = next(r for r in manifest["records"] if r["group"] == exp.GROUP_WITHOUT)
        wi = next(r for r in manifest["records"] if r["group"] == exp.GROUP_WITH)
        wi["prompt_hash"] = w["prompt_hash"]
        errors = exp.validate_records(manifest["records"])
        assert any("identical" in e for e in errors)

    def test_base_prompt_hash_mismatch_rejected(self):
        manifest = self._executed()
        wi = next(r for r in manifest["records"] if r["group"] == exp.GROUP_WITH)
        wi["base_prompt_hash"] = "c" * 64
        errors = exp.validate_records(manifest["records"])
        assert any("base_prompt_hash mismatch" in e for e in errors)

    def test_invalid_run_mode_rejected(self):
        manifest = self._executed()
        manifest["records"][0]["run_mode"] = "bogus"
        errors = exp.validate_records(manifest["records"])
        assert any("invalid run_mode" in e for e in errors)

    def test_legacy_records_without_new_fields_still_valid(self):
        """老记录（无 M2-B2 字段）必须继续通过校验（向后兼容）。"""
        manifest = _manifest_for(["api_500_none_attribute"])
        assert exp.validate_payload(manifest) == []


# ── 离线 / 隔离 ──


class TestIsolation:
    def test_benchmark_modules_do_not_import_app(self):
        """benchmark 全部模块（含新模块）都不得 import app/。"""
        code = (
            "import sys;"
            "import benchmark.experiment, benchmark.runner, benchmark.llm_provider,"
            " benchmark.llm_experiment, benchmark.prompting, benchmark.hashing;"
            "bad=[m for m in sys.modules if m.split('.')[0]=='app'];"
            "print(bad);"
            "raise SystemExit(1 if bad else 0)"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 0, proc.stdout + proc.stderr
        assert "[]" in proc.stdout

    def test_no_network_calls_without_injected_transport(self, monkeypatch):
        """未注入 transport 时，测试中也不得真的发起网络请求。

        生产 `_urllib_transport` 走 `_NO_REDIRECT_OPENER.open`（不是
        `urllib.request.urlopen`），故必须 patch 真实调用路径。
        """
        def _boom(*_args, **_kwargs):
            raise AssertionError("network call attempted in unit test")

        monkeypatch.setattr(lp._NO_REDIRECT_OPENER, "open", _boom)
        transport, calls = _transport_returning(200, _ok_body())
        _run_manifest(_manifest_for(["api_500_none_attribute"]), transport)
        assert len(calls) == 2

    def test_real_transport_is_blocked_by_isolation_guard(self, monkeypatch):
        """守卫有效性自证：直接调用真实 transport 必须被拦截。"""
        def _boom(*_args, **_kwargs):
            raise AssertionError("network call attempted in unit test")

        monkeypatch.setattr(lp._NO_REDIRECT_OPENER, "open", _boom)
        with pytest.raises(AssertionError):
            lp._urllib_transport("http://127.0.0.1:1/x", {}, b"{}", 1.0)


# ── 重定向安全：Authorization 不得跨 origin 转发 ──


@contextlib.contextmanager
def _loopback_redirect_server(redirect_code: int, *, same_origin: bool = False):
    """本地回环 server：/redirect 回 3xx 指向 /target，记录 /target 收到的请求。

    记录 **(method, path, Authorization)**，并同时实现 GET 与 POST——301/302/303
    会把 POST 降级为 GET，若 target 只实现 do_POST，跟随后的 GET 会得到 501 且
    不被记录，令 `received == []` 假阳性通过。两种方法都必须记录才能真实检测跟随。

    `same_origin=True` 时重定向到同一 host 的 /target（同 origin）；
    否则使用第二个回环端口（跨 origin）。均只绑定 127.0.0.1，不访问公网。
    """
    received: list[tuple[str, str, str | None]] = []

    class _Handler(http.server.BaseHTTPRequestHandler):
        def _record(self) -> None:
            received.append((self.command, self.path, self.headers.get("Authorization")))

        def _read_body(self) -> None:
            length = int(self.headers.get("Content-Length", 0) or 0)
            if length:
                self.rfile.read(length)

        def _handle(self) -> None:
            self._read_body()
            if self.path.startswith("/target"):
                self._record()
                body = _ok_body()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
                return
            self.send_response(redirect_code)
            self.send_header("Location", self.server.target_url)  # type: ignore[attr-defined]
            self.end_headers()

        do_GET = _handle  # noqa: N815 - BaseHTTPRequestHandler 接口
        do_POST = _handle  # noqa: N815
        do_PUT = _handle  # noqa: N815
        do_HEAD = _handle  # noqa: N815

        def log_message(self, *args):  # noqa: D401 - 静音
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_address[1]
    if same_origin:
        server.target_url = f"http://127.0.0.1:{port}/target"  # type: ignore[attr-defined]
    else:
        # 第二个回环端口 = 不同 origin
        other = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        other.target_url = f"http://127.0.0.1:{other.server_address[1]}/target"  # type: ignore[attr-defined]
        threading.Thread(target=other.serve_forever, daemon=True).start()
        server.target_url = other.target_url  # type: ignore[attr-defined]
        server._other = other  # type: ignore[attr-defined]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{port}/redirect", received
    finally:
        other = getattr(server, "_other", None)
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        if other is not None:
            other.shutdown()
            other.server_close()


class TestRedirectSafety:
    """默认 transport 必须拒绝重定向，绝不把 Authorization 转发到别处。"""

    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    @pytest.mark.parametrize("same_origin", [True, False])
    def test_authorization_never_sent_to_redirect_target(self, code, same_origin):
        """默认 transport 遇 3xx 必须拒绝跟随：目标从未收到任何请求。

        覆盖同 origin 与跨 origin；target 同时记录 GET/POST，故 301/302/303
        降级为 GET 时也能被真实捕获。
        """
        secret = "sk-LEAKME-abcdef123456"
        with _loopback_redirect_server(code, same_origin=same_origin) as (url, received):
            status, _body, _headers = lp._urllib_transport(
                url, {"Authorization": f"Bearer {secret}"}, b"{}", 5.0
            )
            assert status == code
            assert received == [], f"{code} 重定向被跟随，目标收到了 {received}"
            assert all(auth is None for _m, _p, auth in received)

    @pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
    def test_transport_classifies_redirect_as_failure(self, code):
        """默认 transport 遇到 3xx 必须落账为明确失败，且不跟随、不重试。"""
        secret = "sk-LEAKME-abcdef123456"
        called_urls: list[str] = []

        def _spy_transport(url, headers, body, timeout_s):
            called_urls.append(url)
            return lp._urllib_transport(url, headers, body, timeout_s)

        with _loopback_redirect_server(code) as (url, received):
            # max_retries>0：重定向也必须只调用一次（绝不重试）
            config = _config(base_url=url.rsplit("/", 1)[0], api_key=secret, max_retries=3)
            provider = lp.LLMProvider(config, transport=_spy_transport)
            result = provider.call([{"role": "user", "content": "hi"}])
            assert result.ok is False
            assert result.error_class == lp.ERROR_REDIRECT
            assert result.text is None
            assert result.attempts == 1
            assert received == []
        assert len(called_urls) == 1
        assert secret not in (result.error_message or "")

    def test_urllib_transport_refuses_redirect_without_following(self):
        """底层 transport 本身：3xx → 返回状态码而非跟随。"""
        with _loopback_redirect_server(302) as (url, received):
            status, body, headers = lp._urllib_transport(url, {}, b"{}", 5.0)
            assert status == 302
            assert received == []

    def test_followed_redirect_would_be_caught_by_helper(self):
        """守卫自证：用**跟随重定向**的 opener 打同一 helper，必须被记录到。

        证明 helper 不是空转（301/302/303 降级为 GET 也会被记录）。
        """
        for code in (301, 302, 303):
            with _loopback_redirect_server(code, same_origin=False) as (url, received):
                follower = urllib.request.build_opener()  # 默认跟随
                req = urllib.request.Request(
                    url, data=b"{}", headers={"Authorization": "Bearer sk-X"}, method="POST"
                )
                try:
                    follower.open(req, timeout=5).read()
                except Exception:
                    pass
                assert received, f"{code}: helper 未能记录到跟随后的请求（假阳性守卫失效）"
                assert any(auth == "Bearer sk-X" for _m, _p, auth in received)


# ── provider 元数据不得回显凭据 ──


class TestProviderMetadataRedaction:
    def test_request_id_redacted_before_persist(self):
        """攻击者可控的 x-request-id 不得把凭据带进 manifest。"""
        secret = "sk-live-REQID-abcdef123456"
        transport, _ = _transport_returning(
            200, _ok_body(), {"x-request-id": f"Bearer {secret}"}
        )
        manifest = _run_manifest(_manifest_for(["api_500_none_attribute"]), transport)
        assert secret not in json.dumps(manifest, ensure_ascii=False)

    def test_usage_and_finish_reason_do_not_persist_secrets(self):
        """恶意 provider 在 usage / finish_reason 里回显密钥时不得落盘。"""
        secret = "sk-live-USAGE-abcdef123456"
        body = json.dumps(
            {
                "choices": [
                    {"message": {"content": "ok"}, "finish_reason": f"Bearer {secret}"}
                ],
                "usage": {"prompt_tokens": 1, "note": f"api_key={secret}"},
            }
        )
        transport, _ = _transport_returning(200, body)
        manifest = _run_manifest(_manifest_for(["api_500_none_attribute"]), transport)
        assert secret not in json.dumps(manifest, ensure_ascii=False)


# ── context-mode none：只允许 without 组 ──


class TestContextModeNone:
    def test_context_mode_none_rejects_with_group_before_calls(
        self, tmp_path, monkeypatch, capsys
    ):
        """--context-mode none 与 with_lujo 冲突：必须在调用 provider 前失败。"""
        path = tmp_path / "m.json"
        path.write_text(
            json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8"
        )
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setattr(lp, "_urllib_transport", transport)
        before = path.read_text(encoding="utf-8")

        rc = runner.main(["run", str(path), "--context-mode", "none"])
        assert rc != 0
        assert calls == []  # 未发起任何网络调用
        assert path.read_text(encoding="utf-8") == before  # 未写出无效 Manifest
        assert "context" in capsys.readouterr().err.lower()

    def test_context_mode_none_without_group_produces_valid_manifest(
        self, tmp_path, monkeypatch
    ):
        """--context-mode none --group without 合法：产物可校验、无 context_hash。"""
        path = tmp_path / "m.json"
        path.write_text(
            json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8"
        )
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setattr(lp, "_urllib_transport", transport)

        rc = runner.main(
            ["run", str(path), "--context-mode", "none", "--group", "without"]
        )
        assert rc == 0
        assert len(calls) == 1  # 只跑 without 组
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert exp.validate_payload(saved) == []
        for record in saved["records"]:
            if record["group"] == exp.GROUP_WITHOUT:
                assert record["context_mode"] == exp.CONTEXT_MODE_NONE
                assert record["context_hash"] is None

    def test_execute_plan_rejects_none_with_with_group(self):
        """直接调用 execute_plan 时，none + with 也必须抛错而非写出无效记录。"""
        manifest = _manifest_for(["api_500_none_attribute"])
        plan = lx.build_plan(manifest, skip_measured=False, groups=(exp.GROUP_WITH,))
        provider = lp.LLMProvider(_config(), transport=_transport_returning(200, _ok_body())[0])
        with pytest.raises(ValueError):
            lx.execute_plan(
                manifest,
                plan,
                provider=provider,
                provider_model="test-model",
                temperature=0.0,
                max_tokens=128,
                timeout_s=5.0,
                endpoint_host="https://api.example.com/v1",
                context_mode=exp.CONTEXT_MODE_NONE,
            )


# ── manifest 元数据一致性 ──


class TestManifestMetadataConsistency:
    def test_new_manifest_syncs_top_level_from_provider(self):
        """init 默认 model=unspecified，run 后顶层必须与 records 一致且可校验。"""
        manifest = _manifest_for(["api_500_none_attribute"])
        manifest["model"] = "unspecified"
        for record in manifest["records"]:
            record["model"] = "unspecified"
        updated = _run_manifest(manifest, _transport_returning(200, _ok_body())[0])
        assert exp.validate_payload(updated) == []
        assert updated["model"] == "test-model"
        assert all(r["model"] == "test-model" for r in updated["records"])

    def test_resume_model_drift_rejected_before_calls(self, tmp_path, monkeypatch, capsys):
        """已有执行的 manifest 换 model resume 必须在调用前失败。"""
        path = tmp_path / "m.json"
        manifest = _manifest_for(["api_500_none_attribute"])
        path.write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setattr(lp, "_urllib_transport", transport)
        assert runner.main(["run", str(path)]) == 0
        first_calls = len(calls)

        # 换模型 resume（--force-rerun）必须拒绝
        monkeypatch.setenv(lp.ENV_MODEL, "different-model")
        rc = runner.main(["run", str(path), "--force-rerun"])
        assert rc != 0
        assert len(calls) == first_calls  # 未发起任何新调用
        assert "model" in capsys.readouterr().err.lower()

    def test_repo_sha_override_backfills_all_records_and_top(self):
        """--repo-sha 回填必须同时覆盖顶层与全部 records。"""
        manifest = _manifest_for(["api_500_none_attribute"])
        new_sha = "a" * 40
        updated = _run_manifest(
            manifest, _transport_returning(200, _ok_body())[0], repo_sha=new_sha
        )
        assert updated["repo_sha"] == new_sha
        assert all(r["repo_sha"] == new_sha for r in updated["records"])
        assert exp.validate_payload(updated) == []

    def test_cli_repo_sha_backfills_empty_template_before_validate(
        self, tmp_path, monkeypatch
    ):
        """init 未带 --repo-sha（空 repo_sha）时，run --repo-sha 必须先回填再校验。"""
        path = tmp_path / "m.json"
        assert runner.main(["init", "--output", str(path), "--force"]) == 0
        assert json.loads(path.read_text(encoding="utf-8"))["repo_sha"] == ""
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setattr(lp, "_urllib_transport", transport)
        rc = runner.main(
            ["run", str(path), "--case-id", "api_500_none_attribute", "--repo-sha", "b" * 40]
        )
        assert rc == 0
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert saved["repo_sha"] == "b" * 40
        assert exp.validate_payload(saved) == []

    def test_resume_repo_sha_drift_rejected(self, tmp_path, monkeypatch, capsys):
        """已有执行结果后 --repo-sha 与既有值不同：必须拒绝（不得改写历史）。"""
        path = tmp_path / "m.json"
        path.write_text(json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        monkeypatch.setattr(lp, "_urllib_transport", _transport_returning(200, _ok_body())[0])
        assert runner.main(["run", str(path)]) == 0

        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setattr(lp, "_urllib_transport", transport)
        rc = runner.main(["run", str(path), "--force-rerun", "--repo-sha", "c" * 40])
        assert rc != 0
        assert calls == []

    def test_one_side_then_resume_keeps_control_vars_consistent(
        self, tmp_path, monkeypatch
    ):
        """只跑 without 再 resume with：两侧控制变量保持一致，全程可校验。"""
        path = tmp_path / "m.json"
        path.write_text(json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        monkeypatch.setattr(lp, "_urllib_transport", _transport_returning(200, _ok_body())[0])
        assert (
            runner.main(
                ["run", str(path), "--case-id", "api_500_none_attribute", "--group", "without"]
            )
            == 0
        )
        assert exp.validate_payload(json.loads(path.read_text(encoding="utf-8"))) == []

        monkeypatch.setattr(lp, "_urllib_transport", _transport_returning(200, _ok_body())[0])
        rc = runner.main(
            ["run", str(path), "--case-id", "api_500_none_attribute", "--group", "with"]
        )
        assert rc == 0
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert exp.validate_payload(saved) == []
        by_group = {r["group"]: r for r in saved["records"]}
        assert by_group[exp.GROUP_WITHOUT]["model"] == by_group[exp.GROUP_WITH]["model"]
        assert by_group[exp.GROUP_WITHOUT]["repo_sha"] == by_group[exp.GROUP_WITH]["repo_sha"]


# ── 本次运行统计不得混入历史结果 ──


class TestRunStatsScopedToPlan:
    def test_historical_error_does_not_fail_scoped_run(self, tmp_path, monkeypatch, capsys):
        """历史失败记录不在本次 plan 内时，本次成功运行必须退出 0。"""
        path = tmp_path / "m.json"
        manifest = _manifest_for(["api_500_none_attribute", "db_error_null_column"])
        path.write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")

        # 第一次：api case 报 500 → 留下历史失败
        monkeypatch.setattr(lp, "_urllib_transport", _transport_returning(500, "boom")[0])
        runner.main(["run", str(path), "--case-id", "api_500_none_attribute"])
        saved = json.loads(path.read_text(encoding="utf-8"))
        assert any(
            (r.get("execution") or {}).get("error_class") for r in saved["records"]
        )

        # 第二次：只跑 db case，应成功且退出 0（历史失败不影响）
        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setattr(lp, "_urllib_transport", transport)
        rc = runner.main(["run", str(path), "--case-id", "db_error_null_column"])
        assert rc == 0
        assert len(calls) == 2

    def test_current_run_error_still_exits_nonzero(self, tmp_path, monkeypatch):
        """本次 plan 内发生失败仍必须返回非零。"""
        path = tmp_path / "m.json"
        path.write_text(
            json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8"
        )
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        monkeypatch.setattr(lp, "_urllib_transport", _transport_returning(500, "boom")[0])
        assert runner.main(["run", str(path)]) == 1


# ── experiment-id 过滤 ──


class TestExperimentIdFilter:
    def test_build_plan_filters_by_experiment_id(self):
        manifest = _manifest_for(["api_500_none_attribute"])
        manifest["experiment_id"] = "exp-A"
        for record in manifest["records"]:
            record["experiment_id"] = "exp-A"
        assert len(lx.build_plan(manifest, skip_measured=False, experiment_id="exp-A")) == 2
        assert lx.build_plan(manifest, skip_measured=False, experiment_id="exp-B") == []

    def test_unknown_experiment_id_does_not_call_provider(self, tmp_path, monkeypatch, capsys):
        path = tmp_path / "m.json"
        path.write_text(
            json.dumps(_manifest_for(["api_500_none_attribute"])), encoding="utf-8"
        )
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setattr(lp, "_urllib_transport", transport)
        rc = runner.main(["run", str(path), "--experiment-id", "does-not-exist"])
        assert rc == 0
        assert calls == []
        assert "没有" in capsys.readouterr().err


# ── M2-B2.3：metadata 字典 key 也必须脱敏 ──


def _body_with_usage(usage: object, finish_reason: str = "stop") -> str:
    return json.dumps(
        {
            "choices": [{"message": {"content": "ok"}, "finish_reason": finish_reason}],
            "usage": usage,
        }
    )


class TestMetadataKeyRedaction:
    """远端可控 metadata 的 key 与 value 都必须脱敏，且键不得静默丢失。"""

    def test_secret_as_usage_key_is_redacted(self):
        """secret 作为 usage 的 **key** 时不得落盘（原缺陷：只脱敏 value）。"""
        secret = "sk-KEY-LEAK-abcdef123456"
        transport, _ = _transport_returning(200, _body_with_usage({secret: 1}))
        manifest = _run_manifest(_manifest_for(["api_500_none_attribute"]), transport)
        blob = json.dumps(manifest, ensure_ascii=False)
        assert secret not in blob

    def test_secret_in_nested_key_and_value_both_redacted(self):
        """顶层 key、多层嵌套 key、value 同时含 secret 时全部脱敏。"""
        secret = "sk-NESTED-abcdef123456"
        usage = {
            secret: 1,
            "outer": {secret: {"api_key=" + secret: 2}},
            "list": [{"token=" + secret: 3}, secret],
        }
        transport, _ = _transport_returning(200, _body_with_usage(usage))
        manifest = _run_manifest(_manifest_for(["api_500_none_attribute"]), transport)
        blob = json.dumps(manifest, ensure_ascii=False)
        assert secret not in blob

    def test_multiple_sensitive_keys_do_not_collapse(self):
        """多个敏感 key 脱敏后不得因同名而静默互相覆盖。"""
        out = lp.redact_metadata({"sk-aaaa111111": 1, "sk-bbbb222222": 2})
        assert len(out) == 2, f"key 静默丢失：{out}"
        assert 1 in out.values() and 2 in out.values()

    def test_non_sensitive_keys_are_preserved(self):
        """非敏感 key 尽量保持原值（不改变既有 metadata 结构）。"""
        out = lp.redact_metadata({"prompt_tokens": 5, "completion_tokens": 7})
        assert out == {"prompt_tokens": 5, "completion_tokens": 7}

    def test_throwing_str_on_key_does_not_escape(self):
        """key 的 __str__ 抛异常时不得向外抛携密异常。"""
        class Evil:
            def __str__(self):
                raise RuntimeError("boom sk-EVILKEY-abcdef123456")

            __repr__ = __str__

        out = lp.redact_metadata({Evil(): 1})  # 不得抛
        blob = json.dumps(out, ensure_ascii=False)
        assert "sk-EVILKEY-abcdef123456" not in blob

    def test_throwing_str_on_value_does_not_escape(self):
        """value 字符串化抛异常时不得向外抛携密异常。"""
        class Evil:
            def __str__(self):
                raise RuntimeError("boom sk-EVILVAL-abcdef123456")

            __repr__ = __str__

        out = lp.redact_metadata({"k": Evil()})
        blob = json.dumps(out, ensure_ascii=False)
        assert "sk-EVILVAL-abcdef123456" not in blob

    def test_provider_call_never_raises_on_hostile_metadata(self):
        """恶意 metadata 不得让 provider.call 抛异常（契约：失败也返回 LLMResult）。

        注入型 transport 可返回字符串化会抛异常的头值——provider 必须降级为
        安全占位符而非向外抛携密异常。
        """
        class Evil:
            def __str__(self):
                raise RuntimeError("boom sk-RAISE-abcdef123456")

            __repr__ = __str__

        transport, _ = _transport_returning(200, _ok_body(), {"x-request-id": Evil()})
        provider = lp.LLMProvider(_config(), transport=transport)
        result = provider.call([{"role": "user", "content": "hi"}])
        assert isinstance(result, lp.LLMResult)
        assert "sk-RAISE-abcdef123456" not in json.dumps(
            {"r": result.request_id}, ensure_ascii=False, default=str
        )

    def test_tuple_and_scalar_types_survive(self):
        """tuple / None / 数字 / bool 经脱敏后仍可 JSON 序列化且不抛。"""
        out = lp.redact_metadata({"t": (1, 2), "n": None, "i": 3, "f": 1.5, "b": True})
        json.dumps(out, ensure_ascii=False)  # 不抛即通过
        assert out["n"] is None and out["i"] == 3 and out["b"] is True

    def test_full_manifest_on_disk_has_no_secret(self, tmp_path, monkeypatch):
        """写盘后的完整 Manifest 文本不得含原始 secret（含 key 路径）。"""
        secret = "sk-DISK-LEAK-abcdef123456"
        path = tmp_path / "m.json"
        manifest = _manifest_for(["api_500_none_attribute"])
        manifest["model"] = "test-model"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, secret)
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        monkeypatch.setattr(
            lp,
            "_urllib_transport",
            _transport_returning(200, _body_with_usage({secret: 1}))[0],
        )
        assert runner.main(["run", str(path)]) == 0
        assert secret not in path.read_text(encoding="utf-8")


# ── M2-B2.3：非有限 temperature 必须在任何调用/写盘前拒绝 ──


NON_FINITE_TEMPERATURES = ("nan", "+nan", "-nan", "inf", "+inf", "-inf", "infinity", "1e999")


class TestNonFiniteTemperature:
    @pytest.mark.parametrize("raw", NON_FINITE_TEMPERATURES)
    def test_env_temperature_rejected(self, tmp_path, monkeypatch, capsys, raw):
        """BENCHMARK_LLM_TEMPERATURE 非有限值必须非零退出、不调用、不写盘。"""
        path = tmp_path / "m.json"
        manifest = _manifest_for(["api_500_none_attribute"])
        manifest["model"] = "test-model"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        before = path.read_text(encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        monkeypatch.setenv(lp.ENV_TEMPERATURE, raw)
        transport, calls = _transport_returning(200, _ok_body())
        monkeypatch.setattr(lp, "_urllib_transport", transport)

        with pytest.raises(lp.LLMNotConfiguredError):
            lp.load_config_from_env(env=os.environ)

        rc = runner.main(["run", str(path)])
        assert rc != 0
        assert calls == []
        assert path.read_text(encoding="utf-8") == before

    @pytest.mark.parametrize("raw", NON_FINITE_TEMPERATURES)
    def test_init_temperature_rejected(self, tmp_path, raw):
        """init --temperature 非有限值必须非零退出且不写文件。"""
        path = tmp_path / "plan.json"
        rc = runner.main(["init", "--output", str(path), "--repo-sha", "0" * 40, "--temperature", raw])
        assert rc != 0
        assert not path.exists()

    def test_finite_temperature_still_works(self, tmp_path, monkeypatch):
        """有限值（含 0.0）必须继续可用并产出可校验 Manifest。"""
        path = tmp_path / "m.json"
        manifest = _manifest_for(["api_500_none_attribute"])
        manifest["model"] = "test-model"
        path.write_text(json.dumps(manifest), encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        monkeypatch.setenv(lp.ENV_TEMPERATURE, "0.0")
        monkeypatch.setattr(lp, "_urllib_transport", _transport_returning(200, _ok_body())[0])
        assert runner.main(["run", str(path)]) == 0
        assert exp.validate_payload(json.loads(path.read_text(encoding="utf-8"))) == []

    def test_non_finite_does_not_overwrite_existing_manifest(self, tmp_path, monkeypatch):
        """已有 Manifest 在非有限 temperature 下必须逐字不变。"""
        path = tmp_path / "m.json"
        path.write_text("KEEP-ME-EXACTLY", encoding="utf-8")
        monkeypatch.setenv(lp.ENV_BASE_URL, "https://api.example.com/v1")
        monkeypatch.setenv(lp.ENV_API_KEY, "sk-test-key-abcdef123456")
        monkeypatch.setenv(lp.ENV_MODEL, "test-model")
        monkeypatch.setenv(lp.ENV_TEMPERATURE, "inf")
        assert runner.main(["run", str(path)]) != 0
        assert path.read_text(encoding="utf-8") == "KEEP-ME-EXACTLY"
