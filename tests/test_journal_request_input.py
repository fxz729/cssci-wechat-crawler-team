"""Synthetic-only contracts for journal TXT input and fixed targets."""
import importlib
import importlib.util
import traceback
from pathlib import Path

import pytest

from crawl.profile_session_probe import HomeRequest
from sources.journal_catalog import ProfileLinkTarget

MODULE = "config.journal_request_input"
HOME = ("https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=test-biz"
        "&uin=TEST_UIN&key=TEST_KEY&pass_ticket=TEST_TICKET")
CHECK = "https://mp.weixin.qq.com/mp/urlcheck?uin=TEST_UIN&key=TEST_KEY"


@pytest.fixture
def api():
    assert importlib.util.find_spec(MODULE) is not None, "TXT input module is not implemented"
    return importlib.import_module(MODULE)


def request(url=HOME, headers="User-Agent: TEST_AGENT\nCookie: sid=TEST_COOKIE"):
    return f"GET {url} HTTP/1.1\nHost: mp.weixin.qq.com\n{headers}\n\n"


def test_http_response_is_truncated_and_repr_is_safe(api):
    result = api.read_request_input(request() + "HTTP/1.1 200 OK\n"
                                    "User-Agent: RESPONSE_SECRET\nSet-Cookie: RESPONSE_SECRET\n\n"
                                    + request(HOME.replace("TEST_KEY", "RESPONSE_SECRET")))
    assert type(result) is HomeRequest
    assert result.biz == "test-biz"
    assert result.headers == {"User-Agent": "TEST_AGENT", "Cookie": "sid=TEST_COOKIE"}
    assert "TEST_" not in repr(result)
    assert "test-biz" not in repr(result)
    assert "RESPONSE_SECRET" not in repr(result.headers)


@pytest.mark.parametrize("text", [HOME, "\ufeff" + HOME + "\r\n", request(headers=""),
    "# home\n" + HOME + "\n\n# optional urlcheck\n" + CHECK,
    CHECK + "\n" + HOME])
def test_supported_inputs_and_missing_user_agent(api, text):
    result = api.read_request_input(text, expected_biz="test-biz")
    assert result.params["key"] == "TEST_KEY"
    assert "User-Agent" not in result.headers
    assert result.params["action"] == "home"


def test_url_with_explicit_optional_headers(api):
    result = api.read_request_input(HOME + "\n\n# Optional captured headers\nUser-Agent: TEST_AGENT\n")
    assert result.headers == {"User-Agent": "TEST_AGENT"}


@pytest.mark.parametrize("raw,expected", [("A+B", "A+B"), ("A%2BB", "A+B"),
    ("A%252BB", "A%2BB"), ("A%26key%3DB", "A&key=B"), ("A%3DB", "A=B")])
def test_session_values_are_decoded_once_without_form_plus_conversion(api, raw, expected):
    result = api.read_request_input(HOME.replace("TEST_TICKET", raw))
    assert result.params["pass_ticket"] == expected
    assert result.params["key"] == "TEST_KEY"


def test_fiddler_tsv_with_header_and_relative_url(api):
    text = ("#\tResult\tProtocol\tHost\tURL\tBody\tProcess\n"
            "1\t200\tHTTPS\tmp.weixin.qq.com\t" + HOME.split("mp.weixin.qq.com", 1)[1]
            + "\t123\tTEST_PROCESS\n")
    assert api.read_request_input(text).biz == "test-biz"


def test_fiddler_tsv_without_header_and_absolute_url(api):
    text = "1\t200\tHTTPS\tmp.weixin.qq.com\t" + HOME + "\t123\n"
    assert api.read_request_input(text).biz == "test-biz"


def test_fiddler_named_columns_can_be_reordered(api):
    text = "URL\tHost\tProtocol\n" + HOME + "\tmp.weixin.qq.com\tHTTPS\n"
    assert api.read_request_input(text).biz == "test-biz"


@pytest.mark.parametrize("suffix", ["&key=OTHER", "&%6bey=OTHER", "&scene=1&scene=1", "&empty=&empty="])
def test_duplicate_query_parameters_are_rejected(api, suffix):
    with pytest.raises(ValueError):
        api.read_request_input(HOME + suffix)


