# x2xhs

把 X（Twitter）帖子自动翻译成中文，推送到 Telegram，发布到小红书。

## 功能

- **自动抓取**：监控指定账号（via RSSHub）+ 关键词热门推文（via Playwright）
- **AI 翻译**：OpenRouter 接入 Claude / Gemini / DeepSeek，重写为小红书风格中文
- **截图 + 翻译卡片**：Playwright 截取推文原图，PIL 在底部拼接中文翻译
- **Telegram 推送**：自动发送图文消息，定时任务或手动触发
- **小红书发布**：通过 MCP 工具一键发布，自动标记状态

## 快速开始

### 1. 环境准备

```bash
git clone https://github.com/your-username/x2xhs.git
cd x2xhs
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

### 2. 配置环境变量

```bash
cp .env.example .env
# 编辑 .env，填入各项 API Key 和 Cookie
```

必填项：
- `OPENROUTER_API_KEY` — [OpenRouter](https://openrouter.ai) API Key
- `RSSHUB_BASE_URL` — RSSHub 地址（本地：`docker run -p 1200:1200 diygod/rsshub`）
- `TELEGRAM_BOT_TOKEN` + `TELEGRAM_CHAT_ID` — Telegram Bot
- `TWSCRAPE_AUTH_TOKEN` + `TWSCRAPE_CT0` — Twitter Cookie（关键词搜索用）
- `TWITTER_AUTH_TOKEN` — Twitter Cookie（推文截图用）

### 3. 初始化数据库

```bash
python main.py setup
```

### 4. 添加监控目标

```bash
python main.py add-account claudeai
python main.py add-keyword "AI agents"
```

### 5. 抓取并发送

```bash
# 一步完成：抓取 → 翻译 → 截图 → 发 Telegram
python main.py deliver --limit 3 --scrape

# 或分步执行
python main.py scrape --rsshub
python main.py process --limit 5
```

## CLI 命令

| 命令 | 说明 |
|------|------|
| `setup` | 初始化数据库 |
| `scrape --rsshub` | 抓取账号推文 + 关键词推文 |
| `process [--limit N]` | 翻译未处理推文，生成中文稿件 |
| `deliver [--limit N]` | 翻译 + 截图 + Telegram 发送 |
| `status` | 查看各状态数量统计 |
| `add-account <handle>` | 添加监控账号 |
| `remove-account <handle>` | 移除监控账号 |
| `list-accounts` | 查看监控账号列表 |
| `add-keyword <词>` | 添加关键词 |
| `remove-keyword <词>` | 移除关键词 |
| `list-keywords` | 查看关键词列表 |
| `drain` | 清空 new 状态队列 |

## Telegram Bot 指令

启动 bot：`python bot.py`

| 指令 | 说明 |
|------|------|
| `/start` | 查看帮助 |
| `/status` | 查看状态统计 |
| `/deliver` | 手动触发发送 |
| `/scrape` | 手动触发抓取 |
| `/add_account <handle>` | 添加监控账号 |
| `/add_keyword <词>` | 添加关键词 |
| `/list` | 查看账号和关键词 |

## 架构

详见 [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)。

## 技术栈

- Python 3.12+，全异步（asyncio）
- Playwright — 推文截图 + 关键词搜索
- OpenRouter — AI 翻译（支持 Claude / Gemini / DeepSeek）
- SQLite WAL — 本地数据存储
- Pillow — 翻译卡片图片处理
- python-telegram-bot — Telegram Bot

## 注意事项

- Twitter Cookie（`auth_token` / `ct0`）有效期约数周至数月，过期后需手动更新 `.env`
- 关键词搜索使用 Playwright 启动真实 Chromium，每个关键词约 7 秒
- 翻译内容自动清除 URL 和 @提及，符合小红书平台规范
