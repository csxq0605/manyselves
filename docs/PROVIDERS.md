# 提供商配置指南

## 支持的 AI 服务商

### 🇨🇳 国内服务商

| 服务商 | 预设 ID | 协议 | 默认模型 | API 地址 |
|--------|----------|----------|----------|----------|
| **MiMo API（小米，服务端推荐）** | `openai-xiaomi-mimo-api-china` | `openai` | mimo-v2.5-pro | https://api.xiaomimimo.com/v1 |
| **MiMo Token Plan（仅 Coding 工具）** | `anthropic-xiaomi-mimo-token-plan-china` | `anthropic` | mimo-v2.5-pro | https://token-plan-cn.xiaomimimo.com/anthropic |
| DeepSeek | `openai-deepseek` | `openai` | deepseek-chat | https://api.deepseek.com |
| Kimi（月之暗面） | `openai-kimi` | `openai` | moonshot-v1-8k | https://api.moonshot.cn/v1 |
| 通义千问（阿里云） | `openai-qwen` | `openai` | qwen-turbo | https://dashscope.aliyuncs.com/compatible-mode/v1 |
| 智谱AI（GLM） | `openai-zhipu-glm` | `openai` | glm-4-flash | https://open.bigmodel.cn/api/paas/v4 |

### 🌍 国际服务商

| 服务商 | 预设 ID | 协议 | 默认模型 | API 地址 |
|--------|----------|----------|----------|----------|
| OpenAI (ChatGPT) | `openai-official` | `openai` | gpt-4o | https://api.openai.com/v1 |
| Claude (Anthropic) | `anthropic-official` | `anthropic` | claude-sonnet-4-20250514 | https://api.anthropic.com |
| Google Gemini | `openai-google-gemini` | `openai` | gemini-2.0-flash-exp | https://generativelanguage.googleapis.com/v1beta/openai |

### 🔀 聚合平台

| 服务商 | 预设 ID | 协议 | 默认模型 | API 地址 |
|--------|----------|----------|----------|----------|
| OpenRouter | `openai-openrouter` | `openai` | anthropic/claude-sonnet-4.6 | https://openrouter.ai/api/v1 |
| Groq | `openai-groq` | `openai` | llama-3.3-70b-versatile | https://api.groq.com/openai/v1 |

## 使用方式

### 方式 1：前端界面切换（推荐）

1. 访问 `http://服务器IP:9090`
2. 登录后进入"设置"页面
3. 在“预设”中选择服务商，再确认“协议”、API 地址、默认模型和配置名称
4. 输入 API Key；需要额外认证时，在“高级连接设置”填写附加请求头 JSON
5. 点击“测试连接”只验证当前表单，不保存任何内容
6. 勾选“设为活动配置”后点击“保存配置”，配置、活动项和默认模型会一起生效

### 方式 2：修改配置文件

编辑 `/data/manyselves/manyselves.config.yaml`：

```yaml
providers:
  active: mimo-cn  # ← 保存后的配置 ID，不是协议或预设 ID
  configurations:
  - id: mimo-cn
    presetId: anthropic-xiaomi-mimo-token-plan-china
    provider: anthropic
    apiBase: https://token-plan-cn.xiaomimimo.com/anthropic
    defaultModel: mimo-v2.5-pro
    enabled: true   # ← 启用该提供商
```

然后重启服务：

```bash
podman restart manyselves-phase1_api_1
```

### 方式 3：环境变量

在 `deploy/.env` 中设置：

```bash
# DeepSeek API Key
MANYSELVES_DEEPSEEK_API_KEY=your-deepseek-key

# 或 Kimi API Key
MANYSELVES_OPENAI_API_KEY=your-kimi-key  # Kimi 使用 OpenAI 协议
```

## 切换示例

### 切换到 DeepSeek

**前端操作**：
1. 选择预设：`DeepSeek`
2. 输入 API Key：`sk-xxxxx`
3. 点击"测试连接"验证
4. 点击"保存"

**配置文件**：
```yaml
providers:
  active: deepseek
  configurations:
  - id: deepseek
    enabled: true
    apiKey: null  # 由环境变量或前端界面提供
```

### 切换到 Kimi

**前端操作**：
1. 选择预设：`Kimi`
2. 输入 API Key：`sk-xxxxx`
3. 默认模型可改为：`moonshot-v1-32k`（如需长上下文）
4. 点击"保存"

### 切换到 OpenAI（ChatGPT）

**前端操作**：
1. 选择预设：`OpenAI (ChatGPT)`
2. 输入 API Key：`sk-xxxxx`
3. 点击"保存"

## API Key 管理

### 安全建议

1. **不要在配置文件中硬编码 API Key**
2. **使用环境变量**：在 `.env` 文件中设置
3. **或在前端界面输入**：前端会安全传输到服务器

### 环境变量命名

```bash
# MiMo / OpenAI 协议服务
MANYSELVES_OPENAI_API_KEY=tp-your-key

# DeepSeek（如果使用独立环境变量）
MANYSELVES_DEEPSEEK_API_KEY=sk-your-key

# Anthropic (Claude)
MANYSELVES_ANTHROPIC_API_KEY=sk-ant-your-key
```

## 自定义配置

### 添加新提供商

如果需要添加不在列表中的服务商：

1. 在“预设”选择“自定义连接”
2. 填写配置名称、协议、API 地址、模型名称和 API Key
3. 如需网关专用头，在“高级连接设置”填写附加请求头 JSON
4. 测试成功后保存；只有勾选“设为活动配置”才会切换默认模型

### 修改默认模型

在前端界面：
1. 选择提供商
2. 在"默认模型"输入框修改
3. 点击"保存"

## 常见问题

### Q: 如何验证配置是否生效？

查看日志：
```bash
podman logs manyselves-phase1_api_1 | grep "Sending openai request"
```

应该显示：
```
Sending openai request: model=mimo-v2.5-pro
```

### Q: 切换提供商后需要重启吗？

- **前端界面修改**：自动生效，无需重启
- **配置文件修改**：需要重启容器

### Q: 可以同时配置多个提供商吗？

可以！配置文件中可以配置多个提供商，通过设置 `active` 切换，或在前端界面动态选择。

### Q: API Key 会保存在哪里？

- 前端输入：安全传输到服务器，保存在配置文件或环境变量中
- 前端界面永远不会返回 API Key（只显示"已配置"）

## 技术细节

多数国内服务商（DeepSeek、Kimi、Qwen、GLM）使用 **OpenAI 兼容协议**，通过不同的 `apiBase` 区分服务商。MiMo Token Plan 预设使用 **Anthropic Messages 协议**，必须保留其 `/anthropic` API 地址；它不能因为与 Claude 同协议而共享或覆盖 Claude 配置。

预设 ID、协议和配置 ID 是三种不同的标识。一个协议下可以保存多条配置；页面会按预设 ID 复用同一预设配置，并保留旧/自定义配置供人工清理。

这保证了良好的兼容性和易用性。
