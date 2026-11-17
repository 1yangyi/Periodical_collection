import re
from email.message import Message
from pathlib import PurePosixPath
from urllib.parse import parse_qs, unquote, urlencode, urljoin, urlsplit, urlunsplit

from bs4 import BeautifulSoup, NavigableString, Tag


_ATTACHMENT_EXTENSIONS = ('.doc', '.docx', '.pdf', '.wps', '.zip', '.rar')
_NOISE_PATHS = {'downloadtoplist.aspx', 'kbdownload.aspx', 'top_down', 'top_view', 'top_cited'}
_NOISE_LABELS = {'下载排行', '全文下载排行', '浏览排行', '引用排行', '被引频次排行',
                 '摘要查看排行', '过刊浏览', '本期目次', '期刊订阅', '期刊封面',
                 '网络首发', '订购指南', '审稿指南', 'most downloaded', 'most read', 'most cited',
                 'current issue', 'online first', 'archives'}
_GUID = r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}'
_CNKI_WEB_PUBLICATION = re.compile(r'/(WKE|WKG)/WebPublication/', re.I)
_CNKI_DOWNLOAD = re.compile(
    rf'''\s*downFileContentTypeTwo\s*\(\s*(['"])({_GUID})\1\s*,\s*(['"])([a-z0-9_-]+)\3\s*,\s*(['"])({_GUID})\5\s*\)\s*;?\s*''',
    re.I,
)


def navigation_noise(url: str, label: str = '') -> bool:
    path = PurePosixPath(unquote(urlsplit(url).path).lower())
    label = ' '.join(label.split()).casefold()
    return (path.name in _NOISE_PATHS or path.name.startswith('volumn_')
            or path.suffix in {'.wmv', '.mp4', '.avi', '.mp3'}
            or label in _NOISE_LABELS or label.startswith(('编辑办公入口', '专家办公入口')))


def attachment_hint(url: str) -> str | None:
    parts = urlsplit(url)
    query = parse_qs(parts.query)
    for value in [unquote(parts.path), *query.get('file_name', []), *query.get('filename', [])]:
        name = PurePosixPath(value.replace('\\', '/')).name
        if name.lower().endswith(_ATTACHMENT_EXTENSIONS):
            return name
    return None

def normalize_url(value: str, base: str | None = None) -> str | None:
    value = str(value or '').strip().strip('“”<>').rstrip('。，；')
    if not value or value.startswith('#') or re.search(r'\s', value):
        return None
    if base:
        value = urljoin(base, value)
    elif value.startswith('//'):
        value = 'https:' + value
    elif not re.match(r'^[a-zA-Z][\w+.-]*:', value):
        value = 'https://' + value
    try:
        parts = urlsplit(value)
        if parts.scheme.lower() not in ('http', 'https') or not parts.hostname:
            return None
        if parts.username or parts.password or not parts.port and parts.netloc.endswith(':'):
            return None
        host = parts.hostname.lower()
        if '.' not in host and host != 'localhost' and ':' not in host:
            return None
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path or '/', parts.query, ''))
    except ValueError:
        return None


def excluded(host: str, rules: dict) -> bool:
    host = host.lower()
    return host.endswith('.tw') or any(k in host for k in rules['exclude_markers'])


def classify(host: str, rules: dict) -> str:
    if any(host == k or host.endswith('.' + k) for k in rules['cnki_markers']):
        return '知网系'
    if host.endswith('.edu.cn'):
        return '高校自建'
    if any(k in host for k in rules['pro_platform_markers']):
        return '专业平台'
    return '长尾'


def matching_terms(text: str, terms: list[str]) -> list[str]:
    compact = re.sub(r'\s+', '', text or '').casefold()
    return [term for term in terms if re.sub(r'\s+', '', term).casefold() in compact]


def match(text: str, rules: dict) -> str | None:
    for level in ('strong', 'content', 'weak'):
        if len(matching_terms(text, rules[level])) >= (2 if level == 'weak' else 1):
            return level
    return None


