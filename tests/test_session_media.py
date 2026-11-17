"""Offline tests for the single-article session media adapter."""
import copy
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import socket

import pytest
import requests

from crawl import bundle_media
from crawl.media_download import download_media_assets


IMAGE = "https://mmbiz.qpic.cn/body.png"
COVER = "https://mmbiz.qpic.cn/cover.png"
AUDIO = "https://res.wx.qq.com/audio.mp3"
VIDEO = "https://res.wx.qq.com/video.mp4"
PNG = b"\x89PNG\r\n\x1a\n" + b"original image" * 8
MP3 = b"ID3\x04\x00\x00\x00\x00\x00\x00" + b"original audio" * 8
MP4 = b"\x00\x00\x00\x18ftypisom" + b"\x00" * 12 + b"original video" * 8
FIELDS = {"article_key", "mid", "idx", "biz", "title", "kind", "url", "id",
          "local_path", "status", "bytes", "sha256", "error_code", "role", "resource_index", "asset_index"}


def load_media():
    assert importlib.util.find_spec("crawl.session_media") is not None, "Session media adapter is missing"
    return importlib.import_module("crawl.session_media")


def article(**extra):
    row = {"biz": "journal-biz", "mid": "123", "idx": 1, "title": "单刊正文",
           "media_assets": [], "local_html": "附件/123_1/article.html",
           "local_text": "附件/123_1/article.txt"}
    row.update(extra)
    return row


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Real network access is forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *args, **kwargs: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
    ])
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden)


@pytest.fixture
def fake_fetch(monkeypatch):
    payloads = {IMAGE: (PNG, "image/png"), COVER: (PNG, "image/png"),
                AUDIO: (MP3, "audio/mpeg"), VIDEO: (MP4, "video/mp4")}
    calls, requests_seen = [], []

    class Response:
        def __init__(self, payload, content_type):
            self.payload = payload
            self.status_code = 200
            self.headers = {"Content-Type": content_type, "Content-Length": str(len(payload))}

        def iter_content(self, chunk_size):
            yield self.payload

        def close(self):
            pass

    class Transport:
        def get(self, url, **kwargs):
            assert set(kwargs) == {"stream", "allow_redirects", "timeout"}
            assert kwargs["stream"] is True and kwargs["allow_redirects"] is False
            requests_seen.append(url)
            if url not in payloads:
                raise requests.Timeout("private-cookie-value must not be persisted")
            return Response(*payloads[url])

    def fetch(asset, article_dir):
        assert set(asset) <= {"kind", "url", "id"}
        calls.append((dict(asset), Path(article_dir)))
        record, = download_media_assets([asset], article_dir, session=Transport())
        # Transport diagnostics and unknown fields must not reach the manifest.
        record["headers"] = {"Cookie": "private-cookie-value"}
        return record, [{"Authorization": "private-auth-value"}]

    monkeypatch.setattr(bundle_media, "fetch_asset", fetch)
    return calls, requests_seen, payloads


def test_cover_deduplicated_with_cover_url_alias(tmp_path, fake_fetch):
    media = load_media()
    result = media.download_article_media(article(
        cover_url=COVER, media_assets=[{"kind": "image", "url": COVER}]), tmp_path)
    assert result["status"] == "complete"
    assert len(result["rows"]) == len(fake_fetch[0]) == 1
    assert result["rows"][0]["role"] == "body_and_cover"
    assert result["rows"][0]["resource_index"] == 0


def test_json_assets_download_body_without_appending_cover(tmp_path, fake_fetch):
    media = load_media()
    assets = json.dumps([{"kind": "image", "url": IMAGE}])
    result = media.download_article_media(article(cover_image_url=COVER, media_assets=assets), tmp_path)
    assert [row["role"] for row in result["rows"]] == ["body"]
    assert [row["resource_index"] for row in result["rows"]] == [0]
    assert [call[0]["url"] for call in fake_fetch[0]] == [IMAGE]
    assert result["media_downloaded"] == 1


