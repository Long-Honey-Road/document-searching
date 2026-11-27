from langchain_core.messages import AIMessage
from regagent_backend.citations import extract_citations, public_text, verify_citations


def test_extract_citations_in_first_occurrence_order() -> None:
    assert extract_citations("[f:a@1#2.1] [f:practice:14] [f:a@1#2.1]") == [
        "a@1#2.1",
        "practice:14",
    ]


def test_unknown_or_malformed_citations_fail_even_with_valid_sources() -> None:
    assert not verify_citations("Срок [f:a@1#2.1] [f:unknown]", {"a@1#2.1"}).ok
    assert not verify_citations("Срок [f:a@1#2.1] [f:bad id]", {"a@1#2.1"}).ok
    assert not verify_citations("Срок — 1 день.", {"a@1#2.1"}).ok


def test_public_text_removes_reasoning_and_incomplete_think_blocks() -> None:
    message = AIMessage(
        content=[
            {"type": "reasoning", "reasoning": "secret"},
            {"type": "text", "text": "<think>секрет\n</think>Итог. <think>черновик"},
        ],
        additional_kwargs={"reasoning_content": "secret"},
    )
    assert public_text(message) == "Итог."
