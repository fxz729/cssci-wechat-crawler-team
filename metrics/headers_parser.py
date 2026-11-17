import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse


@dataclass(frozen=True)
class JournalCredentials:
    biz: str
    uin: str
    key: str
    pass_ticket: str
    appmsg_token: str      # refreshed from /s? Set-Cookie response
    cookie: str            # full Cookie string from profile_ext request
    user_agent: str
    sample_article_mid: str
    sample_article_idx: int
    sample_article_sn: str
    has_tts_sample: bool


def parse_headers_file(path: Path) -> dict[str, JournalCredentials]:
    """Returns biz -> credentials mapping. Later occurrences override earlier ones."""
    text = path.read_text(encoding="utf-8")
    blocks = [b.strip() for b in re.split(r"-{30,}", text) if b.strip()]

    # Group blocks into journal groups: each group starts with a profile_ext block
    groups: list[list[str]] = []
    for block in blocks:
        first_line = block.split("\n", 1)[0]
        if "profile_ext" in first_line and "action=home" in first_line:
            groups.append([block])
        elif groups:
            groups[-1].append(block)

    result: dict[str, JournalCredentials] = {}

    for group in groups:
        profile_block = group[0]
        first_line = profile_block.split("\n", 1)[0]
        # Extract URL from "GET <url> HTTP/1.1"
        url_str = first_line.split(" ", 1)[1].rsplit(" ", 1)[0]
        qs = parse_qs(urlparse(url_str).query)

        biz = qs.get("__biz", [""])[0]
        uin = unquote(qs.get("uin", [""])[0])
        key = unquote(qs.get("key", [""])[0])
        pass_ticket = unquote(qs.get("pass_ticket", [""])[0])

        # Extract Cookie and User-Agent from profile_ext request headers
        cookie = ""
        user_agent = ""
        for line in profile_block.split("\n"):
            if line.startswith("Cookie:"):
                cookie = line[len("Cookie:"):].strip()
            elif line.startswith("User-Agent:"):
                user_agent = line[len("User-Agent:"):].strip()

        # Find /s? article block and /mp/tts block
        article_mid = ""
        article_idx = 0
        article_sn = ""
        appmsg_token = ""
        has_tts = False

        for block in group[1:]:
            first = block.split("\n", 1)[0]
            if "/mp/tts" in first:
                has_tts = True
            elif "/s?" in first and "__biz=" in first:
                url_str2 = first.split(" ", 1)[1].rsplit(" ", 1)[0]
                qs2 = parse_qs(urlparse(url_str2).query)
                article_mid = qs2.get("mid", [""])[0]
                article_idx = int(qs2.get("idx", ["0"])[0])
                article_sn = qs2.get("sn", [""])[0]
                # Find appmsg_token in response Set-Cookie (after HTTP/1.1 200 line)
                in_response = False
                for line in block.split("\n"):
                    if line.startswith("HTTP/1.1"):
                        in_response = True
                    if in_response and line.startswith("Set-Cookie: appmsg_token="):
                        val = line[len("Set-Cookie: appmsg_token="):].split(";")[0].strip()
                        appmsg_token = unquote(val)
                        break

        if not biz:
            continue

        result[biz] = JournalCredentials(
            biz=biz,
            uin=uin,
            key=key,
            pass_ticket=pass_ticket,
            appmsg_token=appmsg_token,
            cookie=cookie,
            user_agent=user_agent,
            sample_article_mid=article_mid,
            sample_article_idx=article_idx,
            sample_article_sn=article_sn,
            has_tts_sample=has_tts,
        )

    return result