@pytest.mark.parametrize("text", ["", "garbage", CHECK, HOME + "\n" + HOME,
    HOME.replace("https:", "http:"), HOME.replace("mp.weixin.qq.com/", "mp.weixin.qq.com.evil/"),
    HOME.replace("mp.weixin.qq.com/", "user@mp.weixin.qq.com/"),
    HOME.replace("mp.weixin.qq.com/", "mp.weixin.qq.com:443/"),
    HOME.replace("profile_ext", "jsmonitor"), HOME.replace("profile_ext", "%70rofile_ext"),
    HOME.replace("action=home", "action=getmsg"), HOME.replace("&key=TEST_KEY", ""),
    HOME.replace("TEST_KEY", ""), HOME.replace("TEST_KEY", "%0D%0AX-Injected%3Ayes"),
    HOME.replace("TEST_KEY", "%GG"), HOME.replace("TEST_KEY", "%FF"),
    HOME.replace("test-biz", "bad%2Bbiz"), HOME + "#evil", HOME.replace("https:", "https:\t"),
    "POST " + HOME + " HTTP/1.1\n\n", HOME + "\nnot a header", "\x00" + HOME])
def test_invalid_request_inputs_are_rejected(api, text):
    with pytest.raises(ValueError):
        api.read_request_input(text)


@pytest.mark.parametrize("headers", ["Host: evil.invalid", "User-Agent: A\nuser-agent: B",
    "User-Agent : A", "User-Agent: A\n injected", "User-Agent: A\rX-Injected: yes",
    "Cookie: sid=A\x00B", "User-Agent: A%0d%0aX-Injected: yes", "X-Bad Name: secret",
    "Content-Length: 3", "Transfer-Encoding: chunked", "User-Agent: A\u2028X-Injected: yes"])
def test_header_injection_and_ambiguous_headers_rejected(api, headers):
    with pytest.raises(ValueError):
        api.read_request_input(request(headers=headers))


def test_unknown_but_valid_headers_are_not_forwarded(api):
    result = api.read_request_input(request(headers="X-Debug: TEST_SECRET\nAccept: text/html\nAccept-Language: zh-CN"))
    assert result.headers == {"Accept": "text/html", "Accept-Language": "zh-CN"}


@pytest.mark.parametrize("check", [CHECK.replace("TEST_KEY", "OTHER_SECRET"),
    CHECK + "&__biz=other-biz", CHECK + "&key=TEST_KEY",
    "https://mp.weixin.qq.com/mp/urlcheck?scene=1", CHECK.replace("https:", "http:")])
def test_urlcheck_conflicts_or_missing_association_rejected(api, check):
    with pytest.raises(ValueError):
        api.read_request_input(HOME + "\n" + check)


def test_urlcheck_does_not_supply_home_fields_or_headers(api):
    result = api.read_request_input(request(headers="") + request(CHECK + "&appmsg_token=TEST_EXTRA"))
    assert result.headers == {}
    assert "appmsg_token" not in result.params
    with pytest.raises(ValueError):
        api.read_request_input(HOME.replace("&key=TEST_KEY", "") + "\n" + CHECK)


def test_expected_biz_and_errors_do_not_expose_secrets(api):
    with pytest.raises(ValueError) as caught:
        api.read_request_input(HOME, expected_biz="SECRET_EXPECTED_BIZ")
    rendered = "".join(traceback.format_exception(caught.type, caught.value, caught.tb))
    assert "SECRET_EXPECTED_BIZ" not in str(caught.value)
    assert "TEST_KEY" not in rendered
    assert "TEST_TICKET" not in rendered
    assert "TEST_" not in repr(caught.value)


@pytest.mark.parametrize("protocol,host", [("HTTP", "mp.weixin.qq.com"),
    ("HTTPS", "evil.invalid"), ("", "mp.weixin.qq.com")])
def test_tsv_requires_consistent_explicit_https_and_host(api, protocol, host):
    with pytest.raises(ValueError):
        api.read_request_input(f"1\t200\t{protocol}\t{host}\t{HOME}\t0")


def test_input_type_and_size_limits(api):
    for text in (None, b"bytes", "x" * (1024 * 1024 + 1), "\ud800" + HOME):
        with pytest.raises(ValueError):
            api.read_request_input(text)


