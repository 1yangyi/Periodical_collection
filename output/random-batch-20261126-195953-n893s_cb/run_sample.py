"""Run this recorded sample with HTTP first and no-original-only cloud fallback."""
from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from periodical_collector.crawler import Collection, CrawlConfig, Journal, Lead, safe_name
from periodical_collector.http import HttpClient

OUTPUT = Path(__file__).resolve().parent


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


class NoOriginalFallback(Collection):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fallback_invoked = False
        self.before_fallback = None

    def use_agent(self):
        # The existing engine can also request a scan for pending issues.
        # This batch permits it only when no validated Word/PDF exists.
        if self.has_original():
            return
        self.fallback_invoked = True
        self.before_fallback = {
            "pages": len(self.pages),
            "artifacts": len(self.artifacts),
            "pending": len(self.pending),
        }
        print(f"[{now()}] {self.journal.name}: 无有效 Word/PDF，开始 Browser Use 补查", flush=True)
        super().use_agent()


def main():
    sample = json.loads((OUTPUT / "sample.json").read_text(encoding="utf-8"))
    summary_path = OUTPUT / "batch-summary.json"
    if summary_path.exists():
        raise SystemExit("This recorded sample has already been started; refusing a duplicate paid run.")
    config = CrawlConfig(use_playwright=False, use_browser_use=True)
    summary = {"started_at": now(), "source": sample["source"],
               "policy": sample["policy"], "planned_count": len(sample["sample"]),
               "counts": {"completed": 0, "uncompleted": 0}, "results": []}
    write_json(summary_path, summary)
    for index, record in enumerate(sample["sample"], 1):
        journal = Journal(name=record["name"], url=record["url"], row=record["row"])
        target = OUTPUT / f"{index:02d}_{safe_name(journal.name)}"
        target.mkdir()
        print(f"[{now()}] [{index}/5] 默认采集: {journal.name} {journal.url}", flush=True)
        with HttpClient(config.timeout, config.delay, config.max_bytes) as http:
            collection = NoOriginalFallback(journal, target, config, http)
            try:
                manifest = collection.run()
            except Exception as error:
                collection.issue(Lead(journal.url, journal.name, "", (), 0),
                                 "run_failed", f"{type(error).__name__}: {error}")
                manifest = collection.finish()
        result = {"journal": journal.name, "url": journal.url, "source_row": journal.row,
                  "status": manifest["status"], "browser_use_invoked": collection.fallback_invoked,
                  "before_fallback": collection.before_fallback,
                  "manifest": str((target / "manifest.json").relative_to(OUTPUT)),
                  "pages": len(manifest["pages"]),
                  "attachments": [{"title": item["title"], "format": item["format"],
                                   "local_path": str((target / item["local_path"]).relative_to(OUTPUT))}
                                  for item in manifest["artifacts"] if item["type"] == "attachment"],
                  "webpage_text": sum(item["type"] == "webpage_text" for item in manifest["artifacts"]),
                  "pending": manifest["pending"]}
        summary["results"].append(result)
        summary["counts"][manifest["status"]] += 1
        write_json(summary_path, summary)
        print(f"[{now()}] {journal.name}: {result['status']}, 附件 {len(result['attachments'])}, "
              f"网页正文 {result['webpage_text']}, 待查 {len(result['pending'])}", flush=True)
    summary["finished_at"] = now()
    write_json(summary_path, summary)
    print(summary_path, flush=True)


if __name__ == "__main__":
    main()
