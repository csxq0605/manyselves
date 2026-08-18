# Manyselves 第一阶段 Web 与桌面服务化设计

**状态：** 已批准

**日期：** 2026-07-31

**范围：** React Web、Electron 桌面端、FastAPI 服务端、Linux Docker Compose 部署

**原则：** 功能完整迁移、核心行为冻结、服务器工作区权威、PyQt 可回退

## 1. 目标

在不重写 Agent、Loop、MessageBus、Reporting、ConversationStore 和既有项目文件格式的前提下，为 Manyselves 增加 React Web、Electron 桌面端和 FastAPI 服务端。第一阶段完成后，浏览器、Electron 和原 PyQt 均能使用同一套核心运行能力，Linux 服务器可通过 Docker Compose 部署。

## 2. 范围边界

### 2.1 冻结范围

- `manyselves/core/**`
- `manyselves/templates/**`
- `.manyselves/conversations/` 的 JSONL 与元数据格式
- `Inputs/`、`Knowledge/`、`Templates/`、`Work/runs/`、`Outputs/` 的目录语义
- Agent、工具、Reporting 和检查点的业务行为

冻结目录如需变更，必须独立批准，不能作为 Web 适配的附带修改。

### 2.2 允许变更

- 对 `manyselves/app.py` 做行为等价的启动装配提取
- 新增 `manyselves/application/` 应用适配层
- 新增 `manyselves/webapi/` HTTP/SSE 适配层
- 新增 `frontend/` React 工程
- 新增 `desktop/` Electron 工程
- 新增 `deploy/` 部署资产
- 增加依赖、测试、契约和迁移验收文档

### 2.3 第一阶段明确不做

- 不引入 MySQL、Redis、Milvus、MinIO 或 LLM Wiki
- 不实现真正的租户、用户、RBAC 和项目级 Worker 调度
- 不让多个 Gunicorn worker 共享同一工作区
- 不做客户端与服务器目录双向同步
- 不删除 PyQt
- 不把模型 API Key 下发给浏览器或 Electron

## 3. 目标架构

```text
React Web ───────┐
                 ├─ HTTPS REST + SSE ─ FastAPI ─ RuntimeFacade ─ RuntimeHost
Electron + React ┘                                             │
                                                              ├─ MessageBus
PyQt ──────────────────────────────────────────────────────────┼─ LoopManager
                                                              ├─ Agent Loops
                                                              └─ Reporting

服务器工作区是项目文件、对话、检查点和输出的唯一事实源。
```

FastAPI 只负责协议、校验、错误映射、事件分发和应用层编排，不承载 Agent 业务规则。`RuntimeFacade` 是 Web 唯一可以调用的运行入口；当前 GUI 对私有状态的读取集中封装在 `LegacyRuntimeAdapter`，避免向 HTTP 层扩散。

生产模块同时导出 `create_app()` 和轻量的 `app = create_app()`；Runtime 只在 ASGI lifespan 中创建。Uvicorn 开发模式使用 `create_app --factory`，Gunicorn 生产模式加载 `manyselves.webapi.main:app`，不向 Gunicorn 传递 Uvicorn 专用的 `--factory` 参数。

## 4. 运行模型

- 每个服务实例恰好拥有一个 `RuntimeHost`。
- 每个 Runtime 同一时刻只有一个活动工作区。
- Gunicorn 固定一个 `uvicorn_worker.UvicornWorker`。
- 查询可并发，运行时变更命令经单一异步锁串行化。
- 长命令返回 `202` 和 `commandId`，结果由 SSE 推送。
- 多客户端可观察；运行时控制使用带过期时间的控制租约。
- 多场景同时运行在第一阶段通过多个 Compose project 实例实现。

## 5. 数据与文件语义

服务器项目根目录：

```text
/data/manyselves/projects/{project_id}/
├── Inputs/
├── Knowledge/
├── Templates/
├── Work/runs/
├── Outputs/
├── Capabilities/
└── .manyselves/conversations/
```

客户端只保存服务器地址、安全令牌、UI 偏好、未保存草稿、有限预览缓存和用户主动下载的副本。本地文件或文件夹必须显式上传后，Agent 才能通过服务器相对路径使用。

