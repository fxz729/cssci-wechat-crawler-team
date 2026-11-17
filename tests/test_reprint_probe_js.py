"""Offline contract tests: Node VM has only fake fetch, timers and browser globals."""

import json
import shutil
import subprocess
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "probe_reprint_search.js"
ENDPOINT = "https://mp.weixin.qq.com/cgi-bin/operate_appmsg"
SUB = "check_appmsg_copyright_stat"
BIZ = "MzTESTjournal=="
OTHER_BIZ = "MzOTHERjournal=="
# Deliberately synthetic sentinels, never taken from a session or capture file.
TOKEN = "TEST_ONLY_TOKEN_DO_NOT_EXPORT_47"
FINGERPRINT = "TEST_ONLY_FINGERPRINT_DO_NOT_EXPORT_93"
RANDOM = "TEST_ONLY_RANDOM_DO_NOT_EXPORT_61"
COOKIE = "TEST_ONLY_COOKIE_DO_NOT_EXPORT_28"
SECRETS = (TOKEN, FINGERPRINT, RANDOM, COOKIE)

NODE_HARNESS = r"""
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
const calls = [], logs = [], scheduled = [], fired = [], pending = new Map();
let uiActions = 0, nextTimer = 0, jsonCalls = 0;
const forbidden = new Proxy({}, {get() { uiActions++; throw new Error('UI forbidden'); }});
const sandbox = {
  URL, URLSearchParams, AbortController, AbortSignal,
  location: {origin: input.origin || 'https://mp.weixin.qq.com'},
  document: forbidden, localStorage: forbidden, sessionStorage: forbidden,
  console: Object.fromEntries(['log', 'info', 'warn', 'error', 'debug', 'table']
    .map(name => [name, (...args) => logs.push(args)])),
  setTimeout(fn, ms) {
    const id = ++nextTimer;
    scheduled.push(ms);
    pending.set(id, {fn, ms});
    setImmediate(() => {
      if (!pending.has(id)) return;
      pending.delete(id);
      fired.push(ms);
      fn();
    });
    return id;
  },
  clearTimeout(id) { pending.delete(id); },
  fetch: async (url, options) => {
    const signal = options && options.signal;
    calls.push({url: String(url), method: options.method, body: String(options.body),
      headers: Object.fromEntries(new Headers(options.headers).entries()),
      credentials: options.credentials, redirect: options.redirect, mode: options.mode,
      hasSignal: signal instanceof AbortSignal, initiallyAborted: signal?.aborted,
      signal});
    const response = input.responses[calls.length - 1];
    if (!response) throw new Error('Unexpected extra request');
    if (response.hang === 'fetch') return new Promise(() => {});
    if (response.throw) throw new Error(response.throw);
    const status = response.status === undefined ? 200 : response.status;
    return {status, ok: status >= 200 && status < 300,
      statusText: response.statusText || '',
      async json() {
        jsonCalls++;
        if (response.hang === 'json') return new Promise(() => {});
        if (response.jsonError) throw new Error(response.jsonError);
        return response.payload;
      }};
  }
};
vm.createContext(sandbox, {codeGeneration: {strings: false, wasm: false}});
(async () => {
  let error = null, result = null;
  try { vm.runInContext(input.source, sandbox, {timeout: 1000}); }
  catch (err) { error = String(err.message); }
  const callable = typeof sandbox.runReprintSearchProbe === 'function';
  const callsAtLoad = calls.length;
  const before = JSON.stringify(input.options);
  if (!error && callable && input.invoke !== false) {
    sandbox.__probeInput = input.options;
    try {
      result = await vm.runInContext('runReprintSearchProbe(__probeInput)', sandbox,
        {timeout: 1000});
    } catch (err) { error = String(err.message); }
  }
  process.stdout.write(JSON.stringify({callable, callsAtLoad, result, error, logs,
    uiActions, jsonCalls, scheduled, fired, activeTimers: pending.size,
    observedUnchanged: before === JSON.stringify(input.options),
    calls: calls.map(({signal, ...call}) => ({...call, finallyAborted: signal?.aborted}))}));
})().catch(() => { process.stderr.write('Offline harness failed'); process.exitCode = 1; });
"""


