"""Обязательные правила системного промпта (dev_docs/04-backend-agent.md)."""

from regagent_backend.guard import PROMPTS


def test_system_prompt_explains_follow_refs() -> None:
    prompt = (PROMPTS / "system.md").read_text(encoding="utf-8")
    assert "follow_refs" in prompt
    # Ссылка на документ — не источник: нужен поиск и цитата пункта связанного документа
    rule = next(line for line in prompt.splitlines() if "follow_refs" in line)
    assert "search_docs" in rule
