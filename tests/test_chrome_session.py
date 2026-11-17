import threading
import unittest
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from journal_pipeline.config import load_settings
from journal_pipeline.fetch import Fetcher, FetchError


class Site(BaseHTTPRequestHandler):
    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError):
            pass  # Chrome can close speculative local connections during cleanup.

    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path in ('/challenge', '/challenge-visible'):
            self.send_response(412)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            if self.path == '/challenge-visible':
                self.wfile.write(b'<html><body>Checking your browser...<script>'
                                 b'if (!navigator.webdriver) setTimeout(() => '
                                 b'location.replace("/guidelines"), 800);</script></body></html>')
            else:
                self.wfile.write(b'<html><body><script>if (!navigator.webdriver) '
                                 b'location.replace("/guidelines");</script></body></html>')
        elif self.path == '/guidelines':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.send_header('Set-Cookie', 'journal_session=ready; Path=/')
            self.end_headers()
            self.wfile.write('<html><head><title>投稿指南</title></head><body>'
                             '<article>投稿格式要求：请按要求提供摘要、正文、图表和参考文献。'
                             '作者须仔细检查资料。</article>'
                             '<a href="/download">投稿模板</a></body></html>'.encode())
        elif self.path in ('/download', '/large') and 'journal_session=ready' in self.headers.get('Cookie', ''):
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.send_header('Content-Disposition', 'attachment; filename="template.pdf"')
            self.end_headers()
            self.wfile.write(b'%PDF-test-fixture' if self.path == '/download' else b'%PDF-' + b'x' * 4096)
        elif self.path == '/fake.doc':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<!DOCTYPE html><html><body>Missing attachment</body></html>')
        elif self.path == '/link-index':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write('<html><body><a href="/guidelines">投稿须知</a></body></html>'.encode())
        elif self.path == '/dynamic':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write('<html><body><script>setTimeout(() => document.body.innerHTML = '
                             '"<article>投稿格式要求：请提供摘要、正文、图表和参考文献，'
                             '并仔细核对各项内容。</article>", 200)</script></body></html>'.encode())
        elif self.path == '/verify':
            self.send_response(200)
            self.send_header('Content-Type', 'text/html; charset=utf-8')
            self.end_headers()
            self.wfile.write('<html><head><title>人机验证</title></head><body>'
                             '<script>if (!navigator.webdriver) location.replace("/guidelines");'
                             '</script></body></html>'.encode())
        elif self.path == '/public.pdf':
            self.send_response(200)
            self.send_header('Content-Type', 'application/pdf')
            self.end_headers()
            self.wfile.write(b'%PDF-test-fixture')
        else:
            self.send_response(404)
            self.send_header('Content-Type', 'text/html')
            self.end_headers()
            self.wfile.write(b'<html><body>Not found</body></html>')


class ChromeSessionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Site)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def settings(self, **kwargs):
        return replace(load_settings(), browser='chrome', timeout=6, delay=0,
                       browser_wait_ms=100, **kwargs)

    def collector(self, **kwargs):
        fetcher = Fetcher(self.settings(**kwargs))
        fetcher.session.trust_env = False  # The local fixture must not use machine proxies.
        return fetcher

    def test_initial_412_can_finish_with_valid_document_and_session_download(self):
        fetcher = self.collector()
        try:
            try:
                resource = fetcher.get(self.base + '/challenge')
            except FetchError as error:
                self.fail(f'A successful final navigation must supersede initial 412: {error}')
            self.assertEqual(resource.url, self.base + '/guidelines')
            self.assertIn('投稿格式要求', resource.text)
            self.assertTrue(resource.rendered)
            attachment = fetcher.get(self.base + '/download')
            self.assertEqual(attachment.content, b'%PDF-test-fixture')
            self.assertEqual(attachment.text, '')
        finally:
            fetcher.close()

    def test_permanent_error_is_not_accepted_as_content(self):
        fetcher = self.collector()
        try:
            with self.assertRaises(FetchError):
                fetcher.get(self.base + '/missing')
        finally:
            fetcher.close()

    def test_attachment_size_limit(self):
        fetcher = self.collector(max_bytes=1024)
        try:
            fetcher.get(self.base + '/guidelines')
            with self.assertRaises(FetchError):
                fetcher.get(self.base + '/large')
        finally:
            fetcher.close()

    def test_chrome_is_an_explicit_valid_mode(self):
        self.settings().validate()

    def test_html_disguised_as_doc_is_not_returned_as_attachment(self):
        fetcher = self.collector()
        try:
            with self.assertRaisesRegex(FetchError, 'HTML'):
                fetcher.get(self.base + '/fake.doc')
        finally:
            fetcher.close()

    def test_visible_412_message_waits_for_delayed_successful_navigation(self):
        fetcher = self.collector()
        try:
            try:
                resource = fetcher.get(self.base + '/challenge-visible')
            except FetchError as error:
                self.fail(f'Visible challenge text must not end the final-document wait: {error}')
            self.assertEqual(resource.url, self.base + '/guidelines')
            self.assertIn('投稿格式要求', resource.text)
        finally:
            fetcher.close()

    def test_default_http_mode_still_collects_static_page(self):
        fetcher = Fetcher(replace(self.settings(), browser='off'))
        fetcher.session.trust_env = False
        try:
            self.assertIn('投稿格式要求', fetcher.get(self.base + '/guidelines').text)
        finally:
            fetcher.close()


class AutoChromeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(('127.0.0.1', 0), Site)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f'http://127.0.0.1:{cls.server.server_port}'

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def collector(self, **kwargs):
        options = dict(browser='auto-chrome', timeout=6, delay=0, browser_wait_ms=100)
        options.update(kwargs)
        fetcher = Fetcher(replace(load_settings(), **options))
        fetcher.session.trust_env = False
        return fetcher

    def test_valid_static_content_needs_no_browser(self):
        fetcher = self.collector()
        try:
            resource = fetcher.get(self.base + '/guidelines')
            self.assertIn('投稿格式要求', resource.text)
            self.assertFalse(resource.rendered)
            self.assertIsNone(fetcher.chrome_session)
        finally:
            fetcher.close()

    def test_short_relevant_link_index_needs_no_browser(self):
        fetcher = self.collector()
        try:
            resource = fetcher.get(self.base + '/link-index')
            self.assertIn('投稿须知', resource.text)
            self.assertFalse(resource.rendered)
            self.assertIsNone(fetcher.chrome_session)
        finally:
            fetcher.close()

    def test_412_falls_back_to_one_reused_chrome_session(self):
        fetcher = self.collector()
        try:
            first = fetcher.get(self.base + '/challenge')
            session = fetcher.chrome_session
            self.assertIsNotNone(session)
            self.assertIn('投稿格式要求', first.text)
            second = fetcher.get(self.base + '/challenge-visible')
            self.assertIs(fetcher.chrome_session, session)
            self.assertIn('投稿格式要求', second.text)
        finally:
            fetcher.close()

    def test_dynamic_shell_uses_normal_chrome(self):
        fetcher = self.collector()
        try:
            resource = fetcher.get(self.base + '/dynamic')
            from journal_pipeline.extract import page_text
            self.assertIn('投稿格式要求', page_text(resource.text)[1])
            self.assertTrue(resource.rendered)
            self.assertIsNotNone(fetcher.chrome_session)
        finally:
            fetcher.close()

    def test_http_200_verification_page_is_not_accepted_as_static_content(self):
        fetcher = self.collector()
        try:
            resource = fetcher.get(self.base + '/verify')
            self.assertEqual(resource.url, self.base + '/guidelines')
            self.assertIn('投稿格式要求', resource.text)
        finally:
            fetcher.close()

    def test_public_attachment_needs_no_browser(self):
        fetcher = self.collector()
        try:
            resource = fetcher.get(self.base + '/public.pdf')
            self.assertEqual(resource.content, b'%PDF-test-fixture')
            self.assertFalse(resource.rendered)
            self.assertIsNone(fetcher.chrome_session)
        finally:
            fetcher.close()

    def test_oversized_http_response_does_not_start_chrome(self):
        fetcher = self.collector(max_bytes=4)
        try:
            with self.assertRaises(FetchError):
                fetcher.get(self.base + '/public.pdf')
            self.assertIsNone(fetcher.chrome_session)
        finally:
            fetcher.close()

    def test_auto_chrome_is_a_valid_explicit_mode(self):
        replace(load_settings(), browser='auto-chrome').validate()


if __name__ == '__main__':
    unittest.main()