def test_body_image_is_root_relative_and_preserves_sources(tmp_path, fake_fetch):
    media = load_media()
    row = article(media_assets=[{"kind": "image", "url": IMAGE}])
    original = copy.deepcopy(row)
    folder = tmp_path / "附件" / "123_1"
    folder.mkdir(parents=True)
    html, text = folder / "article.html", folder / "article.txt"
    html.write_text("<p>正文不重写</p>", encoding="utf-8")
    text.write_text("正文不重写", encoding="utf-8")
    before = (html.read_bytes(), text.read_bytes())
    result = media.download_article_media(row, str(tmp_path))
    record, = result["rows"]
    assert result["status"] == "complete"
    assert record["local_path"].startswith("附件/123_1/images/")
    assert not Path(record["local_path"]).is_absolute()
    assert (tmp_path / record["local_path"]).read_bytes() == PNG
    assert record["bytes"] == len(PNG)
    assert record["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert (record["article_key"], record["mid"], record["idx"], record["biz"], record["title"]) == (
        "123_1", "123", 1, "journal-biz", "单刊正文")
    assert result["media_local_paths"] == result["image_local_paths"] == [record["local_path"]]
    assert result["media_pending"] == 0
    assert row == original and (html.read_bytes(), text.read_bytes()) == before
    assert {p.name for p in folder.iterdir()} == {"article.html", "article.txt", "images", "media_manifest.json"}


def test_only_image_failures_stay_pending_and_av_are_metadata_only(tmp_path, fake_fetch):
    media = load_media()
    assets = [{"kind": "image", "url": IMAGE},
              {"kind": "image", "url": "https://mmbiz.qpic.cn/unavailable.png"},
              {"kind": "image"}, {"kind": "audio", "id": "voice-id"},
              {"kind": "video", "id": "video-id"},
              {"kind": "video", "url": "https://v.qq.com/iframe/player.html?vid=player-id", "id": "player-id"},
              {"kind": "video", "url": "https://res.wx.qq.com/live.m3u8"}]
    result = media.download_article_media(article(media_assets=assets), tmp_path)
    assert result["status"] == "partial"
    assert result["media_downloaded"] == 1 and result["media_pending"] == 2
    assert [row["status"] for row in result["rows"]] == ["downloaded", "error", "needs_resolution"] + ["metadata_only"] * 4
    assert [row["id"] for row in result["rows"]][3:6] == ["voice-id", "video-id", "player-id"]
    for record in result["rows"][1:3]:
        assert record["error_code"] and not record["local_path"] and not record["sha256"]
        assert record["bytes"] == 0
    assert result["audio_local_paths"] == result["video_local_paths"] == []
    assert len(result["media_local_paths"]) == 1


def test_only_body_images_have_local_paths(tmp_path, fake_fetch):
    media = load_media()
    assets = [{"kind": kind, "url": url} for kind, url in [("image", IMAGE), ("audio", AUDIO), ("video", VIDEO)]]
    result = media.download_article_media(article(media_assets=assets), tmp_path)
    assert result["status"] == "complete" and result["media_downloaded"] == 1
    assert result["media_pending"] == 0
    assert fake_fetch[1] == [IMAGE]
    assert [call[0]["kind"] for call in fake_fetch[0]] == ["image"]
    assert result["media_local_paths"] == result["image_local_paths"]
    assert result["audio_local_paths"] == result["video_local_paths"] == []
    for record in result["rows"][1:]:
        assert record["status"] == "metadata_only"
        assert record["local_path"] == record["sha256"] == record["error_code"] == ""
        assert record["bytes"] == 0


def test_escaped_missing_and_unverified_success_paths_are_rejected(tmp_path, fake_fetch, monkeypatch):
    media = load_media()
    outside = tmp_path / "other.png"
    outside.write_bytes(PNG)
    paths = [str(outside), "../../other.png", "../other_article/image.png", "images/missing.png",
             "images/bad_hash.png", ""]

    def fetch(asset, folder):
        path = paths[int(asset["id"])]
        if path == "images/bad_hash.png":
            target = Path(folder) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"corrupted")
        return {"kind": "image", "source_url": "", "local_path": path, "status": "downloaded",
                "bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest(), "error_code": ""}, []

    monkeypatch.setattr(bundle_media, "fetch_asset", fetch)
    assets = [{"kind": "image", "url": IMAGE + "?case=" + str(i), "id": str(i)} for i in range(len(paths))]
    result = media.download_article_media(article(media_assets=assets), tmp_path)
    assert result["status"] == "partial" and result["media_pending"] == len(paths)
    assert result["media_downloaded"] == 0 and result["media_local_paths"] == []
    for record in result["rows"]:
        assert record["status"] == "error" and record["error_code"]
        assert record["local_path"] == record["sha256"] == "" and record["bytes"] == 0
    assert outside.read_bytes() == PNG


def test_invalid_article_ids_and_linked_directory_are_rejected_before_fetch(tmp_path, fake_fetch, monkeypatch):
    media = load_media()
    for invalid in ({"mid": "../../outside"}, {"idx": "1/../../outside"}):
        with pytest.raises(bundle_media.BundleError, match="invalid_article_directory"):
            media.download_article_media(article(**invalid), tmp_path)
    folder = tmp_path / "附件" / "123_1"
    real_is_symlink = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == folder or real_is_symlink(path))
    with pytest.raises(bundle_media.BundleError, match="unsafe_path"):
        media.download_article_media(article(media_assets=[{"kind": "image", "url": IMAGE}]), tmp_path)
    assert not fake_fetch[0] and not folder.exists()


