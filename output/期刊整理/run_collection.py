"""Execute the recorded accessible-sheet batch using the existing collector."""
from __future__ import annotations

import csv
import hashlib
import json
import logging
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from periodical_collector.crawler import Collection, CrawlConfig, Journal, Lead, safe_name
from periodical_collector.http import HttpClient
from periodical_collector.browser import _browser_use_key

HTTP_WORKERS = 10
CLOUD_WORKERS = 4
CLOUD_SLOTS = threading.BoundedSemaphore(CLOUD_WORKERS)
STATE_LOCK = threading.RLock()
ACTIVE = {}
SUMMARY = {}
CONFIG = CrawlConfig(use_playwright=False, use_browser_use=True)
LOG = logging.getLogger("periodical_batch")


def now():
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def progress():
    with STATE_LOCK:
        write_json(OUTPUT / "progress.json", {
            "updated_at": now(), "planned_count": SUMMARY["planned_count"],
            "processed_count": len(SUMMARY["results"]), "counts": SUMMARY["counts"],
            "browser_use_invocations": sum(r["browser_use_invoked"] for r in SUMMARY["results"]),
            "active": sorted(ACTIVE.values(), key=lambda item: item["index"]),
        })


def stage(record, directory, value):
    with STATE_LOCK:
        state = {"index": record["index"], "journal": record["name"],
                 "stage": value, "updated_at": now(),
                 "directory": str(directory.relative_to(OUTPUT))}
        ACTIVE[record["index"]] = state
        write_json(directory / "execution-state.json", state)
        progress()
    LOG.info("[%d] %s: %s", record["index"], record["name"], value)


class NoOriginalFallback(Collection):
    def __init__(self, *args, record, **kwargs):
        super().__init__(*args, **kwargs)
        self.record = record
        self.fallback_invoked = False
        self.before_fallback = None

    def use_agent(self):
        if self.has_original():
            return
        self.before_fallback = {"pages": len(self.pages), "artifacts": len(self.artifacts),
                                "pending": len(self.pending), "has_original": False}
        stage(self.record, self.output, "waiting_for_browser_use")
        with CLOUD_SLOTS:
            self.fallback_invoked = True
            stage(self.record, self.output, "browser_use")
            super().use_agent()


def directory_for(record):
    return OUTPUT / f"{record['index']:04d}_{safe_name(record['name'])}"


def process(record):
    target = directory_for(record)
    target.mkdir(exist_ok=True)
    result_path = target / "result.json"
    if result_path.is_file():
        result = json.loads(result_path.read_text(encoding="utf-8"))
        if result["url"] != record["url"] or result["journal"] != record["name"]:
            raise RuntimeError("Existing result does not match recorded input")
        return result
    if (target / "execution-state.json").exists():
        raise RuntimeError(f"Interrupted entry requires review before retry to avoid duplicate cloud charges: {target}")
    journal = Journal(name=record["name"], url=record["url"], row=record["source_row"])
    stage(record, target, "http")
    started = now()
    with HttpClient(CONFIG.timeout, CONFIG.delay, CONFIG.max_bytes) as http:
        collection = NoOriginalFallback(journal, target, CONFIG, http, record=record)
        try:
            manifest = collection.run()
        except Exception as error:
            LOG.exception("[%d] collector failure: %s", record["index"], record["name"])
            collection.issue(Lead(journal.url, journal.name, "", (), 0), "run_failed",
                             f"{type(error).__name__}: {error}")
            manifest = collection.finish()
    attachment_list = []
    for item in manifest["artifacts"]:
        if item["type"] == "attachment":
            attachment_list.append({"title": item["title"], "format": item["format"],
                                    "local_path": str((target / item["local_path"]).relative_to(OUTPUT)),
                                    "sha256": item["sha256"], "bytes": item["bytes"]})
    result = {"index": record["index"], "journal": journal.name, "url": journal.url,
              "source_sheet": record["source_sheet"], "source_row": record["source_row"],
              "started_at": started, "finished_at": now(), "status": manifest["status"],
              "browser_use_invoked": collection.fallback_invoked,
              "before_fallback": collection.before_fallback,
              "browser_use_errors": [item["detail"] for item in manifest["pending"]
                                     if item["reason"] == "browser_use_failed"],
              "manifest": str((target / "manifest.json").relative_to(OUTPUT)),
              "pages": len(manifest["pages"]), "attachments": attachment_list,
              "original_count": sum(item["format"] in {"doc", "docx", "pdf"} for item in attachment_list),
              "webpage_text": sum(item["type"] == "webpage_text" for item in manifest["artifacts"]),
              "pending_count": len(manifest["pending"]),
              "pending_reasons": sorted({item["reason"] for item in manifest["pending"]})}
    write_json(result_path, result)
    stage(record, target, "finished")
    LOG.info("[%d] %s: %s; originals=%d; cloud=%s; pending=%d", record["index"],
             journal.name, result["status"], result["original_count"],
             result["browser_use_invoked"], result["pending_count"])
    return result


