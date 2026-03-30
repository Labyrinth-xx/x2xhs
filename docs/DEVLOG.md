# DEVLOG

## 2026-03-30 — xAI 背景调研 prompt 全面优化（5维度→3维度）

### 完成内容
- **ResearchBrief 精简**：5 字段（author_status / recent_events / structural_background / timing_subtext / broader_context）→ 3 字段（author_recent / event_context / notable_connections），消除重叠
- **xAI 调研 prompt 重写**：角色改为"调查记者"；加入质量底线反面示例；notable_connections 提供时间巧合/前后矛盾/利害关系三类示例；总量预算制（500-1000字按需分配）；要求具体日期
- **参数调整**：max_tokens 1200→2500、timeout 15s→45s（防止 web 搜索超时静默失败）、推文截断 500→800 字符
- **to_prompt_section 重写**：header 改为"你已了解到"认知框架（不再说"背景调研"），字段顺序调整为 event_context→author_recent→notable_connections
- **Claude system prompt 精炼**：新增"背景知识的运用"章节（6行）；deep 模式偏倚防护（背景不改变模式判断标准）；分析角度措辞从"发现事实"→"基于已知事实做判断"；Rule 7 简化
- **测试**：新增 test_context_enricher.py（10个用例），更新 test_translator.py，全部 15 个测试通过

### 关键决策
- xAI 分工：只提供事实和事实关联，不做主观判断（"可能是想转移注意力"这类推测由 Claude 做）
- 字数策略选总量预算制而非逐字段限定，让 xAI 按信息量自行分配，避免简单推文也填满字数
- timeout 从 15s 改为 45s：web 搜索 + 2500 token 生成，15s 必然频繁超时导致静默失败

### 遗留问题 / 下次继续
- 端到端验证（test_xai.py / test_prompt.py）需真实 API key 手动运行，确认 Grok 输出质量符合预期
- VPS 尚未同步本次改动，需在合适时机 push→pull→restart

## 2026-03-27 — 四项体验优化：整点刷新 / 分维度评分 / 文案视角 / 候选来源

### 完成内容
- **整点刷新**：bot 定时任务从"启动后每 60 分钟"改为对齐整点，新增 `_seconds_until_next_hour()`，`resume` 逻辑同步
- **分维度评分**：评分器从单一总分改为 4 维度（信息差 35% + 纵深 25% + 眼光 20% + 势能 20%）加权综合分，保留一位小数；评分结果存入 `filter_scores_detail` 列；`/scores` 展示明细
- **默认阈值 7.5**：`FilterConfig.threshold` 改为 float，默认从 7 提高到 7.5；`/threshold` 命令支持小数
- **文案视角修正**：prompt 追加明确禁令——不在正文写对内容本身的元评论（"信号价值有限"/"不宜过度解读"等）；觉得没深度就少写，不解释
- **候选来源展示**：候选消息每条附注 `（来自 @sama）` 或 `（关键词: AI）`，让用户知道每条推文的来源
- **关闭关键词抓取**：VPS `.env` 中 `SCRAPER_KEYWORDS` 已确认为空，DB 无关键词，关键词路径静默

### 关键决策
- 评分综合分由 Python 计算而非 LLM 直接给，避免模型保守聚集在 6-8 区间
- 阈值类型改为 float 以支持 7.5 这样的精细控制；SQLite INTEGER 列存 float 自动 REAL affinity 无需 ALTER
- 文案问题根因是 LLM 把编辑判断写进了读者文章，解法是 prompt 层面明确禁止，而非事后过滤

### 遗留问题 / 下次继续
- 关键词抓取后期需重新启用，届时候选消息的来源标注会同时展示关键词来源
- 旧数据 `filter_scores_detail` 为 NULL（历史评分未重打），重新触发评分后自动补全

## 2026-03-18
- 初始化 `x2xhs` 项目结构。
- 完成 RSSHub 抓取、Claude 翻译、HTML 预览、JSON 导出四段异步流程骨架。
- 接入 SQLite WAL、Rich CLI、Jinja2 模板和 `.env.example`。

## 2026-03-19 — 接入小红书 MCP 自动发布

### 完成内容
- `ProcessedStatus` 新增 `PUSHED` 状态，状态机扩展为：drafted → approved → published → pushed。
- `ProcessedContent` 增加 `pushed_at` 字段。
- `processed_content` 表增加 `pushed_at` 列，`Database.initialize()` 含幂等 migration（兼容已有 DB）。
- `TweetRepository.mark_pushed()` 更新状态并写入 `pushed_at`。
- `Pipeline.list_pending()` 读取 status=PUBLISHED 的条目，返回可直接传给 MCP 工具的 payload 列表。
- `Pipeline.mark_pushed()` 代理到 repo 层。
- CLI 新增 `list-pending`（支持 `--json` 原始输出）和 `mark-pushed --id <tweet_external_id>`。
- `status` 命令表格新增 `pushed` 行。

