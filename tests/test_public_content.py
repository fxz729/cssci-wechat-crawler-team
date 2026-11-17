from utils.public_content import public_html, public_snapshot


def test_body_preserved_but_scripts_private_query_and_handlers_removed():
    raw = '<p>论文，完整正文\n第二行</p><script>key="PRIVATE"</script><img data-src="https://mmbiz.qpic.cn/x?wx_fmt=png&amp;pass_ticket=PRIVATE" onerror="PRIVATE"><a href="https://mp.weixin.qq.com/s?mid=1&amp;key=PRIVATE&amp;sn=abc">链接</a>'
    out = public_html(raw)
    assert 'PRIVATE' not in out and '<script' not in out and 'onerror' not in out
    assert '论文，完整正文\n第二行' in out and 'wx_fmt=png' in out and 'mid=1' in out


def test_snapshot_allowlist_filters_nested_private_objects_without_losing_zero():
    article = dict(biz='b', mid='1', idx=1, sn='a'*32, title='title', content_html='<p>text</p>',
                   metrics={'read_count':0}, metric_fields={'$html.cgiDataNew.user_info.appmsg_bar_data.read_num': {'present':True,'raw_value':0}},
                   user_info={'openid':'PRIVATE'}, cookie='PRIVATE',
                   media_assets=[{'kind':'video','id':'public_id','ckey':'PRIVATE','url':'https://example.org/v?key=PRIVATE&vid=public_id'}])
    result=public_snapshot(article)
    assert 'PRIVATE' not in str(result)
    assert result['metrics']['read_count']==0
    assert result['media_assets'][0]['id']=='public_id'
    assert result['metric_fields']['$html.cgiDataNew.user_info.appmsg_bar_data.read_num']['raw_value']==0


def test_dangerous_urls_srcdoc_css_and_encoded_secret_names_removed():
    out=public_html('<iframe srcdoc="PRIVATE" src="javascript:PRIVATE"></iframe><img src="https://x/a?%6bey=PRIVATE&v=1"><p style="background:url(https://x/a?pass_ticket=PRIVATE)">字</p>')
    assert 'PRIVATE' not in out and 'javascript:' not in out and 'srcdoc' not in out
    assert '字' in out


def test_cleaning_is_idempotent_and_does_not_mutate_input():
    article={'title':'test','content_html':'<p>正文</p>','metrics':{'favorite_count':None}}
    result=public_snapshot(article)
    assert result==public_snapshot(result)
    assert article['metrics']=={'favorite_count':None}

def test_data_private_attrs_and_nested_url_attributes_are_projected():
    out=public_html('<mpvideo vid="PUBLIC_ID" data-key="TEST_SECRET" data-pass_ticket="TEST_SECRET"></mpvideo><iframe data-display-src="https://mp.weixin.qq.com/mp/videoplayer?vid=PUBLIC_ID&key=TEST_SECRET"></iframe>')
    assert 'TEST_SECRET' not in out and 'PUBLIC_ID' in out


def test_signed_query_names_and_protocol_relative_media():
    from utils.public_content import public_url
    assert 'TEST_SECRET' not in public_url('https://mpvideo.qpic.cn/v.mp4?auth_key=TEST_SECRET&vid=PUBLIC_ID')
    out=public_html('<img data-src="//mmbiz.qpic.cn/PUBLIC_IMAGE_ID/0?wx_fmt=png&wxtokenkey=TEST_SECRET"><iframe src="//v.qq.com/iframe/player.html?vid=PUBLIC_VIDEO_ID"></iframe>')
    assert 'TEST_SECRET' not in out and 'PUBLIC_IMAGE_ID' in out and 'PUBLIC_VIDEO_ID' in out
    assert 'https://mmbiz.qpic.cn/' in out and 'https://v.qq.com/' in out


def test_approved_historical_capture_provider_is_preserved_without_secret_strings():
    assert public_snapshot({'metrics_provider':'fiddler_classic:/s'})['metrics_provider']=='fiddler_classic:/s'
    assert 'metrics_provider' not in public_snapshot({'metrics_provider':'https://example.invalid/?key=PRIVATE'})
