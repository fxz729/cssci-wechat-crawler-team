"""Offline media-download contract tests; sockets and requests are blocked by default."""

import base64
import hashlib
import importlib
import importlib.util
import json
import socket
from pathlib import Path
from unittest.mock import Mock

import pytest
import requests


PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jK1sAAAAASUVORK5CYII="
)
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"\xff\xfb\x90\x00" + bytes(96)
MP4 = b"\x00\x00\x00\x18ftypmp42\x00\x00\x00\x00mp42isom" + bytes(32)
URL = "https://cdn.example.test/original?token=private-value&fmt=png"
MANIFEST_FIELDS = {"kind", "source_url", "local_path", "bytes", "sha256", "status", "error_code"}


class FakeResponse:
    def __init__(self, body=PNG, content_type="image/png", status=200, headers=None, chunks=None):
        self.status_code = status
        self.headers = {"Content-Type": content_type, "Content-Length": str(len(body))}
        self.headers.update(headers or {})
        self.chunks = chunks if chunks is not None else [body]
        self.closed = False

    def iter_content(self, chunk_size):
        assert 0 < chunk_size <= 1024 * 1024
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True

    @property
    def content(self):
        raise AssertionError("Must stream, not buffer response.content")

    @property
    def text(self):
        raise AssertionError("Must not decode/re-encode original media")


class FakeSession:
    def __init__(self, *responses):
        self.get = Mock(side_effect=responses)

    def __getattr__(self, name):
        raise AssertionError(f"Session configuration must not be inspected: {name}")


def forbidden_network(*args, **kwargs):
    raise AssertionError("Real networking is forbidden in these tests")


def public_dns(host, port, *args, **kwargs):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", public_dns)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden_network)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)


@pytest.fixture
def media():
    return importlib.import_module("crawl.media_download")


def test_public_entrypoint_exists():
    assert importlib.util.find_spec("crawl.media_download") is not None, "Missing media download module"
    assert callable(importlib.import_module("crawl.media_download").download_media_assets)


def assert_saved(row, root, body, kind, suffix):
    assert set(row) == MANIFEST_FIELDS
    assert row["kind"] == kind
    assert row["status"] == "downloaded"
    assert row["error_code"] == ""
    relative = Path(row["local_path"])
    assert not relative.is_absolute()
    assert ".." not in relative.parts
    assert relative.parts[0] == {"image": "images", "audio": "audio", "video": "video"}[kind]
    assert relative.suffix == suffix
    assert (root / relative).read_bytes() == body
    assert row["bytes"] == len(body)
    assert row["sha256"] == hashlib.sha256(body).hexdigest()
    assert not list(root.rglob("*.part"))


@pytest.mark.parametrize("kind,body,mime,suffix", [
    ("image", PNG, "image/png", ".png"),
    ("image", b"GIF89a" + bytes(64), "image/gif", ".gif"),
    ("image", b"<svg xmlns=\"http://www.w3.org/2000/svg\"><path/></svg>", "image/svg+xml", ".svg"),
    ("audio", MP3, "audio/mpeg", ".mp3"),
    ("audio", b"#!AMR\n" + bytes(64), "audio/amr", ".amr"),
    ("audio", b"\x02#!SILK_V3" + bytes(64), "application/octet-stream", ".silk"),
    ("video", MP4, "video/mp4", ".mp4"),
    ("video", b"\x1a\x45\xdf\xa3" + bytes(64), "video/webm", ".webm"),
])
def test_streams_original_bytes_by_kind(media, tmp_path, kind, body, mime, suffix):
    response = FakeResponse(body, mime, chunks=[body[:1], b"", body[1:8], body[8:]])
    session = FakeSession(response)
    row, = media.download_media_assets([{"kind": kind, "url": URL}], tmp_path, session=session)
    assert_saved(row, tmp_path, body, kind, suffix)
    assert response.closed
    assert session.get.call_args.args == (URL,)
    assert session.get.call_args.kwargs == {
        "stream": True, "allow_redirects": False, "timeout": (10, 60),
    }