### 关键决策
- MCP 工具只能由 Claude 调用，Python 无法直接调用；因此采用混合架构：Python 管理状态/输出 payload，Claude 调用 `mcp__xiaohongshu__publish_content`，调用成功后再由 Claude 执行 `mark-pushed` 更新 DB。
- `pushed_at` migration 采用 try/except ALTER TABLE，兼容旧 DB 不重建表。

### 遗留问题 / 下次继续
- 实际推送工作流：需先 `python main.py list-pending --json` → Claude 读取 → 调用 MCP → `mark-pushed`。
- 若图片为远程 URL，MCP 工具自动下载；若本地截图路径不存在则需 fallback。
- 可考虑加 `--dry-run` 模式只打印不推送。

## 2026-03-19 — 中文覆盖图功能调试与参数确认

### 完成内容
- `publisher/image_overlay.py` 完整实现并经多轮测试确认。
- 覆盖模型切换为 `anthropic/claude-sonnet-4-6`（Qwen 会把每行识别为独立小块，字号被压到 14px；Sonnet 整段合并效果更好）。
- 字体确认为冬青黑体（Hiragino Sans GB），用户偏好。
- 正文字号定为 36px，溢出时逐步缩小。
- 渲染两步走：先背景色实心矩形消除英文，再放中文。
- `_find_separator_y` 像素扫描解决模型 y2 系统性偏小问题（详见 ARCHITECTURE.md）。

### 关键决策
- 只翻译正文，不处理链接卡片、附图等内容（当前阶段）。
- 正文边界靠横线分隔线判断，而非依赖间距大小，更准确。
- 模型 y2 坐标不可信，必须用像素扫描修正底边，否则最后一行英文必然残留。

### 遗留问题 / 下次继续
- 图片内文字翻译（链接卡片图、附图内的英文）暂未实现，留待后续。
- `_find_separator_y` 的 120px 扫描上限在某些长正文推文上可能不够，需更多测试。

## 2026-03-20 — 图片覆盖边界条件测试与 3 处 bug 修复

### 完成内容
- 对 8 种代表性推文类型（极短/短/中/长/超长RT/含图/RT含图/多图）做全量测试，发现并修复 3 个边界 bug。
- 旧架构说明（`_find_separator_y`、`box:[x1,y1,x2,y2]` 格式、`MIN_Y`/`MAX_BLOCK_GAP` 常量）均已作废，更新了内存文件 `project_translation_overlay.md`。

### 关键修复

**Bug 1：右侧英文泄露**
- 原因：`_find_actual_x2` 搜索范围 `hint_x2 + 100`，RT 推文中模型低估 x2 达 150px+，扫描范围不覆盖实际文字。
- 修复：搜索范围改为 `hint_x2 + 250`。

**Bug 2：左侧英文泄露**
- 原因：模型 x1 有时偏大 20-30px，未覆盖左侧文字像素。
- 修复：新增 `_find_actual_x1`，对称处理左边界（向左扩展 100px 搜索）。背景矩形和文字起点均改用 `bg_x1`。

**Bug 3：RT 推文 `_find_body_start_y` 误判**
- 原因：「↰ Naval reposted」文字行结束后有 ~16px 空白，恰好 ≥ MIN_GAP_ROWS=10，状态机在 Moses Kagan 头像开始处误判为正文起点。
- 修复：触发候选位置时，向后扫描最多 15 行，取深色像素横向 max_span；span ≥ 0.4 才接受（正文横跨全列），否则重置继续扫（头像/归因图标只分布在左侧小范围）。

### 关键决策
- span check 用 15 行前瞻而非单行，因为字符顶端笔画可能只有几像素宽，正文第一行本身也可能触发 narrow 误判。
- x1/x2 的像素扫描修正是必要的：LLM 的像素坐标在不同推文格式下系统性偏移。

### 遗留问题 / 下次继续
- 图片内文字翻译（链接卡片图、附图内的英文）未实现。
- RT 推文（如 Naval RT Moses Kagan）中，Moses Kagan 的头像+用户名行也被覆盖在中文区域内；这是 span check 局限，可接受，不影响内容正确性。

## 2026-03-20 — 引用卡片正文翻译覆盖（双层防护方案）

### 完成内容
- `publisher/image_overlay.py` 完整实现引用卡片正文的中文覆盖翻译，所有 6 项计划改动已落地。
- 通过 4 类代表性推文全量测试：sama（主文+图表+引用卡）、Anthropic（主文+窄span引用卡）、Naval/Moses（转发+引用卡+视频）、纯文字推文×2（回归验证），全部通过。

