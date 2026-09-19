from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Literal
from urllib.parse import parse_qs, quote, urlencode, urlparse

import httpx
from pydantic import BaseModel, Field

from mlc_agent.schemas import CompanyIdentity, SourceValue, WorkupAgentState


GoogleExchange = Literal["SHE", "SHA"]
CNINFO_COMPANY_BASE_URL = "https://www.cninfo.com.cn/new/disclosure/stock"
CNINFO_COMPANY_SEARCH_URL = "https://www.cninfo.com.cn/new/information/topSearch/query"
CNINFO_SZ_ORG_ID_PREFIX = "gssz"
GOOGLE_FINANCE_BASE_URL = "https://www.google.com/finance/quote"


class ExternalLinksResult(BaseModel):
    source_values: list[SourceValue]
    errors: list[str] = Field(default_factory=list)


def google_finance_exchange(company: CompanyIdentity) -> GoogleExchange | None:
    suffix = company.eastmoney_secu_code.rsplit(".", 1)[-1].upper()
    exchange_by_suffix: dict[str, GoogleExchange] = {"SZ": "SHE", "SH": "SHA"}
    if suffix == "BJ":
        return None
    if suffix not in exchange_by_suffix:
        raise ValueError(f"unsupported A-share exchange suffix: {suffix}")
    return exchange_by_suffix[suffix]


def build_google_finance_url(company: CompanyIdentity) -> str:
    exchange = google_finance_exchange(company)
    if exchange is None:
        raise ValueError("Google Finance has no configured verifiable Beijing Stock Exchange page")
    symbol = quote(f"{company.stock_code}:{exchange}", safe=":")
    return f"{GOOGLE_FINANCE_BASE_URL}/{symbol}?hl=en"


def validate_google_finance_url(
    client: httpx.Client,
    company: CompanyIdentity,
) -> str:
    url = build_google_finance_url(company)
    exchange = google_finance_exchange(company)
    response = client.get(url)
    response.raise_for_status()
    final_url = str(response.url)
    decoded_path = urlparse(final_url).path
    expected_symbol = f"{company.stock_code}:{exchange}"
    body = response.text
    if expected_symbol not in decoded_path or expected_symbol not in body:
        raise ValueError(
            f"Google Finance response does not match company symbol {expected_symbol}"
        )
    return final_url


def build_cninfo_company_url(*, stock_code: str, org_id: str) -> str:
    normalized_org_id = org_id.strip()
    if not normalized_org_id:
        raise ValueError("CNINFO org_id is required")
    return f"{CNINFO_COMPANY_BASE_URL}?{urlencode({'orgId': normalized_org_id, 'stockCode': stock_code})}"


def derive_cninfo_sz_org_id(company: CompanyIdentity) -> str:
    """Derive the published CNINFO orgId form for a Shenzhen listing.

    CNINFO's public Shenzhen company pages use ``gssz`` followed by the
    seven-digit, zero-padded security code.  This is only a Shenzhen rule;
    other markets must continue using an orgId obtained from CNINFO itself.
    """
    suffix = company.eastmoney_secu_code.rsplit(".", 1)[-1].upper()
    if suffix != "SZ":
        raise ValueError("CNINFO Shenzhen orgId derivation requires an SZ listing")
    if not re.fullmatch(r"\d{6}", company.stock_code):
        raise ValueError(f"invalid six-digit Shenzhen stock code: {company.stock_code}")
    return f"{CNINFO_SZ_ORG_ID_PREFIX}0{company.stock_code}"


def validate_constructed_cninfo_company_url(company: CompanyIdentity) -> str:
    """Validate a deterministic Shenzhen CNINFO URL without topSearch.

    The validation is intentionally local: the URL's host, path, stockCode,
    and derived orgId must all match the company identity.  CNINFO may reject
    automated HTTP requests with 403 even when the canonical company page is
    valid, so a remote request would make this link field depend on an
    unrelated anti-bot response.
    """
    org_id = derive_cninfo_sz_org_id(company)
    url = build_cninfo_company_url(stock_code=company.stock_code, org_id=org_id)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    if (
        parsed.scheme != "https"
        or parsed.netloc.lower() != "www.cninfo.com.cn"
        or parsed.path != "/new/disclosure/stock"
        or query.get("stockCode") != [company.stock_code]
        or query.get("orgId") != [org_id]
    ):
        raise ValueError(f"constructed CNINFO URL does not match {company.stock_code}")
    return url