def observed_request():
    fields = [
        ("token", TOKEN), ("lang", "zh_CN"), ("f", "json"), ("ajax", "1"),
        ("fingerprint", FINGERPRINT), ("random", RANDOM),
        ("url", "previous query"), ("allow_reprint", "0"), ("begin", "0"),
        ("count", "10"),
    ]
    return {
        "method": "POST", "url": f"{ENDPOINT}?sub={SUB}",
        # A non-canonical untouched encoding catches whole-body reserialization.
        "body": urlencode(fields) + "&scene=already%2fobserved%20value",
    }


def public_url(mid=1, biz=BIZ, sn=None):
    return "https://mp.weixin.qq.com/s?" + urlencode({
        "__biz": biz, "mid": str(mid), "idx": "1", "sn": sn or f"{mid:032x}",
    })


def article(mid=1, biz=BIZ, **extra):
    return {
        "url": public_url(mid, biz), "title": f"论文{mid}", "nickname": "同名期刊",
        "author": "作者", "digest": "公开摘要", "content": "<p>公开内容</p>",
        "cover_url": "https://mmbiz.qpic.cn/mmbiz_png/public/640?wx_fmt=png",
        **extra,
    }


def ok(items, total=10000):
    return {"payload": {"base_resp": {"ret": 0, "err_msg": "ok"},
                        "total": total, "list": items}}


def run_probe(responses=(), *, invoke=True, origin=None, **overrides):
    node = shutil.which("node")
    assert node, "Node is required for these offline VM tests; do not install dependencies"
    options = {"observedRequest": observed_request(), "query": "期刊", "targetBiz": BIZ}
    options.update(overrides)
    data = {"source": SCRIPT.read_text(encoding="utf-8-sig") if SCRIPT.exists() else "",
            "options": options, "responses": responses, "invoke": invoke, "origin": origin}
    completed = subprocess.run(
        [node, "-e", NODE_HARNESS], input=json.dumps(data, ensure_ascii=False),
        text=True, encoding="utf-8", capture_output=True, timeout=10, check=False,
    )
    assert completed.returncode == 0, "Offline Node harness failed"
    assert not completed.stderr, "Unexpected Node stderr"
    output = json.loads(completed.stdout)
    assert output["callable"], "probe script must define globalThis.runReprintSearchProbe"
    assert output["error"] is None, "Probe must return a safe report, not throw request details"
    assert output["callsAtLoad"] == 0, "Loading the script must not initiate any request"
    assert output["logs"] == [], "Probe must not log"
    assert output["uiActions"] == 0, "Probe must not access UI, cookies or storage"
    assert output["observedUnchanged"], "Captured request is read-only"
    assert output["activeTimers"] == 0, "Abort timers must be cleared"
    if invoke:
        report = output["result"]
        serialized = json.dumps(report, ensure_ascii=False)
        for secret in SECRETS:
            assert secret not in serialized, "A synthetic credential escaped into the report"
        assert report["schema_version"] == 1
        assert report["endpoint"] == ENDPOINT
        assert report["sub"] == SUB
        assert report["method"] == "POST"
        assert report["observed_scope_notice"] == "只能搜索并转载原创文章"
        for name in ("production_resumed", "reprint_confirmed", "credentials_exported",
                     "catalog_complete"):
            assert report[name] is False
        assert report["client_ui_actions"] == 0
        assert datetime.fromisoformat(report["captured_at"].replace("Z", "+00:00")).tzinfo
    return output


def test_loading_only_defines_the_probe():
    assert run_probe(invoke=False)["calls"] == []


