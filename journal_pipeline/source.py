import hashlib
from pathlib import Path
from urllib.parse import urlsplit

import openpyxl

from .models import Journal
from .rules import classify, excluded, normalize_url


def journal_from_url(value: str, settings, name: str | None = None) -> tuple[list[Journal], dict]:
    url = normalize_url(value)
    if not url:
        raise ValueError('网址无效，请填写 http:// 或 https:// 网站地址，也可省略协议')
    host = urlsplit(url).hostname
    if excluded(host, settings.rules):
        raise ValueError('网址被当前域名范围规则排除，请检查 config/rules.json')
    label = name.strip() if name is not None else host
    if not label:
        raise ValueError('--name 不能为空')
    identifier = 'url-' + hashlib.sha256(url.encode('utf-8')).hexdigest()[:16]
    journal = Journal(identifier, label, [url], classify(host, settings.rules))
    audit = {'input_type': 'url', 'rows': 1, 'accepted': 1, 'skipped': [],
             'filtered_urls': [], 'url': url}
    return [journal], audit


def load_journals(path: Path, settings) -> tuple[list[Journal], dict]:
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    audit = {'rows': 0, 'accepted': 0, 'skipped': [], 'filtered_urls': []}
    journals, ids = [], set()
    try:
        if settings.sheet not in workbook.sheetnames:
            raise ValueError(f'工作表不存在: {settings.sheet}；可用: {workbook.sheetnames}')
        rows = iter(workbook[settings.sheet].iter_rows(values_only=True))
        header = [str(c or '').strip() for c in next(rows, [])]
        required = ('journal_id', '期刊名', '官网')
        if any(name not in header for name in required):
            raise ValueError(f'表头必须包含 {required}，实际为 {header}')
        columns = {name: header.index(name) for name in required}
        for row_number, row in enumerate(rows, 2):
            if not any(c is not None for c in row):
                continue
            audit['rows'] += 1
            def cell(name):
                pos = columns[name]
                return str(row[pos] or '').strip() if pos < len(row) else ''
            name, jid = cell('期刊名'), cell('journal_id')
            reason = None
            if not name or not jid:
                reason = 'missing_name_or_id'
            elif jid in ids:
                reason = 'duplicate_id'
            urls = []
            for raw in cell('官网').splitlines():
                url = normalize_url(raw)
                why = 'invalid_url' if not url else ('excluded_domain' if excluded(urlsplit(url).hostname, settings.rules) else None)
                if why:
                    audit['filtered_urls'].append({'row': row_number, 'url': raw, 'reason': why})
                elif url not in urls:
                    urls.append(url)
            if not urls:
                reason = reason or 'no_usable_url'
            if reason:
                audit['skipped'].append({'row': row_number, 'id': jid, 'name': name, 'reason': reason})
                continue
            ids.add(jid)
            journals.append(Journal(jid, name, urls, classify(urlsplit(urls[0]).hostname, settings.rules)))
    finally:
        workbook.close()
    audit['accepted'] = len(journals)
    return journals, audit
