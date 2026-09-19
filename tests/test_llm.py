import json

from mlc_agent.llm import MAX_DESCRIPTION_WORDS, _limit_words, chat_completion_text


def test_business_description_is_deterministically_limited():
    text = " ".join(f"word{index}" for index in range(80))
    result = _limit_words(text, MAX_DESCRIPTION_WORDS)
    assert len(result.rstrip(".").split()) == MAX_DESCRIPTION_WORDS
    assert result.endswith(".")


class _Delta:
    def __init__(self, content):
        self.content = content
        self.reasoning_content = "thinking"


class _Chunk:
    def __init__(self, content):
        self.choices = [type("Choice", (), {"delta": _Delta(content)})()]


class _StreamingCompletions:
    def __init__(self, pieces):
        self.pieces = pieces
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        return [_Chunk(piece) for piece in self.pieces]


class _NonStreamingMessage:
    def __init__(self, content):
        self.content = content


class _NonStreamingCompletions:
    def create(self, **kwargs):
        return type(
            "Response", (), {"choices": [type("Choice", (), {"message": _NonStreamingMessage('{"ok": true}')})()]}
        )()


def _client(completions):
    return type("Client", (), {"chat": type("Chat", (), {"completions": completions})()})()


def test_chat_completion_text_streams_and_joins_content():
    completions = _StreamingCompletions(['{"a"', ": 1", "}"])
    text = chat_completion_text(_client(completions), model="m", messages=[{"role": "user", "content": "x"}])
    assert text == '{"a": 1}'
    assert completions.kwargs["stream"] is True
    assert json.loads(text) == {"a": 1}


def test_chat_completion_text_accepts_non_streaming_test_double():
    text = chat_completion_text(_client(_NonStreamingCompletions()), model="m", messages=[{"role": "user", "content": "x"}])
    assert json.loads(text) == {"ok": True}

