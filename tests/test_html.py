import unittest
from urllib.parse import unquote

from periodical_collector.html import analyze_html


class HtmlDiscoveryTest(unittest.TestCase):
    def analyze(self, markup, url="https://journal.example/index", hint=""):
        return analyze_html(markup.encode(), url, hint)

    def test_prioritizes_guidelines_over_ordinary_news_and_papers(self):
        page = self.analyze("""
            <title>示例期刊</title>
            <nav><a href="/guide">投稿须知</a><a href="/announcements">通知公告</a>
              <a href="/login">在线投稿</a></nav>
            <a href="/papers/1.pdf">论文正文</a>
            <a href="/news/1">学术会议新闻</a>
        """)
        self.assertEqual(
            [("/guide", "page"), ("/announcements", "page")],
            [(x.url.removeprefix("https://journal.example"), x.kind) for x in page.links],
        )
        self.assertFalse(page.markdown)

    def test_extensionless_attachment_is_detected_but_boilerplate_is_not_a_format(self):
        page = self.analyze("""
            <title>投稿须知</title><main><p>稿件格式要求详见附件。</p>
              <a href="/system/download.jsp?fileId=abc">稿件格式要求2025.11</a>
              <a href="/review.pdf">录用后填写稿件审查表</a>
            </main>
        """)
        self.assertEqual(["attachment", "review"], [link.kind for link in page.links])
        self.assertFalse(page.markdown)

    def test_recognizes_chinese_filename_on_external_attachment_host(self):
        page = self.analyze("""
            <title>投稿须知</title><main>
              <a href="https://media.example/files/稿件格式要求 (1).docx#download">下载</a>
            </main>
        """)
        link, = page.links
        self.assertEqual("attachment", link.kind)
        self.assertTrue(unquote(link.url).endswith("/稿件格式要求 (1).docx"))

    def test_inline_format_preserves_structured_body(self):
        page = self.analyze("""
            <title>示例期刊</title>
            <main><h1>投稿须知</h1><h2>一、排版规范</h2>
              <ol><li>正文使用宋体。</li><li>摘要不超过300字。</li></ol>
              <table><tr><th>栏目</th><th>要求</th></tr>
              <tr><td>字号</td><td>小四</td></tr></table>
              <footer>网站版权和技术支持</footer>
            </main>
        """)
        self.assertEqual("投稿须知", page.title)
        self.assertIn("## 一、排版规范", page.markdown)
        self.assertIn("1. 正文使用宋体。", page.markdown)
        self.assertIn("| 字号 | 小四 |", page.markdown)
        self.assertNotIn("网站版权", page.markdown)

    def test_only_follows_pagination_in_relevant_navigation(self):
        markup = '<title>标题</title><a href="?page=2" rel="next">下一页</a>'
        self.assertEqual([], self.analyze(markup).links)
        link, = self.analyze(markup, hint="下载中心").links
        self.assertEqual("https://journal.example/index?page=2", link.url)
        self.assertEqual("page", link.kind)

    def test_identifies_official_site_from_surrounding_text(self):
        page = self.analyze("""
            <p>本刊官方网站：<a href="https://journal.university.example/">
                https://journal.university.example/</a></p>
            <a href="https://other.example/">友情链接</a>
        """)
        link, = page.links
        self.assertEqual("official", link.kind)
        self.assertEqual("https://journal.university.example/", link.url)

    def test_static_data_href_and_public_dynamic_navigation(self):
        page = self.analyze("""
            <a href="#" data-href="/guide">投稿须知</a>
            <button onclick="loadDownloads()">下载中心</button>
            <button onclick="login()">作者登录</button>
        """)
        link, = page.links
        self.assertEqual("https://journal.example/guide", link.url)
        self.assertEqual(["下载中心"], page.dynamic_controls)

    def test_finds_cnki_announcement_title_and_date(self):
        page = self.analyze("""
            <h1>示例期刊</h1><div class="paperDetail_tt">《示例期刊》征稿启事</div>
            <div class="paperDetail_publishTimeDate">发布时间：2026-09-15</div>
            <div class="paperDetail_html pd1"><p>正文使用宋体。</p></div>
        """)
        self.assertEqual("《示例期刊》征稿启事", page.title)
        self.assertEqual("2026-09-15", page.published_at)
        self.assertIn("正文使用宋体。", page.markdown)
