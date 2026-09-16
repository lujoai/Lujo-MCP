"""M2-B1 可复现离线 Benchmark 对照框架单元测试。

原行为：`benchmark/` 只有静态 fixture（`BenchmarkCase`）+ `list`/`show`/`quality`
三个命令，没有成对实验清单、记录校验、离线汇总能力；`EvaluationMetrics` 的 4 个
字段默认 `0.0`，无法区分「未测量」与「真实测得 0」。

新行为：新增 `benchmark.experiment` 模块，提供：
- 稳定哈希（复用结构化 JSON + 完整 sha256，避免键顺序抖动）
- `build_manifest` 生成 without_lujo / with_lujo 成对实验清单模板
- `validate_records` 校验外部填写/导入的记录（配对完整、无重复、控制变量一致、
  指标范围、时间非负）
- `summarize_records` 离线汇总（measured/missing/coverage，None 与 0 区分，无样本
  返回 null 而非虚假均值）

锁定不变量（以下断言破坏即红）：
- 未测量必须用 None 表达，真实 0 必须保留为 0；二者在汇总中分别计入 missing / measured。
- 比率类指标闭区间 [0,1]；时间类指标非负。
- 配对键 = (experiment_id, case_id, run_index)，每组恰好 1 条 without + 1 条 with。
- 同一配对内 model / temperature / repo_sha / input_hash 必须一致（tool_policy 是
  实验变量允许不同）。
- 无样本时统计值为 null，绝不产生虚假均值；空记录集也安全返回。
- hash 只存摘要，不回显明文用户描述 / lujo_context / secrets / 绝对路径。
- 旧 list/show/quality 命令行为与返回值保持不变。
"""

from __future__ import annotations

import json

import pytest

from benchmark import runner
from benchmark import experiment as exp
from benchmark.cases import BENCHMARK_CASES


def _record(group="without_lujo", run_index=1, case_id="api_500_none_attribute", **overrides):
    """构造一条合法实验记录，返回 dict。"""
    base = {
        "schema_version": exp.SCHEMA_VERSION,
        "experiment_id": "exp-1",
        "case_id": case_id,
        "group": group,
        "run_index": run_index,
        "model": "gpt-4o",
        "temperature": 0.0,
        "tool_policy": "no_lujo_tools" if group == "without_lujo" else "lujo_mcp_tools",
        "repo_sha": "0" * 40,
        "input_hash": "a" * 64,
        "created_at": "2026-09-16T00:00:00Z",
        "metrics": dict.fromkeys(exp.METRIC_NAMES),
        "notes": "",
    }
    base.update(overrides)
    return base


def _pair(run_index=1, case_id="api_500_none_attribute"):
    """返回 (without, with) 两条控制变量一致的配对记录。"""
    return (
        _record("without_lujo", run_index, case_id),
        _record("with_lujo", run_index, case_id),
    )


# ── 稳定哈希 ──


class TestHash:
    def test_stable_hash_deterministic(self):
        """原：无哈希能力；新：同一对象两次哈希相等且键顺序无关。"""
        a = {"b": 1, "a": [1, 2, 3]}
        b = {"a": [1, 2, 3], "b": 1}
        assert exp.stable_hash(a) == exp.stable_hash(b)

    def test_stable_hash_sensitive_to_content(self):
        """不同输入必须产生不同哈希（杜绝把不同输入当同一复现）。"""
        assert exp.stable_hash({"x": 1}) != exp.stable_hash({"x": 2})

    def test_input_hash_full_sha256(self):
        """input_hash 采用完整 sha256 hexdigest（64 位，不截断）。"""
        h = exp.compute_input_hash("case-1", "用户描述")
        assert len(h) == 64
        assert exp.compute_input_hash("case-1", "用户描述") == h


# ── 成对清单生成 ──


