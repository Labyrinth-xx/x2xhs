# x2xhs 架构说明

## 项目概述

把 X（Twitter）帖子自动翻译并推送到 Telegram，最终发布到小红书的自动化 pipeline。

## 完整数据流

```
Twitter / RSSHub
      │
      ▼
 scraper 层                账号推文 → RSSHubClient（RSS 解析）
                           关键词推文 → TwscrapeClient（Playwright 拦截 GraphQL API）
      │
      ▼
 storage 层                SQLite WAL → tweets 表（原始推文）
      │
      ▼
 processor 层              ClaudeTranslator（OpenRouter）→ 中文标题/正文/tags
                           ContentFormatter → 清洗 URL / @mentions，归一化 tags
      │
      ▼
 publisher 层              TweetScreenshotter（Playwright 截图）
                           ImageDownloader（原始推文附图）
                           TweetImageOverlayer（PIL 拼接中文翻译卡片）
                           TelegramNotifier → 发送图文消息
      │
      ▼
 小红书                    人工确认后，通过 MCP publish_content 工具发布
```

## 目录结构

```
x2xhs/
├── config.py              # 环境变量加载、配置校验、路径收敛
├── pipeline.py            # 串联所有阶段的主控类
├── main.py                # argparse + rich CLI 入口
├── bot.py                 # python-telegram-bot 定时任务 + 指令处理
├── bot_intent.py          # Telegram 指令意图解析（Claude 驱动）
├── scraper/
│   ├── models.py          # RawTweet / ProcessedContent 数据模型
│   ├── rsshub_client.py   # RSSHub RSS 抓取（账号推文）
│   ├── twscrape_client.py # Playwright 关键词搜索（拦截 SearchTimeline GraphQL）
│   ├── image_downloader.py# 推文附图下载
│   └── tweet_screenshotter.py # Playwright 推文截图
├── processor/
│   ├── translator.py      # OpenRouter 翻译（Claude / Gemini / DeepSeek）
│   └── content_formatter.py   # 清洗 URL、@mentions，格式化 tags
├── publisher/
│   ├── image_overlay.py   # PIL 拼接中文翻译卡片
│   ├── telegram_notifier.py   # Telegram Bot 消息发送
│   └── utils.py           # 共享工具函数
└── storage/
    ├── database.py        # SQLite 连接管理、schema 初始化、WAL
    └── tweet_repo.py      # 仓储接口（CRUD + 状态流转）
```

## 核心模块说明

### scraper/twscrape_client.py — 关键词搜索

Twitter 的 SearchTimeline GraphQL API 要求 `x-client-transaction-id` 头，
该头由 Twitter 的 JS bundle 动态生成，Python 直接发 HTTP 请求无法伪造。

解决方案：启动 Playwright Chromium，注入浏览器 Cookie（`auth_token` + `ct0`），
导航到 `x.com/search?q=...`，监听 `SearchTimeline` 响应并解析 JSON。
浏览器自动生成所有必要头，完全绕过 anti-bot 机制。

每次关键词搜索约 7 秒（Chromium 启动 + 页面加载）。Cookie 过期后需手动更新 `.env`。

### processor/content_formatter.py — 内容清洗

双层防护，避免内容被小红书标记为「引流」：
1. **LLM prompt 层**：system prompt 明确要求不输出 URL 和 @提及
2. **后处理层**：正则兜底，清除 `https?://\S+` 和 `@\w+`，并整理多余空行

### publisher/image_overlay.py — 翻译卡片

在推文截图底部拼接中文翻译卡片，模拟 X App 原生翻译效果：
- 调用 `translate_literal_parts()` 直译（Gemini Flash，速度快）
- PIL 绘制卡片：动态检测截图宽度、自适应亮/暗模式背景色
- 支持主推文 + 引用推文双卡片

## 数据库

```sql
tweets              -- 原始推文（external_id, handle, content, image_urls...）
processed_content   -- 中文稿件（title_zh, body_zh, tags, status, pushed_at...）
monitored_accounts  -- 监控账号列表
monitored_keywords  -- 关键词列表
scrape_log          -- 抓取历史记录
```

### 状态流转

```
drafted → approved → published → pushed
```
- `drafted`：已翻译，待人工审核
- `approved`：审核通过，可截图发送
- `published`：已导出待发布 JSON
- `pushed`：已通过 MCP 发布到小红书

## CLI 常用命令

```bash
python main.py scrape --rsshub          # 抓取账号 + 关键词推文
python main.py process --limit 5        # 翻译并生成草稿
python main.py deliver --limit 1        # 翻译 + 截图 + 发 Telegram（一步完成）
python main.py status                   # 查看各状态数量
python main.py add-keyword "AI agents"  # 添加关键词
python main.py list-keywords            # 查看关键词列表
```

## 环境依赖

| 依赖 | 用途 |
|------|------|
| playwright | 推文截图 + 关键词搜索 |
| httpx | RSSHub RSS 抓取 |
| openai | OpenRouter API 客户端 |
| python-telegram-bot | Telegram Bot |
| Pillow | 翻译卡片图片处理 |
| aiosqlite | 异步 SQLite |
| feedparser | RSS 解析 |
| Jinja2 | HTML 预览模板 |
| rich | CLI 美化输出 |
