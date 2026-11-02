"""Serial, cookie-aware curl transport. No certificate bypass or WAF circumvention."""
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit, urlencode, urljoin
import json
import os
import random
import re
import shutil
import subprocess
import tempfile
import time

from .models import ArchiveError, SchemaError, official_url, REFERERS


@dataclass
class Response:
    status: int
    headers: dict
    path: Path
    url: str
    error: str = ""

    def close(self):
        self.path.unlink(missing_ok=True)


def parse_headers(raw):
    status, headers = 0, {}
    for line in raw.replace("\r\n", "\n").split("\n"):
        if re.match(r"^HTTP/\S+ \d{3}", line):
            status = int(line.split()[1])
            headers = {}
        elif ":" in line:
            key, value = line.split(":", 1)
            headers[key.lower().strip()] = value.strip()
    return status, headers


def retry_after(value, now=None):
    if not value:
        return 0
    try:
        return max(0, float(value))
    except ValueError:
        try:
            return max(0, parsedate_to_datetime(value).timestamp() - (time.time() if now is None else now))
        except (ValueError, TypeError, OverflowError):
            return 0


def parse_json(data):
    try:
        text = data.decode("utf-8-sig").strip()
        if not text.startswith(("{", "[")):
            match = re.fullmatch(r"[\w.$]+\s*\((.*)\)\s*;?", text, re.S)
            if not match:
                raise ValueError("不是 JSON/JSONP")
            text = match[1]
        return json.loads(text)
    except (ValueError, UnicodeError) as exc:
        raise SchemaError("接口未返回有效 JSON，可能正在维护或访问受限；本次不会记为成功") from exc


class Transport:
    def __init__(self, state_dir, config, logger, sleep=time.sleep):
        if not shutil.which("curl"):
            raise ArchiveError("需要系统 curl（macOS 自带；Linux 可通过系统软件包安装）")
        self.config, self.logger, self.sleep = config, logger, sleep
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.cookie_file = self.state_dir / "cookies.txt"
        self.last_request = 0.0
        self.blocked = {}

    def _pace(self):
        delay = self.config["request_interval_seconds"] + random.uniform(0, self.config["jitter_seconds"])
        self.sleep(max(0, self.last_request + delay - time.monotonic()))
        self.last_request = time.monotonic()

    def _once(self, url, exchange, method, form, headers, max_bytes):
        self._pace()
        body_fd, body_path = tempfile.mkstemp(prefix="response-", dir=self.state_dir)
        header_fd, header_path = tempfile.mkstemp(prefix="headers-", dir=self.state_dir)
        os.close(body_fd)
        os.close(header_fd)
        cmd = ["curl", "--silent", "--show-error", "--globoff", "--proto", "=https",
               "--connect-timeout", str(self.config["connect_timeout_seconds"]),
               "--max-time", str(self.config["request_timeout_seconds"]),
               "--max-filesize", str(max_bytes), "--dump-header", header_path,
               "--output", body_path, "--cookie", str(self.cookie_file), "--cookie-jar", str(self.cookie_file),
               "--user-agent", "Mozilla/5.0 IPOArchive/1.0 (personal research; serial requests)",
               "--referer", REFERERS[exchange], "--header", "Accept-Encoding: identity"]
        for key, value in (headers or {}).items():
            cmd += ["--header", f"{key}: {value}"]
        if method == "POST":
            cmd += ["--data", urlencode(form or {})]
        cmd += ["--url", url]
        try:
            try:
                result = subprocess.run(cmd, capture_output=True, timeout=self.config["request_timeout_seconds"] + 10)
                process_error = result.stderr.decode(errors="replace").strip() if result.returncode else ""
            except subprocess.TimeoutExpired:
                process_error = "curl 进程超时，稍后重试"
            status, response_headers = parse_headers(Path(header_path).read_text(errors="replace"))
            if self.cookie_file.exists():
                self.cookie_file.chmod(0o600)
            if Path(body_path).stat().st_size > max_bytes:
                raise ArchiveError("响应超过配置的大小上限")
            return Response(status, response_headers, Path(body_path), url, process_error)
        except BaseException:
            Path(body_path).unlink(missing_ok=True)
            raise
        finally:
            Path(header_path).unlink(missing_ok=True)

    def request(self, url, exchange, method="GET", form=None, headers=None, max_bytes=20 * 1024 * 1024,
                allow_partial=False):
        if self.blocked.get(exchange, 0) > time.time():
            raise ArchiveError("交易所冷却中", self.blocked[exchange] - time.time())
        url = official_url(url)
        redirects, failures = 0, 0
        while True:
            response = self._once(url, exchange, method, form, headers, max_bytes)
            status = response.status
            if status in (301, 302, 303, 307, 308):
                location = response.headers.get("location")
                response.close()
                redirects += 1
                if not location or redirects > 4:
                    self.blocked[exchange] = time.time() + 900
                    raise ArchiveError("官方站点连续跳转，已暂停该交易所，稍后自动重试", 900)
                # Cookies are written even on redirects. Repeating a same-URL redirect is
                # allowed only within this small bound; no script/challenge is solved.
                next_url = official_url(urljoin(url, location))
                if urlsplit(next_url).hostname != urlsplit(url).hostname:
                    raise ArchiveError("跨域跳转已停止，需核查官方接口")
                if status == 303 or (status in (301, 302) and method == "POST"):
                    method, form = "GET", None
                url = next_url
                continue
            if status in (403, 429):
                cooldown = max(900 if status == 403 else 120, retry_after(response.headers.get("retry-after")))
                response.close()
                self.blocked[exchange] = time.time() + cooldown
                raise ArchiveError(f"HTTP {status}：已暂停该交易所，保留进度等待重试", cooldown)
            if status in (200, 206, 304, 416) and (not response.error or (allow_partial and status in (200, 206))):
                return response
            message = response.error or f"HTTP {status}"
            cooldown = retry_after(response.headers.get("retry-after"))
            response.close()
            transient = status == 0 or status in (408, 425) or status >= 500 or bool(response.error)
            failures += 1
            if not transient or failures >= self.config["max_attempts"]:
                cooldown = max(cooldown, 300 if transient else 3600)
                if transient:
                    self.blocked[exchange] = time.time() + cooldown
                raise ArchiveError("请求失败：" + message[:300], cooldown)
            wait = max(cooldown, 2 ** failures + random.random())
            if wait > 30:
                raise ArchiveError("服务要求稍后重试，已保留进度", wait)
            self.logger.warning("暂时连接失败，%.1f 秒后重试（%d/%d）", wait, failures, self.config["max_attempts"])
            self.sleep(wait)

    def json(self, url, exchange, form=None):
        # The official BSE JSONP endpoint also accepts GET, as used by JSONP in
        # browsers. Put each field in the query exactly once: duplicate form/query
        # scalars make its list controller return HTTP 500.
        if form is not None:
            url += ("&" if "?" in url else "?") + urlencode(form)
        response = self.request(url, exchange)
        try:
            if response.status != 200:
                raise SchemaError(f"元数据接口返回意外状态 {response.status}")
            return parse_json(response.path.read_bytes())
        finally:
            response.close()
