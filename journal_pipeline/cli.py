import argparse
import csv
import json
import os
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, replace
from datetime import datetime, timezone
from pathlib import Path

from .config import load_settings
from .pipeline import atomic_json, process_journal, sha256
from .source import journal_from_url, load_journals
from .semantic import SemanticMatcher


def parser():
    ap = argparse.ArgumentParser(description='中文期刊投稿格式采集 pipeline')
    commands = ap.add_subparsers(dest='command', required=True)
    for name, help_text in [('inspect', '检查输入和分类，不访问网站'),
                            ('run', '执行采集：--url 单个网站，或 Excel 默认前 5 本')]:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument('--config', type=Path)
        source = sub.add_mutually_exclusive_group()
        source.add_argument('--input', type=Path, help='Excel 输入；不传时使用默认清单')
        source.add_argument('--url', help='直接处理一个网站地址，不读取 Excel')
        sub.add_argument('--name', help='--url 任务显示名称，默认使用域名')
        sub.add_argument('--output', type=Path)
        sub.add_argument('--sheet')
        if name == 'run':
            group = sub.add_mutually_exclusive_group()
            group.add_argument('--limit', type=int, help='最多处理 N 本，必须大于 0')
            group.add_argument('--all', action='store_true', help='显式运行全部符合范围的期刊')
            sub.add_argument('--journals', help='精确期刊名，使用英文或中文逗号分隔')
            sub.add_argument('--workers', type=int)
            sub.add_argument('--browser', choices=('auto', 'off', 'always', 'chrome', 'auto-chrome'),
                             help='auto-chrome：HTTP 优先，不足时复用正常 Chrome；chrome：直接使用正常 Chrome')
            sub.add_argument('--delay', type=float)
            sub.add_argument('--max-pages', type=int, help='每个任务处理的网址数量上限')
            sub.add_argument('--max-depth', type=int, help='最大链接深度；输入网址为第 0 层')
            sub.add_argument('--allow-http-fallback', dest='http_fallback',
                             action=argparse.BooleanOptionalAction, default=None,
                             help='HTTPS 连接失败时允许尝试明文 HTTP，默认关闭')
            sub.add_argument('--semantic', action=argparse.BooleanOptionalAction, default=None,
                             help='启用本地 embedding 补充链接筛选与正文验证')
            sub.add_argument('--embedding-model', help='本地模型目录或已缓存的 Hugging Face 模型名')
            sub.add_argument('--semantic-threshold', type=float, help='语义链接最低相似度')
            sub.add_argument('--semantic-content-threshold', type=float, help='正文语义验证最低相似度')
            sub.add_argument('--semantic-top-k', type=int, help='每页最多补充的语义候选数量')
            sub.add_argument('--resume', action='store_true', help='跳过指纹与文件哈希均相同的成功结果')
            sub.add_argument('--dry-run', action='store_true', help='仅写出所选任务，不联网')
    return ap


def csv_report(path, rows):
    columns = ['journal_id', 'name', 'bucket', 'status', 'artifacts', 'errors', 'warnings', 'directory',
               'format_requirements', 'format_review']
    temporary = path.with_suffix('.csv.tmp')
    with temporary.open('w', encoding='utf-8-sig', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=columns)
        writer.writeheader()
        for row in rows:
            safe = {k: ("'" + v if isinstance(v, str) and v.startswith(('=', '+', '-', '@')) else v)
                    for k, v in row.items()}
            writer.writerow(safe)
    temporary.replace(path)