def test_manifest_whitelist_is_readable_and_no_credentials_are_forwarded(tmp_path, fake_fetch):
    media = load_media()
    signed_url = IMAGE + "?token=private-token-value#private-fragment-value"
    fake_fetch[2][signed_url.split("#")[0]] = (PNG, "image/png")
    asset = {"kind": "image", "url": signed_url, "headers": {"Cookie": "private-cookie-value"},
             "token": "private-token-value"}
    row = article(media_assets=[asset], cookies={"session": "private-cookie-value"},
                  headers={"Authorization": "private-auth-value"}, token="private-token-value")
    original = copy.deepcopy(row)
    result = media.download_article_media(row, tmp_path)
    manifest = tmp_path / "附件" / "123_1" / "media_manifest.json"
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert saved == result["rows"] and all(set(record) == FIELDS for record in saved)
    assert result["status"] == "complete"
    assert saved[0]["url"] == IMAGE
    assert fake_fetch[0][0][0] == {"kind": "image", "url": signed_url}
    serialized = json.dumps(result) + manifest.read_text(encoding="utf-8")
    for secret in ("private-cookie-value", "private-auth-value", "private-token-value", "private-fragment-value"):
        assert secret not in serialized
    assert row == original


def test_existing_downloader_revalidates_cache_without_custom_resume(tmp_path, fake_fetch):
    media = load_media()
    row = article(media_assets=[{"kind": "image", "url": IMAGE}])
    first = media.download_article_media(row, tmp_path)
    again = media.download_article_media(row, tmp_path)
    assert first["rows"][0]["status"] == "downloaded"
    assert again["rows"][0]["status"] == "skipped"
    assert again["status"] == "complete" and again["media_downloaded"] == 1
    assert len(fake_fetch[0]) == 2 and fake_fetch[1] == [IMAGE]
    target = tmp_path / first["media_local_paths"][0]
    target.write_bytes(b"corrupted cache")
    repaired = media.download_article_media(row, tmp_path)
    assert repaired["rows"][0]["status"] == "downloaded" and target.read_bytes() == PNG
    assert fake_fetch[1] == [IMAGE, IMAGE]


def test_no_media_writes_empty_readable_manifest(tmp_path, fake_fetch):
    media = load_media()
    result = media.download_article_media(article(), tmp_path)
    assert result == {"status": "complete", "rows": [], "media_local_paths": [],
                      "image_local_paths": [], "audio_local_paths": [], "video_local_paths": [],
                      "media_downloaded": 0, "media_pending": 0}
    manifest = tmp_path / "附件" / "123_1" / "media_manifest.json"
    assert json.loads(manifest.read_text(encoding="utf-8")) == []
    assert not fake_fetch[0]