def test_accepts_parser_assets_and_leaves_them_unchanged(media, tmp_path):
    from parsers.article_parser import parse_article_html

    parsed = parse_article_html('''<div id="js_content">
        <img data-src="https://mmbiz.qpic.cn/content/0?wx_fmt=png">
        <audio><source src="https://cdn.example.test/original.mp3"></audio>
        <mpvideo vid="only-an-id"></mpvideo>
        <iframe class="video_iframe" src="https://v.qq.com/iframe/player.html?vid=abc"></iframe>
    </div>''')
    before = json.dumps(parsed.media_assets, sort_keys=True)
    session = FakeSession(FakeResponse(), FakeResponse(MP3, "audio/mpeg"))
    rows = media.download_media_assets(parsed.media_assets, tmp_path, session=session)
    assert [row["status"] for row in rows] == ["downloaded", "downloaded", "needs_resolution", "needs_resolution"]
    assert json.dumps(parsed.media_assets, sort_keys=True) == before
    assert session.get.call_count == 2


def test_empty_assets_do_not_create_directory_or_request(media, tmp_path):
    session = FakeSession()
    root = tmp_path / "not-created"
    assert media.download_media_assets([], root, session=session) == []
    assert not root.exists()
    session.get.assert_not_called()


def test_defaults_to_plain_requests_and_does_not_change_parameters(media, tmp_path, monkeypatch):
    get = Mock(return_value=FakeResponse())
    monkeypatch.setattr(requests, "get", get)
    url = URL + "&signature=%2F%2B%3D&repeat=1&repeat=2"
    row, = media.download_media_assets([{"kind": "image", "url": url}], tmp_path, timeout=(2, 5))
    assert row["status"] == "downloaded"
    assert get.call_args.args == (url,)
    assert get.call_args.kwargs["timeout"] == (2, 5)


def test_duplicate_url_is_rehashed_and_skipped_across_calls(media, tmp_path):
    session = FakeSession(FakeResponse())
    asset = {"kind": "image", "url": URL}
    first, duplicate = media.download_media_assets([asset, asset], tmp_path, session=session)
    again, = media.download_media_assets([asset], tmp_path, session=session)
    assert first["status"] == "downloaded"
    assert duplicate == dict(first, status="skipped")
    assert again == duplicate
    assert session.get.call_count == 1


def test_filenames_are_stable_and_do_not_use_untrusted_path_names(media, tmp_path):
    assets = [
        {"kind": "image", "url": "https://cdn.example.test/../../CON?x=1"},
        {"kind": "image", "url": "https://cdn.example.test/../../CON?x=2"},
    ]
    first = media.download_media_assets(assets, tmp_path / "one", session=FakeSession(FakeResponse(), FakeResponse()))
    second = media.download_media_assets(list(reversed(assets)), tmp_path / "two", session=FakeSession(FakeResponse(), FakeResponse()))
    assert [row["local_path"] for row in first] == [row["local_path"] for row in reversed(second)]
    assert first[0]["local_path"] != first[1]["local_path"]
    assert all("CON" not in row["local_path"] for row in first)


def test_redacts_all_source_paths_queries_fragments_and_userinfo(media, tmp_path):
    url = "https://cdn.example.test/opaque-secret/file.png?custom_key=query-secret#fragment-secret"
    good, bad = media.download_media_assets([
        {"kind": "image", "url": url},
        {"kind": "image", "url": "https://username:password@cdn.example.test/secret"},
    ], tmp_path, session=FakeSession(FakeResponse()))
    assert good["source_url"].startswith("https://cdn.example.test/")
    assert bad["status"] == "error"
    output = json.dumps([good, bad])
    for secret in ("opaque-secret", "query-secret", "fragment-secret", "username", "password"):
        assert secret not in output


@pytest.mark.parametrize("asset,code", [
    ({"kind": "audio", "id": "voice-only"}, "missing_direct_url"),
    ({"kind": "video", "id": "vid-only"}, "missing_direct_url"),
    ({"kind": "video"}, "missing_direct_url"),
    ({"kind": "video", "url": "https://v.qq.com/iframe/player.html?vid=a"}, "player_url"),
    ({"kind": "video", "url": "https://mp.weixin.qq.com/mp/readtemplate?t=pages/video_player_tmpl&vid=a"}, "player_url"),
    ({"kind": "video", "url": "https://cdn.example.test/live.m3u8"}, "playlist_requires_resolution"),
    ({"kind": "video", "url": "https://cdn.example.test/manifest.mpd"}, "playlist_requires_resolution"),
])
def test_unresolved_sources_are_not_fabricated_or_requested(media, tmp_path, asset, code):
    session = FakeSession()
    row, = media.download_media_assets([asset], tmp_path, session=session)
    assert row["status"] == "needs_resolution"
    assert row["error_code"] == code
    assert row["local_path"] == ""
    assert row["bytes"] == 0
    assert row["sha256"] == ""
    session.get.assert_not_called()


