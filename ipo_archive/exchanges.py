"""Official public IPO project APIs; schemas checked against site JS, 2026-11-02."""
from collections import Counter
from dataclasses import asdict
from math import ceil
from urllib.parse import urlencode
import re

from .models import Project, Document, SchemaError, REFERERS, CATEGORIES, official_url, date_text, digest

SSE_QUERY = "https://query.sse.com.cn/commonSoaQuery.do?"
SZSE_GROUPS = dict(zip(("disclosureMaterials", "enquiryResponseAttachment", "meetingConclusionAttachment",
                       "terminationNoticeAttachment", "registrationResultAttachment"), CATEGORIES[:5]))
BSE_GROUPS = dict(zip(("xxgkInfo", "wxhfhInfo", "hyggjgInfo", "zzscInfo", "hztzInfo"), CATEGORIES[:5]))


def project_url(exchange, project_id):
    if not re.fullmatch(r"\d+", str(project_id)):
        raise SchemaError("项目编号必须为数字")
    if exchange == "sse":
        return REFERERS[exchange] + "index_listing_detail.shtml?auditId=" + str(project_id)
    if exchange == "szse":
        return "https://www.szse.cn/listing/projectdynamic/ipo/detail/index.html?id=" + str(project_id)
    return "https://www.bse.cn/audit/project_news_detail.html?id=" + str(project_id)


def project_from_row(exchange, row):
    if exchange == "sse":
        ident = row.get("stockAuditNum")
        issuer = (row.get("stockIssuer") or [{}])[0]
        name = issuer.get("s_issueCompanyFullName") or row.get("stockAuditName")
        changed = [row.get(k) for k in ("updateDate", "currStatus", "commitiResult", "registeResult", "suspendStatus")]
        updated, status = row.get("updateDate"), str(row.get("currStatus", ""))
    elif exchange == "szse":
        ident, name = row.get("prjid"), row.get("cmpnm")
        changed = [row.get(k) for k in ("updtdt", "prjstatus", "prjst", "stage")]
        updated, status = row.get("updtdt"), row.get("prjst", "")
    else:
        ident, name = row.get("id"), row.get("companyName")
        changed = [row.get(k) for k in ("updateDate", "status", "operatingTime")]
        updated, status = row.get("updateDate"), row.get("status", "")
    if not isinstance(name, str) or not name.strip() or not re.fullmatch(r"\d+", str(ident)):
        raise SchemaError("项目接口缺少有效编号或公司全称")
    return Project(exchange, str(ident), name.strip(), project_url(exchange, ident), digest([name, changed]), date_text(updated), str(status))


def parse_page(exchange, payload, expected_page):
    try:
        if exchange == "sse":
            rows, page = payload["result"], payload["pageHelp"]
            total, size, current = page["total"], page["pageSize"], int(page["pageNo"]) - 1
        elif exchange == "szse":
            rows, total, size, current = payload["data"], payload["totalSize"], payload["pageSize"], payload["pageIndex"]
        else:
            page = payload[0]["listInfo"]
            rows, total, size, current = page["content"], page["totalElements"], page["size"], page["number"]
        if not isinstance(rows, list) or isinstance(total, bool) or int(total) < 0 or int(size) <= 0 or int(current) != expected_page:
            raise ValueError("分页字段异常")
        pages = ceil(int(total) / int(size))
        if (expected_page < pages and not rows) or len(rows) > int(size):
            raise ValueError("分页内容缺失")
        return [project_from_row(exchange, row) for row in rows], int(total), max(1, pages)
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise SchemaError(f"{exchange} 列表结构或页码发生变化，本次发现未完成") from exc


def walk_files(value, path=()):
    if isinstance(value, list):
        for item in value:
            yield from walk_files(item, path)
    elif isinstance(value, dict):
        if value.get("destFilePath") or value.get("dfpth") or value.get("filePath") or value.get("url"):
            yield path, value
        for key, child in value.items():
            if isinstance(child, (dict, list)):
                yield from walk_files(child, path + (key,))


def sse_category(row):
    kind = row.get("fileTypeMap", "")
    if re.fullmatch(r"I00[1-6][1-3]", kind):
        return CATEGORIES[0]
    if kind in ("I3010", "I3020"):
        return CATEGORIES[1]
    if kind in ("I2010", "I2011", "I2020", "I2021"):
        return CATEGORIES[2]
    if kind == "I1020":
        return CATEGORIES[3]
    if kind == "I1010":
        return CATEGORIES[4]
    return CATEGORIES[5]


