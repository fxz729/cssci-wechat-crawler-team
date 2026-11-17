from copy import deepcopy
import pytest
from capture.reader import normalize_event, to_wire_event, CaptureValidationError

BIZ='Mzk3NTE3NTU0Ng=='

def event():
    e={'schema_version':1,'kind':'metrics','event_id':'test-bootstrap','source':'fiddler_classic',
       'captured_at':'2026-09-06T12:41:02+00:00','endpoint':'/s','biz':BIZ,'mid':'2247487163','idx':1,
       'sn':'e8bfa36036fbdef2ac1ef4beecdce96e','metric_format':'client_html_bootstrap_v1',
       'http_status':200,'process':'wechatappex:20060','session_id':2512,
       'comment_id':'4597967772206546947','html_identity':{'biz':BIZ,'mid':'2247487163','idx':'1','sn':'e8bfa36036fbdef2ac1ef4beecdce96e'},
       'bootstrap_fields':{}}
    fields={'$html.cgiData.read_num':'546','$html.appmsg_bar_data.old_like_count':'9',
            '$html.appmsg_bar_data.like_count':'4','$html.appmsg_bar_data.share_count':'46',
            '$html.appmsg_bar_data.collect_count':'6','$html.appmsg_bar_data.comment_count':'0'}
    e['bootstrap_fields']={k:[{'raw_value':v,'value':int(v)}] for k,v in fields.items()}
    return e


def test_six_raw_fields_and_html_has_no_json_business_return_code():
    n=normalize_event(event(),{BIZ})
    assert list(n['metrics'].values())==[546,9,4,46,6,0]
    assert n['field_meta']['favorite_count']['raw_field']=='$html.appmsg_bar_data.collect_count'
    assert n['field_meta']['comment_count']['raw_value']=='0'
    assert n['field_meta']['read_count']['business_ret'] is None
    assert n['field_meta']['read_count']['business_ret_status']=='not_applicable_html'
    assert n['field_meta']['read_count']['mapping']=='client-html-bootstrap-v1'


def test_empty_or_sentinel_is_missing_not_zero_and_not_boolean_flag():
    e=event();e['bootstrap_fields']['$html.appmsg_bar_data.collect_count']=[{'raw_value':''}]
    e['bootstrap_fields']['$html.appmsg_bar_data.comment_count']=[{'raw_value':'-1','value':-1}]
    e['bootstrap_fields']['$html.appmsg_bar_data.is_collect']=[{'raw_value':'1'}]
    n=normalize_event(e,{BIZ})
    assert n['metrics']['favorite_count'] is None
    assert n['metrics']['comment_count'] is None


@pytest.mark.parametrize('change',[{'http_status':403},{'process':'chrome:1234'}, {'html_identity':{'biz':BIZ,'mid':'999','idx':'1'}}])
def test_rejects_wrong_source_or_identity(change):
    e=event();e.update(change)
    with pytest.raises(CaptureValidationError):normalize_event(e,{BIZ})


def test_conflicting_duplicate_fields_fail_instead_of_overwriting():
    e=event();e['bootstrap_fields']['$html.appmsg_bar_data.collect_count'].append({'raw_value':'7'})
    with pytest.raises(CaptureValidationError,match='conflicting_bootstrap_field'):normalize_event(e,{BIZ})


def test_normalized_wire_roundtrip_keeps_provenance_and_discards_secrets():
    e=event();e['cookie']='SECRET_COOKIE';e['bootstrap_fields']['$.token']=[{'raw_value':'SECRET_TOKEN'}]
    n=normalize_event(e,{BIZ});wire=to_wire_event(n)
    assert normalize_event(wire,{BIZ})==n
    assert 'SECRET' not in repr(wire)
    assert 'stats' not in wire

def test_extended_cgidatanew_field_paths_and_real_business_code():
    e=event();e['business_ret']=0
    e['bootstrap_fields']={'$html.cgiDataNew.user_info.appmsg_bar_data.'+p.rsplit('.',1)[1]:v for p,v in e['bootstrap_fields'].items()}
    n=normalize_event(e,{BIZ})
    assert list(n['metrics'].values())==[546,9,4,46,6,0]
    assert n['field_meta']['read_count']['business_ret']==0
    assert n['field_meta']['read_count']['business_ret_path']=='$html.cgiDataNew.base_resp.ret'
    assert normalize_event(to_wire_event(n),{BIZ})==n
    e['business_ret']=-3
    with pytest.raises(CaptureValidationError,match='business_rejected'):normalize_event(e,{BIZ})
