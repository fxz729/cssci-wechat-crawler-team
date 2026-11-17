"""Synthetic, offline tests for the single-writer session export contract."""
import copy
import csv
import json
from pathlib import Path
from urllib.parse import urlencode

from bs4 import BeautifulSoup
import pytest

from crawl.profile_session_probe import article_snapshot
from export.realtime_exporter import default_export_headers


BIZ = "test-journal"
SN = "a" * 32
CAPTURED = "2026-09-09T00:00:00+00:00"
SOURCE = "$html.cgiDataNew.user_info.appmsg_bar_data."
MEDIA_FIELDS = {
    "media_manifest", "media_status", "media_downloaded", "media_pending",
    "media_local_paths", "image_local_paths", "audio_local_paths", "video_local_paths",
}


def sample(mid="123", idx=1, biz=BIZ, sn=SN, **changes):
    url = "https://mp.weixin.qq.com/s?" + urlencode(dict(__biz=biz, mid=mid, idx=idx, sn=sn))
    data = {
        "base_resp": {"ret": 0}, "link": url, "title": "同名文章", "author": "",
        "nick_name": "测试期刊", "desc": "", "content_noencode": "<p>正文，含逗号</p><p>第二段</p>",
        "create_time": "1700000000", "comment_id": "42",
        "user_info": {"appmsg_bar_data": {"read_num": "10", "old_like_count": "2",
                     "like_count": "3", "share_count": "4", "collect_count": "0"}},
    }
    article = article_snapshot("<script>window.cgiDataNew = " + json.dumps(data) + ";</script>", url)
    article["captured_at"] = CAPTURED
    article.update(changes)
    return article


def exporter(root):
    # Import here so the RED run reports missing-feature failures, not collection errors.
    from export.session_article_exporter import SessionArticleExporter
    return SessionArticleExporter(root)


def read_csv(root, filename="文章数据.csv"):
    with (root / filename).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        return reader.fieldnames, list(reader)


def watch_csv_replacements(monkeypatch, root):
    import export.session_article_exporter as module
    original = module.os.replace
    replacements = []

    def replace(source, destination):
        if Path(destination) == root / "文章数据.csv":
            replacements.append(Path(destination))
        return original(source, destination)

    monkeypatch.setattr(module.os, "replace", replace)
    return replacements