文本文件读取返回内容和 SHA-256 revision；保存必须携带 `baseRevision`。revision 不匹配返回 `409 FILE_REVISION_CONFLICT`。所有路径先解析到项目根目录，再验证没有绝对路径、`..`、符号链接逃逸或保留目录越权。

## 6. HTTP 与事件契约

所有 API 使用 `/api/v1` 前缀。REST 是事实快照和命令入口，SSE 是非权威实时通知。核心资源包括健康状态、bootstrap、控制租约、项目、文件、会话、Agent、Reporting 和设置。

SSE 事件统一字段：

```json
{
  "schemaVersion": 1,
  "eventId": "evt_xxx",
  "sequence": 42,
  "type": "agent.message.completed",
  "timestamp": "2026-07-31T00:00:00Z",
  "projectId": "project_xxx",
  "sessionId": "session_xxx",
  "agentId": "main",
  "runId": null,
  "messageId": "message_xxx",
  "payload": {}
}
```

服务端维护有界环形缓冲区。客户端用 `Last-Event-ID` 重连；无法补发时接收 `stream.resync_required` 并重新获取 bootstrap。delta 可合并，终态、错误、等待用户和检查点事件不能丢失。

## 7. 客户端架构

React 使用 TypeScript 严格模式、Vite、React Router、TanStack Query、Zustand、Monaco Editor、Vitest 和 Playwright。OpenAPI 契约生成 TypeScript API 类型。

同一 React 代码通过 `PlatformBridge` 适配浏览器和 Electron。Electron 只负责本地文件/目录选择、上传、下载保存、本地打开、系统通知、快捷键和令牌安全存储；Agent 核心与 Provider 密钥始终位于服务器。

Electron 必须禁用 Node integration、启用 context isolation 与 sandbox，只通过经过参数校验的 preload API 暴露最小能力，并加载本地打包的 React 资源。

## 8. 功能完整性

迁移验收矩阵必须覆盖：项目管理、文件树、文本编辑、多标签页、PDF/图片/SVG/Excel/CSV/DOCX/Markdown/代码预览、Python 运行、文件引用、会话管理、流式消息、编辑重发、回滚、主 Agent 和动态 Agent、队列、Thinking、工具调用、中断、调试、Provider/模型/预设配置以及完整 Reporting 生命周期。

每项记录原 PyQt 证据、React 组件、API/SSE、浏览器行为、Electron 行为、自动化测试和人工验收。矩阵未达到 100% 时不得移除 PyQt 或宣称迁移完成。

## 9. 部署与安全

第一阶段 Compose 包含 `manyselves-api` 和 `manyselves-web`；Nginx 提供 React 静态资源、TLS、反向代理和 SSE。API 容器固定单副本、单 worker，以非 root 用户运行，项目数据挂载到 `/data/manyselves`。日志输出 stdout，数据卷定期备份并演练恢复。

第一阶段只面向企业内网、VPN 或可信网络。采用部署级访问令牌，不提供用户级身份和 RBAC。当前 Python/工具执行能力在服务器上仍属于受信任代码执行，不能把第一阶段实例直接暴露到公共互联网。

## 10. 发布门槛

- 冻结目录没有未经批准的行为变更
- 原核心和 PyQt 测试全部通过
- PyQt 入口仍可运行
- 旧项目、会话、运行检查点和输出直接兼容
- API、SSE、React、Electron 和 Compose 测试全部通过
- 浏览器刷新、客户端重连、事件补发、文件冲突、上传中断和服务关闭恢复通过
- 功能矩阵达到 100%
- 完成全新 Linux 主机部署和备份恢复演练
- 没有高危安全问题

## 11. 第二阶段预留

所有 DTO 保留明确的 `projectId`、`sessionId`、`runId` 和 `actorId`，但第一阶段不伪造租户能力。第二阶段可在 `RuntimeFacade` 外增加控制面，在项目级 Worker 外增加 Redis 队列，在文件存储外增加 MinIO，在元数据外增加 MySQL，在知识检索外增加 Milvus，而无需让 React 直接依赖这些基础设施。
