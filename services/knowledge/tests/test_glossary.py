from regagent_knowledge.glossary import expand_query


def test_search_does_not_expand_ambiguous_lowercase_or_partial_words() -> None:
    question = "С кем связаться по CRM2 и TSS?"
    assert expand_query(question) == question


def test_search_preserves_original_and_keeps_repeated_forms_once() -> None:
    question = "Что согласует ПА и кто повторно передаёт ему РП или рп?"
    expanded = expand_query(question)
    assert expanded.startswith(question)
    assert expanded.count("руководитель проекта") == 1
    assert expanded.count("пресейл-архитектор") == 1
    assert expand_query(expanded) == expanded
