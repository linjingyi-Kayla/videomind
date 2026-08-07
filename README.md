# VideoMind

把 YouTube 视频一键变成可读总结与复习提醒的 PWA。

支持：

- 登录后按用户隔离任务
- 粘贴 / 一键读取剪贴板导入视频链接
- RapidAPI 拉取字幕 + DeepSeek 生成总结与分类
- Web Push / 站内提醒（iOS 建议「添加到主屏幕」）
- PWA Web Share Target（Android / 部分浏览器分享菜单更完整；iPhone 更推荐剪贴板导入）

## 技术栈

- **后端**：FastAPI + SQLAlchemy（本地 SQLite / 生产可接 Postgres）
- **前端**：静态 HTML/JS PWA（`static/` + `manifest.json` + `service-worker.js`）
- **AI**：DeepSeek（OpenAI 兼容接口）
- **字幕**：RapidAPI `youtube-transcript3`

## 快速开始（本地）

### 1. 环境

- Python 3.12（仓库含 `.python-version` / `mise.toml`，Railway 构建可用）

### 2. 安装依赖

```bash
pip install -r requirements.txt
```

### 3. 配置密钥

复制示例文件，填入你自己的 Key（**不要提交 `.env`**）：

```bash
cp .env.example .env
```

最少需要：

| 变量 | 用途 |
|------|------|
| `DEEPSEEK_API_KEY` | 总结 / 分类 |
| `RAPIDAPI_KEY` | YouTube 字幕 |
| `VAPID_PUBLIC_KEY` / `VAPID_PRIVATE_KEY` | Web Push（可选，不配仍可用站内提醒） |

更多变量说明见 `.env.example`。

### 4. 启动

```bash
uvicorn main:app --host 0.0.0.0 --port 8000 --reload
```

浏览器打开：`http://127.0.0.1:8000`

默认账号流程：打开站点 → 注册 / 登录 → 首页粘贴 YouTube 链接导入。

## 主要接口（摘要）

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/api/register` | 注册 |
| `POST` | `/api/login` | 登录（返回 JWT，并写 HttpOnly Cookie） |
| `POST` | `/api/logout` | 退出并清 Cookie |
| `GET` | `/api/me` | 当前用户 |
| `POST` | `/api/summarize` | 创建总结任务（需登录） |
| `GET` | `/api/history` | 任务列表 |
| `GET` | `/api/share-target` | Web Share Target：解析链接后建任务并重定向首页 |
| `POST` | `/api/share-target` | 兼容旧分享 / 表单导入 |

## 部署（Railway）

1. 连接本仓库，启动命令见 `Procfile`：`uvicorn main:app --host 0.0.0.0 --port $PORT`
2. 在 Railway **Variables** 中配置与 `.env.example` 对应的密钥（**不要**把真实 `.env` 推上 GitHub）
3. 生产 HTTPS 建议设置：`COOKIE_SECURE=1`
4. 若构建时报 Python GitHub attestation 相关错误，仓库已含 `mise.toml`（`python.github_attestations = false`）；也可在服务变量中设置 `MISE_PYTHON_GITHUB_ATTESTATIONS=false`

## 安全说明

- `.env` 已加入 `.gitignore`，**请勿**再把真实 API Key / VAPID 私钥提交到 Git。
- 若历史上曾把 `.env` 推到公开仓库，请立刻在 DeepSeek、RapidAPI 等平台**轮换（作废并重建）密钥**，并在 Railway 更新新 Key。仅从最新提交删掉 `.env` **无法**清除旧 commit 里的泄露内容。

## 目录结构（简要）

```
main.py                 # FastAPI 入口
videomind/              # 鉴权、DB、抽取字幕、AI、推送
static/                 # 前端页面与静态资源
manifest.json           # PWA / share_target
service-worker.js
requirements.txt
.env.example            # 环境变量模板（无真实密钥）
```

## License

仅供个人 / 学习项目使用；请自行遵守 YouTube、RapidAPI、DeepSeek 等服务条款。
