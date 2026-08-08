# Lujo-MCP v0.4.0-beta

## Overview

Lujo-MCP 是面向 AI Agent 的运行时调试上下文基础设施。

目标：让 AI Agent 不仅读取代码，还能够理解真实 Bug 运行现场。

## What's New

### Runtime Debug Context

采集：

- runtime events
- exceptions
- logs
- user behavior context

### Debug Experience RAG

通过历史 Debug Experience 增强 Agent 分析能力：

- `DebugExperienceRecord` 输出 DTO/View（纯 View，不建存储、不替代 DebugCase）
- `retrieve_debug_experience()` 三层检索：L1 fingerprint 精确 → L2 message normalize → L3 vector（默认关闭）

### Context Assembly

Agent 上下文构建与 RAG 解耦：

- `_safe_debug_experience_recall()` 集成于 `assemble()` 输出（可选字段，默认 None）
- `debug_experience_enabled` 默认 `False`，关闭状态零调用、零耗时

### Verification Foundation

提供验证能力基础（Verifier / assert engine / spec store）。

## Architecture

```
Runtime
    ↓
Debug Context
    ↓
Debug Experience RAG
    ↓
Agent
    ↓
Verifier
```

## Validation

测试：874 passed / 6 skipped / 0 failed

说明：无回归。

## Architecture Rules

- Runtime 保持纯净（不依赖 RAG / Agent / LLM / MCP）
- RAG 独立（不反向依赖 Agent / Runtime / LLM / MCP）
- Agent 消费 RAG（允许方向：Agent → RAG）

## Limitations

当前版本：

- 不包含自动修复
- 不包含 Patch 生成
- 不包含 Repair Loop

## Roadmap

未来：

- Debug Experience Quality Evaluation
- Benchmark Dataset
- Repair Loop Research
- Human Approval Workflow
