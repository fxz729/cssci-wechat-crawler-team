from pathlib import Path
import tempfile
from metrics.headers_parser import parse_headers_file

SEP = "------------------------------------------------------------------"

PROFILE_BLOCK = """\
GET https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=TestBiz123==&scene=124&uin=MyUin%3D%3D&key=mykey123&pass_ticket=myticket%2F1 HTTP/1.1
Host: mp.weixin.qq.com
User-Agent: Mozilla/5.0 TestAgent
Cookie: appmsg_token=oldtoken; wxuin=999


HTTP/1.1 200 OK
Content-Type: text/html; charset=UTF-8

"""

ARTICLE_BLOCK = """\
GET https://mp.weixin.qq.com/s?__biz=TestBiz123==&mid=111&idx=2&sn=abc123 HTTP/1.1
Host: mp.weixin.qq.com
User-Agent: Mozilla/5.0 TestAgent
Cookie: appmsg_token=oldtoken


HTTP/1.1 200 OK
Content-Type: text/html; charset=UTF-8
Set-Cookie: appmsg_token=newtoken_fresh; Path=/

"""


def _write_tmp(content: str) -> Path:
    f = tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".txt", delete=False)
    f.write(content)
    f.close()
    return Path(f.name)


def test_parse_headers_file_extracts_biz_credentials():
    p = _write_tmp(f"{PROFILE_BLOCK}\n{SEP}\n{ARTICLE_BLOCK}\n")
    result = parse_headers_file(p)
    assert "TestBiz123==" in result
    cred = result["TestBiz123=="]
    assert cred.biz == "TestBiz123=="
    assert cred.uin == "MyUin=="
    assert cred.key == "mykey123"
    assert cred.pass_ticket == "myticket/1"


def test_parse_headers_file_picks_latest_credentials_per_biz():
    profile2 = PROFILE_BLOCK.replace("mykey123", "newkey999").replace("myticket%2F1", "newticket%2F2")
    article2 = ARTICLE_BLOCK.replace("newtoken_fresh", "newtoken_second")
    content = f"{PROFILE_BLOCK}\n{SEP}\n{ARTICLE_BLOCK}\n{SEP}\n{profile2}\n{SEP}\n{article2}\n"
    p = _write_tmp(content)
    result = parse_headers_file(p)
    cred = result["TestBiz123=="]
    assert cred.key == "newkey999"
    assert cred.pass_ticket == "newticket/2"
    assert cred.appmsg_token == "newtoken_second"


def test_parse_headers_file_extracts_refreshed_appmsg_token_from_article_response():
    p = _write_tmp(f"{PROFILE_BLOCK}\n{SEP}\n{ARTICLE_BLOCK}\n")
    result = parse_headers_file(p)
    cred = result["TestBiz123=="]
    assert cred.appmsg_token == "newtoken_fresh"
    assert cred.sample_article_mid == "111"
    assert cred.sample_article_idx == 2
    assert cred.sample_article_sn == "abc123"