def test_template_bytes_are_reused_only_url_and_begin_change_and_short_page_continues():
    request = observed_request()
    keyword = "研究 / 期刊+创新"
    output = run_probe([
        ok([article(i) for i in range(1, 10)]),
        ok([article(i) for i in range(10, 20)]), ok([article(20)]),
    ], observedRequest=request, query=keyword, begin=20, maxPages=3)
    report = output["result"]
    assert report["stop_reason"] == "max_pages"
    assert [p["begin"] for p in report["pages"]] == [20, 30, 40]
    assert [p["returned_count"] for p in report["pages"]] == [9, 10, 1]
    assert output["fired"] == [2000, 2000]
    original = dict(parse_qsl(request["body"], keep_blank_values=True))
    assert report["request_parameter_names"] == sorted(original)
    for offset, call in zip([20, 30, 40], output["calls"]):
        expected = {**original, "url": keyword, "begin": str(offset)}
        assert dict(parse_qsl(call["body"], keep_blank_values=True)) == expected
        old_parts = [p for p in request["body"].split("&") if not p.startswith(("url=", "begin="))]
        new_parts = [p for p in call["body"].split("&") if not p.startswith(("url=", "begin="))]
        assert old_parts == new_parts
        assert call["url"] == request["url"]
        assert call["method"] == "POST"
        assert call["credentials"] == "same-origin"
        assert call["redirect"] == "error"
        assert call["hasSignal"] and not call["initiallyAborted"]
        assert not call["finallyAborted"]
        assert call["headers"] == {"content-type": "application/x-www-form-urlencoded;charset=UTF-8"}
    assert set(report) == {
        "schema_version", "captured_at", "endpoint", "sub", "method",
        "request_parameter_names", "observed_scope_notice", "production_resumed",
        "client_ui_actions", "reprint_confirmed", "credentials_exported", "catalog_complete",
        "stop_reason", "pages",
    }
    assert set(report["pages"][0]) == {
        "name", "query", "target_biz", "begin", "count", "mode", "http_status",
        "business_ret", "business_message", "total_hint", "returned_count", "target_count",
        "other_count", "response_keys", "item_keys", "target_articles", "returned_identities",
    }
    for page in report["pages"]:
        assert page["name"] == "keyword_probe" and page["mode"] == "background_same_session"
        assert (page["query"], page["target_biz"], page["count"]) == (keyword, BIZ, 10)
        assert (page["http_status"], page["business_ret"], page["business_message"]) == (200, 0, "ok")
        assert page["response_keys"] == ["base_resp", "list", "total"]


def test_no_target_results_continue_and_biz_is_exact_not_nickname_or_prefix():
    output = run_probe([
        ok([article(1, OTHER_BIZ), article(2, BIZ + "x")]),
        ok([article(3), article(4, OTHER_BIZ)]),
    ])
    first, second = output["result"]["pages"]
    assert (first["target_count"], first["other_count"]) == (0, 2)
    assert first["target_articles"] == []
    assert (second["target_count"], second["other_count"]) == (1, 1)
    assert second["target_articles"][0]["url"] == public_url(3)
    assert second["returned_identities"] == [
        {"url": public_url(3), "nickname": "同名期刊"},
        {"url": public_url(4, OTHER_BIZ), "nickname": "同名期刊"},
    ]
    assert output["result"]["stop_reason"] == "max_pages"


@pytest.mark.parametrize("total", [0, 1, 10000])
def test_total_hint_never_proves_catalog_completeness_or_stops_paging(total):
    output = run_probe([ok([article(i)], total) for i in range(1, 4)], maxPages=3)
    assert len(output["calls"]) == 3
    assert all(p["total_hint"] == total for p in output["result"]["pages"])
    assert output["result"]["catalog_complete"] is False


@pytest.mark.parametrize("ret", [64701, 200013, 1, -1])
def test_any_nonzero_business_code_stops_without_retry_and_keeps_previous_page(ret):
    output = run_probe([ok([article(1)]),
                        {"payload": {"base_resp": {"ret": ret, "err_msg": "rejected"}}},
                        ok([article(3)])], maxPages=5)
    report = output["result"]
    assert report["stop_reason"] == "business_error"
    assert len(output["calls"]) == len(report["pages"]) == 2
    assert report["pages"][0]["target_articles"][0]["url"] == public_url(1)
    assert report["pages"][1]["business_ret"] == ret
    assert report["pages"][1]["target_articles"] == []


@pytest.mark.parametrize("biz", [BIZ, OTHER_BIZ])
def test_cross_page_identity_repeat_stops_even_with_new_sn_or_other_account(biz):
    output = run_probe([
        ok([article(1, biz)]),
        ok([article(1, biz, url=public_url(1, biz, "f" * 32)), article(2)]),
        ok([article(3)]),
    ], maxPages=5)
    assert output["result"]["stop_reason"] == "duplicate_identity"
    assert len(output["calls"]) == 2
    assert output["result"]["pages"][1]["target_articles"][-1]["url"] == public_url(2)


