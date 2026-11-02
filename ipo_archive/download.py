from hashlib import sha256
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit
import json
import os
import re
import shutil

from .models import ArchiveError, SchemaError, Document, EXTENSIONS, official_url, digest
from .store import atomic_json


def file_hash(path):
    result = sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
    return result.hexdigest()


def valid_signature(path, extension):
    with Path(path).open("rb") as stream:
        head = stream.read(1024)
    if not head:
        return False
    if extension == ".pdf":
        return head.startswith(b"%PDF-")
    if extension in (".zip", ".docx", ".xlsx", ".pptx"):
        return head.startswith((b"PK\x03\x04", b"PK\x05\x06"))
    if extension in (".doc", ".xls", ".ppt"):
        return head.startswith(bytes.fromhex("D0CF11E0A1B11AE1"))
    if extension == ".rar":
        return head.startswith(b"Rar!")
    if extension == ".7z":
        return head.startswith(b"7z\xbc\xaf\x27\x1c")
    if extension in (".html", ".htm", ".shtml"):
        data = Path(path).read_bytes()
        text = data.decode("utf-8", errors="replace") if b"charset=gb" not in data[:4096].lower() else data.decode("gb18030", errors="replace")
        return (bool(re.search(r"<html\b|<!doctype\s+html", text, re.I)) and
                not re.search(r"<title[^>]*>\s*(?:403|404|500|502|503)|captcha|Access Denied|访问过于频繁|人机验证|访问被拒绝", text, re.I))
    if extension == ".txt":
        try:
            text = Path(path).read_text("utf-8-sig")
            return bool(text.strip()) and "<html" not in text.lower()
        except UnicodeError:
            return False
    return False


class AttachmentLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links, self.current = [], None

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            values = dict(attrs)
            self.current = {"href": values.get("href", ""), "title": values.get("title", "")}

    def handle_data(self, data):
        if self.current is not None and not self.current["title"]:
            self.current["title"] += data.strip()

    def handle_endtag(self, tag):
        if tag.lower() == "a" and self.current is not None:
            self.links.append(self.current)
            self.current = None


def html_attachments(row, path):
    parser = AttachmentLinks()
    data = Path(path).read_bytes()
    text = data.decode("utf-8", errors="replace")
    if b"charset=gb" in data[:4096].lower():
        text = data.decode("gb18030", errors="replace")
    parser.feed(text)
    result = {}
    for link in parser.links:
        try:
            url = official_url(link["href"], row["url"])
        except SchemaError:
            continue
        extension = Path(urlsplit(url).path).suffix.lower()
        if extension not in EXTENSIONS - {".html", ".htm", ".shtml", ".txt"}:
            continue
        result[url] = Document(link["title"] or Path(urlsplit(url).path).name, url, row["category"],
            row["published"] or "", digest(["html-attachment", row["revision"], url]), metadata={"parent_url": row["url"]})
    return list(result.values())


