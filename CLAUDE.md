# x2xhs — Claude 工作指南

## 项目是什么

把 X（Twitter）帖子自动翻译并发布到小红书的 pipeline。

工作目录：`/Users/zzx/Desktop/AI_code/x2xhs`
数据库：`data/x2xhs.db`
发布输出：`output/pending_publish/`
截图/图片：`output/previews/images/`

## 监控账号

`claudeai` `AnthropicAI` `sama` `elonmusk` `karpathy` `realDonaldTrump`

> Trump 帖子在 `preview --approve` 阶段手动筛选，只发政策相关（关税/科技制裁/中美关系），拒掉纯党派/情绪内容。

## 标准发布流程

```
scrape → process → preview --approve → publish → check → MCP publish_content → mark-pushed
```

发布一条的完整命令序列：

```bash
python main.py scrape --rsshub          # 拉取新推文（可选，已有草稿可跳过）
python main.py process --limit 1        # 翻译生成草稿
python main.py preview --limit 1 --approve   # 截图 + 生成中文覆盖图 + 标记 approved
python main.py publish --limit 1        # 导出 payload JSON
python main.py check --limit 1          # 发布前质量检查
# → 用 MCP publish_content 发布
# → python main.py mark-pushed --id <tweet_external_id>
```

## 用户说「发一条 XX 的」时的操作步骤

1. 查数据库找该账号最新的 drafted 推文（按 `updated_at DESC`）
2. 若队列靠后，用 `UPDATE processed_content SET updated_at=datetime('now','+1 hour')` 提前
3. 依次执行：preview --approve → publish → check
4. 展示 check 结果供确认，再用 MCP `mcp__xiaohongshu__publish_content` 发布
5. 发布成功后执行 `mark-pushed --id <tweet_external_id>`

## 中文覆盖图（image_overlay.py）

每次 preview 阶段自动生成 `*_tweet_zh.png`，规则：

- **实现**：纯像素处理（PIL），不调用任何模型
- **字体**：冬青黑体 `/System/Library/Fonts/Hiragino Sans GB.ttc`
- **字号**：36px 起，溢出才缩小
- **只翻译正文**，不处理链接卡片、附图、互动栏
- **正文底边**：用 `_find_text_bottom` 像素扫描定位（找最后一个文字像素行），不依赖外部坐标
- 测试时必须同时打开原图和覆盖图对比

## MCP 发布参数

```python
mcp__xiaohongshu__publish_content(
    title=payload["title"],
    content=payload["body"],
    images=payload["images"],   # 顺序：原截图 → 中文覆盖图 → 推文附图
    tags=payload["tags"],       # 不含 # 前缀，由 MCP 处理
)
```

## 注意事项

- `check` 通过标准：正文 200–800 字、tag 3–5 个、图片本地文件存在、无 X 原链接
- 队列顺序按 `processed_content.updated_at DESC`，可手动调整优先级
- 覆盖图生成约 30–60 秒（调用视觉模型），请耐心等待
- 参考 `docs/ARCHITECTURE.md` 了解模块设计，`docs/DEVLOG.md` 了解历史决策
