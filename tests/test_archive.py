"""Offline regression tests. Only localhost TLS receives attachment requests."""
from collections import Counter
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from unittest.mock import patch
import io
from datetime import datetime, timedelta, timezone
import json
import logging
import os
import socket
import ssl
import subprocess
import time
import unittest

from ipo_archive.cli import DEFAULT_CONFIG, load_config, main
from ipo_archive.models import ArchiveError, SchemaError, Project, Document, CATEGORIES, component, official_url
from ipo_archive.exchanges import ExchangeAPI, parse_documents, parse_page, project_from_row, sse_category
from ipo_archive.store import Store, atomic_json, instance_lock
from ipo_archive.engine import Engine
from ipo_archive.download import Downloader, file_hash, html_attachments
from ipo_archive.transport import Transport, parse_json, retry_after, parse_headers

FIXTURES = Path(__file__).parent / "fixtures"
LOG = logging.getLogger("tests")
LOG.addHandler(logging.NullHandler())
PDF = b"%PDF-1.7\n" + b"test-local-fixture\n" * 200 + b"\n%%EOF\n"


def fixture(name):
    return json.loads((FIXTURES / name).read_text())


def sample(exchange):
    detail = fixture(exchange + "-detail.json")
    row = fixture("sse-list.json")["result"][0] if exchange == "sse" else detail["data"] if exchange == "szse" else detail[0]["projectNews"]
    return project_from_row(exchange, row), detail


