# Final Runtime Web Demo Handoff

## 目的

这是 Manyselves 声明式运行时的最终真实测试交接。真实 Provider、真实项目和
浏览器流程在本交接前均不宣称已经验证；本文件只描述待执行的网页版 Demo
操作。

用户只需要操作 Demo 页面，不需要比较 Legacy 与 Declarative 路径，也不需要
检查内部文件或调用内部 API。开发者的自动化等价验证另列在文末。

## 启动 Demo

如果 Demo 尚未启动，由开发环境先构建当前分支的前端，再启动一次服务：

```bash
npm --prefix frontend run build
uv run python run_web.py \
  --host 127.0.0.1 \
  --port 9092 \
  --data-dir /绝对路径/到/本次真实测试数据目录
```

`--data-dir` 必须填写明确的绝对路径，并指向本次测试使用的数据目录；该目录中
应包含要测试的真实项目。不要依赖启动命令所在目录隐式选择 `.manyselves`，也
不要指向需要保持只读的已知成功基线。

然后用当前 Chrome 或 Edge 打开：

<http://127.0.0.1:9092/>

如果页面已经打开，直接从登录步骤开始。

## 用户操作流程

### 1. 登录并设置 Provider

1. 登录 Demo。
2. 打开 **模型设置**，选择要使用的真实 Provider 和模型。
3. 填写 API Key，保存，并点击 **测试连接**。
4. 选择要测试的真实项目。

### 2. 测试 `parameter-adjustment`

1. 打开侧边栏的 **工作流** 页面。
2. 选择 Workflow `parameter-adjustment`。
3. 在通用表单的数值字段填写 `4`，点击 **启动工作流**。
4. 观察同一页面的状态、事件、输出和成本。

预期结果：状态为 `completed`，输出值为 `10`，没有 Provider 调用，tokens
为 `0`。如果当前环境没有配置模型定价，金额会显示“成本未知”，这是定价状态
的明确投影，不代表该工作流调用了 Provider。

可将数值改为 `12` 再启动一次；预期输出为 `12`，同样不产生 Provider
调用；tokens 仍为 `0`，金额显示规则与上面相同。

### 3. 测试 `full-report`

1. 仍在 **工作流** 页面选择 Workflow `full-report`。
2. 使用页面根据 Schema 生成的通用表单填写：
   - **instruction**：针对当前项目生成完整供配电评估报告，并保持事实可追溯、明确不确定性
   - **missing_evidence_policy**：`draft`
   - **cost_control_mode**：`observe`
   - **max_provider_attempts**：保持默认值或按实际测试预算填写
   - **max_total_tokens**：保持默认值或按实际测试预算填写
3. 点击 **启动工作流**，记下页面显示的 Run ID。
4. 保持页面打开，观察状态、事件、输出和成本，不要另起一个替代 Run。

预期结果：

- 状态持续更新，并最终显示 `completed`；
- Events 区域持续显示当前 Run 的工作流与 Action 生命周期；
- 输出列表出现当前 Run 生成的报告产物；
- 成本区域显示本次真实 Provider 使用情况；
- 页面没有出现未处理错误。

### 4. 必要时继续 WAITING

如果页面显示等待输入：

1. 在页面显示的继续输入表单中填写所要求的值；
2. 点击 **提交运行输入**；
3. 继续观察同一个 Run 的状态、事件、输出和成本。

不要刷新后另起 Run，也不要绕过页面提交输入。

## 网页 Demo 的完成标准

本次网页版真实测试在以下结果都出现时完成：

- `parameter-adjustment` 能通过通用表单完成并得到预期输出，没有 Provider 调用且
  tokens 为 `0`；未配置定价时允许金额显示“成本未知”；
- `full-report` 能通过通用 Schema 表单启动；
- Reporting Run 的状态、事件、输出、成本能够在页面中持续投影；
- 若出现 WAITING，可以在同一页面继续并保留同一个 Run；
- 完成结果对应当前页面显示的 Run，而不是用户手动选择的旧结果。

这次用户流程不包含 Legacy/Declarative 人工 A/B，也不要求切换默认执行路径。

## 开发者自动验证（用户不操作）

开发者在后台通过 focused 自动化测试验证：

- 通用 Schema 控件、nullable 值、数组输入、复杂 JSON 字段和未知 Schema fallback；
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
