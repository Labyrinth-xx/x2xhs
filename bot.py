from __future__ import annotations

import logging
import traceback

from telegram import Update
from telegram.ext import Application, ApplicationBuilder, CommandHandler, ContextTypes, MessageHandler, filters

from bot_intent import Intent, chat_reply, parse_intent
from config import AppConfig, load_config
from pipeline import Pipeline

# 专门用于 NL 解析和对话的模型（需要支持 JSON 输出，不能是纯推理模型）
_BOT_MODEL = "google/gemini-2.0-flash-001"

logger = logging.getLogger(__name__)

_BOT_DATA_CONFIG_KEY = "config"
_BOT_DATA_PIPELINE_KEY = "pipeline"


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
    keys = ["tweets", "new", "sent", "scrape_log"]
    lines = [f"{key}: {counts.get(key, 0)}" for key in keys]
    return "当前状态：\n" + "\n".join(lines)


async def _post_init(application: Application) -> None:
    config = load_config()
    pipeline = Pipeline(config)
    await pipeline.setup()
    application.bot_data[_BOT_DATA_CONFIG_KEY] = config
    application.bot_data[_BOT_DATA_PIPELINE_KEY] = pipeline

    # 注册定时抓取发送任务
    interval_seconds = config.poll_interval_minutes * 60
    application.job_queue.run_repeating(
        _auto_deliver_job,
        interval=interval_seconds,
        first=interval_seconds,
        name="auto_deliver",
    )
    logger.info("定时任务已注册，间隔 %d 分钟", config.poll_interval_minutes)


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


async def _auto_deliver_job(context: ContextTypes.DEFAULT_TYPE) -> None:
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
        result = await pipeline.deliver(accounts=None, scrape_first=True)
        logger.info(
            "定时推送完成: fetched=%d inserted=%d sent=%d",
            result["fetched"], result["inserted"], result["sent"],
        )
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


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, _ = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return
    await update.effective_message.reply_text(
        "\n".join([
            "x2xhs bot 已启动，每小时自动抓取并推送新内容。",
            "",
            "/accounts 查看监控账号",
            "/add <handle> 添加账号",
            "/remove <handle> 删除账号",
            "/keywords 查看监控关键词",
            "/status 查看状态",
            "",
            "或直接用自然语言说：",
            "「发一条维斯塔潘的」「抓一下 elonmusk」「加上 sama」",
            "「监控 AI Agent 话题」「删除关键词 LLM」",
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


async def _build_nl_context(pipeline: Pipeline) -> str:
    try:
        accounts = await pipeline.list_accounts()
        counts = await pipeline.status()
        account_str = ", ".join(f"@{a}" for a in accounts) if accounts else "无"
        return (
            f"监控账号: {account_str}\n"
            f"tweets={counts.get('tweets', 0)} "
            f"new={counts.get('new', 0)} "
            f"sent={counts.get('sent', 0)}"
        )
    except Exception:
        return "暂无"


async def _execute_intent(update: Update, pipeline: Pipeline, intent: Intent) -> None:
    action = intent.action
    params = intent.params
    msg = update.effective_message

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

        elif action == "deliver":
            accounts = params.get("accounts") or None
            scrape_first = bool(params.get("scrape_first", True))
            temp = bool(params.get("temp", False))
            limit = params.get("limit")

            if not accounts:
                accounts = await pipeline.list_accounts()
            if not accounts:
                await msg.reply_text("⚠️ 没有监控账号，请先添加（比如：加上 sama）")
                return

            result = await pipeline.deliver(accounts=accounts, limit=limit, scrape_first=scrape_first)
            sent = result["sent"]
            inserted = result["inserted"]
            if sent > 0:
                await msg.reply_text(f"✅ 已发送 {sent} 条（本次新增 {inserted} 条）")
            else:
                await msg.reply_text(f"暂无新内容（本次新增 {inserted} 条，队列已空）")

        elif action == "status":
            counts = await pipeline.status()
            await msg.reply_text(_format_status(counts))

        elif action == "scrape":
            accounts = params.get("accounts") or None
            keywords = params.get("keywords") or None
            result = await pipeline.scrape(use_rsshub=True, accounts=accounts, keywords=keywords)
            await msg.reply_text(
                f"✅ 抓取完成：获取 {result['fetched']} 条，新增 {result['inserted']} 条"
            )

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
        tb = traceback.format_exc()
        logger.exception("Intent execution failed")
        await msg.reply_text(f"❌ 执行失败\n\n{tb[:1000]}")


async def handle_natural_language(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config, pipeline = _get_pipeline(context)
    if not await _ensure_allowed(update, config):
        return

    text = (update.effective_message.text or "").strip()
    if not text:
        return

    await update.effective_chat.send_action("typing")

    ctx = await _build_nl_context(pipeline)
    intent = await parse_intent(
        message=text,
        openrouter_api_key=config.processor.openrouter_api_key,
        model=_BOT_MODEL,
        context=ctx,
    )

    if intent.reply and intent.action != "chat":
        await update.effective_message.reply_text(intent.reply)

    await _execute_intent(update, pipeline, intent)


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
    application.add_handler(CommandHandler("accounts", accounts_command))
    application.add_handler(CommandHandler("add", add_command))
    application.add_handler(CommandHandler("remove", remove_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("keywords", keywords_command))
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
