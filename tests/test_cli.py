import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import Workbook

from periodical_collector import __main__ as cli


class CommandLineTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.excel = self.root / "期刊链接.xlsx"
        book = Workbook()
        sheet = book.active
        sheet.title = "期刊链接"
        sheet.append(["编号", "journal_id", "期刊名", "学科", "官网"])
        sheet.append([1, "J-001", "财经法学", "法学", "https://journal.example/"])
        sheet.append([2, "J-002", "缺少网址", "法学", None])
        sheet.append([3, "J-003", "其他期刊", "法学", "https://second.example/"])
        book.save(self.excel)
        book.close()

    def run_cli(self, *args):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            result = cli.main([
                *args, "--excel", str(self.excel), "--output", str(self.root / "results")
            ])
        self.assertEqual(result, 0)
        return output.getvalue()

    @staticmethod
    def collect_result(journal, directory, config):
        result = {"status": "completed", "artifacts": [
            {"type": "attachment", "format": "pdf", "local_path": "格式.pdf"}
        ]}
        (directory / "格式.pdf").write_bytes(b"%PDF-fixture")
        (directory / "manifest.json").write_text(json.dumps(result), encoding="utf-8")
        return result

    def test_single_finds_journal_and_forwards_official_entry(self):
        with patch.object(cli, "collect", side_effect=self.collect_result) as collect:
            output = self.run_cli(
                "single", "--journal", "《财经法学》",
                "--entry-url", "https://publisher.example/guide",
            )
        journal, _, config = collect.call_args.args
        self.assertEqual((journal.name, journal.journal_id, journal.row), ("财经法学", "J-001", 2))
        self.assertEqual(journal.extra_urls, ("https://publisher.example/guide",))
        self.assertFalse(config.use_browser_use)
        self.assertIn("completed", output)

    def test_batch_limit_and_invalid_rows_are_reported(self):
        with patch.object(cli, "collect", side_effect=self.collect_result) as collect:
            self.run_cli("batch", "--limit", "2")
        self.assertEqual(collect.call_count, 1)
        [summary] = (self.root / "results").glob("batch-*/batch-summary.json")
        data = json.loads(summary.read_text(encoding="utf-8"))
        self.assertEqual([row["status"] for row in data["results"]], ["completed", "uncompleted"])
        self.assertIn("官网", data["results"][1]["error"])

    def test_batch_resume_skips_completed_matching_run(self):
        with patch.object(cli, "collect", side_effect=self.collect_result):
            self.run_cli("batch", "--limit", "1")
        with patch.object(cli, "collect") as collect:
            self.run_cli("batch", "--limit", "1", "--resume")
        collect.assert_not_called()
        self.assertEqual(len(list((self.root / "results").glob("batch-*/batch-summary.json"))), 2)

    def test_batch_resume_runs_again_if_previous_original_was_removed(self):
        with patch.object(cli, "collect", side_effect=self.collect_result):
            self.run_cli("batch", "--limit", "1")
        [original] = (self.root / "results").glob("*/run-*/格式.pdf")
        original.unlink()
        with patch.object(cli, "collect", side_effect=self.collect_result) as collect:
            self.run_cli("batch", "--limit", "1", "--resume")
        collect.assert_called_once()