def test_empty_page_stops_but_retains_prior_results():
    output = run_probe([ok([article(1)]), ok([]), ok([article(2)])], maxPages=5)
    report = output["result"]
    assert report["stop_reason"] == "empty_page"
    assert len(output["calls"]) == 2
    assert report["pages"][0]["target_count"] == 1
    assert report["pages"][1]["returned_count"] == 0


@pytest.mark.parametrize("status", [301, 302, 401, 403, 429, 500])
def test_http_error_stops_without_reading_error_body_or_losing_progress(status):
    output = run_probe([ok([article(1)]), {"status": status, "statusText": TOKEN,
                                          "payload": {"token": TOKEN}}], maxPages=5)
    assert output["result"]["stop_reason"] == "http_error"
    assert output["jsonCalls"] == 1
    assert len(output["calls"]) == 2
    assert output["result"]["pages"][0]["target_count"] == 1
    assert output["result"]["pages"][1]["http_status"] == status


def test_network_exception_is_not_echoed_and_previous_results_survive():
    output = run_probe([ok([article(1)]), {"throw": "request failed " + TOKEN + FINGERPRINT}], maxPages=5)
    assert output["result"]["stop_reason"] == "network_error"
    assert len(output["calls"]) == 2
    assert output["result"]["pages"][0]["target_count"] == 1
    assert output["result"]["pages"][1]["http_status"] is None


@pytest.mark.parametrize("hang", ["fetch", "json"])
def test_timeout_aborts_fetch_and_json_body_and_keeps_previous_results(hang):
    output = run_probe([ok([article(1)]), {"hang": hang}], maxPages=5)
    assert output["result"]["stop_reason"] == "timeout"
    assert len(output["calls"]) == 2 and output["calls"][-1]["finallyAborted"]
    assert output["result"]["pages"][0]["target_count"] == 1
    assert all(isinstance(ms, int) and 0 < ms <= 60000 for ms in output["scheduled"])


@pytest.mark.parametrize("payload", [
    None, [], {}, {"base_resp": {"ret": "0", "err_msg": "ok"}, "list": [], "total": 0},
    {"base_resp": {"ret": 0}, "list": [], "total": 0},
    {"base_resp": {"ret": 0, "err_msg": "ok"}, "list": "changed", "total": 0},
    {"base_resp": {"ret": 0, "err_msg": "ok"}, "list": [], "total": "10000"},
    {"base_resp": {"ret": 0, "err_msg": "ok"}, "list": [None], "total": 10000},
    {"base_resp": {"ret": 0, "err_msg": "ok"}, "list": [article(title={})], "total": 10000},
])
def test_response_shape_changes_stop_and_preserve_completed_pages(payload):
    output = run_probe([ok([article(1)]), {"payload": payload}], maxPages=5)
    assert output["result"]["stop_reason"] == "format_changed"
    assert output["result"]["pages"][0]["target_count"] == 1
    assert len(output["calls"]) == 2


def test_invalid_json_stops_with_fixed_error_not_the_parser_exception():
    output = run_probe([ok([article(1)]), {"jsonError": "bad JSON " + TOKEN}], maxPages=5)
    assert output["result"]["stop_reason"] == "format_changed"
    assert output["result"]["pages"][0]["target_count"] == 1


@pytest.mark.parametrize("bad_url", [
    "https://example.com/s?__biz=" + BIZ,
    public_url().replace("/s?", "/cgi-bin/operate_appmsg?"),
    public_url() + "&__biz=" + OTHER_BIZ,
    public_url() + "&__biz=" + BIZ,
    public_url() + "&biz=" + OTHER_BIZ,
    public_url() + "&mid=2", public_url() + "&idx=2", public_url() + "&sn=other",
    public_url().replace("mid=1&", ""), public_url().replace("idx=1&", "idx=0&"),
    public_url().replace("&sn=" + f"{1:032x}", ""),
    public_url().replace("mp.weixin.qq.com", "user:pass@mp.weixin.qq.com"),
    "/s?__biz=" + BIZ,
])
def test_missing_or_conflicting_identity_is_never_a_successful_article(bad_url):
    output = run_probe([ok([article(url=bad_url)])])
    page = output["result"]["pages"][0]
    assert output["result"]["stop_reason"] == "invalid_identity"
    assert page["returned_count"] == 1
    assert page["target_count"] == page["other_count"] == 0
    assert page["target_articles"] == page["returned_identities"] == []
    assert len(output["calls"]) == 1