def save_summary():
    with STATE_LOCK:
        SUMMARY["results"].sort(key=lambda item: item["index"])
        SUMMARY["updated_at"] = now()
        SUMMARY["processed_count"] = len(SUMMARY["results"])
        SUMMARY["browser_use_invocations"] = sum(r["browser_use_invoked"] for r in SUMMARY["results"])
        SUMMARY["original_files"] = sum(r["original_count"] for r in SUMMARY["results"])
        SUMMARY["browser_use_failures"] = sum(bool(r["browser_use_errors"]) for r in SUMMARY["results"])
        write_json(OUTPUT / "batch-summary.json", SUMMARY)
        csv_path = OUTPUT / "期刊整理汇总.csv"
        temp = csv_path.with_suffix(".tmp")
        with temp.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["序号", "期刊名", "官网", "来源工作表", "来源行号", "状态", "Word/PDF数量",
                             "Browser Use补查", "网页正文副本数量", "访问页数", "待查数量", "待查原因", "Browser Use错误", "清单路径"])
            for r in SUMMARY["results"]:
                writer.writerow([r["index"], r["journal"], r["url"], r["source_sheet"], r["source_row"],
                                 r["status"], r["original_count"], "是" if r["browser_use_invoked"] else "否",
                                 r["webpage_text"], r["pages"], r["pending_count"], "; ".join(r["pending_reasons"]),
                                 "; ".join(r["browser_use_errors"]), r["manifest"]])
        temp.replace(csv_path)
        progress()


def main():
    logging.basicConfig(level=logging.INFO, filename=OUTPUT / "run.log", encoding="utf-8",
                        format="%(asctime)s %(levelname)s %(message)s")
    if not _browser_use_key():
        raise SystemExit("BROWSER_USE_API_KEY is missing")
    plan = json.loads((OUTPUT / "input-records.json").read_text(encoding="utf-8"))
    if hashlib.sha256(Path(plan["source"]).read_bytes()).hexdigest() != plan["source_sha256"]:
        raise SystemExit("Input workbook changed after the batch was prepared")
    summary_path = OUTPUT / "batch-summary.json"
    if summary_path.exists():
        SUMMARY.update(json.loads(summary_path.read_text(encoding="utf-8")))
        if SUMMARY.get("finished_at"):
            raise SystemExit("This batch is already finished; no duplicate collection started")
    else:
        SUMMARY.update({"source": plan["source"], "source_sha256": plan["source_sha256"],
                        "source_sheet": plan["sheet"], "policy": plan["policy"],
                        "started_at": now(), "planned_count": plan["planned_count"],
                        "http_workers": HTTP_WORKERS, "cloud_workers": CLOUD_WORKERS,
                        "counts": {"completed": 0, "uncompleted": 0}, "results": [], "worker_errors": []})
    done = {r["index"] for r in SUMMARY["results"]}
    save_summary()
    print(f"[{now()}] 开始: {plan['planned_count']} 本，HTTP 并发 {HTTP_WORKERS}，Browser Use 并发上限 {CLOUD_WORKERS}", flush=True)
    with ThreadPoolExecutor(max_workers=HTTP_WORKERS) as pool:
        futures = {pool.submit(process, record): record for record in plan["records"] if record["index"] not in done}
        for future in as_completed(futures):
            record = futures[future]
            try:
                result = future.result()
            except Exception as error:
                LOG.exception("Worker failed for %s", record["name"])
                with STATE_LOCK:
                    SUMMARY["worker_errors"].append({"index": record["index"], "journal": record["name"],
                                                      "error": f"{type(error).__name__}: {error}"})
                    ACTIVE.pop(record["index"], None)
                    save_summary()
                print(f"[{now()}] 执行异常: {record['name']}: {error}", flush=True)
                continue
            with STATE_LOCK:
                SUMMARY["results"].append(result)
                SUMMARY["counts"][result["status"]] += 1
                ACTIVE.pop(record["index"], None)
                save_summary()
                count = len(SUMMARY["results"])
            if count % 10 == 0 or count == plan["planned_count"]:
                print(f"[{now()}] {count}/{plan['planned_count']}; 完成 {SUMMARY['counts']['completed']}; "
                      f"未完成 {SUMMARY['counts']['uncompleted']}; 原件 {SUMMARY['original_files']}; "
                      f"Browser Use {SUMMARY['browser_use_invocations']}", flush=True)
    SUMMARY["execution_finished_at"] = now()
    if len(SUMMARY["results"]) == plan["planned_count"]:
        SUMMARY["finished_at"] = now()
    save_summary()
    print(f"[{now()}] 批次执行结束: {len(SUMMARY['results'])}/{plan['planned_count']}; "
          f"worker_errors={len(SUMMARY['worker_errors'])}; {summary_path}", flush=True)


if __name__ == "__main__":
    main()