def test_legacy_video_is_preserved_but_not_counted_or_reused(tmp_path, monkeypatch):
    import hashlib,json
    from crawl.session_media import download_article_media
    root=tmp_path/'output';folder=root/'附件/123_1/video';folder.mkdir(parents=True)
    file=folder/'verified.mp4';file.write_bytes(b'verified-video')
    prior={'article_key':'123_1','biz':'biz','mid':'123','idx':1,'title':'video','kind':'video',
           'id':'wxv_123','url':'https://mpvideo.qpic.cn/#redacted','local_path':'附件/123_1/video/verified.mp4',
           'status':'downloaded','bytes':14,'sha256':hashlib.sha256(b'verified-video').hexdigest(),'error_code':'','role':'body','resource_index':0}
    (folder.parent/'media_manifest.json').write_text(json.dumps([prior]),encoding='utf-8')
    def no_network(*args):raise AssertionError('signed URL must not be needed again')
    monkeypatch.setattr('crawl.session_media.bundle_media.fetch_asset',no_network)
    result=download_article_media({'biz':'biz','mid':'123','idx':1,'title':'video','media_assets':[{'kind':'video','id':'wxv_123'}]},root)
    assert result['status']=='complete' and result['media_downloaded']==0 and result['media_pending']==0
    assert result['video_local_paths']==[]
    assert result['rows'][0]['status']=='metadata_only'
    assert result['rows'][0]['local_path']==''
    assert file.read_bytes()==b'verified-video'

@pytest.mark.parametrize("field", ["cover_url", "cover_image_url"])
def test_standalone_cover_is_not_a_download_task(tmp_path, fake_fetch, field):
    result = load_media().download_article_media(article(**{field: COVER}), tmp_path)
    assert result["rows"] == []
    assert result["media_downloaded"] == result["media_pending"] == 0
    assert not fake_fetch[0]


def test_duplicate_images_share_fetch_but_preserve_occurrences(tmp_path, fake_fetch):
    assets = [{"kind": "image", "url": IMAGE, "id": "first"},
              {"kind": "audio", "url": AUDIO, "id": "voice-id"},
              {"kind": "image", "url": IMAGE + "#second", "id": "second"}]
    result = load_media().download_article_media(article(media_assets=assets), tmp_path)
    assert len(fake_fetch[0]) == 1
    assert fake_fetch[1] == [IMAGE]
    assert [r["status"] for r in result["rows"]] == ["downloaded", "metadata_only", "skipped"]
    assert [r["resource_index"] for r in result["rows"]] == [0, 1, 2]
    assert [r["asset_index"] for r in result["rows"]] == [0, 1, 2]
    assert [r["id"] for r in result["rows"]] == ["first", "voice-id", "second"]
    assert result["rows"][0]["local_path"] == result["rows"][2]["local_path"]
    assert result["media_downloaded"] == 2 and result["media_pending"] == 0


def test_stop_between_images_keeps_remaining_positions_and_av_metadata(tmp_path, fake_fetch):
    assets = [{"kind": "image", "url": IMAGE}, {"kind": "audio", "url": AUDIO},
              {"kind": "image", "url": COVER}, {"kind": "video", "id": "wxv_1"},
              {"kind": "image", "url": IMAGE}]
    checks = []

    def should_stop():
        checks.append(True)
        return len(checks) == 2

    result = load_media().download_article_media(article(media_assets=assets), tmp_path, should_stop=should_stop)
    assert len(checks) == 2
    assert fake_fetch[1] == [IMAGE]
    assert [r["status"] for r in result["rows"]] == ["downloaded", "metadata_only", "pending", "metadata_only", "pending"]
    assert [r["asset_index"] for r in result["rows"]] == list(range(5))
    assert result["status"] == "partial"
    assert result["media_downloaded"] == 1 and result["media_pending"] == 2
    saved = json.loads((tmp_path / "附件/123_1/media_manifest.json").read_text(encoding="utf-8"))
    assert saved == result["rows"]
    resumed = load_media().download_article_media(article(media_assets=assets), tmp_path, should_stop=lambda: False)
    assert resumed["status"] == "complete" and resumed["media_downloaded"] == 3
    assert fake_fetch[1] == [IMAGE, COVER]


def test_stop_before_first_image_still_emits_metadata(tmp_path, fake_fetch):
    assets = [{"kind": "image", "url": IMAGE}, {"kind": "video", "url": VIDEO}]
    result = load_media().download_article_media(article(media_assets=assets), tmp_path, should_stop=lambda: True)
    assert not fake_fetch[0]
    assert result["media_pending"] == 1
    assert [r["status"] for r in result["rows"]] == ["pending", "metadata_only"]


@pytest.mark.parametrize("assets", ["", None, [], "[]"])
def test_empty_csv_and_list_assets_are_supported(tmp_path, fake_fetch, assets):
    result = load_media().download_article_media(article(media_assets=assets), tmp_path)
    assert result["status"] == "complete" and result["rows"] == []
    assert not fake_fetch[0]


