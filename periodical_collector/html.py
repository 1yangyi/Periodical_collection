"""Discover submission links and copy public, inline formatting guidance."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import quote, unquote, urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Comment, NavigableString, Tag


_GUIDE = re.compile(
    r"投稿\s*(?:须知|指南|说明|要求|格式|模板|细则|规范)|"
    r"来稿\s*(?:须知|说明|要求|格式|细则|规范)|"
    r"作者\s*(?:须知|指南|说明|投稿指南|服务)|"
    r"(?:稿件|文稿|论文|文章|撰稿|写作)\s*(?:格式|模板|体例|规范|要求)|"
    r"(?:编排|排版|引文|注释|著录)\s*(?:格式|体例|规范|要求)|"
    r"(?:征稿|约稿)\s*(?:启事|通知|简则|简章)|"
    r"author\s*(?:guide(?:lines)?|instructions)|"
    r"(?:submission|manuscript)\s*(?:guide(?:lines)?|instructions|format|template)",
    re.IGNORECASE,
)
_LISTING = re.compile(
    r"下载\s*(?:中心|专区|索引)|资料下载|表格下载|模板下载|"
    r"通知公告|通知通告|财经公告|公告栏|最新公告|期刊公告|编辑部公告|"
    r"专题\s*(?:征稿|投稿)|作者服务|downloads?|announcements?",
    re.IGNORECASE,
)
_OFFICIAL = re.compile(
    r"(?:(?:本刊|期刊|杂志|编辑部|官方)(?:的)?(?:官方)?(?:网站|网址|主页|首页|平台)|官网|"
    r"(?:官方|本刊的?|我刊的?)?投稿平台|official\s*(?:website|site))",
    re.IGNORECASE,
)
_GENERIC_ATTACHMENT = re.compile(
    r"附件|下载|文档|文件|原文|格式要求|格式说明|格式规范|格式示例|模板|"
    r"(?:提交|录用)表单|审查表|样例|download|template|attachment",
    re.IGNORECASE,
)
_AMBIGUOUS = re.compile(r"审查表|授权(?:书|协议)|版权(?:转让|协议)|伦理声明|作者声明|录用后")
_PAPER = re.compile(r"^PDF$|^CAJ$|^HTML$|全文(?:下载)?|论文正文|下载本文|阅读原文|论文汇编")
_LOGIN = re.compile(r"登录|注册|审稿系统|投稿系统|在线投稿|作者投稿|用户中心|\b(?:login|sign.in)\b", re.IGNORECASE)
_DETAIL = re.compile(
    r"字体|字号|宋体|黑体|楷体|行距|页边距|段前|段后|"
    r"页眉|页脚|脚注|尾注|\b[aA]4\b|"
    r"关键词|内容提要|作者简介|参考文献|文献著录|"
    r"(?:正文|摘要|全文|篇幅|稿件|标题).{0,14}(?:\d+\s*字|字数|使用|采用|应|须|格式为|必须|限于)|"
    r"\d+\s*字(?:以内|左右|以下|以上)|"
    r"(?:提交|上传|请用|采用).{0,15}(?:Word|DOCX?|PDF|LaTeX)|"
    r"(?:匿名|双盲)(?:稿件|投稿|上传|提交|评审)|"
    r"font\s*(?:size|family)|line\s*spacing|manuscript\s*length|"
    r"(?:submit|upload).{0,30}(?:word|docx?|pdf|latex)",
    re.IGNORECASE,
)
_NEXT = re.compile(
    r"^(?:下一页|下页|尾页|末页|最后一页|后页|下一章|next|last|[›»>]+|\d{1,4})$",
    re.IGNORECASE,
)
_FILE = re.compile(r"\.(?:docx?|pdf|zip|rar|7z|tex|dotx?|rtf|odt|wps|xlsx?)$", re.IGNORECASE)
_DOWNLOAD_URL = re.compile(
    r"download(?:attach(?:url)?|file)?(?:[./_?=&]|$)|wbfileid=|/attachment/|/file/down",
    re.IGNORECASE,
)
_BLOCKS = {
    "address", "article", "blockquote", "dd", "div", "dl", "dt", "form",
    "h1", "h2", "h3", "h4", "h5", "h6", "hr", "li", "main", "ol",
    "p", "pre", "section", "table", "ul",
}


@dataclass(frozen=True)
class Link:
    url: str
    label: str
    kind: str
    priority: int
    context: str = ""


@dataclass
class HtmlPage:
    title: str
    markdown: str
    links: list[Link]
    dynamic_controls: list[str] = field(default_factory=list)
    published_at: str = ""


def _compact(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _label(tag: Tag) -> str:
    for value in (tag.get_text(" ", strip=True), tag.get("aria-label"), tag.get("title"), tag.get("value")):
        if value:
            return _compact(str(value))
    return ""


def _source_url(base: str, raw: str) -> str:
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return ""
    try:
        url = urldefrag(urljoin(base, raw))[0]
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
            return ""
        if parts.port is not None and not (1 <= parts.port <= 65535):
            return ""
        path = quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        query = quote(parts.query, safe="/%:?@!$&'()*+,;=-._~")
        return parts._replace(path=path, query=query).geturl()
    except ValueError:
        return ""


def _context(anchor: Tag) -> str:
    for parent in anchor.parents:
        if parent.name in ("p", "li", "td", "div"):
            surrounding = _compact(parent.get_text(" ", strip=True))
            if surrounding and len(surrounding) <= 240 and len(parent.find_all("a", limit=4)) <= 3:
                return surrounding
            break
    return _label(anchor)


def _pagination(anchor: Tag, text: str, url: str) -> bool:
    if "next" in anchor.get("rel", []):
        return True
    if not _NEXT.fullmatch(text):
        return False
    if not text.isdigit():
        return True
    return bool(
        re.search(r"(?:[?&](?:page|pageno|currpage|curpage|pagenum)=|[_/\-]\d+\.(?:s?html?|aspx?)(?:\?|$))", url, re.IGNORECASE)
    )


def _link(anchor: Tag, base: str, relevant: bool) -> Link | None:
    label = _label(anchor)
    url = next((
        candidate
        for attribute in ("href", "data-url", "data-href")
        if (candidate := _source_url(base, str(anchor.get(attribute) or "")))
    ), "")
    if not url:
        return None
    context = _context(anchor)
    decoded = unquote(url)
    destination = urlsplit(decoded)
    same_host = destination.hostname == urlsplit(base).hostname
    basename = destination.path.rsplit("/", 1)[-1]
    evidence = f"{label} {basename}"
    direct_guide = bool(_GUIDE.search(evidence))
    document = bool(anchor.has_attr("download") or _FILE.search(destination.path) or _DOWNLOAD_URL.search(decoded))
    if document:
        if _AMBIGUOUS.search(evidence):
            if relevant or direct_guide:
                return Link(url, label or basename, "review", 50, context)
            return None
        if direct_guide or (relevant and _GUIDE.search(context) and not _PAPER.search(label)):
            return Link(url, label or basename, "attachment", 100, context)
        if relevant and not _PAPER.search(label):
            if re.search(r"模板|格式|排版|体例|template", evidence, re.IGNORECASE):
                return Link(url, label or basename, "attachment", 95, context)
            return Link(url, label or basename, "review", 50, context)
        return None
    if _LOGIN.search(label):
        # A journal can link to another platform's public landing page.
        # Actual sign-in routes and form actions stay outside discovery.
        if same_host or _LOGIN.search(unquote(destination.path)) or re.search(
            r"/(?:login|signin|register)(?:[/.?]|$)", destination.path, re.IGNORECASE
        ):
            return None
    if not same_host and (_OFFICIAL.search(label) or _OFFICIAL.search(context)):
        return Link(url, label or basename or url, "official", 80, context)
    if direct_guide:
        return Link(url, label or basename, "page", 85, context)
    if _LISTING.search(label) or label in ("通知", "公告", "下载"):
        return Link(url, label or basename, "page", 65, context)
    if relevant and same_host and (_pagination(anchor, label, url) or label in ("更多", "查看更多", "查看全部")):
        return Link(url, label or basename, "page", 55, context)
    return None


_TITLES = (
    ".t2_tt", ".paperDetail_tt", ".paperDetail_title", ".paperDetail_title_text", "article h1",
    "main h1", ".article-title", ".news-title", "h1", "h2",
)
_BODY = (
    ".t2_main", ".paperDetail_html.pd1", "#vsb_content", "#vsb_content_2",
    ".v_news_content", "#zoom", "#article-content", ".article-content",
    ".news-content", ".article-body", ".detail-content", '[itemprop="articleBody"]',
    "article", "main", ".content", "body",
)
_CHROME = (
    "script, style, noscript, nav, footer, aside, form, iframe, header, "
    "[hidden], [aria-hidden=true], [role=navigation], [role=banner], "
    ".breadcrumb, .breadcrumbs, .footer, .navigation, .share, .sidebar"
)


def _page_title(soup: BeautifulSoup, hint: str) -> str:
    candidates = [
        _label(tag)
        for selector in _TITLES
        for tag in soup.select(selector, limit=3)
    ]
    if soup.title:
        candidates.append(_label(soup.title))
    return next(
        (title for title in candidates if title and len(title) <= 180 and _GUIDE.search(title)),
        next((title for title in candidates if title and len(title) <= 180), hint),
    )


def _inline(node: Tag | NavigableString) -> str:
    if isinstance(node, Comment):
        return ""
    if isinstance(node, NavigableString):
        return re.sub(r"\s+", " ", str(node))
    if node.name == "br":
        return "\n"
    if node.name == "img":
        return str(node.get("alt", ""))
    return "".join(_inline(child) for child in node.children)


def _table(table: Tag) -> str:
    rows = [
        [_compact(_inline(cell)).replace("|", "\\|") for cell in row.find_all(["td", "th"], recursive=False)]
        for row in table.find_all("tr")
    ]
    rows = [row for row in rows if row]
    if not rows:
        return ""
    width = max(map(len, rows))
    first_row = table.find("tr")
    if first_row and not first_row.find("th", recursive=False):
        rows.insert(0, [""] * width)
    rows.insert(1, ["---"] * width)
    return "\n".join("| " + " | ".join(row + [""] * (width - len(row))) + " |" for row in rows)


def _list(tag: Tag, level: int = 0) -> str:
    try:
        start = int(tag.get("start", 1))
    except (ValueError, TypeError):
        start = 1
    lines = []
    for index, item in enumerate(tag.find_all("li", recursive=False), start=start):
        text = _compact("".join(_inline(child) for child in item.children if child.name not in ("ul", "ol")))
        bullet = f"{index}." if tag.name == "ol" else "-"
        lines.append(f'{"    " * level}{bullet} {text}'.rstrip())
        for nested in item.find_all(["ul", "ol"], recursive=False):
            lines.append(_list(nested, level + 1))
    return "\n".join(lines)


def _render(node: Tag) -> str:
    name = node.name
    if name in ("ul", "ol"):
        return _list(node)
    if name == "table":
        return _table(node)
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        return "#" * int(name[1]) + " " + _compact(_inline(node))
    if name == "hr":
        return "---"
    if name == "pre":
        return "\n".join(("```", node.get_text().strip(), "```"))
    if name == "blockquote":
        return "\n".join("> " + line for line in _render_children(node).splitlines())
    return _render_children(node)


def _render_children(node: Tag) -> str:
    chunks: list[str] = []
    inline: list[str] = []

    def flush_inline() -> None:
        text = "\n".join(_compact(line) for line in "".join(inline).splitlines()).strip()
        if text:
            chunks.append(text)
        inline.clear()

    for child in node.children:
        if child.name in _BLOCKS:
            flush_inline()
            block = _render(child)
            if block:
                chunks.append(block)
        else:
            inline.append(_inline(child))
    flush_inline()
    return "\n\n".join(chunks)


def _inline_guidance(soup: BeautifulSoup) -> str:
    body = next((found for selector in _BODY if (found := soup.select_one(selector))), soup)
    clean = BeautifulSoup(str(body), "html.parser")
    for tag in clean.select(_CHROME):
        if tag.parent is not None:
            tag.decompose()
    visible = clean.get_text(" ", strip=True)
    if not _DETAIL.search(visible):
        return ""
    return _render_children(clean).strip()


def _published_at(soup: BeautifulSoup) -> str:
    tag = soup.select_one(
        'meta[property="article:published_time"], meta[name="citation_publication_date"], '
        '[itemprop="datePublished"], .paperDetail_publishTimeDate, .t2_date, '
        '.publish-date, .publish-time, article time[datetime]'
    )
    if not tag:
        return ""
    value = str(tag.get("content") or tag.get("datetime") or tag.get_text(" ", strip=True))
    match = re.search(r"(\d{4})[.年/-](\d{1,2})[.月/-](\d{1,2})", value)
    if not match:
        return ""
    year, month, day = (int(part) for part in match.groups())
    return f"{year:04d}-{month:02d}-{day:02d}" if 1 <= month <= 12 and 1 <= day <= 31 else ""


def analyze_html(content: bytes, url: str, hint: str = "") -> HtmlPage:
    try:
        decoded: str | bytes = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        decoded = content
    soup = BeautifulSoup(decoded, "html.parser")
    title = _page_title(soup, hint)
    relevant = bool(_GUIDE.search(f"{title} {hint}"))
    listing = relevant or bool(_LISTING.search(f"{title} {hint}"))
    base = _source_url(url, url) or url
    links: dict[tuple[str, str], Link] = {}
    dynamic_controls: list[str] = []
    for tag in soup.find_all(["a", "area", "button", "input"]):
        link = _link(tag, base, listing) if tag.name in ("a", "area") else None
        if link and link.url != base:
            key = link.url, link.kind
            if key not in links or link.priority > links[key].priority:
                links[key] = link
        elif not link:
            label = _label(tag)
            confirmed_control = _GUIDE.search(label) or _LISTING.search(label)
            related_download = relevant and _GENERIC_ATTACHMENT.search(label) and not _LOGIN.search(label)
            if (confirmed_control or related_download) and (
                tag.name == "button" or tag.get("onclick") or str(tag.get("href", "")).lower().startswith("javascript:")
            ):
                if label not in dynamic_controls:
                    dynamic_controls.append(label)
    body = _inline_guidance(soup) if relevant else ""
    return HtmlPage(
        title, body, sorted(links.values(), key=lambda link: -link.priority),
        dynamic_controls, _published_at(soup),
    )
