"""Human-readable journal-based artifact names with collision protection."""
import hashlib
import re
from pathlib import Path

from bs4 import BeautifulSoup

from .rules import match, safe_name

GENERIC_LABELS = {'', '输入网址', '官网候选', '附件', '语义候选', '点击查看', '查看', '点击下载', '下载', '更多'}


def page_label(html: str, incoming: str, rules: dict) -> str:
    if incoming.strip() not in GENERIC_LABELS:
        return incoming.strip()
    soup = BeautifulSoup(html, 'html.parser')
    content = soup.find('main') or soup.find('article') or soup
    for heading in content.find_all(['h1', 'h2', 'h3']):
        label = heading.get_text(' ', strip=True)
        if match(label, rules):
            return label
    title = soup.title.get_text(' ', strip=True) if soup.title else ''
    return title or '投稿资料'


def artifact_name(journal_name: str, label: str, url: str, suffix: str,
                  claims: dict[str, str], original_filename: str | None = None) -> str:
    label = label.strip()
    if label in GENERIC_LABELS:
        label = Path(original_filename).stem if original_filename else '投稿资料'
        if label.isdigit():
            label = '附件'
    label = re.sub(r'\.(?:docx?|pdf|wps|zip|rar)$', '', label, flags=re.I)
    label = label.replace(journal_name, '').strip(' _-—:：') or '投稿资料'
    journal = safe_name(journal_name)
    material = safe_name(label)
    stem = journal + '_' + material[:max(1, 90 - len(journal) - 1)]
    result = stem + suffix
    owner = claims.get(result.casefold())
    if owner is not None and owner != url:
        digest = hashlib.sha256(url.encode('utf-8')).hexdigest()[:10]
        result = f'{stem}_{digest}{suffix}'
        counter = 2
        while result.casefold() in claims and claims[result.casefold()] != url:
            result = f'{stem}_{digest}_{counter}{suffix}'
            counter += 1
    claims[result.casefold()] = url
    return result