def test_identity_fields_conflicting_with_url_are_rejected_but_valid_page_items_are_kept():
    output = run_probe([ok([article(1), article(2, __biz=OTHER_BIZ), article(3)])])
    page = output["result"]["pages"][0]
    assert output["result"]["stop_reason"] == "invalid_identity"
    assert [item["url"] for item in page["target_articles"]] == [public_url(1), public_url(3)]
    assert page["returned_count"] == 3 and page["target_count"] == 2


@pytest.mark.parametrize("options", [
    {"query": ""}, {"query": " \n "}, {"query": "x" * 1025}, {"query": None},
    {"targetBiz": ""}, {"targetBiz": "bad&biz"}, {"targetBiz": "bad biz"}, {"targetBiz": "中"},
    {"begin": -1}, {"begin": 0.5}, {"begin": "0"}, {"begin": True}, {"begin": 2**53 - 1},
    {"maxPages": 0}, {"maxPages": 6}, {"maxPages": 1.5}, {"maxPages": True},
    {"count": 9}, {"count": 11}, {"count": "10"},
    {"delayMs": 999}, {"delayMs": None}, {"delayMs": "2000"}, {"delayMs": 2**31},
])
def test_invalid_options_do_not_issue_any_request(options):
    output = run_probe(**options)
    assert output["calls"] == []
    assert output["result"]["stop_reason"] == "invalid_input"
    assert output["result"]["pages"] == []


@pytest.mark.parametrize("bad_url", [
    ENDPOINT.replace("https:", "http:") + "?sub=" + SUB,
    ENDPOINT.replace("mp.weixin.qq.com", "mp.weixin.qq.com.evil.invalid") + "?sub=" + SUB,
    ENDPOINT.replace("mp.weixin.qq.com", "user:pass@mp.weixin.qq.com") + "?sub=" + SUB,
    ENDPOINT.replace("mp.weixin.qq.com", "mp.weixin.qq.com:444") + "?sub=" + SUB,
    ENDPOINT + "/?sub=" + SUB, ENDPOINT + "?sub=publish", ENDPOINT,
    ENDPOINT + "?sub=" + SUB + "&sub=" + SUB,
    ENDPOINT + "?sub=" + SUB + "&%73ub=publish",
    ENDPOINT + "?sub=" + SUB + "#fragment", "not a url " + TOKEN,
])
def test_endpoint_is_an_exact_https_post_read_only_allowlist(bad_url):
    output = run_probe(observedRequest={**observed_request(), "url": bad_url})
    assert output["calls"] == [] and output["result"]["stop_reason"] == "invalid_input"


@pytest.mark.parametrize("method", ["GET", "post", "PUT", "DELETE", None])
def test_only_observed_post_is_accepted(method):
    output = run_probe(observedRequest={**observed_request(), "method": method})
    assert output["calls"] == [] and output["result"]["stop_reason"] == "invalid_input"


@pytest.mark.parametrize("change", ["missing_token", "missing_url", "missing_begin", "count",
                                    "allow_reprint", "duplicate", "body_sub", "object"])
def test_invalid_templates_are_rejected_not_repaired_or_given_new_credentials(change):
    request = observed_request()
    if change.startswith("missing_"):
        key = change.removeprefix("missing_")
        request["body"] = "&".join(p for p in request["body"].split("&") if not p.startswith(key + "="))
    elif change == "count":
        request["body"] = request["body"].replace("count=10", "count=20")
    elif change == "allow_reprint":
        request["body"] = request["body"].replace("allow_reprint=0", "allow_reprint=1")
    elif change == "duplicate":
        request["body"] += "&token=" + TOKEN
    elif change == "body_sub":
        request["body"] += "&sub=publish"
    else:
        request["body"] = {"token": TOKEN}
    output = run_probe(observedRequest=request)
    assert output["calls"] == [] and output["result"]["stop_reason"] == "invalid_input"


def test_wrong_page_origin_never_replays_session_request():
    output = run_probe(origin="https://example.com")
    assert output["calls"] == [] and output["result"]["stop_reason"] == "invalid_input"