@pytest.mark.parametrize("assets", ["not-json-secret", "{}", "null", "false", "42", {}, False, 0,
                                  (), [None], [{"kind": "unknown"}], [{"kind": "image", "url": []}],
                                  [{"kind": "video", "id": {"cookie": "secret"}}]])
def test_invalid_asset_structures_fail_closed_before_writing(tmp_path, fake_fetch, assets):
    with pytest.raises(bundle_media.BundleError, match="^invalid_media_assets$"):
        load_media().download_article_media(article(media_assets=assets), tmp_path)
    assert not fake_fetch[0]
    assert not (tmp_path / "附件").exists()


@pytest.mark.parametrize("url", [VIDEO + "?token=private-token-value#private-fragment-value",
                                "https://user:private-password@res.wx.qq.com/video.mp4",
                                "not-a-url", "https://[bad", ""])
def test_av_metadata_never_fetches_and_does_not_leak_credentials(tmp_path, fake_fetch, url):
    assets = [{"kind": "audio", "url": url, "id": "voice-id", "headers": {"Cookie": "private-cookie-value"}},
              {"kind": "video", "url": url, "id": "wxv_1", "token": "private-token-value"}]
    result = load_media().download_article_media(article(media_assets=assets), tmp_path)
    assert not fake_fetch[0]
    assert result["status"] == "complete"
    assert result["media_downloaded"] == result["media_pending"] == 0
    assert [r["id"] for r in result["rows"]] == ["voice-id", "wxv_1"]
    assert all(r["status"] == "metadata_only" and set(r) == FIELDS for r in result["rows"])
    if url.startswith(VIDEO):
        assert result["rows"][0]["url"] == VIDEO
        assert result["rows"][1]["url"] == ""
    serialized = json.dumps(result)
    for secret in ("private-password", "private-cookie-value", "private-token-value", "private-fragment-value"):
        assert secret not in serialized


def test_duplicate_reuse_rechecks_hash_before_claiming_success(tmp_path, fake_fetch):
    checks = []

    def should_stop():
        checks.append(True)
        if len(checks) == 2:
            target, = (tmp_path / "附件/123_1/images").glob("*")
            target.write_bytes(b"tampered")
        return False

    assets = [{"kind": "image", "url": IMAGE}] * 2
    result = load_media().download_article_media(article(media_assets=assets), tmp_path, should_stop=should_stop)
    second = result["rows"][1]
    if second["status"] in {"downloaded", "skipped"}:
        content = (tmp_path / second["local_path"]).read_bytes()
        assert hashlib.sha256(content).hexdigest() == second["sha256"]
    else:
        assert second["status"] == "error" and second["local_path"] == ""
        assert result["media_pending"] == 1


