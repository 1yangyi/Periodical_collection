import hashlib
import json
from dataclasses import asdict
from datetime import datetime, timezone
from heapq import heappop, heappush
from itertools import count
from pathlib import Path
from urllib.parse import urlsplit

from . import __version__
from .extract import extract_document, page_text
from .fetch import BlockedError, FetchError, Fetcher
from .naming import artifact_name, page_label
from .rules import (attachment_hint, attachment_name, excluded, extract_link_candidates,
                    match, matching_terms, navigation_noise, normalize_url, official_candidates,
                    related_resource_candidates, safe_name)
from .semantic import SemanticError, SemanticMatcher
from .submission_format import build_format_report, find_requirements, format_markdown


_RESOURCE_SECTIONS = ('作者园地', '下载中心', '下载专区', '资料下载', '投稿须知', '投稿指南',
                      'author resources', 'downloads', 'guide for authors', 'author guidelines')
_GENERIC_DOWNLOADS = ('附件', '下载', '模板', '模版', 'download', 'template')
_HOME_LINKS = {'首页', '期刊首页', '本刊首页', 'home', 'journal home'}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    temporary.replace(path)


def fingerprint(journal, settings):
    options = asdict(settings)
    for key in ('input', 'output', 'workers'):
        options.pop(key)
    data = {'version': __version__, 'journal': asdict(journal), 'settings': options}
    return sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode('utf-8'))