@pytest.mark.parametrize("url", [
    "http://localhost/file.png", "http://localhost./file.png", "http://a.localhost/file.png",
    "http://service.local/file.png", "http://127.0.0.1/file.png", "http://10.0.0.1/file.png",
    "http://172.16.0.1/file.png", "http://192.168.1.1/file.png", "http://169.254.169.254/metadata",
    "http://0.0.0.0/file.png", "http://100.64.0.1/file.png", "http://224.0.0.1/file.png",
    "http://[::1]/file.png", "http://[fc00::1]/file.png", "http://[fe80::1]/file.png",
    "http://[::ffff:127.0.0.1]/file.png", "http://[ff02::1]/file.png",
])
def test_blocks_nonpublic_literals_and_local_names_before_request(media, tmp_path, url):
    session = FakeSession()
    row, = media.download_media_assets([{"kind": "image", "url": url}], tmp_path, session=session)
    assert row["status"] == "error"
    assert row["error_code"] == "blocked_address"
    session.get.assert_not_called()


@pytest.mark.parametrize("url", [
    "file:///secret.png", "ftp://cdn.example.test/x.png", "data:image/png;base64,abc",
    "//cdn.example.test/file.png", "/local.png", "https://cdn.example.test:bad/file",
    "https://cdn.example.test\\@localhost/file.png", "https://cdn.example.test/x\nfile.png",
    "https://[broken", "https://user:pass@cdn.example.test/x.png",
])
def test_invalid_urls_fail_closed(media, tmp_path, url):
    session = FakeSession()
    row, = media.download_media_assets([{"kind": "image", "url": url}], tmp_path, session=session)
    assert row["status"] == "error"
    assert row["error_code"] == "invalid_url"
    session.get.assert_not_called()


@pytest.mark.parametrize("addresses", [["10.1.2.3"], ["93.184.216.34", "::1"], ["127.0.0.1"]])
def test_blocks_dns_private_results_including_mixed_results(media, tmp_path, monkeypatch, addresses):
    def resolve(host, port, *args, **kwargs):
        return [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port)) for ip in addresses]

    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    session = FakeSession()
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["error_code"] == "blocked_address"
    session.get.assert_not_called()


def test_dns_failure_is_sanitized_and_does_not_request(media, tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(side_effect=socket.gaierror("private-dns-detail")))
    session = FakeSession()
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["error_code"] == "dns_error"
    assert "private-dns-detail" not in json.dumps(row)
    session.get.assert_not_called()


def test_redirects_are_manual_validated_and_closed(media, tmp_path):
    redirect = FakeResponse(b"", status=302, headers={"Location": "/final?signature=unchanged"})
    final = FakeResponse()
    session = FakeSession(redirect, final)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["status"] == "downloaded"
    assert [call.args[0] for call in session.get.call_args_list] == [URL, "https://cdn.example.test/final?signature=unchanged"]
    assert all(call.kwargs["allow_redirects"] is False for call in session.get.call_args_list)
    assert redirect.closed and final.closed


@pytest.mark.parametrize("location,code", [
    ("http://127.0.0.1/x.png", "blocked_address"),
    ("http://[::1]/x.png", "blocked_address"),
    ("https://user:secret@cdn.example.test/x.png", "invalid_url"),
    ("https://v.qq.com/iframe/player.html?vid=a", "player_url"),
])
def test_redirect_cannot_escape_address_or_resolution_checks(media, tmp_path, location, code):
    response = FakeResponse(b"", status=302, headers={"Location": location})
    session = FakeSession(response)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["error_code"] == code
    assert row["status"] != "downloaded"
    assert session.get.call_count == 1
    assert response.closed


def test_redirect_loop_is_bounded(media, tmp_path):
    responses = [FakeResponse(b"", status=302, headers={"Location": URL}) for _ in range(8)]
    session = FakeSession(*responses)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["error_code"] == "too_many_redirects"
    assert session.get.call_count <= 6
    assert all(response.closed for response in responses[:session.get.call_count])


