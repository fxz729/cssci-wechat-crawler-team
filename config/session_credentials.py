import logging
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qs, urlparse


_LOGGER = logging.getLogger(__name__)
_REQUIRED_COOKIE_NAMES = frozenset({
    "slave_sid",
    "data_ticket",
    "appmsg_token",
    "wap_sid2",
    "pass_ticket",
    "wxuin",
})
_OPTIONAL_COOKIE_NAMES = frozenset({"wxtokenkey", "devicetype", "version"})


class CredentialValidationError(ValueError):
    pass


@dataclass(frozen=True, repr=False)
class SessionCredential:
    session_id: str
    backend_token: str = field(repr=False)
    fingerprint: str = field(repr=False)
    cookie: str = field(repr=False)
    user_agent: str = field(repr=False)
    cookie_names: frozenset[str] = field(repr=False)
    fakeid: str = field(default="", repr=False)

    def __repr__(self) -> str:
        return f"SessionCredential(session_id={self.session_id!r})"

    def safe_summary(self) -> str:
        missing_optional = sorted(_OPTIONAL_COOKIE_NAMES - self.cookie_names)
        return f"session_id={self.session_id} optional_cookie_fields_missing={missing_optional}"


def load_session_credentials(path: Path, *, enforce_permissions: bool = True) -> tuple[SessionCredential, ...]:
    if enforce_permissions:
        _validate_file_permissions(path)
    text = path.read_text(encoding="utf-8", errors="ignore")
    is_labeled = "### session:" in text
    credentials: list[SessionCredential] = []
    rejected = 0
    for block in _split_blocks(text):
        try:
            credentials.append(_parse_request_block(block))
        except CredentialValidationError:
            if is_labeled:
                raise
            rejected += 1
            session_id = block.splitlines()[0].removeprefix("### session:").strip()
            _LOGGER.warning("Skipping incomplete candidate session: %s", session_id)
    if not credentials:
        raise CredentialValidationError("no complete session credentials found")
    result = tuple(credentials)
    _validate_unique_session_ids(result)
    return result


def _validate_file_permissions(path: Path) -> None:
    if os.name == "nt":
        _LOGGER.warning("Windows cannot verify credential ACLs automatically; restrict the file with icacls.")
        return
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise CredentialValidationError("credentials file permissions must deny group and other access")


def _split_blocks(text: str) -> tuple[str, ...]:
    if "### session:" in text:
        return _split_labeled_blocks(text)
    return _split_unlabeled_blocks(text)


def _split_labeled_blocks(text: str) -> tuple[str, ...]:
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if line.startswith("### session:"):
            if current:
                blocks.append("\n".join(current))
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    if not blocks:
        raise CredentialValidationError("no session request blocks found")
    return tuple(blocks)


def _split_unlabeled_blocks(text: str) -> tuple[str, ...]:
    blocks: list[str] = []
    current: list[str] = []
    for line in text.splitlines():
        if _is_appmsgpublish_request_line(line):
            if current:
                blocks.append("\n".join(current))
            current = [f"### session: session-{len(blocks) + 1}", line]
        elif current:
            current.append(line)
    if current:
        blocks.append("\n".join(current))
    if not blocks:
        raise CredentialValidationError("no appmsgpublish request blocks found")
    return tuple(blocks)


def _is_appmsgpublish_request_line(line: str) -> bool:
    return _is_request_line(line) and urlparse(line.split(" ", 2)[1]).path == "/cgi-bin/appmsgpublish"


