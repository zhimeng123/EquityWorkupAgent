import httpx
import pytest

from mlc_agent.company_resolver import resolve_a_share_company
from mlc_agent.exceptions import CompanyResolutionError


def _client(records):
    def handler(request):
        return httpx.Response(
            200,
            json={"QuotationCodeTable": {"Data": records, "Status": 0}},
            request=request,
        )

    return httpx.Client(transport=httpx.MockTransport(handler))


def record(code="000938", name="紫光股份"):
    return {
        "Code": code,
        "Name": name,
        "Classify": "AStock",
        "SecurityTypeName": "深A",
        "QuoteID": f"0.{code}",
    }


def test_resolve_exact_stock_code():
    with _client([record()]) as client:
        company = resolve_a_share_company(client, "000938")
    assert company.stock_code == "000938"
    assert company.eastmoney_secu_code == "000938.SZ"
    assert company.xueqiu_symbol == "SZ000938"


def test_ambiguous_company_name_is_rejected():
    with _client([record(), record("000001", "平安银行")]) as client:
        with pytest.raises(CompanyResolutionError, match="歧义"):
            resolve_a_share_company(client, "股份")