@pytest.mark.parametrize("kind,mime,body,status,code", [
    ("image", "text/html", b"<html>authentication failed</html>", "error", "not_media"),
    ("image", "image/png", b"\xef\xbb\xbf  <!DOCTYPE html><html>error</html>", "error", "not_media"),
    ("image", "application/octet-stream", b"{\"error\":\"expired\"}", "error", "unsupported_media"),
    ("audio", "text/html", b"<html>player</html>", "needs_resolution", "not_media"),
    ("video", "video/mp4", b"\n<!-- player -->\n<html></html>", "needs_resolution", "not_media"),
    ("video", "application/vnd.apple.mpegurl", b"#EXTM3U\n#EXT-X-VERSION:3", "needs_resolution", "playlist_requires_resolution"),
    ("video", "application/octet-stream", b"#EXTM3U\nsegment.ts", "needs_resolution", "playlist_requires_resolution"),
    ("image", "image/png", MP3, "error", "media_kind_mismatch"),
    ("image", "audio/mpeg", PNG, "error", "content_type_mismatch"),
    ("image", "image/png", b"", "error", "empty_download"),
])
def test_rejects_pages_playlists_and_mismatched_media(media, tmp_path, kind, mime, body, status, code):
    response = FakeResponse(body, mime)
    row, = media.download_media_assets([{"kind": kind, "url": URL}], tmp_path, session=FakeSession(response))
    assert row["status"] == status
    assert row["error_code"] == code
    assert row["local_path"] == "" and row["sha256"] == "" and row["bytes"] == 0
    assert response.closed
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


@pytest.mark.parametrize("status", [204, 206, 401, 403, 404, 429, 500, 503])
def test_http_errors_and_partial_responses_never_become_files(media, tmp_path, status):
    response = FakeResponse(status=status)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=FakeSession(response))
    assert row["status"] == "error"
    assert row["error_code"] == f"http_{status}"
    assert row["local_path"] == ""
    assert response.closed
    assert not list(tmp_path.rglob("*.part"))


def test_missing_redirect_location_is_an_error(media, tmp_path):
    response = FakeResponse(b"", status=302)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=FakeSession(response))
    assert row["error_code"] == "invalid_redirect"
    assert response.closed


def test_every_redirect_hop_revalidates_dns(media, tmp_path, monkeypatch):
    resolve = Mock(side_effect=[
        public_dns("cdn.example.test", 443),
        [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.0.0.2", 443))],
    ])
    monkeypatch.setattr(socket, "getaddrinfo", resolve)
    response = FakeResponse(b"", status=302, headers={"Location": "/next"})
    session = FakeSession(response)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["error_code"] == "blocked_address"
    assert session.get.call_count == 1
    assert resolve.call_count == 2


@pytest.mark.parametrize("headers,code", [
    ({"Content-Length": "1000"}, "incomplete_download"),
    ({"Content-Length": "5"}, "incomplete_download"),
    ({"Content-Length": "not-a-number"}, "invalid_content_length"),
    ({"Content-Length": "-1"}, "invalid_content_length"),
])
def test_content_length_must_match_unencoded_media(media, tmp_path, headers, code):
    response = FakeResponse(headers=headers)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=FakeSession(response))
    assert row["error_code"] == code
    assert not [path for path in tmp_path.rglob("*") if path.is_file()]


def test_transfer_decoding_is_not_confused_with_transcoding(media, tmp_path):
    response = FakeResponse(headers={"Content-Encoding": "gzip", "Content-Length": "123"})
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=FakeSession(response))
    assert_saved(row, tmp_path, PNG, "image", ".png")


@pytest.mark.parametrize("length_header", [True, False])
def test_size_limit_checks_both_headers_and_streamed_bytes(media, tmp_path, length_header):
    response = FakeResponse(chunks=[PNG[:20], PNG[20:]])
    if not length_header:
        response.headers.pop("Content-Length")
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=FakeSession(response), max_bytes=32)
    assert row["error_code"] == "size_limit"
    assert not list(tmp_path.rglob("*.part"))


