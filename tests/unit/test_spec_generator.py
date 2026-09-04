"""
测试 spec_generator 与 verify 自动推导规范端到端行为。
"""
from app.mcp.tools.verify_api import verify_handler
from app.runtime.verifier.spec_generator import (
    infer_spec_from_sample,
    parse_openapi_to_specs,
)


def test_infer_spec_from_sample_basic():
    """测试从实际成功响应样本中推导规范。"""
    sample = {
        "status_code": 200,
        "body": {
            "code": 0,
            "data": {
                "user_id": 12345,
                "role": "admin",
            }
        }
    }
    spec = infer_spec_from_sample(target="/api/user/info", sample=sample)
    assert spec["kind"] == "api"
    assert spec["target"] == "/api/user/info"
    assert spec["expect"]["status"] == 200
    assert spec["expect"]["body_rules"]["code"] == 0
    assert spec["expect"]["body_rules"]["data.user_id"] == 12345
    assert spec["expect"]["body_rules"]["data.role"] == "admin"


def test_infer_spec_invalid_sample():
    """测试边界样本输入。"""
    res = infer_spec_from_sample("/api", None)
    assert res["expect"] == {}


def test_parse_openapi_to_specs():
    """测试解析标准 OpenAPI 数据。"""
    openapi = {
        "paths": {
            "/api/orders": {
                "get": {
                    "summary": "获取订单列表",
                    "responses": {
                        "200": {"description": "成功"},
                    }
                },
                "post": {
                    "summary": "创建订单",
                    "responses": {
                        "201": {"description": "已创建"},
                    }
                }
            }
        }
    }
    specs = parse_openapi_to_specs(openapi)
    assert len(specs) == 2
    
    get_spec = next(s for s in specs if "GET" in s["target"])
    assert get_spec["expect"]["status"] == 200
    assert get_spec["description"] == "获取订单列表"
    
    post_spec = next(s for s in specs if "POST" in s["target"])
    assert post_spec["expect"]["status"] == 201


def test_verify_with_sample_detects_silent_failure():
    """测试使用 sample 自动推导规范并成功捕获静默失败。"""
    # 正常样本：状态码 200，code 0
    sample = {
        "status_code": 200,
        "body": {"code": 0, "msg": "ok"}
    }
    # 实际结果：状态码 200（无报错），但业务 code 为 -1（静默失败）
    actual = {
        "status_code": 200,
        "body": {"code": -1, "msg": "insufficient_balance"}
    }

    result = verify_handler({
        "actual": actual,
        "sample": sample,
        "target": "/api/pay",
    })

    assert result["matched"] is False
    assert result["silent_failure"] is True
    assert len(result["diffs"]) > 0
    # 断言差异被明确标出
    assert any(d["field"] == "body.code" for d in result["diffs"])
