# Equity Workup Skill 实施计划

## 一 最终目标

从零实现一个项目级 Skill，使 Codex 或 OpenCode 能根据固定 Word 模板完成 A 股上市公司公开信息调研，并生成可追溯、可复核的 Word 报告。

用户每次必须人工提供：

- 一家被调查公司；
- 两家竞品公司；
- 可选的调研截止日期，未提供时使用运行日。

Skill 只能解析和确认这三家公司的身份，不得发现、推荐、排名、替换或补充竞品。

## 二 已确认的实现边界

1. 不使用 `main` 分支的任何代码、配置、测试、输出、依赖设计或程序架构。
2. 允许使用的项目依据只有：
   - `README.md` 中描述的业务目标和业务口径；
   - `Workup_template_260617-外测版.docx` 的内容、结构和版式。
3. 不开发新的 Agent 框架、Planner、LangGraph、任务队列、调度系统或多 Agent Runtime。
4. Codex 或 OpenCode 本身负责理解 Skill、制定当次计划、执行调研、检查缺口和决定重试顺序。
5. 可以编写少量全新脚本，但脚本只负责必须确定执行的机械工作，不负责 Agent 编排。
6. 第一版只支持目标公司和两家竞品均为 A 股上市公司。
7. 报告继续使用现有 Word 模板作为视觉和内容底座。

## 三 Skill 结构

Skill 放置在以下项目目录，使 Codex 和 OpenCode 共用：

```text
.agents/skills/equity-workup/
├── SKILL.md
├── references/
│   ├── report-contract.md
│   ├── evidence-policy.md
│   └── research-playbook.md
└── scripts/
    ├── calculate_metrics.py
    ├── write_report.py
    └── audit_report.py
```

不得增加没有明确用途的框架层、抽象层或脚本。

## 四 Skill 职责

`SKILL.md` 应规定：

1. 校验一家目标公司和两家竞品均已提供且互不重复。
2. 根据 Word 模板建立待调查字段清单。
3. 优先收集可复用的年报、半年报、公告、监管记录和公司官方资料。
4. 根据实际证据情况自行安排调研顺序。
5. 每完成一个字段立即保存证据，不得最后补造来源。
6. 第一轮结束后检查未完成字段，仅对存在明确替代来源或替代提取方法的字段重试。
7. 每个字段最多尝试两种有效证据路径；完整重试一轮没有新增结果时停止。
8. 调用确定性脚本完成财务计算、图表、Word 写入和最终审计。
9. 渲染最终 Word，并逐页检查排版后交付。

Skill 不要求宿主支持子 Agent。宿主支持并行时可以并行调研；不支持时必须能够顺序完成。

## 五 自动填写范围

Skill 应调查并尽可能填写：

- 公司概况、国企属性、上市信息、市值和官方链接；
- 上市子公司、董事外部任职、并购事项和业务描述；
- 美国子公司、美国收入和美国员工敞口；
- 收入结构、经营趋势、客户供应商集中度、业务展望和风险；
- 关联交易；
- 目标公司与两家人工指定竞品的财务和股价比较；
- 流动性、偿债、资本开支、现金流和应收账款分析；
- IPO、股价、52 周高低、指数对比、异常下跌和证券发行；
- 公司治理、管理层、主要股东和员工结构；
- 审计意见、审计师变化、财务重述和重大经营变化；
- 诉讼、监管处罚和过去 12 个月的中英文负面新闻；
- 两张股价图：目标公司单独走势图，以及目标公司与两家竞品的归一化对比图。

## 六 必须保留人工填写的范围

以下内容不得根据公开信息自动生成：

- Underwriter、Branch、Producer、Commission；
- Reason for Referral、Date Approval Required；
- Overall Relationship with the Company；
- Brief of Competition；
- Clearance Obtained；
- New Business or Renewal、Written Since、Premium Earned、Claim History；
- Recommendation、Recommended D&O、Recommended POSI；
- Subjectivities；
- Rated Premium 和相关定价理由；
- Sign-off 和 Date。

这些字段涉及内部核保信息或人工决策，应在最终 Word 中保持原有人工填写位置。

## 七 Word 模板处理结论

1. 原始 Word 模板必须保持不变。
2. 实现时创建一个工作模板副本。
3. 工作模板保持原来的页面、样式、字体、表格、页眉和页脚。
4. 将模板中的三家同行调整为两家人工指定竞品：
   - 比较表只保留目标公司、竞品一、竞品二；
   - 同行股价图只包含目标公司和两家竞品；
   - 所有文字说明中的同行数量从三家改为两家。
5. 为自动填写位置增加稳定、不可见的标签或内容控件，避免依赖易变化的段落序号。
6. 只有证据状态合格的研究字段可以覆盖模板占位内容。

## 八 证据要求

每个自动填写字段至少保存：

```text
field_id
status
value
period
source_url
source_title
source_type
published_at
captured_at
page_or_section
evidence_quote
calculation
```

字段状态只允许：

- `supported`：来源直接支持；
- `derived`：由有证据的输入确定计算；
- `not_disclosed`：已经检查相关正式披露范围，但没有披露；
- `not_applicable`：有证据证明不适用；
- `unresolved`：证据缺失、冲突、不可访问或无法验证。

`unresolved` 不得写入结论，只能进入缺口清单。没有搜索结果不能作为 `No` 的证据。大模型输出不能作为事实来源。

## 九 允许的少量新脚本

### calculate_metrics.py

只负责确定性计算，包括财务比率、增长率、股价收益、回撤、同行中位数和图表输入。不得访问大模型，不得决定竞品。

### write_report.py

只负责把已验证字段和图片写入工作模板副本。不得自行生成研究结论，不得修改人工填写区域。

### audit_report.py

只负责检查字段状态、证据完整性、公司身份、竞品一致性、输出文件和 Word 写入结果，并调用可用的渲染工具完成逐页验收准备。

如果宿主已有可靠的下载、浏览、PDF 阅读或 Word 渲染能力，直接使用宿主能力，不重复开发相同工具。

## 十 输出文件

每次运行至少产生：

```text
result.docx
evidence.json
gaps.json
run-plan.json
standalone-stock-chart.png
competitor-comparison-chart.png
```

负面新闻需要附件时，另生成对应附件文件，但不得复制无法合法获取的全文。

## 十一 验收标准

实现完成必须满足：

1. Codex 和 OpenCode 都能发现并读取该 Skill。
2. 输入必须是一个目标公司和两家人工指定竞品。
3. Agent 不会自动选择或替换竞品。
4. 所有已填写研究字段都有完整证据或计算来源。
5. 所有未完成字段都有明确原因和尝试记录。
6. 人工核保区域没有被自动填写。
7. 原始 Word 模板未被修改。
8. 最终 Word 使用两家竞品，且不存在第三家竞品残留。
9. 最终 Word 可以正常打开，并已逐页渲染检查，无文字裁切、重叠、断表或图片错位。
10. 项目中不存在对 `main` 分支旧代码的引用、复制、导入或运行依赖。

## 十二 明确不做

- 不开发新的 Agent 框架或工作流引擎；
- 不自动发现竞品；
- 不支持非 A 股公司；
- 不自动填写内部核保意见和定价；
- 不绕过网站访问控制；
- 不为了提高填写率而编造、推断或弱化证据标准；
- 不复用 `main` 分支的任何实现。
