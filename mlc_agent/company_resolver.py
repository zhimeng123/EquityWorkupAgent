from __future__ import annotations

from urllib.parse import quote

import httpx

from mlc_agent.exceptions import CompanyResolutionError
from mlc_agent.schemas import CompanyIdentity


SUGGEST_URL = "https://searchapi.eastmoney.com/api/suggest/get"
SUGGEST_TOKEN = "D43BF722C8E33BDC906FB84D85E326E8"


def _market_details(item: dict[str, object]) -> tuple[str, str, str]:
    security_type = str(item.get("SecurityTypeName") or "")
    quote_id = str(item.get("QuoteID") or "")
    if "深" in security_type:
        return "深圳证券交易所", "SZ", quote_id or f"0.{item['Code']}"
    if "沪" in security_type:
        return "上海证券交易所", "SH", quote_id or f"1.{item['Code']}"
    if "北" in security_type:
        return "北京证券交易所", "BJ", quote_id or f"0.{item['Code']}"
    raise CompanyResolutionError(f"无法识别 A 股交易所: {security_type or 'unknown'}")


def resolve_a_share_company(client: httpx.Client, company_input: str) -> CompanyIdentity:
    query = company_input.strip()
    if not query:
        raise CompanyResolutionError("公司名称或股票代码不能为空。")

    response = client.get(
        SUGGEST_URL,
        params={"input": query, "type": "14", "token": SUGGEST_TOKEN},
    )
    response.raise_for_status()
    payload = response.json()
    records = payload.get("QuotationCodeTable", {}).get("Data") or []
    a_shares = [record for record in records if record.get("Classify") == "AStock"]
    if query.isdigit() and len(query) == 6:
        matches = [record for record in a_shares if record.get("Code") == query]
    else:
        exact = [record for record in a_shares if record.get("Name") == query]
        matches = exact or a_shares

    if not matches:
        raise CompanyResolutionError(f"未找到 A 股公司: {query}")
    if len(matches) != 1:
        choices = ", ".join(f"{item.get('Name')}({item.get('Code')})" for item in matches[:8])
        raise CompanyResolutionError(f"公司输入存在歧义，请改用六位股票代码: {choices}")

    selected = matches[0]
    stock_code = str(selected["Code"])
    exchange, suffix, eastmoney_secid = _market_details(selected)
    return CompanyIdentity(
        company_name=str(selected["Name"]),
        company_short_name=str(selected["Name"]),
        stock_code=stock_code,
        exchange=exchange,
        eastmoney_secid=eastmoney_secid,
        eastmoney_secu_code=f"{stock_code}.{suffix}",
        xueqiu_symbol=f"{suffix}{stock_code}",
    )


def build_eastmoney_url(company: CompanyIdentity) -> str:
    suffix = company.eastmoney_secu_code.rsplit(".", 1)[-1].lower()
    return f"https://quote.eastmoney.com/{suffix}{company.stock_code}.html"


def build_xueqiu_url(company: CompanyIdentity) -> str:
    return f"https://xueqiu.com/S/{quote(company.xueqiu_symbol)}"