def test_new_append_preserves_bom_fixed_header_zero_blank_and_sources(tmp_path, monkeypatch):
    output = exporter(tmp_path)
    row = output.save(sample())
    path = tmp_path / "文章数据.csv"
    before = path.read_bytes()
    assert before.startswith(b"\xef\xbb\xbf")
    headers, rows = read_csv(tmp_path)
    assert headers[:len(default_export_headers([]))] == default_export_headers([])
    assert len(headers) == len(set(headers))
    assert MEDIA_FIELDS | {"local_article_dir"} <= set(headers)
    assert row["local_article_dir"] == "附件/123_1"
    assert row["content_html"] == sample()["content_html"]
    assert rows[0]["favorite_count"] == "0"
    assert rows[0]["comment_count"] == rows[0]["author"] == rows[0]["digest"] == ""
    assert rows[0]["metrics_provider"] == "background_client_session_html"
    assert json.loads(rows[0]["metric_fields"])[SOURCE + "collect_count"]["raw_value"] == "0"
    assert json.loads(rows[0]["metric_field_status"])["comment_count"] == "not_observed"

    original_open = Path.open
    modes = []

    def append_only(self, mode="r", *args, **kwargs):
        if self == path:
            modes.append(mode)
            assert "a" in mode, "new articles must not read or rewrite the CSV"
        return original_open(self, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", append_only)
        second = sample(mid="124", unrelated_column="ignored")
        second["metrics"]["comment_count"] = None
        second["metric_fields"][SOURCE + "comment_count"] = {"present": True, "raw_value": "99"}
        output.save(second)
    assert modes
    assert path.read_bytes().startswith(before)
    assert path.read_bytes().count(b"\xef\xbb\xbf") == 1
    assert read_csv(tmp_path)[0] == headers
    assert read_csv(tmp_path)[1][1]["comment_count"] == ""  # No raw/HTML fallback.


def test_same_title_does_not_deduplicate_distinct_identities(tmp_path):
    output = exporter(tmp_path)
    for article in (sample(), sample(idx=2), sample(mid="124")):
        output.save(article)
    rows = read_csv(tmp_path)[1]
    assert len(rows) == 3
    assert len({row["title"] for row in rows}) == 1
    assert [(row["mid"], row["idx"]) for row in rows] == [("123", "1"), ("123", "2"), ("124", "1")]


def test_repeat_has_no_duplicate_and_changed_value_rewrites_once(tmp_path, monkeypatch):
    output = exporter(tmp_path)
    article = sample()
    output.save(article)
    output.save(sample(mid="124"))
    before = (tmp_path / "文章数据.csv").read_bytes()
    replacements = watch_csv_replacements(monkeypatch, tmp_path)
    output.save(article)
    assert (tmp_path / "文章数据.csv").read_bytes() == before
    assert replacements == []
    changed = copy.deepcopy(article)
    changed["title"] = "修订标题"
    changed["metrics"]["read_count"] = 20
    changed["metric_fields"][SOURCE + "read_num"]["raw_value"] = "20"
    row = output.save(changed)
    assert row["read_count"] == 20
    assert len(replacements) == 1
    rows = read_csv(tmp_path)[1]
    assert [(r["mid"], r["title"]) for r in rows] == [("123", "修订标题"), ("124", "同名文章")]
    assert rows[0]["read_count"] == "20"


def test_instantiate_resumes_csv_index_and_single_journal(tmp_path):
    exporter(tmp_path).save(sample())
    resumed = exporter(tmp_path)
    resumed.save(sample())
    resumed.save(sample(mid="125"))
    assert len(read_csv(tmp_path)[1]) == 2
    with pytest.raises(ValueError):
        resumed.save(sample(biz="second-journal"))


def test_metadata_before_csv_interruption_can_resume_same_article(tmp_path, monkeypatch):
    output = exporter(tmp_path)
    original_open = Path.open

    def fail_csv(self, mode="r", *args, **kwargs):
        if self == tmp_path / "文章数据.csv" and "a" in mode:
            raise OSError("simulated append interruption")
        return original_open(self, mode, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(Path, "open", fail_csv)
        with pytest.raises(OSError, match="simulated"):
            output.save(sample())
    assert (tmp_path / "附件/123_1/metadata.json").is_file()
    resumed = exporter(tmp_path)
    with pytest.raises(ValueError):
        resumed.save(sample(mid="999", biz="second-journal"))
    resumed.save(sample())
    resumed.save(sample())
    assert len(read_csv(tmp_path)[1]) == 1


def test_over_16mb_html_survives_save_resume_and_streaming_update(tmp_path):
    text = "x" * (17 * 1024 * 1024) + "尾部，换行\nEND"
    html = "<p>" + text + "</p>"
    article = sample(content_html=html)
    output = exporter(tmp_path)
    output.save(article)
    assert len(repr(vars(output))) < 50_000  # Identity/hash index, not cached article bodies.
    del output
    resumed = exporter(tmp_path)
    resumed.save(dict(article, title="长文修订"))
    rows = read_csv(tmp_path)[1]
    assert len(rows) == 1
    assert rows[0]["content_html"] == html
    assert rows[0]["content"] == rows[0]["content_text"] == text
    assert (tmp_path / rows[0]["local_text"]).read_text(encoding="utf-8") == text
    assert html in (tmp_path / rows[0]["local_html"]).read_text(encoding="utf-8")
    assert json.loads((tmp_path / rows[0]["local_metadata"]).read_text(encoding="utf-8"))["content_html"] == html


def test_invalid_identity_domain_and_second_biz_rejected_before_writes(tmp_path):
    output = exporter(tmp_path)
    article = sample()
    bad_urls = [
        article["url"].replace("mp.weixin.qq.com", "mp.weixin.qq.com.evil.test"),
        article["url"].replace("/s?", "/other?"),
        article["url"] + "&mid=123", article["url"] + "&__biz=" + BIZ,
        article["url"] + "&idx=1", article["url"] + "&sn=" + SN,
        article["url"].replace("mp.weixin.qq.com", "user@mp.weixin.qq.com"),
        article["url"].replace("mid=123", "mid=../123"),
    ]
    for bad in [*(dict(article, url=url) for url in bad_urls),
                dict(article, mid="999"), dict(article, idx=True),
                dict(article, identity_matches=1), dict(article, business_ret=1)]:
        with pytest.raises(ValueError):
            output.save(bad)
    assert not list(tmp_path.rglob("metadata.json"))
    output.save(article)
    before = (tmp_path / "文章数据.csv").read_bytes()
    for bad in (sample(biz="second-journal"), sample(sn="b" * 32)):
        with pytest.raises(ValueError):
            output.save(bad)
    assert (tmp_path / "文章数据.csv").read_bytes() == before


def test_content_references_csp_and_media_without_downloading(tmp_path):
    content = ('<p>完整正文</p><img data-src="https://mmbiz.qpic.cn/mmbiz_png/test/640?wx_fmt=png">'
               '<audio src="https://media.example.test/a.mp3"></audio>'
               '<video src="https://media.example.test/a.mp4"></video>'
               '<script>fetch("https://blocked.example.test/")</script>')
    row = exporter(tmp_path).save(sample(title='<标题 & "引号">', content_html=content))
    assert row["image_count"] == row["audio_count"] == row["video_count"] == 1
    assert row["media_status"] == "metadata_only"
    assert row["media_local_paths"] == []
    assert len(row["media_assets"]) == 3
    assert row["media_urls"] == [asset["url"] for asset in row["media_assets"]]
    for field in ("local_html", "local_text", "local_metadata"):
        assert not Path(row[field]).is_absolute()
        assert (tmp_path / row[field]).is_file()
    doc = BeautifulSoup((tmp_path / row["local_html"]).read_text(encoding="utf-8"), "lxml")
    csp = doc.find("meta", attrs={"http-equiv": "Content-Security-Policy"})["content"]
    assert "default-src 'none'" in csp and "script-src 'none'" in csp and "connect-src 'none'" in csp
    assert doc.h1.get_text() == '<标题 & "引号">'
    assert (tmp_path / row["local_text"]).read_text(encoding="utf-8") == row["content"]
    assert json.loads((tmp_path / row["local_metadata"]).read_text(encoding="utf-8")) == row
    assert sorted(p.name for p in (tmp_path / row["local_article_dir"]).iterdir()) == ["metadata.json", "正文.html", "正文.txt"]


def test_unknown_secret_and_raw_body_fields_are_never_exported(tmp_path):
    article = sample()
    secret = "SYNTHETIC_DO_NOT_EXPORT"
    for key in ("cookie", "headers", "credentials", "key", "uin", "pass_ticket", "body", "source_body", "cgiDataNew"):
        article[key] = {"nested": secret}
    article["url"] += "&key=" + secret + "&pass_ticket=" + secret
    article["metrics"]["unknown_counter"] = secret
    article["metric_fields"]["untrusted_source"] = {"raw_value": secret}
    article["metric_fields"][SOURCE + "read_num"]["credentials"] = secret
    before = copy.deepcopy(article)
    row = exporter(tmp_path).save(article)
    assert article == before
    assert row["url"] == sample()["url"]
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert secret not in path.read_text(encoding="utf-8-sig")
    assert "credentials" not in read_csv(tmp_path)[0]


def test_output_path_escape_and_production_root_rejected(tmp_path, monkeypatch):
    root = tmp_path / "output"
    output = exporter(root)
    original_resolve = Path.resolve
    outside = tmp_path / "outside"

    def redirected(self, *args, **kwargs):
        resolved = original_resolve(self, *args, **kwargs)
        if root / "附件" == self or root / "附件" in self.parents:
            return outside / self.name
        return resolved

    with monkeypatch.context() as patch:
        patch.setattr(Path, "resolve", redirected)
        with pytest.raises(ValueError):
            output.save(sample())
    assert not outside.exists()
    with pytest.raises(ValueError):
        exporter(tmp_path / "production")
    assert not (tmp_path / "production").exists()


def media_result(row):
    path = row["local_article_dir"] + "/images/a.jpg"
    return {"status": "complete", "rows": [{"kind": "image", "url": "https://media.example.test/a.jpg",
            "local_path": path, "status": "downloaded", "bytes": 7, "cookie": "MEDIA_SECRET"}],
            "media_local_paths": [path], "image_local_paths": [path], "audio_local_paths": [],
            "video_local_paths": [], "media_downloaded": 1, "media_pending": 0,
            "credentials": "MEDIA_SECRET", "title": "must not replace article title"}


def test_update_media_is_one_csv_rewrite_with_metadata_and_media_index(tmp_path, monkeypatch):
    output = exporter(tmp_path)
    first, second = output.save(sample()), output.save(sample(mid="124"))
    output.save(sample(mid="125"))
    headers, before = read_csv(tmp_path)
    updates = {"123_1": media_result(first), "124_1": media_result(second)}
    replacements = watch_csv_replacements(monkeypatch, tmp_path)
    output.update_media(updates)
    assert len(replacements) == 1
    after_headers, after = read_csv(tmp_path)
    assert after_headers == headers
    for old, new in zip(before[:2], after[:2]):
        assert {k: v for k, v in old.items() if k not in MEDIA_FIELDS} == {k: v for k, v in new.items() if k not in MEDIA_FIELDS}
        assert new["media_status"] == "complete"
        assert new["media_pending"] == "0"
        metadata = json.loads((tmp_path / new["local_metadata"]).read_text(encoding="utf-8"))
        assert metadata["media_local_paths"] == json.loads(new["media_local_paths"])
        assert metadata["media_status"] == "complete"
        assert (tmp_path / new["media_manifest"]).is_file()
    assert after[2] == before[2]
    media_headers, media = read_csv(tmp_path, "媒体索引.csv")
    assert len(media) == 2 and {item["article_key"] for item in media} == {"123_1", "124_1"}
    assert "cookie" not in media_headers
    assert (tmp_path / "媒体索引.csv").read_bytes().startswith(b"\xef\xbb\xbf")
    output.update_media(updates)
    assert len(replacements) == 2
    assert len(read_csv(tmp_path, "媒体索引.csv")[1]) == 2
    assert output.save(sample())["media_status"] == "metadata_only"
    assert len(replacements) == 3  # Re-save applies the current metadata-only policy.
    exporter(tmp_path).save(sample())
    assert read_csv(tmp_path)[1][0]["media_status"] == "metadata_only"
    stable = (tmp_path / "文章数据.csv").read_bytes()
    output.update_media({})
    for invalid in ({"999_1": media_result(first)},
                    {"123_1": dict(media_result(first), image_local_paths=["../escape.jpg"])},
                    {"123_1": dict(media_result(first), media_local_paths=["production/a.jpg"])}):
        with pytest.raises(ValueError):
            output.update_media(invalid)
    assert (tmp_path / "文章数据.csv").read_bytes() == stable
    for path in tmp_path.rglob("*"):
        if path.is_file():
            assert "MEDIA_SECRET" not in path.read_text(encoding="utf-8-sig")


def test_failed_csv_rewrite_preserves_original_and_retry_recovers(tmp_path, monkeypatch):
    import os
    output = exporter(tmp_path)
    row = output.save(sample())
    output.save(sample(mid="124"))
    before = (tmp_path / "文章数据.csv").read_bytes()
    original_replace = os.replace

    def fail_replace(source, destination):
        if Path(destination) == tmp_path / "文章数据.csv":
            raise OSError("simulated replace failure")
        return original_replace(source, destination)

    with monkeypatch.context() as patch:
        patch.setattr(os, "replace", fail_replace)
        with pytest.raises(OSError, match="simulated"):
            output.save(sample(title="更新"))
        assert (tmp_path / "文章数据.csv").read_bytes() == before
        with pytest.raises(OSError, match="simulated"):
            output.update_media({"123_1": media_result(row)})
        assert (tmp_path / "文章数据.csv").read_bytes() == before
    assert not list(tmp_path.rglob("*.tmp"))
    resumed = exporter(tmp_path)
    resumed.save(sample(title="更新"))
    resumed.update_media({"123_1": media_result(row)})
    assert len(read_csv(tmp_path)[1]) == 2
    assert read_csv(tmp_path)[1][0]["title"] == "更新"
    assert read_csv(tmp_path)[1][0]["media_status"] == "complete"


def test_profile_home_link_is_public_and_has_no_session_credentials(tmp_path):
    row = exporter(tmp_path).save(sample())
    from urllib.parse import parse_qs, urlsplit
    link=urlsplit(row['profile_url'])
    assert link.hostname=='mp.weixin.qq.com' and link.path=='/mp/profile_ext'
    assert parse_qs(link.query)=={'action':['home'],'__biz':[BIZ],'scene':['124']}

def test_formatted_wechat_publication_time_is_preserved_and_parsed(tmp_path):
    row=exporter(tmp_path).save(sample(publish_timestamp='2026-01-04 14:36'))
    assert row['publish_timestamp']=='2026-01-04 14:36'
    assert row['publish_time']=='2026-01-04T06:36:00+00:00'
    assert row['field_provenance']['publish_time']=='cgiDataNew.create_time'

def test_video_without_text_is_exported_without_fabricating_body_or_losing_metrics(tmp_path):
    a=sample(content_html='',article_type='9',media_assets=[{'kind':'video','id':'wxv_123'}])
    row=exporter(tmp_path).save(a)
    assert row['content']=='' and row['content_html']==''
    assert row['video_count']==1
    assert row['media_assets']==[{'kind':'video','id':'wxv_123'}]
    assert row['read_count']==10
    assert row['content_origin']=='cgiDataNew.video_page_info'

def test_all_media_are_metadata_only_and_video_link_is_preserved(tmp_path):
    a=sample(content_html='',article_type='9',media_assets=[{'kind':'video','id':'wxv_123'}])
    row=exporter(tmp_path).save(a)
    assert row['url'] in row['media_urls']
    assert row['media_status']=='metadata_only'
    assert row['media_pending']==0
    assert row['video_local_paths']==[]
