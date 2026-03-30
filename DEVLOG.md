# DEVLOG — x2xhs

## 2026-03-31 — 关键词扫描系统完整实现并验证上线

### 完成内容
- **新建 4 个文件**：`scraper/keyword_queries.py`（10 条查询）、`processor/keyword_sweep.py`（扫描编排器）、`processor/event_dedup.py`（事件聚类）、`processor/keyword_refresh.py`（关键词自适应刷新）
- **修改 10 个文件**：database.py（新增 keyword_queries/sweep_log 表）、tweet_repo.py（新增 CRUD）、config.py（sweep 配置）、prompts.py（4 个 Opus 设计的 prompt）、scorer.py（5 维度评分）、xai_client.py（聚类/核查/建议方法）、context_enricher.py（未知作者路由）、pipeline.py（薄编排）、bot.py（定时任务+命令）、bot_intent.py（新意图）
- 代码审查修复了 6 个问题（时区 bug、credibility_note 未入库、命中统计、web plugin 误用等）
- 部署 VPS：`git pull` + `systemctl restart x2xhs`，DB 自动迁移
- **验证**：首次扫描产出 178 原始 → 49 新 → 11 候选入池，关键词刷新生成 9 条建议

### 关键决策
- Sonnet 搭结构（TODO_OPUS_FILL 占位）→ Opus 填核心 prompt + 查询 → Sonnet 接线，分工保证质量
- 评分 5 维度（newsworthiness 30% / info_diff 25% / source_authority 20% / depth 15% / viral 10%），阈值 7.0（低于账号 7.5，因已有互动量预筛）
- context_enricher 按 `source_type` 路由：keyword_sweep/keyword_merge 走"身份核查"模板，保留原有账号模板不变
- MERGE 综述通过 `_digest_to_raw_tweet()` 封装成 RawTweet 占位，使其能进入相同评分和入池流程

### 遗留问题 / 下次继续
- 关键词刷新建议（9 条）需要在 Telegram 查看并决定是否采纳
- sweep 每 8 小时自动触发，下次触发时观察是否有噪音（min_faves 门槛是否合适）
- keyword_refresh.py 的建议 UI（Telegram 确认/拒绝流）尚未实现（intent 已注册，但 apply_keyword_suggestion 逻辑待测试）
