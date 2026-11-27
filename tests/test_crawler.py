import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from periodical_collector.crawler import CrawlConfig, Journal, collect
from periodical_collector.http import DownloadError, Resource


ROOT = "https://journal.example/"


def page(body):
    return ("<html><meta charset='utf-8'>" + body + "</html>").encode()


def word():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>')
        archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body/></w:document>')
    return buffer.getvalue()


class CollectionTest(unittest.TestCase):
    def run_case(self, resources, **options):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        output = Path(tmp.name)

        def fetch(url, **kwargs):
            if url not in resources:
                raise DownloadError("not found")
            body, mime = resources[url]
            return Resource(url, body, mime, "")

        with patch("periodical_collector.crawler.HttpClient.get", side_effect=fetch):
            result = collect(Journal("测试期刊", ROOT), output, CrawlConfig(delay=0, **options))
        return result, output

    def test_copies_web_only_requirements_but_marks_incomplete(self):
        result, output = self.run_case({
            ROOT: (page('<a href="/guide">投稿须知</a>'), "text/html"),
            ROOT + "guide": (page("<main><h1>投稿须知</h1><p>正文采用宋体五号。</p></main>"), "text/html"),
        })
        self.assertEqual(result["status"], "uncompleted")
        self.assertIn("正文采用宋体五号", (output / "测试期刊投稿格式.md").read_text())
        self.assertTrue(any(item["type"] == "webpage" for item in result["artifacts"]))

    def test_downloads_extensionless_files_deduplicates_and_continues_search(self):
        doc = word()
        result, output = self.run_case({
            ROOT: (page('<a href="/guide">投稿须知</a><a href="/downloads">下载中心</a>'), "text/html"),
            ROOT + "guide": (page('<main><h1>投稿须知</h1><a href="/download?id=10">稿件格式.docx</a></main>'), "text/html"),
            ROOT + "downloads": (page('<main><h1>下载中心</h1><a href="/template.docx">稿件模板</a><a href="/fake.pdf">投稿格式</a></main>'), "text/html"),
            ROOT + "download?id=10": (doc, "application/octet-stream"),
            ROOT + "template.docx": (doc, "application/octet-stream"),
            ROOT + "fake.pdf": (page("<h1>请登录</h1>"), "text/html"),
        })
        self.assertEqual(result["status"], "completed")
        attachments = [item for item in result["artifacts"] if item["type"] == "attachment"]
        self.assertEqual(len(attachments), 1)
        self.assertEqual(len(attachments[0]["sources"]), 2)
        self.assertEqual((output / attachments[0]["local_path"]).read_bytes(), doc)
        self.assertTrue(any(item["url"].endswith("fake.pdf") for item in result["pending"]))

    def test_reports_unvisited_candidates_when_page_budget_is_used(self):
        result, _ = self.run_case({
            ROOT: (page('<a href="/guide">投稿须知</a>'), "text/html"),
        }, max_pages=1)
        self.assertEqual(result["status"], "uncompleted")
        self.assertEqual(result["pending"][0]["reason"], "page_limit")

    def test_follows_public_cnki_homepage_after_legacy_redirect(self):
        root = "https://test.cbpt.cnki.net/"
        landing = root + "portal/journal/portal/client/index"
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        replies = {
            root: Resource(root + "EditorGN/Quit.aspx", b"", "text/html", ""),
            landing: Resource(landing, page('<a href="/rules.docx">稿件格式</a>'), "text/html", ""),
            root + "rules.docx": Resource(root + "rules.docx", word(), "application/octet-stream",
                                          "%E6%A0%BC%E5%BC%8F.docx"),
        }
        with patch("periodical_collector.crawler.HttpClient.get", side_effect=lambda url, **_: replies[url]):
            result = collect(Journal("测试期刊", root), Path(tmp.name), CrawlConfig(delay=0))
        self.assertEqual(result["status"], "completed")
        attachment = next(item for item in result["artifacts"] if item["type"] == "attachment")
        self.assertIn("格式_", attachment["local_path"])


if __name__ == "__main__":
    unittest.main()
