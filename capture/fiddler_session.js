// Review-only helper. After explicit user approval, an existing OnDone handler
// may call CssciSessionCapture.Record(s). This file does not install a handler.
import System;
import System.IO;
import System.Text;
import System.Collections;
import System.Diagnostics;
import System.Security.Principal;
import System.Security.AccessControl;
import System.Text.RegularExpressions;
import System.Threading;
import Fiddler;

class CssciSessionCapture {
    static var Root: String = "__CSSCI_SESSION_ROOT__";
    static var LastError: String = ""; // codes only; never session/error text
    static var LastCandidate: Hashtable = null; // non-secret metadata only
    static var Gate: Object = new Object();
    static var HealthGate: Object = new Object();
    static var HealthSnapshot: Hashtable = null; // immutable after publication
    static var RoundKey: String = "";
    static var Seen: Hashtable = new Hashtable();
    static var Homes: ArrayList = new ArrayList();
    static var Epoch: DateTime = new DateTime(1970, 1, 1, 0, 0, 0, DateTimeKind.Utc);

    static function Text(v: Object): String { return v == null ? "" : Convert.ToString(v); }
    static function Seconds(d: DateTime): double { return (d.ToUniversalTime() - Epoch).TotalSeconds; }
    static function Trusted(sid: String): boolean {
        return sid == WindowsIdentity.GetCurrent().User.Value || sid == "S-1-5-18" || sid == "S-1-5-32-544";
    }
    static function Check(path: String): void {
        if (!Path.IsPathRooted(path) || path.StartsWith("\\\\") || path.IndexOf(':', 2) >= 0 ||
            Regex.IsMatch(path, "(^|[\\\\/])\\.\\.?([\\\\/]|$)")) throw new Exception("permission_denied");
        var first: boolean = true;
        var current: String = path;
        while (current != null && current != "") {
            var attrs: FileAttributes = File.GetAttributes(current);
            if ((attrs & FileAttributes.ReparsePoint) != 0) throw new Exception("permission_denied");
            var acl: FileSystemSecurity;
            if ((attrs & FileAttributes.Directory) != 0) acl = Directory.GetAccessControl(current);
            else acl = File.GetAccessControl(current);
            var owner: String = acl.GetOwner(SecurityIdentifier).Value;
            if (!Trusted(owner) && (first || owner != "S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464"))
                throw new Exception("permission_denied");
            var raw: RawSecurityDescriptor = new RawSecurityDescriptor(acl.GetSecurityDescriptorBinaryForm(), 0);
            if (raw.DiscretionaryAcl == null || raw.DiscretionaryAcl.Count == 0) throw new Exception("permission_denied");
            for (var ai: int = 0; ai < raw.DiscretionaryAcl.Count; ai++) {
                var ace: GenericAce = raw.DiscretionaryAcl[ai];
                if (ace.AceType != AceType.AccessAllowed && ace.AceType != AceType.AccessDenied)
                    throw new Exception("permission_denied");
            }
            var rules: AuthorizationRuleCollection = acl.GetAccessRules(true, true, SecurityIdentifier);
            for (var i: int = 0; i < rules.Count; i++) {
                var rule: FileSystemAccessRule = FileSystemAccessRule(rules[i]);
                if (!first && (rule.PropagationFlags & PropagationFlags.InheritOnly) != 0) continue;
                if (rule.AccessControlType == AccessControlType.Allow && !Trusted(rule.IdentityReference.Value)) {
                    if (first || (Convert.ToInt64(rule.FileSystemRights) & 0x500D0040) != 0)
                        throw new Exception("permission_denied");
                }
            }
            first = false;
            current = Path.GetDirectoryName(current);
        }
    }
    static function Decode(value: String): IDictionary {
        var decoded = Fiddler.WebFormats.JSON.JsonDecode(value);
        return decoded == null ? null : IDictionary(decoded.JSONObject);
    }
    static function Control(): IDictionary {
        Check(Root);
        var path: String = Path.Combine(Root, "control.json");
        Check(path);
        if (new FileInfo(path).Length > 4096) throw new Exception("control_invalid");
        var control: IDictionary = Decode(File.ReadAllText(path, Encoding.UTF8));
        Check(path);
        if (control == null || Text(control["schema"]) != "1" || !(control["enabled"] != null && control["enabled"].GetType().FullName == "System.Boolean") ||
            !Convert.ToBoolean(control["enabled"]) || !Regex.IsMatch(Text(control["run_id"]), "^[0-9a-f]{32}$") ||
            !Regex.IsMatch(Text(control["generation"]), "^[1-9][0-9]*$") ||
            !Regex.IsMatch(Text(control["target_biz"]), "^[A-Za-z0-9_=-]{1,200}$")) return null;
        var now: double = Seconds(DateTime.UtcNow);
        var begin: double = Convert.ToDouble(control["armed_at"]);
        var end: double = Convert.ToDouble(control["deadline"]);
        if (!(begin <= now && now < end && end - begin >= 1 && end - begin <= 300)) return null;
        return control;
    }
    static function Key(c: IDictionary): String {
        return Text(c["run_id"]) + ":" + Text(c["generation"]) + ":" + Text(c["target_biz"]) +
            ":" + Text(c["armed_at"]) + ":" + Text(c["deadline"]);
    }
    static function Query(u: Uri): Hashtable {
        var result: Hashtable = new Hashtable();
        var parts: String[] = u.Query.TrimStart('?').Split('&');
        for (var i: int = 0; i < parts.Length; i++) {
            if (parts[i] == "") continue;
            var pos: int = parts[i].IndexOf('=');
            if (pos < 1 || Regex.IsMatch(parts[i], "%(?![0-9a-fA-F]{2})")) return null;
            // Literal '+' is a URL token, not form whitespace.
            var name: String = Uri.UnescapeDataString(parts[i].Substring(0, pos));
            var value: String = Uri.UnescapeDataString(parts[i].Substring(pos + 1));
            if (result.ContainsKey(name) || Regex.IsMatch(name + value, "[\\x00-\\x20\\x7f]")) return null;
            result[name] = value;
        }
        return result;
    }
    static function SessionParams(q: IDictionary): Hashtable {
        var result: Hashtable = new Hashtable();
        var names: String[] = "uin,key,pass_ticket,appmsg_token,wxtoken,sessionparams".Split(',');
        for (var i: int = 0; i < names.Length; i++)
            if (q.Contains(names[i])) result[names[i]] = q[names[i]];
        return result;
    }
    static function Associated(home: IDictionary, referer: String, params: IDictionary, headers: IDictionary): boolean {
        var original: IDictionary = IDictionary(home["sessionparams"]);
        var shared: Hashtable = new Hashtable();
        var keys: IEnumerator = params.Keys.GetEnumerator();
        while (keys.MoveNext()) {
            var k: String = Text(keys.Current);
            if (!original.Contains(k)) continue;
            if (Text(params[k]) != Text(original[k])) return false;
            shared[k] = true;
        }
        var byRef: boolean = referer != "" && referer == Text(home["url"]);
        var byParams: boolean = shared.ContainsKey("sessionparams") ||
            (shared.ContainsKey("uin") && (shared.ContainsKey("key") || shared.ContainsKey("pass_ticket")));
        return (byRef || byParams) && (referer == "" || byRef) &&
            Text(headers["user-agent"]) == Text(home["user_agent"]) && Text(headers["cookie"]) == Text(home["cookie"]);
    }
    static function RequestHeaders(s: Session): Hashtable {
        var result: Hashtable = new Hashtable();
        var seen: Hashtable = new Hashtable();
        var iterator: IEnumerator = s.oRequest.headers.GetEnumerator();
        while (iterator.MoveNext()) {
            var h: HTTPHeaderItem = HTTPHeaderItem(iterator.Current);
            var name: String = h.Name.ToLowerInvariant();
            if (seen.ContainsKey(name) || Regex.IsMatch(h.Value, "[\\x00-\\x1f\\x7f]")) return null;
            seen[name] = true;
            if (Regex.IsMatch(name, "^(proxy-authorization|proxy-connection|forwarded|x-forwarded-for|via)$")) return null;
            if (Regex.IsMatch(name, "^(host|user-agent|cookie|accept|accept-language|referer)$")) result[name] = h.Value;
        }
        if (Text(result["user-agent"]) == "" || Text(result["cookie"]) == "") return null;
        if (Text(result["host"]) != "" && Text(result["host"]).ToLowerInvariant() != "mp.weixin.qq.com") return null;
        return result;
    }
    static function CookieUpdates(s: Session): ArrayList {
        var result: ArrayList = new ArrayList();
        var iterator: IEnumerator = s.oResponse.headers.GetEnumerator();
        while (iterator.MoveNext()) {
            var h: HTTPHeaderItem = HTTPHeaderItem(iterator.Current);
            if (h.Name.ToLowerInvariant() != "set-cookie") continue; // Set-Cookie, never the HTML body
            if (h.Value.Length > 8192 || Regex.IsMatch(h.Value, "[\\r\\n]")) continue;
            if (Regex.IsMatch(h.Value, "^(wap_sid2|wxuin|wxtokenkey|appmsg_token|pass_ticket)=")) result.Add(h.Value);
            if (result.Count >= 32) break;
        }
        return result;
    }
    static function Publish(row: Hashtable, key: String): boolean {
        Check(Root);
        var destination: String = Path.Combine(Root, Text(row["event_id"]) + ".event.json");
        var temporary: String = Path.Combine(Root, Guid.NewGuid().ToString("N") + ".tmp");
        var bytes: byte[] = new UTF8Encoding(false).GetBytes(Fiddler.WebFormats.JSON.JsonEncode(row));
        if (bytes.Length > 131072) return false;
        var stream: FileStream = new FileStream(temporary, FileMode.CreateNew, FileAccess.Write, FileShare.None);
        try {
            Check(temporary); // empty file must be private BEFORE credentials are written
            stream.Write(bytes, 0, bytes.Length);
            stream.Flush(true);
        } finally { stream.Close(); }
        Check(temporary);
        var control: IDictionary = Control();
        if (control == null || Key(control) != key) return false;
        Check(Root);
        File.Move(temporary, destination); // exclusive, same-directory atomic publication
        Check(destination);
        return true;
    }
    static function SafeError(value: String): String {
        return Regex.IsMatch(value, "\\A(?:|capture_rejected|permission_denied|control_invalid|method_rejected|http_status_rejected|process_unavailable|process_rejected|query_rejected|action_rejected|headers_rejected|control_inactive|outside_capture_window|target_biz_mismatch|duplicate_or_capacity|urlcheck_unassociated|publish_rejected)\\z") ? value : "capture_rejected";
    }
    static function PublishHealth(): void {
        var row: Hashtable = new Hashtable();
        row["version"] = "cssci-session-health-v1";
        row["last_error"] = SafeError(LastError);
        var candidate: Hashtable = null;
        if (LastCandidate != null) candidate = Hashtable(LastCandidate.Clone());
        row["last_candidate"] = candidate;
        // Publish only an immutable reference under this separate short lock.
        if (!Monitor.TryEnter(HealthGate)) return;
        try { HealthSnapshot = row; } finally { Monitor.Exit(HealthGate); }
    }
    static function HandleHealth(s: Session): boolean {
        if (s == null || s.oRequest.headers.HTTPMethod != "GET" || s.fullUrl != "http://cssci-session.invalid/health") return false;
        // Bypass first: this exact synthetic endpoint must never reach DNS/upstream.
        s.utilCreateResponseAndBypassServer();
        s.oResponse.headers.HTTPResponseCode = 200;
        s.oResponse.headers.HTTPResponseStatus = "200 OK";
        s.oResponse.headers["Content-Type"] = "application/json; charset=utf-8";
        s.oResponse.headers["Cache-Control"] = "no-store";
        try {
            var row: Hashtable = null;
            var busy: boolean = !Monitor.TryEnter(HealthGate);
            if (!busy) {
                try { row = HealthSnapshot; } finally { Monitor.Exit(HealthGate); }
            }
            if (row == null) {
                row = new Hashtable();
                row["version"] = "cssci-session-health-v1";
                row["last_error"] = busy ? "health_busy" : SafeError(LastError);
                var empty: Hashtable = null;
                row["last_candidate"] = empty;
            }
            // No lock across serialization, and no competition for capture Gate.
            s.utilSetResponseBody(Fiddler.WebFormats.JSON.JsonEncode(row));
        } catch (ignored) {
            s.oResponse.headers.HTTPResponseCode = 500;
            s.oResponse.headers.HTTPResponseStatus = "500 Internal Server Error";
            s.utilSetResponseBody('{"version":"cssci-session-health-v1","last_error":"health_failed","last_candidate":null}');
        }
        return true;
    }
    static function Record(s: Session): void {
        // TryEnter avoids piling up OnDone callbacks. No sleeps, network or UI.
        if (!Monitor.TryEnter(Gate)) return;
        try { RecordCore(s); }
        catch (ignored) { LastError = SafeError(Text(ignored.Message)); if (LastError == "") LastError = "capture_rejected"; }
        finally {
            try { PublishHealth(); } catch (ignoredHealth) {}
            Monitor.Exit(Gate);
        }
    }
    static function RecordCore(s: Session): void {
        if (s == null) return;
        var u: Uri = null;
        try { u = new Uri(s.fullUrl); } catch (ignored) { return; }
        if (u.Scheme != "https" || u.Authority != "mp.weixin.qq.com" || u.UserInfo != "" || u.Fragment != "" ||
            (u.AbsolutePath != "/mp/profile_ext" && u.AbsolutePath != "/mp/urlcheck")) return;
        // Only profile-related candidates affect health; never retain request material.
        var candidate: Hashtable = new Hashtable();
        candidate["kind"] = "unknown"; candidate["http_status"] = s.responseCode;
        candidate["process_id"] = s.LocalProcessID; candidate["process_name"] = "";
        candidate["captured_at"] = Seconds(DateTime.UtcNow);
        LastCandidate = candidate; LastError = "capture_rejected";
        var q: Hashtable = Query(u);
        if (q == null) { LastError = "query_rejected"; return; }
        var kind: String = "";
        if (u.AbsolutePath == "/mp/profile_ext" && Text(q["action"]) == "home") kind = "home";
        else if ((u.AbsolutePath == "/mp/profile_ext" && Text(q["action"]) == "urlcheck") ||
                 (u.AbsolutePath == "/mp/urlcheck" && (!q.ContainsKey("action") || Text(q["action"]) == "urlcheck"))) kind = "urlcheck";
        else { LastError = "action_rejected"; return; }
        var headers: Hashtable = null;
        candidate["kind"] = kind;
        var processName: String = "";
        if (s.LocalProcessID > 0) {
            try {
                var proc: Process = Process.GetProcessById(s.LocalProcessID);
                try { processName = proc.ProcessName.ToLowerInvariant(); } finally { proc.Dispose(); }
            } catch (ignored) { processName = ""; }
        }
        candidate["process_name"] = processName;
        if (!s.HTTPMethodIs("GET")) { LastError = "method_rejected"; return; }
        if (s.responseCode != 200) { LastError = "http_status_rejected"; return; }
        if (s.LocalProcessID <= 0 || processName == "") { LastError = "process_unavailable"; return; }
        if (processName != "wechatappex") { LastError = "process_rejected"; return; }
        headers = RequestHeaders(s);
        if (headers == null) { LastError = "headers_rejected"; return; }
        var c: IDictionary = Control();
        if (c == null) { LastError = "control_inactive"; return; }
        var key: String = Key(c);
        if (key != RoundKey) { RoundKey = key; Seen.Clear(); Homes.Clear(); }
        var started: double = Seconds(s.Timers.ClientBeginRequest);
        var captured: double = Seconds(DateTime.UtcNow);
        if (!(Convert.ToDouble(c["armed_at"]) <= started && started <= captured && captured < Convert.ToDouble(c["deadline"]))) { LastError = "outside_capture_window"; return; }
        if (kind == "home" && Text(q["__biz"]) != Text(c["target_biz"])) { LastError = "target_biz_mismatch"; return; }
        if (kind == "urlcheck" && q.ContainsKey("__biz") && Text(q["__biz"]) != Text(c["target_biz"])) { LastError = "target_biz_mismatch"; return; }
        var id: String = s.LocalProcessID.ToString() + ":" + s.id.ToString() + ":" + s.Timers.ClientBeginRequest.Ticks.ToString();
        if (Seen.ContainsKey(id) || Seen.Count >= 64) { LastError = "duplicate_or_capacity"; return; }
        var referer: String = Text(headers["referer"]);
        var sessionparams: Hashtable = SessionParams(q);
        if (kind == "urlcheck") {
            var matches: int = 0;
            for (var i: int = 0; i < Homes.Count; i++) {
                var home: IDictionary = IDictionary(Homes[i]);
                if (Convert.ToInt32(home["process_id"]) != s.LocalProcessID) continue;
                if (Associated(home, referer, sessionparams, headers) &&
                    started >= Convert.ToDouble(home["started_at"])) matches++;
            }
            if (matches != 1) { LastError = "urlcheck_unassociated"; return; } // early/unrelated/ambiguous optional response is dropped
        }
        var request: StringBuilder = new StringBuilder("GET " + s.fullUrl + " HTTP/1.1\r\n");
        var names: String[] = "host,user-agent,cookie,accept,accept-language,referer".Split(',');
        for (var n: int = 0; n < names.Length; n++) {
            if (headers.ContainsKey(names[n])) {
                var label: String = names[n] == "user-agent" ? "User-Agent" : (names[n] == "cookie" ? "Cookie" : names[n]);
                request.Append(label).Append(": ").Append(Text(headers[names[n]])).Append("\r\n");
            }
        }
        request.Append("\r\n");
        var row: Hashtable = new Hashtable();
        row["schema"] = 1; row["event_id"] = Guid.NewGuid().ToString("N"); row["kind"] = kind;
        row["run_id"] = c["run_id"]; row["generation"] = c["generation"]; row["target_biz"] = c["target_biz"];
        row["started_at"] = started; row["captured_at"] = captured;
        row["process_name"] = "wechatappex"; row["process_id"] = s.LocalProcessID; row["session_id"] = s.id.ToString();
        row["request_text"] = request.ToString(); row["referer"] = referer; row["sessionparams"] = sessionparams;
        row["response_set_cookie"] = CookieUpdates(s);
        if (!Publish(row, key)) { LastError = "publish_rejected"; return; }
        Seen[id] = true;
        if (kind == "home") {
            var link: Hashtable = new Hashtable();
            link["process_id"] = s.LocalProcessID; link["url"] = s.fullUrl;
            link["sessionparams"] = sessionparams; link["started_at"] = started;
            link["user_agent"] = headers["user-agent"]; link["cookie"] = headers["cookie"];
            Homes.Add(link);
        }
        LastError = "";
    }
}
