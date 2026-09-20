# Manyselves 操作手册

面向日常使用：启动服务、在 Main 对话里完成报告、理解默认参数与确认时机。

开发架构约定见仓库根目录 [`AGENTS.md`](../AGENTS.md)。内部实施状态不在本手册范围。

---

## 1. 启动

### 本地 Web（推荐演示/验收）

```bash
# 在仓库根目录
.venv\Scripts\python.exe run_web.py --host 127.0.0.1 --port 9092 --data-dir <包含 test 项目的工作目录>
```

示例（Windows）：

```powershell
.venv\Scripts\python.exe run_web.py --host 127.0.0.1 --port 9092 --data-dir "E:\PKU\program\2026\Aug\work"
```

浏览器打开：<http://127.0.0.1:9092/>

默认登录：`admin` / `yuanxi@2026`（可在设置中修改）。

登录后：

1. 选择项目（如 `test`）
2. 在 **模型设置** 配置 Provider 与 API Key
3. 在 **Main 对话** 用自然语言说明目标

### 桌面端

```bash
uv run manyselves
```

---

## 2. 六种报告操作

用户只需要在 Main 里说目标，由 Main 选择操作并调用 `run_reporting_workflow`。

| 你说什么 | Main 选择 |
|---|---|
| 用当前项目资料生成完整报告 | `full_report` |
| 只写某一个/几个模块 | `module_report` |
| 把已有五份模块报告合成总报告 | `aggregate_existing` |
| 把现有 Markdown 转成 Word | `render_existing` |
| 从模板 DOCX 蒸馏角色 Skill | `distill_template_skill` |
| 改一份已有完整报告 | `revise_report` |

不需要用户填写内部 Schema，也不需要进入工作流页面。

---

## 3. 修订已有报告（revise_report）

### 3.1 启动时必须能确定的

| 参数 | 说明 |
|---|---|
| `baseline_run_id` | 基于哪一份完整报告 Run；对话已确认时 Main 可直接用，**不会自动选「最近一次」** |
| 修改范围 | 见下方三种方式 |

### 3.2 修改范围怎么定

| 方式 | 适用 | `impact_mode` | `requested_changes` |
|---|---|---|---|
| 手工点小节 | 你已知道要改哪里 | `none`（默认） | 必填，如 `{"2.3.1":"澄清定值表述"}` |
| 先看影响清单 | 「先分析会影响哪些地方」 | `confirm` | 可空 |
| 按新资料直接改 | 「按新资料修订并交付」 | `auto` | 可空，可作种子 |

**confirm 流程：**

```text
系统比较基线与当前 Inputs
  → 生成影响清单（业务产物）
  → 停下等你确认
      accept_all      全部接受
      accept_selected 只接受部分小节
      abort           取消
  → 继续模块修订 → Cross → 汇总 → 终审 → 交付
```

影响清单位置：

```text
Work/runs/<修订Run>/reviews/impact-analysis.json
```

包含：输入变化、影响小节、原因、建议指令、证据 ID。

### 3.3 修订会改到哪里

- **指定/确认的小节**：模块作者修订 + 独立复审
- **未指定模块**：保留基准正文，不重写
- **Cross**：修订后仍会联动相关模块，保持全文一致（不能承诺只改你点名的小节）

---

## 4. 默认参数（一般不用改）

| 参数 | 默认 | 何时改 |
|---|---|---|
| `missing_evidence_policy` | `draft` | 缺证据要停/问/跳过时改为 `ask` / `block` / `skip` |
| `cost_control_mode` | `observe` | 完成版产品按 observe 记录用量；其他枚举值仅保留输入兼容，不承诺暂停语义 |
| `preparation_mode` | `deterministic_workers` | 需要串行排查时改为 `serial` |
| `preparation_concurrency` | `5` | 与五模块对齐；可 1–16 |
| `max_provider_attempts` | `600` | 用量记录元数据，不作为完成版产品的强制停止条件 |
| `max_total_tokens` | `30_000_000` | 用量记录元数据，不作为完成版产品的强制停止条件 |
| `impact_mode` | `none` | 仅 revise；见上文 |

**说明：** 完成版产品的成本合同是记录和展示用量；`warn` / `pause_at_boundary` 与两个 `max_*` 输入不构成「超限必停」承诺。这是已确认的产品边界，不是未完成项。

---

## 5. Main 能自己决定什么

**Main 可以：**

- 选择六种 operation 中的一种
- 在基线唯一时选用哪个 `baseline_run_id`
- 根据你的说法设置 `impact_mode`
- 使用上述默认参数

**Main 不应：**

- 编造小节 ID 或专业结论
- 在未启动影响分析时声称「已自动分析完」
- 在 Run 未 completed 前宣称已交付

**需要你出现的时刻：**

- 基线 Run 无法唯一确定
- `impact_mode=confirm` 确认清单
- 明确要求特殊缺证策略、预算或文件名

---

## 6. 状态怎么看

| 状态 | 含义 |
|---|---|
| `accepted` | 请求已受理，尚未完成 |
| `waiting` | 等待你输入（如影响清单确认） |
| `running` | 正在执行；以运行卡/运行态为准 |
| `completed` | 已完成；有交付产物才能称「已交付」，同时检查是否带有渲染警告 |
| `failed` | 失败；记录真实 Run ID 与错误，同 Run 可恢复 |

输出通常在：

```text
Outputs/Reports/          # 公开报告与来源索引
Work/runs/<runId>/        # 该 Run 的中间产物与交付包
```

### DOCX 发布与验证

- 生成文件无法被 Word/WPS 打开时，渲染失败，不发布成功结果。
- 文件可以打开，但缺少批准正文/标题或仍含未解析内容 token 时，系统继续发布 DOCX，同时在 render result/log 中写入 `validation_warnings`，并将 `protected_prose_verified` 设为 `false`。
- 带警告的 `completed` 表示文件已经交付，不表示内容或版式已经通过专业验收；应结合警告和人工验收判断。

---

## 7. 常见说法示例

```text
「用当前项目三份 Excel 和知识库生成完整供配电评估报告」
→ full_report

「改上一份报告的 2.3.1，澄清定值保护表述」
→ revise_report + requested_changes={"2.3.1": "..."}

「Inputs 更新了，先看会影响哪些地方」
→ revise_report + impact_mode=confirm

「按新资料修订并交付」
→ revise_report + impact_mode=auto

「只接受清单里的 2.3.1」
→ confirm 等待时选择 accept_selected + ["2.3.1"]
```
