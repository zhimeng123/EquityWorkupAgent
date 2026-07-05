from mlc_agent.llm import MAX_DESCRIPTION_WORDS, _limit_words


def test_business_description_is_deterministically_limited():
    text = " ".join(f"word{index}" for index in range(80))
    result = _limit_words(text, MAX_DESCRIPTION_WORDS)
    assert len(result.rstrip(".").split()) == MAX_DESCRIPTION_WORDS
    assert result.endswith(".")