def _parse_request_block(block: str) -> SessionCredential:
    lines = block.splitlines()
    session_id = lines[0].removeprefix("### session:").strip()
    if not session_id:
        raise CredentialValidationError("session id is required")
    requests = _requests_in_session_block(lines[1:], session_id)
    backend_url, backend_headers = next(
        ((url, headers) for url, headers in requests if urlparse(url).path == "/cgi-bin/appmsgpublish"),
        (None, None),
    )
    if backend_url is None or backend_headers is None:
        raise CredentialValidationError(f"session {session_id}: request must target appmsgpublish")
    parsed_url = urlparse(backend_url)
    query = parse_qs(parsed_url.query, keep_blank_values=True)
    backend_token = query.get("token", [""])[0].strip()
    fingerprint = query.get("fingerprint", [""])[0].strip()
    fakeid = query.get("fakeid", [""])[0].strip()
    if not backend_token:
        raise CredentialValidationError(f"session {session_id}: backend token is required")
    if not fingerprint:
        raise CredentialValidationError(f"session {session_id}: fingerprint is required")
    user_agent = backend_headers.get("user-agent", "")
    if not user_agent:
        raise CredentialValidationError(f"session {session_id}: User-Agent header is required")
    backend_cookie = backend_headers.get("cookie", "")
    if not backend_cookie:
        raise CredentialValidationError(f"session {session_id}: Cookie header is required")
    cookie = _merge_cookies(
        headers.get("cookie", "")
        for _url, headers in requests
        if not session_id.startswith("session-") or _same_backend_session(backend_cookie, headers.get("cookie", ""))
    )
    cookie_names = frozenset(_cookie_names(cookie))
    missing = sorted(_REQUIRED_COOKIE_NAMES - cookie_names)
    if missing:
        raise CredentialValidationError(f"session {session_id}: missing required Cookie fields: {', '.join(missing)}")
    return SessionCredential(
        session_id=session_id,
        backend_token=backend_token,
        fingerprint=fingerprint,
        cookie=cookie,
        user_agent=user_agent,
        cookie_names=cookie_names,
        fakeid=fakeid,
    )


def _requests_in_session_block(lines: list[str], session_id: str) -> tuple[tuple[str, dict[str, str]], ...]:
    requests: list[tuple[str, dict[str, str]]] = []
    current: list[str] = []
    in_response = False
    for line in lines:
        if line.startswith("HTTP/"):
            if current:
                requests.append(_parse_request_lines(current, session_id))
                current = []
            in_response = True
            continue
        if _is_request_line(line):
            if current:
                requests.append(_parse_request_lines(current, session_id))
            current = [line]
            in_response = False
        elif not in_response and current:
            current.append(line)
    if current:
        requests.append(_parse_request_lines(current, session_id))
    if not requests:
        raise CredentialValidationError(f"session {session_id}: request line is required")
    return tuple(requests)


def _parse_request_lines(lines: list[str], session_id: str) -> tuple[str, dict[str, str]]:
    request_url = _request_url(lines, session_id)
    parsed_url = urlparse(request_url)
    if parsed_url.scheme != "https" or parsed_url.hostname != "mp.weixin.qq.com" or parsed_url.port is not None:
        raise CredentialValidationError(f"session {session_id}: request must target the official HTTPS endpoint")
    return request_url, _headers(lines[1:])


def _is_request_line(line: str) -> bool:
    parts = line.split(" ")
    return len(parts) == 3 and parts[0] == "GET" and parts[2].startswith("HTTP/")


def _request_url(lines: list[str], session_id: str) -> str:
    if not lines or not _is_request_line(lines[0]):
        raise CredentialValidationError(f"session {session_id}: request must be a complete GET request")
    return lines[0].split(" ", 2)[1]


def _same_backend_session(backend_cookie: str, candidate_cookie: str) -> bool:
    backend = _cookie_map(backend_cookie)
    candidate = _cookie_map(candidate_cookie)
    return all(
        backend.get(name) and backend.get(name) == candidate.get(name)
        for name in ("slave_sid", "data_ticket")
    )


def _cookie_map(cookie_header: str) -> dict[str, str]:
    return {
        name: value
        for part in cookie_header.split(";")
        if "=" in part
        for name, value in [part.strip().split("=", 1)]
        if name
    }


def _merge_cookies(cookie_headers) -> str:
    cookies: dict[str, str] = {}
    for cookie_header in cookie_headers:
        for part in cookie_header.split(";"):
            if "=" not in part:
                continue
            name, value = part.strip().split("=", 1)
            if name:
                cookies[name] = value
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def _headers(lines: list[str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in lines:
        if ":" not in line:
            continue
        name, value = line.split(":", 1)
        normalized_name = name.strip().lower()
        if normalized_name in headers and normalized_name in {"cookie", "user-agent"}:
            raise CredentialValidationError(f"duplicate {name.strip()} header")
        headers[normalized_name] = value.strip()
    return headers


def _cookie_names(cookie: str) -> tuple[str, ...]:
    return tuple(part.strip().split("=", 1)[0] for part in cookie.split(";") if "=" in part and part.strip())


def _validate_unique_session_ids(credentials: tuple[SessionCredential, ...]) -> None:
    identifiers = [credential.session_id for credential in credentials]
    if len(identifiers) != len(set(identifiers)):
        raise CredentialValidationError("duplicate session id")
