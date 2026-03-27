from __future__ import annotations

import datetime
import logging
import os
import re
import signal
import traceback

import json as _json

from telegram import BotCommand, Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from bot_intent import Intent, chat_reply, parse_intent
from config import AppConfig, load_config
from pipeline import Pipeline
from processor.translator import TranslationSkipped

# 专门用于 NL 解析和对话的模型（需要支持 JSON 输出，不能是纯推理模型）
_BOT_MODEL = "google/gemini-2.0-flash-001"

logger = logging.getLogger(__name__)

_BOT_DATA_CONFIG_KEY = "config"
_BOT_DATA_PIPELINE_KEY = "pipeline"


def _seconds_until_next_hour() -> float:
    now = datetime.datetime.now()
    next_hour = (now + datetime.timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    return (next_hour - now).total_seconds()


def _parse_limit(args: list[str]) -> int | None:
    if not args:
        return None
    try:
        return int(args[0])
    except ValueError:
        return None


def _format_accounts(accounts: list[str]) -> str:
    if not accounts:
        return "当前没有监控账号。"
    return "监控账号：\n" + "\n".join(f"- @{handle}" for handle in accounts)


def _format_keywords(keywords: list[str]) -> str:
    if not keywords:
        return "当前没有监控关键词。"
    return "监控关键词：\n" + "\n".join(f"- {kw}" for kw in keywords)


def _format_status(counts: dict[str, int]) -> str:
    keys = ["tweets", "sent", "filtered", "scrape_log"]
    lines = [f"{key}: {counts.get(key, 0)}" for key in keys]
    return "当前状态：\n" + "\n".join(lines)


async def _post_init(application: Application) -> None:
    config = load_config()
    pipeline = Pipeline(config)
    await pipeline.setup()
    application.bot_data[_BOT_DATA_CONFIG_KEY] = config
    application.bot_data[_BOT_DATA_PIPELINE_KEY] = pipeline

    # 注册定时抓取+评分任务，对齐整点
    first_seconds = _seconds_until_next_hour()
    application.job_queue.run_repeating(
        _auto_score_job,
        interval=3600,
        first=first_seconds,
        name="auto_score",
    )
    logger.info("定时评分任务已注册，下次触发在整点（%.0f 秒后）", first_seconds)

    # 注册命令菜单（输入 / 时显示）
    await application.bot.set_my_commands([
        BotCommand("start", "查看帮助"),
        BotCommand("scores", "最近评分"),
        BotCommand("threshold", "查看/调整评分阈值"),
        BotCommand("feedback", "给评分器反馈偏好"),
        BotCommand("accounts", "监控账号列表"),
        BotCommand("add", "添加监控账号"),
        BotCommand("remove", "删除监控账号"),
        BotCommand("keywords", "监控关键词列表"),
        BotCommand("status", "查看系统状态"),
        BotCommand("viral", "关键词爆文搜索"),
        BotCommand("digest", "话题综述（xAI）"),
        BotCommand("pause", "暂停自动推送"),
        BotCommand("resume", "恢复自动推送"),
        BotCommand("off", "🛑 紧急停止 bot"),
    ])


def _get_pipeline(context: ContextTypes.DEFAULT_TYPE) -> tuple[AppConfig, Pipeline]:
    config = context.application.bot_data.get(_BOT_DATA_CONFIG_KEY)
    pipeline = context.application.bot_data.get(_BOT_DATA_PIPELINE_KEY)
    if not isinstance(config, AppConfig) or not isinstance(pipeline, Pipeline):
        raise RuntimeError("Pipeline 尚未初始化")
    return config, pipeline


async def _ensure_allowed(update: Update, config: AppConfig) -> bool:
    if config.telegram is None:
        return False
    chat_id = str(update.effective_chat.id) if update.effective_chat else ""
    if chat_id == config.telegram.chat_id:
        return True
    if update.effective_message:
        await update.effective_message.reply_text("未授权的聊天。")
    return False


async def _auto_score_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """定时任务：抓取 → 评分 → 推送候选到 Telegram。"""
    config = context.application.bot_data.get(_BOT_DATA_CONFIG_KEY)
    pipeline = context.application.bot_data.get(_BOT_DATA_PIPELINE_KEY)
    if not isinstance(config, AppConfig) or not isinstance(pipeline, Pipeline):
        logger.error("定时任务：Pipeline 未初始化")
        return
    try:
        accounts = await pipeline.list_accounts()
        keywords = await pipeline.list_keywords()
        if not accounts and not keywords:
            logger.info("定时任务：无监控账号和关键词，跳过")
            return
        result = await pipeline.score_and_present(scrape_first=True)
        logger.info(
            "定时评分完成: fetched=%d inserted=%d scored=%d candidates=%d",
            result["fetched"], result["inserted"], result["scored"],
            len(result.get("candidates", [])),
        )
        # 只推送未播报过的新候选，已出现过的静默跳过
        candidates = result.get("candidates", [])
        new_candidates = [
            c for c in candidates
            if c["external_id"] not in pipeline._presented_candidate_ids
        ]
        if not new_candidates:
            logger.info("定时任务：无新候选，静默")
            return

        # 缓存新候选，保证编号与消息一致
        pipeline._last_candidates = new_candidates

        message = pipeline._format_candidates(new_candidates)
        if config.telegram:
            from telegram import Bot
            bot = Bot(token=config.telegram.bot_token)
            await bot.send_message(
                chat_id=config.telegram.chat_id,
                text=message,
            )
        for c in new_candidates:
            pipeline._presented_candidate_ids.add(c["external_id"])
    except Exception:
        tb = traceback.format_exc()
        logger.error("定时任务失败:\n%s", tb)
        if config.telegram:
            try:
                from telegram import Bot
                bot = Bot(token=config.telegram.bot_token)
                await bot.send_message(
                    chat_id=config.telegram.chat_id,
                    text=f"⚠️ 定时任务失败\n\n<pre>{tb[:3000]}</pre>",
                    parse_mode="HTML",
                )
            except Exception as send_exc:
                logger.error("发送错误通知失败: %s", send_exc)


async def off_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """紧急停止：立刻杀掉 bot 进程（systemd 不会自动重启，需 SSH 手动启动）。"""
    config, _ = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    await update.effective_message.reply_text("🛑 紧急停止，bot 已关闭。重启：ssh zhang@91.99.136.130 然后 sudo systemctl start x2xhs")
    os.kill(os.getpid(), signal.SIGTERM)


async def pause_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _ = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    jobs = context.job_queue.get_jobs_by_name("auto_score")
    if not jobs:
        await update.effective_message.reply_text("⚠️ 定时任务不存在或已暂停。")
        return
    for job in jobs:
        job.schedule_removal()
    logger.info("定时推送已暂停")
    await update.effective_message.reply_text("⏸ 定时推送已暂停。发 /resume 恢复。")


async def resume_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _ = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    jobs = context.job_queue.get_jobs_by_name("auto_score")
    if jobs:
        await update.effective_message.reply_text("⚠️ 定时任务已在运行中，无需恢复。")
        return
    first_seconds = _seconds_until_next_hour()
    context.job_queue.run_repeating(
        _auto_score_job,
        interval=3600,
        first=first_seconds,
        name="auto_score",
    )
    logger.info("定时推送已恢复")
    await update.effective_message.reply_text(f"▶️ 定时推送已恢复，将在下一个整点（{int(first_seconds // 60)} 分钟后）执行。")


async def viral_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """关键词爆文搜索：/viral <关键词>"""
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    if not context.args:
        await update.effective_message.reply_text("用法: /viral <关键词>\n例如: /viral AI agents")
        return
    keyword = " ".join(context.args)
    await update.effective_message.reply_text(f"⏳ 正在搜索关键词「{keyword}」的爆文...")
    result = await pipeline.keyword_viral(keyword)
    if result["success"]:
        await update.effective_message.reply_text(
            f"✅ 已发送 @{result['handle']} 的推文（来源: {result.get('source', '?')}）\n"
            f"标题：{result['title']}"
        )
    else:
        await update.effective_message.reply_text(f"❌ 搜索失败：{result['reason']}")


async def digest_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """话题综述：/digest <关键词>"""
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    if not context.args:
        await update.effective_message.reply_text("用法: /digest <关键词>\n例如: /digest AI agents")
        return
    keyword = " ".join(context.args)
    await update.effective_message.reply_text(f"⏳ 正在生成「{keyword}」话题综述，约需 15-30 秒...")
    result = await pipeline.topic_digest(keyword)
    if result["success"]:
        await update.effective_message.reply_text(result["message"])
    else:
        await update.effective_message.reply_text(f"❌ 生成失败：{result['reason']}")


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _ = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    await update.effective_message.reply_text(
        "\n".join([
            "x2xhs bot 已启动，每小时自动抓取+评分并推送候选。",
            "",
            "📋 候选确认：回复「发1」确认发布",
            "/scores 查看最近评分",
            "/threshold [n] 查看/调整评分阈值",
            "/feedback <内容> 给评分器反馈偏好",
            "",
            "/viral <关键词> 关键词爆文搜索",
            "/digest <关键词> 话题综述（需 XAI_API_KEY）",
            "",
            "/accounts 查看监控账号",
            "/add <handle> 添加账号",
            "/remove <handle> 删除账号",
            "/keywords 查看监控关键词",
            "/status 查看状态",
            "/pause 暂停自动推送",
            "/resume 恢复自动推送",
            "",
            "或直接用自然语言说：",
            "「发1」「跳过」「严格一点」「技术类的更好」",
        ])
    )


async def accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    accounts = await pipeline.list_accounts()
    await update.effective_message.reply_text(_format_accounts(accounts))


async def add_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    if not context.args:
        await update.effective_message.reply_text("用法: /add <handle>")
        return
    handle = context.args[0]
    created = await pipeline.add_account(handle)
    message = f"已添加 @{handle.lstrip('@')}" if created else f"账号已存在或无效: {handle}"
    await update.effective_message.reply_text(message)


async def remove_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    if not context.args:
        await update.effective_message.reply_text("用法: /remove <handle>")
        return
    handle = context.args[0]
    removed = await pipeline.remove_account(handle)
    message = f"已删除 @{handle.lstrip('@')}" if removed else f"未找到账号: {handle}"
    await update.effective_message.reply_text(message)


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    counts = await pipeline.status()
    await update.effective_message.reply_text(_format_status(counts))


async def keywords_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    keywords = await pipeline.list_keywords()
    await update.effective_message.reply_text(_format_keywords(keywords))


async def feedback_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    text = " ".join(context.args) if context.args else ""
    if not text:
        await update.effective_message.reply_text("用法: /feedback <你的反馈>\n例如: /feedback 技术教程类的适当加分")
        return
    await pipeline.add_feedback(text)
    await update.effective_message.reply_text(f"✅ 已记录反馈，后续评分会参考")


async def threshold_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    if not context.args:
        await update.effective_message.reply_text(f"当前评分阈值: {pipeline.threshold}")
        return
    try:
        new_val = float(context.args[0])
        if not 1 <= new_val <= 10:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("阈值必须是 1-10 的数字（支持小数，如 7.5）")
        return
    await pipeline.set_threshold(new_val)
    await update.effective_message.reply_text(f"✅ 评分阈值已调整为 {new_val}")


async def scores_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    scores = await pipeline.list_recent_scores(limit=10)
    if not scores:
        await update.effective_message.reply_text("暂无评分记录")
        return
    lines = ["📊 最近评分：\n"]
    for s in scores:
        detail_str = ""
        raw_detail = s.get("filter_scores_detail")
        if raw_detail:
            try:
                d = _json.loads(raw_detail)
                detail_str = (
                    f"  📐 信息差{d.get('info_diff','?')} "
                    f"纵深{d.get('depth','?')} "
                    f"眼光{d.get('angle','?')} "
                    f"势能{d.get('viral','?')}\n"
                )
            except Exception:
                pass
        lines.append(
            f"[{s['filter_score']}分] @{s['handle']}\n"
            f"  {s['content_preview']}...\n"
            f"{detail_str}"
            f"  💡 {s['filter_reason']}\n"
        )
    await update.effective_message.reply_text("\n".join(lines))


async def _build_nl_context(pipeline: Pipeline) -> str:
    try:
        accounts = await pipeline.list_accounts()
        counts = await pipeline.status()
        account_str = ", ".join(f"@{a}" for a in accounts) if accounts else "无"
        threshold = pipeline.threshold
        candidates = pipeline._last_candidates
        if candidates:
            cand_lines = []
            for i, c in enumerate(candidates, 1):
                preview = c["content"][:60].replace("\n", " ")
                cand_lines.append(f"[{i}] @{c['handle']}({c['filter_score']}分): {preview}")
            candidate_str = "\n".join(cand_lines)
        else:
            candidate_str = "无"
        return (
            f"监控账号: {account_str}\n"
            f"tweets={counts.get('tweets', 0)} "
            f"sent={counts.get('sent', 0)} "
            f"filtered={counts.get('filtered', 0)}\n"
            f"评分阈值={threshold}\n"
            f"当前候选:\n{candidate_str}"
        )
    except Exception:
        return "暂无"


async def _execute_intent(update: Update, pipeline: Pipeline, intent: Intent, context: ContextTypes.DEFAULT_TYPE | None = None) -> None:
    action = intent.action
    params = intent.params
    msg = update.effective_message
    config = pipeline._config

    try:
        if action == "add_account":
            handle = str(params.get("handle", "")).strip().lstrip("@")
            if not handle:
                await msg.reply_text("请告诉我要添加哪个账号。")
                return
            added = await pipeline.add_account(handle)
            await msg.reply_text(
                f"✅ 已添加 @{handle}" if added else f"⚠️ @{handle} 已在列表中"
            )

        elif action == "remove_account":
            handle = str(params.get("handle", "")).strip().lstrip("@")
            if not handle:
                await msg.reply_text("请告诉我要移除哪个账号。")
                return
            removed = await pipeline.remove_account(handle)
            await msg.reply_text(
                f"✅ 已移除 @{handle}" if removed else f"⚠️ 未找到 @{handle}"
            )

        elif action == "list_accounts":
            accounts = await pipeline.list_accounts()
            await msg.reply_text(_format_accounts(accounts))

        elif action == "add_keyword":
            keyword = str(params.get("keyword", "")).strip()
            if not keyword:
                await msg.reply_text("请告诉我要添加哪个关键词。")
                return
            added = await pipeline.add_keyword(keyword)
            await msg.reply_text(
                f"✅ 已添加关键词「{keyword}」" if added else f"⚠️ 关键词「{keyword}」已在列表中"
            )

        elif action == "remove_keyword":
            keyword = str(params.get("keyword", "")).strip()
            if not keyword:
                await msg.reply_text("请告诉我要删除哪个关键词。")
                return
            removed = await pipeline.remove_keyword(keyword)
            await msg.reply_text(
                f"✅ 已删除关键词「{keyword}」" if removed else f"⚠️ 未找到关键词「{keyword}」"
            )

        elif action == "list_keywords":
            keywords = await pipeline.list_keywords()
            await msg.reply_text(_format_keywords(keywords))

        elif action == "score_and_present":
            accounts = params.get("accounts") or None
            scrape_first = bool(params.get("scrape_first", True))
            await msg.reply_text("⏳ 正在抓取并评分，稍等...")
            result = await pipeline.score_and_present(
                scrape_first=scrape_first,
                accounts=accounts,
            )
            pipeline._last_candidates = result.get("candidates", [])
            message = result.get("message")
            if message:
                await msg.reply_text(message)
            else:
                await msg.reply_text(
                    f"暂无高分候选（已评分 {result['scored']} 条，新增 {result['inserted']} 条）"
                )

        elif action == "deliver":
            accounts = params.get("accounts") or None
            scrape_first = bool(params.get("scrape_first", True))
            temp = bool(params.get("temp", False))
            limit = params.get("limit")
            # 用户未指定数量时默认最多 2 条，防止刷屏
            if limit is None:
                limit = 2

            if not accounts:
                monitored_accounts = await pipeline.list_accounts()
                monitored_keywords = await pipeline.list_keywords()
                if not monitored_accounts and not monitored_keywords:
                    await msg.reply_text("⚠️ 没有监控账号或关键词，请先添加。")
                    return
            elif temp:
                logger.info("临时发送账号: %s", accounts)

            result = await pipeline.deliver(
                accounts=accounts,
                limit=limit,
                scrape_first=scrape_first,
                force=False,
            )
            sent = result["sent"]
            inserted = result["inserted"]
            errors = result.get("errors", [])

            if sent > 0:
                reply = f"✅ 已发送 {sent} 条（本次新增 {inserted} 条）"
            elif inserted > 0:
                reply = f"抓到 {inserted} 条新增内容，但这次没有成功发送"
            else:
                reply = f"没有可发的内容（新增 {inserted} 条）"

            if errors:
                error_text = "\n".join(str(err) for err in errors[:3])
                if len(errors) > 3:
                    error_text += f"\n…共 {len(errors)} 条失败"
                reply += f"\n\n⚠️ 失败详情：\n{error_text}"

            await msg.reply_text(reply)
            return

        elif action == "status":
            counts = await pipeline.status()
            await msg.reply_text(_format_status(counts))

        elif action == "pause":
            if context is None:
                await msg.reply_text("⚠️ 无法执行，请直接发 /pause 命令。")
            else:
                jobs = context.job_queue.get_jobs_by_name("auto_score")
                if not jobs:
                    await msg.reply_text("⚠️ 定时任务不存在或已暂停。")
                else:
                    for job in jobs:
                        job.schedule_removal()
                    await msg.reply_text("⏸ 定时推送已暂停。说「恢复」可重新开启。")

        elif action == "resume":
            if context is None:
                await msg.reply_text("⚠️ 无法执行，请直接发 /resume 命令。")
            else:
                jobs = context.job_queue.get_jobs_by_name("auto_score")
                if jobs:
                    await msg.reply_text("⚠️ 定时任务已在运行中，无需恢复。")
                else:
                    first_seconds = _seconds_until_next_hour()
                    context.job_queue.run_repeating(
                        _auto_score_job,
                        interval=3600,
                        first=first_seconds,
                        name="auto_score",
                    )
                    await msg.reply_text(f"▶️ 定时推送已恢复，将在下一个整点（{int(first_seconds // 60)} 分钟后）执行。")

        elif action == "viral":
            keyword = str(params.get("keyword", "")).strip()
            if not keyword:
                await msg.reply_text("请告诉我要搜索哪个关键词，比如「AI agents 爆文」")
                return
            await msg.reply_text(f"⏳ 正在搜索「{keyword}」的爆文...")
            result = await pipeline.keyword_viral(keyword)
            if result["success"]:
                await msg.reply_text(
                    f"✅ 已发送 @{result['handle']} 的推文（来源: {result.get('source', '?')}）\n"
                    f"标题：{result['title']}"
                )
            else:
                await msg.reply_text(f"❌ 搜索失败：{result['reason']}")

        elif action == "digest":
            keyword = str(params.get("keyword", "")).strip()
            if not keyword:
                await msg.reply_text("请告诉我要综述哪个关键词，比如「AI agents 综述」")
                return
            await msg.reply_text(f"⏳ 正在生成「{keyword}」话题综述，约需 15-30 秒...")
            result = await pipeline.topic_digest(keyword)
            if result["success"]:
                await msg.reply_text(result["message"])
            else:
                await msg.reply_text(f"❌ 生成失败：{result['reason']}")

        elif action == "scrape":
            accounts = params.get("accounts") or None
            keywords = params.get("keywords") or None
            result = await pipeline.scrape(use_rsshub=True, accounts=accounts, keywords=keywords)
            await msg.reply_text(
                f"✅ 抓取完成：获取 {result['fetched']} 条，新增 {result['inserted']} 条"
            )

        elif action == "approve_candidate":
            index = int(params.get("index", 0))
            if index < 1 or not pipeline._last_candidates or index > len(pipeline._last_candidates):
                await msg.reply_text(f"没找到对应的候选（当前 {len(pipeline._last_candidates)} 条），能再描述一下吗？")
                return

            # 二次确认：展示匹配到的推文，等用户确认
            if not params.get("_confirmed") and context is not None:
                c = pipeline._last_candidates[index - 1]
                preview = c["content"][:200].replace("\n", " ")
                await msg.reply_text(
                    f"找到这条：\n"
                    f"@{c['handle']} [{c['filter_score']}分]\n\n"
                    f"{preview}...\n\n"
                    f"是这条吗？"
                )
                context.user_data[_PENDING_INTENT_KEY] = Intent(
                    action="approve_candidate",
                    params={**params, "_confirmed": True},
                )
                return

            await msg.reply_text(f"⏳ 正在处理...")
            try:
                result = await pipeline.process_candidate(index)
                if result.get("success"):
                    await msg.reply_text(
                        f"✅ 已处理 @{result['handle']}: {result['title']}\n"
                        f"内容已发送到 Telegram，请在 Claude Code 中用 MCP 发布到小红书"
                    )
                else:
                    await msg.reply_text(f"⚠️ 处理失败: {result.get('reason', '未知')}")
            except ValueError as exc:
                await msg.reply_text(f"⚠️ {exc}")
            except TranslationSkipped as exc:
                await msg.reply_text(f"⚠️ 模型判断不适合写: {exc.reason}")

        elif action == "skip_candidates":
            pipeline._last_candidates = []
            await msg.reply_text("✅ 已跳过当前所有候选")

        elif action == "scorer_feedback":
            feedback_content = str(params.get("content", "")).strip()
            if not feedback_content:
                await msg.reply_text("请告诉我你的反馈内容")
                return
            await pipeline.add_feedback(feedback_content)
            await msg.reply_text(f"✅ 已记录反馈，后续评分会参考")

        elif action == "set_threshold":
            value = params.get("value")
            if value is None:
                await msg.reply_text(f"当前评分阈值: {pipeline.threshold}")
                return
            try:
                new_val = float(value)
                if not 1 <= new_val <= 10:
                    raise ValueError
            except (ValueError, TypeError):
                await msg.reply_text("阈值必须是 1-10 的数字（支持小数，如 7.5）")
                return
            await pipeline.set_threshold(new_val)
            await msg.reply_text(f"✅ 评分阈值已调整为 {new_val}")

        elif action == "list_scores":
            scores = await pipeline.list_recent_scores(limit=10)
            if not scores:
                await msg.reply_text("暂无评分记录")
            else:
                lines = ["📊 最近评分：\n"]
                for s in scores:
                    detail_str = ""
                    raw_detail = s.get("filter_scores_detail")
                    if raw_detail:
                        try:
                            d = _json.loads(raw_detail)
                            detail_str = (
                                f"  📐 信息差{d.get('info_diff','?')} "
                                f"纵深{d.get('depth','?')} "
                                f"眼光{d.get('angle','?')} "
                                f"势能{d.get('viral','?')}\n"
                            )
                        except Exception:
                            pass
                    lines.append(
                        f"[{s['filter_score']}分] @{s['handle']}\n"
                        f"  {s['content_preview']}...\n"
                        f"{detail_str}"
                        f"  💡 {s['filter_reason']}\n"
                    )
                await msg.reply_text("\n".join(lines))

        elif action == "clarify":
            # Bot 反问用户以确认意图，reply 字段包含反问内容
            pass  # reply 已在上面发送

        elif action == "chat":
            reply = await chat_reply(
                message=update.effective_message.text or "",
                openrouter_api_key=pipeline._config.processor.openrouter_api_key,
                model=_BOT_MODEL,
            )
            if reply:
                await msg.reply_text(reply)

        else:
            await msg.reply_text(f"⚠️ 未知操作: {action}")

    except Exception as exc:
        logger.exception("Intent execution failed")
        await msg.reply_text(f"❌ 执行出错：{exc}")


_CONFIRM_YES = re.compile(r"^(确认|是|好|好的|可以|执行|对|嗯|行|ok|yes|✓)[\s!！。]*$", re.IGNORECASE)
_CONFIRM_NO = re.compile(r"^(取消|不|算了|不了|不要|不用|no|cancel|×)[\s!！。]*$", re.IGNORECASE)
_PENDING_INTENT_KEY = "pending_intent"


async def handle_natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return

    text = (update.effective_message.text or "").strip()
    if not text:
        return

    # ── 处理待确认的 intent ──
    pending: Intent | None = context.user_data.get(_PENDING_INTENT_KEY)
    if pending is not None:
        if _CONFIRM_YES.match(text):
            del context.user_data[_PENDING_INTENT_KEY]
            await update.effective_chat.send_action("typing")
            await _execute_intent(update, pipeline, pending, context)
            return
        elif _CONFIRM_NO.match(text):
            del context.user_data[_PENDING_INTENT_KEY]
            await update.effective_message.reply_text("已取消。")
            return
        # 其他内容视为新指令，覆盖 pending

    await update.effective_chat.send_action("typing")

    ctx = await _build_nl_context(pipeline)
    intent = await parse_intent(
        message=text,
        openrouter_api_key=config.processor.openrouter_api_key,
        model=_BOT_MODEL,
        context=ctx,
    )

    if intent.needs_confirm:
        context.user_data[_PENDING_INTENT_KEY] = intent
        confirm_text = intent.reply or f"要执行「{intent.action}」吗？"
        await update.effective_message.reply_text(f"{confirm_text}\n\n回复「确认」执行，「取消」放弃。")
        return

    if intent.reply and intent.action != "chat":
        await update.effective_message.reply_text(intent.reply)

    await _execute_intent(update, pipeline, intent, context)


async def _handle_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    tb = traceback.format_exc()
    logger.exception("Telegram bot handler error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        await update.effective_message.reply_text(f"❌ 执行失败: {context.error}\n\n{tb[:500]}")


def build_application() -> Application:
    config = load_config()
    if config.telegram is None:
        raise ValueError("未配置 Telegram，请检查 TELEGRAM_BOT_TOKEN 和 TELEGRAM_CHAT_ID")

    application = (
        ApplicationBuilder()
        .token(config.telegram.bot_token)
        .post_init(_post_init)
        .build()
    )
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("pause", pause_command))
    application.add_handler(CommandHandler("resume", resume_command))
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("keywords", keywords_command))
    application.add_handler(CommandHandler("feedback", feedback_command))
    application.add_handler(CommandHandler("threshold", threshold_command))
    application.add_handler(CommandHandler("scores", scores_command))
    application.add_handler(CommandHandler("viral", viral_command))
    application.add_handler(CommandHandler("digest", digest_command))
    application.add_handler(CommandHandler("off", off_command))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_natural_language))
    application.add_error_handler(_handle_error)
    return application


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    application = build_application()
    application.run_polling()


if __name__ == "__main__":
    main()