def test_stream_failure_isolated_and_does_not_delete_other_files(media, tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    unrelated = folder / "another-worker.part"
    unrelated.write_bytes(b"another worker's bytes")
    broken = FakeResponse(chunks=[PNG[:12], requests.ConnectionError("secret-url?token=private")])
    valid = FakeResponse()
    rows = media.download_media_assets([
        {"kind": "image", "url": URL}, {"kind": "image", "url": URL + "&other=1"},
    ], tmp_path, session=FakeSession(broken, valid))
    assert rows[0]["error_code"] == "network_error"
    assert rows[1]["status"] == "downloaded"
    assert "secret-url" not in json.dumps(rows)
    assert unrelated.read_bytes() == b"another worker's bytes"
    assert list(folder.glob("*.part")) == [unrelated]
    assert broken.closed and valid.closed


def test_request_exception_is_sanitized_and_next_asset_runs(media, tmp_path):
    session = FakeSession(requests.Timeout("Cookie: private-value; Authorization: confidential"), FakeResponse())
    rows = media.download_media_assets([
        {"kind": "image", "url": URL}, {"kind": "image", "url": URL + "&next=1"},
    ], tmp_path, session=session)
    assert [row["status"] for row in rows] == ["error", "downloaded"]
    assert rows[0]["error_code"] == "network_error"
    assert "private-value" not in json.dumps(rows) and "confidential" not in json.dumps(rows)


def test_hash_cache_corruption_requires_redownload_and_failure_keeps_file(media, tmp_path):
    asset = {"kind": "image", "url": URL}
    first, = media.download_media_assets([asset], tmp_path, session=FakeSession(FakeResponse()))
    path = tmp_path / first["local_path"]
    path.write_bytes(b"corrupted original")
    failed, = media.download_media_assets([asset], tmp_path, session=FakeSession(requests.Timeout("offline")))
    assert failed["status"] == "error"
    assert path.read_bytes() == b"corrupted original"
    restored, = media.download_media_assets([asset], tmp_path, session=FakeSession(FakeResponse()))
    assert restored == first
    assert path.read_bytes() == PNG


def test_verified_cache_does_not_need_dns_or_network(media, tmp_path, monkeypatch):
    asset = {"kind": "image", "url": URL}
    first, = media.download_media_assets([asset], tmp_path, session=FakeSession(FakeResponse()))
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    session = FakeSession()
    cached, = media.download_media_assets([asset], tmp_path, session=session)
    assert cached == dict(first, status="skipped")
    session.get.assert_not_called()


def test_file_is_complete_in_part_before_atomic_rename(media, tmp_path, monkeypatch):
    original_replace = media.os.replace
    observations = []

    def check_replace(source, target):
        source, target = Path(source), Path(target)
        assert source.suffix == ".part"
        assert source.parent == target.parent == tmp_path / "images"
        assert source.read_bytes() == PNG
        assert not target.exists()
        observations.append((source, target))
        return original_replace(source, target)

    monkeypatch.setattr(media.os, "replace", check_replace)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=FakeSession(FakeResponse()))
    assert row["status"] == "downloaded"
    assert len(observations) == 1
    assert not observations[0][0].exists()


def test_atomic_rename_failure_cleans_only_own_part(media, tmp_path, monkeypatch):
    monkeypatch.setattr(media.os, "replace", Mock(side_effect=PermissionError("sensitive-path")))
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=FakeSession(FakeResponse()))
    assert row["error_code"] == "io_error"
    assert "sensitive-path" not in json.dumps(row)
    assert not list(tmp_path.rglob("*.part"))


def test_media_directory_must_resolve_inside_article_directory(media, tmp_path, monkeypatch):
    original_resolve = Path.resolve
    outside = tmp_path.parent / "outside-test-directory"

    def redirected_resolve(path, *args, **kwargs):
        if path == tmp_path / "images":
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    session = FakeSession()
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["error_code"] == "unsafe_path"
    assert not (tmp_path / "images").exists()
    session.get.assert_not_called()


def test_existing_cache_symlink_is_never_followed(media, tmp_path, monkeypatch):
    asset = {"kind": "image", "url": URL}
    first, = media.download_media_assets([asset], tmp_path, session=FakeSession(FakeResponse()))
    cached = tmp_path / first["local_path"]
    original_resolve = Path.resolve

    def redirected_resolve(path, *args, **kwargs):
        if path == cached:
            return tmp_path.parent / "outside-sensitive-file"
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", redirected_resolve)
    session = FakeSession()
    row, = media.download_media_assets([asset], tmp_path, session=session)
    assert row["error_code"] == "unsafe_path"
    session.get.assert_not_called()


def test_malformed_assets_do_not_stop_valid_resources(media, tmp_path):
    rows = media.download_media_assets([
        None, {"kind": "../../bad", "url": URL}, {"kind": "image", "url": 123},
        {"kind": "image", "url": URL},
    ], tmp_path, session=FakeSession(FakeResponse()))
    assert [row["status"] for row in rows] == ["error", "error", "error", "downloaded"]
    assert [row["error_code"] for row in rows[:3]] == ["invalid_asset", "invalid_kind", "invalid_url"]