def cached_result(path, expected):
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
        if record['status'] != 'success' or record['fingerprint'] != expected or not record['artifacts']:
            return None
        entries = [entry for artifact in record['artifacts']
                   for entry in (artifact, artifact.get('extraction', {})) if 'path' in entry]
        entries.extend(record['format_report']['files'])
        for entry in entries:
            local = (path.parent / entry['path']).resolve()
            if not local.is_relative_to(path.parent.resolve()) or sha256(local.read_bytes()) != entry['sha256']:
                return None
        return dict(record, resumed=True)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def process_journal(journal, settings, resume=False, matcher=None):
    key = sha256(journal.id.encode('utf-8'))[:10]
    directory = f'{safe_name(journal.name)}_{journal.bucket}_{key}'
    outdir = settings.output / directory
    outdir.mkdir(parents=True, exist_ok=True)
    signature = fingerprint(journal, settings)
    manifest = outdir / 'manifest.json'
    cached = cached_result(manifest, signature) if resume else None
    if cached:
        return cached
    if settings.semantic and matcher is None:
        matcher = SemanticMatcher(settings)
    report = {'schema_version': 1, 'pipeline_version': __version__, 'journal': asdict(journal),
              'directory': directory, 'fingerprint': signature,
              'started_at': datetime.now(timezone.utc).isoformat(), 'status': 'failed',
              'artifacts': [], 'errors': [], 'warnings': [], 'visits': [], 'candidates': [],
              'limits_reached': False, 'resumed': False}
    sequence = count()
    queue = [(0, 0, next(sequence), url, '输入网址', {'method': 'input', 'source_url': None}) for url in journal.urls]
    queued, seen = dict.fromkeys(journal.urls, (0, 0)), set()
    name_claims = {}
    fetcher = Fetcher(settings)

    def enqueue(url, label, depth, source_url, selection, priority):
        normalized = normalize_url(url)
        record = {'source_url': source_url, 'candidate_url': normalized or url,
                  'label': label, 'depth': depth, 'priority': priority, **selection}
        if not normalized or excluded(urlsplit(normalized).hostname, settings.rules):
            record['queue_status'] = 'invalid_or_excluded'
        elif navigation_noise(normalized, label):
            record['queue_status'] = 'out_of_scope'
        elif normalized in seen or (normalized in queued and queued[normalized] <= (depth, priority)):
            record['queue_status'] = 'duplicate'
        elif depth > settings.max_depth:
            report['limits_reached'] = True
            record['queue_status'] = 'max_depth'
        else:
            record['queue_status'] = 'queued'
            queued[normalized] = depth, priority
            heappush(queue, (depth, priority, next(sequence), normalized, label, dict(selection, source_url=source_url)))
        report['candidates'].append(record)

    try:
        while queue and len(report['visits']) < settings.max_pages:
            depth, priority, _, url, label, selection = heappop(queue)
            if queued.get(url) != (depth, priority):
                continue
            del queued[url]
            if url in seen:
                continue
            seen.add(url)
            visit = {'url': url, 'label': label, 'depth': depth, 'selection': selection}
            report['visits'].append(visit)
            try:
                resource = fetcher.get(url)
                final = normalize_url(resource.url)
                if not final or excluded(urlsplit(final).hostname, settings.rules):
                    raise FetchError('重定向目标不在配置范围内')
                visit.update(final_url=final, rendered=resource.rendered)
                if resource.protocol_fallback:
                    visit['protocol_fallback'] = resource.protocol_fallback
                if final != url and final in seen:
                    visit['status'] = 'duplicate_redirect'
                    continue
                seen.add(final)
                noisy_page = navigation_noise(final, label)
                if noisy_page and selection['method'] != 'input':
                    visit['status'] = 'out_of_scope'
                    continue
                filename = attachment_name(final, resource.headers)
                if filename:
                    suffix = Path(filename).suffix.lower()
                    if suffix not in settings.rules['download_exts']:
                        raise FetchError(f'不支持的附件类型: {suffix or filename}')
                    if resource.text and ('html' in resource.text[:500].lower() or '<' in resource.text[:100]):
                        # Blocked pages must not be written as .doc/.pdf files.
                        page_text(resource.text)
                        raise FetchError('附件链接返回 HTML 页面，可能是登录页或错误页')
                    if not resource.content:
                        raise FetchError('附件内容为空')
                    name = artifact_name(journal.name, label, final, suffix, name_claims, filename)
                    path = outdir / name
                    path.write_bytes(resource.content)
                    extraction = extract_document(path)
                    if extraction.get('path'):
                        extraction['sha256'] = sha256((outdir / extraction['path']).read_bytes())
                    artifact = {'kind': 'attachment', 'label': label, 'source_url': url,
                                'final_url': final, 'path': name, 'bytes': len(resource.content),
                                'sha256': sha256(resource.content), 'extraction': extraction,
                                'depth': depth, 'selection': selection, 'journal_name': journal.name,
                                'document_title': label, 'original_filename': filename}
                    report['artifacts'].append(artifact)
                    if selection['method'] == 'semantic':
                        validation = {'method': 'unverified', 'accepted': False,
                                      'reason': '附件未提取出可供语义验证的足够文字'}
                        artifact.update(content_validation=validation, retained_for_review=True)
                        if extraction['status'] == 'ok':
                            attachment_text = (outdir / extraction['path']).read_text(encoding='utf-8')
                            if len(attachment_text.strip()) >= settings.min_text_length:
                                validation = matcher.validate_content(attachment_text)
                        artifact.update(content_validation=validation,
                                        retained_for_review=not validation['accepted'])
                        if not validation['accepted']:
                            report['errors'].append({
                                'url': url, 'stage': 'semantic',
                                'type': 'semantic_rejected' if validation['method'] == 'semantic' else 'semantic_unverified',
                                'message': '语义附件正文未通过验证或不可验证，原件已保留供人工复核'})
                    if extraction['status'] in ('failed', 'empty', 'unavailable'):
                        report['errors'].append({'url': url, 'stage': 'extract', 'type': extraction['status'],
                                                 'message': extraction['reason']})
                    visit['status'] = 'attachment'
                    continue
                if selection.get('action'):
                    page_text(resource.text)
                    raise FetchError('站点下载操作未返回可识别的附件')
                if not resource.text:
                    raise FetchError('无法识别的响应类型')
                text, evidence = page_text(resource.text)
                content_words = matching_terms(evidence, settings.rules['content'])
                validation = {'method': 'keyword', 'accepted': False, 'matched_words': content_words}
                if noisy_page:
                    validation['reason'] = '排行或普通论文入口，仅用于发现期刊首页'
                elif selection['method'] != 'semantic' and any(find_requirements(evidence)):
                    validation['accepted'] = True
                    validation['method'] = 'format_statement'
                elif len(evidence) < settings.min_text_length:
                    validation['reason'] = '正文字符数不足'
                elif content_words and selection['method'] != 'semantic':
                    validation['accepted'] = True
                elif matcher is not None:
                    validation = matcher.validate_content(evidence)
                else:
                    validation['reason'] = '未命中正文内容词'
                visit['content_validation'] = validation
                if validation['accepted']:
                    title = page_label(resource.text, label, settings.rules)
                    name = artifact_name(journal.name, title, final, '.txt', name_claims)
                    data = f'期刊: {journal.name}\n来源: {final}\n标题: {title}\n\n{text}\n'.encode('utf-8')
                    (outdir / name).write_bytes(data)
                    report['artifacts'].append({'kind': 'html', 'label': label, 'source_url': url,
                                                'final_url': final, 'path': name, 'bytes': len(data),
                                                'sha256': sha256(data), 'depth': depth,
                                                'selection': selection, 'content_validation': validation,
                                                'journal_name': journal.name, 'document_title': title})
                    visit['status'] = 'text_saved'
                else:
                    visit['status'] = 'no_content'
                links = extract_link_candidates(resource.text, final)
                if noisy_page:
                    home_links = [link for link in links if link['label'].strip().casefold() in _HOME_LINKS
                                  and urlsplit(link['url']).hostname == urlsplit(final).hostname
                                  and link['url'] != final and not navigation_noise(link['url'])]
                    if home_links:
                        for link in home_links:
                            enqueue(link['url'], link['label'], depth + 1, final,
                                    {'method': 'journal_home', 'parent_label': label}, 0)
                        continue
                rule_targets = set()
                for link in links:
                    title, target = link['label'], link['url']
                    level = match(title, settings.rules)
                    direct = attachment_hint(target)
                    generic = matching_terms(title, _GENERIC_DOWNLOADS)
                    local_section = link['section'] or link['nearby']
                    in_section = (match(label, settings.rules) == 'strong'
                                  or bool(matching_terms(local_section, _RESOURCE_SECTIONS)))
                    if navigation_noise(target, local_section) and not level:
                        continue
                    if navigation_noise(target, title) or level or (in_section and (direct or generic)):
                        rule_targets.add(target)
                        words = matching_terms(title, settings.rules[level]) if level else []
                        method = 'site_action' if link.get('action') else ('keyword' if level else 'attachment')
                        rank = 1 if level == 'strong' else (2 if direct or level == 'content' else 4)
                        candidate_selection = {'method': method, 'level': level, 'matched_words': words,
                                               'parent_label': label}
                        if link.get('action'):
                            candidate_selection['action'] = link['action']
                        enqueue(target, title or '附件', depth + 1, final, candidate_selection, rank)
                for resource_candidate in related_resource_candidates(resource.text, final, settings.rules):
                    target = resource_candidate['url']
                    if target not in rule_targets:
                        rule_targets.add(target)
                        decision = {k: v for k, v in resource_candidate.items() if k not in ('url', 'label')}
                        enqueue(target, resource_candidate['label'], depth + 1, final,
                                dict(decision, method='resource_hint', parent_label=label), 2)
                if settings.discover_official:
                    for target in official_candidates(resource.text, final):
                        rule_targets.add(target)
                        enqueue(target, '官网候选', depth + 1, final,
                                {'method': 'official_hint', 'reason': '页面包含官网/投稿网站提示；未经人工认证'}, 1)
                if matcher is not None:
                    unmatched = [c for c in links
                                 if c['url'] not in rule_targets and c['url'] not in seen and c['url'] not in queued
                                 and not excluded(urlsplit(c['url']).hostname, settings.rules)
                                 and not navigation_noise(c['url'], c['label'])]
                    semantic_hits = matcher.select_links(unmatched)
                    visit['semantic_scan'] = {'unmatched_links': len(unmatched),
                                              'evaluated_links': min(len(unmatched), settings.semantic_candidate_limit),
                                              'selected_links': len(semantic_hits)}
                    if len(unmatched) > settings.semantic_candidate_limit:
                        report['warnings'].append({'stage': 'semantic', 'url': final,
                                                   'message': f'未匹配链接较多，只评估前 {settings.semantic_candidate_limit} 条'})
                    for candidate in semantic_hits:
                        decision = {k: v for k, v in candidate.items() if k not in ('url', 'label')}
                        enqueue(candidate['url'], candidate['label'] or '语义候选', depth + 1, final, decision, 3)
            except Exception as error:
                kind = ('blocked' if isinstance(error, BlockedError) else
                        'semantic_failed' if isinstance(error, SemanticError) else 'fetch_failed')
                visit['status'] = kind
                report['errors'].append({'url': url, 'stage': 'semantic' if kind == 'semantic_failed' else 'collect', 'type': kind,
                                         'message': f'{type(error).__name__}: {error}'[:1000]})
    finally:
        fetcher.close()
    if queued.keys() - seen:
        report['limits_reached'] = True
    report['warnings'].extend(fetcher.warnings)
    report['crawl_stats'] = {
        'visited_urls': len(report['visits']),
        'max_attempted_depth': max((v['depth'] for v in report['visits']), default=None),
        'max_fetched_depth': max((v['depth'] for v in report['visits'] if 'final_url' in v), default=None),
        'max_saved_depth': max((a['depth'] for a in report['artifacts']), default=None)}
    if report['artifacts']:
        report['status'] = 'partial' if report['errors'] or report['limits_reached'] else 'success'
    elif any(error['type'] == 'blocked' for error in report['errors']):
        report['status'] = 'blocked'
    elif report['errors']:
        report['status'] = 'failed'
    else:
        report['status'] = 'no_match'
    report['finished_at'] = datetime.now(timezone.utc).isoformat()
    format_report = build_format_report(report, outdir)
    atomic_json(outdir / 'submission-format.json', format_report)
    (outdir / 'submission-format.md').write_text(format_markdown(format_report), encoding='utf-8')
    report['format_report'] = {'requirements': len(format_report['requirements']),
                               'review': len(format_report['review']) + len(format_report['possible_conflicts']),
                               'files': [{'path': name, 'sha256': sha256((outdir / name).read_bytes())}
                                         for name in ('submission-format.json', 'submission-format.md')]}
    atomic_json(manifest, report)
    lines = [f"期刊: {journal.name} ({journal.id})", f"状态: {report['status']}",
             f"访问: {len(report['visits'])}，产物: {len(report['artifacts'])}",
             f"深度统计: {json.dumps(report['crawl_stats'], ensure_ascii=False)}",
             f"达到遍历限制: {report['limits_reached']}", '', '采集清单:']
    lines.append(f"格式条目: {report['format_report']['requirements']}，详见 submission-format.md")
    lines.extend(f"[{a['label']}] {a['final_url']}\n  -> {a['path']}" for a in report['artifacts'])
    lines.extend(['', '候选链接与选择原因:'] + [json.dumps(c, ensure_ascii=False) for c in report['candidates']])
    lines.extend(['', '错误与提示:'] + [json.dumps(e, ensure_ascii=False) for e in report['errors'] + report['warnings']])
    (outdir / 'manifest.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return report
