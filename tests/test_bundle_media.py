"""Offline contract and regression tests for the reusable bundle integration."""
import hashlib
import importlib
import importlib.util
import inspect
import json
from pathlib import Path
import socket
from unittest.mock import Mock

import pytest
import requests


URL = "https://mmbiz.qpic.cn/test/image?signature=opaque"
PNG = b"\x89PNG\r\n\x1a\n" + b"original-media" * 8


def forbidden_network(*args, **kwargs):
    raise AssertionError("Real networking is forbidden")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda host, port, *a, **kw: [
        (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))
    ])
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden_network)
    monkeypatch.setattr(requests.sessions.Session, "request", forbidden_network)


@pytest.fixture
def media():
    assert importlib.util.find_spec("crawl.bundle_media") is not None, "Reusable bundle module is missing"
    return importlib.import_module("crawl.bundle_media")


def article(root, key="123_1", assets=None, **extra):
    folder = root / "articles" / key
    folder.mkdir(parents=True)
    mid, idx = key.split("_")
    metadata = {"mid": mid, "idx": int(idx), "title": "Title <&>",
                "content_html": '<p>Body &amp; text</p><img data-src="' + URL + '">',
                "media_assets": assets if assets is not None else []}
    metadata.update(extra)
    (folder / "metadata.json").write_text(json.dumps(metadata), encoding="utf-8")
    (folder / "article.html").write_text("<p>Original HTML</p>", encoding="utf-8")
    (folder / "article.txt").write_text("Original text", encoding="utf-8")
    return folder


def read_json(path):
    return json.loads(path.read_text(encoding="utf-8"))


class Response:
    status_code = 200
    headers = {"Content-Type": "image/png", "Content-Length": str(len(PNG))}

    def __init__(self, chunks=None):
        self.chunks = [PNG] if chunks is None else chunks
        self.closed = False

    def iter_content(self, chunk_size):
        for chunk in self.chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


def fake_download(monkeypatch, responses):
    calls = Mock(side_effect=responses)

    def get(session, url, **kwargs):
        assert session.trust_env is False
        assert session.verify is True
        assert session.auth is None
        assert not session.cookies
        assert kwargs == {"stream": True, "allow_redirects": False, "timeout": (10, 30)}
        return calls(url)

    monkeypatch.setattr(requests.sessions.Session, "get", get)
    return calls


def test_entrypoint_requires_explicit_bundle_and_disables_seed(media):
    signature = inspect.signature(media.run_bundle)
    assert signature.parameters["workers"].default == 4
    assert signature.parameters["bundle_root"].kind == inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["bundle_root"].default is inspect.Parameter.empty
    assert signature.parameters["smoke_root"].kind == inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["smoke_root"].default is None
    with pytest.raises(TypeError):
        media.run_bundle()
    source = inspect.getsource(media)
    assert "journal_validation_20260906" not in source
    assert "sys.path" not in source
    assert "dont_write_bytecode" not in source
    assert not hasattr(media, "BUNDLE_ROOT")
    assert not hasattr(media, "SMOKE_ROOT")


@pytest.mark.parametrize("case", ["absent", "no_metadata", "empty_object", "empty_file", "empty_list"])
def test_empty_metadata_never_succeeds(media, tmp_path, case):
    root = tmp_path / "bundle"
    if case != "absent":
        folder = root / "articles" / "123_1"
        folder.mkdir(parents=True)
        if case != "no_metadata":
            text = {"empty_object": "{}", "empty_file": "", "empty_list": "[]"}[case]
            (folder / "metadata.json").write_text(text, encoding="utf-8")
    assert media.run_bundle(bundle_root=root) == 2
    report = read_json(root / "media_download_report.json")
    assert report["status"] == "partial"
    assert report["processed_article_count"] == 0
    assert report["snapshot_errors"]


def test_no_media_article_is_valid_and_seed_is_not_called(media, tmp_path, monkeypatch):
    article(tmp_path)
    seed = Mock(side_effect=AssertionError("Default must not inspect smoke"))
    monkeypatch.setattr(media, "seed_smoke", seed)
    assert media.run_bundle(bundle_root=tmp_path) == 0
    seed.assert_not_called()
    report = read_json(tmp_path / "media_download_report.json")
    assert report["status"] == "complete"
    assert report["http_request_count"] == report["smoke_seeded_files"] == 0


