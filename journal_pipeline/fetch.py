import threading
import time
from urllib.parse import urlsplit, urlunsplit

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from .models import Resource
from .rules import (attachment_name, extract_links, match, matching_terms,
                    official_candidates, related_resource_candidates)


class FetchError(RuntimeError):
    pass


class BlockedError(FetchError):
    pass


class TransportError(FetchError):
    pass


class ResponseTooLargeError(FetchError):
    pass


class RateLimiter:
    """Across worker threads, space request starts to each host."""

    def __init__(self):
        self.lock = threading.Lock()
        self.next_start = {}

    def wait(self, url, delay):
        host = urlsplit(url).netloc
        with self.lock:
            now = time.monotonic()
            start = max(now, self.next_start.get(host, now))
            self.next_start[host] = start + delay
        time.sleep(max(0, start - time.monotonic()))


LIMITER = RateLimiter()


class Fetcher:
    def __init__(self, settings):
        self.settings = settings
        self.session = requests.Session()
        self.session.headers['User-Agent'] = 'PeriodicalBlueprint/1.0 (journal-guideline-collector)'
        retry = Retry(total=settings.retries, backoff_factor=0.5,
                      status_forcelist=(429, 500, 502, 503, 504),
                      allowed_methods=frozenset(['GET']), respect_retry_after_header=False)
        self.session.mount('http://', HTTPAdapter(max_retries=retry))
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        self.browser_unavailable = False
        self.warnings = []
        self.chrome_session = None

    def close(self):
        if self.chrome_session is not None:
            self.chrome_session.close()
        self.session.close()

    def http(self, url):
        LIMITER.wait(url, self.settings.delay)
        try:
            with self.session.get(url, timeout=self.settings.timeout, stream=True) as response:
                if response.status_code in (401, 403):
                    raise BlockedError(f'HTTP {response.status_code}: 需要认证或站点拒绝访问')
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > self.settings.max_bytes:
                        raise ResponseTooLargeError(f'响应超过 max_bytes={self.settings.max_bytes}')
                    chunks.append(chunk)
                content = b''.join(chunks)
                content_type = response.headers.get('Content-Type', '').lower()
                looks_html = content.lstrip()[:100].lower().startswith((b'<html', b'<!doctype', b'<head', b'<body'))
                is_html = 'html' in content_type or looks_html or not attachment_name(response.url, response.headers)
                text = ''
                if is_html:
                    response._content = content
                    if not response.encoding or response.encoding.lower() == 'iso-8859-1':
                        response.encoding = response.apparent_encoding or 'utf-8'
                    text = response.text
                return Resource(response.url, content, dict(response.headers), text)
        except (requests.ConnectionError, requests.Timeout) as error:
            raise TransportError(f'{type(error).__name__}: {error}') from error
        except requests.RequestException as error:
            raise FetchError(f'{type(error).__name__}: {error}') from error

    def render(self, url):
        from playwright.sync_api import sync_playwright
        LIMITER.wait(url, self.settings.delay)
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)
            try:
                page = browser.new_page(user_agent=self.session.headers['User-Agent'])
                response = page.goto(url, timeout=self.settings.timeout * 1000, wait_until='domcontentloaded')
                if response and response.status >= 400:
                    raise FetchError(f'浏览器 HTTP {response.status}')
                page.wait_for_timeout(self.settings.browser_wait_ms)
                html = page.content()
                content = html.encode('utf-8')
                if len(content) > self.settings.max_bytes:
                    raise FetchError('渲染后的 HTML 超过大小限制')
                return Resource(page.url, content, {'Content-Type': 'text/html; charset=utf-8'}, html, True)
            finally:
                browser.close()

    def get(self, url):
        try:
            return self._get(url)
        except TransportError as error:
            parsed = urlsplit(url)
            if not self.settings.http_fallback or parsed.scheme != 'https' or parsed.port not in (None, 443):
                raise
            host = parsed.hostname
            host = f'[{host}]' if ':' in host else host
            fallback = urlunsplit(('http', host, parsed.path, parsed.query, ''))
            detail = {'stage': 'http_fallback', 'from_url': url, 'to_url': fallback,
                      'message': f'HTTPS 连接失败，按显式配置尝试明文 HTTP：{error}'}
            self.warnings.append(detail)
            resource = self._get(fallback)  # Once only; a redirect to broken HTTPS cannot recurse.
            resource.protocol_fallback = detail
            return resource

    def _chrome(self, url):
        from .chrome_session import ChromeSession
        if self.chrome_session is None:
            self.chrome_session = ChromeSession(self.settings)
        LIMITER.wait(url, self.settings.delay)
        return self.chrome_session.get(url)

    def _http_usable(self, resource):
        if resource is None:
            return False
        if not resource.text:
            return bool(resource.content and attachment_name(resource.url, resource.headers))
        # A 200 response alone does not prove that the actual page was returned.
        from .extract import page_text
        from .submission_format import find_requirements
        try:
            _, evidence = page_text(resource.text)
        except BlockedError:
            return False
        if any(find_requirements(evidence)):
            return True
        if (len(evidence) >= self.settings.min_text_length
                and matching_terms(evidence, self.settings.rules['content'])):
            return True
        if any(match(label, self.settings.rules)
               for label, _ in extract_links(resource.text, resource.url)):
            return True
        return bool(related_resource_candidates(resource.text, resource.url, self.settings.rules)
                    or (self.settings.discover_official
                        and official_candidates(resource.text, resource.url)))

    def _auto_chrome(self, url):
        resource, http_error = None, None
        try:
            resource = self.http(url)
        except ResponseTooLargeError:
            raise  # A configured size budget must not trigger a larger browser download.
        except FetchError as error:
            http_error = error
        if self._http_usable(resource):
            return resource
        try:
            return self._chrome(url)
        except ResponseTooLargeError:
            raise
        except Exception as error:
            self.warnings.append({'url': url, 'stage': 'browser',
                                  'message': f'Chrome 回退失败: {error}'[:500]})
            if resource is not None:
                return resource  # Preserve static discovery and the normal downstream checks.
            raise http_error or FetchError(f'Chrome 回退失败: {error}')

    def _get(self, url):
        if self.settings.browser == 'chrome':
            return self._chrome(url)
        if self.settings.browser == 'auto-chrome':
            return self._auto_chrome(url)
        resource, http_error = None, None
        try:
            resource = self.http(url)
        except BlockedError:
            raise
        except FetchError as error:
            http_error = error
        # Binary resources never go through the browser.
        if resource and not resource.text:
            return resource
        links = extract_links(resource.text, resource.url) if resource else []
        needs_render = not resource or len(links) < 8 or not any(match(t, self.settings.rules) for t, _ in links)
        enabled = self.settings.browser != 'off' and not self.browser_unavailable
        if enabled and (needs_render or self.settings.browser == 'always'):
            try:
                rendered = self.render(url)
                def usefulness(page):
                    targets = sum(bool(match(label, self.settings.rules))
                                  for label, _ in extract_links(page.text, page.url))
                    visible = BeautifulSoup(page.text, 'html.parser').get_text(' ', strip=True)
                    return targets, len(visible)
                # Raw HTML size mostly measures scripts and boilerplate, not useful content.
                score = usefulness(rendered)
                if score != (0, 0) and (not resource or score >= usefulness(resource)):
                    resource = rendered
            except Exception as error:
                if isinstance(error, ImportError) or 'Executable doesn\'t exist' in str(error):
                    self.browser_unavailable = True
                self.warnings.append({'url': url, 'stage': 'browser', 'message': str(error)[:500]})
        if resource is None:
            raise http_error or FetchError('无法获取页面')
        return resource
