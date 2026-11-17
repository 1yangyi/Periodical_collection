import json
import tempfile
import unittest
from dataclasses import replace
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from docx import Document
from docx.shared import Pt

from journal_pipeline.config import load_settings
from journal_pipeline.models import Journal, Resource
from journal_pipeline.pipeline import cached_result, process_journal
from journal_pipeline.rules import extract_link_candidates
from journal_pipeline.submission_format import find_requirements


BASE = 'https://journal.example'


class SubmissionFormatTests(unittest.TestCase):
    def collect(self, pages, seed='/', max_pages=10, base=BASE):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        settings = replace(load_settings(), output=Path(directory.name), browser='off', max_pages=max_pages)
        calls = []

        def fetch(url):
            calls.append(url.removeprefix(base))
            body = pages[url.removeprefix(base)]
            if isinstance(body, Resource):
                return body
            if isinstance(body, bytes):
                return Resource(url, body, {})
            return Resource(url, body.encode(), {'Content-Type': 'text/html'}, body)

        with patch('journal_pipeline.pipeline.Fetcher') as factory:
            factory.return_value.get.side_effect = fetch
            factory.return_value.warnings = []
            report = process_journal(Journal('test', '示例学报', [base + seed], '长尾'), settings)
        root = settings.output / report['directory']
        return report, root, calls

    def test_ranking_seed_recovers_home_and_skips_paper_downloads(self):
        seed = '/WKI/WebPublication/downloadTopList.aspx'
        paper = '/WKI/WebPublication/kbDownload.aspx?fn=example'
        pages = {
            seed: f'<a href="/">首页</a><a href="{paper}">下载</a><a href="/stale">投稿须知</a>',
            '/': '<a href="/guide">投稿须知</a>',
            '/guide': '<p>标题使用二号黑体。</p>',
        }
        report, root, calls = self.collect(pages, seed, max_pages=3)
        self.assertEqual(calls, [seed, '/', '/guide'])
        self.assertEqual(report['artifacts'][0]['label'], '投稿须知')
        saved = json.loads((root / 'submission-format.json').read_text(encoding='utf-8'))
        self.assertEqual(saved['requirements'][0]['quote'], '标题使用二号黑体。')
        source = saved['requirements'][0]['sources'][0]
        lines = (root / source['path']).read_text(encoding='utf-8').splitlines()
        self.assertEqual(lines[source['line'] - 1], saved['requirements'][0]['quote'])

    def test_short_template_index_finds_english_named_word_template(self):
        doc = Document()
        title = doc.add_paragraph('Sample Title', style='Title')
        title.add_run(' Here').font.size = Pt(16)
        doc.styles['Title'].font.size = Pt(16)
        doc.add_paragraph('Insert your abstract here.')
        buffer = BytesIO()
        doc.save(buffer)
        pages = {
            '/': '<a href="/templates">论文模板</a>',
            '/templates': '<main><a href="/Article.docx">Article</a></main>',
            '/Article.docx': buffer.getvalue(),
        }
        report, root, calls = self.collect(pages)
        self.assertIn('/Article.docx', calls)
        saved = json.loads((root / 'submission-format.json').read_text(encoding='utf-8'))
        self.assertTrue(any(item['basis'] == 'template_style' and '16 pt' in item['quote']
                            for item in saved['requirements']))
        self.assertEqual(report['format_report']['requirements'], len(saved['requirements']))
        self.assertIsNotNone(cached_result(root / 'manifest.json', report['fingerprint']))
        (root / 'submission-format.json').write_text('{}', encoding='utf-8')
        self.assertIsNone(cached_result(root / 'manifest.json', report['fingerprint']))

    def test_keeps_conflicting_requirements_and_reports_missing_template(self):
        pages = {
            '/': '<a href="/one">投稿须知</a><a href="/two">投稿指南</a><a href="/template">论文模板</a>',
            '/one': '<main><p>稿件字数在1.8万到2.5万字为宜（含脚注）。</p></main>',
            '/two': '<main><p>稿件字数在1.5万到2.5万字为宜（含脚注和空格）。</p></main>',
            '/template': '<main>请下载</main>',
        }
        _, root, _ = self.collect(pages)
        saved = json.loads((root / 'submission-format.json').read_text(encoding='utf-8'))
        self.assertEqual(len(saved['possible_conflicts'][0]['requirements']), 2)
        self.assertTrue(any(item['url'].endswith('/template') for item in saved['review']))

    def test_inline_markup_preserves_scope_and_excludes_browser_instructions(self):
        pages = {
            '/': '<a href="/guide">Author Guidelines</a>',
            '/guide': ('<main><p><strong>Research Articles</strong>: The maximum number of figures is 8.</p>'
                       '<p>建议使用分辨率为1024*768的IE 10.0或更高版本浏览此页面。</p></main>'),
        }
        _, root, _ = self.collect(pages)
        saved = json.loads((root / 'submission-format.json').read_text(encoding='utf-8'))
        self.assertEqual([item['quote'] for item in saved['requirements']],
                         ['Research Articles: The maximum number of figures is 8.'])

    def test_homepage_download_heading_does_not_select_articles_from_other_sections(self):
        pages = {
            '/': ('<h2>网络首发</h2><a href="/article.pdf">普通论文</a>'
                  '<h2>下载中心</h2><a href="/Article.docx">Article</a>'),
            '/Article.docx': b'invalid Word fixture',
        }
        _, _, calls = self.collect(pages)
        self.assertEqual(calls, ['/', '/Article.docx'])

    def test_extracts_short_instructions_but_not_link_labels(self):
        text = '标题居中。\n正文使用宋体。\n稿件格式要求下载'
        self.assertEqual([quote for _, quote, _ in find_requirements(text)], ['标题居中。', '正文使用宋体。'])

    def test_cnki_download_center_saves_layout_docx_and_rejects_html_attachment(self):
        base = 'https://example.cbpt.cnki.net'
        seed = '/WKE/WebPublication/index.aspx?mid=sample'
        layout = '23e4c1b5-6393-4ef1-995f-789b1c99bc87'
        invalid = '37ee1437-6572-4cbd-8b24-a268c3f998ad'
        column = 'eaf499e4-5e56-4b65-bf00-a5305c9c5eaf'
        layout_path = f'/WKE/WebPublication/wkDownFileByLink.aspx?fid={layout}&mid=sample&cid={column}'
        invalid_path = f'/WKE/WebPublication/wkDownFileByLink.aspx?fid={invalid}&mid=sample&cid={column}'
        doc = Document()
        doc.add_paragraph('标题使用二号黑体。')
        data = BytesIO()
        doc.save(data)
        html = '<p>标题使用五号黑体。</p>'
        pages = {
            seed: ('<h3>下载中心</h3>'
                   f'<a href="#" onclick="downFileContentTypeTwo(\'{layout}\',\'sample\',\'{column}\')">论文编排格式</a>'
                   f'<a href="#" onclick="downFileContentTypeTwo(\'{invalid}\',\'sample\',\'{column}\')">投稿须知</a>'),
            layout_path: Resource(base + layout_path, data.getvalue(), {
                'Content-Type': 'application/octet-stream', 'Content-Disposition': 'attachment; filename=layout.docx'}),
            invalid_path: Resource(base + invalid_path, html.encode(), {'Content-Type': 'text/html'}, html),
        }
        report, root, calls = self.collect(pages, seed, base=base)
        self.assertEqual(calls, [seed, layout_path, invalid_path])
        self.assertEqual(len(report['artifacts']), 1)
        self.assertEqual(report['artifacts'][0]['selection']['method'], 'site_action')
        self.assertEqual(report['visits'][-1]['status'], 'fetch_failed')
        saved = json.loads((root / 'submission-format.json').read_text(encoding='utf-8'))
        self.assertEqual([item['quote'] for item in saved['requirements']], ['标题使用二号黑体。'])

    def test_cnki_download_handler_is_scoped_and_rejects_extra_script(self):
        action = ("downFileContentTypeTwo('23e4c1b5-6393-4ef1-995f-789b1c99bc87',"
                  "'sample','eaf499e4-5e56-4b65-bf00-a5305c9c5eaf')")
        html = f'<a href="#" onclick="{action}">论文编排格式</a>'
        path = '/WKE/WebPublication/index.aspx?mid=sample'
        self.assertEqual(extract_link_candidates(html, BASE + path), [])
        self.assertEqual(extract_link_candidates(html.replace(action, action + '; doOtherThing()'),
                                                 'https://sample.cbpt.cnki.net' + path), [])

    def test_cnki_wkg_download_uses_the_wkg_endpoint(self):
        fid = '55fd71e6-898d-42e8-8e2e-bf7b1343755e'
        cid = 'eaf499e4-5e56-4b65-bf00-a5305c9c5eaf'
        html = f'<a href="#" onclick="downFileContentTypeTwo(\'{fid}\',\'lyxk\',\'{cid}\')">论文模板</a>'
        base = 'https://lyxk.cbpt.cnki.net/WKG/WebPublication/'
        candidates = extract_link_candidates(html, base + 'index.aspx?mid=lyxk')
        self.assertEqual([(item['label'], item['action'], item['url']) for item in candidates], [
            ('论文模板', 'downFileContentTypeTwo', base + f'wkDownFileByLink.aspx?fid={fid}&mid=lyxk&cid={cid}'),
        ])


if __name__ == '__main__':
    unittest.main()