def test_same_url_in_different_kinds_cannot_reuse_wrong_media(media, tmp_path):
    rows = media.download_media_assets([
        {"kind": "image", "url": URL}, {"kind": "audio", "url": URL},
    ], tmp_path, session=FakeSession(FakeResponse(), FakeResponse()))
    assert rows[0]["status"] == "downloaded"
    assert rows[1]["error_code"] == "media_kind_mismatch"
    assert (tmp_path / rows[0]["local_path"]).read_bytes() == PNG


def test_audio_mp4_cache_is_reusable_without_response_mime(media, tmp_path):
    asset = {"kind": "audio", "url": URL}
    session = FakeSession(FakeResponse(MP4, "audio/mp4"))
    first, = media.download_media_assets([asset], tmp_path, session=session)
    cached, = media.download_media_assets([asset], tmp_path, session=session)
    assert_saved(first, tmp_path, MP4, "audio", ".m4a")
    assert cached == dict(first, status="skipped")
    assert session.get.call_count == 1


def test_cross_origin_redirect_never_reuses_caller_session(media, tmp_path):
    # A caller session may carry default auth headers: do not inspect, copy or
    # override those settings, and do not forward the session to another origin.
    redirect = FakeResponse(b"", status=302, headers={"Location": "https://other.example.test/file.png"})
    session = FakeSession(redirect, FakeResponse())
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["error_code"] == "cross_origin_redirect"
    assert session.get.call_count == 1
    assert redirect.closed


def test_plain_requests_can_follow_valid_public_cross_origin_redirect(media, tmp_path, monkeypatch):
    redirect = FakeResponse(b"", status=302, headers={"Location": "https://other.example.test/file.png"})
    get = Mock(side_effect=[redirect, FakeResponse()])
    monkeypatch.setattr(requests, "get", get)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path)
    assert row["status"] == "downloaded"
    assert get.call_count == 2


def test_redirect_control_characters_are_not_silently_normalized(media, tmp_path):
    redirect = FakeResponse(b"", status=302, headers={"Location": "\thttps://cdn.example.test/file.png"})
    session = FakeSession(redirect, FakeResponse())
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["error_code"] == "invalid_url"
    assert session.get.call_count == 1


def test_https_redirect_downgrade_is_blocked_without_client_changes(media, tmp_path, monkeypatch):
    redirect = FakeResponse(b"", status=302, headers={"Location": "http://cdn.example.test/file.png"})
    get = Mock(side_effect=[redirect, FakeResponse()])
    monkeypatch.setattr(requests, "get", get)
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path)
    assert row["error_code"] == "insecure_redirect"
    assert get.call_count == 1


@pytest.mark.parametrize("url", ["http://127.1/x", "http://2130706433/x", "http://0x7f000001/x"])
def test_obscure_numeric_local_addresses_cannot_bypass_dns_check(media, tmp_path, monkeypatch, url):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80)),
    ]))
    session = FakeSession()
    row, = media.download_media_assets([{"kind": "image", "url": url}], tmp_path, session=session)
    assert row["error_code"] == "blocked_address"
    session.get.assert_not_called()