class TestManifest:
    def test_build_manifest_generates_pairs(self):
        """6 个 case × 1 run × {without, with} 应生成 12 条记录。"""
        manifest = exp.build_manifest(
            BENCHMARK_CASES, run_count=1, model="gpt-4o", repo_sha="0" * 40
        )
        records = manifest["records"]
        assert len(records) == len(BENCHMARK_CASES) * 2
        groups = [r["group"] for r in records]
        assert groups == ["without_lujo", "with_lujo"] * len(BENCHMARK_CASES)

    def test_build_manifest_multiple_runs(self):
        """run_count=3 时每个 case 应生成 3 组 run_index 1..3。"""
        manifest = exp.build_manifest(BENCHMARK_CASES, run_count=3, repo_sha="0" * 40)
        for case in BENCHMARK_CASES:
            runs = sorted(
                r["run_index"]
                for r in manifest["records"]
                if r["case_id"] == case.case_id
            )
            assert runs == [1, 1, 2, 2, 3, 3]

    def test_manifest_records_have_none_metrics(self):
        """清单模板的 metrics 必须全 None（未测量），不得用 0.0 伪装。"""
        manifest = exp.build_manifest(BENCHMARK_CASES, repo_sha="0" * 40)
        for r in manifest["records"]:
            assert all(v is None for v in r["metrics"].values())

    def test_manifest_pair_control_vars_consistent(self):
        """清单中同一配对的两侧 input_hash 必须一致（同一基础现场）。"""
        manifest = exp.build_manifest(BENCHMARK_CASES, repo_sha="0" * 40)
        by_pair: dict[tuple, dict] = {}
        for r in manifest["records"]:
            key = (r["experiment_id"], r["case_id"], r["run_index"])
            by_pair.setdefault(key, {})[r["group"]] = r
        for pair in by_pair.values():
            assert pair["without_lujo"]["input_hash"] == pair["with_lujo"]["input_hash"]
            assert pair["without_lujo"]["model"] == pair["with_lujo"]["model"]

    def test_manifest_template_is_valid_after_fill(self):
        """填好 repo_sha 后，清单模板应通过结构校验（结果仍为未测量）。"""
        manifest = exp.build_manifest(BENCHMARK_CASES, repo_sha="0" * 40)
        assert exp.validate_records(manifest["records"]) == []

    def test_coerce_records_dict_branch(self):
        """coerce_records 应同时接受数组与 ``{"records": [...]}`` 两种形态。"""
        manifest = exp.build_manifest(BENCHMARK_CASES, repo_sha="0" * 40)
        assert exp.coerce_records(manifest) == manifest["records"]
        assert exp.coerce_records(manifest["records"]) == manifest["records"]
        with pytest.raises(ValueError):
            exp.coerce_records({"nope": 1})


# ── 校验：配对完整 / 重复 / 控制变量 ──


class TestValidate:
    def test_valid_pair_passes(self):
        assert exp.validate_records(list(_pair())) == []

    def test_missing_with_rejected(self):
        """原：无配对概念；新：只有 without 缺 with 必须明确报错。"""
        errors = exp.validate_records([_record("without_lujo")])
        assert any("with_lujo" in e or "pair" in e.lower() for e in errors)

    def test_missing_without_rejected(self):
        errors = exp.validate_records([_record("with_lujo")])
        assert any("without_lujo" in e or "pair" in e.lower() for e in errors)

    def test_duplicate_record_rejected(self):
        """同一配对键 + 同一 group 出现两次必须拒绝。"""
        w, _ = _pair()
        errors = exp.validate_records([w, w, _record("with_lujo")])
        assert any("duplicate" in e.lower() for e in errors)

    def test_control_var_mismatch_rejected(self):
        """同一配对的 model 不一致必须拒绝（不能当对照）。"""
        w, _ = _pair()
        w["model"] = "gpt-3.5"
        errors = exp.validate_records([w, _record("with_lujo")])
        assert any("mismatch" in e.lower() or "inconsistent" in e.lower() for e in errors)

    def test_input_hash_mismatch_rejected(self):
        """input_hash 不一致说明两侧不是同一基础现场，必须拒绝。"""
        w, _ = _pair()
        w["input_hash"] = "b" * 64
        errors = exp.validate_records([w, _record("with_lujo")])
        assert any("mismatch" in e.lower() or "inconsistent" in e.lower() for e in errors)

    def test_unknown_case_id_rejected(self):
        w = _record(case_id="no_such_case")
        errors = exp.validate_records([w, _record("with_lujo", case_id="no_such_case")])
        assert any("case" in e.lower() for e in errors)

    def test_unknown_group_rejected(self):
        w = _record(group="bogus_group")
        errors = exp.validate_records([w])
        assert any("group" in e.lower() for e in errors)

    def test_unsupported_schema_version_rejected(self):
        w = _record(schema_version="0.9")
        errors = exp.validate_records([w])
        assert any("schema" in e.lower() for e in errors)

    def test_tool_policy_may_differ(self):
        """tool_policy 是实验变量，允许两侧不同（不应触发不一致错误）。"""
        w, wi = _pair()
        w["tool_policy"] = "no_lujo_tools"
        wi["tool_policy"] = "lujo_mcp_tools"
        assert exp.validate_records([w, wi]) == []