HEADER = ("# Synthetic roster\n\n"
          "| 序号 | 学科 | 类别 | 期刊原名 | 搜索到的公众号作者 | 公众号主页链接 | 样本文章标题 |\n"
          "| --- | --- | --- | --- | --- | --- | --- |\n")


def row(index=1, name="测试期刊", biz="test-biz"):
    return (f"| {index} | 教育学 | cssci | {name} | {name} | "
            f"https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz={biz}&scene=124#wechat_redirect | 样本 |\n")


def roster_file(tmp_path, text):
    path = tmp_path / "synthetic-roster.md"
    path.write_text(text, encoding="utf-8-sig")
    return path


def test_load_targets_reuses_catalog_types_and_preserves_order_provenance(api, tmp_path):
    path = roster_file(tmp_path, HEADER + row(2, "期刊乙", "biz-b") + row(1))
    result = api.load_targets(path)
    assert isinstance(result, list)
    assert all(type(item) is ProfileLinkTarget for item in result)
    assert [item.biz for item in result] == ["biz-b", "test-biz"]
    assert [item.source_line for item in result] == [5, 6]
    assert all(item.source_file == str(path) for item in result)
    assert len(api.load_targets(str(roster_file(tmp_path, HEADER + row())))) == 1


@pytest.mark.parametrize("text", [HEADER, HEADER + row() + row(),
    HEADER + row() + row(2, "另一刊"), HEADER + row() + row(2, biz="biz-b"),
    HEADER + row() + row(1, "另一刊", "biz-b"),
    HEADER + row().replace("| cssci |", "| unknown |"),
    HEADER + row().replace("| 教育学 |", "| |"),
    HEADER + row().replace("| 测试期刊 | 测试期刊 |", "| 测试期刊 | 别的公众号 |"),
    HEADER + row().replace("| 1 |", "| bad-index |"),
    HEADER + row().replace("| 1 |", "| 0 |"),
    HEADER + row().replace(" | 样本 |", " |"),
    HEADER + row().replace("| 教育学 |", "| extra | 教育学 |"),
    HEADER + row().replace("https:", "http:"),
    HEADER + row().replace("mp.weixin.qq.com/", "user@mp.weixin.qq.com/"),
    HEADER + row().replace("mp.weixin.qq.com/", "mp.weixin.qq.com:443/"),
    HEADER + row().replace("profile_ext", "urlcheck"),
    HEADER + row().replace("action=home", "action=getmsg"),
    HEADER + row().replace("&scene=124", "&__biz=second"),
    HEADER + row().replace("test-biz", "bad%2Bbiz"),
    HEADER + row().replace("&scene=124", "&key=TEST_SECRET"),
    HEADER + row() + "| malformed |\n",
    HEADER + row() + row(2, "测试期刊（甲）", "biz-b") + row(3, "测试期刊(甲)", "biz-c")])
def test_invalid_roster_rows_never_silently_disappear(api, tmp_path, text):
    with pytest.raises(ValueError) as caught:
        api.load_targets(roster_file(tmp_path, text))
    assert "TEST_SECRET" not in str(caught.value)
    assert "test-biz" not in repr(caught.value)


def test_missing_or_invalid_utf8_roster_has_sanitized_error(api, tmp_path):
    path = tmp_path / "SECRET_PATH.md"
    with pytest.raises(ValueError) as caught:
        api.load_targets(path)
    assert "SECRET_PATH" not in str(caught.value)
    path.write_bytes(b"\xff")
    with pytest.raises(ValueError):
        api.load_targets(path)


def test_example_is_placeholder_only(api):
    path = Path(__file__).resolve().parents[1] / "config" / "journal-session.private.example.txt"
    text = path.read_text(encoding="utf-8")
    assert "HOME" in text and "URLCHECK" in text and "User-Agent" in text
    assert "https://" not in text
    with pytest.raises(ValueError):
        api.read_request_input(text)


def test_public_module_is_available():
    assert importlib.util.find_spec(MODULE) is not None, "TXT input module is not implemented"


def test_roster_accepts_public_markdown_preamble(api, tmp_path):
    text = HEADER.replace("# Synthetic roster\n", "# Synthetic roster\n\n- 来源目录：公开目录\n- 已找到：若干\n") + row()
    assert len(api.load_targets(roster_file(tmp_path, text))) == 1


