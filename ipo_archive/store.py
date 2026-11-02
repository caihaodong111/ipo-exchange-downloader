from dataclasses import asdict
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import csv
import fcntl
import json
import os
import sqlite3
import time

from .models import ArchiveError, EXCHANGES, component


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    with temp.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temp, path)


@contextmanager
def instance_lock(state_dir):
    Path(state_dir).mkdir(parents=True, exist_ok=True)
    with (Path(state_dir) / "run.lock").open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ArchiveError("已有下载或扫描任务运行中，本次不重复启动") from exc
        handle.seek(0)
        handle.truncate()
        handle.write(str(os.getpid()))
        handle.flush()
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


class Store:
    def __init__(self, state_dir):
        self.path = Path(state_dir) / "index.sqlite3"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS projects (
            key TEXT PRIMARY KEY, exchange TEXT NOT NULL, project_id TEXT NOT NULL,
            name TEXT NOT NULL, folder TEXT NOT NULL, source_url TEXT NOT NULL,
            fingerprint TEXT NOT NULL, detail_fingerprint TEXT, checked_at REAL,
            last_seen REAL NOT NULL, last_error TEXT, retry_at REAL NOT NULL DEFAULT 0);
          CREATE TABLE IF NOT EXISTS documents (
            project_key TEXT NOT NULL, key TEXT NOT NULL, title TEXT NOT NULL, url TEXT NOT NULL,
            category TEXT NOT NULL, published TEXT, revision TEXT, metadata TEXT NOT NULL,
            relative_path TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
            size INTEGER, sha256 TEXT, etag TEXT, last_modified TEXT,
            discovered_at REAL NOT NULL, downloaded_at REAL,
            attempts INTEGER NOT NULL DEFAULT 0, retry_at REAL NOT NULL DEFAULT 0, last_error TEXT,
            PRIMARY KEY(project_key, key));
          CREATE TABLE IF NOT EXISTS runtime (key TEXT PRIMARY KEY, value TEXT NOT NULL);
          CREATE INDEX IF NOT EXISTS pending_documents ON documents(status,retry_at);
          PRAGMA user_version=1;
        """)
        self.db.commit()

    def close(self):
        self.db.close()

    def get_project(self, key):
        return self.db.execute("SELECT * FROM projects WHERE key=?", (key,)).fetchone()

    def see(self, project):
        previous = self.get_project(project.key)
        if previous:
            folder = previous["folder"]  # Renaming an issuer does not orphan its downloaded history.
        else:
            folder_name = component(project.name, 180)
            same = self.db.execute("SELECT name, folder FROM projects WHERE exchange=?", (project.exchange,)).fetchall()
            collision = any(Path(row["folder"]).name == folder_name and row["name"] != project.name for row in same)
            if collision:
                folder_name += "__" + project.id
            folder = str(Path(EXCHANGES[project.exchange]) / folder_name)
        with self.db:
            self.db.execute("""INSERT INTO projects(key,exchange,project_id,name,folder,source_url,fingerprint,last_seen)
                VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET
                name=excluded.name,source_url=excluded.source_url,fingerprint=excluded.fingerprint,last_seen=excluded.last_seen""",
                (project.key, project.exchange, project.id, project.name, folder, project.source_url, project.fingerprint, time.time()))
        return self.get_project(project.key)

    def needs_check(self, project, days, force=False, overlap_days=2):
        row = self.get_project(project.key)
        # Some exchange lists expose only a date. A reply published after an
        # earlier same-day check must still be discovered on the next run.
        cutoff = (datetime.fromtimestamp(row["checked_at"] or 0, timezone(timedelta(hours=8)))
                  - timedelta(days=overlap_days)).date().isoformat()
        recent = bool(project.updated and project.updated >= cutoff)
        return force or (row["retry_at"] <= time.time() and
            (row["fingerprint"] != row["detail_fingerprint"] or not row["checked_at"] or recent
             or row["checked_at"] < time.time() - days * 86400))

    def save_details(self, project, documents):
        with self.db:
            new = self._add_documents(project.key, documents)
            self.db.execute("UPDATE projects SET detail_fingerprint=?,checked_at=?,last_error=NULL,retry_at=0,name=? WHERE key=?",
                            (project.fingerprint, time.time(), project.name, project.key))
        return new

    def _add_documents(self, project_key, documents):
        row = self.get_project(project_key)
        new = 0
        for doc in documents:
            relative = str(Path(row["folder"]) / doc.category / doc.filename(row["project_id"]))
            cursor = self.db.execute("""INSERT OR IGNORE INTO documents
                (project_key,key,title,url,category,published,revision,metadata,relative_path,discovered_at)
                VALUES(?,?,?,?,?,?,?,?,?,?)""", (project_key, doc.key, doc.title, doc.url, doc.category,
                doc.published, doc.revision, json.dumps(doc.metadata, ensure_ascii=False), relative, time.time()))
            new += cursor.rowcount
        return new

    def add_documents(self, project_key, documents):
        with self.db:
            return self._add_documents(project_key, documents)

    def project_error(self, key, error):
        with self.db:
            self.db.execute("UPDATE projects SET last_error=?,retry_at=? WHERE key=?",
                            (str(error)[:1000], time.time() + max(120, getattr(error, "cooldown", 0)), key))

    def pending(self, project_key=None, exchange=None):
        sql = """SELECT d.*,p.exchange FROM documents d JOIN projects p ON p.key=d.project_key
                 WHERE d.status!='downloaded' AND d.retry_at<=?"""
        params = [time.time()]
        if project_key:
            sql += " AND d.project_key=?"
            params.append(project_key)
        if exchange:
            sql += " AND p.exchange=?"
            params.append(exchange)
        return self.db.execute(sql + " ORDER BY d.discovered_at,d.project_key,d.key", params).fetchall()

    def complete(self, row, result):
        with self.db:
            self.db.execute("""UPDATE documents SET status='downloaded',size=?,sha256=?,etag=?,last_modified=?,
                downloaded_at=?,last_error=NULL,retry_at=0 WHERE project_key=? AND key=?""",
                (result["size"], result["sha256"], result.get("etag"), result.get("last_modified"), time.time(), row["project_key"], row["key"]))

    def fail(self, row, error):
        wait = max(getattr(error, "cooldown", 0), min(86400, 120 * 2 ** min(10, row["attempts"])))
        with self.db:
            self.db.execute("""UPDATE documents SET status='failed',attempts=attempts+1,last_error=?,retry_at=?
                WHERE project_key=? AND key=?""", (str(error)[:1000], time.time() + wait, row["project_key"], row["key"]))

    def counts(self):
        return {"companies": self.db.execute("SELECT COUNT(DISTINCT folder) FROM projects").fetchone()[0],
                "projects": self.db.execute("SELECT COUNT(*) FROM projects").fetchone()[0],
                "documents": dict(self.db.execute("SELECT status,COUNT(*) FROM documents GROUP BY status").fetchall())}

    def runtime(self, key, value=None):
        if value is None:
            row = self.db.execute("SELECT value FROM runtime WHERE key=?", (key,)).fetchone()
            return json.loads(row[0]) if row else None
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO runtime VALUES(?,?)", (key, json.dumps(value)))

    def export_csv(self, filename):
        rows = self.db.execute("""SELECT p.exchange,p.name,p.project_id,d.category,d.title,d.published,d.status,
            d.relative_path,d.url,d.sha256,d.size,d.last_error FROM documents d JOIN projects p ON p.key=d.project_key
            ORDER BY p.exchange,p.name,d.category,d.published,d.key""")
        temp = Path(str(filename) + ".tmp")
        temp.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["交易所", "公司", "项目编号", "类别", "标题", "披露日期", "下载状态", "保存位置", "官方链接", "SHA256", "字节数", "最近错误"])
            for row in rows:
                values = list(row)
                values[0] = EXCHANGES[values[0]]
                # CSV is intended for Excel; neutralize formula-like titles/URLs.
                writer.writerow([("'" + v) if isinstance(v, str) and v.startswith(("=", "+", "-", "@")) else v for v in values])
        os.replace(temp, filename)
