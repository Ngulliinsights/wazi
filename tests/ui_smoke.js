// Drives the real page script against stubbed DOM + fetch: flaky network, hostile tender id. Prints JSON.
const fs = require('fs'), vm = require('vm');
const js = /<script>([\s\S]*)<\/script>/.exec(fs.readFileSync(process.argv[2], 'utf8'))[1];
const mk = () => ({ innerHTML: '', textContent: '', value: '', hidden: false, disabled: false, className: '', children: [], appendChild(c) { this.children.push(c); }, oninput: null });
const els = {}; const hist = [];
const document = { getElementById: id => els[id] || (els[id] = mk()), createElement: () => mk() };
els.msg = mk(); Object.defineProperty(els.msg, 'textContent', { set(v) { hist.push(v); this._t = v; }, get() { return this._t; } });
const timers = []; let evFails = 2; const urls = []; let approveBody = null; const HASH = 'ab12cd34ef56' + '0'.repeat(52);
async function fetch(url, opts) {
  urls.push(url);
  const ok = body => ({ ok: true, json: async () => body });
  if (url === '/api/config') return ok({ llm: 'stub', as_of: '2025-05-01' });
  if (url.startsWith('/api/tenders')) return ok([{ ocid: 'o"1\' onfocus=\'x', buyer_name: '<b>B</b>', category: 'c' }]);
  if (url === '/api/runs' && opts && opts.method === 'POST') return ok({ run_id: 'r1' });
  if (/^\/api\/runs\/r1\/events/.test(url)) { if (evFails-- > 0) throw new Error('network down'); return ok({ events: [{ event: 'intake', ts: 1, ocid: 'x' }], next: 1 }); }
  if (url === '/api/runs/r1') return ok({ done: true, status: 'awaiting_human_approval', seconds: 1, llm_calls: 1, filing_id: 'f1' });
  if (url === '/api/filings') return ok([{ filing_id: 'f1', md_sha256: HASH, markdown: '# T\n## S\n- **Note:** hi <script>alert(1)</script>' }]);
  if (url === '/api/filings/f1/approve') { approveBody = JSON.parse(opts.body); return ok({ action: 'approved', approver: approveBody.approver, decided_at: 't' }); }
  if (url === '/api/filings/f1/verify') return ok({ verdict: 'REPRODUCED', checks: [] });
  throw new Error('unexpected url ' + url);
}
const ctx = vm.createContext({ document, fetch, Date, JSON, String, Math, Promise, console, setTimeout: f => timers.push(f), clearTimeout: () => {} });
const tick = () => new Promise(r => setImmediate(r));
(async () => {
  vm.runInContext(js, ctx); for (let i = 0; i < 5; i++) await tick();
  els.tender.value = 'o"1'; await els.go.onclick(); for (let i = 0; i < 5; i++) await tick();
  for (let i = 0; i < 20 && timers.length; i++) { const f = timers.shift(); await f(); for (let k = 0; k < 5; k++) await tick(); }
  document.getElementById('who').value = 'Real Person'; document.getElementById('role').value = 'Procurement Officer';
  document.getElementById('read').checked = true; await document.getElementById('ok').onclick(); for (let k = 0; k < 5; k++) await tick();
  const gm = document.getElementById('gmsg').textContent; await document.getElementById('vf').onclick(); for (let k = 0; k < 5; k++) await tick();
  console.log(JSON.stringify({ approveBody, hash: HASH, gm, verifyShown: document.getElementById('gmsg').innerHTML, hist, option: els.tender.innerHTML, brief: els.brief.innerHTML, goDisabled: els.go.disabled, gateHidden: els.gate.hidden }));
})().catch(e => { console.log(JSON.stringify({ crash: String(e) })); process.exit(1); });
