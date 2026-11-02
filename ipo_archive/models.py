from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, quote, unquote
import json
import re
import unicodedata

EXCHANGES = {"sse": "上海证券交易所", "szse": "深圳证券交易所", "bse": "北京证券交易所"}
CATEGORIES = ("信息披露", "问询与回复", "上市委会议公告与结果公告", "终止审核", "证监会注册结果", "其他文件")
REFERERS = {
    "sse": "https://www.sse.com.cn/listing/renewal/ipo/",
    "szse": "https://www.szse.cn/listing/projectdynamic/ipo/index.html",
    "bse": "https://www.bse.cn/audit/project_news.html",
}
HOSTS = {"www.sse.com.cn", "query.sse.com.cn", "static.sse.com.cn", "www.szse.cn",
         "reportdocs.static.szse.cn", "reportdocs.szse.cn", "docs.static.szse.cn",
         "www.bse.cn", "static.bse.cn", "www.csrc.gov.cn"}
EXTENSIONS = {".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx", ".zip", ".rar", ".7z", ".txt", ".html", ".htm", ".shtml"}


class ArchiveError(Exception):
    def __init__(self, message, cooldown=0):
        super().__init__(message)
        self.cooldown = cooldown


class SchemaError(ArchiveError):
    pass


def digest(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return sha256(value.encode()).hexdigest()


def official_url(value, base=""):
    url = urlsplit(urljoin(base, str(value).strip()))
    if url.scheme not in ("https", "http") or url.hostname not in HOSTS or url.username or url.password or url.port:
        raise SchemaError("不受支持的官方地址：" + str(value)[:180])
    # All supported official sites provide HTTPS. Never follow an insecure redirect.
    return urlunsplit(("https", url.netloc, quote(unquote(url.path), safe="/!$&'()*+,:;=@-._~"), url.query, ""))


def component(value, max_bytes=150):
    value = unicodedata.normalize("NFC", str(value))
    value = re.sub(r'[\x00-\x1f\x7f/\\:*?"<>|]', "_", value).strip(" .")
    value = re.sub(r"\s+", " ", value)
    if value in ("", ".", ".."):
        value = "未命名"
    raw = value.encode("utf-8")
    return raw[:max_bytes].decode("utf-8", errors="ignore").rstrip(" .") or "未命名"


def date_text(value):
    if isinstance(value, dict):
        if all(k in value for k in ("year", "month", "date")):
            return f"{int(value['year']) + 1900:04}-{int(value['month']) + 1:02}-{int(value['date']):02}"
        if isinstance(value.get("time"), (int, float)):
            return datetime.fromtimestamp(value["time"] / 1000, timezone(timedelta(hours=8))).date().isoformat()
    match = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", str(value or ""))
    return "-".join(match.groups()) if match else ""


@dataclass
class Project:
    exchange: str
    id: str
    name: str
    source_url: str
    fingerprint: str
    updated: str = ""
    status: str = ""

    @property
    def key(self):
        return f"{self.exchange}:{self.id}"


@dataclass
class Document:
    title: str
    url: str
    category: str
    published: str = ""
    revision: str = ""
    official_id: str = ""
    metadata: dict = field(default_factory=dict)

    @property
    def key(self):
        # Timestamp/version changes at the same URL create a new archived version.
        return digest([self.url, self.revision])

    def filename(self, project_id):
        extension = Path(urlsplit(self.url).path).suffix.lower()
        if extension not in EXTENSIONS:
            raise SchemaError("无法识别的文件扩展名：" + self.url)
        title = re.sub(r"\.(?:pdf|docx?|xlsx?|pptx?|zip|rar|7z|txt|s?html?)$", "", self.title, flags=re.I)
        return f"{self.published or '日期未知'}_{component(title, 135)}__{project_id}_{self.key[:16]}{extension}"
