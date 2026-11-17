from pathlib import Path

import pytest

from sources.fixed_roster import load_fixed_roster


def _links(rows):
    return """| 序号 | 学科 | 类别 | 期刊原名 | 搜索到的公众号作者 | 公众号主页链接 | 样本文章标题 |
| --- | --- | --- | --- | --- | --- | --- |
""" + "\n".join(
        f"| {i} | 测试 | cssci | 期刊{i} | 期刊{i} | https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=biz{i}&scene=124#wechat_redirect | 样本 |"
        for i in range(1, rows + 1)
    )


def test_fixed_roster_validates_count_and_biz_uniqueness(tmp_path):
    path = tmp_path / "links.md"
    path.write_text(_links(2), encoding="utf-8")
    roster = load_fixed_roster(path, expected_count=2)
    assert len(roster.targets) == 2
    assert roster.targets[0].biz == "biz1"
    assert len(roster.source_sha256) == 64


def test_fixed_roster_rejects_wrong_count(tmp_path):
    path = tmp_path / "links.md"
    path.write_text(_links(1), encoding="utf-8")
    with pytest.raises(ValueError, match="fixed_roster_count"):
        load_fixed_roster(path, expected_count=2)


def test_fixed_roster_rejects_duplicate_biz(tmp_path):
    path = tmp_path / "links.md"
    path.write_text(_links(2).replace("biz2", "biz1"), encoding="utf-8")
    with pytest.raises(ValueError, match="duplicate_roster_biz"):
        load_fixed_roster(path, expected_count=2)