@pytest.mark.parametrize("asset,code", [
    ({"kind": "video", "url": ""}, "missing_direct_url"),
    ({"kind": "video", "url": "https://v.qq.com/iframe/player.html?vid=1"}, "player_url"),
    ({"kind": "video", "url": "https://cdn.example.test/video.m3u8"}, "playlist_requires_resolution"),
])
def test_unresolved_media_returns_two_and_keeps_player(media, tmp_path, asset, code):
    source = '<p>Body</p><iframe src="https://v.qq.com/iframe/player.html?vid=1"></iframe>'
    folder = article(tmp_path, assets=[asset], content_html=source)
    assert media.run_bundle(bundle_root=tmp_path) == 2
    row, = read_json(tmp_path / "media_rows.json")
    assert (row["status"], row["error_code"]) == ("needs_resolution", code)
    report = read_json(tmp_path / "media_download_report.json")
    assert report["status"] == report["articles"][0]["status"] == "partial"
    assert report["http_request_count"] == 0
    assert source in (folder / "article.local.html").read_text(encoding="utf-8")
    assert not (folder / "video").exists()


@pytest.mark.parametrize("url,code", [
    ("file:///private/image.png", "invalid_url"),
    ("https://user:secret@cdn.example.test/x.png", "invalid_url"),
    ("http://127.0.0.1/x.png", "blocked_address"),
])
def test_unsafe_media_is_rejected_without_requests(media, tmp_path, url, code):
    article(tmp_path, assets=[{"kind": "image", "url": url}])
    assert media.run_bundle(bundle_root=tmp_path) == 2
    row, = read_json(tmp_path / "media_rows.json")
    assert (row["status"], row["error_code"]) == ("error", code)
    assert read_json(tmp_path / "media_download_report.json")["http_request_count"] == 0
    assert "secret" not in (tmp_path / "media_rows.json").read_text(encoding="utf-8")


def test_hash_atomic_media_and_resume_preserve_sources(media, tmp_path, monkeypatch):
    folder = article(tmp_path, assets=[{"kind": "image", "url": URL}], cover_image_url=URL)
    originals = {p: p.read_bytes() for p in folder.iterdir()}
    response = Response()
    get = fake_download(monkeypatch, [response])
    assert media.run_bundle(bundle_root=tmp_path) == 0
    row, = read_json(tmp_path / "media_rows.json")
    assert (row["resource_index"], row["asset_index"], row["role"]) == (0, 0, "body_and_cover")
    target = tmp_path / row["local_path"]
    assert target.read_bytes() == PNG
    assert row["sha256"] == hashlib.sha256(PNG).hexdigest()
    assert row["bytes"] == len(PNG)
    assert row["status"] == "downloaded"
    assert response.closed
    html = (folder / "article.local.html").read_text(encoding="utf-8")
    assert target.relative_to(folder).as_posix() in html
    assert "Body &amp; text" in html
    assert not list(tmp_path.rglob("*.part"))
    assert media.run_bundle(bundle_root=tmp_path) == 0
    resumed, = read_json(tmp_path / "media_rows.json")
    assert resumed["status"] == "skipped"
    assert resumed["sha256"] == row["sha256"]
    assert get.call_count == 1
    assert read_json(tmp_path / "media_download_report.json")["http_request_count"] == 0
    for path, before in originals.items():
        assert path.read_bytes() == before
    # A corrupted cache must be downloaded again, not marked skipped.
    target.write_bytes(b"corrupt")
    fake_download(monkeypatch, [Response()])
    assert media.run_bundle(bundle_root=tmp_path) == 0
    assert target.read_bytes() == PNG
    assert read_json(tmp_path / "media_rows.json")[0]["status"] == "downloaded"


def test_interrupted_download_leaves_no_final_or_part_file(media, tmp_path, monkeypatch):
    folder = article(tmp_path, assets=[{"kind": "image", "url": URL}])
    response = Response([PNG[:12], requests.ConnectionError("private-detail")])
    fake_download(monkeypatch, [response])
    assert media.run_bundle(bundle_root=tmp_path) == 2
    row, = read_json(tmp_path / "media_rows.json")
    assert row["status"] == "error"
    assert row["local_path"] == ""
    assert response.closed
    assert not list((folder / "images").iterdir())
    assert "private-detail" not in (tmp_path / "media_download_report.json").read_text(encoding="utf-8")


