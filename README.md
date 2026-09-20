# Equity Workup Agent

面向 A 股上市公司的**证据驱动型研究报告 Skill**。Codex / OpenCode 读取项目级 Skill，
按固定 Word 模板完成公开信息调研，生成可追溯、可复核的 Word 报告。

> 本分支 `codex/greenfield-equity-workup` 是与 `main` 完全独立的绿地实现：
> 不使用、不导入、不依赖 `main` 的代码、配置、测试、输出或架构；
> 不包含 LangGraph、Planner、调度器、任务队列或多 Agent Runtime。
> 规划、调研、查缺和重试顺序由宿主 Agent 负责，脚本只做机械、可复现的工作。

## 它是什么

- 一个 **Skill**：`.agents/skills/equity-workup/`，Codex 与 OpenCode 均可发现。
- 一份 **契约**：字段清单、模板槽位、证据 schema、输出清单。
- 一份 **证据策略**：什么算证据、字段状态、固定业务口径。
- 一份 **调研手册**：一次运行的标准步骤与重试纪律。
- 三个 **确定性脚本**：财务/行情计算与图表、Word 写入、报告验收与渲染。

## 目录结构

```text
.agents/skills/equity-workup/
├── SKILL.md
├── references/
│   ├── report-contract.md      # 104 个字段 id、模板槽位、证据 schema、输出
│   ├── evidence-policy.md      # 证据标准、状态、固定业务口径
│   └── research-playbook.md    # 端到端调研步骤与重试规则
├── scripts/
│   ├── calculate_metrics.py    # 比率、增长、回撤、同行中位数、两张股价图
│   ├── write_report.py         # prepare 工作模板 / write 已验证字段
│   └── audit_report.py         # 证据、身份、人工区、哈希、渲染逐页校验
└── tests/                      # pytest 测试
```

## 输入

每次运行需要：

- **1 家目标 A 股上市公司**；
- **2 家竞品 A 股上市公司**（默认由用户人工指定；仅在用户明确授权并记录
  `peer_selection.mode = "user_authorized_auto"` 及 `authorization` / `rationale`
  时才允许 Agent 自动选择）；
- 可选的调研截止日期，未提供时使用运行日。

Skill 只解析和确认这三家公司的身份，不会在未授权时发现、推荐、排名、替换或补充竞品。

## 工作流程

```text
write_report.py prepare     # 复制原模板、3 家同行→2 家竞品、加稳定书签、记录原模板哈希
   ↓
宿主 Agent 调研             # 用宿主自带的搜索 / 下载 / PDF / Word 能力，逐字段立即存证
   ↓
calculate_metrics.py        # 确定性计算与两张图
   ↓
write_report.py write       # 只写入合格字段，人工核保区永不写入
   ↓
audit_report.py --render    # 验收 + 逐页渲染
```

## 输出

每次运行至少产生：

```text
result.docx                       # 最终报告
working-template.docx             # 工作模板副本
template-manifest.json            # 原模板哈希、工作模板哈希、书签映射
evidence.json                     # 字段级证据
gaps.json                         # 未完成字段及原因、尝试记录
run-plan.json                     # 三家身份、截止日、peer_selection
metrics.json                      # 确定性计算结果
standalone-stock-chart.png        # 目标公司股价图
competitor-comparison-chart.png   # 目标 + 两家竞品归一化对比图
audit.json                        # 验收结果
renders/page-N.png                # 逐页渲染，供人工复核排版
```

## 证据模型

字段状态只允许：`supported`、`derived`、`not_disclosed`、`not_applicable`、
`unresolved`。只有前四种会写入报告；`unresolved` 只进入 `gaps.json`，绝不伪装成
`No` / `Not disclosed`。没有搜索结果不能作为 `No` 的证据，大模型输出不能作为事实来源。

## 人工核保区（永不自动填写）

`Underwriter`、`Branch`、`Producer`、`Commission`、`Reason for Referral`、
`Date Approval Required`、`Overall Relationship`、`Brief of Competition`、
`Clearance Obtained`、`New Business or Renewal`、`Written Since`、`Premium Earned`、
`Claim History`、`Recommendation`、`Recommended D&O`、`Recommended POSI`、
`Subjectivities`、`Rated Premium` 及定价理由、`Sign-off`、`Date`。
`write_report.py` 硬拒绝这些字段，`audit_report.py` 逐表比对原文证明未被改动。

## 安装

```bash
python3.13 -m venv .venv
.venv/bin/pip install -e '.[dev]'
# 或
uv venv && uv pip install -e '.[dev]'
```

依赖：`python-docx`、`matplotlib`（渲染另需 `pymupdf`；渲染引擎用 LibreOffice 或
Microsoft Word）。**请勿提交 `.env` 等本地模型配置，已在 `.gitignore` 中忽略。**

## 运行

```bash
python .agents/skills/equity-workup/scripts/write_report.py prepare \
  --template "Workup_template_260617-外测版.docx" \
  --out <run-dir>/working-template.docx \
  --manifest <run-dir>/template-manifest.json

python .agents/skills/equity-workup/scripts/calculate_metrics.py \
  --input <run-dir>/metrics-input.json \
  --output <run-dir>/metrics.json \
  --chart-dir <run-dir>

python .agents/skills/equity-workup/scripts/write_report.py write \
  --working <run-dir>/working-template.docx \
  --evidence <run-dir>/evidence.json \
  --out <run-dir>/result.docx \
  --charts <run-dir>

python .agents/skills/equity-workup/scripts/audit_report.py \
  --run <run-dir> \
  --template "Workup_template_260617-外测版.docx" \
  --render
```

## 测试与校验

```bash
.venv/bin/ruff check .
.venv/bin/python -m pytest -o addopts="" -q
```

## 边界

- 只支持 A 股上市公司；不自动发现竞品（除非用户明确授权并留痕）。
- 不开发新的 Agent 框架 / 工作流引擎；不绕过网站访问控制。
- 不自动填写内部核保意见、推荐与定价。
- 不为了提高填写率而编造、推断或弱化证据标准。
- 原始 Word 模板在整个过程中保持不变，所有写入都在工作模板副本上进行。

## 风险提示与责任声明

本项目仅用于技术研究、学习和公开信息整理，不构成投资建议、法律意见或任何形式的
专业承诺。项目会访问第三方网站并采集公开数据，使用者应自行遵守适用的法律法规及
第三方规则，不得绕过登录验证、验证码、访问控制或反爬措施，也不得采集、处理或传播
无合法依据的个人信息或受保护内容。开发者不对数据的准确性、完整性、时效性作任何
保证，也不对使用本项目造成的损失承担责任；所有结论均应结合原始披露文件人工复核。