# ── 指标范围与负时间校验 ──


class TestMetricValidation:
    def test_ratio_out_of_range_rejected(self):
        """比率类指标超出 [0,1] 必须拒绝（-0.1 与 1.1 均非法）。"""
        w, wi = _pair()
        w["metrics"]["root_cause_accuracy"] = -0.1
        assert exp.validate_records([w, wi])
        w["metrics"]["root_cause_accuracy"] = 1.1
        assert exp.validate_records([w, wi])

    def test_ratio_bounds_accepted(self):
        """比率类指标 0.0 与 1.0 是合法值。"""
        w, wi = _pair()
        w["metrics"]["debug_success_rate"] = 0.0
        w["metrics"]["verification_success_rate"] = 1.0
        assert exp.validate_records([w, wi]) == []

    def test_negative_duration_rejected(self):
        """时间指标为负必须拒绝。"""
        w, wi = _pair()
        w["metrics"]["time_to_diagnosis"] = -1.0
        assert exp.validate_records([w, wi])

    def test_zero_duration_accepted(self):
        """时间指标 0 是合法真实值（立即命中），不得当缺失。"""
        w, wi = _pair()
        w["metrics"]["time_to_diagnosis"] = 0.0
        assert exp.validate_records([w, wi]) == []

    def test_nan_metric_rejected(self):
        """NaN 不属于合法测量值。"""
        w, wi = _pair()
        w["metrics"]["root_cause_accuracy"] = float("nan")
        assert exp.validate_records([w, wi])

    def test_unknown_metric_key_rejected(self):
        """metrics 中出现框架未定义的指标键应拒绝（防止拼写漂移）。"""
        w, wi = _pair()
        w["metrics"]["root_cause_accuraccy"] = 0.5
        assert exp.validate_records([w, wi])

    def test_bool_temperature_rejected(self):
        """temperature 为 bool 不算实数，应拒绝（bool 是 int 子类，需显式排除）。"""
        w = _record(temperature=True)
        assert exp.validate_records([w])

    def test_inf_metric_rejected(self):
        """inf 虽满足 >= 0 但不属有限实数，应拒绝。"""
        w, wi = _pair()
        w["metrics"]["time_to_diagnosis"] = float("inf")
        assert exp.validate_records([w, wi])


# ── None 与 0 的区分 ──


class TestNoneVsZero:
    def test_none_is_missing_zero_is_measured(self):
        """原：EvaluationMetrics 默认 0.0 无法区分；新：None 计 missing、0.0 计 measured。"""
        w, wi = _pair()
        w["metrics"]["root_cause_accuracy"] = None
        wi["metrics"]["root_cause_accuracy"] = 0.0
        summary = exp.summarize_records([w, wi])
        stat = summary["metrics"]["root_cause_accuracy"]
        assert stat["measured"] == 1
        assert stat["missing"] == 1
        assert stat["mean"] == 0.0  # 真实 0，不是缺失填充


# ── 汇总 measured/missing/coverage ──


