from parsers.url_parser import parse_wechat_url


def test_parse_wechat_url_extracts_batch_and_position_from_sample_url():
    url = "http://mp.weixin.qq.com/s?__biz=MzI3MDE3Nzg0OA==&amp;mid=2247488874&amp;idx=1&amp;sn=bf59a19d7899ec62b8b40ac12de35ef8&amp;chksm=abc&amp;scene=27#wechat_redirect"

    parsed = parse_wechat_url(url)

    assert parsed.biz == "MzI3MDE3Nzg0OA=="
    assert parsed.mid == "2247488874"
    assert parsed.idx == 1
    assert parsed.sn == "bf59a19d7899ec62b8b40ac12de35ef8"
    assert parsed.batch_key == "MzI3MDE3Nzg0OA==:2247488874"
    assert parsed.position_in_batch == 1
    assert parsed.canonical_url.startswith("https://mp.weixin.qq.com/s?")
    assert "&amp;" not in parsed.canonical_url


def test_parse_wechat_url_keeps_missing_idx_as_none():
    parsed = parse_wechat_url("https://mp.weixin.qq.com/s?__biz=biz&mid=1&sn=abc")

    assert parsed.idx is None
    assert parsed.position_in_batch is None
