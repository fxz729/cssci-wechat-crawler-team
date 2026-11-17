import json
import pytest
from capture.uia import build_uia_event

BIZ='Mzk3NTE3NTU0Ng=='

def test_uia_labels_are_joined_only_to_verified_article_and_keep_missing_counts():
    observation={'title':'Title','url':'https://mp.weixin.qq.com/s/short','captured_at':'2026-09-06T10:00:00Z','labels':['阅读123','赞 0','分享 5','在看 2','留言 写留言','收藏 Ctrl+D']}
    article={'biz':BIZ,'mid':'123','idx':1,'sn':'x','article':{'title':'Title','url':'https://mp.weixin.qq.com/s?__biz='+BIZ+'&mid=123&idx=1&sn=x'}}
    observation['identity_stable']=True
    observation['metric_candidates']=[{'label':s,'control_type':'Group' if s.startswith('阅读') else 'Button','scope':'article_read_count' if s.startswith('阅读') else 'article_action_bar'} for s in observation['labels']]
    event=build_uia_event(observation,article,confirmed_public_url=observation['url'])
    assert event['metrics']['like_count']==0
    assert event['metrics']['comment_count'] is None and event['metrics']['favorite_count'] is None
    assert event['field_meta']['watching_count']['raw_label']=='在看'
    assert event['source']=='wechat_uia'
    observation['title']='Different'
    with pytest.raises(ValueError):build_uia_event(observation,article,confirmed_public_url=observation['url'])

def test_copy_link_does_not_authorize_matching_by_title_alone():
    o={'title':'Title','url':'https://mp.weixin.qq.com/s/a','captured_at':'2026-09-06T10:00:00Z','labels':['阅读123']}
    a={'biz':BIZ,'mid':'123','idx':1,'sn':'x','article':{'title':'Title'}}
    with pytest.raises(ValueError):build_uia_event(o,a,confirmed_public_url='https://mp.weixin.qq.com/s/b')

def test_conflicting_identity_in_short_url_is_rejected():
    o={'title':'Title','url':'https://mp.weixin.qq.com/s/a?__biz=wrong&mid=999','captured_at':'2026-09-06T10:00:00Z','labels':['赞 100']}
    a={'biz':BIZ,'mid':'123','idx':1,'sn':'x','endpoint':'/s/a','article':{'title':'Title'}}
    o['identity_stable']=True; o['metric_candidates']=[{'label':s,'control_type':'Button','scope':'article_action_bar'} for s in o['labels']]
    with pytest.raises(ValueError):build_uia_event(o,a,confirmed_public_url=o['url'])

def test_conflicting_metric_candidates_are_not_last_value_wins():
    o={'title':'Title','url':'https://mp.weixin.qq.com/s/a','captured_at':'2026-09-06T10:00:00Z','labels':['赞 100','赞 3']}
    a={'biz':BIZ,'mid':'123','idx':1,'sn':'x','endpoint':'/s/a','article':{'title':'Title'}}
    o['identity_stable']=True; o['metric_candidates']=[{'label':s,'control_type':'Button','scope':'article_action_bar'} for s in o['labels']]
    with pytest.raises(ValueError):build_uia_event(o,a,confirmed_public_url=o['url'])
