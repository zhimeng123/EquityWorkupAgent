from mlc_agent.evidence_text import anchor_extracted_evidence, contains_normalized_evidence


def test_evidence_matching_normalizes_unicode_and_whitespace_only():
    document = "金额为 １２３ 元。\n董事会确认该事项。"

    assert contains_normalized_evidence(document, "金额为 123   元。 董事会")
    assert not contains_normalized_evidence(document, "金额为123元…董事会")


def test_anchor_uses_actual_supplied_page_and_exact_source_line():
    url = "https://example.com/report.pdf"
    documents = [{
        "source_url": url,
        "pages": [
            {"page_number": 3, "text": "普通章节。"},
            {"page_number": 8, "text": "公司在美国设立 Alpha US Inc.，注册地为 California。"},
        ],
    }]
    output = {
        "name": "Alpha US Inc.",
        "country": "美国",
        "evidence": {
            "source_url": url,
            "page_number": 214,
            "evidence_text": "公司在美国设立 Alpha US Inc.",
        },
    }

    anchored = anchor_extracted_evidence(output, documents)

    assert anchored["evidence"]["page_number"] == 8
    assert anchored["evidence"]["evidence_text"] in documents[0]["pages"][1]["text"]
