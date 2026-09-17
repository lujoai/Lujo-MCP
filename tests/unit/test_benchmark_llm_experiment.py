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

import json
import subprocess
import sys
from pathlib import Path

import pytest

from benchmark import llm_experiment as lx
from benchmark import prompting as pr
from benchmark import runner
from benchmark import experiment as exp
from benchmark import llm_provider as lp
from benchmark.cases import BENCHMARK_CASES, get_case

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
        """未注入 transport 时，测试中也不得真的发起网络请求。"""
        import urllib.request

        def _boom(*_args, **_kwargs):
            raise AssertionError("network call attempted in unit test")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        transport, calls = _transport_returning(200, _ok_body())
        _run_manifest(_manifest_for(["api_500_none_attribute"]), transport)
        assert len(calls) == 2
