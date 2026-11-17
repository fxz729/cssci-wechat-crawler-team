from datetime import datetime


HEADERS = [
    "标题", "作者", "发文时间", "阅读量", "点赞", "收藏", "在看", "赞赏", "转发", "评论", "版权", "位置", "摘要", "原文文件", "链接", "原文链接",
]


def export_articles_markdown(account_name: str, rows: list[dict]) -> str:
    lines = [f"# {account_name}", "", f"共 {len(rows)} 条记录", ""]
    lines.append("| " + " | ".join(HEADERS) + " |")
    lines.append("| " + " | ".join(["---"] * len(HEADERS)) + " |")
    for row in rows:
        lines.append("| " + " | ".join(_format_row(row)) + " |")
    lines.append("")
    return "\n".join(lines)


def _format_row(row: dict) -> list[str]:
    return [
        _quote(row.get("title")),
        _quote(row.get("author")),
        _format_time(row.get("publish_time")),
        _cell(row.get("read_count")),
        _cell(row.get("like_count")),
        _cell(row.get("favorite_count")),
        _cell(row.get("watching_count")),
        _cell(row.get("reward_count")),
        _cell(row.get("share_count")),
        _cell(row.get("comment_count")),
        _cell(row.get("copyright_text")),
        _cell(row.get("position_in_batch")),
        _quote(row.get("digest")),
        _cell(row.get("raw_html_path")),
        _cell(row.get("source_url")),
        _cell(row.get("canonical_url")),
    ]


def _format_time(value) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y/%m/%d %H:%M")
    return str(value)


def _quote(value) -> str:
    text = _cell(value).replace("\n", "\\n").replace("|", "｜")
    return f'"{text}"'


def _cell(value) -> str:
    if value is None:
        return ""
    return str(value).replace("\n", "\\n").replace("|", "｜")
