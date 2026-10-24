# Manyselves 操作手册

面向日常使用：启动服务、在 Main 对话里出报告和改报告、看运行状态和输出。

开发架构约定见仓库根目录 [`AGENTS.md`](../AGENTS.md)；报告流程的阶段划分见 [README](../README_zh.md#内置能力配电安全评估报告)。

---

## 1. 启动

### 本地 Web

```bash
# 在仓库根目录
uv sync
cp manyselves.config.example.yaml manyselves.config.yaml
cp .env.example .env            # 设置管理员账号和密码
cd frontend && npm ci && npm run build -- --base / && cd ..
uv run python run_web.py --host 127.0.0.1 --port 9090 --data-dir <工作目录>
```

前端要从根路径访问时必须带 `--base /` 构建（默认构建给 `/manyselves/` 子路径部署用，从根路径打开会是空白页）。

浏览器打开 <http://127.0.0.1:9090/>，用 `.env` 里的 `MANYSELVES_ADMIN_USERNAME` / `MANYSELVES_ADMIN_PASSWORD` 登录，然后：

1. 选择项目；
2. 在 **模型设置** 配置供应商和 API Key；
3. 在 **Main 对话** 用自然语言说明目标。

更多启动方式（`start.sh`、Docker、多账户）见 [RUNNING.md](RUNNING.md) 和 [RUN_LOCAL.md](RUN_LOCAL.md)。

### 桌面端

```bash
uv run manyselves
```

---

## 2. 项目目录

| 目录 | 放什么 |
|---|---|
| `Inputs/` | 评估表格（Excel）、现场照片、专项文件等原始资料 |
| `Work/runs/<运行编号>/` | 该次运行的中间产物：准备记录、判断台账（`ledger/`）、草稿、审查记录、版本（`versions/`） |
| `Outputs/Reports/` | 交付的报告（`.md`、`.docx`）和「证据与来源索引」 |

第二章的小节编号和标题只从评估信息汇总表读；第四章「专项问题分析」只看 `Inputs/` 里有没有对应的专项文件。资料不足的地方报告照实写资料不足。

---

## 3. 报告操作

用户只需要在 Main 里说目标，由 Main 选择操作并调用 `run_reporting_workflow`。

| 你说什么 | Main 选择 |
|---|---|
| 用当前项目资料生成一份新报告 | `full_report` |
| 改已有报告的某节、某个模块或全文 | `revise_report` |
| 资料更新了，按新资料修订 | `revise_report` + `new_inputs=true` |
| 从报告模板重新提炼写作 Skill | `extract_skills` |
| 把提炼结果发布为现用 Skill | `publish_skills` |
| 把现有 Markdown 转成 Word | `render_existing` |
| 中断或失败的运行接着跑 | `resume_run`（不要用 `full_report` 或 `revise_report`） |

不需要用户填写内部 Schema，也不需要进入工作流页面。

---

## 4. 修订已有报告（revise_report）

- **改哪份：** 传那份报告的 `run_id`。没说清是哪份时 Main 会先问，不会自动选「最近一次」。
- **出在哪里：** 在同一个运行编号下产生下一个版本，上一版本完整保存在该运行的 `versions/` 里。
- **改什么：** `changes` 每项写三样：

| 字段 | 说明 |
|---|---|
| `target` | 第二章小节编号（如 `2.3.1`）、模块编号（如 `2.3`）、`1`、`3` 或 `全文`；按这份报告第二章实际的编号 |
| `instruction` | 修改要求，照用户原话 |
| `scope` | `text` 只改措辞；`judgment`（默认）重新判断 |

- **按新资料修订：** `new_inputs=true` 时程序重新冻结当前输入，比较事实的变化，只重开用到变化事实的模块。
- **不重做的部分：** 没受影响的判断、正文和第 1、3 章条目原样复用；判断行改动由程序带到所有引用它的地方，修订后程序对账并只核对改动的部分。

---

## 5. 提炼和发布写作 Skill

1. `extract_skills`：从报告模板重新提炼写作 Skill，产生一次新运行，结果只写在该运行目录下，不影响现用 Skill。
2. `publish_skills`（不带 `confirm`）：返回每个角色的变化——新增、修改（带差异）、未变、保留。Main 把要点告诉你。
3. 你明确同意后，Main 再用 `confirm=true` 发布。发布会替换现用 Skill，之后的新报告都用它。

---

## 6. 其他参数（一般不用改）

| 参数 | 默认 | 何时改 |
|---|---|---|
| `missing_evidence_policy` | `draft` | 你明确要求缺资料时暂停、阻断或跳过，改为 `ask` / `block` / `skip` |
| `output_filename` | 自动 | 你指定交付文件名时 |
| `user_supplements` | 空 | 你在对话里补充、确认了某个事实或要求时，作为本次运行可追溯的补充 |

---

## 7. Main 能自己决定什么

**Main 可以：**

- 选择上面的一种操作；
- 在只有一份候选报告时选用它的 `run_id`；
- 按你的说法设 `scope`、`new_inputs`；
- 使用默认参数。

**Main 不应：**

- 编造小节编号或专业结论；
- 在运行没有 `completed` 前说已交付；
- 未经你同意发布 Skill。

**需要你出现的时刻：**

- 要改的报告无法唯一确定；
- 发布 Skill 前确认；
- 你要特殊的缺资料策略或文件名。

---

## 8. 状态和输出

| 状态 | 含义 |
|---|---|
| `accepted` | 请求已受理，尚未开始 |
| `running` | 正在执行；以运行卡和运行页为准 |
| `waiting` | 等你输入 |
| `completed` | 已完成；有交付产物才算已交付，同时看是否带渲染警告 |
| `failed` | 失败；记下运行编号和错误，用 `resume_run` 在同一运行上接着跑 |

### DOCX 发布与验证

- 生成的文件无法被 Word/WPS 打开时，渲染失败，不发布成功结果。
- 文件能打开，但缺少批准的正文或标题、或仍含未解析的内容标记时，系统照样发布 DOCX，同时在渲染结果和日志里写 `validation_warnings`，并把 `protected_prose_verified` 设为 `false`。
- 带警告的 `completed` 只表示文件已交付，不表示内容或版式通过了专业验收。

---

## 9. 常见说法示例

```text
「用当前项目的资料生成完整的供配电评估报告」
→ full_report

「把上一份报告的 2.3.1 里定值保护的说法改清楚」
→ revise_report + changes=[{target: "2.3.1", instruction: "...", scope: "text"}]

「2.4 的判断不对，按现场记录重新判断」
→ revise_report + changes=[{target: "2.4", instruction: "...", scope: "judgment"}]

「Inputs 更新了，按新资料修订」
→ revise_report + new_inputs=true

「刚才那次运行断了，接着跑」
→ resume_run
```