@pytest.mark.parametrize("old_path", [None, [], {}, 1])
def test_malformed_old_image_path_is_not_trusted(tmp_path, fake_fetch, old_path):
    folder = tmp_path / "附件/123_1"
    folder.mkdir(parents=True)
    prior = {"kind": "image", "id": "image-id", "status": "downloaded", "local_path": old_path,
             "bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}
    (folder / "media_manifest.json").write_text(json.dumps([prior]), encoding="utf-8")
    result = load_media().download_article_media(article(media_assets=[{"kind": "image", "id": "image-id"}]), tmp_path)
    assert result["media_downloaded"] == 0 and result["media_pending"] == 1
    assert result["rows"][0]["status"] == "needs_resolution"
    assert not fake_fetch[1]


def test_id_only_image_cache_is_hashed_and_keeps_position(tmp_path, fake_fetch):
    folder = tmp_path / "附件/123_1/images"
    folder.mkdir(parents=True)
    target = folder / "legacy.png"
    target.write_bytes(PNG)
    prior = {"kind": "image", "id": "image-id", "status": "downloaded",
             "local_path": "附件/123_1/images/legacy.png", "url": IMAGE + "?token=private-token-value",
             "bytes": len(PNG), "sha256": hashlib.sha256(PNG).hexdigest()}
    manifest = folder.parent / "media_manifest.json"
    manifest.write_text(json.dumps([prior]), encoding="utf-8")
    row = article(media_assets=[{"kind": "video", "id": "wxv_1"}, {"kind": "image", "id": "image-id"}])
    result = load_media().download_article_media(row, tmp_path)
    record = result["rows"][1]
    assert record["status"] == "skipped" and set(record) == FIELDS
    assert record["asset_index"] == record["resource_index"] == 1
    assert result["media_downloaded"] == 1 and not fake_fetch[0]
    assert "private-token-value" not in json.dumps(result)
    target.write_bytes(b"corrupted legacy image")
    result = load_media().download_article_media(row, tmp_path)
    assert result["media_downloaded"] == 0 and result["media_pending"] == 1
    assert target.read_bytes() == b"corrupted legacy image"


def test_unlisted_legacy_media_and_cover_are_never_removed(tmp_path, fake_fetch):
    old_files = {"images/cover.png": PNG, "audio/old.mp3": MP3, "video/old.mp4": b"old-corrupt-video"}
    for relative, content in old_files.items():
        target = tmp_path / "附件/123_1" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    result = load_media().download_article_media(article(cover_image_url=COVER,
        media_assets=[{"kind": "image", "url": IMAGE}]), tmp_path)
    assert result["media_downloaded"] == 1
    for relative, content in old_files.items():
        assert (tmp_path / "附件/123_1" / relative).read_bytes() == content


def test_av_metadata_requires_neither_dns_nor_stop_callback(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Metadata must not enter DNS, downloader or image pause hook")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(bundle_media, "fetch_asset", forbidden)
    result = load_media().download_article_media(article(media_assets=[
        {"kind": "audio", "url": AUDIO}, {"kind": "video", "url": VIDEO}]), tmp_path, should_stop=forbidden)
    assert result["status"] == "complete" and result["media_pending"] == 0


def test_real_bundle_entrypoint_keeps_tls_and_credentials_isolated(tmp_path, monkeypatch):
    calls = []

    class Response:
        status_code = 200
        headers = {"Content-Type": "image/png", "Content-Length": str(len(PNG))}

        def iter_content(self, chunk_size):
            yield PNG

        def close(self):
            pass

    def get(session, url, **kwargs):
        assert session.verify is True and session.trust_env is False
        assert session.auth is None and not session.cookies
        assert "Cookie" not in session.headers and "Authorization" not in session.headers
        assert kwargs == {"stream": True, "allow_redirects": False, "timeout": (10, 30)}
        calls.append(url)
        return Response()

    monkeypatch.setattr(requests.sessions.Session, "get", get)
    row = article(media_assets=[{"kind": "image", "url": IMAGE, "headers": {"Cookie": "secret"}},
                                {"kind": "audio", "url": AUDIO}, {"kind": "video", "url": VIDEO}],
                  cookies={"session": "secret"}, headers={"Authorization": "secret"})
    result = load_media().download_article_media(row, tmp_path)
    assert calls == [IMAGE] and result["media_downloaded"] == 1


@pytest.mark.parametrize("address", ["127.0.0.1", "10.0.0.1", "169.254.169.254"])
def test_real_bundle_entrypoint_still_blocks_ssrf(tmp_path, monkeypatch, address):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))])
    calls = []
    monkeypatch.setattr(requests.sessions.Session, "get", lambda *a, **kw: calls.append(a))
    result = load_media().download_article_media(article(media_assets=[{"kind": "image", "url": IMAGE}]), tmp_path)
    assert not calls
    assert result["rows"][0]["status"] == "error"
    assert result["rows"][0]["error_code"] == "blocked_address"
    assert result["media_pending"] == 1


def test_invalid_later_asset_prevents_any_earlier_image_fetch(tmp_path, fake_fetch):
    assets = [{"kind": "image", "url": IMAGE}, {"kind": "audio", "url": []}]
    with pytest.raises(bundle_media.BundleError, match="^invalid_media_assets$"):
        load_media().download_article_media(article(media_assets=json.dumps(assets)), tmp_path)
    assert not fake_fetch[0]


def test_non_public_id_is_not_copied_to_metadata(tmp_path, fake_fetch):
    result = load_media().download_article_media(article(media_assets=[
        {"kind": "video", "id": "https://example.test/?token=private-token-value"}]), tmp_path)
    assert result["rows"][0]["id"] == ""
    assert result["rows"][0]["status"] == "metadata_only"
    assert "private-token-value" not in json.dumps(result)


