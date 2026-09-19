from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field

from mlc_agent.exceptions import DataSourceError


CNINFO_QUERY_URL = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_STATIC_BASE_URL = "https://static.cninfo.com.cn/"
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")


class AnnouncementDocument(BaseModel):
    announcement_id: str
    stock_code: str
    title: str
    published_at: datetime
    url: str
    document_type: Literal["annual_report", "interim_report", "announcement"]
    report_year: int | None = None
    source: Literal["cninfo", "exchange"] = "cninfo"


class ReportCatalog(BaseModel):
    annual_reports: list[AnnouncementDocument] = Field(default_factory=list)
    interim_reports: list[AnnouncementDocument] = Field(default_factory=list)
    announcements: list[AnnouncementDocument] = Field(default_factory=list)


class SelectedReports(BaseModel):
    latest_full_annual_report: AnnouncementDocument | None
    latest_interim_report: AnnouncementDocument | None
    window_announcements: list[AnnouncementDocument]


def _classify_title(title: str) -> tuple[str, int | None]:
    compact = title.replace(" ", "")
    year = None
    for token in compact.split("年", 1)[:1]:
        if len(token) >= 4 and token[-4:].isdigit():
            year = int(token[-4:])
    if ("半年度报告" in compact or "中期报告" in compact) and "摘要" not in compact:
        return "interim_report", year
    if "年度报告" in compact and "摘要" not in compact:
        return "annual_report", year
    return "announcement", year


def _row_matches_stock(row: dict[str, Any], stock_code: str) -> bool:
    value = row.get("secCode")
    if value in (None, ""):
        return True
    if isinstance(value, list):
        return stock_code in {str(item).strip() for item in value}
    return str(value).strip() == stock_code


def fetch_company_announcements(
    client: httpx.Client,
    *,
    stock_code: str,
    org_id: str,
    start_date: date,
    end_date: date,
    page_size: int = 30,
    column: Literal["szse", "sse", "bjse"] = "szse",
) -> list[AnnouncementDocument]:
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    documents: list[AnnouncementDocument] = []
    page = 1
    while True:
        response = client.post(
            CNINFO_QUERY_URL,
            data={
                "pageNum": str(page),
                "pageSize": str(page_size),
                "column": column,
                "tabName": "fulltext",
                "plate": "",
                "stock": f"{stock_code},{org_id}",
                "searchkey": "",
                "secid": "",
                "category": "",
                "trade": "",
                "seDate": f"{start_date.isoformat()}~{end_date.isoformat()}",
                "sortName": "time",
                "sortType": "desc",
                "isHLtitle": "true",
            },
        )
        response.raise_for_status()
        payload: dict[str, Any] = response.json()
        rows = payload.get("announcements") or []
        if not isinstance(rows, list):
            raise DataSourceError("CNINFO response announcements is not a list")
        for row in rows:
            if not isinstance(row, dict) or not _row_matches_stock(row, stock_code):
                continue
            adjunct = str(row.get("adjunctUrl") or "").lstrip("/")
            if not adjunct:
                continue
            title = str(row.get("announcementTitle") or "").replace("<em>", "").replace("</em>", "")
            document_type, report_year = _classify_title(title)
            timestamp = row.get("announcementTime")
            if not isinstance(timestamp, (int, float)):
                continue
            documents.append(
                AnnouncementDocument(
                    announcement_id=str(row.get("announcementId") or adjunct),
                    stock_code=str(row.get("secCode") or stock_code),
                    title=title,
                    published_at=datetime.fromtimestamp(
                        timestamp / 1000, tz=CHINA_TIMEZONE
                    ).replace(tzinfo=None),
                    url=CNINFO_STATIC_BASE_URL + adjunct,
                    document_type=document_type,
                    report_year=report_year,
                )
            )
        if not payload.get("hasMore"):
            break
        page += 1
    return documents


def build_report_catalog(documents: list[AnnouncementDocument]) -> ReportCatalog:
    return ReportCatalog(
        annual_reports=sorted(
            (item for item in documents if item.document_type == "annual_report"),
            key=lambda item: item.published_at,
            reverse=True,
        ),
        interim_reports=sorted(
            (item for item in documents if item.document_type == "interim_report"),
            key=lambda item: item.published_at,
            reverse=True,
        ),
        announcements=sorted(documents, key=lambda item: item.published_at, reverse=True),
    )


def select_reports(
    catalog: ReportCatalog,
    *,
    as_of: date,
    announcement_start: date,
) -> SelectedReports:
    if announcement_start > as_of:
        raise ValueError("announcement_start must not be after as_of")
    eligible_annual = [
        item
        for item in catalog.annual_reports
        if item.published_at.date() <= as_of and item.report_year is not None and item.report_year < as_of.year
    ]
    eligible_annual.sort(
        key=lambda item: (item.report_year or 0, item.published_at), reverse=True
    )
    eligible_interim = [
        item for item in catalog.interim_reports if item.published_at.date() <= as_of
    ]
    eligible_interim.sort(
        key=lambda item: (item.report_year or 0, item.published_at), reverse=True
    )
    window = [
        item
        for item in catalog.announcements
        if announcement_start <= item.published_at.date() <= as_of
    ]
    return SelectedReports(
        latest_full_annual_report=eligible_annual[0] if eligible_annual else None,
        latest_interim_report=eligible_interim[0] if eligible_interim else None,
        window_announcements=window,
    )


def download_announcement(client: httpx.Client, document: AnnouncementDocument) -> bytes:
    response = client.get(document.url)
    response.raise_for_status()
    content = response.content
    if not content:
        raise DataSourceError(f"{document.source.upper()} document is empty: {document.url}")
    return content
