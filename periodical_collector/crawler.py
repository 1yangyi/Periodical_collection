from __future__ import annotations

import hashlib
import heapq
import io
import json
import re
import zipfile
from dataclasses import asdict, dataclass
from datetime import datetime
from itertools import count
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zoneinfo import ZoneInfo

from .html import analyze_html
from .http import DownloadError, HttpClient, Resource, allowed_url, canonical_url, host_for


@dataclass(frozen=True)
class Journal:
    name: str
    url: str
    journal_id: str = ""
    row: int | None = None
    extra_hosts: tuple[str, ...] = ()
    extra_urls: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrawlConfig:
    max_pages: int = 30
    max_depth: int = 3
    max_downloads: int = 25
    timeout: float = 15
    delay: float = 0.4
    max_bytes: int = 30 * 1024 * 1024
    use_playwright: bool = False
    use_browser_use: bool = False


@dataclass(frozen=True)
class Lead:
    url: str
    label: str
    source: str
    path: tuple[str, ...]
    depth: int
    kind: str = "page"
    context: str = ""


def safe_name(value: str) -> str:
    return re.sub(r'[\x00-\x1f<>:"/\\|?*]', "_", value).strip(" .")[:70] or "资料"


def file_kind(content: bytes, label: str, filename: str, url: str) -> str:
    suffixes = {Path(value).suffix.lower() for value in (
        label, filename, unquote(urlsplit(url).path)
    )}
    if content.startswith(b"%PDF-") and b"%%EOF" in content[-8192:]:
        return "pdf"
    if (
        content.startswith(bytes.fromhex("d0cf11e0a1b11ae1"))
        and len(content) >= 512
        and "WordDocument".encode("utf-16-le") in content
    ):
        return "doc"
    if content.startswith(b"PK"):
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                names = set(archive.namelist())
                if {"[Content_Types].xml", "word/document.xml"}.issubset(names):
                    # Bound decompression when checking container integrity.
                    if sum(item.file_size for item in archive.infolist()) <= 120 * 1024 * 1024:
                        if archive.testzip() is None:
                            return "docx"
                elif ".zip" in suffixes and names:
                    return "zip"
        except (OSError, RuntimeError, ValueError, NotImplementedError, zipfile.BadZipFile):
            return ""
    if ".tex" in suffixes and re.search(rb"\\(?:documentclass|begin\s*\{document\})", content):
        return "tex"
    return ""


def is_html(resource: Resource) -> bool:
    sample = resource.content[:2048].lower()
    return resource.mime in {"text/html", "application/xhtml+xml"} or bool(
        re.search(rb"<(?:!doctype\s+html|html|head|body)(?:\s|>)", sample)
    )