def _anchor_destination(anchor, base, page_url):
    href = anchor['href'].strip()
    target = normalize_url(href, base)
    if target or href not in ('', '#'):
        return target, None
    page = urlsplit(page_url)
    platform = _CNKI_WEB_PUBLICATION.match(page.path)
    if not (platform and page.hostname and page.hostname.endswith('.cbpt.cnki.net')):
        return None, None
    handler = _CNKI_DOWNLOAD.fullmatch(anchor.get('onclick', ''))
    if not handler:
        return None, None
    query = urlencode(dict(fid=handler[2], mid=handler[4], cid=handler[6]))
    download_path = f'/{platform[1].upper()}/WebPublication/wkDownFileByLink.aspx'
    url = page._replace(path=download_path, query=query, fragment='').geturl()
    return url, 'downFileContentTypeTwo'


def extract_links(html: str, base: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(html, 'html.parser')
    page_url = base
    base_tag = soup.find('base', href=True)
    if base_tag:
        base = normalize_url(base_tag['href'], base) or base
    result, seen = [], set()
    for anchor in soup.find_all('a', href=True):
        url, _ = _anchor_destination(anchor, base, page_url)
        if url and url not in seen:
            seen.add(url)
            text = anchor.get_text(' ', strip=True) or anchor.get('title', '')
            result.append((text, url))
    return result


def related_resource_candidates(html: str, base: str, rules: dict) -> list[dict]:
    """Follow explicit resource cues near a link, including plaintext external URLs."""
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup(['script', 'style', 'noscript']):
        tag.decompose()
    base_tag = soup.find('base', href=True)
    if base_tag:
        base = normalize_url(base_tag['href'], base) or base
    hints = rules.get('resource_hints', rules['strong'])
    records, seen = [], set()

    def add(raw, context, label, reason):
        url = normalize_url(raw, base)
        terms = matching_terms(context, hints)
        if not url or url in seen or not terms:
            return
        seen.add(url)
        purpose = label if matching_terms(label, hints) else max(terms, key=len)
        records.append({'url': url, 'label': purpose, 'context': context[:500],
                        'reason': reason, 'matched_words': terms})

    def local_context(anchor):
        container = anchor.find_parent(['p', 'li', 'td', 'dd', 'div']) or anchor.parent
        if len(container.find_all('a', href=True)) > 5 or len(container.get_text()) > 1000:
            return ''
        nodes = list(container.descendants)
        position = next(index for index, node in enumerate(nodes) if node is anchor)
        own_nodes = {id(node) for node in anchor.descendants}
        before, after = [], []
        for node in reversed(nodes[:position]):
            if isinstance(node, Tag) and node.name == 'a' and node.has_attr('href'):
                break
            if isinstance(node, NavigableString):
                if node.find_parent('a', href=True):
                    break
                before.append(str(node))
        for node in nodes[position + 1:]:
            if id(node) in own_nodes:
                continue
            if isinstance(node, Tag) and node.name == 'a' and node.has_attr('href'):
                break
            if isinstance(node, NavigableString):
                if node.find_parent('a', href=True):
                    break
                after.append(str(node))
        return ' '.join([*reversed(before), anchor.get_text(' ', strip=True), *after])[:500]

    for anchor in soup.find_all('a', href=True):
        label = anchor.get_text(' ', strip=True) or anchor.get('title', '')
        compact_label = re.sub(r'\s+', '', label).lower()
        generic = (not compact_label or compact_label in {'更多', '详情', '了解更多', '点此', '点击', 'here', 'link'}
                   or re.fullmatch(r'(?:点击|点此|请|立即)?(?:查看|下载|访问|获取|进入|打开)(?:详情|文件|资料|附件|链接|使用)?', compact_label)
                   or re.match(r'^(?:https?://|www\.)', compact_label))
        if generic or matching_terms(label, hints):
            add(anchor['href'], local_context(anchor), label, '链接附近有投稿资料提示')

    pattern = re.compile(r'(?:https?://|www\.)[^\s<>"\'，。；、（）()\[\]{}]+', re.I)
    blocks = soup.find_all(['p', 'li', 'td', 'dd', 'div'])
    if not blocks and soup.body:
        blocks = [soup.body]
    for block in blocks:
        if block.find(['p', 'li', 'td', 'dd', 'div'], recursive=False):
            continue
        if len(block.get_text()) > 1000:
            continue
        # Do not borrow an anchor's label as evidence for another URL in the paragraph.
        segments, current = [], []
        for node in block.descendants:
            if isinstance(node, Tag) and node.name == 'a' and node.has_attr('href'):
                segments.append(' '.join(current))
                current = []
            elif isinstance(node, NavigableString) and not node.find_parent('a', href=True):
                current.append(str(node))
        segments.append(' '.join(current))
        for segment in segments:
            previous_end = 0
            for found in pattern.finditer(segment):
                context = segment[previous_end:found.end()]
                previous_end = found.end()
                raw = found.group(0).rstrip('.,;:')
                # An explicit bare www address gets HTTPS, rather than a relative path.
                url = normalize_url(raw) if raw.lower().startswith('www.') else normalize_url(raw, base)
                add(url, context, '', '正文投稿资料提示附近的明文网址')
    return records


def extract_link_candidates(html: str, base: str) -> list[dict]:
    """Context for real links and explicitly supported site download actions."""
    soup = BeautifulSoup(html, 'html.parser')
    page_url = base
    base_tag = soup.find('base', href=True)
    if base_tag:
        base = normalize_url(base_tag['href'], base) or base
    records, seen = [], set()
    for anchor in soup.find_all('a', href=True):
        url, action = _anchor_destination(anchor, base, page_url)
        if not url or url in seen:
            continue
        seen.add(url)
        label = (anchor.get_text(' ', strip=True) or anchor.get('title', '')
                 or anchor.get('aria-label', ''))[:120]
        title = anchor.get('title', '')[:100]
        parent = anchor.parent
        nearby = parent.get_text(' ', strip=True)[:240] if len(parent.find_all('a')) <= 3 else ''
        heading = anchor.find_previous(['h1', 'h2', 'h3', 'h4', 'h5', 'h6'])
        section = heading.get_text(' ', strip=True)[:80] if heading else ''
        path = unquote(urlsplit(url).path)[:160]
        context = f'链接：{label}\n提示：{title}\n栏目：{section}\n附近文字：{nearby}\n路径：{path}'
        record = {'label': label, 'url': url, 'context': context, 'section': section, 'nearby': nearby}
        if action:
            record['action'] = action
        records.append(record)
    return records


def safe_name(value: str) -> str:
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(value)).strip(' .')[:65]
    if re.match(r'^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\.|$)', value, re.I):
        value = '_' + value
    return value or 'unnamed'