@pytest.mark.parametrize("kind,url", [("image", IMAGE), ("audio", AUDIO), ("video", VIDEO)])
def test_public_media_urls_remain_locatable_with_public_query(tmp_path, fake_fetch, kind, url):
    public = url + "?wx_fmt=png&scene=27&vid=public-id"
    fake_fetch[2][public] = (PNG, "image/png")
    result = load_media().download_article_media(article(media_assets=[{"kind": kind, "url": public}]), tmp_path)
    assert result["rows"][0]["url"] == public
    assert "/redacted/" not in result["rows"][0]["url"]


@pytest.mark.parametrize("query", ["ckey=private-signature", "signature=private-signature",
                                  "vkey=private-signature", "encfilekey=private-signature",
                                  "X-Amz-Signature=private-signature"])
def test_private_signed_video_exports_only_public_id(tmp_path, fake_fetch, query):
    url = "https://mpvideo.qpic.cn/video.mp4?vid=wxv_1&" + query
    result = load_media().download_article_media(article(media_assets=[{"kind": "video", "url": url, "id": "wxv_1"}]), tmp_path)
    record = result["rows"][0]
    assert record["id"] == "wxv_1" and record["url"] == ""
    assert record["status"] == "metadata_only" and not fake_fetch[0]
    assert "private-signature" not in json.dumps(result)


def test_private_query_removed_but_public_query_kept_for_images(tmp_path, fake_fetch):
    raw = IMAGE + "?wx_fmt=png&%74oken=private-token-value&scene=27&pass_ticket=private-ticket#fragment"
    fake_fetch[2][raw.split("#")[0]] = (PNG, "image/png")
    result = load_media().download_article_media(article(media_assets=[{"kind": "image", "url": raw}]), tmp_path)
    assert result["rows"][0]["url"] == IMAGE + "?wx_fmt=png&scene=27"
    assert fake_fetch[1] == [raw.split("#")[0]]
    assert "private-" not in json.dumps(result)


def test_different_private_download_queries_do_not_alias_public_cache_key(tmp_path, fake_fetch):
    first, second = IMAGE + "?token=first", IMAGE + "?token=second"
    fake_fetch[2][first] = (PNG, "image/png")
    fake_fetch[2][second] = (PNG + b"second", "image/png")
    result = load_media().download_article_media(article(media_assets=[
        {"kind": "image", "url": first}, {"kind": "image", "url": second}]), tmp_path)
    assert fake_fetch[1] == [first, second]
    assert [r["url"] for r in result["rows"]] == [IMAGE, IMAGE]
    assert result["rows"][0]["sha256"] != result["rows"][1]["sha256"]


@pytest.fixture
def requester_transport(monkeypatch):
    from unittest.mock import Mock

    response = Mock(status_code=200, headers={"Content-Type": "image/png"})
    response.iter_content.side_effect = lambda **kwargs: iter([PNG])
    ordinary_get = Mock(return_value=response)
    monkeypatch.setattr(requests.sessions.Session, "get", ordinary_get)
    return ordinary_get


def test_requester_skips_duplicate_images_and_verified_disk_cache(tmp_path, requester_transport):
    from unittest.mock import Mock

    def request(get, url, *, kind, **kwargs):
        assert get is requester_transport
        assert kind == "image"
        return get(url, **kwargs)
    requester = Mock(side_effect=request)
    row = article(media_assets=[{"kind": "image", "url": IMAGE},
                                {"kind": "image", "url": IMAGE + "#duplicate"},
                                {"kind": "audio", "url": AUDIO}])
    first = load_media().download_article_media(row, tmp_path, requester=requester)
    resumed = load_media().download_article_media(row, tmp_path, requester=requester)
    assert [r["status"] for r in first["rows"]] == ["downloaded", "skipped", "metadata_only"]
    assert [r["status"] for r in resumed["rows"]] == ["skipped", "skipped", "metadata_only"]
    requester.assert_called_once_with(requester_transport, IMAGE, kind="image",
                                    stream=True, allow_redirects=False, timeout=(10, 30))
    requester_transport.assert_called_once()


