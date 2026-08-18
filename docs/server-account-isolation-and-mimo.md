# 服务端账户隔离与 MiMo 配置

## 多账户隔离启动

多账户模式只启动一个 FastAPI/HTTP 服务。登录会话中的 `account_id` 决定请求进入哪个账户
worker；每个账户在同一服务进程内拥有独立的 `RuntimeHost`、LoopManager、消息总线、SSE
Broker、控制租约和 Provider 客户端。不同账户可并行工作，但不会共享可变运行时。

1. 复制 `deploy/accounts.example.yaml`，为每个账户设置唯一的 `id`、登录用户名和保存密码的
   环境变量名 `passwordEnv`。账户文件不保存 API Key。
2. 在服务环境中设置各 `passwordEnv` 对应的登录密码。在 Linux/macOS 上对账户清单执行
   `chmod 600 accounts.yaml`；权限更宽时服务会拒绝加载。
3. 启动：

   ```bash
   python run_web.py --accounts-file /absolute/path/to/accounts.yaml
   ```

所有账户共享同一个端口和 React 网页。登录后，从“账户与设置 → 模型配置”保存 API Key；
请求会把设置操作绑定到登录账户的 ConfigManager，密钥只写入：

```text
<dataRoot>/accounts/<accountId>/.manyselves/config/manyselves.config.yaml
```

多账户 worker 不读取进程级 Provider API Key 或仓库 `.env` 中的 Provider Key，避免一个全局
密钥自动进入所有账户。项目、会话、报告、日志、事件库和全局知识库也全部位于
`<dataRoot>/accounts/<accountId>/` 下。HTTP Cookie 只携带签名后的账户身份；每个业务请求再据此
选择账户 worker，因此相同项目 ID 可以在不同账户内独立创建和写入。

## MiMo 模型能力

`mimo-v2.5-pro` 和 `mimo-v2.5` 的 OpenAI 兼容请求会自动：

- 通过 `extra_body.thinking.type=enabled` 开启深度思考；
- 使用 `max_completion_tokens` 作为思考与最终回答的合计上限，并省略思考模式不支持的自定义
  `temperature`；
- 读取响应和流式增量的 `reasoning_content`；
- 在带工具调用的后续轮次完整回传 assistant 的 `reasoning_content`；
- 通过 `LLMProvider.chat_structured()` 使用 `response_format={"type":"json_object"}`，并在
  返回前校验完整内容确实是 JSON object。

结构化输出的 prompt 仍需明确要求只返回 JSON，并定义字段、层级和类型；过小的
`max_completion_tokens` 仍可能得到被截断的无效 JSON。

## 模型选择与计价

单账户部署的 `.env` 可用以下任一模型值：

```dotenv
MANYSELVES_BOOTSTRAP_MODEL=mimo-v2.5-pro
# 或
MANYSELVES_BOOTSTRAP_MODEL=mimo-v2.5
```

也兼容 `MIMO_MODEL`。多账户部署由每个账户在网页模型设置中分别选择模型。运行台账中已有
`resolved_model`/`model` 时，以台账事实为准；只有旧台账
缺少模型字段时才用环境模型补全。成功终态会分别按实际模型输出国内按量 API 价格和 Token
Plan Credits，并比较 Lite、Standard、Pro、Max 的月付/年付额度占比。缓存写入按官方按量
价格为免费；Token Plan 没有单列缓存写入价格，因此按未命中 Credits 换算。

Token Plan 官方条款限定其用于 AI 编程工具，禁止自定义应用后端等明显非 Coding 场景。这里的
Token Plan 数字仅用于额度和固定订阅成本折算，不表示服务端可以使用 Token Plan Key；自定义
服务端应使用普通 API Key 的按量计费通道。
