from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from mlc_agent.exceptions import DataSourceError


class PdfTable(BaseModel):
    rows: list[list[str | None]]


class PdfPage(BaseModel):
    page_number: int = Field(ge=1)
    text: str
    tables: list[PdfTable] = Field(default_factory=list)


class ParsedReport(BaseModel):
    path: str
    pages: list[PdfPage]


def parse_machine_generated_pdf(path: Path) -> ParsedReport:
    """Parse a machine-generated PDF after the approved parser is installed.

    The dependency is intentionally imported at call time so the rest of the
    application remains usable before project-leader approval. No alternate PDF
    package and no OCR fallback are used.
    """
    if not path.is_file():
        raise DataSourceError(f"PDF file does not exist: {path}")
    try:
        import pdfplumber
    except ImportError as exc:
        raise DataSourceError(
            "PDF parsing is unavailable until pdfplumber==0.11.10 is approved and installed"
        ) from exc

    pages: list[PdfPage] = []
    try:
        with pdfplumber.open(path) as report:
            for page_number, page in enumerate(report.pages, start=1):
                text = page.extract_text() or ""
                tables = [PdfTable(rows=rows) for rows in page.extract_tables()]
                pages.append(PdfPage(page_number=page_number, text=text, tables=tables))
    except Exception as exc:
        raise DataSourceError(f"Failed to parse machine-generated PDF: {path}") from exc
    if not any(page.text.strip() or page.tables for page in pages):
        raise DataSourceError("PDF contains no extractable text or tables; OCR fallback is out of scope")
    return ParsedReport(path=str(path.resolve()), pages=pages)