def discover_cninfo_org_id(
    client: httpx.Client,
    company: CompanyIdentity,
) -> str:
    response = client.post(
        CNINFO_COMPANY_SEARCH_URL,
        data={"keyWord": company.stock_code, "maxNum": "10"},
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise ValueError("CNINFO company search response is not a list")
    matches = [
        item
        for item in payload
        if isinstance(item, dict)
        and str(item.get("code") or "") == company.stock_code
        and str(item.get("orgId") or "").strip()
    ]
    if len(matches) != 1:
        raise ValueError(
            f"CNINFO company search did not return one exact match for {company.stock_code}"
        )
    return str(matches[0]["orgId"]).strip()


def validate_cninfo_company_url(
    client: httpx.Client,
    company: CompanyIdentity,
    *,
    org_id: str,
) -> str:
    url = build_cninfo_company_url(stock_code=company.stock_code, org_id=org_id)
    response = client.get(url)
    response.raise_for_status()
    final_url = str(response.url)
    parsed = urlparse(final_url)
    query = parse_qs(parsed.query)
    query_codes = query.get("stockCode", [])
    body = response.text
    code_matches = query_codes == [company.stock_code] and company.stock_code in body
    org_matches = query.get("orgId", []) == [org_id.strip()]
    if parsed.netloc.lower() != "www.cninfo.com.cn" or parsed.path != "/new/disclosure/stock":
        raise ValueError("CNINFO response is not a company disclosure page")
    if not code_matches or not org_matches:
        raise ValueError(
            f"CNINFO response does not match stock code {company.stock_code} and org_id {org_id.strip()}"
        )
    return final_url


def collect_external_links(
    client: httpx.Client,
    *,
    company: CompanyIdentity,
    cninfo_org_id: str | None,
    captured_at: datetime,
) -> ExternalLinksResult:
    values: list[SourceValue] = []
    errors: list[str] = []
    try:
        google_url = validate_google_finance_url(client, company)
        values.append(
            SourceValue(
                field_id="google_finance_url",
                value=google_url,
                raw_value={
                    "stock_code": company.stock_code,
                    "google_exchange": google_finance_exchange(company),
                },
                source="system",
                source_url=google_url,
                captured_at=captured_at,
                metadata={"validation": "HTTP response matched exact stock symbol"},
            )
        )
    except (httpx.HTTPError, ValueError) as exc:
        errors.append(f"google_finance_url: {exc}")
    try:
        if cninfo_org_id and cninfo_org_id.strip():
            verified_org_id = cninfo_org_id.strip()
            cninfo_url = validate_cninfo_company_url(client, company, org_id=verified_org_id)
            validation = "HTTP company page matched stockCode and orgId"
        elif company.eastmoney_secu_code.rsplit(".", 1)[-1].upper() == "SZ":
            verified_org_id = derive_cninfo_sz_org_id(company)
            cninfo_url = validate_constructed_cninfo_company_url(company)
            validation = "canonical CNINFO Shenzhen orgId matched stockCode and URL"
        else:
            verified_org_id = discover_cninfo_org_id(client, company)
            cninfo_url = validate_cninfo_company_url(client, company, org_id=verified_org_id)
            validation = "HTTP company page matched stockCode and orgId"
        values.append(
            SourceValue(
                field_id="cninfo_company_url",
                value=cninfo_url,
                raw_value={"stock_code": company.stock_code, "org_id": verified_org_id},
                source="cninfo",
                source_url=cninfo_url,
                captured_at=captured_at,
                metadata={"validation": validation},
            )
        )
    except (httpx.HTTPError, ValueError) as exc:
        errors.append(f"cninfo_company_url: {exc}")
    return ExternalLinksResult(source_values=values, errors=errors)


def external_links_node(
    state: WorkupAgentState,
    *,
    client: httpx.Client,
) -> dict[str, Any]:
    inputs = state.get("part_results", {}).get("part_02_input", {})
    if not isinstance(inputs, dict):
        raise ValueError("part_results.part_02_input must be a mapping when provided")
    result = collect_external_links(
        client,
        company=CompanyIdentity.model_validate(state["company"]),
        cninfo_org_id=inputs.get("cninfo_org_id"),
        captured_at=datetime.fromisoformat(state["created_at"]),
    )
    part_results = dict(state.get("part_results", {}))
    part_results["part_02"] = result.model_dump(mode="json")
    return {
        "part_results": part_results,
        "source_values": list(state.get("source_values", []))
        + [item.model_dump(mode="json") for item in result.source_values],
        "node_errors": list(state.get("node_errors", []))
        + [{"node": "part_02", "message": item} for item in result.errors],
    }