def test_explicit_smoke_seed_is_matching_and_verified(media, tmp_path):
    root = tmp_path / "bundle"
    article(root, assets=[{"kind": "image", "url": URL}])
    smoke = tmp_path / "smoke" / "123_1"
    images = smoke / "images"
    images.mkdir(parents=True)
    digest = hashlib.sha256(PNG).hexdigest()
    url_key = hashlib.sha256(URL.encode()).hexdigest()[:24]
    relative = f"images/{url_key}-{digest}.png"
    (smoke / relative).write_bytes(PNG)
    (smoke / "manifest.json").write_text(json.dumps([
        {"kind": "image", "status": "downloaded", "local_path": relative, "bytes": len(PNG), "sha256": digest}
    ]), encoding="utf-8")
    assert media.run_bundle(bundle_root=root, smoke_root=smoke) == 0
    report = read_json(root / "media_download_report.json")
    assert report["smoke_seeded_files"] == 1
    assert report["status_counts"] == {"skipped": 1}
    assert report["http_request_count"] == 0


def test_atomically_written_report_preserves_old_file_on_failure(media, tmp_path, monkeypatch):
    target = tmp_path / "report.json"
    target.write_text("old", encoding="utf-8")
    monkeypatch.setattr(media.os, "replace", Mock(side_effect=OSError("interrupted")))
    with pytest.raises(OSError):
        media.atomic_json(tmp_path, target, {"status": "complete"})
    assert target.read_text(encoding="utf-8") == "old"
    assert not list(tmp_path.glob("*.part"))


def test_atomic_write_cannot_escape_bundle(media, tmp_path):
    root = tmp_path / "bundle"
    root.mkdir()
    with pytest.raises(media.BundleError, match="unsafe_path"):
        media.atomic_text(root, tmp_path / "outside.txt", "bad")
    assert not (tmp_path / "outside.txt").exists()


def test_parallel_articles_have_stable_indices_and_appended_cover(media, tmp_path, monkeypatch):
    article(tmp_path, "456_2", media_assets_json="[]", media_assets=None, cover_image_url=URL)
    article(tmp_path, "123_1", assets=[{"kind": "image", "url": URL}])
    fake_download(monkeypatch, [Response(), Response()])
    assert media.run_bundle(2, bundle_root=tmp_path) == 0
    rows = read_json(tmp_path / "media_rows.json")
    assert [row["article_key"] for row in rows] == ["123_1", "456_2"]
    assert rows[1]["asset_index"] is None
    assert rows[1]["resource_index"] == 0
    assert rows[1]["role"] == "cover"
    assert read_json(tmp_path / "media_download_report.json")["reusable_file_count"] == 2


def test_reusable_module_exists():
    assert importlib.util.find_spec("crawl.bundle_media") is not None, "Reusable bundle module is missing"


def test_failed_article_can_resume_without_redownloading_verified_media(media, tmp_path, monkeypatch):
    folder = article(tmp_path, assets=[{"kind": "image", "url": URL}])
    get = fake_download(monkeypatch, [Response()])
    render = media.local_html
    monkeypatch.setattr(media, "local_html", Mock(side_effect=RuntimeError("private-detail")))
    assert media.run_bundle(bundle_root=tmp_path) == 2
    report = read_json(tmp_path / "media_download_report.json")
    assert report["status"] == "partial"
    assert report["article_errors"][0]["error_code"] == "article_media_processing_failed"
    assert "private-detail" not in json.dumps(report)
    assert list((folder / "images").glob("*.png"))
    monkeypatch.setattr(media, "local_html", render)
    assert media.run_bundle(bundle_root=tmp_path) == 0
    assert read_json(tmp_path / "media_rows.json")[0]["status"] == "skipped"
    assert get.call_count == 1


def test_run_freezes_roster_until_next_invocation(media, tmp_path, monkeypatch):
    article(tmp_path)
    process = media.process_article

    def add_late_article(snapshot, bundle, smoke):
        article(bundle, "456_1")
        return process(snapshot, bundle, smoke)

    monkeypatch.setattr(media, "process_article", add_late_article)
    assert media.run_bundle(bundle_root=tmp_path) == 0
    report = read_json(tmp_path / "media_download_report.json")
    assert report["snapshot_article_keys"] == ["123_1"]
    assert not (tmp_path / "articles/456_1/media_manifest.json").exists()
    monkeypatch.setattr(media, "process_article", process)
    assert media.run_bundle(bundle_root=tmp_path) == 0
    assert read_json(tmp_path / "media_download_report.json")["snapshot_article_keys"] == ["123_1", "456_1"]