class TestSummarize:
    def test_coverage_and_stats(self):
        """4 条记录（2 完整配对），root_cause_accuracy 值 [None, 0.0, 0.6, 0.8]。"""
        w1, wi1 = _pair(run_index=1, case_id="api_500_none_attribute")
        w2, wi2 = _pair(run_index=1, case_id="frontend_blank_fetch_error")
        w1["metrics"]["root_cause_accuracy"] = None
        wi1["metrics"]["root_cause_accuracy"] = 0.0
        w2["metrics"]["root_cause_accuracy"] = 0.6
        wi2["metrics"]["root_cause_accuracy"] = 0.8
        w1["metrics"]["time_to_diagnosis"] = 100.0
        wi1["metrics"]["time_to_diagnosis"] = 50.0
        wi2["metrics"]["time_to_diagnosis"] = 30.0
        # w2 的 time_to_diagnosis 保持 None

        summary = exp.summarize_records([w1, wi1, w2, wi2])
        assert summary["record_count"] == 4
        assert summary["pair_count"] == 2
        assert summary["incomplete_pairs"] == 0

        rca = summary["metrics"]["root_cause_accuracy"]
        assert rca["measured"] == 3
        assert rca["missing"] == 1
        assert rca["coverage"] == pytest.approx(0.75)
        assert rca["mean"] == pytest.approx((0.0 + 0.6 + 0.8) / 3)
        assert rca["min"] == 0.0
        assert rca["max"] == 0.8

        ttd = summary["metrics"]["time_to_diagnosis"]
        assert ttd["measured"] == 3
        assert ttd["missing"] == 1
        assert ttd["mean"] == pytest.approx((100.0 + 50.0 + 30.0) / 3)
        assert ttd["min"] == 30.0
        assert ttd["max"] == 100.0

    def test_no_sample_produces_null_not_zero(self):
        """某指标全 None 时，统计值应为 null，不得输出虚假均值 0.0。"""
        w, wi = _pair()
        summary = exp.summarize_records([w, wi])
        stat = summary["metrics"]["repeated_bug_recall"]
        assert stat["measured"] == 0
        assert stat["coverage"] == 0.0
        assert stat["mean"] is None
        assert stat["min"] is None
        assert stat["max"] is None

    def test_empty_records_safe(self):
        """空记录集汇总不抛异常，所有指标统计值为 null。"""
        summary = exp.summarize_records([])
        assert summary["record_count"] == 0
        for stat in summary["metrics"].values():
            assert stat["measured"] == 0
            assert stat["mean"] is None

    def test_summarize_invalid_records_raises(self):
        """汇总前必须先校验：不完整配对应报错，不得产出虚假汇总。"""
        with pytest.raises(ValueError):
            exp.summarize_records([_record("without_lujo")])

    def test_by_group_breakdown(self):
        """汇总应按 group 拆分，便于对照 without vs with 增量。"""
        w, wi = _pair()
        w["metrics"]["root_cause_accuracy"] = 0.5
        wi["metrics"]["root_cause_accuracy"] = 0.9
        summary = exp.summarize_records([w, wi])
        by_group = summary["metrics"]["root_cause_accuracy"]["by_group"]
        assert by_group["without_lujo"]["mean"] == 0.5
        assert by_group["with_lujo"]["mean"] == 0.9


# ── 复现元数据与脱敏 ──


