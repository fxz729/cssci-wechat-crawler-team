/* Bounded, read-only probe. Load in the logged-in page, then pass one observed
 * reprint-search request. No UI, storage, logging, login, or publishing actions. */
(() => {
  const ENDPOINT = 'https://mp.weixin.qq.com/cgi-bin/operate_appmsg';
  const SUB = 'check_appmsg_copyright_stat';
  const SECRET = /^(?:token|fingerprint|random|cookie|authorization|key|uin|pass_ticket|appmsg_token|wxtoken)$/i;
  const FIELDS = ['title', 'nickname', 'author', 'digest', 'content', 'cover_url', 'url',
    'cover_url_16_9', 'cover_url_1_1', 'cover_url_235_1', 'cover_url_3_4',
    'source_url', 'source_reprint_status', 'reprint_allow_edit', 'article_type', 'is_pay_subscribe'];
  const object = value => value !== null && typeof value === 'object' && !Array.isArray(value);
  const integer = (value, low, high) => Number.isSafeInteger(value) && value >= low && value <= high;
  const unescapeUrl = value => value.replace(/&amp;/g, '&');

  function identity(value, item = {}) {
    if (typeof value !== 'string' || !/^https?:\/\//.test(value)) return null;
    try {
      const url = new URL(unescapeUrl(value));
      if (url.host !== 'mp.weixin.qq.com' || url.username || url.password || url.pathname !== '/s') return null;
      const keys = ['__biz', 'mid', 'idx', 'sn'];
      if (keys.some(key => url.searchParams.getAll(key).length !== 1)) return null;
      const [biz, mid, idx, sn] = keys.map(key => url.searchParams.get(key));
      if (!/^[A-Za-z0-9_=-]{1,200}$/.test(biz) || !/^\d{1,30}$/.test(mid) ||
          !/^[1-9]\d{0,2}$/.test(idx) || !/^[a-fA-F0-9]{32}$/.test(sn)) return null;
      if (url.searchParams.has('biz') && (url.searchParams.getAll('biz').length !== 1 || url.searchParams.get('biz') !== biz)) return null;
      for (const [key, expected] of Object.entries({biz, __biz: biz, mid, idx, sn})) {
        if (key in item && String(item[key]) !== expected) return null;
      }
      return {biz, key: [biz, mid, idx].join(':'),
        url: 'https://mp.weixin.qq.com/s?' + new URLSearchParams({__biz: biz, mid, idx, sn})};
    } catch (_) { return null; }
  }

  globalThis.runReprintSearchProbe = async function (options = {}) {
    const report = {schema_version: 1, captured_at: new Date().toISOString(), endpoint: ENDPOINT,
      sub: SUB, method: 'POST', request_parameter_names: [],
      observed_scope_notice: '只能搜索并转载原创文章', production_resumed: false, client_ui_actions: 0,
      reprint_confirmed: false, credentials_exported: false, catalog_complete: false,
      stop_reason: 'invalid_input', pages: []};
    const {observedRequest, query, targetBiz, begin = 0, maxPages = 2, count = 10, delayMs = 2000} = options;
    let sourceUrl, form;
    try {
      if (location.origin !== 'https://mp.weixin.qq.com' || !object(observedRequest) ||
          observedRequest.method !== 'POST' || typeof observedRequest.body !== 'string') return report;
      sourceUrl = new URL(observedRequest.url);
      form = new URLSearchParams(observedRequest.body);
      if (sourceUrl.origin !== 'https://mp.weixin.qq.com' || sourceUrl.username || sourceUrl.password ||
          sourceUrl.pathname !== '/cgi-bin/operate_appmsg' || sourceUrl.hash ||
          sourceUrl.searchParams.getAll('sub').length !== 1 || sourceUrl.searchParams.get('sub') !== SUB ||
          !['token', 'fingerprint', 'random', 'url', 'begin', 'count', 'allow_reprint', 'lang', 'f', 'ajax'].every(key => form.has(key)) ||
          [...form.keys()].some(key => form.getAll(key).length !== 1) || form.has('sub') || form.has('action') ||
          form.get('count') !== '10' || form.get('allow_reprint') !== '0' || !form.get('token') ||
          typeof query !== 'string' || !query.trim() || query.length > 1024 ||
          typeof targetBiz !== 'string' || !/^[A-Za-z0-9_=-]{1,200}$/.test(targetBiz) ||
          !integer(begin, 0, Number.MAX_SAFE_INTEGER - 50) || !integer(maxPages, 1, 5) || count !== 10 ||
          !integer(delayMs, 1000, 60000)) return report;
    } catch (_) { return report; }

    const secrets = new Set();
    const collect = params => { for (const [key, value] of params) if (SECRET.test(key) && value) secrets.add(value); };
    collect(form); collect(sourceUrl.searchParams);
    const isUrlQuery = /^https?:\/\//.test(query);
    if (isUrlQuery) { try { collect(new URL(unescapeUrl(query)).searchParams); } catch (_) {} }
    const redact = value => {
      let text = String(value);
      for (const secret of secrets) text = text.split(secret).join('[redacted]');
      return text.replace(/(?:token|fingerprint|cookie|authorization|key|uin|pass_ticket|appmsg_token|wxtoken)\s*=\s*[^\s&<>"']+/gi, '[redacted]');
    };
    const safeKeys = value => Object.keys(value).filter(key => /^[A-Za-z_][A-Za-z0-9_]{0,80}$/.test(key) &&
      !secrets.has(key)).sort();
    const publicAsset = value => {
      try {
        const url = new URL(unescapeUrl(value));
        if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) return '';
        for (const key of [...url.searchParams.keys()]) if (SECRET.test(key)) url.searchParams.delete(key);
        return redact(url.href);
      } catch (_) { return ''; }
    };
    const publicQuery = isUrlQuery ? (identity(query)?.url || null) : redact(query);
    report.request_parameter_names = safeKeys(Object.fromEntries(form));
    report.stop_reason = 'max_pages';
    const seen = new Set();
    for (let number = 0; number < maxPages; number++) {
      if (number) await new Promise(resolve => setTimeout(resolve, delayMs));
      const offset = begin + number * count;
      // Preserve every untouched form component byte-for-byte, including encoding.
      const body = observedRequest.body.split('&').map(part => {
        const split = part.indexOf('=');
        const key = decodeURIComponent((split < 0 ? part : part.slice(0, split)).replace(/\+/g, ' '));
        if (key === 'url') return part.slice(0, split + 1) + new URLSearchParams({v: query}).toString().slice(2);
        if (key === 'begin') return part.slice(0, split + 1) + offset;
        return part;
      }).join('&');
      const page = {name: 'keyword_probe', query: publicQuery, target_biz: targetBiz, begin: offset, count,
        mode: 'background_same_session', http_status: null, business_ret: null, business_message: null,
        total_hint: null, returned_count: 0, target_count: 0, other_count: 0, response_keys: [],
        item_keys: [], target_articles: [], returned_identities: []};
      report.pages.push(page);
      const controller = new AbortController();
      let timer, timedOut = false, phase = 'fetch', payload;
      const timeout = new Promise((_, reject) => { timer = setTimeout(() => {
        timedOut = true; controller.abort(); reject(new Error('timeout'));
      }, 25000); });
      try {
        const response = await Promise.race([fetch(observedRequest.url, {method: 'POST',
          headers: {'Content-Type': 'application/x-www-form-urlencoded;charset=UTF-8'},
          credentials: 'same-origin', redirect: 'error', body, signal: controller.signal}), timeout]);
        page.http_status = response.status;
        if (response.status !== 200) { report.stop_reason = 'http_error'; break; }
        phase = 'json';
        payload = await Promise.race([response.json(), timeout]);
      } catch (_) {
        report.stop_reason = timedOut ? 'timeout' : phase === 'json' ? 'format_changed' : 'network_error';
        break;
      } finally { clearTimeout(timer); }
      if (!object(payload) || !object(payload.base_resp) || !Number.isInteger(payload.base_resp.ret) ||
          typeof payload.base_resp.err_msg !== 'string') { report.stop_reason = 'format_changed'; break; }
      page.response_keys = safeKeys(payload);
      page.business_ret = payload.base_resp.ret;
      page.business_message = redact(payload.base_resp.err_msg);
      if (page.business_ret !== 0) { report.stop_reason = 'business_error'; break; }
      if (!Array.isArray(payload.list) || !integer(payload.total, 0, Number.MAX_SAFE_INTEGER) ||
          payload.list.some(item => !object(item) || ['title', 'nickname', 'author', 'digest', 'content', 'cover_url', 'url']
            .some(key => typeof item[key] !== 'string'))) { report.stop_reason = 'format_changed'; break; }
      page.total_hint = payload.total;
      page.returned_count = payload.list.length;
      page.item_keys = [...new Set(payload.list.flatMap(safeKeys))].sort();
      let invalid = false, duplicate = false;
      for (const item of payload.list) {
        const id = identity(item.url, item);
        if (!id) { invalid = true; continue; }
        if (seen.has(id.key)) { duplicate = true; continue; }
        seen.add(id.key);
        page.returned_identities.push({url: id.url, nickname: redact(item.nickname)});
        if (id.biz !== targetBiz) { page.other_count++; continue; }
        const safe = {};
        for (const key of FIELDS) {
          if (!(key in item)) continue;
          if (key === 'url') safe.url = id.url;
          else if (key.includes('url')) safe[key] = publicAsset(item[key]);
          else if (typeof item[key] === 'string') safe[key] = redact(item[key]);
          else if (typeof item[key] === 'boolean' || Number.isFinite(item[key])) safe[key] = item[key];
        }
        page.target_articles.push(safe);
        page.target_count++;
      }
      if (invalid || duplicate || !payload.list.length) {
        report.stop_reason = invalid ? 'invalid_identity' : duplicate ? 'duplicate_identity' : 'empty_page';
        break;
      }
    }
    return report;
  };
})();