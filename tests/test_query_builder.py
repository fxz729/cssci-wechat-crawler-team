from discovery.candidate_ranker import rank_candidate
from discovery.query_builder import build_discovery_queries
from discovery.sogou_search import parse_article_search_candidates


def test_build_discovery_queries_includes_aliases_and_context_terms():
    queries = build_discovery_queries("华东师范大学学报(教育科学版)")

    assert queries[:3] == [
        "华东师范大学学报(教育科学版)",
        "华东师范大学学报",
        "华东师范大学学报(教育科学版) 编辑部",
    ]
    assert "华东师范大学学报 微信公众号" in queries
    assert len(queries) == len(set(queries))


def test_rank_candidate_scores_exact_official_journal_highly():
    score = rank_candidate(
        journal_name="北京大学教育评论",
        candidate_name="北京大学教育评论",
        description="北京大学教育评论编辑部官方公众号",
        evidence_titles=["《北京大学教育评论》2024年总目录"],
        is_verified=True,
    )

    assert score >= 0.92


def test_rank_candidate_penalizes_unrelated_service_account():
    score = rank_candidate(
        journal_name="北京大学教育评论",
        candidate_name="论文发表助手",
        description="论文代发和投稿咨询",
        evidence_titles=["快速发表论文"],
        is_verified=False,
    )

    assert score < 0.5


def test_parse_article_search_candidates_groups_titles_by_account_name():
    html = """
    <ul class="news-list">
      <li><div class="txt-box"><h3><a>《北京大学教育评论》2024年总目录</a></h3><div class="s-p"><a class="all-time-y2">北京大学教育评论</a></div></div></li>
      <li><div class="txt-box"><h3><a>北京大学教育评论2024年第4期</a></h3><div class="s-p"><a class="all-time-y2">北京大学教育评论</a></div></div></li>
    </ul>
    """

    candidates = parse_article_search_candidates(html)

    assert len(candidates) == 1
    assert candidates[0].name == "北京大学教育评论"
    assert candidates[0].evidence_titles == ["《北京大学教育评论》2024年总目录", "北京大学教育评论2024年第4期"]
