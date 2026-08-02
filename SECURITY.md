# 安全政策 / Security Policy

## 支持的版本 / Supported Versions

| 版本 | 支持状态 |
|------|---------|
| v0.3.x | ✅ 积极维护 |
| < v0.3 | ❌ 不再维护 |

## 报告漏洞 / Reporting a Vulnerability

我们非常重视安全问题。请**不要**在公开的 GitHub Issues 中报告安全漏洞。

如需报告安全漏洞，请通过以下方式联系维护者：

1. 在 GitHub 上创建一个 **Security Advisory**（推荐）：前往仓库的 `Security` → `Advisories` → `New advisory`
2. 或通过电子邮件联系项目维护者（见仓库主页）

我们将在 48 小时内确认收到报告，并在评估后制定修复计划。

## 安全最佳实践

### 部署安全

- 始终设置 `API_KEY` 环境变量，默认空串会关闭鉴权
- 生产环境使用 `HOST=127.0.0.1` 或反向代理，避免暴露在公网
- 设置 `CORS_ORIGINS` 为显式域名列表，避免使用通配符 `*`
- 启用 `DEBUG_ENDPOINTS_ENABLED=false`（默认值）关闭调试端点
- 使用反向代理过滤 `api_key` 查询参数，避免日志泄露

### 配置安全

```bash
# 最小安全生产配置
API_KEY=your-strong-secret-key        # 必填
HOST=127.0.0.1                        # 建议绑定本地
CORS_ORIGINS=https://your-domain.com  # 显式白名单
DEBUG_ENDPOINTS_ENABLED=false         # 默认关闭
RBAC_ENABLED=true                     # 启用角色控制
```

### 依赖安全

- 定期更新依赖至最新版本
- 使用 `pip-audit` 或类似工具扫描已知漏洞
- 生产部署建议使用 requirements-locked.txt 锁定版本

## 安全架构概述

本项目采用纵深防御策略：

1. **鉴权层**：API Key 恒定时间比较 + 多 Key 轮换
2. **授权层**：RBAC 三级角色（admin/developer/viewer）
3. **限流层**：端点级滑动窗口限流，fail-closed
4. **输入层**：请求体大小限制、输入校验、路径遍历防护
5. **输出层**：敏感信息脱敏、错误信息 sanitize
6. **传输层**：CORS 白名单、安全 HTTP 头（CSP、X-Frame-Options 等）