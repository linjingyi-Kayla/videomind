# Python MVP：Product Hunt AI 新品抓取 + Claude 打分 + Markdown 报告

这个小项目会：

- 用 BeautifulSoup 抓取 Product Hunt AI 相关页面（Next.js）
- 解析页面内嵌的 `__NEXT_DATA__` JSON，提取“新品/条目”
- 按关键词（如 `Vibe`, `No-code`, `Agentic`）过滤
- 调用 Claude 3.5 Sonnet 给每个工具打「Profitability Vibe」分数（1-10）
- 生成一个简单的 Markdown 报告

## 目录

- 入口脚本：`vibe_coding_report.py`
- 依赖：`requirements.txt`
- 环境变量示例：`.env.example`

## 运行环境

- Python 3.10+（建议 3.11/3.12）

## 安装依赖

```bash
pip install -r requirements.txt
```

## 配置 API Key

设置环境变量 `ANTHROPIC_API_KEY`（或 `CLAUDE_API_KEY`）。

Windows PowerShell 示例：

```powershell
$env:ANTHROPIC_API_KEY="你的key"
```

可选：覆盖默认模型（默认 `claude-3-5-sonnet-latest`）：

```powershell
$env:CLAUDE_MODEL="claude-3-5-sonnet-latest"
```

## 生成报告

最简单：

```bash
python vibe_coding_report.py
```

自定义关键词、限制数量、输出文件：

```bash
python vibe_coding_report.py --keywords "vibe,no-code,agentic" --limit 20 --out report.md
```

指定一个更具体的页面 URL（例如某个 AI 类别页/话题页，并使用其“Recent/New”排序参数）：

```bash
python vibe_coding_report.py --url "https://www.producthunt.com/topics/artificial-intelligence"
```

## 常见问题

### 抓不到数据/报错找不到 __NEXT_DATA__

Product Hunt 可能会：

- 调整页面结构
- 对未登录用户/机器人请求做限制

你可以尝试：

- 换一个更具体的 URL（例如 categories/topics 的不同入口）
- 稍后重试

### 评分依据是什么？

脚本只把 `name/tagline/url` 交给 Claude，因此评分是“快速直觉版”，适合做 MVP 的初筛，不代表严谨的商业尽调。
