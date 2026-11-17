from sources.journal_catalog import build_aliases


CONTEXT_TERMS = ["编辑部", "期刊", "微信公众号"]


def build_discovery_queries(journal_name: str) -> list[str]:
    queries: list[str] = []
    aliases = build_aliases(journal_name)
    for alias in aliases:
        _append_unique(queries, alias)
    for term in CONTEXT_TERMS:
        for alias in aliases:
            _append_unique(queries, f"{alias} {term}")
    return queries


def _append_unique(values: list[str], value: str) -> None:
    if value and value not in values:
        values.append(value)
