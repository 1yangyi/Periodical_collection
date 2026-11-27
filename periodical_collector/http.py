import ipaddress
import re
import time
from dataclasses import dataclass
from email.message import Message
from urllib.parse import urldefrag, urljoin, urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


@dataclass
class Resource:
    url: str
    content: bytes
    mime: str
    filename: str


class DownloadError(Exception):
    pass


def host_for(url: str) -> str:
    parts = urlsplit(url)
    host = parts.hostname or ""
    if (
        parts.scheme not in {"http", "https"} or not host or "." not in host
        or parts.username is not None or parts.password is not None
        or host.endswith((".local", ".localhost", ".internal"))
    ):
        raise ValueError(f"无效的公开网页地址：{url}")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if re.fullmatch(r"[\d.]+", host):
            raise ValueError(f"无效的主机名：{host}")
    else:
        if not address.is_global:
            raise ValueError(f"地址不属于公开网络：{host}")
    return host.lower()


def canonical_url(url: str) -> str:
    url, _ = urldefrag(url.strip())
    return requests.utils.requote_uri(url)


def allowed_url(url: str, hosts: set[str]) -> bool:
    try:
        host = host_for(url)
    except ValueError:
        return False
    return host in hosts or host.removeprefix("www.") in hosts or f"www.{host}" in hosts


class HttpClient:
    def __init__(self, timeout: float, delay: float, max_bytes: int):
        self.timeout = timeout
        self.delay = delay
        self.max_bytes = max_bytes
        self.last_request = 0.0
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "PeriodicalCollection/0.1 (public submission guides)"
        adapter = HTTPAdapter(max_retries=Retry(
            total=1, backoff_factor=0.5, status_forcelist=(429, 502, 503, 504),
            respect_retry_after_header=False, raise_on_status=False,
        ))
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.session.close()

    def get(self, url: str, *, allowed_hosts: set[str], referer: str = "") -> Resource:
        if not allowed_url(url, allowed_hosts):
            raise DownloadError("目标域名未在采集范围内")
        headers = {"Referer": referer} if referer else {}
        try:
            for _ in range(6):
                wait = self.delay - (time.monotonic() - self.last_request)
                if wait > 0:
                    time.sleep(wait)
                self.last_request = time.monotonic()
                with self.session.get(
                    url, headers=headers, timeout=self.timeout, stream=True, allow_redirects=False
                ) as response:
                    if response.is_redirect:
                        url = canonical_url(urljoin(response.url, response.headers["Location"]))
                        if not allowed_url(url, allowed_hosts):
                            raise DownloadError(f"跳转到未核实域名：{url}")
                        continue
                    response.raise_for_status()
                    content = bytearray()
                    for chunk in response.iter_content(chunk_size=65536):
                        content.extend(chunk)
                        if len(content) > self.max_bytes:
                            raise DownloadError("文件超过下载大小上限")
                    disposition = Message()
                    disposition["Content-Disposition"] = response.headers.get("Content-Disposition", "")
                    return Resource(
                        response.url, bytes(content),
                        response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower(),
                        disposition.get_filename() or "",
                    )
        except requests.RequestException as exc:
            raise DownloadError(str(exc)) from exc
        raise DownloadError("跳转次数超过上限")