### 关键修复（6 处，全在 image_overlay.py）

**改动 1：修复 NameError**
- `return search_near_y or 0` → `return scan_after_y or 0`（参数已改名，旧变量未同步）

**改动 2+4：y_hint scan floor（Layer 1）**
- `_find_body_start_y` 新增 `y_hint` 参数；scan_after_y 分支用 `scan_floor = max(scan_after_y, y_hint - 200)` 跳过 Block 间的图表/媒体区域。
- `_render_overlay` 传递 `y_hint=block.get("y_hint")` 给 `_find_body_start_y`。

**改动 3：降低 MIN_SPAN**
- scan_after_y 分支 `MIN_SPAN: 0.38 → 0.25`，支持卡片边框内缩进导致的窄跨度文字（如 Anthropic 引用卡 span=0.31）。

**改动 5：重构 `_find_body_end_y` 文字行判定（Layer 2）**
- 删除 `consecutive_colored` 检测（图表检测逻辑不准确）。
- 新文字判定：近灰色（max(R,G,B)<100 且 max-min<30）+ 跨度≥0.38，排除图表轴标签等彩色/局部深色元素。
- 底部边距保持 38px；确保 Block 1 的 y2 不穿越图表，为 scan_after_y 提供准确起点。

**改动 6：blocks 按 y_hint 排序**
- `sorted(blocks, key=lambda b: b.get("y_hint", 0))` 防止模型返回顺序错乱。

### 关键决策
- 双层防护协同：Layer 1（y_hint 跳转）处理正常情况；Layer 2（_find_body_end_y 改进）在 y_hint 不可靠时确保 scan_after_y 起点准确，两层互补。
- MIN_SPAN 降到 0.25 的安全性由三重过滤保障：y_hint 下限 + 饱和度过滤（MAX_SAT=40）+ CONFIRM_ROWS=4。
- y_hint 绝对值不稳定（同一 block 跨次可差 300-500px），但相对顺序（主推 < 引用卡）稳定，排序仍有效。

### 代码审查发现的非阻塞边界条件
- `sample_xs` 长度为 1（x_range 宽度 12-48px）时静默失败，有 warning 日志但无提前 guard；实际场景模型不会返回这么窄的范围。
- `getpixel()` 逐像素 Python 调用性能低（~94000 次/帧），但被模型调用耗时（30-60s）掩盖，暂不优化。

### 遗留问题 / 下次继续
- 图片内英文（引用卡蓝底图、附图 overlay 文字）仍未翻译，是下阶段目标。
- `sample_xs` 长度 1 的边界条件可加显式 guard + warning，但非当前优先级。

## 2026-03-24 — 翻译卡片重构 + 关键词抓取调研

### 完成内容

**翻译卡片重构（image_overlay.py 完全重写）**
- 删除所有 Vision API 调用和像素扫描覆盖逻辑，改为「截图 + 底部拼接翻译卡片」方案。
- 新增 `append_translations(screenshot_path, translations)` 支持多卡片（主推文 + 引用推文各一张）。
- 卡片样式：无分割线，动态检测英文文字边界对齐中文宽度，支持亮/暗模式自适应背景色，引用卡片边框连续性处理。
- 确认参数：PAD_TOP=4, PAD_BOTTOM=40, LABEL_GAP=16, BODY_FONT_SIZE=36, LABEL_FONT_SIZE=20。
- `translator.py` 新增 `translate_literal_parts()`，区分主推文和引用推文分别直译，用 gemini-2.0-flash-001。
- `pipeline.py` 更新 `_build_payload()` 使用新接口。

**关键词抓取基础设施（已实现，但 keyword 接口暂不可用）**
- DB 新增 `monitored_keywords` 表，pipeline/repo/CLI/bot 全部支持关键词 CRUD。
- `scrape()` 支持 `--keywords` 参数，与账号抓取并行运行。
- `deliver()` 修复：无显式 accounts 时不过滤 handle，关键词推文也能发出。
- bot 新增 add_keyword/remove_keyword/list_keywords 指令。

### 关键决策
- 翻译卡片方案：Vision 覆盖耗时 30-60s + API 成本高 → 改为纯 PIL 拼接，速度降至 ~3s，效果更接近 X App 原生翻译。
- 引用卡片支持：检测灰色分割线或亮→暗过渡点定位插入位置，从下往上插入保证 Y 坐标不漂移。

