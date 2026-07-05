from decimal import Decimal

from mlc_agent.annual_related_parties import extract_annual_related_party_transactions
from mlc_agent.report_parser import ParsedReport, PdfPage, PdfTable


def test_extracts_units_continuations_and_prefers_precise_note_row():
    material = [
        "关联方甲", "同一控制人", None, "销售产品", "市场公允价值",
        "100.00", "100.00", "1%", None, None, "合同结算", "不适用", None, None,
    ]
    report = ParsedReport(path="/tmp/a.pdf", pages=[
        PdfPage(page_number=1, text="十四、重大关联交易", tables=[PdfTable(rows=[material])]),
        PdfPage(page_number=2, text="十五、重大合同", tables=[]),
        PdfPage(page_number=3, text=(
            "十二、关联方关系及其交易\n5. 关联方交易\n"
            "向关联方销售商品和提供劳务\n交易内容 2025年 2024年\n"
            "关联方甲 产品销售 1,000,001.23 900,000.00"
        ), tables=[]),
        PdfPage(page_number=4, text="6. 关联方应收应付款项余额", tables=[]),
    ])

    rows = extract_annual_related_party_transactions(
        report, source_url="https://example/annual.pdf", report_year=2025
    )

    assert len(rows) == 1
    assert rows[0].amount_cny == Decimal("1000001.23")
    assert rows[0].source_page == 3
    assert rows[0].period == "FY2025"


def test_rejects_incomplete_rows_and_lessee_multi_measure_rows():
    report = ParsedReport(path="/tmp/a.pdf", pages=[
        PdfPage(page_number=1, text=(
            "5. 关联方交易\n（2） 关联方租赁\n作为出租人\n"
            "关联方甲 房屋及建筑物 12.34 10.00\n"
            "作为承租人\n关联方乙 房屋及建筑物 - - 99.00 1.00 -"
        ), tables=[]),
        PdfPage(page_number=2, text="6. 关联方应收应付款项余额", tables=[]),
    ])

    rows = extract_annual_related_party_transactions(
        report, source_url="https://example/annual.pdf", report_year=2025
    )

    assert [(item.related_party, item.amount_cny) for item in rows] == [
        ("关联方甲", Decimal("12.34"))
    ]
