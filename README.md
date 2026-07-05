# Equity Workup Agent

Equity Workup Agent 是一个面向 A 股上市公司的证据驱动型研究报告生成系统。用户输入公司名称或股票代码后，系统会采集公开数据、解析定期报告与公告、执行财务及市场分析，并将经过验证的结果写入固定 DOCX 模板。

项目基于 LangGraph 编排完整工作流。每个成功字段都会记录数据来源和证据；无法可靠生成的字段会保留模板原文，并在失败清单中说明原因。

## 核心能力

- 对接东方财富、雪球、巨潮资讯和公司官网等公开数据源。
- 将模板待填内容抽象为字段配置，统一描述 `field_id`、数据来源、模板位置、写入策略和输出格式。
- 编排 11 个业务分析模块，覆盖公司属性、海外敞口、经营表现、同行比较、财务分析、市场表现、公司治理及风险事件。
- 将确定性计算与大模型提取分离：财务比率和行情指标由代码计算，大模型仅基于已采集证据执行结构化提取或摘要。
- 按字段级来源优先级合并候选值，并保留来源冲突、失败原因和原始证据。
- 支持向 DOCX 段落、表格、超链接和图片锚点写入结果。
- 生成字段结果、证据链、失败记录、执行计划、日志和图表等审计产物。

## 工作流

```text
initialize_run
  -> load_template_mapping
  -> create_execution_plan
  -> confirm_plan
  -> resolve_company
  -> fetch_eastmoney / fetch_xueqiu / fetch_official_site
  -> shared_disclosures
  -> part_01 ... part_11
  -> merge_fields
  -> write_docx
  -> generate_evidence_files
  -> self_check
  -> finalize_run
```

巨潮资讯公告和报告只采集、下载、解析一次，随后由不同业务模块复用，避免重复请求和重复解析。

## 业务模块

| 模块 | 主要职责 |
| --- | --- |
| Part 01 | 国企属性、上市子公司、外部任职及并购信息 |
| Part 02 | Google Finance、巨潮资讯等公司链接生成与验证 |
| Part 03 | 美国子公司、收入、员工及其他美国业务敞口 |
| Part 04 | 收入拆分、季度经营趋势、客户供应商集中度、业务展望与风险 |
| Part 05 | 关联交易、固定同行选择、同行财务指标比较 |
| Part 06 | 流动比率、速动比率、资本开支、偿债能力和现金流分析 |
| Part 07 | 应收账款增长、坏账减值和无形资产风险分析 |
| Part 08 | IPO、指数及同行对比、异常股价下跌和证券发行分析 |
| Part 09 | 公司股价图和同行归一化表现图生成 |
| Part 10 | 高管、董事会、主要股东及全球员工结构分析 |
| Part 11 | 审计意见、财务重述、重大变化、诉讼监管和负面新闻分析 |

## 字段与证据模型

模板字段定义位于 `configs/fixed_template_mapping.yaml` 和 `configs/fields/part_XX.yaml`，数据来源优先级位于 `configs/source_priority_policy.yaml`。

字段处理遵循以下原则：

1. 业务模块生成带来源、期间和原始证据的候选值。
2. 合并阶段按照字段级来源优先级选择最终结果。
3. 多个来源结果不一致时保留冲突记录。
4. 字段缺少可靠证据或未通过校验时，不推断、不补造，并写入 `failed_fields.json`。
5. 自检阶段验证字段覆盖、证据完整性、模板写入结果和输出文件有效性。

## 技术栈

- Python 3.13
- LangGraph 1.2.7
- Pydantic 2.13.4
- OpenAI SDK 2.44.0（接入 DeepSeek）
- HTTPX、Beautiful Soup、pdfplumber
- python-docx、Matplotlib
- Pytest

完整版本约束见 `pyproject.toml`。

## 安装

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
cp .env.example .env
```

在 `.env` 中配置：

```dotenv
DEEPSEEK_API_KEY=
```

`.env` 仅用于本地运行，不应提交到版本控制。

## 运行

```bash
.venv/bin/python -m mlc_agent run \
  --template './Workup_template_260617-外测版.docx' \
  --company '000938' \
  --output './outputs' \
  --yes
```

`--company` 支持无歧义的 A 股公司名称或六位股票代码。移除 `--yes` 后，CLI 会展示执行计划并等待确认。

## 输出

每次运行会在 `outputs/` 下创建独立目录，主要包含：

- `result.docx`：最终报告。
- `extracted_data.json`：运行状态及结构化结果。
- `sources.json`：字段级来源与证据记录。
- `failed_fields.json`：未完成字段及失败原因。
- `execution_plan.json`：工作流执行状态。
- `run.log`：运行日志。
- `standalone_stock_chart.png`：公司股价走势图。
- `peer_comparison_chart.png`：同行归一化股价对比图。
- `negative_news_articles.docx`：存在通过验证的负面新闻时生成的来源材料。

## 测试

```bash
.venv/bin/pytest
```

测试覆盖公司解析、模板映射、来源优先级、财务计算、公告证据提取、同行分析、市场历史、图表生成、失败隔离以及完整 LangGraph 工作流。

## 当前边界

- 当前版本面向项目内固定 DOCX 模板，不会运行时解析任意 Word 文档并自动生成字段定义。
- 当前只支持无歧义的 A 股上市公司。
- Part 05 从 `configs/fixed_peers.yaml` 读取目标公司预设的三家同行；未配置时明确失败。
- 雪球行情不可用时，由字段来源优先级决定是否采用东方财富候选值。
- DeepSeek 仅处理已经抓取并携带来源信息的证据，不作为原始事实来源。

## 免责声明

本项目仅用于技术研究和公开信息整理，不构成投资建议。数据准确性和完整性仍需结合原始披露文件人工复核。
