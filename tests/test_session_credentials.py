from pathlib import Path

import pytest

from config.session_credentials import CredentialValidationError, load_session_credentials


COOKIE = (
    "slave_sid=synthetic-slave; data_ticket=synthetic-ticket; "
    "appmsg_token=synthetic-appmsg; wap_sid2=synthetic-wap; "
    "pass_ticket=synthetic-pass; wxuin=123456; wxtokenkey=777; "
    "devicetype=UnifiedPCWindows; version=f2541b36"
)


def _request_block(session_id: str = "desktop-01", *, cookie: str = COOKIE, token: str = "backend-token", fingerprint: str = "fingerprint-value") -> str:
    return f"""### session: {session_id}
GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?action=list_ex&token={token}&fingerprint={fingerprint} HTTP/1.1
User-Agent: Mozilla/5.0 synthetic-agent
Cookie: {cookie}
"""


def _write_credentials(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "wechat-sessions.private.txt"
    path.write_text(text, encoding="utf-8")
    return path


def test_loads_multiple_complete_appmsgpublish_request_blocks(tmp_path):
    path = _write_credentials(tmp_path, _request_block() + "\n" + _request_block("desktop-02"))

    credentials = load_session_credentials(path, enforce_permissions=False)

    assert [credential.session_id for credential in credentials] == ["desktop-01", "desktop-02"]
    assert credentials[0].backend_token == "backend-token"
    assert credentials[0].fingerprint == "fingerprint-value"
    assert credentials[0].cookie == COOKIE
    assert credentials[0].user_agent == "Mozilla/5.0 synthetic-agent"


def test_merges_article_cookie_fields_from_same_session_request_block(tmp_path):
    backend_cookie = "slave_sid=synthetic-slave; data_ticket=synthetic-ticket; wxuin=123456"
    article_cookie = (
        "appmsg_token=synthetic-appmsg; wap_sid2=synthetic-wap; "
        "pass_ticket=synthetic-pass; wxtokenkey=777; "
        "devicetype=UnifiedPCWindows; version=f2541b36"
    )
    text = f"""### session: desktop-01
GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?action=list_ex&token=backend-token&fingerprint=fingerprint-value HTTP/1.1
User-Agent: Mozilla/5.0 synthetic-agent
Cookie: {backend_cookie}

GET https://mp.weixin.qq.com/mp/geticon?biz=target-biz&mid=1&idx=1 HTTP/1.1
User-Agent: Mozilla/5.0 synthetic-agent
Cookie: {article_cookie}
"""
    path = _write_credentials(tmp_path, text)

    credential = load_session_credentials(path, enforce_permissions=False)[0]

    assert credential.cookie_names >= {"slave_sid", "data_ticket", "appmsg_token", "wap_sid2", "pass_ticket", "wxuin"}
    assert "synthetic-slave" in credential.cookie
    assert "synthetic-appmsg" in credential.cookie


def test_loads_unlabeled_raw_requests_as_separate_candidate_sessions(tmp_path):
    raw = """GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?action=list_ex&token=first-token&fingerprint=first-fingerprint HTTP/1.1
User-Agent: synthetic-agent
Cookie: slave_sid=first-slave; data_ticket=first-ticket; wxuin=111

GET https://mp.weixin.qq.com/mp/geticon?biz=first-biz&mid=1&idx=1 HTTP/1.1
User-Agent: synthetic-agent
Cookie: slave_sid=first-slave; data_ticket=first-ticket; appmsg_token=first-appmsg; wap_sid2=first-wap; pass_ticket=first-pass

GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?action=list_ex&token=second-token&fingerprint=second-fingerprint HTTP/1.1
User-Agent: synthetic-agent
Cookie: slave_sid=second-slave; data_ticket=second-ticket; appmsg_token=second-appmsg; wap_sid2=second-wap; pass_ticket=second-pass; wxuin=222
"""
    path = _write_credentials(tmp_path, raw)

    credentials = load_session_credentials(path, enforce_permissions=False)

    assert [credential.session_id for credential in credentials] == ["session-1", "session-2"]
    assert credentials[0].backend_token == "first-token"
    assert credentials[0].fakeid == ""
    assert "first-appmsg" in credentials[0].cookie
    assert "second-appmsg" not in credentials[0].cookie
    assert credentials[1].backend_token == "second-token"


def test_skips_incomplete_unlabeled_candidate_when_later_candidate_is_complete(tmp_path, caplog):
    raw = """GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?action=list_ex&token=first-token&fingerprint=first-fingerprint&fakeid=first-biz HTTP/1.1
User-Agent: synthetic-agent
Cookie: slave_sid=first-slave; data_ticket=first-ticket; wxuin=111

GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?action=list_ex&token=second-token&fingerprint=second-fingerprint&fakeid=second-biz HTTP/1.1
User-Agent: synthetic-agent
Cookie: slave_sid=second-slave; data_ticket=second-ticket; appmsg_token=second-appmsg; wap_sid2=second-wap; pass_ticket=second-pass; wxuin=222
"""
    path = _write_credentials(tmp_path, raw)

    credentials = load_session_credentials(path, enforce_permissions=False)

    assert [credential.session_id for credential in credentials] == ["session-2"]
    assert credentials[0].fakeid == "second-biz"
    assert "session-1" in caplog.text
    assert "first-token" not in caplog.text
    assert "first-slave" not in caplog.text


def test_does_not_merge_unlabeled_article_request_with_different_backend_session(tmp_path):
    raw = """GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?action=list_ex&token=first-token&fingerprint=first-fingerprint HTTP/1.1
User-Agent: synthetic-agent
Cookie: slave_sid=first-slave; data_ticket=first-ticket; wxuin=111

GET https://mp.weixin.qq.com/mp/geticon?biz=other-biz&mid=1&idx=1 HTTP/1.1
User-Agent: synthetic-agent
Cookie: slave_sid=other-slave; data_ticket=other-ticket; appmsg_token=other-appmsg; wap_sid2=other-wap; pass_ticket=other-pass
"""
    path = _write_credentials(tmp_path, raw)

    with pytest.raises(CredentialValidationError, match="no complete session credentials"):
        load_session_credentials(path, enforce_permissions=False)


def test_ignores_response_headers_when_loading_unlabeled_requests(tmp_path):
    raw = """GET https://mp.weixin.qq.com/cgi-bin/appmsgpublish?action=list_ex&token=backend-token&fingerprint=fingerprint-value HTTP/1.1
User-Agent: synthetic-agent
Cookie: slave_sid=synthetic-slave; data_ticket=synthetic-ticket; appmsg_token=synthetic-appmsg; wap_sid2=synthetic-wap; pass_ticket=synthetic-pass; wxuin=123456

HTTP/1.1 200 OK
Set-Cookie: appmsg_token=response-token; Path=/
Cookie: response-cookie
"""
    path = _write_credentials(tmp_path, raw)

    credential = load_session_credentials(path, enforce_permissions=False)[0]

    assert "response-token" not in credential.cookie
    assert "response-cookie" not in credential.cookie


    backend_cookie = "slave_sid=synthetic-slave; data_ticket=synthetic-ticket; wxuin=123456"
    article_cookie = "appmsg_token=synthetic-appmsg; wap_sid2=synthetic-wap; pass_ticket=synthetic-pass"
    text = _request_block(cookie=backend_cookie) + f"""
### session: desktop-02
GET https://mp.weixin.qq.com/mp/geticon?biz=target-biz&mid=1&idx=1 HTTP/1.1
User-Agent: Mozilla/5.0 synthetic-agent
Cookie: {article_cookie}
"""
    path = _write_credentials(tmp_path, text)

    with pytest.raises(CredentialValidationError, match="appmsg_token"):
        load_session_credentials(path, enforce_permissions=False)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (_request_block() + "\n" + _request_block(), "duplicate session id"),
        ("### session: desktop-01\nGET https://mp.weixin.qq.com/s?mid=1 HTTP/1.1\nUser-Agent: agent\nCookie: value\n", "appmsgpublish"),
        (_request_block(token=""), "backend token"),
        (_request_block(fingerprint=""), "fingerprint"),
        (_request_block(cookie="slave_sid=synthetic-slave"), "data_ticket"),
        (_request_block(cookie="slave_sid=x; data_ticket=x; appmsg_token=x; wap_sid2=x; pass_ticket=x"), "wxuin"),
        (_request_block().replace("https://mp.weixin.qq.com", "http://mp.weixin.qq.com"), "official HTTPS endpoint"),
        (_request_block().replace("mp.weixin.qq.com", "example.invalid"), "official HTTPS endpoint"),
        (_request_block() + "Cookie: another=value\n", "duplicate Cookie header"),
        (_request_block() + "User-Agent: another-agent\n", "duplicate User-Agent header"),
    ],
)
def test_rejects_invalid_request_blocks_without_disclosing_sensitive_values(tmp_path, text, expected):
    path = _write_credentials(tmp_path, text)

    with pytest.raises(CredentialValidationError) as exc_info:
        load_session_credentials(path, enforce_permissions=False)

    message = str(exc_info.value)
    assert expected in message
    for secret in ("synthetic-slave", "synthetic-ticket", "synthetic-appmsg", "synthetic-pass", "backend-token"):
        assert secret not in message


def test_credential_representation_and_summary_are_redacted(tmp_path):
    path = _write_credentials(tmp_path, _request_block())

    credential = load_session_credentials(path, enforce_permissions=False)[0]
    rendered = f"{credential!r} {credential.safe_summary()}"

    assert "desktop-01" in rendered
    for secret in ("synthetic-slave", "synthetic-ticket", "synthetic-appmsg", "synthetic-pass", "backend-token"):
        assert secret not in rendered


def test_rejects_group_or_other_readable_credentials_file_on_posix(tmp_path, monkeypatch):
    path = _write_credentials(tmp_path, _request_block())
    path.chmod(0o644)
    monkeypatch.setattr("config.session_credentials.os.name", "posix")

    with pytest.raises(CredentialValidationError, match="permissions"):
        load_session_credentials(path)


def test_windows_skips_posix_mode_check_and_emits_acl_guidance(tmp_path, monkeypatch, caplog):
    path = _write_credentials(tmp_path, _request_block())
    path.chmod(0o644)
    monkeypatch.setattr("config.session_credentials.os.name", "nt")

    credentials = load_session_credentials(path)

    assert credentials[0].session_id == "desktop-01"
    assert "icacls" in caplog.text
