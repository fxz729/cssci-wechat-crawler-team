import json


def export_articles_json(rows: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8") as file:
        json.dump(rows, file, ensure_ascii=False, indent=2, default=str)