def test_uin_alone_cannot_associate_a_bizless_urlcheck(api):
    with pytest.raises(ValueError):
        api.read_request_input(HOME + "\nhttps://mp.weixin.qq.com/mp/urlcheck?uin=TEST_UIN")


def test_urlcheck_session_encoding_matches_without_merging(api):
    text = HOME.replace("TEST_KEY", "A+B") + "\n" + CHECK.replace("TEST_KEY", "A%2BB")
    assert api.read_request_input(text).params["key"] == "A+B"


@pytest.mark.parametrize("header", ["Cookie: sid=OTHER_SECRET", "User-Agent: OTHER_AGENT"])
def test_urlcheck_conflicting_capture_headers_are_rejected(api, header):
    with pytest.raises(ValueError):
        api.read_request_input(request() + request(CHECK, header))


def test_full_http_crlf_and_redirect_fragment(api):
    result = api.read_request_input(request(HOME + "#wechat_redirect").replace("\n", "\r\n"))
    assert result.headers["User-Agent"] == "TEST_AGENT"


def test_parser_never_opens_files_or_sends_requests(api, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("TXT parsing must be offline and in-memory")
    monkeypatch.setattr(Path, "open", forbidden)
    monkeypatch.setattr("requests.sessions.Session.request", forbidden)
    assert api.read_request_input(HOME).biz == "test-biz"


def test_roster_sample_title_can_contain_legacy_unescaped_pipes(api, tmp_path):
    text = HEADER + row().replace("| 样本 |", "| 样本 | 栏目 | 副题 |")
    result = api.load_targets(roster_file(tmp_path, text))
    assert len(result) == 1
    assert result[0].journal_name == "测试期刊"


@pytest.mark.parametrize("check_path", ["/mp/urlcheck?", "/mp/urlcheck?action=urlcheck&",
                                        "/mp/profile_ext?action=urlcheck&"])
@pytest.mark.parametrize("format_", ["url", "request", "tsv"])
def test_both_urlcheck_paths_keep_home_identity(api, check_path, format_):
    check = "https://mp.weixin.qq.com" + check_path + "uin=TEST_UIN&key=TEST_KEY&__biz=test-biz"
    if format_ == "request":
        text = request(check) + request(HOME)
    elif format_ == "tsv":
        text = "URL\tHost\tProtocol\n" + "\n".join(
            url + "\tmp.weixin.qq.com\tHTTPS" for url in (check, HOME))
    else:
        text = check + "\n" + HOME
    result = api.read_request_input(text, expected_biz="test-biz")
    assert result.params["action"] == "home"
    assert result.biz == "test-biz" and result.params["pass_ticket"] == "TEST_TICKET"


@pytest.mark.parametrize("path,action", [
    ("/mp/profile_ext", "getmsg"), ("/mp/profile_ext", ""),
    ("/mp/profile_ext", "URLCHECK"), ("/mp/urlcheck", "home"),
    ("/mp/urlcheck", "getmsg"), ("/mp/urlcheck", ""), ("/mp/urlcheck", "URLCHECK"),
])
def test_urlcheck_rejects_unapproved_actions(api, path, action):
    check = f"https://mp.weixin.qq.com{path}?action={action}&uin=TEST_UIN&key=TEST_KEY"
    with pytest.raises(ValueError):
        api.read_request_input(HOME + "\n" + check)


@pytest.mark.parametrize("change", ["biz", "key", "uin", "header", "unassociated", "alone", "duplicate"])
def test_profile_urlcheck_preserves_pairing_rejections(api, change):
    check = CHECK.replace("/mp/urlcheck?", "/mp/profile_ext?action=urlcheck&")
    home = request()
    if change == "biz": check += "&__biz=other-biz"
    if change == "key": check = check.replace("TEST_KEY", "OTHER_KEY")
    if change == "uin": check = check.replace("TEST_UIN", "OTHER_UIN")
    if change == "unassociated": check = check.replace("&key=TEST_KEY", "")
    supplement = request(check)
    if change == "header": supplement = supplement.replace("TEST_COOKIE", "OTHER_COOKIE")
    if change == "alone": home = ""
    if change == "duplicate": supplement += request(CHECK)
    with pytest.raises(ValueError):
        api.read_request_input(home + supplement)
