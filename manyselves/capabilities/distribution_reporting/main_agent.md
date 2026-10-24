## 配电报告（run_reporting_workflow）

每次调用选一个 `operation`：

1. `full_report`：从当前项目 `Inputs/` 的原始资料新生成一份完整报告（新运行）。用户说“从头生成”“出一份新报告”时用它。
2. `revise_report`：修改已有报告。传入那份报告的 `run_id`。它在同一个运行编号下产生下一个版本，上一版本完整保存在该运行的 `versions/` 里；没受影响的判断、正文和第 1、3 章条目原样复用，只重做受影响的部分。
   - `changes` 每项写 `target`（第二章小节编号如 `2.3.1`、模块编号如 `2.3`、`1`、`3` 或 `全文`）、`instruction`（用户的修改要求，照用户原话）、`scope`：只改措辞用 `text`，要重新判断用 `judgment`（默认）。小节编号按这份报告第二章实际的编号，不确定时先问用户，不要编造。
   - 用户更新了输入文件、要按新资料修订时，设 `new_inputs=true`；程序会重新冻结当前输入，比较事实的变化，只重开用到变化事实的模块。
   - 用户没有说清改哪份报告时，先确认 `run_id`。
3. `extract_skills`：从报告模板重新提炼写作 Skill（新运行，只写在该运行目录下，不影响现用 Skill）。
4. `publish_skills`：把某次提炼运行的结果发布为现用 Skill。先不带 `confirm` 调用一次，拿到每个角色的变化（新增、修改及差异、未变、保留），把要点告诉用户；用户明确同意后，再用 `confirm=true` 调用发布。发布会替换现用 Skill，之后的新报告都会用它。
5. `render_existing`：把用户指定的现有 Markdown（`source_markdown_ref`）渲染成 DOCX，不调用写作模型。

- `instruction` 保留用户真实要求，不扩写未经确认的专业结论。
- `missing_evidence_policy` 默认 `draft`；用户明确要求缺资料时暂停、阻断或跳过，才选 `ask`、`block` 或 `skip`。
- 报告运行中断或失败后要继续，用 `resume_run`，不要用 `revise_report` 或 `full_report`。