class Collection:
    def __init__(self, journal: Journal, output: Path, config: CrawlConfig, http: HttpClient):
        self.journal = journal
        self.output = output
        self.config = config
        self.http = http
        self.hosts = {host_for(journal.url), *map(host_for, journal.extra_urls)}
        self.hosts.update(host_for(f"https://{host}") for host in journal.extra_hosts)
        self.attachment_hosts = set()
        self.now = datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
        self.todo = []
        self.ordinal = count()
        self.scheduled = set()
        self.pages = {}
        self.requested_pages = set()
        self.downloads = {}
        self.by_hash = {}
        self.artifacts = []
        self.pending = []
        self.web_text = {}
        self.rendered = set()
        self.browser = None
        self.homepage_resolved = ""

    def enqueue(self, lead: Lead, priority: int) -> None:
        key = (lead.url, lead.kind, lead.source if lead.kind == "attachment" else "")
        if key not in self.scheduled:
            self.scheduled.add(key)
            heapq.heappush(self.todo, (-priority, next(self.ordinal), lead))

    def issue(self, lead: Lead, reason: str, detail: str = "") -> None:
        self.pending.append({
            "url": lead.url, "title": lead.label, "source_url": lead.source,
            "path": list(lead.path), "reason": reason, "detail": detail,
            "context": lead.context,
        })

    def source(self, lead: Lead, final_url: str) -> dict:
        return {
            "source_url": lead.source or final_url, "download_url": lead.url,
            "resolved_url": final_url, "path": list(lead.path), "label": lead.label,
            "context": lead.context,
            "source_published_at": self.pages.get(lead.source, {}).get("published_at", ""),
        }

    def save_attachment(self, lead: Lead, resource: Resource) -> bool:
        kind = file_kind(resource.content, lead.label, resource.filename, resource.url)
        if not kind or len(resource.content) > self.config.max_bytes:
            return False
        digest = hashlib.sha256(resource.content).hexdigest()
        source = self.source(lead, resource.url)
        item = self.by_hash.get(digest)
        if item is None:
            name = unquote(resource.filename) or Path(unquote(urlsplit(resource.url).path)).name
            if Path(name).suffix.lower() != f".{kind}":
                name = lead.label or name
            stem = safe_name(Path(name).stem if Path(name).suffix.lower() == f".{kind}" else name)
            relative = f"attachments/{stem}_{digest[:10]}.{kind}"
            target = self.output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(resource.content)
            item = {
                "type": "attachment", "format": kind, "title": lead.label or name,
                "local_path": relative, "sha256": digest, "bytes": len(resource.content),
                "sources": [], "status": "saved",
            }
            self.artifacts.append(item)
            self.by_hash[digest] = item
        if source not in item["sources"]:
            item["sources"].append(source)
        return True

    def handle_html(self, lead: Lead, resource: Resource, *, rendered: bool = False) -> bool:
        url = canonical_url(resource.url)
        if url in self.pages and not rendered:
            return False
        page = analyze_html(resource.content, url, lead.label)
        self.pages[url] = {"url": url, "requested_url": lead.url, "title": page.title,
                           "path": list(lead.path), "visited_at": self.now,
                           "published_at": page.published_at}
        if lead.depth == 0 and lead.url == canonical_url(self.journal.url):
            self.homepage_resolved = url
        parts = urlsplit(url)
        if (parts.hostname and parts.hostname.endswith(".cbpt.cnki.net")
                and parts.path.lower() == "/editorgn/quit.aspx"):
            # Some CNKI journal roots lead to a legacy sign-out endpoint.
            public = parts._replace(path="/portal/journal/portal/client/index", query="").geturl()
            self.enqueue(Lead(public, "", url, (*lead.path, "知网期刊首页"), lead.depth), 950)
        attachment_links = [link for link in page.links if link.kind == "attachment"]
        if page.markdown or attachment_links:
            digest = hashlib.sha256(resource.content).hexdigest()[:10]
            relative = f"web/{safe_name(page.title or lead.label or self.journal.name)}_{digest}.html"
            target = self.output / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(resource.content)
            if not any(item["local_path"] == relative and item["source_url"] == url
                       for item in self.artifacts if item["type"] == "webpage"):
                self.artifacts.append({
                    "type": "webpage", "title": page.title or lead.label, "source_url": url,
                    "path": list(lead.path), "local_path": relative, "status": "saved",
                    "published_at": page.published_at,
                })
        if page.markdown and not attachment_links:
            self.web_text[url] = (page.title or lead.label, page.markdown)
        if attachment_links:
            self.web_text.pop(url, None)
        for link in page.links:
            try:
                target = canonical_url(link.url)
                host = host_for(target)
            except ValueError:
                continue
            child = Lead(target, link.label, url, (*lead.path, link.label), lead.depth + 1,
                         link.kind, link.context)
            if link.kind == "official":
                self.hosts.add(host)
            if link.kind == "attachment":
                self.attachment_hosts.add(host)
            if link.kind == "review":
                self.issue(child, "needs_review")
            elif link.kind == "attachment" or allowed_url(target, self.hosts):
                self.enqueue(child, link.priority)
            else:
                self.issue(child, "unverified_host")
        return bool(page.dynamic_controls)

    def explore(self, lead: Lead) -> None:
        if not self.config.use_playwright or lead.url in self.rendered:
            return
        self.rendered.add(lead.url)
        try:
            from .browser import PlaywrightBrowser
            if self.browser is None:
                self.browser = PlaywrightBrowser(self.config.timeout)
            for snapshot in self.browser.inspect(lead.url, self.browser_hosts()):
                if not allowed_url(snapshot.url, self.hosts):
                    continue
                if snapshot.url not in self.pages and len(self.pages) >= self.config.max_pages:
                    self.issue(lead, "page_limit", snapshot.url)
                    continue
                rendered_lead = Lead(lead.url, lead.label, lead.source, (*lead.path, *snapshot.path), lead.depth)
                if len(snapshot.content) <= self.config.max_bytes:
                    self.handle_html(rendered_lead, Resource(snapshot.url, snapshot.content, "text/html", ""), rendered=True)
                for download in snapshot.downloads:
                    # Generic browser downloads have ambiguous relevance. Route
                    # them for review unless linked to the relevant current lead.
                    if not re.search(r"投稿|稿件|模板|作者指南|格式规范|manuscript|template",
                                     f"{lead.label} {download.filename}", re.I):
                        self.issue(lead, "browser_download_needs_review", download.filename)
                        continue
                    candidate = Lead(download.url, download.filename, snapshot.url,
                                     rendered_lead.path, lead.depth + 1, "attachment")
                    self.accept_browser_download(candidate, download)
        except Exception as exc:
            self.issue(lead, "playwright_failed", str(exc))

    def accept_browser_download(self, lead, download) -> None:
        if lead.depth > self.config.max_depth:
            self.issue(lead, "depth_limit")
            return
        if lead.url not in self.downloads and len(self.downloads) >= self.config.max_downloads:
            self.issue(lead, "download_limit")
            return
        resource = Resource(download.url, download.content, "", download.filename)
        if self.save_attachment(lead, resource):
            self.downloads[lead.url] = resource
        else:
            self.issue(lead, "invalid_attachment")

    def download(self, lead: Lead) -> None:
        if lead.url in self.downloads:
            resource = self.downloads[lead.url]
            if resource and self.save_attachment(lead, resource):
                return
            self.issue(lead, "attachment_unavailable")
            return
        if len(self.downloads) >= self.config.max_downloads:
            self.issue(lead, "download_limit")
            return
        self.downloads[lead.url] = None
        hosts = self.browser_hosts() | {host_for(lead.url)}
        try:
            resource = self.http.get(lead.url, allowed_hosts=hosts, referer=lead.source)
            if self.save_attachment(lead, resource):
                self.downloads[lead.url] = resource
                return
            detail = f"响应类型：{resource.mime or '未知'}"
        except DownloadError as exc:
            detail = str(exc)
        if self.config.use_playwright:
            try:
                from .browser import PlaywrightBrowser
                if self.browser is None:
                    self.browser = PlaywrightBrowser(self.config.timeout)
                if lead.source and allowed_url(lead.source, self.hosts):
                    self.explore(Lead(lead.source, lead.label, "", lead.path[:-1], max(0, lead.depth - 1)))
                    if self.downloads[lead.url]:
                        self.save_attachment(lead, self.downloads[lead.url])
                        return
                download = self.browser.download(lead.url, hosts)
                self.accept_browser_download(lead, download)
                if self.downloads[lead.url]:
                    return
            except Exception as exc:
                detail += f"；浏览器下载：{exc}"
        self.issue(lead, "invalid_or_failed_attachment", detail)

    def visit(self, lead: Lead) -> None:
        if lead.depth > self.config.max_depth:
            self.issue(lead, "depth_limit")
            return
        if lead.kind == "attachment":
            self.download(lead)
            return
        if lead.url in self.requested_pages or lead.url in self.pages:
            return
        if len(self.requested_pages) >= self.config.max_pages:
            self.issue(lead, "page_limit")
            return
        self.requested_pages.add(lead.url)
        try:
            resource = self.http.get(lead.url, allowed_hosts=self.hosts, referer=lead.source)
        except DownloadError as exc:
            self.explore(lead)
            if lead.url not in self.pages:
                self.issue(lead, "page_fetch_failed", str(exc))
            return
        if is_html(resource):
            before = len(self.scheduled)
            dynamic = self.handle_html(lead, resource)
            if dynamic or (len(self.scheduled) == before and
                           canonical_url(resource.url) not in self.web_text):
                self.explore(lead)
            if dynamic and not self.config.use_playwright:
                self.issue(lead, "needs_review", "检测到待操作的动态投稿栏目")
        elif len(self.downloads) >= self.config.max_downloads:
            self.issue(lead, "download_limit")
        elif self.save_attachment(lead, resource):
            self.downloads[lead.url] = resource
        else:
            self.issue(lead, "unrecognized_response", resource.mime)

    def drain_queue(self) -> None:
        while self.todo:
            _, _, lead = heapq.heappop(self.todo)
            self.visit(lead)

    def use_agent(self) -> None:
        from .browser import browser_use_scan
        try:
            candidates = browser_use_scan(
                journal=self.journal.name, homepage=self.homepage_resolved or self.journal.url,
                allowed_hosts=self.browser_hosts(), visited=list(self.pages),
                found=[item["title"] for item in self.artifacts],
                unresolved=[f"{item['reason']}: {item['title']} {item['url']}" for item in self.pending],
                timeout=max(self.config.timeout, 60),
            )
        except Exception as exc:
            self.issue(Lead(self.journal.url, "Browser Use", "", (), 0), "browser_use_failed", str(exc))
            return
        for candidate in candidates:
            hosts = self.browser_hosts() if candidate.kind == "attachment" else self.hosts
            if not (allowed_url(candidate.url, hosts)
                    and allowed_url(candidate.source_url, self.hosts)):
                continue
            if candidate.kind == "attachment" and canonical_url(candidate.url) in self.downloads:
                if not self.downloads[canonical_url(candidate.url)]:
                    self.downloads.pop(canonical_url(candidate.url))
            lead = Lead(canonical_url(candidate.url), candidate.label, candidate.source_url,
                        tuple(candidate.path), 1, candidate.kind)
            # Agent discoveries can refine a URL previously found but not visited.
            self.visit(lead)
        self.drain_queue()

    def browser_hosts(self) -> set[str]:
        return self.hosts | self.attachment_hosts

    def finish(self) -> dict:
        if self.web_text:
            name = f"{safe_name(self.journal.name)}投稿格式.md"
            sections = [f"# {self.journal.name}投稿格式"]
            for url, (title, markdown) in self.web_text.items():
                sections.append(f"## {title or '投稿格式'}\n\n来源网址：{url}\n\n采集时间：{self.now}\n\n{markdown}")
            (self.output / name).write_text("\n\n".join(sections) + "\n", encoding="utf-8")
            self.artifacts.append({
                "type": "webpage_text", "title": f"{self.journal.name}投稿格式",
                "local_path": name, "source_urls": list(self.web_text), "status": "saved",
            })
        has_original = self.has_original()
        if not has_original:
            self.issue(Lead(self.journal.url, self.journal.name, "", (), 0),
                       "no_valid_word_or_pdf", "未发现已成功验证的相关 Word 或 PDF 原件")
        manifest = {
            "journal": asdict(self.journal), "status": "completed" if has_original else "uncompleted",
            "homepage_resolved": self.homepage_resolved, "collected_at": self.now,
            "scope": {"allowed_hosts": sorted(self.hosts), "limits": asdict(self.config)},
            "pages": list(self.pages.values()), "artifacts": self.artifacts, "pending": self.pending,
        }
        target = self.output / "manifest.json"
        tmp = target.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(target)
        return manifest

    def has_original(self) -> bool:
        return any(item.get("format") in {"doc", "docx", "pdf"} for item in self.artifacts)

    def run(self) -> dict:
        for url in (self.journal.url, *self.journal.extra_urls):
            self.enqueue(Lead(canonical_url(url), "", "", ("官网入口",), 0), 1000)
        try:
            self.drain_queue()
            actionable = {"needs_review", "invalid_or_failed_attachment", "page_fetch_failed", "unverified_host"}
            if self.config.use_browser_use and (
                not self.has_original() or any(item["reason"] in actionable for item in self.pending)
            ):
                self.use_agent()
            return self.finish()
        finally:
            if self.browser is not None:
                self.browser.close()


def collect(journal: Journal, output: Path, config: CrawlConfig) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    with HttpClient(config.timeout, config.delay, config.max_bytes) as http:
        return Collection(journal, output, config, http).run()
