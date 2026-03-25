# x2xhs 重构方案

## Context

当前 pipeline 有 5 个状态（drafted → approved → published → pushed → rejected）和 6 步手动流程，导致：
1. **Bot 文不对题**：79 条积压 `published` 内容按 `updated_at DESC` 堵在队首，说"维斯塔潘"给的却是 sama/naval
2. **流程繁琐**：用户只需要"处理好直接发给我"，不需要中间审批状态
3. **没有实时推送**：全靠手动触发，无定时自动抓取

目标：简化为 2 状态 + 1 步流程，Bot 每 60 分钟自动抓取并推送新内容。

**数据库保留**：用于推文去重（避免重复发送）和记录发送历史。

**其他已确认需求**：
- 只推送原创推文（过滤 RT 转推和 @回复）
- 定时任务失败时 Telegram 发送具体错误原因
- 监控账号统一由 Bot 管理（数据库），不再依赖 `.env` 的 `SCRAPER_ACCOUNTS`

---

## 方案

### 1. 状态系统：5 → 2

| 旧状态 | 新状态 | 说明 |
|---|---|---|
| drafted / approved / published | `new` | 已翻译，待推送 |
| pushed / rejected | `sent` | 已推送或丢弃 |

**数据库迁移**（在 `database.py` 的 `initialize()` 末尾追加，幂等）：
```sql
-- 79 条积压全部丢弃（包括 published/approved）
UPDATE processed_content SET status = 'sent'
WHERE status IN ('published', 'pushed', 'rejected', 'approved');
-- drafted 变为 new
UPDATE processed_content SET status = 'new'
WHERE status = 'drafted';
```

### 2. 核心流程：6 步 → 1 步

**重构前**：
```
scrape → process → preview --approve → publish → notify → mark-pushed
```

**重构后**：
```
deliver(accounts?, limit?)
  ├─ [可选] scrape（若明确指定 accounts 且需要最新内容）
  ├─ 从库里取 new 状态推文（按 handle 过滤）
  ├─ 翻译（translator）
  ├─ 截图 + 中文覆盖图（screenshotter + image_overlay）
  ├─ Telegram 发送（telegram_notifier）
  └─ mark_sent
```

**说明**：
- 用户说"发一条维斯塔潘的" → 取库里该账号的 new 内容直接处理，**不重新 scrape**，速度快
- 定时任务每 60 分钟 scrape 全部监控账号，新内容翻译后存入库（status=new），**不自动发送**
- 用户主动说"发" → 从库里取 new 状态内容处理发送

### 3. 定时任务（抓取 + 处理 + 自动发送）

Bot 启动时注册 JobQueue，每 60 分钟自动执行完整流程：scrape → process → 截图/覆盖图 → Telegram 推送。
有新内容就直接发给用户，无需手动触发。
环境变量 `POLL_INTERVAL_MINUTES`（默认 60）可调。

```python
# _auto_deliver_job: 每 60 分钟
async def _auto_deliver_job(context):
    accounts = await pipeline.list_accounts()
    if not accounts:
        return
    # scrape 拉新推文，deliver 处理并发送所有 new 状态内容
    result = await pipeline.deliver(accounts=accounts, scrape_first=True)
    # 若有新内容已发出，记录日志即可，不再额外通知
    logger.info("定时推送完成: fetched=%d sent=%d", result["fetched"], result["sent"])
```

**说明**：
- `scrape_first=True` 时 deliver 内部先 scrape 再处理
- 若本轮无新推文（全部已在库中处理过），sent=0，静默跳过，不发消息打扰用户
- 用户手动说"发维斯塔潘的" → `deliver(accounts=["Max33Verstappen"], scrape_first=False)`，直接取库里 new 状态内容
- **定时任务失败**时 Telegram 发送具体错误原因，格式：`⚠️ 定时任务失败\n{traceback}` 便于排查

### 6. 原创推文过滤

在 `scraper/rsshub_client.py` 的推文入库前过滤：
- 过滤条件：`content` 以 `RT ` 开头 → 转推，跳过
- 过滤条件：`content` 以 `@` 开头 → 回复，跳过
- 其余内容视为原创推文，正常入库

### 7. 账号管理统一到数据库

`.env` 中的 `SCRAPER_ACCOUNTS` 仅作为**初始种子**：Bot 首次启动时若数据库 accounts 表为空，则从 `SCRAPER_ACCOUNTS` 导入一次。
后续统一通过 Bot 的 `/add` `/remove` `/accounts` 命令管理，`.env` 不再影响运行时行为。

### 4. Handle 过滤修复

在 `tweet_repo.py` 的 `list_content_with_tweets()` SQL 增加可选 `handles` 参数：
```sql
WHERE p.status IN (?)
  [AND p.handle IN (?)]   -- 新增
ORDER BY p.updated_at DESC
LIMIT ?
```

### 5. 图片发送后立即删除