def test_public_literal_does_not_require_dns(media, tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    row, = media.download_media_assets([{"kind": "image", "url": "https://93.184.216.34/x"}], tmp_path, session=FakeSession(FakeResponse()))
    assert row["status"] == "downloaded"


def test_unverified_filename_does_not_count_as_verified_cache(media, tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    key = hashlib.sha256(URL.encode()).hexdigest()[:24]
    unverified = folder / (key + "-" + "0" * 64 + ".png")
    unverified.write_bytes(PNG)
    session = FakeSession(FakeResponse())
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["status"] == "downloaded"
    assert tmp_path / row["local_path"] != unverified
    assert unverified.read_bytes() == PNG
    assert session.get.call_count == 1


def test_fragment_does_not_change_cache_identity_or_reach_request(media, tmp_path):
    session = FakeSession(FakeResponse())
    rows = media.download_media_assets([
        {"kind": "image", "url": URL + "#one"}, {"kind": "image", "url": URL + "#two"},
    ], tmp_path, session=session)
    assert rows[1] == dict(rows[0], status="skipped")
    assert session.get.call_args.args == (URL,)


def test_concurrent_calls_use_distinct_part_files(media, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    barrier = Barrier(2)

    class SynchronizedResponse(FakeResponse):
        def iter_content(self, chunk_size):
            barrier.wait(timeout=5)
            assert len(list((tmp_path / "images").glob("*.part"))) == 2
            yield PNG
            barrier.wait(timeout=5)

    def download():
        return media.download_media_assets(
            [{"kind": "image", "url": URL}], tmp_path, session=FakeSession(SynchronizedResponse()),
        )[0]

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: download(), range(2)))
    assert results[0] == results[1]
    assert_saved(results[0], tmp_path, PNG, "image", ".png")


@pytest.mark.parametrize("kind,body,mime,suffix", [
    ("image", b"\xff\xd8\xff\xe0" + bytes(60), "image/jpeg", ".jpg"),
    ("image", b"RIFF" + bytes(4) + b"WEBP" + bytes(52), "image/webp", ".webp"),
    ("image", b"\x00\x00\x00\x18ftypavif" + bytes(52), "image/avif", ".avif"),
    ("image", PNG, "application/octet-stream", ".png"),
    ("image", PNG, "", ".png"),
    ("audio", b"RIFF" + bytes(4) + b"WAVE" + bytes(52), "audio/wav", ".wav"),
    ("audio", b"fLaC" + bytes(60), "audio/flac", ".flac"),
    ("audio", b"OggS" + bytes(24) + b"OpusHead" + bytes(28), "application/ogg", ".ogg"),
    ("audio", b"\xff\xf1\x50\x80" + bytes(60), "audio/aac", ".aac"),
    ("audio", MP4, "application/octet-stream", ".m4a"),
    ("audio", b"\x1a\x45\xdf\xa3" + bytes(60), "application/octet-stream", ".webm"),
    ("video", b"OggS" + bytes(24) + b"theora" + bytes(30), "video/ogg", ".ogv"),
])
def test_common_direct_containers_round_trip_and_reuse_cache(media, tmp_path, kind, body, mime, suffix):
    session = FakeSession(FakeResponse(body, mime))
    asset = {"kind": kind, "url": URL}
    first, cached = media.download_media_assets([asset, asset], tmp_path, session=session)
    assert_saved(first, tmp_path, body, kind, suffix)
    assert cached == dict(first, status="skipped")
    assert session.get.call_count == 1


def test_empty_dns_results_fail_closed(media, tmp_path, monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", Mock(return_value=[]))
    session = FakeSession()
    row, = media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session)
    assert row["error_code"] == "dns_error"
    session.get.assert_not_called()


@pytest.mark.parametrize("limit", [0, -1, True, "100"])
def test_invalid_size_budget_is_rejected_before_any_request(media, tmp_path, limit):
    session = FakeSession()
    with pytest.raises(ValueError, match="max_bytes"):
        media.download_media_assets([{"kind": "image", "url": URL}], tmp_path, session=session, max_bytes=limit)
    session.get.assert_not_called()


@pytest.mark.parametrize("stop_at", ["get", "stream"])
def test_traffic_stopped_propagates_and_preserves_completed_files(media, tmp_path, stop_at):
    from crawl.request_governor import TrafficStopped

    stopped = TrafficStopped("traffic stopped")
    interrupted = FakeResponse(chunks=[PNG[:8], stopped])
    session = FakeSession(FakeResponse(), stopped if stop_at == "get" else interrupted)
    assets = [{"kind": "image", "url": URL + suffix} for suffix in ("", "&second=1", "&third=1")]
    with pytest.raises(TrafficStopped) as exc:
        media.download_media_assets(assets, tmp_path, session=session)
    assert exc.value is stopped
    assert session.get.call_count == 2
    assert [path.read_bytes() for path in (tmp_path / "images").iterdir()] == [PNG]
    assert not list(tmp_path.rglob("*.part"))
    if stop_at == "stream":
        assert interrupted.closed


def test_ordinary_runtime_error_remains_a_download_error(media, tmp_path):
    session = FakeSession(RuntimeError("client failed"), FakeResponse())
    failed, saved = media.download_media_assets([
        {"kind": "image", "url": URL + "&first=1"}, {"kind": "image", "url": URL},
    ], tmp_path, session=session)
    assert failed["error_code"] == "download_error"
    assert_saved(saved, tmp_path, PNG, "image", ".png")
