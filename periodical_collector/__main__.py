"""Command line entry points for single and batch collection."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sys
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit
from zipfile import BadZipFile

from .crawler import CrawlConfig, Journal, collect


PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_EXCEL = PROJECT_DIR / "data" / "期刊链接.xlsx"


@dataclass(frozen=True)
class WorkbookRow:
    journal: Journal
    error: str = ""


def _value(value: object) -> str:
    return "" if value is None else str(value).strip()


def _name(name: str) -> str:
    name = name.strip()
    if name.startswith("《") and name.endswith("》"):
        return name[1:-1].strip()
    return name


def _web_url(value: str) -> bool:
    try:
        if not value or any(character.isspace() for character in value):
            return False
        parsed = urlsplit(value)
        port = parsed.port
        return bool(
            parsed.scheme in {"http", "https"} and parsed.hostname
            and not parsed.username and not parsed.password and port != 0
        )
    except ValueError:
        return False


def load_journals(path: Path) -> list[WorkbookRow]:
    from openpyxl import load_workbook
    from openpyxl.utils.exceptions import InvalidFileException

    try:
        book = load_workbook(path, read_only=True, data_only=True)
    except (OSError, BadZipFile, InvalidFileException) as error:
        raise ValueError(f"无法打开 Excel：{path}（{error}）") from error
    try:
        sheet = book["期刊链接"] if "期刊链接" in book.sheetnames else book.active
        rows = sheet.iter_rows(values_only=True)
        header = {_value(value): index for index, value in enumerate(next(rows, ()))}
        required = ("期刊名", "官网")
        if any(label not in header for label in required):
            raise ValueError(f"工作表「{sheet.title}」的首行须包含「期刊名」和「官网」")
        records = []
        for number, cells in enumerate(rows, 2):
            if not any(_value(value) for value in cells):
                continue

            def cell(label):
                index = header.get(label, len(cells))
                return _value(cells[index]) if index < len(cells) else ""

            name, url = _name(cell("期刊名")), cell("官网")
            journal = Journal(name=name, url=url, journal_id=cell("journal_id"), row=number)
            if not name:
                error = "缺少期刊名"
            elif not _web_url(url):
                error = "官网为空或不是有效的 HTTP(S) 网址"
            else:
                error = ""
            records.append(WorkbookRow(journal, error))
        if not records:
            raise ValueError(f"工作表「{sheet.title}」没有期刊记录")
        return records
    finally:
        book.close()


def _positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的整数")
    return number


def _nonnegative_int(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("必须是大于等于 0 的整数")
    return number


def _nonnegative_float(value):
    number = float(value)
    if not math.isfinite(number) or number < 0:
        raise argparse.ArgumentTypeError("必须是大于等于 0 的有限数")
    return number


def _positive_float(value):
    number = _nonnegative_float(value)
    if not number:
        raise argparse.ArgumentTypeError("必须大于 0")
    return number


def _official_host(value):
    value = value.rstrip(".")
    try:
        parsed = urlsplit("//" + value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("请填写域名，不带协议或路径") from error
    if (not parsed.hostname or parsed.hostname.lower() != value.lower()
            or re.search(r"[\s*]", value)):
        raise argparse.ArgumentTypeError("请填写域名，不带协议或路径")
    return parsed.hostname


def _parser():
    parser = argparse.ArgumentParser(description="自动采集期刊的公开投稿格式资料")
    commands = parser.add_subparsers(dest="command", required=True)
    single = commands.add_parser("single", help="采集一个期刊")
    single.add_argument("--journal", required=True, help="期刊名，可带书名号")
    single.add_argument("--url", help="公开网站网址，省略时按名称查询 Excel")
    single.add_argument("--allow-host", action="append", type=_official_host, default=[],
                        help="允许访问的额外官方域名，可重复")
    single.add_argument("--entry-url", action="append", default=[],
                        help="额外官方入口，可重复")
    batch = commands.add_parser("batch", help="按 Excel 顺序采集期刊")
    batch.add_argument("--limit", type=_positive_int, help="最多处理前 N 条记录")
    batch.add_argument("--resume", action="store_true",
                       help="跳过输出目录下已有相同期刊和官网的已完成记录")

    defaults = CrawlConfig()
    for command in (single, batch):
        command.add_argument("--excel", type=Path, default=DEFAULT_EXCEL,
                             help="期刊清单 XLSX 文件")
        command.add_argument("--output", type=Path, default=PROJECT_DIR / "output",
                             help="输出目录")
        command.add_argument("--max-pages", type=_positive_int, default=defaults.max_pages,
                             help="每刊网页访问上限")
        command.add_argument("--max-depth", type=_nonnegative_int, default=defaults.max_depth,
                             help="按链接跟踪的最大深度")
        command.add_argument("--max-downloads", type=_positive_int, default=defaults.max_downloads,
                             help="每刊附件尝试上限")
        command.add_argument("--max-mib", type=_positive_int,
                             default=defaults.max_bytes // (1024 * 1024),
                             help="单次响应的最大 MiB")
        command.add_argument("--timeout", type=_positive_float, default=defaults.timeout,
                             help="HTTP 请求超时秒数")
        command.add_argument("--delay", type=_nonnegative_float, default=defaults.delay,
                             help="请求间隔秒数")
        command.add_argument("--browser", action="store_true", help="启用 Playwright 补查")
        command.add_argument("--browser-use", action="store_true",
                             help="启用按用量计费的 Browser Use Cloud 补查")
    return parser


def _single_journal(args):
    name = _name(args.journal)
    if not name:
        raise ValueError("请填写期刊名")
    for url in args.entry_url:
        if not _web_url(url):
            raise ValueError(f"无效的 --entry-url：{url}")
    options = {"extra_hosts": tuple(args.allow_host), "extra_urls": tuple(args.entry_url)}
    if args.url is not None:
        if not _web_url(args.url):
            raise ValueError("--url 必须为有效的 HTTP(S) 网址")
        return Journal(name=name, url=args.url, **options)

    matches = [item for item in load_journals(args.excel) if _name(item.journal.name) == name]
    if not matches:
        raise ValueError(f"Excel 中未找到「{name}」，可以用 --url 指定官网")
    if len(matches) > 1:
        raise ValueError(f"Excel 中有多个「{name}」，请用 --url 指定官网")
    item = matches[0]
    if item.error:
        raise ValueError(f"Excel 第 {item.journal.row} 行：{item.error}")
    return Journal(name=name, url=item.journal.url, journal_id=item.journal.journal_id,
                   row=item.journal.row, **options)


def _journal_directory(output, journal):
    identity = json.dumps([journal.name, journal.url, journal.journal_id], ensure_ascii=False)
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:12]
    title = re.sub(r"[^\w.\-]+", "_", journal.name).strip("_.")[:48] or "期刊"
    return output / f"{title}-{key}"


def _previous_completed(directory):
    for manifest in sorted(directory.glob("run-*/manifest.json"), reverse=True):
        try:
            with manifest.open(encoding="utf-8") as stream:
                result = json.load(stream)
        except (OSError, ValueError):
            continue
        if not isinstance(result, dict) or result.get("status") != "completed":
            continue
        for item in result.get("artifacts") or []:
            if (not isinstance(item, dict) or item.get("type") != "attachment"
                    or item.get("format") not in {"doc", "docx", "pdf"}):
                continue
            relative = item.get("local_path")
            if isinstance(relative, str) and relative:
                original = (manifest.parent / relative).resolve()
                if original.is_relative_to(manifest.parent.resolve()) and original.is_file():
                    return manifest
    return None


def _run_directory(parent, mode):
    parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    return Path(tempfile.mkdtemp(prefix=f"{mode}-{timestamp}-", dir=parent))


def _write_json(path, value):
    # All callers use a freshly created run directory.
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _run_journal(journal, output, config, resume=False):
    directory = _journal_directory(output, journal)
    if resume:
        completed = _previous_completed(directory)
        if completed:
            return {"journal": asdict(journal), "status": "completed", "resumed": True,
                    "manifest": str(completed.relative_to(output))}
    run = _run_directory(directory, "run")
    manifest = run / "manifest.json"
    try:
        result = collect(journal, run, config)
    except Exception as error:
        # Persist any evidence already written and let other journals continue.
        result = {"journal": asdict(journal), "status": "uncompleted", "artifacts": [],
                  "pending": [{"reason": f"{type(error).__name__}: {error}"}]}
        if manifest.exists():
            manifest = run / "interrupted.json"
        _write_json(manifest, result)
    return {"journal": asdict(journal), "status": result["status"],
            "manifest": str(manifest.relative_to(output)),
            "artifacts": len(result.get("artifacts", [])),
            "pending": len(result.get("pending", []))}


def _batch(args, output, config):
    records = load_journals(args.excel)[:args.limit]
    directory = _run_directory(output, "batch")
    summary_path = directory / "batch-summary.json"
    summary = {"spreadsheet": str(args.excel.resolve()),
               "started_at": datetime.now(timezone.utc).isoformat(),
               "planned_count": len(records), "counts": {"completed": 0, "uncompleted": 0},
               "results": []}
    _write_json(summary_path, summary)
    for index, item in enumerate(records, 1):
        if item.error:
            record = {"journal": asdict(item.journal), "status": "uncompleted", "error": item.error}
        else:
            record = _run_journal(item.journal, output, config, args.resume)
        summary["results"].append(record)
        summary["counts"][record["status"]] += 1
        _write_json(summary_path, summary)
        print(f"[{index}/{len(records)}] {item.journal.name or '(缺少期刊名)'} {record['status']}",
              flush=True)
    summary["finished_at"] = datetime.now(timezone.utc).isoformat()
    _write_json(summary_path, summary)
    print(summary_path)


def main(argv=None):
    parser = _parser()
    args = parser.parse_args(argv)
    output = args.output.expanduser().resolve()
    config = CrawlConfig(max_pages=args.max_pages, max_depth=args.max_depth,
                         max_downloads=args.max_downloads, timeout=args.timeout, delay=args.delay,
                         max_bytes=args.max_mib * 1024 * 1024, use_playwright=args.browser,
                         use_browser_use=args.browser_use)
    try:
        if args.command == "single":
            record = _run_journal(_single_journal(args), output, config)
            print(f"{record['journal']['name']} {record['status']}")
            print(output / record["manifest"])
        else:
            _batch(args, output, config)
    except (ValueError, OSError, ImportError) as error:
        print(f"采集未运行：{error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
