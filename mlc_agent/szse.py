from __future__ import annotations

from datetime import date, datetime
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import httpx

from mlc_agent.cninfo import AnnouncementDocument, _classify_title
from mlc_agent.exceptions import DataSourceError


SZSE_ANNOUNCEMENT_API = "https://www.szse.cn/api/disc/announcement/annList"
SZSE_ANNOUNCEMENT_PAGE_URL = "https://www.szse.cn/disclosure/listed/notice/index.html"
SZSE_STATIC_BASE_URL = "https://disc.static.szse.cn/"
CHINA_TIMEZONE = ZoneInfo("Asia/Shanghai")
SZSE_ANNOUNCEMENT_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.szse.cn",
    "Referer": SZSE_ANNOUNCEMENT_PAGE_URL,
}


def _published_at(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return _as_china_naive(value)
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=CHINA_TIMEZONE).replace(tzinfo=None)
    text = str(value or "").strip()
    if not text:
        return None
    if text.isdigit():
        return datetime.fromtimestamp(int(text) / 1000, tz=CHINA_TIMEZONE).replace(tzinfo=None)
    for candidate in (text, text.replace("/", "-")):
        try:
            return _as_china_naive(datetime.fromisoformat(candidate.replace("Z", "+00:00")))
        except ValueError:
            continue
    return None


def _as_china_naive(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value
    return value.astimezone(CHINA_TIMEZONE).replace(tzinfo=None)


def _row_matches_stock(row: dict[str, Any], stock_code: str) -> bool:
    codes = row.get("secCode")
    if isinstance(codes, list):
        return stock_code in {str(item).strip() for item in codes}
    if codes is None:
        return False
    return str(codes).strip() == stock_code


def _document_from_row(row: dict[str, Any], *, stock_code: str) -> AnnouncementDocument | None:
    attach_path = str(row.get("attachPath") or "").strip()
    title = str(row.get("title") or "").strip()
    published_at = _published_at(row.get("publishTime"))
    if not attach_path or not title or published_at is None:
        return None
    document_type, report_year = _classify_title(title)
    return AnnouncementDocument(
        announcement_id=str(row.get("id") or attach_path),
        stock_code=stock_code,
        title=title,
        published_at=published_at,
        url=urljoin(SZSE_STATIC_BASE_URL, f"download/{attach_path.lstrip('/')}"),
        document_type=document_type,
        report_year=report_year,
        source="exchange",
    )


def fetch_szse_announcements(
    client: httpx.Client,
    *,
    stock_code: str,
    start_date: date,
    end_date: date,
    page_size: int = 30,
) -> list[AnnouncementDocument]:
    """Fetch one Shenzhen-listed company from SZSE's public disclosure API."""
    if start_date > end_date:
        raise ValueError("start_date must not be after end_date")
    if page_size <= 0:
        raise ValueError("page_size must be positive")

    documents: list[AnnouncementDocument] = []
    page = 1
    while True:
        response = client.post(
            SZSE_ANNOUNCEMENT_API,
            json={
                "stock": [stock_code],
                "seDate": [start_date.isoformat(), end_date.isoformat()],
                "channelCode": ["listedNotice_disc"],
                "pageSize": page_size,
                "pageNum": page,
            },
            headers=SZSE_ANNOUNCEMENT_HEADERS,
        )
        response.raise_for_status()
        payload = response.json()
        rows = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise DataSourceError("SZSE announcement response data is not a list")
        for row in rows:
            if not isinstance(row, dict) or not _row_matches_stock(row, stock_code):
                continue
            document = _document_from_row(row, stock_code=stock_code)
            if document is not None:
                documents.append(document)
        if len(rows) < page_size:
            break
        page += 1
    return documents