def test_invalid_metadata_does_not_hide_valid_article_or_mark_complete(media, tmp_path):
    article(tmp_path)
    article(tmp_path, "456_1", mid="wrong")
    assert media.run_bundle(bundle_root=tmp_path) == 2
    report = read_json(tmp_path / "media_download_report.json")
    assert report["snapshot_metadata_count"] == 2
    assert report["processed_article_count"] == 1
    assert report["snapshot_errors"][0]["error_code"] == "article_id_mismatch"


@pytest.mark.parametrize("file_path,digest,code", [
    ("images/a.png", "0" * 64, "file_verification_failed"),
    ("../outside.png", hashlib.sha256(PNG).hexdigest(), "unsafe_path"),
])
def test_post_download_verification_rejects_bad_hash_or_path(media, tmp_path, monkeypatch, file_path, digest, code):
    folder = article(tmp_path, assets=[{"kind": "image", "url": URL}])
    (folder / "images").mkdir()
    (folder / "images/a.png").write_bytes(PNG)
    row = {"kind": "image", "source_url": "redacted", "status": "downloaded", "error_code": "",
           "local_path": file_path, "bytes": len(PNG), "sha256": digest}
    monkeypatch.setattr(media, "fetch_asset", lambda *args: (dict(row), []))
    assert media.run_bundle(bundle_root=tmp_path) == 2
    actual, = read_json(tmp_path / "media_rows.json")
    assert (actual["status"], actual["error_code"]) == ("error", code)
    assert actual["local_path"] == actual["sha256"] == ""
    assert actual["bytes"] == 0
    assert URL in (folder / "article.local.html").read_text(encoding="utf-8")


def test_smoke_bad_hash_is_not_seeded_and_matching_article_is_required(media, tmp_path, monkeypatch):
    root = tmp_path / "bundle"
    folder = article(root, assets=[{"kind": "image", "url": URL}])
    smoke = tmp_path / "smoke/123_1"
    (smoke / "images").mkdir(parents=True)
    digest = hashlib.sha256(PNG).hexdigest()
    url_key = hashlib.sha256(URL.encode()).hexdigest()[:24]
    relative = f"images/{url_key}-{digest}.png"
    (smoke / relative).write_bytes(b"corrupt")
    (smoke / "manifest.json").write_text(json.dumps([
        {"kind": "image", "status": "downloaded", "local_path": relative, "bytes": len(PNG), "sha256": digest}
    ]), encoding="utf-8")
    plan = media.asset_plan({"media_assets": [{"kind": "image", "url": URL}]})
    assert media.seed_smoke(root, root / "articles/456_1", plan, smoke) == 0
    with pytest.raises(media.BundleError, match="smoke_checksum_mismatch"):
        media.seed_smoke(root, folder, plan, smoke)
    assert not (folder / "images").exists()
    get = fake_download(monkeypatch, [Response()])
    assert media.run_bundle(bundle_root=root, smoke_root=smoke) == 0
    report = read_json(root / "media_download_report.json")
    assert report["smoke_seeded_files"] == 0
    assert report["articles"][0]["smoke_seed_error"] == "smoke_seed_failed"
    assert get.call_count == 1


def test_bundle_root_symlink_is_rejected_before_reading_metadata(media, tmp_path, monkeypatch):
    article(tmp_path)
    original = Path.is_symlink
    monkeypatch.setattr(Path, "is_symlink", lambda path: path == tmp_path or original(path))
    with pytest.raises(media.BundleError, match="unsafe_bundle_root"):
        media.run_bundle(bundle_root=tmp_path)
    assert not (tmp_path / "media_download_report.json").exists()


def test_fallback_article_html_remains_a_full_document(media, tmp_path):
    folder = article(tmp_path, content_html="")
    source = '<!doctype html><html><body><p>Original &amp; full</p></body></html>'
    (folder / "article.html").write_text(source, encoding="utf-8")
    assert media.run_bundle(bundle_root=tmp_path) == 0
    assert (folder / "article.local.html").read_text(encoding="utf-8") == source
