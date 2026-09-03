# Final Runtime Web Demo Handoff

## 目的

这是 Manyselves 声明式运行时的最终真实测试交接。本轮验收使用真实 Provider、
真实 `test` 项目和浏览器；历史 Run 只作为诊断证据，不能替代本轮结果。

用户只需要在 Demo 的 **Main 对话**中说明报告目标，不需要比较 Legacy 与
Declarative 路径，不需要打开已移除的通用“工作流”产品页，也不需要检查内部
文件或调用内部 API。中立 Capability 的 API smoke 由开发者完成。

## 启动 Demo

如果 Demo 尚未启动，由开发环境先构建当前分支的前端，再启动一次服务：

```bash
npm --prefix frontend run build
uv run python run_web.py \
  --host 127.0.0.1 \
  --port 9092 \
  --data-dir /绝对路径/到/包含test项目的工作目录
```

`--data-dir` 必须填写明确的绝对路径，并指向包含 `test` 项目的工作目录；启动后
明确选择 `test`，不要把 `test` 本身误当成 data root，也不要依赖启动命令所在
目录隐式选择 `.manyselves`。本轮不得覆盖需要保持只读的已知成功基线。

然后用当前 Chrome 或 Edge 打开：

<http://127.0.0.1:9092/>

如果页面已经打开，直接从登录步骤开始。

## 用户操作流程

### 1. 登录并设置 Provider

1. 登录 Demo。
2. 打开 **模型设置**，选择要使用的真实 Provider 和模型。
3. 填写 API Key，保存，并点击 **测试连接**。
4. 选择要测试的真实项目。

### 2. 开发者 smoke：`parameter-adjustment`

这一步不属于用户产品流程。开发者通过通用 `/api/v1/runs` 接口启动
`parameter-adjustment`，输入 `{"value": 4}`，并读取同一 Run 的状态、输出和
成本。

预期结果：状态为 `completed`，输出值为 `10`，没有 Provider 调用，tokens
为 `0`。如果当前环境没有配置模型定价，金额会显示“成本未知”，这是定价状态
的明确投影，不代表该工作流调用了 Provider。

如需第二个边界值，可输入 `12`；预期输出为 `12`，同样不产生 Provider 调用。
用户不需要在浏览器中寻找或启动此中立测试 Capability。

本轮 Windows 验收已在 `127.0.0.1:9092` 完成这一步：Run
`parameter-adjustment-ad8cf9ebb7fb4b69a9b4bc4d5ff164e0` 状态为 `completed`，
输出为 `10`，Provider attempts 与 tokens 均为 `0`；侧边栏运行态页面显示同一
Run `1 / 1` 已完成、队列为空且没有错误。

### 3. 在 Main 对话测试 `full-report`

1. 在所选 `test` 项目的 **Main 对话**中输入：
   “使用当前项目三份 Excel、知识库和已准备的角色 Skill，生成完整供配电评估报告；
   缺少证据时保留草稿并明确不确定性，保持事实可追溯。”
2. Main 应直接选择 `full_report` 并调用本轮注入的
   `run_reporting_workflow`；不应要求用户先列出 Workflow、读取 Schema 或转到
   通用工作流页面。
3. 在 Main 顶部运行卡记下 Run ID。启动回执只表示请求已接受，完成状态以同一
   运行卡为准。
4. 保持页面打开，通过同一运行卡和 **运行态** 详情观察状态、阶段、事件、输出
   和成本，不要另起替代 Run。

预期结果：

- Main 顶部运行卡持续更新，并最终显示 `completed`；
- 运行态详情持续显示当前 Run 的工作流阶段与 Action 生命周期；
- 输出列表出现当前 Run 生成的报告产物；
- 成本区域显示本次真实 Provider 使用情况；
- 页面没有出现未处理错误。

### 4. 必要时继续 WAITING

如果页面显示等待输入：

1. 在 Main 运行卡显示的继续输入表单中选择或填写所要求的值；
2. 点击提交；
3. 继续观察同一个 Run 的状态、事件、输出和成本。

不要刷新后另起 Run，也不要绕过页面提交输入。

## 网页 Demo 的完成标准

本次网页版真实测试在以下结果都出现时完成：

- `parameter-adjustment` 通过开发者 API smoke 完成并得到预期输出，没有 Provider
  调用且 tokens 为 `0`；未配置定价时允许金额显示“成本未知”；
- `full-report` 由 Main 对话直接启动，没有暴露内部 Workflow 选择或 Schema 表单；
- Reporting Run 的状态、阶段、事件、输出、成本在 Main/运行态页面中持续投影；
- 若出现 WAITING，可以在 Main 的同一 Run 卡继续并保留同一个 Run；
- 完成结果对应当前页面显示的 Run，而不是用户手动选择的旧结果。

这次用户流程不包含 Legacy/Declarative 人工 A/B，不要求切换默认执行路径，也不
包含侧边栏“工作流”入口。

## 开发者自动验证（用户不操作）

开发者在后台通过 focused 自动化测试验证：

- 通用 `/runs` 输入合同、nullable 值、数组输入、复杂 JSON 字段和未知 Schema fallback；
- WAITING 投影与同一 Run 的继续输入；
- 通用 Workflow 与既有 Reporting 契约的行为等价、状态恢复、交付和成本投影。

这些自动验证不替代本次真实 Provider、真实项目和浏览器操作，也不要求用户
执行额外命令或检查内部产物。

## 失败处理

如果页面出现失败、异常状态、错误输出、成本不一致，或等待输入无法继续：

1. 立即停止操作；
2. 只提供页面显示的 Run ID；
3. 不刷新重试、不启动新的 Run、不删除或覆盖任何现场数据。

保留当前页面和服务器现场，后续由开发者依据 Run ID 继续诊断。不要自行
恢复、回滚或切换到另一条执行路径。
