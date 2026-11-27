import os
import unittest
from unittest.mock import patch

from periodical_collector.browser import browser_use_scan


class BrowserUseTests(unittest.TestCase):
    def test_scan_constrains_network_and_validates_both_citation_urls(self):
        replies = {
            ("POST", "network-policies"): {"id": "929f9625-9cba-48df-86cf-1b2138bc0344"},
            ("POST", "runs"): {"id": "12bb7280-f5d4-4db7-b2dc-49293b28cace", "status": "completed"},
            ("GET", "runs/12bb7280-f5d4-4db7-b2dc-49293b28cace"): {"output": {"candidates": [
                {"url": "https://journal.example/template.docx", "label": "投稿模板",
                 "source_url": "https://journal.example/guide", "path": ["作者指南"], "kind": "attachment"},
                {"url": "https://outside.example/file.pdf", "label": "外域下载",
                 "source_url": "https://journal.example/guide", "path": ["作者指南"], "kind": "attachment"},
                {"url": "https://journal.example/guide", "label": "外域来源",
                 "source_url": "https://outside.example/guide", "path": ["作者指南"], "kind": "page"},
            ]}},
            ("DELETE", "network-policies/929f9625-9cba-48df-86cf-1b2138bc0344"): {},
        }
        requests = []

        def fake_call(method, path, headers, timeout, **kwargs):
            requests.append((method, path, kwargs))
            return replies[method, path]

        with patch.dict(os.environ, {"BROWSER_USE_API_KEY": "test-key"}):
            with patch("periodical_collector.browser._cloud_request", side_effect=fake_call):
                result = browser_use_scan(
                    journal="测试期刊", homepage="https://journal.example/",
                    allowed_hosts={"journal.example"}, visited=[], found=[], unresolved=["稿件模板"],
                    timeout=5,
                )

        self.assertEqual([item.url for item in result], ["https://journal.example/template.docx"])
        self.assertEqual(requests[0][2]["json"]["defaultAction"], "block")
        self.assertEqual(requests[0][2]["json"]["allow"]["domains"], ["journal.example"])
        self.assertEqual(requests[1][2]["json"]["browserSettings"]["networkPolicyId"],
                         replies["POST", "network-policies"]["id"])
        self.assertEqual(requests[-1][0], "DELETE")

    def test_scan_cancels_run_when_status_poll_times_out(self):
        run_id = "12bb7280-f5d4-4db7-b2dc-49293b28cace"
        policy_id = "929f9625-9cba-48df-86cf-1b2138bc0344"
        calls = []

        def fake_call(method, path, headers, timeout, **kwargs):
            calls.append((method, path))
            if path == "network-policies":
                return {"id": policy_id}
            if path == "runs":
                return {"id": run_id, "status": "running"}
            if path == f"runs/{run_id}/status":
                raise TimeoutError("poll timed out")
            if path == f"runs/{run_id}/cancel":
                return {"status": "cancelled"}
            return {}

        with patch.dict(os.environ, {"BROWSER_USE_API_KEY": "test-key"}):
            with patch("periodical_collector.browser._cloud_request", side_effect=fake_call):
                with self.assertRaises(TimeoutError):
                    browser_use_scan(
                        journal="测试期刊", homepage="https://journal.example/",
                        allowed_hosts={"journal.example"}, visited=[], found=[], unresolved=[],
                        timeout=5,
                    )

        self.assertEqual(calls[-2:], [
            ("POST", f"runs/{run_id}/cancel"),
            ("DELETE", f"network-policies/{policy_id}"),
        ])
