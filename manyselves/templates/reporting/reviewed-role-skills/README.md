# 已上传报告参考资料的人工修订版

对应PR #6的S01/S06、Q01/Q03–Q07、F01/F02。此处14份方法与相邻 `reviewed-knowledge` 配套使用，修订依据见[36对象语义复核](../../../../docs/research/decisions/REPORTING_PR6_OBJECT_SEMANTIC_REVIEW.md)、[收资与房间复核](../../../../docs/research/decisions/REPORTING_PR6_MANAGEMENT_SEMANTIC_REVIEW.md)、[38单元处置](../../../../docs/research/decisions/REPORTING_PR6_KNOWLEDGE_REVIEW.md)和[原始材料审计](../../../../docs/research/decisions/REPORTING_PR6_UPLOADED_SOURCE_AUDIT.md)。

这是一套可放入项目Inputs的人工修订材料，不是重新蒸馏的模板，也不自动替代每个项目的自定义Skill。原模板DOCX未重新核验；文件中的来源定位指用户上传的旧Skill。知识库修订撤销已发现的矛盾或无条件断言，没有发布新的专业限值，38个专业单元均已核查其推断边界，但没有认证任何候选标准的现行性或条款适用。

在仓库根目录运行：

```bash
.venv/bin/python scripts/maintenance/prepare_reporting_references.py \
  --project /path/to/original-project \
  --output /path/to/new-reviewed-project
```

输入项目需包含本次上传版本的 `Inputs`、`Knowledge`。命令先核对要替换的知识单元及术语段落，再创建独立目录：

- 复制Inputs与Knowledge，采用14份修订Skill、11份写作规则，以及3份专业资料中的38个知识单元/术语/FAQ修订；15份Knowledge中客户问答边界经阅读后保留原文。
- 原始Skill包、原source.json及改动前Knowledge保存在 `review-lineage`，不进入新Knowledge索引。旧hash仅在原source.json中保留，不冒充新正文的hash；新来源明确标记人工修订。
- 原Excel字节不改，不复制Work、冻结Run、旧报告或配置，不调用模型。已存在的输出目录不会被覆盖。

新报告项目继续通过既有 `Inputs/report-template-role-skills` 加载，无需新运行时机制。旧Run继续读其冻结输入；仅把文件放到项目live Inputs不会修改已冻结Run。使用新项目或后续新输入快照时，才能采用本版材料。回退可使用原项目/原材料。

本次实际对上传材料生成独立副本，并由生产 `load_template_skill` 加载14份、KnowledgeContextBuilder建立15份来源；三表仍为25/208/83条。作者/审计、Chief与Final共用既有加载边界。14份正文总字符18,459→9,426，只表示重复格式及无依据要求减少，不能换算为token、费用或提速比例。

生产知识选择会随文本变化。真实加载发现部分原KU名称与当前报告小节不匹配；修订材料增加检索关联主题（保留原KU身份），本版实际内联38个修订单元，37个当前小节均找到候选来源。候选关联不是适用性认证，也不自动决定现场事实路由。PLC抗干扰归入其他物理环境相关参考，应急演练关联SOP/EOP；这些方法仅在当前任务确有相关对象时使用。

模块来源数为2/2/2/3/3，最大模块正文13,155字符；质量参考5份、4,835字符，包含结构、表图证据、风险、跨KU与质量核对。报告规则标题保留准确主题，避免重写后现有关键词选择漏掉相关规则。质量来源与字符数只说明当前输入构造，不证明模型召回或专业正确率。

2026-10-18交付包附25条收资、56条明确房间状态及7个占位符的复核与补充编辑稿。A-01按安全用具5间/操作工具6间分别保留对象；“目前没有”等模糊原评语不被写成确定管理缺失。原35个Inputs/Knowledge业务文件与上传ZIP逐字节一致（不含macOS元数据），原Excel仍25/208/83条事实。无新模型生成、无新增测试；实际提示遵循、费用、耗时及官方标准核验仍未验收。
