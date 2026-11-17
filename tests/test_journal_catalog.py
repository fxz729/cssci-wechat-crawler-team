from sources.journal_catalog import parse_catalog_text, parse_profile_links_text, normalize_journal_name


def test_parse_catalog_text_extracts_cssci_and_extended_journals():
    markdown = """# CSSCI 来源刊目录（2025-2026）

## 教育学

| CSSCI（2种） | CSSCI扩展版（1种） |
| 序号 | 期刊 | 序号 | 期刊 |
| 1 | 北京大学教育评论 | 1 | 比较教育学报 |
| 2 | 华东师范大学学报(教育科学版) |  |  |

## 体育学

| CSSCI（1种） | CSSCI扩展版（0种） |
| 序号 | 期刊 | 序号 | 期刊 |
| 1 | 体育科学 |  |  |
"""

    seeds = parse_catalog_text(markdown, source_file="catalog.md", catalog_version="2025-2026")

    assert [(seed.discipline, seed.tier, seed.journal_name) for seed in seeds] == [
        ("教育学", "cssci", "北京大学教育评论"),
        ("教育学", "cssci_extended", "比较教育学报"),
        ("教育学", "cssci", "华东师范大学学报(教育科学版)"),
        ("体育学", "cssci", "体育科学"),
    ]
    assert seeds[0].catalog_version == "2025-2026"
    assert seeds[0].source_file == "catalog.md"
    assert "华东师范大学学报" in seeds[2].aliases


def test_normalize_journal_name_unifies_parentheses_and_spaces():
    assert normalize_journal_name(" 华东师范大学学报（教育科学版） ") == "华东师范大学学报(教育科学版)"


def test_parse_profile_links_text_extracts_confirmed_accounts_and_biz():
    markdown = """# CSSCI 来源刊公众号主页链接

| 序号 | 学科 | 类别 | 期刊原名 | 搜索到的公众号作者 | 公众号主页链接 | 样本文章标题 |
| --- | --- | --- | --- | --- | --- | --- |
| 1 | 教育学 | cssci | 北京大学教育评论 | 北京大学教育评论 | https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=biz-one&scene=124#wechat_redirect | 样本 |
| 2 | 法学 | cssci_extended | 北方法学 | 北方法学 | https://mp.weixin.qq.com/mp/profile_ext?action=home&__biz=biz-two&scene=124#wechat_redirect | 样本 |
"""

    targets = parse_profile_links_text(markdown, source_file="links.md")

    assert [(target.discipline, target.tier, target.journal_name, target.biz) for target in targets] == [
        ("教育学", "cssci", "北京大学教育评论", "biz-one"),
        ("法学", "cssci_extended", "北方法学", "biz-two"),
    ]
    assert targets[0].account_name == "北京大学教育评论"
    assert targets[0].profile_url.endswith("__biz=biz-one&scene=124#wechat_redirect")