def test_requester_stop_propagates_and_completed_image_is_reusable(tmp_path, requester_transport):
    from unittest.mock import Mock
    from crawl.request_governor import TrafficStopped

    stopped = TrafficStopped("traffic stopped")
    def request(get, url, *, kind, **kwargs):
        if url == COVER:
            raise stopped
        return get(url, **kwargs)
    requester = Mock(side_effect=request)
    row = article(media_assets=[{"kind": "image", "url": url} for url in (IMAGE, COVER, IMAGE + "?third=1")])
    with pytest.raises(TrafficStopped) as exc:
        load_media().download_article_media(row, tmp_path, requester=requester)
    assert exc.value is stopped
    assert requester.call_count == 2
    requester_transport.assert_called_once()
    assert [p.read_bytes() for p in tmp_path.rglob("*.png")] == [PNG]
    assert not list(tmp_path.rglob("*.part"))
    cached = load_media().download_article_media(
        article(media_assets=[{"kind": "image", "url": IMAGE}]), tmp_path, requester=requester)
    assert cached["rows"][0]["status"] == "skipped"
    assert requester.call_count == 2


@pytest.mark.parametrize("existing_manifest", [False, True])
def test_requester_stop_atomically_saves_completed_and_pending_rows(
        tmp_path, requester_transport, monkeypatch, existing_manifest):
    from unittest.mock import Mock
    from crawl.request_governor import TrafficStopped

    folder = tmp_path / "附件" / "123_1"
    folder.mkdir(parents=True)
    legacy = folder / "existing.bin"
    legacy.write_bytes(b"keep existing file")
    manifest = folder / "media_manifest.json"
    old_manifest = "[]\n"
    if existing_manifest:
        manifest.write_text(old_manifest, encoding="utf-8")
    row = article(media_assets=[
        {"kind": "image", "url": IMAGE, "id": "image_1"},
        {"kind": "audio", "url": AUDIO, "id": "audio_1"},
        {"kind": "image", "url": COVER, "id": "image_2"},
        {"kind": "video", "url": VIDEO, "id": "video_1"},
        {"kind": "image", "url": IMAGE + "?third=1", "id": "image_3"},
    ])
    original = copy.deepcopy(row)
    expected_statuses = ["downloaded", "metadata_only", "pending", "metadata_only", "pending"]
    replacements = []
    real_replace = bundle_media.os.replace

    def replace(source, target):
        if Path(target) == manifest:
            assert Path(source).parent == manifest.parent
            assert Path(source) != manifest
            assert manifest.exists() == existing_manifest
            if existing_manifest:
                assert manifest.read_text(encoding="utf-8") == old_manifest
            saved = json.loads(Path(source).read_text(encoding="utf-8"))
            assert [r["status"] for r in saved] == expected_statuses
            replacements.append(saved)
        return real_replace(source, target)

    monkeypatch.setattr(bundle_media.os, "replace", replace)
    stopped = TrafficStopped("traffic stopped")

    def request(get, url, *, kind, **kwargs):
        assert kind == "image"
        if url == COVER:
            assert len(list(folder.rglob("*.png"))) == 1
            raise stopped
        return get(url, **kwargs)

    requester = Mock(side_effect=request)
    with pytest.raises(TrafficStopped) as exc:
        load_media().download_article_media(row, tmp_path, requester=requester)
    assert exc.value is stopped
    assert [call.args[1] for call in requester.call_args_list] == [IMAGE, COVER]
    requester_transport.assert_called_once()
    assert manifest.is_file()
    saved = json.loads(manifest.read_text(encoding="utf-8"))
    assert replacements == [saved]
    assert [r["status"] for r in saved] == expected_statuses
    assert [r["id"] for r in saved] == [a["id"] for a in row["media_assets"]]
    assert [r["url"] for r in saved] == [a["url"] for a in row["media_assets"]]
    assert [r["asset_index"] for r in saved] == list(range(5))
    assert [r["resource_index"] for r in saved] == list(range(5))
    assert (tmp_path / saved[0]["local_path"]).read_bytes() == PNG
    assert saved[0]["bytes"] == len(PNG)
    assert saved[0]["sha256"] == hashlib.sha256(PNG).hexdigest()
    for index in (2, 4):
        assert saved[index]["error_code"] == "stopped"
        assert saved[index]["local_path"] == saved[index]["sha256"] == ""
        assert saved[index]["bytes"] == 0
    assert legacy.read_bytes() == b"keep existing file"
    assert not list(folder.rglob("*.part"))
    assert row == original