`deliver()` 中每条推文发送成功后，立即删除对应的本地图片文件（截图、覆盖图、原图）。
`mark_sent` 执行的同时清理文件，确保发送成功才删，不会误删未发出的内容。
`output/previews/images/` 目录保留，但始终接近空。

### 6. 删除小红书发布相关代码

用户自行处理发布，程序不涉及此环节：
- 删除 `publisher/xhs_publisher.py`
- 删除 `publisher/preview.py` 和 `publisher/templates/`
- 删除 `output/pending_publish/` 的写入逻辑
- `bot_intent.py` 删除发布相关 action

---

## image_overlay.py 修复清单

探索发现的问题，在此次重构中一并修复：

### Critical（必修）
1. **L148 — x_range 空值崩溃**：添加 `if not x_range or len(x_range) != 2:` 前置检查，避免模型返回 `null` 导致 IndexError
2. **L280 — 字体加载未捕获**：`ImageFont.truetype()` 异常会级联崩溃，包裹 try-except，失败时跳过该 block 返回原图
3. **L605 — 单字超宽无截断**：添加强制截断，防止超长单词溢出渲染区域

### High（高优先）
4. **L173 — y_hint 无上界检查**：补充 `y_hint <= image.height` 校验
5. **性能优化**：将关键循环中的 `getpixel()` 逐像素调用改为 `tobytes()` 批量读取（预计降低耗时 30-40%）
6. **L279-287 — 字体对象重复创建**：缓存字体对象，避免每次循环都 `ImageFont.truetype()`

### Medium（顺手修）
7. **Prompt 补充 y_hint 范围说明**：避免模型返回负值或超大值

---

## 文件改动清单

### 修改
| 文件 | 改动内容 |
|---|---|
| `scraper/models.py` | `ProcessedStatus` 只保留 `NEW = "new"` / `SENT = "sent"` |
| `storage/database.py` | `initialize()` 末尾追加迁移 SQL（5态→2态，丢弃积压） |
| `storage/tweet_repo.py` | `list_content_with_tweets` 加 `handles` 参数；`mark_pushed` → `mark_sent` |
| `pipeline.py` | 删除 `preview/publish/list_pending/check_published/notify/mark_pushed`；新增 `deliver()`；`__init__` 直接持有 `_screenshotter`/`_downloader` |
| `config.py` | 新增 `poll_interval_minutes: int = 60`；删除 `preview_dir`/`pending_publish_dir` |
| `bot_intent.py` | 精简 action：删除 `notify`/`list_pending`/`process`；`run` → `deliver(accounts?, scrape_first?, temp?)`；临时账号（「抓一下 elonmusk」）只抓一次不加入监控，需用户主动说「把他加到监控」才调用 add_account；更新 system prompt |
| `bot.py` | 删除 `/notify`/`/list` 命令；保留手动触发能力（通过自然语言：「抓一下 elonmusk」「发维斯塔潘最新的」）；`_post_init` 注册 JobQueue 定时推送任务 |
| `main.py` | 删除 `preview`/`publish`/`notify`/`check`/`list-pending`；保留 `setup/scrape/process/deliver/status`；新增 `drain`（清空 new 队列） |
| `publisher/image_overlay.py` | 修复 Critical + High 问题（见上）；性能优化 |

### 删除
- `publisher/xhs_publisher.py`
- `publisher/preview.py`
- `publisher/templates/`

### 修改（补充）
| 文件 | 改动内容 |
|---|---|
| `scraper/rsshub_client.py` | 入库前过滤转推（`RT ` 开头）和回复（`@` 开头） |

### 保留不变
- `scraper/rsshub_client.py`（除过滤逻辑外）
- `scraper/tweet_screenshotter.py`
- `scraper/image_downloader.py`
- `processor/translator.py`
- `processor/content_formatter.py`
- `publisher/telegram_notifier.py`
- `publisher/utils.py`

---

## 实现顺序

1. `scraper/models.py` — 改枚举
2. `storage/database.py` — 加迁移 SQL
3. `storage/tweet_repo.py` — handles 过滤，mark_sent
4. `pipeline.py` — 核心重写，新增 deliver()
5. `config.py` — 加 poll_interval_minutes，清理配置
6. `publisher/image_overlay.py` — 修复 bug + 性能优化
7. `bot_intent.py` — 精简 action/prompt
8. `bot.py` — 加 JobQueue 定时抓取，精简命令
9. `main.py` — 精简子命令
10. 删除 3 个文件

---

## 验证方式

```bash
# 1. 检查迁移（应只剩 new / sent）
sqlite3 data/x2xhs.db "SELECT status, COUNT(*) FROM processed_content GROUP BY status;"

# 2. 手动 deliver，验证 handle 过滤
source .venv/bin/activate && python main.py deliver --accounts Max33Verstappen --limit 1

# 3. 启动 bot，验证定时任务注册成功（日志中应有 JobQueue 启动信息）
python bot.py

# 4. 对话测试：发"维斯塔潘最新一条"，确认精准命中
# 5. 验证 image_overlay 修复：处理一条推文，检查覆盖图是否正常生成
```