class AdapterTests(unittest.TestCase):
    def test_real_official_metadata_categories(self):
        expected = {"sse": (6, {CATEGORIES[0]: 6}),
                    "szse": (25, {CATEGORIES[0]: 20, CATEGORIES[1]: 3, CATEGORIES[2]: 2}),
                    "bse": (28, {CATEGORIES[0]: 15, CATEGORIES[1]: 10, CATEGORIES[2]: 2, CATEGORIES[4]: 1})}
        for exchange, (count, categories) in expected.items():
            project, payload = sample(exchange)
            docs = parse_documents(project, payload)
            self.assertEqual(len(docs), count)
            self.assertEqual(dict(Counter(d.category for d in docs)), categories)
            self.assertTrue(all(d.url.startswith("https://") for d in docs))

    def test_three_official_pagination_schemas(self):
        for exchange, total in (("sse", 1283), ("szse", 1461), ("bse", 874)):
            projects, actual, pages = parse_page(exchange, fixture(exchange + "-list.json"), 0)
            self.assertEqual(actual, total)
            self.assertGreater(pages, 1)
            self.assertTrue(projects)
            with self.assertRaises(SchemaError):
                parse_page(exchange, fixture(exchange + "-list.json"), 1)

    def test_missing_group_and_wrong_project_never_succeed(self):
        project, payload = sample("bse")
        del payload[0]["hztzInfo"]
        with self.assertRaises(SchemaError):
            parse_documents(project, payload)
        project, payload = sample("szse")
        payload["data"]["prjid"] = 999
        with self.assertRaises(SchemaError):
            parse_documents(project, payload)

    def test_nested_supplementary_material_and_future_groups_retained(self):
        project, payload = sample("szse")
        doc = deepcopy(payload["data"]["disclosureMaterials"][0])
        doc.update(dfid=999999, dfpth="/new-supplement.pdf")
        payload["data"]["disclosureMaterials"][0]["subInfoDisclosureList"].append(doc)
        payload["data"]["futureAttachmentGroup"] = [dict(doc, dfid=999998, dfpth="/unknown-category.pdf")]
        docs = parse_documents(project, payload)
        self.assertEqual(len(docs), 27)
        self.assertEqual(sum(d.category == CATEGORIES[5] for d in docs), 1)

    def test_sse_uses_codes_not_title_guessing(self):
        for code, index in (("I3010", 1), ("I2011", 2), ("I2021", 2), ("I1020", 3), ("I1010", 4), ("NEW", 5)):
            self.assertEqual(sse_category({"fileTypeMap": code, "fileTitle": "招股说明书"}), CATEGORIES[index])

    def test_same_url_new_revision_is_preserved(self):
        project, payload = sample("szse")
        before = parse_documents(project, payload)[0]
        payload["data"]["disclosureMaterials"][0]["ddtime"] = "2026-11-02 17:00:00"
        after = parse_documents(project, payload)[0]
        self.assertEqual(before.url, after.url)
        self.assertNotEqual(before.key, after.key)
        self.assertNotEqual(before.filename(project.id), after.filename(project.id))

    def test_external_urls_and_path_traversal(self):
        for value in ("https://evil.test/a.pdf", "https://www.bse.cn@evil.test/a.pdf", "file:///etc/passwd", "https://www.bse.cn:8000/a.pdf"):
            with self.assertRaises(SchemaError):
                official_url(value)
        self.assertNotIn("/", component("../../a/b\\c"))
        self.assertLessEqual(len(component("中文" * 300).encode()), 150)

    def test_jsonp_is_data_and_never_executed(self):
        self.assertEqual(parse_json(b'callback([{"x":1}]);'), [{"x": 1}])
        with self.assertRaises(SchemaError):
            parse_json(b'callback([]);process.exit()')
        self.assertEqual(retry_after("120"), 120)
        self.assertEqual(retry_after("bad value"), 0)
        self.assertEqual(parse_headers("HTTP/1.1 200 Connection established\r\n\r\nHTTP/2 429\r\nRetry-After: 20\r\n\r\n"), (429, {"retry-after": "20"}))

    def test_bse_jsonp_query_parameters_are_not_duplicated(self):
        with TemporaryDirectory() as directory:
            transport = Transport(directory, DEFAULT_CONFIG, LOG)
            with patch.object(transport, "request") as request:
                body = Path(directory) / "data.json"
                body.write_text("[]")
                from ipo_archive.transport import Response
                request.return_value = Response(200, {}, body, "https://www.bse.cn/api")
                transport.json("https://www.bse.cn/api?callback=ipo", "bse", {"page": 1})
                args = request.call_args.args
                self.assertEqual(args, ("https://www.bse.cn/api?callback=ipo&page=1", "bse"))

    def test_official_html_attachment_links_are_scoped(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "result.shtml"
            path.write_text('<html><a href="./result.pdf">注册批复</a><a href="https://evil.test/steal.pdf">bad</a></html>')
            docs = html_attachments({"url": "https://www.csrc.gov.cn/csrc/result.shtml", "category": CATEGORIES[4],
                                     "published": "2026-11-02", "revision": "r1"}, path)
            self.assertEqual(len(docs), 1)
            self.assertEqual(docs[0].url, "https://www.csrc.gov.cn/csrc/result.pdf")


class FakeHTTP:
    def __init__(self):
        self.blocked = {}
        self.requests = []

    def request(self, *args, **kwargs):
        self.requests.append(args)
        raise AssertionError("metadata-only run must not request attachments")


class FakeAPI:
    def __init__(self):
        self.http = FakeHTTP()
        self.project, self.payload = sample("szse")
        # Keep unchanged-project scenarios independent of the date CI runs.
        self.project = replace(self.project, updated="2000-01-01")
        self.detail_calls = 0
        self.fail = False
        self.page_calls = []

    def page(self, exchange, page):
        self.page_calls.append(page)
        return [self.project], 1, 1

    def documents(self, project):
        self.detail_calls += 1
        if self.fail:
            raise SchemaError("site changed")
        return parse_documents(project, self.payload)


class IndexEngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.config = dict(DEFAULT_CONFIG, state_dir=str(self.base / "state"), output_dir=str(self.base / "files"))
        self.store = Store(self.config["state_dir"])
        self.api = FakeAPI()

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def engine(self):
        return Engine(self.config, self.store, self.api, LOG)

    def test_scan_never_downloads_and_next_sync_keeps_pending(self):
        self.engine().run(["szse"])
        self.assertEqual(self.api.http.requests, [])
        self.assertFalse(Path(self.config["output_dir"]).exists())
        self.assertEqual(len(self.store.pending()), 25)
        self.engine().run(["szse"])
        self.assertEqual(self.api.detail_calls, 1)
        self.assertEqual(len(self.store.pending()), 25)

    def test_only_changed_company_gets_details(self):
        first = self.api.project
        second = replace(first, id="1000001", name="另一家公司", fingerprint="second")
        self.store.see(first)
        self.store.save_details(first, [])
        self.store.see(second)
        self.store.save_details(second, [])
        updated = replace(first, fingerprint="same-day-new-status")
        self.api.page = lambda exchange, page: ([updated, second], 2, 1)
        self.engine().run(["szse"])
        self.assertEqual(self.api.detail_calls, 1)

    def test_periodic_recheck_finds_silent_supplement(self):
        self.engine().run(["szse"])
        with self.store.db:
            self.store.db.execute("UPDATE projects SET checked_at=?", (time.time() - 8 * 86400,))
        self.engine().run(["szse"])
        self.assertEqual(self.api.detail_calls, 2)

    def test_same_day_supplement_rechecked_even_if_list_date_unchanged(self):
        today = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        self.api.project = replace(self.api.project, updated=today)
        self.engine().run(["szse"])
        self.engine().run(["szse"])
        self.assertEqual(self.api.detail_calls, 2)

    def test_failure_does_not_advance_details_checkpoint(self):
        self.api.fail = True
        result = self.engine().run(["szse"])
        row = self.store.get_project(self.api.project.key)
        self.assertIsNone(row["checked_at"])
        self.assertIsNone(row["detail_fingerprint"])
        self.assertEqual(result["run"]["detail_errors"], 1)

    def test_repeated_schema_errors_pause_exchange(self):
        projects = [replace(self.api.project, id=str(i)) for i in range(10)]
        self.api.page = lambda exchange, page: (projects, 10, 1)
        self.api.fail = True
        result = self.engine().run(["szse"])
        self.assertEqual(self.api.detail_calls, 3)
        self.assertGreater(self.store.runtime("cooldowns")["szse"], time.time())
        self.assertEqual(result["run"]["exchange_errors"], 1)

    def test_scan_checks_all_pages_and_rejects_repeated_page(self):
        self.api.page = lambda exchange, page: ([self.api.project], 2, 2)
        result = self.engine().run(["szse"])
        self.assertEqual(result["run"]["exchange_errors"], 1)
        self.assertIsNone(self.store.runtime("last_complete_scan_szse"))

    def test_failed_download_remains_retryable(self):
        self.engine().run(["szse"])
        row = self.store.pending()[0]
        self.store.fail(row, ArchiveError("temporary", 20))
        saved = self.store.db.execute("SELECT * FROM documents WHERE key=?", (row["key"],)).fetchone()
        self.assertEqual(saved["status"], "failed")
        self.assertGreater(saved["retry_at"], time.time())

    def test_renamed_company_keeps_archive_folder(self):
        row = self.store.see(self.api.project)
        updated = replace(self.api.project, name="公司新名称")
        after = self.store.see(updated)
        self.assertEqual(row["folder"], after["folder"])

    def test_single_instance_lock(self):
        with instance_lock(self.config["state_dir"]):
            with self.assertRaises(ArchiveError):
                with instance_lock(self.config["state_dir"]):
                    pass

    def test_verify_missing_file_goes_back_to_pending(self):
        self.engine().run(["szse"])
        row = self.store.pending()[0]
        self.store.complete(row, {"size": 10, "sha256": "missing"})
        self.assertEqual(self.engine().reconcile_local(), 1)
        self.assertTrue(any(r["key"] == row["key"] for r in self.store.pending()))

    def test_no_download_flag_is_rejected_before_network(self):
        with self.assertRaises(SystemExit) as result, patch("sys.stderr", new=io.StringIO()):
            main(["sync"])
        self.assertEqual(result.exception.code, 2)


class LocalHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    hits = Counter()
    ranges = []

    def log_message(self, *args):
        pass

    def do_GET(self):
        self.hits[self.path] += 1
        if self.path == "/rate.pdf":
            self.send_response(429)
            self.send_header("Retry-After", "120")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path == "/fake.pdf":
            body = b"<html>access denied</html>"
            self.send_response(200)
        else:
            body = PDF
            offset = 0
            range_header = self.headers.get("Range")
            self.ranges.append((self.path, range_header, self.headers.get("If-Range")))
            if range_header and self.path != "/ignore.pdf":
                offset = int(range_header.split("=")[1].split("-")[0])
                body = PDF[offset:]
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {offset}-{len(PDF)-1}/{len(PDF)}")
            else:
                self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("ETag", '"fixture-v1"')
        self.end_headers()
        if self.path == "/partial.pdf" and self.hits[self.path] == 1:
            self.wfile.write(body[:len(body)//2])
            self.wfile.flush()
            self.close_connection = True
            return
        self.wfile.write(body)


class DownloadIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = TemporaryDirectory()
        cls.base = Path(cls.temp.name)
        cert, key = cls.base / "cert.pem", cls.base / "key.pem"
        openssl_config = cls.base / "openssl.cnf"
        openssl_config.write_text("[req]\ndistinguished_name=dn\nx509_extensions=ext\nprompt=no\n[dn]\nCN=localhost\n[ext]\nsubjectAltName=DNS:localhost\nbasicConstraints=critical,CA:TRUE\n")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-keyout", str(key), "-out", str(cert), "-config", str(openssl_config)], check=True, capture_output=True)
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), LocalHandler)
        tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        tls.load_cert_chain(cert, key)
        cls.server.socket = tls.wrap_socket(cls.server.socket, server_side=True)
        cls.url = "https://localhost:" + str(cls.server.server_port)
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.env = patch.dict(os.environ, {"CURL_CA_BUNDLE": str(cert)})
        cls.env.start()
        # Scoped to this localhost TLS fixture, never used by production code.
        cls.urls = patch("ipo_archive.transport.official_url", side_effect=lambda url: url)
        cls.urls.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.urls.stop()
        cls.env.stop()
        cls.temp.cleanup()

    def setUp(self):
        self.run_dir = TemporaryDirectory()
        self.root = Path(self.run_dir.name)
        config = dict(DEFAULT_CONFIG, request_interval_seconds=0, jitter_seconds=0, request_timeout_seconds=5)
        self.http = Transport(self.root / "state", config, LOG, sleep=lambda _: None)
        self.downloader = Downloader(self.http, self.root / "archive", 1024 * 1024)

    def tearDown(self):
        self.run_dir.cleanup()

    def row(self, name):
        return {"url": self.url + "/" + name, "revision": "v1", "relative_path": "交易所/公司/信息披露/" + name, "exchange": "sse"}

    def test_download_idempotency_and_crash_recovery(self):
        row = self.row("normal.pdf")
        result = self.downloader.download(row)
        hits = LocalHandler.hits["/normal.pdf"]
        recovered = self.downloader.download(row)
        self.assertEqual(recovered["sha256"], result["sha256"])
        self.assertEqual(LocalHandler.hits["/normal.pdf"], hits)
        self.assertEqual(self.downloader.target(row["relative_path"]).read_bytes(), PDF)

    def test_interrupted_download_resumes_with_if_range(self):
        row = self.row("partial.pdf")
        with self.assertRaises(ArchiveError):
            self.downloader.download(row)
        target = self.downloader.target(row["relative_path"])
        self.assertFalse(target.exists())
        self.assertEqual(Path(str(target) + ".part").stat().st_size, len(PDF)//2)
        result = self.downloader.download(row)
        self.assertEqual(target.read_bytes(), PDF)
        self.assertEqual(result["size"], len(PDF))
        self.assertTrue(any(path == "/partial.pdf" and header and validator == '"fixture-v1"' for path, header, validator in LocalHandler.ranges))

    def test_ignored_range_restarts_without_double_append(self):
        row = self.row("ignore.pdf")
        target = self.downloader.target(row["relative_path"])
        target.parent.mkdir(parents=True)
        Path(str(target) + ".part").write_bytes(PDF[:100])
        atomic_json(Path(str(target) + ".part.json"), {"url": row["url"], "revision": "v1", "etag": '"fixture-v1"',
                    "size": 100, "sha256": file_hash(Path(str(target) + ".part"))})
        self.downloader.download(row)
        self.assertEqual(target.read_bytes(), PDF)
        self.assertTrue(any(path == "/ignore.pdf" and header == "bytes=100-" for path, header, _ in LocalHandler.ranges))

    def test_html_error_is_never_saved_as_pdf(self):
        row = self.row("fake.pdf")
        with self.assertRaises(ArchiveError):
            self.downloader.download(row)
        self.assertFalse(self.downloader.target(row["relative_path"]).exists())

    def test_rate_limit_stops_further_requests(self):
        row = self.row("rate.pdf")
        with self.assertRaises(ArchiveError) as error:
            self.downloader.download(row)
        self.assertGreaterEqual(error.exception.cooldown, 120)
        before = sum(LocalHandler.hits.values())
        with self.assertRaises(ArchiveError):
            self.downloader.download(self.row("other.pdf"))
        self.assertEqual(sum(LocalHandler.hits.values()), before)

    def test_local_file_corruption_is_retained_then_repaired(self):
        row = self.row("repair.pdf")
        self.downloader.download(row)
        target = self.downloader.target(row["relative_path"])
        target.write_bytes(b"corrupt")
        self.downloader.download(row)
        self.assertEqual(target.read_bytes(), PDF)
        self.assertTrue(list(target.parent.glob("repair.pdf.unverified-*")))

    def test_target_symlink_escape_is_rejected(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.downloader.root.mkdir(parents=True)
        (self.downloader.root / "escape").symlink_to(outside)
        with self.assertRaises(ArchiveError):
            self.downloader.target("escape/a.pdf")

    def test_shared_announcement_only_fetches_once(self):
        config = dict(DEFAULT_CONFIG, output_dir=str(self.root / "archive"), state_dir=str(self.root / "state"))
        store = Store(config["state_dir"])
        try:
            for ident in ("1", "2"):
                project = Project("sse", ident, "公司" + ident, "https://www.sse.com.cn/", "f")
                store.see(project)
                store.save_details(project, [Document("共享会议公告", self.url + "/shared.pdf", CATEGORIES[2], "2026-11-02", "v1")])
            api = FakeAPI()
            api.http = self.http
            engine = Engine(config, store, api, LOG)
            before = LocalHandler.hits["/shared.pdf"]
            engine.download_pending(["sse"])
            self.assertEqual(LocalHandler.hits["/shared.pdf"] - before, 1)
            self.assertEqual(engine.stats["reused_shared_files"], 1)
            self.assertEqual(store.counts()["documents"], {"downloaded": 2})
        finally:
            store.close()


if __name__ == "__main__":
    unittest.main()
