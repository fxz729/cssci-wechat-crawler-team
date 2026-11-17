"""Offline TXT parsing; session values stay in the redacted HomeRequest.

Only home parameters/headers are returned. Optional urlcheck input must match
shared session fields; it never supplies missing home credentials or headers.
Errors are fixed codes, never source text, values, or filesystem paths.
"""
from __future__ import annotations

from pathlib import Path
import re
from urllib.parse import unquote, urlsplit

from crawl.profile_session_probe import HomeRequest, ProbeError
from sources.journal_catalog import (
    ProfileLinkTarget, normalize_journal_name, parse_profile_links_text,
)

_HOST = "mp.weixin.qq.com"
_HOME = "/mp/profile_ext"
_CHECK = "/mp/urlcheck"
_LIMIT = 1024 * 1024
_BIZ = re.compile(r"[A-Za-z0-9_=-]{1,200}\Z")
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]")
_SESSION = {"uin", "key", "pass_ticket", "appmsg_token", "wxtoken"}
_HEADERS = {name.lower(): name for name in ("User-Agent", "Cookie", "Accept", "Accept-Language")}
_ROSTER_HEADER = ["序号", "学科", "类别", "期刊原名", "搜索到的公众号作者", "公众号主页链接", "样本文章标题"]


def _text(text: str) -> str:
    if not isinstance(text, str):
        raise ProbeError("invalid_input_text")
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise ProbeError("invalid_input_encoding") from None
    if size > _LIMIT:
        raise ProbeError("input_too_large")
    text = text.removeprefix("\ufeff").replace("\r\n", "\n")
    if _CONTROL.search(text.replace("\n", "").replace("\t", "")):
        raise ProbeError("invalid_input_control")
    return text


def _decode(value: str) -> str:
    if re.search(r"%(?![0-9A-Fa-f]{2})", value):
        raise ProbeError("invalid_percent_encoding")
    try:
        decoded = unquote(value, encoding="utf-8", errors="strict")
    except UnicodeError:
        raise ProbeError("invalid_percent_encoding") from None
    if _CONTROL.search(decoded):
        raise ProbeError("invalid_input_control")
    return decoded


def _url(value: str) -> tuple[str, dict[str, str]]:
    # Split first, then decode individual keys/values ONCE. unquote, not
    # parse_qs/unquote_plus: Fiddler's literal '+' is not a form-space.
    if any(char.isspace() for char in value) or _CONTROL.search(value):
        raise ProbeError("invalid_request_url")
    try:
        parsed = urlsplit(value)
    except ValueError:
        raise ProbeError("invalid_request_url") from None
    if (parsed.scheme != "https" or parsed.netloc != _HOST
            or parsed.path not in {_HOME, _CHECK}
            or parsed.fragment not in {"", "wechat_redirect"}):
        raise ProbeError("invalid_request_url")
    params = {}
    for pair in parsed.query.split("&"):
        key, separator, val = pair.partition("=")
        key = _decode(key)
        if not separator or not re.fullmatch(r"[A-Za-z0-9_]+", key):
            raise ProbeError("invalid_request_parameter")
        if key in params:
            raise ProbeError("duplicate_request_parameter")
        params[key] = _decode(val)
    if parsed.path == _HOME and params.get("action") not in {"home", "urlcheck"}:
        raise ProbeError("invalid_home_action")
    if parsed.path == _CHECK and params.get("action", "urlcheck") != "urlcheck":
        raise ProbeError("invalid_urlcheck_action")
    if "__biz" in params and not _BIZ.fullmatch(params["__biz"]):
        raise ProbeError("invalid_biz")
    # Normalize the endpoint kind so profile_ext?action=urlcheck is not a home.
    return (_CHECK if params.get("action") == "urlcheck" else parsed.path), params