def parse_documents(project, payload):
    documents = []
    try:
        if project.exchange == "sse":
            rows = payload["result"]
            if not isinstance(rows, list):
                raise ValueError("result 缺失")
            total = (payload.get("pageHelp") or {}).get("total")
            if total is not None and int(total) > len(rows):
                raise ValueError("非分页文件接口返回不完整")
            for row in rows:
                if str(row.get("auditId")) != project.id:
                    raise ValueError("文件项目编号不一致")
                if not row.get("filePath") or not row.get("fileTitle"):
                    raise ValueError("文件缺少标题或地址")
                url = official_url(row["filePath"].lstrip("/"), "https://static.sse.com.cn/stock/")
                documents.append(Document(row["fileTitle"], url, sse_category(row), date_text(row.get("fileUpdTime")),
                                          digest([row.get(k) for k in ("fileId", "fileUpdTime", "fileVersion", "fileSize")]),
                                          str(row.get("fileId", "")), {k: row.get(k) for k in ("fileTypeMap", "fileType", "fileVersion", "fileSize")}))
        else:
            if project.exchange == "szse":
                data, ident, name_key, groups = payload["data"], "prjid", "cmpnm", SZSE_GROUPS
                base, path_key = "https://reportdocs.static.szse.cn/", "dfpth"
            else:
                data, ident, name_key, groups = payload[0], "id", "companyName", BSE_GROUPS
                base, path_key = "https://www.bse.cn/", "destFilePath"
            record = data if project.exchange == "szse" else data["projectNews"]
            if str(record.get(ident)) != project.id or not record.get(name_key):
                raise ValueError("详情项目身份不一致")
            project.name = record[name_key].strip()
            for group in groups:
                if group not in data or not isinstance(data[group], (list, dict)):
                    raise ValueError("文件分组缺失：" + group)
            # Unknown future attachment groups are retained in 其他文件, never silently dropped.
            for group, value in data.items():
                if group in ("projectNews", "projectStatus", "prjprogs", "zmInfo", "sysDate"):
                    continue
                for nesting, row in walk_files(value):
                    path = row.get(path_key) or row.get("url")
                    if not path:
                        raise ValueError("附件缺少官方地址")
                    if project.exchange == "szse":
                        title = row.get("dfnm") or row.get("displayName") or row.get("configFileName")
                        published = date_text(row.get("ddtime") or row.get("ddt"))
                        revision = digest([row.get(k) for k in ("dfid", "ddtime", "ddt")])
                        official_id = str(row.get("dfid", ""))
                    else:
                        title = (row.get("disclosureTitle") or "") + (row.get("disclosurePostTitle") or "")
                        published = date_text(row.get("publishDate") or row.get("pubDate"))
                        revision = digest([row.get(k) for k in ("disclosureCode", "modifyTimes", "upDate", "publishDate")])
                        official_id = str(row.get("disclosureCode", ""))
                    if not title:
                        raise ValueError("附件缺少名称")
                    documents.append(Document(title, official_url(path, base), groups.get(group, CATEGORIES[5]),
                                              published, revision, official_id, {"group": group, "nested_group": list(nesting)}))
        # Same document can appear in several nested site groups.
        result = {}
        for doc in documents:
            doc.filename(project.id)  # validate local naming before committing any metadata
            if doc.key not in result or result[doc.key].category == CATEGORIES[5]:
                result[doc.key] = doc
        return list(result.values())
    except (KeyError, IndexError, TypeError, ValueError, AttributeError) as exc:
        raise SchemaError(f"{project.exchange}:{project.id} 详情结构异常：{exc}") from exc


class ExchangeAPI:
    def __init__(self, transport, page_size=50):
        self.http, self.page_size = transport, page_size

    def page(self, exchange, page):
        if exchange == "sse":
            payload = self.http.json(SSE_QUERY + urlencode({"sqlId": "SH_XM_LB", "issueMarketType": "1,2",
                "order": "updateDate|desc,stockAuditNum|desc", "isPagination": "true",
                "pageHelp.pageSize": self.page_size, "pageHelp.pageNo": page + 1}), exchange)
        elif exchange == "szse":
            payload = self.http.json("https://www.szse.cn/api/ras/projectrends/query?" + urlencode({
                "pageIndex": page, "pageSize": self.page_size, "bizType": 1, "keywords": ""}), exchange)
        else:
            payload = self.http.json("https://www.bse.cn/projectNewsController/infoResult.do?callback=ipoArchive",
                exchange, {"statetypes": "", "page": page, "companyCode": "", "isNewThree": "1",
                           "sortfield": "updateDate", "sorttype": "desc", "startTime": "", "endTime": "", "keyword": ""})
        return parse_page(exchange, payload, page)

    def documents(self, project):
        if project.exchange == "sse":
            payload = self.http.json(SSE_QUERY + urlencode({"sqlId": "GP_COMMON_FILE_SEARCH", "auditId": project.id,
                "marketType": "1,2", "isPagination": "false"}), "sse")
        elif project.exchange == "szse":
            payload = self.http.json("https://www.szse.cn/api/ras/projectrends/details?id=" + project.id, "szse")
        else:
            payload = self.http.json("https://www.bse.cn/projectNewsController/infoDetailResult.do?callback=ipoArchive", "bse", {"id": project.id})
        return parse_documents(project, payload)

    def by_url(self, url):
        from urllib.parse import parse_qs, urlsplit
        parsed = urlsplit(official_url(url))
        exchange = "sse" if parsed.hostname == "www.sse.com.cn" else "szse" if parsed.hostname == "www.szse.cn" else "bse" if parsed.hostname == "www.bse.cn" else ""
        params = parse_qs(parsed.query)
        ident = params.get("auditId" if exchange == "sse" else "id", [""])[0]
        if not exchange or project_url(exchange, ident) != official_url(url):
            raise SchemaError("请使用交易所 IPO 项目详情页地址")
        if exchange == "sse":
            payload = self.http.json(SSE_QUERY + urlencode({"sqlId": "SH_XM_LB", "stockAuditNum": ident,
                "issueMarketType": "1,2", "isPagination": "true", "pageHelp.pageSize": 5, "pageHelp.pageNo": 1}), exchange)
            candidates, _, _ = parse_page(exchange, payload, 0)
            rows = [p for p in candidates if p.id == ident]
            if len(rows) != 1:
                raise SchemaError("无法从官方项目列表确认该项目")
            project = rows[0]
        else:
            # Detail API provides identity; the placeholder is never persisted if it fails.
            project = Project(exchange, ident, "待核实", url, "")
        docs = self.documents(project)
        if not project.fingerprint:
            project.fingerprint = digest([project.name, [(d.key, d.title) for d in docs]])
        return project, docs
