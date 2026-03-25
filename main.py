from __future__ import annotations

import argparse
import asyncio
import logging

from rich.console import Console
from rich.table import Table

from config import load_config
from pipeline import Pipeline

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="x2xhs: X 平台内容到 Telegram 自动化管道")
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("setup", help="初始化目录和数据库").set_defaults(command="setup")

    scrape_parser = subparsers.add_parser("scrape", help="抓取 RSSHub 推文")
    scrape_parser.add_argument("--rsshub", action="store_true", help="使用 RSSHub 抓取")
    scrape_parser.add_argument("--accounts", nargs="+", help="指定账号（覆盖数据库监控列表）")
    scrape_parser.add_argument("--keywords", nargs="+", help="指定关键词（覆盖数据库关键词列表）")

    deliver_parser = subparsers.add_parser("deliver", help="处理并发送内容到 Telegram")
    deliver_parser.add_argument("--accounts", nargs="+", help="指定账号（不填则取监控列表）")
    deliver_parser.add_argument("--limit", type=int, default=None, help="发送数量上限")
    deliver_parser.add_argument("--no-scrape", action="store_true", help="不先抓取，直接尝试发送库内候选内容")

    subparsers.add_parser("status", help="查看当前状态").set_defaults(command="status")
    subparsers.add_parser("list-keywords", help="查看监控关键词").set_defaults(command="list-keywords")

    add_kw_parser = subparsers.add_parser("add-keyword", help="添加监控关键词")
    add_kw_parser.add_argument("keyword", help="关键词")

    remove_kw_parser = subparsers.add_parser("remove-keyword", help="删除监控关键词")
    remove_kw_parser.add_argument("keyword", help="关键词")

    return parser


async def run_command(args: argparse.Namespace) -> int:
    config = load_config()
    pipeline = Pipeline(config)

    if args.command == "setup":
        db_path = await pipeline.setup()
        console.print(f"[green]初始化完成[/green] 数据库: {db_path}")
        return 0

    if args.command == "scrape":
        result = await pipeline.scrape(
            use_rsshub=args.rsshub,
            accounts=args.accounts,
            keywords=getattr(args, "keywords", None),
        )
        console.print(f"[cyan]抓取完成[/cyan] fetched={result['fetched']} inserted={result['inserted']}")
        return 0

    if args.command == "deliver":
        result = await pipeline.deliver(
            accounts=args.accounts,
            limit=args.limit,
            scrape_first=not args.no_scrape,
            force=False,
        )
        console.print(
            f"[cyan]发送完成[/cyan] "
            f"fetched={result['fetched']} inserted={result['inserted']} sent={result['sent']}"
        )
        return 0

    if args.command == "status":
        counts = await pipeline.status()
        render_status_table(counts)
        return 0

    if args.command == "list-keywords":
        keywords = await pipeline.list_keywords()
        if keywords:
            console.print("监控关键词：\n" + "\n".join(f"- {kw}" for kw in keywords))
        else:
            console.print("当前没有监控关键词。")
        return 0

    if args.command == "add-keyword":
        created = await pipeline.add_keyword(args.keyword)
        if created:
            console.print(f"[green]已添加关键词[/green] {args.keyword}")
        else:
            console.print(f"[yellow]关键词已存在[/yellow] {args.keyword}")
        return 0

    if args.command == "remove-keyword":
        removed = await pipeline.remove_keyword(args.keyword)
        if removed:
            console.print(f"[green]已删除关键词[/green] {args.keyword}")
        else:
            console.print(f"[yellow]未找到关键词[/yellow] {args.keyword}")
        return 0

    raise ValueError(f"未知命令: {args.command}")


def render_status_table(counts: dict[str, int]) -> None:
    table = Table(title="x2xhs 状态")
    table.add_column("项目")
    table.add_column("数量", justify="right")
    for key in ["tweets", "sent", "filtered", "scrape_log"]:
        table.add_row(key, str(counts.get(key, 0)))
    console.print(table)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    parser = build_parser()
    args = parser.parse_args()
    try:
        raise SystemExit(asyncio.run(run_command(args)))
    except Exception as exc:
        console.print(f"[red]执行失败[/red] {exc}")
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
