# x2xhs — Claude 工作指南

## 项目是什么

把 X（Twitter）帖子自动翻译并发布到小红书的 pipeline。

工作目录：`/Users/zzx/Desktop/AI_code/x2xhs`
数据库：`data/x2xhs.db`
截图/图片：`output/previews/images/`

## 监控账号

`claudeai` `AnthropicAI` `sama` `elonmusk` `karpathy` `realDonaldTrump`

> Trump 帖子在 Telegram 确认阶段手动筛选，只发政策相关（关税/科技制裁/中美关系），拒掉纯党派/情绪内容。

## 实际发布流程

**Bot 自动路径（日常）：**
```
scrape → score_and_present → 候选池 → Telegram 确认 → process_candidate → MCP publish_content
```

**手动触发（deliver 一步完成）：**
```bash
python main.py scrape --rsshub          # 拉取新推文（可选）
python main.py deliver --limit 1        # 评分 + 翻译 + 截图 + 发 Telegram
# → 确认内容后，用 MCP publish_content 发布到小红书
```

## 用户说「发一条 XX 的」时的操作步骤

1. 查数据库找该账号候选池中或高分推文
2. 若不在池中，用 `deliver --accounts XX --limit 1` 直接处理
3. Telegram 收到内容后确认
4. 用 MCP `mcp__xiaohongshu__publish_content` 发布

## 可用 CLI 命令

```bash
python main.py setup                        # 初始化目录和数据库
python main.py scrape --rsshub              # 抓取账号 + 关键词推文
python main.py deliver --limit N            # 翻译 + 截图 + 发 Telegram
python main.py status                       # 查看各状态数量
python main.py list-keywords                # 查看监控关键词
python main.py add-keyword "关键词"          # 添加关键词
python main.py remove-keyword "关键词"       # 删除关键词
python main.py viral --keyword "关键词"      # 关键词爆文 → 发 Telegram
python main.py digest --keyword "关键词"     # 话题综述 → 发 Telegram
python main.py discover-fun --n 3           # 趣文发现 → 加入候选池
```

## MCP 发布参数

```python
mcp__xiaohongshu__publish_content(
    title=payload["title"],
    content=payload["body"],
    images=payload["images"],   # 顺序：原截图 → 推文附图（图片已保留在 output/previews/images/）
    tags=payload["tags"],       # 不含 # 前缀，由 MCP 处理
)
```

## 注意事项

- 图片发送 Telegram 后**不再删除**，MCP 发布时直接用已有路径
- 候选池优先级按评分排序，可通过 Bot 指令跳过或确认
- 中文覆盖图（`*_tweet_zh.png`）目前未启用
- 参考 `docs/ARCHITECTURE.md` 了解模块设计，`docs/DEVLOG.md` 了解历史决策