def test_public_whitelist_canonical_urls_and_credential_echo_redaction():
    request = observed_request()
    request["body"] += "&cookie=" + COOKIE
    request["url"] += "&token=" + TOKEN
    item = article(
        title="标题 " + TOKEN, nickname="刊物 " + FINGERPRINT, author=COOKIE,
        digest="digest " + RANDOM + " token=UNKNOWN_TEST_CREDENTIAL",
        content="<p>" + " ".join(SECRETS) + "</p>",
        url=public_url() + "&token=" + TOKEN + "&fingerprint=" + FINGERPRINT + "#wechat_redirect",
        cover_url="https://mmbiz.qpic.cn/public/640?token=" + TOKEN + "&cookie=" + COOKIE,
        token=TOKEN, raw_response={"cookie": COOKIE}, **{FINGERPRINT: TOKEN},
    )
    response = ok([item, article(2, extra_public_key="visible name, not exported value")])
    response["payload"][TOKEN] = TOKEN
    output = run_probe([response], observedRequest=request, maxPages=1)
    report = output["result"]
    page = report["pages"][0]
    exported = page["target_articles"][0]
    assert set(exported) == {"title", "nickname", "author", "digest", "content", "cover_url", "url"}
    assert exported["url"] == public_url()
    assert "?" not in exported["cover_url"]
    assert "UNKNOWN_TEST_CREDENTIAL" not in json.dumps(report)
    assert "extra_public_key" in page["item_keys"]
    assert "visible name, not exported value" not in json.dumps(report)
    assert output["calls"][0]["url"] == request["url"]
    assert "body" not in report and "headers" not in report and "observedRequest" not in report


def test_article_query_is_sent_unchanged_but_reported_as_canonical_public_url():
    query = public_url() + "&token=" + TOKEN + "&fingerprint=" + FINGERPRINT
    output = run_probe([ok([article()])], query=query, maxPages=1)
    assert dict(parse_qsl(output["calls"][0]["body"]))["url"] == query
    assert output["result"]["pages"][0]["query"] == public_url()


def test_business_error_message_cannot_echo_credentials():
    output = run_probe([{"payload": {"base_resp": {
        "ret": 64701, "err_msg": "denied " + TOKEN + " fingerprint=" + FINGERPRINT,
    }}}])
    assert output["result"]["stop_reason"] == "business_error"
    assert output["result"]["pages"][0]["business_ret"] == 64701


def test_http_article_url_and_html_escaped_separators_are_canonicalized():
    source = public_url().replace("https:", "http:").replace("&", "&amp;")
    output = run_probe([ok([article(url=source)])], maxPages=1)
    assert output["result"]["pages"][0]["target_articles"][0]["url"] == public_url()


def test_inclusive_limits_allow_five_pages_and_1024_character_query():
    output = run_probe([ok([article(i)]) for i in range(1, 6)],
                       query="研" * 1024, begin=100, maxPages=5, delayMs=1000)
    assert len(output["calls"]) == 5
    assert [p["begin"] for p in output["result"]["pages"]] == [100, 110, 120, 130, 140]
    assert output["fired"] == [1000] * 4

@pytest.mark.parametrize("credential_name", ["key", "uin", "pass_ticket"])
def test_pasted_article_query_never_exports_session_parameters(credential_name):
    sentinel = "TEST_PASTED_LINK_PRIVATE_VALUE_759"
    query = public_url() + "&" + urlencode({credential_name: sentinel})
    output = run_probe([ok([article(title="echo " + sentinel)])], query=query, maxPages=1)
    report = output["result"]
    assert report["pages"][0]["query"] == public_url()
    assert sentinel not in json.dumps(report)
    assert dict(parse_qsl(output["calls"][0]["body"]))["url"] == query


@pytest.mark.parametrize("query", [
    "https://mp.weixin.qq.com/s/short-link?key=TEST_PASTED_SECRET&uin=TEST_PASTED_UIN",
    public_url() + "&mid=2&pass_ticket=TEST_PASTED_SECRET",
])
def test_unresolvable_pasted_url_is_not_echoed(query):
    output = run_probe([ok([])], query=query)
    page = output["result"]["pages"][0]
    assert page.get("query") is None
    assert "TEST_PASTED" not in json.dumps(output["result"])
    assert dict(parse_qsl(output["calls"][0]["body"]))["url"] == query