def _header(line: str, headers: dict[str, str]) -> None:
    name, separator, value = line.partition(":")
    if not separator or not re.fullmatch(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+", name):
        raise ProbeError("invalid_request_header")
    name = name.lower()
    value = value.strip(" ")
    if _CONTROL.search(value) or _CONTROL.search(unquote(value)):
        raise ProbeError("invalid_request_header")
    if name in headers:
        raise ProbeError("duplicate_request_header")
    if name in {"content-length", "transfer-encoding"}:
        raise ProbeError("unsupported_request_body")
    if name == "host" and value != _HOST:
        raise ProbeError("conflicting_request_host")
    headers[name] = value


def _tsv(cells: list[str], columns: dict[str, int] | None) -> str:
    if columns is None:
        if len(cells) < 5 or not cells[0].isdigit():
            raise ProbeError("invalid_fiddler_row")
        columns = {"protocol": 2, "host": 3, "url": 4}
    if max(columns.values()) >= len(cells):
        raise ProbeError("invalid_fiddler_row")
    if (cells[columns["protocol"]].lower() != "https"
            or cells[columns["host"]] != _HOST
            or ("method" in columns and cells[columns["method"]] != "GET")):
        raise ProbeError("invalid_fiddler_origin")
    value = cells[columns["url"]]
    if value.startswith("/") and not value.startswith("//"):
        value = f"https://{_HOST}{value}"
    return value


def read_request_input(text: str, *, expected_biz: str | None = None) -> HomeRequest:
    """Read one home plus optional matching urlcheck; perform no I/O.

    Accept absolute URLs, GET request text, and Fiddler TSV (named columns or
    standard #/Result/Protocol/Host/URL order). Relative URLs require TSV HTTPS
    and Host columns. Stop at the first HTTP response, including its body.
    Missing User-Agent/Cookie stay absent and require downstream preflight.
    """
    text = _text(text)
    records = []
    current_headers = None
    columns = None
    for line in text.split("\n"):
        if re.match(r"^HTTP/\d(?:\.\d)?\s+\d{3}(?:\s|$)", line):
            break
        if not line.strip():
            continue
        if "\t" in line:
            cells = line.split("\t")
            names = [cell.lower() for cell in cells]
            if {"protocol", "host", "url"}.issubset(names):
                if len(names) != len(set(names)):
                    raise ProbeError("invalid_fiddler_columns")
                columns = {name: names.index(name) for name in ("protocol", "host", "url", "method") if name in names}
                continue
            value = _tsv(cells, columns)
        elif line.startswith("#"):
            continue
        elif line.startswith("GET "):
            match = re.fullmatch(r"GET (\S+) HTTP/(?:1\.[01]|2(?:\.0)?)", line)
            if not match:
                raise ProbeError("invalid_request_line")
            value = match[1]
        elif line.startswith(("https://", "http://")):
            value = line
        else:
            if current_headers is None:
                raise ProbeError("missing_home_request")
            _header(line, current_headers)
            continue
        path, params = _url(value)
        current_headers = {}
        records.append((path, params, current_headers))

    homes = [record for record in records if record[0] == _HOME]
    checks = [record for record in records if record[0] == _CHECK]
    if len(homes) != 1 or len(checks) > 1:
        raise ProbeError("ambiguous_or_missing_home_request")
    _, params, headers = homes[0]
    if not all(params.get(key, "").strip() for key in ("__biz", "uin", "key", "pass_ticket")):
        raise ProbeError("incomplete_home_request")
    if expected_biz is not None and (not isinstance(expected_biz, str) or params["__biz"] != expected_biz):
        raise ProbeError("unexpected_home_biz")
    for _, supplement, supplement_headers in checks:
        shared = _SESSION & params.keys() & supplement.keys()
        if not any(params[key] for key in shared - {"uin"}):
            raise ProbeError("unassociated_urlcheck")
        if any(params[key] != supplement[key] for key in shared):
            raise ProbeError("conflicting_urlcheck_session")
        if "__biz" in supplement and supplement["__biz"] != params["__biz"]:
            raise ProbeError("conflicting_urlcheck_biz")
        for name in ("user-agent", "cookie"):
            if name in headers and name in supplement_headers and headers[name] != supplement_headers[name]:
                raise ProbeError("conflicting_urlcheck_header")
    forwarded = {_HEADERS[name]: value for name, value in headers.items() if name in _HEADERS}
    return HomeRequest(params["__biz"], params, forwarded)


def load_targets(path: str | Path) -> list[ProfileLinkTarget]:
    """Load the public Markdown roster strictly, without a hard-coded count.

    Validate every table row before using the legacy permissive parser. Public
    profile URLs cannot carry session parameters (ProfileLinkTarget has a repr).
    """
    try:
        source = Path(path)
        with source.open("r", encoding="utf-8-sig") as stream:
            text = stream.read(_LIMIT + 1)
    except (OSError, UnicodeError, ValueError, TypeError):
        raise ProbeError("unreadable_target_roster") from None
    text = _text(text)
    seen_ids, seen_biz, seen_names = set(), set(), set()
    row_lines = []
    table_started = False
    for line_number, raw in enumerate(text.split("\n"), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if not table_started and "|" not in line:
            continue
        table_started = True
        if not line.startswith("|") or not line.endswith("|"):
            raise ProbeError("invalid_roster_line")
        # Legacy sample titles contain raw pipes; only the first six columns
        # carry target identity. Keep the entire seventh-column tail as text.
        cells = [cell.strip() for cell in line[1:-1].split("|", 6)]
        if cells == _ROSTER_HEADER or (len(cells) == 7 and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)):
            continue
        if len(cells) != 7:
            raise ProbeError("invalid_roster_row")
        index, discipline, tier, journal, account, profile, _ = cells
        if (not re.fullmatch(r"[1-9][0-9]*", index) or not discipline
                or tier not in {"cssci", "cssci_extended"} or not journal or journal != account
                or any(_CONTROL.search(cell) for cell in cells)):
            raise ProbeError("invalid_roster_row")
        endpoint, params = _url(profile)
        biz = params.get("__biz", "")
        if endpoint != _HOME or not _BIZ.fullmatch(biz):
            raise ProbeError("invalid_roster_profile")
        if set(params) - {"action", "__biz", "scene"}:
            raise ProbeError("nonpublic_roster_parameter")
        name = normalize_journal_name(journal)
        if index in seen_ids or biz in seen_biz or name in seen_names:
            raise ProbeError("duplicate_roster_target")
        seen_ids.add(index)
        seen_biz.add(biz)
        seen_names.add(name)
        row_lines.append(line_number)
    if not row_lines:
        raise ProbeError("empty_target_roster")
    targets = parse_profile_links_text(text, source_file=str(source))
    if [target.source_line for target in targets] != row_lines:
        raise ProbeError("invalid_roster_row")
    return targets
