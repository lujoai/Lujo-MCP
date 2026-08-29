"""v0.7.0 Minor：extract_json 两处重复实现合一的守护测试。

历史缺口（第 6 轮 Minor）：app/llm/output_schema 与 app/agent/utils 各自维护
一份逐字符等价的 extract_json，修一处漏一处。现正本收敛到 app/utils/json_extract
（中性模块，双向 import 无循环导入风险），两个旧入口转发同一函数对象。
"""


def test_two_entry_points_share_one_implementation():
    from app.agent.utils import extract_json
    from app.llm.output_schema import _extract_json
    from app.utils.json_extract import extract_json as canonical

    assert extract_json is canonical
    assert _extract_json is canonical


def test_behavior_contract_markdown_block():
    """markdown code block 提取（与合并前双方实现逐字符等价的既有契约）。"""
    from app.utils.json_extract import extract_json

    content = "```json\n{\"a\": 1}\n```"
    assert extract_json(content) == '{"a": 1}'
    assert extract_json("```\n[1, 2]\n```") == "[1, 2]"


def test_behavior_contract_nested_text_and_none():
    from app.utils.json_extract import extract_json

    assert extract_json('前置说明 {"key": "value"} 后缀') == '{"key": "value"}'
    assert extract_json("nothing here") is None
    assert extract_json("") is None
