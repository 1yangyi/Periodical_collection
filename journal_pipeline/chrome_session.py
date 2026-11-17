"""Opt-in normal Chrome process, isolated from the user's personal profile."""

import os
import shutil
import socket
import subprocess
import tempfile
import time
from pathlib import Path

import requests

from .fetch import BlockedError, FetchError, ResponseTooLargeError
from .models import Resource
from .rules import attachment_name


class ChromeSession:
    def __init__(self, settings):
        self.settings = settings
        self.process = self.profile = self.playwright = self.browser = None
        self.context = self.page = None
        try:
            self._start()
        except Exception:
            self.close()
            raise

    def _start(self):
        from playwright.sync_api import sync_playwright

        candidates = [
            Path(os.environ.get('PROGRAMFILES', 'C:/Program Files')) / 'Google/Chrome/Application/chrome.exe',
            Path(os.environ.get('PROGRAMFILES(X86)', 'C:/Program Files (x86)')) / 'Google/Chrome/Application/chrome.exe',
            Path(os.environ.get('LOCALAPPDATA', '')) / 'Google/Chrome/Application/chrome.exe',
        ]
        executable = next((str(p) for p in candidates if p.is_file()), None)
        executable = executable or shutil.which('google-chrome') or shutil.which('google-chrome-stable')
        if not executable:
            raise FetchError('--browser chrome 需要已安装 Google Chrome')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        self.profile = tempfile.TemporaryDirectory(prefix='journal-chrome-', ignore_cleanup_errors=True)
        self.process = subprocess.Popen([
            executable, f'--remote-debugging-port={port}', '--remote-debugging-address=127.0.0.1',
            f'--user-data-dir={self.profile.name}', '--no-first-run', '--no-default-browser-check',
            'about:blank',
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        endpoint = f'http://127.0.0.1:{port}'
        deadline = time.monotonic() + self.settings.timeout
        with requests.Session() as probe:
            probe.trust_env = False  # A local CDP connection must not pass through an HTTP proxy.
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise FetchError('独立 Chrome 进程在启动时退出')
                try:
                    if probe.get(endpoint + '/json/version', timeout=0.5).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(0.1)
            else:
                raise FetchError('等待 Chrome 本机调试接口超时')
        self.playwright = sync_playwright().start()
        self.browser = self.playwright.chromium.connect_over_cdp(
            endpoint, timeout=self.settings.timeout * 1000)
        self.context = self.browser.contexts[0]
        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.user_agent = self.page.evaluate('navigator.userAgent')

    def _check_size(self, content):
        if len(content) > self.settings.max_bytes:
            raise ResponseTooLargeError(f'响应超过 max_bytes={self.settings.max_bytes}')

    def get(self, url):
        # The browser context request shares cookies with rendered pages. Probe
        # headers as well as suffixes so dynamic download URLs retain their files.
        headers = {'User-Agent': self.user_agent}
        if self.page.url.startswith(('http://', 'https://')):
            headers['Referer'] = self.page.url
        response = None
        try:
            response = self.context.request.get(url, headers=headers,
                                                timeout=self.settings.timeout * 1000)
            if response.ok and attachment_name(response.url, response.headers):
                content = response.body()
                self._check_size(content)
                prefix = content.lstrip()[:100].lower()
                if ('html' in response.headers.get('content-type', '').lower()
                        or prefix.startswith((b'<!doctype', b'<html', b'<head', b'<body'))):
                    raise FetchError('附件链接返回 HTML 页面，未保存为 Word/PDF 原件')
                return Resource(response.url, content, response.headers)
        except FetchError:
            raise
        except Exception:
            # A failed HTTP probe does not prevent browser navigation.
            pass
        finally:
            if response is not None:
                response.dispose()
        return self._page(url)

    def _page(self, url):
        from playwright.sync_api import TimeoutError as BrowserTimeout

        documents = []

        def record(response):
            if response.request.resource_type == 'document' and response.frame == self.page.main_frame:
                documents.append(response)

        self.page.on('response', record)
        try:
            deadline = time.monotonic() + self.settings.timeout
            self.page.goto(url, wait_until='domcontentloaded', timeout=self.settings.timeout * 1000)
            # Challenge pages may already have visible text. Only a later
            # successful main-document response can supersede their HTTP 412.
            while (documents and documents[-1].status == 412
                   and time.monotonic() < deadline):
                self.page.wait_for_timeout(min(100, max(1, (deadline - time.monotonic()) * 1000)))
            if documents and documents[-1].status < 400:
                try:
                    self.page.wait_for_function(
                        "document.readyState !== 'loading' && document.body && "
                        "document.body.innerText.trim().length > 0",
                        timeout=max(1, (deadline - time.monotonic()) * 1000))
                except BrowserTimeout:
                    pass
            self.page.wait_for_timeout(self.settings.browser_wait_ms)
            status = documents[-1].status if documents else None
            if status in (401, 403):
                raise BlockedError(f'Chrome HTTP {status}: 站点拒绝访问')
            if status is None or status >= 400:
                raise FetchError(f'Chrome 最终页面 HTTP {status}，未取得有效正文')
            html = self.page.content()
            content = html.encode('utf-8')
            self._check_size(content)
            return Resource(self.page.url, content, {'Content-Type': 'text/html; charset=utf-8'}, html, True)
        except (FetchError, BlockedError):
            raise
        except Exception as error:
            raise FetchError(f'Chrome: {error}') from error
        finally:
            self.page.remove_listener('response', record)

    def close(self):
        for resource in (self.context, self.browser):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass
        if self.playwright is not None:
            try:
                self.playwright.stop()
            except Exception:
                pass
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.profile is not None:
            self.profile.cleanup()