def execute(args, settings, journals, audit):
    atomic_json(settings.output / 'input-audit.json', audit)
    inventory = [asdict(j) for j in journals]
    if args.command == 'inspect':
        atomic_json(settings.output / 'inventory.json', inventory)
        print(json.dumps({'rows': audit['rows'], 'accepted': len(journals),
                          'skipped': len(audit['skipped']),
                          'buckets': dict(Counter(j.bucket for j in journals)),
                          'report': str(settings.output / 'input-audit.json')}, ensure_ascii=False, indent=2))
        return 0
    direct = args.url is not None
    input_value = journals[0].urls[0] if direct else str(settings.input)
    effective_settings = json.loads(json.dumps(asdict(settings), default=str))
    if direct:
        effective_settings['input'] = None
    atomic_json(settings.output / 'run-plan.json', {
        'selected': inventory, 'input_type': 'url' if direct else 'xlsx',
        'input_url': input_value if direct else None,
        'input_sha256': None if direct else sha256(settings.input.read_bytes()),
        'settings': effective_settings, 'dry_run': args.dry_run})
    if args.dry_run:
        print(f'预演完成：选择 {len(journals)} 本，不访问网站。计划：{settings.output / "run-plan.json"}')
        return 0
    # Validate the optional backend before dispatching workers. No model load for inspect/dry-run.
    matcher = SemanticMatcher(settings) if settings.semantic else None
    results = []
    with ThreadPoolExecutor(max_workers=settings.workers) as executor:
        futures = {executor.submit(process_journal, journal, settings, args.resume, matcher): journal for journal in journals}
        for future in as_completed(futures):
            journal = futures[future]
            try:
                result = future.result()
            except Exception as error:
                result = {'journal': asdict(journal), 'status': 'failed', 'artifacts': [], 'warnings': [],
                          'errors': [{'stage': 'worker', 'message': f'{type(error).__name__}: {error}'}], 'directory': ''}
            results.append(result)
            print(f"[{len(results)}/{len(journals)}] {journal.name}: {result['status']} ({len(result['artifacts'])} 项)", flush=True)
    results.sort(key=lambda item: item['journal']['id'])
    statuses = dict(Counter(item['status'] for item in results))
    atomic_json(settings.output / 'summary.json', {
        'completed_at': datetime.now(timezone.utc).isoformat(), 'input': input_value,
        'input_type': 'url' if direct else 'xlsx',
        'selected': len(journals), 'statuses': statuses, 'results': results})
    rows = [{'journal_id': r['journal']['id'], 'name': r['journal']['name'], 'bucket': r['journal']['bucket'],
             'status': r['status'], 'artifacts': len(r['artifacts']), 'errors': len(r['errors']),
             'warnings': len(r['warnings']), 'directory': r['directory'],
             'format_requirements': r.get('format_report', {}).get('requirements', 0),
             'format_review': r.get('format_report', {}).get('review', 0)} for r in results]
    csv_report(settings.output / 'summary.csv', rows)
    csv_report(settings.output / 'review.csv', [r for r in rows if r['status'] != 'success' or r['warnings']
                                                or r['format_review'] or not r['format_requirements']])
    print(f'汇总: {json.dumps(statuses, ensure_ascii=False)}\n输出: {settings.output}')
    return 0 if all(item['status'] == 'success' for item in results) else 1


def main(argv=None):
    ap = parser()
    args = ap.parse_args(argv)
    try:
        direct = args.url is not None
        if args.name is not None and not direct:
            raise ValueError('--name 只能与 --url 一起使用')
        if direct:
            incompatible = [flag for key, flag in [('sheet', '--sheet'), ('journals', '--journals'),
                                                   ('limit', '--limit'), ('all', '--all')]
                            if getattr(args, key, None) is not None and getattr(args, key) is not False]
            if incompatible:
                raise ValueError('--url 不可与批量输入参数一起使用: ' + ', '.join(incompatible))
        settings = load_settings(args.config)
        overrides = {key: getattr(args, key) for key in ('input', 'output', 'sheet', 'workers', 'browser',
                                                       'delay', 'max_pages', 'max_depth', 'http_fallback',
                                                       'semantic', 'embedding_model', 'semantic_threshold',
                                                       'semantic_content_threshold', 'semantic_top_k')
                     if getattr(args, key, None) is not None}
        for key in ('input', 'output'):
            if key in overrides:
                overrides[key] = overrides[key].resolve()
        settings = replace(settings, **overrides).validate()
        if direct:
            journals, audit = journal_from_url(args.url, settings, args.name)
        else:
            journals, audit = load_journals(settings.input, settings)
        if args.command == 'run' and not direct:
            if args.limit is not None and args.limit <= 0:
                raise ValueError('--limit 必须大于 0；全量请使用 --all')
            if args.journals:
                names = {n.strip() for n in args.journals.replace('，', ',').split(',') if n.strip()}
                unknown = names - {j.name for j in journals}
                if unknown:
                    raise ValueError(f'未找到符合范围的期刊名: {sorted(unknown)}')
                journals = [j for j in journals if j.name in names]
            limit = args.limit if args.limit is not None else (None if args.all or args.journals else 5)
            if limit:
                journals = journals[:limit]
            if not journals:
                raise ValueError('没有可运行的期刊，请先 inspect 检查输入')
        settings.output.mkdir(parents=True, exist_ok=True)
        lock = settings.output / '.run.lock'
        try:
            with lock.open('x', encoding='utf-8') as stream:
                stream.write(f'pid={os.getpid()}\n')
        except FileExistsError:
            raise ValueError(f'输出目录已有运行锁: {lock}；确认无任务运行后可删除残留锁') from None
        try:
            return execute(args, settings, journals, audit)
        finally:
            lock.unlink(missing_ok=True)
    except (OSError, ValueError, KeyError, TypeError) as error:
        ap.error(str(error))


if __name__ == '__main__':
    raise SystemExit(main())
