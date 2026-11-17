// Explicit, data-only snapshot of existing sessions. Never changes requests or proxy settings.
import System;
import System.IO;
import System.Collections;
import System.Text.RegularExpressions;
import Fiddler;

class CssciDiagnostics {
    static var ControlPath: String = "__CSSCI_CONTROL_PATH__";
    static var OutputPath: String = "__CSSCI_DIAGNOSTICS_PATH__";
    static var LiveOutputPath: String = "__CSSCI_DIAGNOSTICS_LIVE_PATH__";
    static function Text(v: Object): String { return v == null ? "" : Convert.ToString(v); }
    static function Field(s: String, name: String): String {
        var m = Regex.Match(s, "(?:^|[?&])"+Regex.Escape(name)+"=([^&#]*)");
        return m.Success ? Uri.UnescapeDataString(m.Groups[1].Value.Replace('+',' ')) : '';
    }
    static function JS(s: String, name: String): String {
        var m=Regex.Match(s,'\\b'+Regex.Escape(name)+'\\s*[:=]\\s*["\']([^"\']*)["\']');
        if(m.Success && m.Groups[1].Value!='')return System.Net.WebUtility.HtmlDecode(m.Groups[1].Value);
        m=Regex.Match(s,'\\b'+Regex.Escape(name)+'\\s*[:=]\\s*["\']["\']\\s*\\|\\|\\s*["\']([^"\']*)["\']');
        return m.Success ? System.Net.WebUtility.HtmlDecode(m.Groups[1].Value) : '';
    }
    static function NumericKey(key: String): Boolean {
        return Regex.IsMatch(key, '^(ret|retcode|errcode|read_num|read_count|old_like_num|old_like_count|like_num|like_count|like_total|watching_count|share_num|share_count|share_total|collect_num|collect_count|collect_total|star_num|favorite_count|fav_num|comment_count|comment_num|comment_total|elected_comment_total_cnt|elected_comment_num|total_count|total_num|total|total_cnt)$');
    }
    static function Walk(value: Object, path: String, key: String, fields: Hashtable, depth: int): void {
        if(depth>9 || fields.Count>=600)return;
        var entry=new Hashtable();
        if(value==null){entry['type']='null';fields[path]=entry;return;}
        if(value.GetType().GetInterface('System.Collections.IDictionary')!=null){
            entry['type']='object';fields[path]=entry;
            var dict: IDictionary=IDictionary(value);
            var en: IDictionaryEnumerator=dict.GetEnumerator();
            while(en.MoveNext()){
                var k: String=Text(en.Key);
                if(!Regex.IsMatch(k,'^[A-Za-z_][A-Za-z0-9_]{0,63}$') || Regex.IsMatch(k,'^[a-fA-F0-9]{24,}$'))continue;
                Walk(en.Value,path+'.'+k,k,fields,depth+1);
            }
        } else if(value.GetType().GetInterface('System.Collections.IList')!=null){
            entry['type']='array';fields[path]=entry;
            var list: IList=IList(value);
            for(var i=0;i<System.Math.Min(list.Count,3);i++)Walk(list[i],path+'['+i+']','',fields,depth+1);
        } else {
            var t: String=Text(value);
            entry['type']=(value.GetType().FullName=='System.String') ? 'string' : ((value.GetType().FullName=='System.Boolean') ? 'boolean' : 'number');
            if(NumericKey(key) && Regex.IsMatch(t,'^-?\\d{1,15}$'))entry['value']=Int64.Parse(t);
            fields[path]=entry;
            if(entry['type']=='string' && t.Length<2097152 && (t.TrimStart().StartsWith('{') || t.TrimStart().StartsWith('['))){
                try {var decoded=Fiddler.WebFormats.JSON.JsonDecode(t);if(decoded!=null)Walk(decoded.JSONObject,path+'<json>','',fields,depth+1);}catch(_){}
            }
        }
    }
    static function InspectBody(body: String): String {
        var result=new Hashtable();var fields=new Hashtable();result['fields']=fields;
        result['format']='not_json';
        if(body.Length<=2097152){
            try {var parsed=Fiddler.WebFormats.JSON.JsonDecode(body);if(parsed!=null && parsed.JSONErrors.sWarningText=='' && parsed.JSONErrors.iErrorIndex<0){result['format']='json';Walk(parsed.JSONObject,'$','',fields,0);}}catch(e){result['error_type']=e.GetType().Name;}
        }
        result['truncated']=fields.Count>=600;
        return Fiddler.WebFormats.JSON.JsonEncode(result);
    }
    static function Unquote(t:String):String {
        var b=new System.Text.StringBuilder();
        for(var i:int=1;i<t.Length-1;i++){
            var c:String=t.Substring(i,1);
            if(c!='\\'){b.Append(c);continue;}
            i++;if(i>=t.Length-1)throw new Exception('bad_literal');c=t.Substring(i,1);
            if(c=='u' || c=='x'){var n:int=c=='u'?4:2;if(i+n>=t.Length)throw new Exception('bad_escape');b.Append(Convert.ToChar(Convert.ToInt32(t.Substring(i+1,n),16)));i+=n;}
            else if(c=='n')b.Append('\n');else if(c=='r')b.Append('\r');else if(c=='t')b.Append('\t');else if(c=='b')b.Append('\b');else if(c=='f')b.Append('\f');else b.Append(c);
        }
        return b.ToString();
    }
    static function ServerObject(body:String):IDictionary {
        var scripts=Regex.Matches(body,'<script\\b[^>]*>([\\s\\S]*?)</script>',RegexOptions.IgnoreCase);
        var assignmentCount:int=0;var found:IDictionary=null;
        for(var si=0;si<scripts.Count;si++){
            if(scripts[si].Groups[1].Value.IndexOf('cgiDataNew')<0)continue;
            var mm=Regex.Matches(scripts[si].Groups[1].Value,'/\\*[\\s\\S]*?\\*/|//[^\\n]*|"(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\'|`(?:\\\\.|[^`\\\\])*`|[A-Za-z_$][A-Za-z0-9_$]*|\\d+(?:\\.\\d+)?|[^\\s]');
            var tokens=new ArrayList();for(var ti=0;ti<mm.Count;ti++){var t:String=mm[ti].Value;if(!t.StartsWith('//')&&!t.StartsWith('/*'))tokens.Add(t);}
            for(var i:int=0;i<tokens.Count-3;i++){
                if(Text(tokens[i])!='window' || Text(tokens[i+1])!='.' || Text(tokens[i+2])!='cgiDataNew' || Text(tokens[i+3])!='=')continue;
                // A comparison such as `window.cgiDataNew == null` tokenizes as a
                // second '=' and must not be mistaken for an assignment.
                if(i+4<tokens.Count && Text(tokens[i+4])=='=')continue;
                assignmentCount++;if(assignmentCount>1)throw new Exception('duplicate_cgidatanew_assignment');
                if(i+4>=tokens.Count || Text(tokens[i+4])!='{')continue;
                var depth:int=0;var json=new System.Text.StringBuilder();var valid:Boolean=true;var closed:Boolean=false;
                try {
                    for(var j:int=i+4;j<tokens.Count;j++){
                        var v:String=Text(tokens[j]);var next:String=j+1<tokens.Count?Text(tokens[j+1]):'';
                        if(v.StartsWith('"') || v.StartsWith("'")){if(v.Length<2){valid=false;break;}json.Append(Fiddler.WebFormats.JSON.JsonEncode(Unquote(v)));}
                        else if(v=='*' && next=='1'){j++;continue;}
                        else if(v==',' && (next=='}'||next==']'))continue;
                        else if(Regex.IsMatch(v,'^[A-Za-z_][A-Za-z0-9_]*$') && next==':')json.Append(Fiddler.WebFormats.JSON.JsonEncode(v));
                        else if(Regex.IsMatch(v,'^(true|false|null|\\d+(?:\\.\\d+)?|[{}\\[\\]:,\\-])$'))json.Append(v);
                        else {valid=false;break;}
                        if(v=='{'||v=='[')depth++;if(v=='}'||v==']')depth--;
                        if(depth==0){closed=true;break;}
                    }
                    if(valid && closed){var parsed=Fiddler.WebFormats.JSON.JsonDecode(json.ToString());if(parsed!=null && parsed.JSONErrors.sWarningText=='' && parsed.JSONErrors.iErrorIndex<0)found=IDictionary(parsed.JSONObject);}
                } catch(_) { valid=false; }
            }
        }
        return found;
    }
    static function GalleryContent(body:String):String {
        var root:IDictionary=ServerObject(body);if(root==null)return '';
        var images:IList=root['picture_page_info_list']==null?null:IList(root['picture_page_info_list']);
        if(images==null || images.Count==0)return '';
        var html=new System.Text.StringBuilder();html.Append('<div id="js_content" data-capture-source="cgiDataNew.picture_page_info_list">');
        if(Text(root['desc'])!='')html.Append('<p>').Append(System.Net.WebUtility.HtmlEncode(Text(root['desc']))).Append('</p>');
        var count:int=0;
        for(var i=0;i<images.Count;i++){
            var image:IDictionary=IDictionary(images[i]);var url:String=Text(image['cdn_url']);var u:Uri=null;try{u=new Uri(url);}catch(_){continue;}
            if(u==null || u.UserInfo!='' || (u.Scheme!='https' && u.Scheme!='http'))continue;
            if(u.Host!='mmbiz.qpic.cn' && u.Host!='mmbiz.qlogo.cn')continue;
            html.Append('<img src="').Append(System.Net.WebUtility.HtmlEncode(CssciCapture.PublicUrl(url))).Append('" />');count++;
        }
        html.Append('</div>');return count>0?html.ToString():'';
    }
    static function FindNamed(value:Object,name:String,path:String,out:Hashtable,depth:int):void {
        if(value==null || depth>10)return;
        if(value.GetType().GetInterface('System.Collections.IDictionary')!=null){
            var en:IDictionaryEnumerator=IDictionary(value).GetEnumerator();while(en.MoveNext()){
                var k:String=Text(en.Key);if(!Regex.IsMatch(k,'^[A-Za-z_][A-Za-z0-9_]*$'))continue;
                if(k==name)out[path+'.'+k]=en.Value;else FindNamed(en.Value,name,path+'.'+k,out,depth+1);
            }
        }else if(value.GetType().GetInterface('System.Collections.IList')!=null){var list:IList=IList(value);for(var i=0;i<list.Count && i<100;i++)FindNamed(list[i],name,path+'['+i+']',out,depth+1);}
    }
    static function ServerInspection(body:String):Hashtable {
        var out=new Hashtable();var root:IDictionary=ServerObject(body);out['parsed']=root!=null;
        if(root==null)return out;
        var names=new ArrayList();var en:IDictionaryEnumerator=root.GetEnumerator();while(en.MoveNext())if(Regex.IsMatch(Text(en.Key),'^[a-zA-Z_][a-zA-Z_0-9]*$'))names.Add(Text(en.Key));out['keys']=names;
        var selected=new Hashtable();var selectedNames:String[]='author,title,desc,nick_name,create_timestamp,ori_create_time'.Split(',');
        for(var si:int=0;si<selectedNames.Length;si++){var selectedName:String=selectedNames[si];if(!root.Contains(selectedName))continue;var selectedValue:String=Text(root[selectedName]);if(selectedValue.Length>500)selectedValue=selectedValue.Substring(0,500);selected[selectedName]=selectedValue;}
        out['selected']=selected;
        var stats:IDictionary=root['appmsg_bar_data']==null?null:IDictionary(root['appmsg_bar_data']);var fields=new Hashtable();
        if(stats!=null)Walk(stats,'$.cgiDataNew.appmsg_bar_data','',fields,0);
        var resp:IDictionary=root['base_resp']==null?null:IDictionary(root['base_resp']);if(resp!=null)Walk(resp,'$.cgiDataNew.base_resp','',fields,0);
        var extra:String[]='appmsg_ext_get,front_end_additional_fields'.Split(',');for(var ei=0;ei<extra.Length;ei++)if(root[extra[ei]]!=null)Walk(root[extra[ei]],'$.cgiDataNew.'+extra[ei],'',fields,0);
        var link:String=System.Net.WebUtility.HtmlDecode(Text(root['link']));var identity=new Hashtable();identity['biz']=Field(link,'__biz');identity['mid']=Field(link,'mid');identity['idx']=Field(link,'idx');identity['sn']=Field(link,'sn');out['public_identity']=identity;
        var bars=new Hashtable();FindNamed(root,'appmsg_bar_data','$.cgiDataNew',bars,0);var be:IDictionaryEnumerator=bars.GetEnumerator();while(be.MoveNext())Walk(be.Value,Text(be.Key),'',fields,0);
        out['fields']=fields;out['gallery_html']=GalleryContent(body);
        return out;
    }
    static function SourceHints(body: String): ArrayList {
        var hints=new ArrayList();var scripts=Regex.Matches(body,'<script\\b[^>]*>([\\s\\S]*?)</script>',RegexOptions.IgnoreCase);
        var allowed='^(getappmsgext|getAppMsgExt|appmsg_comment|getAppMsgComment|fetch|invoke|request|appmsgstat|read_num|like_num|star_num|like_count|collect_count|share_count|comment_count|old_like_num|GetAppMsgExt|GetAppMsgComment|/mp/getappmsgext|/mp/appmsg_comment|picture_page_info_list|picture_page_info|cdn_url|image_url|msg_cdn_url|img_url|picture_page|appmsg_bar_data)$';
        for(var i=0;i<scripts.Count;i++){
            var tokens=Regex.Matches(scripts[i].Groups[1].Value,'/\\*[\\s\\S]*?\\*/|//[^\\n]*|"(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\'|`(?:\\\\.|[^`\\\\])*`|[A-Za-z_$][A-Za-z0-9_$]*|\\d+(?:\\.\\d+)?|[^\\s]');
            var safe=new ArrayList();
            for(var j=0;j<tokens.Count;j++){
                var t:String=tokens[j].Value;
                if(t.StartsWith('//') || t.StartsWith('/*'))continue;
                if(t.StartsWith('"') || t.StartsWith("'") || t.StartsWith('`')){
                    if(t.Length<2){safe.Add("[quote]");continue;}
                    var inner:String=t.Substring(1,t.Length-2);
                    safe.Add(Regex.IsMatch(inner,allowed) ? '"'+inner+'"' : '"[string]"');
                } else if(Regex.IsMatch(t,'^\\d'))safe.Add('[number]');
                else safe.Add(t);
            }
            var perKey=new Hashtable();
            for(var k:int=0;k<safe.Count;k++){
                var key:String=Text(safe[k]).Trim('"');
                if(!Regex.IsMatch(key,allowed) || key=='fetch' || key=='invoke' || key=='request')continue;
                var count:int=perKey.ContainsKey(key)?Convert.ToInt32(perKey[key]):0;
                if(count>=6 || hints.Count>=100)continue;perKey[key]=count+1;
                var excerpt=new System.Text.StringBuilder();
                var start:int=k-32;if(start<0)start=0;var stop:int=k+48;if(stop>safe.Count)stop=safe.Count;
                for(var n:int=start;n<stop;n++)excerpt.Append(Text(safe[n])).Append(' ');
                var h=new Hashtable();h['key']=key;h['script_index']=i;h['code']=excerpt.ToString();var prefix=new System.Text.StringBuilder();var pl:int=safe.Count<70?safe.Count:70;for(var pi=0;pi<pl;pi++)prefix.Append(Text(safe[pi])).Append(' ');h['script_prefix']=prefix.ToString();hints.Add(h);
            }
        }
        return hints;
    }
    static function BootstrapFields(body: String): Hashtable {
        var result=new Hashtable();
        var scripts=Regex.Matches(body,'<script\\b[^>]*>([\\s\\S]*?)</script>',RegexOptions.IgnoreCase);
        var allowed='^(read_num|old_like_count|like_count|share_count|collect_count|comment_count|star_num|get_data_succ|show_collect|show_like|show_old_like|show_share|show_read|comment_enabled|show_star)$';
        for(var si=0;si<scripts.Count;si++){
            var matches=Regex.Matches(scripts[si].Groups[1].Value,'/\\*[\\s\\S]*?\\*/|//[^\\n]*|"(?:\\\\.|[^"\\\\])*"|\'(?:\\\\.|[^\'\\\\])*\'|`(?:\\\\.|[^`\\\\])*`|[A-Za-z_$][A-Za-z0-9_$]*|\\d+(?:\\.\\d+)?|[^\\s]');
            var tokens=new ArrayList();
            for(var ti=0;ti<matches.Count;ti++){
                var token:String=matches[ti].Value;
                if(!token.StartsWith('//') && !token.StartsWith('/*'))tokens.Add(token);
            }
            for(var i:int=0;i<tokens.Count-4;i++){
                var ni:int=-1;
                if(Text(tokens[i])=='var')ni=i+1;
                if(Text(tokens[i])=='window' && Text(tokens[i+1])=='.')ni=i+2;
                if(ni<0)continue;
                var scope:String=Text(tokens[ni]);
                if((scope!='appmsg_bar_data' && scope!='cgiData') || Text(tokens[ni+1])!='=' || Text(tokens[ni+2])!='{')continue;
                var depth:int=1;
                for(var j:int=ni+3;j<tokens.Count && depth>0;j++){
                    var key:String=Text(tokens[j]);
                    if(key=='{'){depth++;continue;}if(key=='}'){depth--;continue;}
                    if(depth!=1 || !Regex.IsMatch(key,allowed) || j+3>=tokens.Count || Text(tokens[j+1])!=':')continue;
                    var vi:int=j+2;var raw:String=Text(tokens[vi]);
                    if(raw=='-' && vi+1<tokens.Count){vi++;raw='-'+Text(tokens[vi]);}
                    if(raw.Length>=2 && ((raw.StartsWith('"') && raw.EndsWith('"')) || (raw.StartsWith("'") && raw.EndsWith("'"))))raw=raw.Substring(1,raw.Length-2);
                    if(!Regex.IsMatch(raw,'^(?:-?\\d{1,15}|true|false)?$'))continue;
                    var after:int=vi+1;
                    if(after+1<tokens.Count && Text(tokens[after])=='*' && Text(tokens[after+1])=='1')after+=2;
                    if(after>=tokens.Count || (Text(tokens[after])!=',' && Text(tokens[after])!='}'))continue;
                    var entry=new Hashtable();entry['raw_value']=raw;entry['source']='server_html_inline_literal';
                    if(Regex.IsMatch(raw,'^-?\\d+$'))entry['value']=Int64.Parse(raw);
                    entry['empty_literal']=raw=='';entry['script_index']=si;
                    var path:String='$html.'+scope+'.'+key;
                    if(!result.ContainsKey(path))result[path]=new ArrayList();
                    ArrayList(result[path]).Add(entry);
                }
            }
        }
        var server:IDictionary=ServerObject(body);if(server!=null){var user:IDictionary=server['user_info']==null?null:IDictionary(server['user_info']);var bar:IDictionary=user==null||user['appmsg_bar_data']==null?null:IDictionary(user['appmsg_bar_data']);if(bar!=null){var keys:String[]='read_num,old_like_count,like_count,share_count,collect_count,comment_count'.Split(',');for(var bi=0;bi<keys.Length;bi++){var serverRaw:String=Text(bar[keys[bi]]);if(bar.Contains(keys[bi]) && Regex.IsMatch(serverRaw,'^-?\\d{1,15}$')){var item=new Hashtable();item['raw_value']=serverRaw;item['value']=Int64.Parse(serverRaw);item['source']='server_html_inline_literal';var list=new ArrayList();list.Add(item);result['$html.cgiDataNew.user_info.appmsg_bar_data.'+keys[bi]]=list;}}}}
        return result;
    }
    static function InspectHtml(body: String): Hashtable {
        var result=new Hashtable();var fields=new Hashtable();var mentions=new Hashtable();var candidates=new ArrayList();var assignments=new Hashtable();
        var ids=new ArrayList();var idMatches=Regex.Matches(body,'\\bid=["\']([A-Za-z_][A-Za-z0-9_-]{0,80})["\']');for(var ii=0;ii<idMatches.Count;ii++)if(!ids.Contains(idMatches[ii].Groups[1].Value))ids.Add(idMatches[ii].Groups[1].Value);result['element_ids']=ids;
        result['format']='html';result['server_object']=ServerInspection(body);result['bootstrap_fields']=BootstrapFields(body);try{result['source_hints']=SourceHints(body);}catch(e){result['source_hints_error']=e.GetType().Name;}result['numeric_literals_unverified']=candidates;result['json_assignments']=assignments;result['field_mentions']=mentions;
        var keys: String[]='read_num,read_count,old_like_num,like_num,like_count,share_num,share_count,collect_num,collect_count,star_num,favorite_count,comment_count,comment_num,comment_id,elected_comment_total_cnt,appmsgstat,getappmsgext,appmsg_comment'.Split(',');
        for(var i=0;i<keys.Length;i++){
            var n:int=Regex.Matches(body,'\\b'+Regex.Escape(keys[i])+'\\b').Count;
            if(n>0)mentions[keys[i]]=n;
        }
        var literals=Regex.Matches(body,'["\'\\b](read_num|read_count|old_like_num|old_like_count|like_num|like_count|share_num|share_count|collect_num|collect_count|star_num|favorite_count|comment_count|comment_num|elected_comment_total_cnt)["\']?\\s*[:=]\\s*["\']?(-?\\d{1,15})["\']?\\s*[,;}]');
        for(var li=0;li<System.Math.Min(literals.Count,100);li++){
            var c=new Hashtable();c['key']=literals[li].Groups[1].Value;c['value']=Int64.Parse(literals[li].Groups[2].Value);c['offset']=literals[li].Index;candidates.Add(c);
        }
        var objects=Regex.Matches(body,'(?:var\\s+|window\\.)([A-Za-z_][A-Za-z0-9_]*)\\s*=\\s*(\\{[\\s\\S]*?\\})\\s*;');
        for(var oi=0;oi<objects.Count;oi++){
            var inspected:IDictionary=IDictionary(Fiddler.WebFormats.JSON.JsonDecode(InspectBody(objects[oi].Groups[2].Value)).JSONObject);
            if(inspected['format']=='json')assignments[objects[oi].Groups[1].Value]=inspected;
        }
        return result;
    }
    static function ProcessName(s: Session): String {
        var p: String=Text(s['x-processinfo']).ToLower();
        var m=Regex.Match(p,'^(wechatappex|weixin|wechat|chrome)(?::\\d+)?$');
        return m.Success ? p : 'other_or_unknown';
    }
    static function AddCount(counts: Hashtable, key: String): void {counts[key]=counts.ContainsKey(key) ? Convert.ToInt32(counts[key])+1 : 1;}
    static function SessionRecord(s: Session, config: IDictionary): Hashtable {
        var r=new Hashtable();
        r['session_id']=s.id;r['process']=ProcessName(s);r['process_id']=s.LocalProcessID;
        r['host']=s.hostname;r['method']=s.RequestMethod;r['http_status']=s.responseCode;
        r['started_at']=s.Timers.ClientBeginRequest.ToUniversalTime().ToString('o');
        r['response_bytes']=s.responseBodyBytes==null?0:s.responseBodyBytes.Length;
        var path: String=new Uri(s.fullUrl).AbsolutePath;
        r['path']=Regex.IsMatch(path,'^/(?:mp|cgi-bin)/[a-zA-Z_0-9/-]{1,100}$') ? path : (path=='/s' || path.StartsWith('/s/') ? '/s' : '[other]');
        r['request_cookie_present']=s.oRequest.headers.Exists('Cookie');
        r['content_type']=Text(s.oResponse.headers['Content-Type']).Split(';')[0];
        var query: String=new Uri(s.fullUrl).Query.TrimStart('?');
        if(s.requestBodyBytes!=null && s.requestBodyBytes.Length<1048576 && Text(s.oRequest.headers['Content-Type']).StartsWith('application/x-www-form-urlencoded'))query+='&'+s.GetRequestBodyAsString();
        var names=new ArrayList();var parts: String[]=query.Split('&');
        for(var i=0;i<parts.Length;i++){
            var k: String=parts[i].Split('=')[0];
            if(Regex.IsMatch(k,'^[A-Za-z_][A-Za-z0-9_]{0,63}$') && !names.Contains(k))names.Add(k);
        }
        r['request_parameter_names']=names;
        var b: String=Field(query,'__biz');if(b=='')b=Field(query,'biz');
        var m: String=Field(query,'mid');if(m=='')m=Field(query,'appmsgid');
        var x: String=Field(query,'idx');
        var origin: String='request';var body: String='';
        if(s.responseBodyBytes!=null && s.responseBodyBytes.Length>0 && s.responseBodyBytes.Length<=2097152)body=s.GetResponseBodyAsString();
        if(path=='/s' || path.StartsWith('/s/')){
            var hb: String=JS(body,'biz');if(hb=='')hb=JS(body,'__biz');
            var hm: String=JS(body,'mid');if(hm=='')hm=JS(body,'appmsgid');
            var hx: String=JS(body,'idx');
            if(hb!='' && hm!='' && hx!=''){b=hb;m=hm;x=hx;origin='article_html';}
        }
        var matched: Boolean=b==Text(config['target_biz']) && ((config['diagnostic_all_articles']==true && Regex.IsMatch(m,'^\\d+$') && Regex.IsMatch(x,'^[1-9]\\d*$')) || (m==Text(config['diagnostic_mid']) && x==Text(config['diagnostic_idx'])));
        r['target_match']=matched;r['identity_source']=origin;
        if(matched){
            var identity=new Hashtable();identity['biz']=b;identity['mid']=m;identity['idx']=x;
            var sn: String=Field(query,'sn');if(sn=='')sn=JS(body,'sn');
            if(Regex.IsMatch(sn,'^[a-fA-F0-9]{32}$'))identity['sn']=sn;
            var comment: String=Field(query,'comment_id');if(comment=='')comment=JS(body,'comment_id');
            if(Regex.IsMatch(comment,'^\\d{1,30}$'))identity['comment_id']=comment;
            r['identity']=identity;
            if(path=='/s' || path.StartsWith('/s/')){var hi=new Hashtable();hi['biz']=JS(body,'biz');hi['mid']=JS(body,'mid');hi['idx']=JS(body,'idx');hi['sn']=JS(body,'sn');r['html_identity']=hi;}
            r['response_inspection']=(path=='/s' || path.StartsWith('/s/')) ? InspectHtml(body) : Fiddler.WebFormats.JSON.JsonDecode(InspectBody(body)).JSONObject;
        }
        return r;
    }
    static function BootstrapEvent(s: Session, body: String, article: IDictionary): Hashtable {
        if(s.responseCode!=200 || !Regex.IsMatch(ProcessName(s),'^(wechatappex|weixin|wechat):'))return null;
        var server:IDictionary=ServerObject(body);var link:String=server==null?'':System.Net.WebUtility.HtmlDecode(Text(server['link']));
        var identity=new Hashtable();var names:String[]='biz,mid,idx,sn'.Split(',');
        for(var i=0;i<names.Length;i++){
            var k:String=names[i];identity[k]=JS(body,k);var fromLink:String=Field(link,k=='biz'?'__biz':k);if(Text(identity[k])=='')identity[k]=fromLink;else if(fromLink!='' && fromLink!=Text(identity[k]))return null;
            if(Text(identity[k])=='' || Text(identity[k])!=Text(article[k]))return null;
        }
        var fields:Hashtable=BootstrapFields(body);if(fields.Count==0)return null;
        var e=new Hashtable();var keys:String[]='schema_version,captured_at,source,endpoint,biz,mid,idx,sn'.Split(',');
        for(var j=0;j<keys.Length;j++)e[keys[j]]=article[keys[j]];
        e['kind']='metrics';e['event_id']=Text(article['event_id'])+'-bootstrap';
        e['metric_format']='client_html_bootstrap_v1';e['bootstrap_fields']=fields;
        var resp:IDictionary=server==null||server['base_resp']==null?null:IDictionary(server['base_resp']);
        if(resp!=null && resp.Contains('ret')){
            var retText:String=Text(resp['ret']);
            if(!Regex.IsMatch(retText,'^-?\\d+$'))throw new Exception('invalid_cgidatanew_business_ret');
            try{e['business_ret']=Convert.ToInt32(retText);}catch(_){throw new Exception('invalid_cgidatanew_business_ret');}
        }
        e['html_identity']=identity;e['http_status']=s.responseCode;e['session_id']=s.id;e['process']=ProcessName(s);
        var comment:String=JS(body,'comment_id');
        if(Regex.IsMatch(comment,'^\\d{1,30}$'))e['comment_id']=comment;
        return e;
    }
    static function Record(s: Session): void {
        try {
            if(!File.Exists(ControlPath))return;
            var config: IDictionary=IDictionary(Fiddler.WebFormats.JSON.JsonDecode(File.ReadAllText(ControlPath)).JSONObject);
            if(config['diagnostic_enabled']!=true || config['diagnostic_expires_at']==null || DateTime.Parse(Text(config['diagnostic_expires_at'])).ToUniversalTime()<=DateTime.UtcNow)return;
            var after: int=config.Contains('diagnostic_after_id')?Convert.ToInt32(config['diagnostic_after_id']):0;
            if(s.id<=after)return;
            var record: Hashtable=SessionRecord(s,config);
            File.AppendAllText(
                LiveOutputPath,
                Fiddler.WebFormats.JSON.JsonEncode(record)+Environment.NewLine,
                new System.Text.UTF8Encoding(false)
            );
        }catch(_){
            // Diagnostics must never alter the primary capture path.
        }
    }
    static function Export(): void {
        if(!File.Exists(ControlPath))return;
        var config: IDictionary=IDictionary(Fiddler.WebFormats.JSON.JsonDecode(File.ReadAllText(ControlPath)).JSONObject);
        if(config['diagnostic_enabled']!=true || config['diagnostic_expires_at']==null || DateTime.Parse(Text(config['diagnostic_expires_at'])).ToUniversalTime()<=DateTime.UtcNow)return;
        var after: int=config.Contains('diagnostic_after_id')?Convert.ToInt32(config['diagnostic_after_id']):0;
        var result=new Hashtable();var sessions=new ArrayList();var counts=new Hashtable();var errors=new Hashtable();
        var all: Session[]=FiddlerApplication.UI.GetAllSessions();var maxId:int=0;
        for(var i=0;i<all.Length;i++){
            var s:Session=all[i];if(s.id>maxId)maxId=s.id;if(s.id<=after)continue;
            AddCount(counts,ProcessName(s));
            var nativeClient:Boolean=Regex.IsMatch(ProcessName(s),'^(wechatappex|weixin|wechat):');
            if(!s.HostnameIs('mp.weixin.qq.com') && !nativeClient)continue;
            if(sessions.Count>=1000)continue;
            try{sessions.Add(SessionRecord(s,config));}catch(e){AddCount(errors,e.GetType().Name);}
        }
        result['captured_at']=DateTime.UtcNow.ToString('o');result['after_session_id']=after;result['max_session_id']=maxId;
        result['process_session_counts']=counts;result['sessions']=sessions;result['errors']=errors;
        result['fiddler_listen_port']=CONFIG.ListenPort;
        result['https_decryption_enabled']=CONFIG.DecryptHTTPS;
        File.WriteAllText(OutputPath,Fiddler.WebFormats.JSON.JsonEncode(result));
    }
}
