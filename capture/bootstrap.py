"""Validate metrics from the same WeChat-client article HTTP response, not UI labels."""
import re

FIELD_PATHS = {
    'read_count': ('$html.cgiData.read_num', '$html.appmsg_bar_data.read_num'),
    'like_count': ('$html.appmsg_bar_data.old_like_count',),
    'watching_count': ('$html.appmsg_bar_data.like_count',),
    'share_count': ('$html.appmsg_bar_data.share_count',),
    'favorite_count': ('$html.appmsg_bar_data.collect_count',),
    'comment_count': ('$html.appmsg_bar_data.comment_count',),
}

for _field, _key in [('read_count','read_num'),('like_count','old_like_count'),('watching_count','like_count'),('share_count','share_count'),('favorite_count','collect_count'),('comment_count','comment_count')]:
    FIELD_PATHS[_field] += ('$html.cgiDataNew.user_info.appmsg_bar_data.'+_key,)


def normalize_bootstrap(raw, out):
    """Fail closed on identity conflicts and never coerce empty/sentinel values to zero."""
    from capture.reader import CaptureValidationError
    if (raw.get('http_status') != 200 or out['source'] != 'fiddler_classic'
            or not (out['endpoint'] == '/s' or out['endpoint'].startswith('/s/'))
            or not re.fullmatch(r'(?:wechatappex|weixin|wechat):[1-9]\d*', str(raw.get('process', '')))):
        raise CaptureValidationError('unverified_client_html_source')
    html_identity = raw.get('html_identity') or {}
    if any(str(html_identity.get(k, '')) != str(out.get(k, '')) for k in ('biz', 'mid', 'idx', 'sn')):
        raise CaptureValidationError('bootstrap_article_identity_mismatch')
    comment_id=str(raw.get('comment_id') or '')
    if comment_id and not re.fullmatch(r'\d{1,30}',comment_id):
        raise CaptureValidationError('invalid_comment_id')
    session_id=raw.get('session_id')
    if isinstance(session_id,bool) or not isinstance(session_id,int) or session_id<1:
        raise CaptureValidationError('invalid_capture_session_id')
    business_ret=raw.get('business_ret')
    if business_ret is not None and (isinstance(business_ret,bool) or not isinstance(business_ret,int) or business_ret!=0):
        raise CaptureValidationError('client_html_business_rejected')
    data=raw.get('bootstrap_fields') or {}
    if not isinstance(data,dict):raise CaptureValidationError('invalid_bootstrap_fields')
    clean={}
    out.update(metric_format='client_html_bootstrap_v1',http_status=200,process=raw['process'],
               session_id=session_id,comment_id=comment_id,
               html_identity={k:str(out[k]) for k in ('biz','mid','idx','sn')},
               metrics={},field_meta={},bootstrap_fields=clean)
    if business_ret is not None:out['business_ret']=business_ret
    for field,paths in FIELD_PATHS.items():
        observed=[]
        for path in paths:
            candidates=data.get(path,[])
            if not isinstance(candidates,list) or len(candidates)>100:
                raise CaptureValidationError('invalid_bootstrap_candidates')
            for item in candidates:
                if not isinstance(item,dict):raise CaptureValidationError('invalid_bootstrap_candidate')
                literal=item.get('raw_value')
                if not isinstance(literal,str) or not re.fullmatch(r'(?:-?\d{1,15})?',literal):
                    continue
                clean.setdefault(path,[]).append({'raw_value':literal})
                if literal and int(literal)>=0:observed.append((path,literal,int(literal)))
        values={v for _,_,v in observed}
        if len(values)>1:raise CaptureValidationError('conflicting_bootstrap_field')
        path,literal,value=observed[0] if observed else (None,None,None)
        out['metrics'][field]=value
        meta={'status':'observed' if value is not None else 'not_returned',
              'precision':'exact' if value is not None else 'unknown',
              'raw_field':path,'captured_at':out['captured_at'],'source':out['source'],
              'mapping':'client-html-bootstrap-v1','source_channel':'client_http_response_html',
              'http_status':200,'business_ret':business_ret,'business_ret_status':'observed_in_html_payload' if business_ret is not None else 'not_applicable_html',
              'fiddler_session_id':session_id,'process':raw['process']}
        if value is not None:meta['raw_value']=literal
        if business_ret is not None:meta['business_ret_path']='$html.cgiDataNew.base_resp.ret'
        if field=='comment_count':meta.update(scope='article_page_reported_comment_count',comment_id=comment_id)
        out['field_meta'][field]=meta
    return out
