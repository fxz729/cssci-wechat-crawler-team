// Data-only observer for Fiddler Classic. No proxy, certificate or login changes.
import System;
import System.IO;
import System.Collections;
import System.Text.RegularExpressions;
import Fiddler;

class CssciCapture {
    static var ControlPath: String = "__CSSCI_CONTROL_PATH__";
    static var InboxPath: String = "__CSSCI_INBOX_PATH__";
    static var Seen: Hashtable = new Hashtable();
    static var Errors: Hashtable = new Hashtable();
    static var Eligible: int = 0;

    static function Text(v: Object): String { return v == null ? "" : Convert.ToString(v); }
    static function Decode(s: String): IDictionary {
        var result = Fiddler.WebFormats.JSON.JsonDecode(s);
        return result == null ? null : IDictionary(result.JSONObject);
    }
    static function Field(s: String, name: String): String {
        var m = Regex.Match(s, "(?:^|[?&])" + Regex.Escape(name) + "=([^&#]*)");
        return m.Success ? Uri.UnescapeDataString(m.Groups[1].Value.Replace("+", " ")) : "";
    }
    static function JS(s: String, name: String): String {
        var value:String=CssciDiagnostics.JS(s,name);
        if(value!='')return value;
        var m=Regex.Match(s,"\\b"+Regex.Escape(name)+"\\s*[:=]\\s*(\\d+)");
        return m.Success ? m.Groups[1].Value : "";
    }
    static function Meta(s: String, key: String): String {
        var tags = Regex.Matches(s, "<meta\\b[^>]*>", RegexOptions.IgnoreCase);
        for (var i=0; i<tags.Count; i++) {
            var t: String = tags[i].Value;
            if (!Regex.IsMatch(t, "(?:name|property)=[\"']"+Regex.Escape(key)+"[\"']", RegexOptions.IgnoreCase)) continue;
            var m = Regex.Match(t, "content=[\"']([^\"']*)[\"']", RegexOptions.IgnoreCase);
            if (m.Success) return System.Net.WebUtility.HtmlDecode(m.Groups[1].Value);
        }
        return "";
    }
    static function PublicUrl(url: String): String {
        if (!(url.StartsWith("https://") || url.StartsWith("http://"))) return "";
        var u:Uri=null;try{u=new Uri(System.Net.WebUtility.HtmlDecode(url));}catch(_){return '';}
        if(u==null || !u.IsAbsoluteUri || u.Host=='' || u.UserInfo!='')return '';
        var kept = new ArrayList();
        var parts: String[] = u.Query.TrimStart('?').Split('&');
        for (var pp=0; pp<parts.Length; pp++) { var part: String=parts[pp];
            if (part.Length == 0) continue;
            var name: String = Uri.UnescapeDataString(part.Split('=')[0].Replace('+',' ')).ToLowerInvariant();
            if (Regex.IsMatch(name, "^(cookie|authorization|token|appmsg_token|pass_ticket|key|uin|wxuin|wap_sid2|slave_sid|data_ticket|fingerprint|wxtoken|exportkey|poc_token)$") || name.EndsWith("_token")) continue;
            kept.Add(part);
        }
        return u.GetLeftPart(UriPartial.Path) + (kept.Count > 0 ? "?" + JoinParts(kept) : "");
    }
    static function JoinParts(parts: ArrayList): String { var result: String=""; for(var i=0;i<parts.Count;i++){if(i>0)result+="&";result+=Text(parts[i]);}return result; }
    static function BodyContent(html: String): String {
        var start = Regex.Match(html, "<div\\b[^>]*\\bid=[\"']js_content[\"'][^>]*>", RegexOptions.IgnoreCase);
        if (!start.Success) return "";
        var tags = Regex.Matches(html.Substring(start.Index), "</?div\\b[^>]*>", RegexOptions.IgnoreCase);
        var depth: int = 0; var end: int = 0;
        for (var i=0; i<tags.Count; i++) {
            depth += tags[i].Value.StartsWith("</") ? -1 : 1;
            if (depth == 0) { end = tags[i].Index + tags[i].Length; break; }
        }
        if (end <= 0) return "";
        var body: String = html.Substring(start.Index, end);
        body = Regex.Replace(body, "<(script|form|input|button)\\b[^>]*>[\\s\\S]*?</\\1>", "", RegexOptions.IgnoreCase);
        body = Regex.Replace(body, "\\s+on[a-z]+\\s*=\\s*([\"'])[\\s\\S]*?\\1", "", RegexOptions.IgnoreCase);
        body=Regex.Replace(body,"([?&](?:amp;)?)([A-Za-z0-9_%+.-]+)=([^&\"'<>\\s]*)",CssciCapture.CleanQueryComponent);
        return body;
    }
    static function CleanQueryComponent(m:Match):String {
        var name:String=Uri.UnescapeDataString(m.Groups[2].Value.Replace('+',' ')).ToLowerInvariant();
        if(Regex.IsMatch(name,'^(cookie|authorization|token|appmsg_token|pass_ticket|key|uin|wxuin|wap_sid2|slave_sid|data_ticket|fingerprint|wxtoken|exportkey|poc_token)$') || name.EndsWith('_token'))return m.Groups[1].Value+m.Groups[2].Value+'=';
        return m.Value;
    }
    static function HtmlTextById(body:String,id:String):String {
        var pattern:String="<[A-Za-z][^>]*\\bid=[\\\"']"+Regex.Escape(id)+"[\\\"'][^>]*>([\\s\\S]*?)</[A-Za-z][^>]*>";
        var m=Regex.Match(body,pattern,RegexOptions.IgnoreCase);if(!m.Success)return '';
        var value:String=Regex.Replace(m.Groups[1].Value,"<[^>]+>"," ");
        return System.Net.WebUtility.HtmlDecode(value).Trim();
    }
    static function ArticleMetadata(body:String,biz:String,mid:String,idx:String,sn:String):Hashtable {
        var a=new Hashtable();
        a['url']=Canonical(biz,mid,idx,sn);
        a['title']=JS(body,'msg_title');if(Text(a['title'])=='')a['title']=Meta(body,'og:title');
        a['author']=Meta(body,'author');
        a['account_name']=JS(body,'nickname');
        a['digest']=Meta(body,'description');
        a['cover_url']=PublicUrl(Meta(body,'og:image'));
        a['publish_time']=JS(body,'ct');
        var domAuthor:String=HtmlTextById(body,'js_author_name');if(domAuthor=='')domAuthor=HtmlTextById(body,'js_author');
        var domAccount:String=HtmlTextById(body,'js_name');if(domAccount=='')domAccount=HtmlTextById(body,'js_wx_follow_nickname');
        var server:IDictionary=CssciDiagnostics.ServerObject(body);
        if(server!=null){
            if(Text(a['title'])=='' && Text(server['title'])!='')a['title']=Text(server['title']);
            if(Text(server['author'])!='')a['author']=Text(server['author']);
            if(Text(server['desc'])!='')a['digest']=Text(server['desc']);
            if(Text(server['nick_name'])!='')a['account_name']=Text(server['nick_name']);
            if(Text(a['cover_url'])=='' && Text(server['cdn_url'])!='')a['cover_url']=PublicUrl(Text(server['cdn_url']));
            if(Text(a['publish_time'])=='' && Regex.IsMatch(Text(server['create_timestamp']),'^\d{10}$'))a['publish_time']=Text(server['create_timestamp']);
            if(Text(a['publish_time'])=='' && Regex.IsMatch(Text(server['ori_create_time']),'^\d{10}$'))a['publish_time']=Text(server['ori_create_time']);
            if(Text(server['req_id'])!='')a['req_id']=Text(server['req_id']);
            if(Text(server['comment_id'])!='')a['comment_id']=Text(server['comment_id']);
        }
        if(domAuthor!='')a['author']=domAuthor;
        if(domAccount!='')a['account_name']=domAccount;
        return a;
    }
    static function ArticleContent(body:String,biz:String,mid:String,idx:String,sn:String):Hashtable {
        var server:IDictionary=CssciDiagnostics.ServerObject(body);var link:String=server==null?'':System.Net.WebUtility.HtmlDecode(Text(server['link']));
        var present=new Hashtable();var expected=new Hashtable();expected['__biz']=biz;expected['mid']=mid;expected['idx']=idx;expected['sn']=sn;
        if(link!=''){
            var u:Uri=null;try{u=new Uri(link);}catch(_){return null;}
            if(u==null || !u.IsAbsoluteUri || u.UserInfo!='' || u.Host!='mp.weixin.qq.com' || (u.AbsolutePath!='/s'&&!u.AbsolutePath.StartsWith('/s/')))return null;
            var parts:String[]=u.Query.TrimStart('?').Split('&');
            for(var i=0;i<parts.Length;i++){
                var at:int=parts[i].IndexOf('=');if(at<0)continue;
                var name:String=Uri.UnescapeDataString(parts[i].Substring(0,at).Replace('+',' '));
                if(!expected.ContainsKey(name))continue;
                var value:String=Uri.UnescapeDataString(parts[i].Substring(at+1).Replace('+',' '));
                if(value!=Text(expected[name]))return null;present[name]=true;
            }
        }
        var content:String=BodyContent(body);var origin:String='html_fragment';
        var text:String=Regex.Replace(content,'<(script|style)\\b[^>]*>[\\s\\S]*?</\\1>|<!--[\\s\\S]*?-->|<[^>]+>','',RegexOptions.IgnoreCase);
        var meaningful:Boolean=Regex.IsMatch(content,'<(img|svg|audio|video|iframe|mpvoice|mp-common-videosnap)\\b',RegexOptions.IgnoreCase)||!Regex.IsMatch(System.Net.WebUtility.HtmlDecode(text),'^[\\s\\u200b\\uFEFF]*$');
        if(!meaningful){
            if(present.Count!=4)return null;
            content=CssciDiagnostics.GalleryContent(body);origin='cgiDataNew.picture_page_info_list';
        }
        if(content=='')return null;
        var result=new Hashtable();result['content_html']=content;result['content_origin']=origin;return result;
    }
    static function Canonical(biz: String, mid: String, idx: String, sn: String): String {
        return "https://mp.weixin.qq.com/s?__biz="+Uri.EscapeDataString(biz)+"&mid="+mid+"&idx="+idx+"&sn="+Uri.EscapeDataString(sn);
    }
    static function CopyFields(source: IDictionary, keys: String[]): Hashtable {
        var result = new Hashtable();
        if (source != null) for (var ki=0; ki<keys.Length; ki++) { var key: String=keys[ki]; if (source.Contains(key) && source[key] != null && Regex.IsMatch(Text(source[key]), "^\\d+(?:\\.\\d+)?[万亿]?\\+?$")) result[key] = source[key]; }
        return result;
    }
    static function Status(): void {
        var s = new Hashtable(); s['status']='loaded'; s['captured_at']=DateTime.UtcNow.ToString('o'); s['errors']=Errors; s['eligible']=Eligible; s['recorded']=Seen.Count;
        File.WriteAllText(InboxPath+'.status.json', Fiddler.WebFormats.JSON.JsonEncode(s));
    }
    static function ExportExisting(): void {
        var all: Session[] = FiddlerApplication.UI.GetAllSessions();
        for (var si=0; si<all.Length; si++) Record(all[si]);
        Status();
    }
    static function Record(s: Session): void {
        var stage: String="filter";
        try {
            if (!s.HostnameIs('mp.weixin.qq.com') || !File.Exists(ControlPath)) return;
            var path: String = new Uri(s.fullUrl).AbsolutePath;
            if (!(path == '/s' || path.StartsWith('/s/') || path == '/mp/profile_ext' || path == '/cgi-bin/appmsgpublish' || path == '/mp/getappmsgext' || path == '/mp/appmsg_comment')) return;
            Eligible++; stage="config";
            var config: IDictionary = Decode(File.ReadAllText(ControlPath));
            if (config == null || config['enabled'] != true) return;
            if (config['expires_at'] == null || DateTime.Parse(Text(config['expires_at'])).ToUniversalTime() <= DateTime.UtcNow) return;
            var target: String = Text(config['target_biz']);
            var eventId: String = s.id.ToString()+'-'+s.Timers.ClientBeginRequest.Ticks.ToString();
            if (Seen.ContainsKey(eventId)) return;
            if (s.responseBodyBytes == null || s.responseBodyBytes.Length == 0 || s.responseBodyBytes.Length > 8388608) return;
            var query: String = s.fullUrl;
            if (path == '/mp/getappmsgext') query += '&'+s.GetRequestBodyAsString();
            var biz: String = Field(query,'__biz');
            if (biz == '') biz=Field(query,'fakeid');
            stage="body";
            var body: String = s.GetResponseBodyAsString();
            if (path == '/s' || path.StartsWith('/s/')) {
                var actual: String = JS(body,'biz'); if(actual=='') actual=JS(body,'__biz');
                if(actual != '') biz=actual;
            }
            if (biz != target) return; stage="parse_target";
            var e = new Hashtable();
            e['schema_version']=1; e['event_id']=eventId; e['source']='fiddler_classic';
            e['captured_at']=s.Timers.ClientDoneResponse.ToUniversalTime().ToString('o');
            e['endpoint']=path; e['biz']=biz;
            if (path == '/s' || path.StartsWith('/s/')) {
                var mid: String=JS(body,'mid'); if(mid=='') mid=JS(body,'appmsgid'); if(mid=='') mid=Field(query,'mid');
                var idx: String=JS(body,'idx'); if(idx=='') idx=Field(query,'idx');
                var sn: String=JS(body,'sn'); if(sn=='') sn=Field(query,'sn');
                if (!Regex.IsMatch(mid,'^\\d+$') || !Regex.IsMatch(idx,'^[1-9]\\d*$')) return;
                e['kind']='article';e['mid']=mid;e['idx']=Int32.Parse(idx);e['sn']=sn;
                var extracted:Hashtable=ArticleContent(body,biz,mid,idx,sn);if(extracted==null)return;var content:String=Text(extracted['content_html']);var contentOrigin:String=Text(extracted['content_origin']);
                var a:Hashtable=ArticleMetadata(body,biz,mid,idx,sn);a['content_html']=content;a['content_origin']=contentOrigin;
                e['article']=a;
            } else {
                var root: IDictionary=Decode(body); if(root==null) return;
                var baseResp: IDictionary=IDictionary(root['base_resp']);
                var ret: int=baseResp!=null && baseResp.Contains('ret') ? Convert.ToInt32(baseResp['ret']) : 0;
                e['ret']=ret;
                if (path=='/mp/getappmsgext' || path=='/mp/appmsg_comment') {
                    var m: String=Field(query,'mid'); if(m=='')m=Field(query,'appmsgid');
                    var x: String=Field(query,'idx');
                    if (!Regex.IsMatch(m,'^\\d+$') || !Regex.IsMatch(x,'^[1-9]\\d*$')) return;
                    e['mid']=m; e['idx']=Int32.Parse(x); e['sn']=Field(query,'sn');
                    if(ret!=0){e['kind']='capability';e['articles']=new ArrayList();}
                    else { e['kind']='metrics';
                        var stat: IDictionary=IDictionary(root['appmsgstat']);
                        e['stats']=CopyFields(stat,['read_num','old_like_num','like_num','share_num','collect_num','star_num']);
                        e['comments']=path=='/mp/appmsg_comment' ? CopyFields(root,['total_count','elected_comment_total_cnt','comment_count']) : new Hashtable();
                    }
                } else {
                    e['kind']='catalog'; var items=new ArrayList(); e['articles']=items;
                    e['next_cursor']='';
                    if (path=='/mp/profile_ext' && ret==0) {
                        var general: IDictionary=typeof(root['general_msg_list']) == 'string' ? Decode(Text(root['general_msg_list'])) : IDictionary(root['general_msg_list']);
                        var list: IList=general!=null ? IList(general['list']) : null;
                        if(list!=null)for(var k=0;k<list.Count;k++){
                            var msg: IDictionary=IDictionary(list[k]); var main: IDictionary=IDictionary(msg['app_msg_ext_info']);
                            if(main==null)continue; AddCatalog(items,main,biz,'content_url');
                            var sub: IList=IList(main['multi_app_msg_item_list']);
                            if(sub!=null)for(var j=0;j<sub.Count;j++)AddCatalog(items,IDictionary(sub[j]),biz,'content_url');
                        }
                        if(root.Contains('can_msg_continue'))e['has_more']=Convert.ToInt32(root['can_msg_continue'])==1;
                        e['next_cursor']=Text(root['next_offset']);
                    }
                    if (path=='/cgi-bin/appmsgpublish' && ret==0) {
                        var page: IDictionary=typeof(root['publish_page']) == 'string' ? Decode(Text(root['publish_page'])) : IDictionary(root['publish_page']);
                        var pubs: IList=page!=null ? IList(page['publish_list']) : null;
                        if(pubs!=null)for(var pi=0;pi<pubs.Count;pi++){
                            var pub: IDictionary=IDictionary(pubs[pi]); var info: IDictionary=Decode(System.Net.WebUtility.HtmlDecode(Text(pub['publish_info'])));
                            if(info==null)continue; var arts: IList=IList(info['appmsgex']);
                            if(arts==null || arts.Count==0)arts=IList(info['appmsg_info']);
                            if(arts!=null)for(var ai=0;ai<arts.Count;ai++)AddCatalog(items,IDictionary(arts[ai]),biz,'link');
                        }
                    }
                }
            }
            File.AppendAllText(InboxPath,Fiddler.WebFormats.JSON.JsonEncode(e)+Environment.NewLine,new System.Text.UTF8Encoding(false));
            if(e['kind']=='article'){
                stage='bootstrap_metrics';
                var bootstrap:Hashtable=CssciDiagnostics.BootstrapEvent(s,body,e);
                if(bootstrap!=null)File.AppendAllText(InboxPath,Fiddler.WebFormats.JSON.JsonEncode(bootstrap)+Environment.NewLine,new System.Text.UTF8Encoding(false));
            }
            Seen[eventId]=true;
        } catch (ex) {
            var code: String = stage;
            Errors[code] = Errors.ContainsKey(code) ? Convert.ToInt32(Errors[code])+1 : 1;
        }
    }
    static function AddCatalog(items: ArrayList,a: IDictionary,biz: String,urlKey: String): void {
        if(a==null)return; var url: String=PublicUrl(Text(a[urlKey])); if(url=='')url=PublicUrl(Text(a['content_url']));
        var actual: String=Field(url,'__biz'); if(actual!='' && actual!=biz)return;
        var mid: String=Field(url,'mid'); if(mid=='')mid=Text(a['appmsgid']);
        var idx: String=Field(url,'idx'); if(idx=='')idx=Text(a['itemidx']);
        if(!Regex.IsMatch(mid,'^\\d+$') || !Regex.IsMatch(idx,'^[1-9]\\d*$'))return;
        var item=new Hashtable(); item['biz']=biz; item['mid']=mid; item['idx']=Int32.Parse(idx); item['sn']=Field(url,'sn');
        item['url']=url; item['title']=Text(a['title']); item['publish_time']=''; items.Add(item);
    }
}



