from discovery.sogou_search import parse_article_search_results


def test_parse_article_search_results_extracts_article_url_account_and_title():
    html = """
    <ul class="news-list">
      <li>
        <div class="txt-box">
          <h3><a href="/link?url=abc">《北京大学教育评论》2024年总目录</a></h3>
          <p class="txt-info">第22卷第1-4期</p>
          <div class="s-p"><a class="all-time-y2">北京大学教育评论</a></div>
        </div>
      </li>
    </ul>
    """

    results = parse_article_search_results(html)

    assert len(results) == 1
    assert results[0].title == "《北京大学教育评论》2024年总目录"
    assert results[0].account_name == "北京大学教育评论"
    assert results[0].summary == "第22卷第1-4期"
    assert results[0].url == "https://weixin.sogou.com/link?url=abc"
