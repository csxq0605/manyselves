# 已上传报告参考资料的人工修订版

对应PR #6的S01/S06、Q01/Q03–Q07、F01/F02。此处14份方法与相邻 `reviewed-knowledge` 配套使用，修订依据见[36对象语义复核](../../../../docs/research/decisions/REPORTING_PR6_OBJECT_SEMANTIC_REVIEW.md)和[原始材料审计](../../../../docs/research/decisions/REPORTING_PR6_UPLOADED_SOURCE_AUDIT.md)。

这是一套可放入项目Inputs的人工修订材料，不是重新蒸馏的模板，也不自动替代每个项目的自定义Skill。原模板DOCX未重新核验；文件中的来源定位指用户上传的旧Skill。知识库修订撤销已发现的矛盾或无条件断言，没有发布新的专业限值，也没有认证其余条款。

在仓库根目录运行：

```bash
.venv/bin/python scripts/maintenance/prepare_reporting_references.py \
  --project /path/to/original-project \
  --output /path/to/new-reviewed-project
```

输入项目需包含本次上传版本的 `Inputs`、`Knowledge`。命令先核对要替换的知识单元及术语段落，再创建独立目录：

- 复制Inputs与Knowledge，替换14份Skill、7份写作规则，以及2份专业资料中的8个知识单元/术语定义。
- 原始Skill包、原source.json及改动前Knowledge保存在 `review-lineage`，不进入新Knowledge索引。旧hash仅在原source.json中保留，不冒充新正文的hash；新来源明确标记人工修订。
- 原Excel字节不改，不复制Work、冻结Run、旧报告或配置，不调用模型。已存在的输出目录不会被覆盖。

新报告项目继续通过既有 `Inputs/report-template-role-skills` 加载，无需新运行时机制。旧Run继续读其冻结输入；仅把文件放到项目live Inputs不会修改已冻结Run。使用新项目或后续新输入快照时，才能采用本版材料。回退可使用原项目/原材料。

本次实际对上传材料生成独立副本，并由生产 `load_template_skill` 加载14份、KnowledgeContextBuilder建立15份来源；三表仍为25/208/83条。作者/审计、Chief与Final共用既有加载边界。14份正文总字符18,459→9,138，只表示重复格式及无依据要求减少，不能换算为token、费用或提速比例。

生产知识选择会随文本变化：本次模块来源数仍为2/2/2/3/3，质量参考从5变4，最大模块正文11,971字符、质量正文13,971字符。候选数量变化不是召回质量提升；剩余未审条款、官方标准状态及新模型成稿仍未验收。