### 遗留问题 / 下次继续
- **关键词抓取目前不可用**：RSSHub 的 `/twitter/keyword/` 接口调用 Twitter GraphQL SearchTimeline，端点 ID 已 404（Twitter 定期轮换）。经调研这是结构性问题，不适合作为长期方案。
- **待决策：换用 twscrape 替代 RSSHub 的关键词搜索**
  - twscrape 专注 Twitter 搜索，跟进 API 变化更快，支持 `min_faves`/`min_retweets` 操作符
  - 需要提供一个 Twitter 小号凭证（不用主账号，可隔离封号风险）
  - 改动范围：仅新增 `scraper/twscrape_client.py` + 修改 pipeline.py 几行，翻译/图片/Telegram 不受影响
  - 暂未动手，等用户确认方向后再实施

## 2026-03-25 — 关键词抓取上线（Playwright 方案）+ 内容引流清洗

### 完成内容

**关键词抓取：Playwright 拦截 SearchTimeline**
- 放弃 twscrape 库（xclid.py 中 `e=>e+"."+` JS 模式已失效，无法生成 x-client-transaction-id）
- 改用 Playwright Chromium 注入 Cookie 打开搜索页，监听 `SearchTimeline` GraphQL 响应直接解析
- 浏览器自动处理所有 anti-bot 头，无需逆向 JS，彻底绕过封锁
- 实测：4 个关键词各 ~7s，单次 scrape 共抓取 78 条推文，全部正确解析
- `screen_name` 在新 API 响应中位于 `user_result.core.screen_name`（非旧版 `legacy.screen_name`），已适配

**内容引流清洗（小红书平台合规）**
- `translator.py` system prompt 新增规则：禁止输出 URL 和 @提及，@用户改为直接写名字
- `translate_literal_parts`（图片翻译卡片）同步更新：删除 URL，@符号去掉只保留名字
- `content_formatter.py` 新增后处理兜底：正则清除 `https?://\S+` 和 `@\w+`，整理多余空行

### 关键决策
- Playwright 拦截方案稳定性优于直接 HTTP：不依赖端点 ID 轮换，不依赖 x-client-transaction-id 生成逻辑
- 代价是速度（7s/关键词 vs 理论上 <1s），但可接受
- Cookie 过期后需手动更新 `.env` 中的 `TWSCRAPE_AUTH_TOKEN` 和 `TWSCRAPE_CT0`

### 遗留问题 / 下次继续
- Cookie 自动刷新：目前需手动更新 `.env`，可考虑接入 Playwright 自动登录续期
- 关键词搜索每次启动新 Chromium 实例，有启动开销；多关键词可考虑复用同一 browser context

## 2026-03-26 — 截图翻译方案重构（X 内置翻译）+ 引用卡片支持

### 完成内容

**截图集成 X 平台内置翻译（`scraper/tweet_screenshotter.py`）**
- 将 X 账号显示语言改为简体中文，使「翻译帖子」按钮对英文推文可见
- 截图流程新增：关 cookie 弹窗 → 点翻译 → 等待展开 → 截图
- 注入 CSS 隐藏 sticky header（高度 53px），防止遮挡推文头像/用户名
- 改用 `element.screenshot()` 替代 `page.screenshot()+clip`，不受 viewport 高度限制
- 同时从 DOM 读取 `div[role="link"]`（引用卡片）的底部 Y 坐标，以截图像素返回

**引用卡片自定义翻译叠加（`publisher/image_overlay.py`）**
- 新增 `append_at_y(path, translation, insert_y)` 方法，在精确 Y 位置插入单张翻译卡片
- 复用现有卡片渲染逻辑（`_make_card`、边框连续性检测）

**修复 `scraper/twscrape_client.py`**
- 支持 `note_tweet` 字段读取完整长文（`full_text` 被 API 截断至 ~280 字符）
- 支持 `quoted_status_result` 引用推文内容拼接到正文末尾

**写作风格优化（`processor/prompts.py`）**
- 去除独立「元过渡句」（如「这句话值得停一下想想。」）
- 禁用「」引文格式，引用改为转述融入句子
- 归因动词从「说」改为认为/指出/坦言/强调等有立场感的词

### 关键决策
- 放弃自定义 PIL 覆盖图作为主翻译方案，改用 X 内置 Google 翻译：位置更自然，无需像素扫描
- 引用卡片翻译代码已实现但**暂时禁用**（`pipeline.py` 注释），待后续调整风格后启用
- VPS 用 `root` 用户（非 `zhang`），本地公钥已写入 `root@91.99.136.130:~/.ssh/authorized_keys`

### 遗留问题 / 下次继续
- 引用卡片翻译：取消 `pipeline.py` 中的注释即可启用，翻译文本风格可单独调整
- X 账号语言已改为中文，X 界面全程中文；若需改回需手动进设置