def attachment_name(url: str, headers: dict) -> str | None:
    headers = {k.lower(): v for k, v in headers.items()}
    message = Message()
    message['Content-Disposition'] = headers.get('content-disposition', '')
    filename = message.get_filename()
    if filename:
        return unquote(filename).replace('\\', '/').rsplit('/', 1)[-1]
    name = unquote(PurePosixPath(urlsplit(url).path).name)
    if name.lower().endswith(_ATTACHMENT_EXTENSIONS):
        return name
    mime = headers.get('content-type', '').split(';')[0].lower()
    hinted = attachment_hint(url)
    if hinted and mime in ('', 'application/octet-stream'):
        return hinted
    extension = {'application/pdf': '.pdf', 'application/msword': '.doc',
                 'application/vnd.openxmlformats-officedocument.wordprocessingml.document': '.docx',
                 'application/zip': '.zip', 'application/x-rar-compressed': '.rar'}.get(mime)
    return 'attachment' + extension if extension else None


def official_candidates(html: str, base: str) -> list[str]:
    cues = ('官方网站', '期刊官网', '投稿网站', '本刊网站', '本刊网址', '编辑部网站')
    found = [url for label, url in extract_links(html, base) if any(k in label for k in cues)]
    text = BeautifulSoup(html, 'html.parser').get_text(' ', strip=True)
    for phrase in re.finditer(r'(?:官方网站|期刊官网|投稿网站|本刊网站|本刊网址|编辑部网站)[:：\s]*([^\n]{1,160})', text):
        candidate = re.search(r'(?:https?://|www\.)[^\s<>，。；（）()]+', phrase.group(1))
        if candidate:
            url = normalize_url(candidate.group(0).rstrip('.,;'))
            if url:
                found.append(url)
    return list(dict.fromkeys(found))