class TestReproducibilityAndPrivacy:
    def test_manifest_repo_sha_recorded(self):
        """repo_sha 等复现元数据必须写入清单（可复现）。"""
        manifest = exp.build_manifest(BENCHMARK_CASES, repo_sha="1" * 40)
        assert all(r["repo_sha"] == "1" * 40 for r in manifest["records"])

    def test_manifest_does_not_leak_case_content(self):
        """清单只存 input_hash，不回显 user_description 原文（脱敏）。"""
        manifest = exp.build_manifest(BENCHMARK_CASES, repo_sha="0" * 40)
        blob = json.dumps(manifest, ensure_ascii=False)
        for case in BENCHMARK_CASES:
            assert case.user_description not in blob

    def test_summarize_does_not_echo_notes(self):
        """汇总只做统计聚合，不回显 notes 中的敏感内容。"""
        w, wi = _pair()
        w["notes"] = "secret: API_KEY=super-secret-value"
        w["metrics"]["root_cause_accuracy"] = 0.5
        wi["metrics"]["root_cause_accuracy"] = 0.5
        blob = json.dumps(exp.summarize_records([w, wi]), ensure_ascii=False)
        assert "super-secret-value" not in blob
        assert "API_KEY" not in blob

    def test_manifest_no_absolute_paths(self):
        """清单不得包含用户绝对路径（复现只依赖相对 case_id + hash）。"""
        manifest = exp.build_manifest(BENCHMARK_CASES, repo_sha="0" * 40)
        blob = json.dumps(manifest, ensure_ascii=False)
        assert "C:\\" not in blob
        assert "C:/" not in blob


# ── CLI 集成与向后兼容 ──


class TestCLI:
    def test_validate_ok_exit_zero(self, tmp_path):
        w, wi = _pair()
        path = tmp_path / "records.json"
        path.write_text(json.dumps([w, wi]), encoding="utf-8")
        assert runner.main(["validate", str(path)]) == 0

    def test_validate_failure_exit_nonzero(self, tmp_path, capsys):
        """校验失败必须返回非零退出码且错误写 stderr。"""
        path = tmp_path / "bad.json"
        path.write_text(json.dumps([_record("without_lujo")]), encoding="utf-8")
        assert runner.main(["validate", str(path)]) == 1
        assert capsys.readouterr().err

    def test_summarize_ok_exit_zero(self, tmp_path, capsys):
        w, wi = _pair()
        w["metrics"]["root_cause_accuracy"] = 0.5
        wi["metrics"]["root_cause_accuracy"] = 0.9
        path = tmp_path / "records.json"
        path.write_text(json.dumps([w, wi]), encoding="utf-8")
        assert runner.main(["summarize", str(path)]) == 0
        out = capsys.readouterr().out
        assert json.loads(out)["metrics"]["root_cause_accuracy"]["mean"] == pytest.approx(0.7)

    def test_init_writes_file_no_overwrite(self, tmp_path):
        """已存在文件且未 --force 时不得覆盖，返回非零。"""
        path = tmp_path / "plan.json"
        path.write_text("precious", encoding="utf-8")
        rc = runner.main(["init", "--output", str(path), "--repo-sha", "0" * 40])
        assert rc == 1
        assert path.read_text(encoding="utf-8") == "precious"

    def test_init_writes_file_with_force(self, tmp_path):
        """--force 允许覆盖。"""
        path = tmp_path / "plan.json"
        path.write_text("precious", encoding="utf-8")
        rc = runner.main(
            ["init", "--output", str(path), "--force", "--repo-sha", "0" * 40]
        )
        assert rc == 0
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert "records" in payload

    def test_init_stdout_stable(self, capsys):
        """同一参数两次 init 的 stdout 输出必须完全相同（稳定可比较）。"""
        args = ["init", "--repo-sha", "0" * 40, "--model", "gpt-4o"]
        runner.main(args)
        first = capsys.readouterr().out
        runner.main(args)
        second = capsys.readouterr().out
        assert first == second

    def test_legacy_list_show_quality_preserved(self, capsys):
        """旧 list/show 命令行为与返回值保持不变。"""
        assert runner.main(["list"]) == 0
        assert "api_500_none_attribute" in capsys.readouterr().out
        assert runner.main(["show", "api_500_none_attribute"]) == 0
        assert runner.main(["show", "no-such"]) == 1
    def test_init_unknown_arg_exit_nonzero(self):
        assert runner.main(["init", "--bogus", "x"]) == 1

    def test_init_write_to_directory_fails_gracefully(self, tmp_path, capsys):
        """--output 指向目录/不可写时应走 stderr + 非零退出，而非抛 traceback。"""
        rc = runner.main(["init", "--output", str(tmp_path), "--repo-sha", "0" * 40])
        assert rc == 1
        assert capsys.readouterr().err