class Downloader:
    def __init__(self, transport, root, max_bytes):
        self.http, self.root, self.max_bytes = transport, Path(root).resolve(), max_bytes

    def target(self, relative):
        path = self.root / relative
        if not path.resolve().is_relative_to(self.root):
            raise ArchiveError("归档路径超出资料目录")
        return path

    def download(self, row):
        target = self.target(row["relative_path"])
        extension = target.suffix.lower()
        if extension not in EXTENSIONS:
            raise ArchiveError("不支持的文件类型")
        target.parent.mkdir(parents=True, exist_ok=True)
        receipt = Path(str(target) + ".receipt.json")
        if target.exists():
            try:
                saved = json.loads(receipt.read_text())
            except (OSError, ValueError):
                saved = {}
            if (saved.get("url") == row["url"] and saved.get("revision") == row["revision"]
                    and saved.get("size") == target.stat().st_size and saved.get("sha256") == file_hash(target)
                    and valid_signature(target, extension)):
                if extension in (".html", ".htm", ".shtml"):
                    saved["linked_documents"] = html_attachments(row, target)
                return saved
            # Never overwrite an unknown/corrupt local file; retain it for inspection.
            orphan = Path(str(target) + ".unverified-" + file_hash(target)[:12])
            os.replace(target, orphan)
        part, meta = Path(str(target) + ".part"), Path(str(target) + ".part.json")
        try:
            previous = json.loads(meta.read_text())
        except (OSError, ValueError):
            previous = {}
        offset = part.stat().st_size if part.exists() else 0
        validator = previous.get("etag") or previous.get("last_modified")
        if (previous.get("url") != row["url"] or previous.get("revision") != row["revision"]
                or not validator or offset > self.max_bytes or previous.get("size") != offset
                or (offset and previous.get("sha256") != file_hash(part))):
            offset = 0
        headers = {"Range": f"bytes={offset}-", "If-Range": validator} if offset else {}
        response = self.http.request(row["url"], row["exchange"], headers=headers, max_bytes=self.max_bytes, allow_partial=True)
        try:
            if response.status == 416:
                part.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                raise ArchiveError("服务端不接受旧断点，下次将重新下载", 120)
            if response.status not in (200, 206):
                raise ArchiveError("下载接口返回意外状态")
            if response.status == 200:
                offset = 0  # Range ignored / If-Range failed: replace partial, never append.
            etag = response.headers.get("etag", "")
            if etag.startswith("W/"):
                etag = ""  # Weak ETag is not a valid If-Range validator.
            modified = response.headers.get("last-modified", "")
            total = None
            if response.status == 206:
                match = re.fullmatch(r"bytes (\d+)-(\d+)/(\d+)", response.headers.get("content-range", ""))
                if not match or int(match[1]) != offset or int(match[2]) < offset or int(match[3]) <= int(match[2]):
                    raise ArchiveError("断点响应范围异常，拒绝拼接文件")
                if offset and ((previous.get("etag") and etag != previous["etag"]) or
                               (not previous.get("etag") and modified != previous.get("last_modified"))):
                    part.unlink(missing_ok=True)
                    meta.unlink(missing_ok=True)
                    raise ArchiveError("远端文件已变更，旧断点已作废，下次重新下载", 120)
                total = int(match[3])
                expected_body = int(match[2]) - offset + 1
            else:
                length = response.headers.get("content-length", "")
                expected_body = int(length) if length.isdigit() else None
                total = expected_body
            body_size = response.path.stat().st_size
            if total and total > self.max_bytes or offset + body_size > self.max_bytes:
                raise ArchiveError("文件超过大小上限")
            if expected_body is not None and body_size > expected_body:
                raise ArchiveError("下载响应超出声明长度，拒绝写入")
            if not offset and not valid_signature(response.path, extension):
                raise ArchiveError("文件签名不符（可能返回了拦截页），没有保存为正式文件", 900)
            if offset and not valid_signature(part, extension):
                part.unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
                raise ArchiveError("本地断点签名不符，已作废，下次重试")
            with part.open("ab" if offset else "wb") as output, response.path.open("rb") as source:
                for block in iter(lambda: source.read(1024 * 1024), b""):
                    output.write(block)
                output.flush()
                os.fsync(output.fileno())
            atomic_json(meta, {"url": row["url"], "revision": row["revision"], "etag": etag,
                               "last_modified": modified, "total": total,
                               "size": part.stat().st_size, "sha256": file_hash(part)})
            if response.error or (expected_body is not None and body_size != expected_body) or (total is not None and part.stat().st_size != total):
                raise ArchiveError("连接中断或文件长度不完整，断点已保留", 120)
            if not valid_signature(part, extension):
                raise ArchiveError("文件校验失败")
            # Hash before rename; a receipt permits recovery after a crash between rename and DB commit.
            result = {"url": row["url"], "revision": row["revision"], "size": part.stat().st_size,
                      "sha256": file_hash(part), "etag": etag, "last_modified": modified}
            atomic_json(receipt, result)
            os.replace(part, target)
            meta.unlink(missing_ok=True)
            if extension in (".html", ".htm", ".shtml"):
                result["linked_documents"] = html_attachments(row, target)
            return result
        finally:
            response.close()

    def reuse(self, row, cached):
        source = self.target(cached["relative_path"])
        target = self.target(row["relative_path"])
        if (not source.is_file() or source.stat().st_size != cached["size"] or file_hash(source) != cached["sha256"]
                or not valid_signature(source, source.suffix.lower())):
            return None
        if target.exists():
            return None  # The regular recovery path preserves/repairs an existing local file.
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(str(target) + ".copying")
        shutil.copyfile(source, temporary)
        result = {"url": row["url"], "revision": row["revision"], "size": cached["size"], "sha256": cached["sha256"],
                  "etag": cached["etag"], "last_modified": cached["last_modified"]}
        atomic_json(Path(str(target) + ".receipt.json"), result)
        os.replace(temporary, target)
        if target.suffix.lower() in (".html", ".htm", ".shtml"):
            result["linked_documents"] = html_attachments(row, target)
        return result
