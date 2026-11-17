import json

from export.json_exporter import export_articles_json


def test_export_articles_json_writes_array_file(tmp_path):
    rows = [{"title": "标题", "read_count": None}]
    output = tmp_path / "articles.json"

    export_articles_json(rows, str(output))

    assert json.loads(output.read_text(encoding="utf-8")) == rows
