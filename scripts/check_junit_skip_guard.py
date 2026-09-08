#!/usr/bin/env python3
"""JUnit XML 全 skip 假绿守卫 —— CI 各测试 job 复用的统一实现。

背景（v0.7.6 起：自 ci.yml 内联 heredoc 抽出为脚本）：pytest 在 CI 环境缺依赖/
缺浏览器时可能整体 skip ——「0 通过、0 失败/错误、全是 skip」在退出码上是绿的，
属假绿。本脚本解析 --junitxml 产物：
  1. 打印 passed / skipped / failures / errors 计数（::notice，进 Actions 面板）；
  2. 全 skip 时打 ::error 并以非零退出码阻断流水线。

unit / integration / e2e 三个测试 job 复用同一实现，避免多份拷贝漂移。
用法：python scripts/check_junit_skip_guard.py <junit-results.xml>
"""
import sys
import xml.etree.ElementTree as ET


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {sys.argv[0]} <junit-results.xml>", file=sys.stderr)
        return 2

    try:
        root = ET.parse(sys.argv[1]).getroot()
    except (OSError, ET.ParseError) as exc:
        print(f"::error::{sys.argv[1]} unreadable ({exc}) —— 无法计算 skip 汇总")
        return 1

    suites = root.findall(".//testsuite") if root.tag == "testsuites" else [root]
    totals = {"tests": 0, "skipped": 0, "failures": 0, "errors": 0}
    for s in suites:
        for k in totals:
            totals[k] += int(s.get(k, 0))

    passed = totals["tests"] - totals["skipped"] - totals["failures"] - totals["errors"]
    print(
        f"::notice::passed={passed}, skipped={totals['skipped']}, "
        f"failures={totals['failures']}, errors={totals['errors']}"
    )

    # 全 skip 假绿：没有任何通过、没有任何失败/错误、却存在被跳过的用例
    if passed == 0 and totals["failures"] == 0 and totals["errors"] == 0 and totals["skipped"] > 0:
        print(
            "::error::All tests were skipped — possible false-green! "
            "请检查是否缺少依赖/环境导致用例整体被跳过。"
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
