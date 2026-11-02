from collections import Counter
from dataclasses import asdict
from pathlib import Path
import json
import time
from collections import deque

from .models import ArchiveError, SchemaError, CATEGORIES, EXCHANGES
from .store import atomic_json
from .download import Downloader, file_hash


class Engine:
    def __init__(self, config, store, api, logger):
        self.config, self.store, self.api, self.log = config, store, api, logger
        self.root = Path(config["output_dir"]).resolve()
        self.downloader = Downloader(api.http, self.root, config["max_file_size_mb"] * 1024 * 1024)
        self.api.http.blocked.update(store.runtime("cooldowns") or {})
        self.stats = Counter()
        self.detail_failure_streak = Counter()

    def save_cooldowns(self):
        self.store.runtime("cooldowns", self.api.http.blocked)

    def process_project(self, project, documents=None, force=False):
        self.stats["projects_seen"] += 1
        self.store.see(project)
        if documents is None and not self.store.needs_check(project, self.config["recheck_days"], force,
                                                          self.config["recent_overlap_days"]):
            self.stats["details_skipped"] += 1
            return
        try:
            documents = self.api.documents(project) if documents is None else documents
            self.stats["details_checked"] += 1
            self.stats["new_documents"] += self.store.save_details(project, documents)
            self.detail_failure_streak[project.exchange] = 0
            self.log.info("%s · %s：%d 个文件条目", EXCHANGES[project.exchange], project.name, len(documents))
        except (ArchiveError, OSError) as error:
            self.store.project_error(project.key, error)
            self.stats["detail_errors"] += 1
            self.detail_failure_streak[project.exchange] += 1
            self.log.error("%s · %s：%s", EXCHANGES[project.exchange], project.name, error)
            if self.detail_failure_streak[project.exchange] >= 3:
                self.api.http.blocked[project.exchange] = max(self.api.http.blocked.get(project.exchange, 0), time.time() + 900)
            if self.api.http.blocked.get(project.exchange, 0) > time.time():
                raise

    def scan_exchange(self, exchange, force=False, max_projects=None):
        seen, page, expected_total, page_signatures = set(), 0, None, set()
        while True:
            projects, total, pages = self.api.page(exchange, page)
            if expected_total is None:
                expected_total = total
            signature = tuple(p.key for p in projects)
            if signature and signature in page_signatures:
                raise SchemaError("分页内容重复，停止该交易所，本次未完成全量发现")
            page_signatures.add(signature)
            self.log.info("%s：第 %d/%d 页，官方记录 %d 项", EXCHANGES[exchange], page + 1, pages, total)
            for project in projects:
                if project.key in seen:
                    continue
                seen.add(project.key)
                self.process_project(project, force=force)
                if max_projects is not None and len(seen) >= max_projects:
                    self.stats["limited_exchanges"] += 1
                    return
            page += 1
            if page >= pages:
                if len(seen) != total or expected_total != total:
                    raise SchemaError("扫描期间列表数量变化或分页有重复/缺项；已保存有效条目，下次会重新分页检查")
                self.stats["complete_exchanges"] += 1
                self.store.runtime("last_complete_scan_" + exchange, time.time())
                return
            if page >= 10000:
                raise SchemaError("分页超过合理上限，停止请求")

    def reconcile_local(self, full_hash=False, exchanges=None):
        rows = self.store.db.execute("""SELECT d.*,p.exchange FROM documents d JOIN projects p ON p.key=d.project_key
                                      WHERE status='downloaded'""").fetchall()
        broken = 0
        for row in rows:
            if exchanges and row["exchange"] not in exchanges:
                continue
            path = self.downloader.target(row["relative_path"])
            good = path.is_file() and path.stat().st_size == row["size"]
            if good and full_hash:
                good = file_hash(path) == row["sha256"]
            if not good:
                with self.store.db:
                    self.store.db.execute("""UPDATE documents SET status='pending',retry_at=0,last_error='本地文件缺失或校验失败'
                        WHERE project_key=? AND key=?""", (row["project_key"], row["key"]))
                broken += 1
        return broken

    def write_company_manifest(self, key):
        project = dict(self.store.get_project(key))
        folder = self.downloader.target(project["folder"])
        for category in CATEGORIES:
            (folder / category).mkdir(parents=True, exist_ok=True)
        documents = [dict(row) for row in self.store.db.execute("SELECT * FROM documents WHERE project_key=? ORDER BY category,published", (key,))]
        atomic_json(folder / ("项目档案_" + project["project_id"] + ".json"), {"project": project, "documents": documents})

    def download_pending(self, exchanges, only_key=None, max_downloads=None):
        self.stats["local_repairs"] += self.reconcile_local(exchanges=exchanges)
        attempts = 0
        touched = set()
        for exchange in exchanges:
            queue = deque(self.store.pending(project_key=only_key, exchange=exchange))
            enqueued = {(row["project_key"], row["key"]) for row in queue}
            while queue:
                row = queue.popleft()
                if self.api.http.blocked.get(exchange, 0) > time.time():
                    break
                if max_downloads is not None and attempts >= max_downloads:
                    self.stats["download_limit_reached"] = 1
                    break
                attempts += 1
                touched.add(row["project_key"])
                try:
                    cached = self.store.db.execute("""SELECT * FROM documents WHERE status='downloaded' AND url=? AND revision=? LIMIT 1""",
                                                   (row["url"], row["revision"])).fetchone()
                    result = self.downloader.reuse(row, cached) if cached else None
                    if result:
                        self.stats["reused_shared_files"] += 1
                    else:
                        result = self.downloader.download(row)
                    self.store.complete(row, result)
                    linked = result.get("linked_documents", [])
                    if linked:
                        self.stats["new_documents"] += self.store.add_documents(row["project_key"], linked)
                        for child in self.store.pending(project_key=row["project_key"]):
                            ident = child["project_key"], child["key"]
                            if ident not in enqueued:
                                queue.append(child)
                                enqueued.add(ident)
                    self.stats["downloaded"] += 1
                    self.log.info("已保存：%s", row["relative_path"])
                except (ArchiveError, OSError, ValueError) as error:
                    self.store.fail(row, error)
                    self.stats["download_errors"] += 1
                    self.log.error("文件保留待重试：%s · %s", row["title"], error)
            if max_downloads is not None and attempts >= max_downloads:
                break
        for key in touched:
            self.write_company_manifest(key)

    def run(self, exchanges, download=False, url=None, force=False, max_projects=None, max_downloads=None):
        started = time.time()
        only_key = None
        try:
            if url:
                project, docs = self.api.by_url(url)
                exchanges = [project.exchange]
                only_key = project.key
                self.process_project(project, documents=docs, force=True)
            else:
                for exchange in exchanges:
                    try:
                        if self.api.http.blocked.get(exchange, 0) > time.time():
                            raise ArchiveError("该交易所仍在冷却期，本次跳过")
                        self.scan_exchange(exchange, force, max_projects)
                    except (ArchiveError, OSError, ValueError) as error:
                        self.stats["exchange_errors"] += 1
                        self.log.error("%s：%s", EXCHANGES[exchange], error)
            if download:
                self.download_pending(exchanges, only_key, max_downloads)
        except (ArchiveError, OSError, ValueError) as error:
            self.stats["exchange_errors"] += 1
            self.log.error("本轮未完成：%s", error)
        finally:
            self.save_cooldowns()
            self.store.export_csv(Path(self.config["state_dir"]) / "文件清单.csv")
            summary = {"mode": "download" if download else "metadata_only", "started_at": started,
                       "finished_at": time.time(), "run": dict(self.stats), "catalog": self.store.counts()}
            atomic_json(Path(self.config["state_dir"]) / "最近运行.json", summary)
        return summary
