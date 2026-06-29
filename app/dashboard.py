"""Self-contained admin console served at GET / and /dashboard.

Access is gated behind an admin login (username + password → signed HttpOnly session
cookie). The page polls /stats and drives the admin endpoints (key management, model
toggles, a test prompt, CSV export) using the session cookie + a CSRF double-submit
token. No API key is ever stored in the browser.

Three exports:
  * SETUP_HTML     — shown when ADMIN_USER/ADMIN_PASSWORD_HASH aren't configured.
  * LOGIN_HTML     — the login screen.
  * DASHBOARD_HTML — the authenticated console.
"""

# ── shared dark theme (used by login + setup + dashboard) ─────────────────────
_BASE_CSS = r"""
  :root{
    --bg:#0a0e14; --panel:#111824; --panel2:#0d1420; --line:#1d2940;
    --text:#cdd6e6; --muted:#5d6e8c; --green:#39d98a; --amber:#ffb454; --red:#ff5c7c;
    --cyan:#56b6ff; --violet:#b58cff;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:radial-gradient(1200px 600px at 70% -10%,#11203a22,transparent),var(--bg);
       color:var(--text);font-family:Inter,system-ui,Segoe UI,Roboto,sans-serif;}
  a{color:var(--cyan)}
  button{font-family:var(--mono);font-size:12px;cursor:pointer;border:1px solid var(--line);
         background:var(--panel);color:var(--text);padding:7px 12px;border-radius:8px;transition:.15s}
  button:hover{border-color:#34507f}
  button.warn{border-color:#5a2a3a;color:var(--red)}
  button.go{border-color:#1c3b2c;color:var(--green)}
  button:disabled{opacity:.5;cursor:not-allowed}
  input,textarea,select{font-family:var(--mono);background:var(--panel2);border:1px solid var(--line);
        color:var(--text);padding:9px 11px;border-radius:8px}
"""

# ── login ──────────────────────────────────────────────────────────────────────
LOGIN_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ripgpt · login</title>
<style>""" + _BASE_CSS + r"""
  .box{max-width:360px;margin:14vh auto 0;padding:0 20px}
  .logo{font-family:var(--mono);font-weight:700;font-size:22px;text-align:center;margin-bottom:22px}
  .logo b{color:var(--green)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px}
  label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.7px;color:var(--muted);margin:0 0 6px}
  input{width:100%;margin-bottom:16px}
  .card button{width:100%;padding:11px}
  .err{color:var(--red);font-family:var(--mono);font-size:12px;min-height:16px;margin-top:12px;text-align:center}
  .foot{margin-top:16px;font-family:var(--mono);font-size:11px;color:var(--muted);text-align:center}
</style></head>
<body>
<div class="box">
  <div class="logo"><span>🏴‍☠️</span> <b>ripgpt</b> · console</div>
  <div class="card">
    <form id="f">
      <label>username</label>
      <input id="u" autocomplete="username" autofocus />
      <label>password</label>
      <input id="p" type="password" autocomplete="current-password" />
      <button class="go" type="submit">sign in</button>
      <div class="err" id="err"></div>
    </form>
  </div>
  <div class="foot">authorised access only</div>
</div>
<script>
const $=id=>document.getElementById(id);
$('f').addEventListener('submit', async e=>{
  e.preventDefault(); $('err').textContent='';
  const btn=$('f').querySelector('button'); btn.disabled=true;
  try{
    const r=await fetch('/admin/login',{method:'POST',credentials:'same-origin',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({username:$('u').value,password:$('p').value})});
    if(r.ok){ window.location='/'; return; }
    const j=await r.json().catch(()=>({}));
    $('err').textContent=(j.error&&j.error.message)||('Error '+r.status);
  }catch(e){ $('err').textContent='Network error'; }
  btn.disabled=false;
});
</script>
</body></html>
"""

# ── setup (admin not configured) ────────────────────────────────────────────────
SETUP_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ripgpt · setup</title>
<style>""" + _BASE_CSS + r"""
  .box{max-width:620px;margin:10vh auto 0;padding:0 20px}
  .logo{font-family:var(--mono);font-weight:700;font-size:22px;margin-bottom:18px}
  .logo b{color:var(--green)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:22px}
  h2{margin:0 0 10px;font-size:15px}
  p{color:var(--muted);font-size:13px;line-height:1.55}
  pre{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:12px;
      font-family:var(--mono);font-size:12px;overflow:auto;color:var(--cyan)}
  code{font-family:var(--mono);color:var(--amber)}
</style></head>
<body>
<div class="box">
  <div class="logo"><span>🏴‍☠️</span> <b>ripgpt</b> · console</div>
  <div class="card">
    <h2>Admin console is locked</h2>
    <p>No admin credentials are configured, so the console can't be opened. Generate a
       password hash and add it to your <code>.env</code>, then restart:</p>
    <pre>python -m app.adminpw
# or in Docker:
docker compose exec api python -m app.adminpw</pre>
    <p>Paste the printed <code>ADMIN_USER</code> and <code>ADMIN_PASSWORD_HASH</code> into
       <code>.env</code>, then <code>docker compose up -d</code>. The API itself keeps
       working — only this console is locked until then.</p>
  </div>
</div>
</body></html>
"""

# ── dashboard (authenticated) ────────────────────────────────────────────────────
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ripgpt · console</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<style>
""" + _BASE_CSS + r"""
  .wrap{max-width:1180px;margin:0 auto;padding:18px 20px 60px}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .logo{font-family:var(--mono);font-weight:700;letter-spacing:.5px;font-size:20px}
  .logo .pirate{filter:drop-shadow(0 0 6px #39d98a55)}
  .logo b{color:var(--green)}
  .spacer{flex:1}
  .pill{font-family:var(--mono);font-size:11px;color:var(--muted);border:1px solid var(--line);
        padding:4px 9px;border-radius:999px;background:var(--panel2)}
  #hero{position:relative;height:180px;margin:14px 0 6px;border:1px solid var(--line);
        border-radius:14px;overflow:hidden;background:linear-gradient(180deg,#0c1422,#0a0e14)}
  #hero canvas{display:block}
  #heroLabel{position:absolute;left:16px;bottom:12px;font-family:var(--mono);font-size:12px;color:var(--muted)}
  #heroState{position:absolute;right:16px;top:12px;font-family:var(--mono);font-size:12px}
  #banner{display:none;margin:10px 0;padding:11px 14px;border-radius:10px;font-family:var(--mono);
          font-size:13px;border:1px solid;}
  .grid{display:grid;gap:12px}
  .strip{grid-template-columns:repeat(4,1fr)}
  @media(max-width:760px){.strip{grid-template-columns:repeat(2,1fr)}}
  .tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 15px;position:relative;overflow:hidden}
  .tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted)}
  .tile .v{font-family:var(--mono);font-size:26px;margin-top:6px;font-weight:600}
  .tile .sub{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}
  .tile .edge{position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--muted)}
  .ok{color:var(--green)} .warn-c{color:var(--amber)} .bad{color:var(--red)} .dim{color:var(--muted)}
  .e-ok{background:var(--green)} .e-warn{background:var(--amber)} .e-bad{background:var(--red)} .e-dim{background:var(--muted)}
  .row2{grid-template-columns:1.4fr 1fr}
  @media(max-width:760px){.row2{grid-template-columns:1fr}}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px}
  .card h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);font-weight:600;
           display:flex;align-items:center;gap:10px}
  .card h3 .act{margin-left:auto;font-weight:400;text-transform:none;letter-spacing:0}
  .chartbox{position:relative;height:190px;width:100%}
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
  th{text-align:left;color:var(--muted);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line);position:sticky;top:0;background:var(--panel)}
  td{padding:6px 8px;border-bottom:1px solid #131c2c;white-space:nowrap}
  .tbl-wrap{max-height:360px;overflow:auto}
  .tag{font-size:10px;padding:1px 6px;border-radius:5px;border:1px solid var(--line)}
  .muted{color:var(--muted)}
  .usage-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:10px}
  .ucard{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px 13px;transition:.15s}
  .ucard:hover{border-color:#34507f}
  .ucard .um{font-family:var(--mono);font-size:13px;color:var(--cyan);font-weight:600;display:flex;justify-content:space-between;align-items:baseline;gap:8px}
  .ucard .um .share{font-size:10px;color:var(--muted);font-weight:400}
  .ucard .un{font-family:var(--mono);font-size:30px;font-weight:600;line-height:1.1;margin:7px 0 1px}
  .ucard .ul{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}
  .bar{height:6px;border-radius:4px;background-color:#19233a;margin:10px 0 8px;overflow:hidden}
  .bar > i{display:block;height:100%;border-radius:4px;transition:width .4s}
  .ustats{display:flex;flex-wrap:wrap;gap:3px 12px;font-family:var(--mono);font-size:11px;color:var(--muted)}
  .ustats b{color:var(--text);font-weight:600}
  .foot{margin-top:22px;font-family:var(--mono);font-size:11px;color:var(--muted);text-align:center}
  /* keys / models / test */
  .row3{grid-template-columns:1fr 1fr}
  @media(max-width:860px){.row3{grid-template-columns:1fr}}
  .kform{display:flex;gap:8px;margin-bottom:12px}
  .kform input{flex:1}
  .secretbox{display:none;margin:10px 0;padding:12px;border:1px solid var(--green);border-radius:10px;background:#0c1c14}
  .secretbox .lbl{font-size:11px;color:var(--amber);font-family:var(--mono);margin-bottom:6px}
  .secretbox .val{font-family:var(--mono);font-size:13px;color:var(--green);word-break:break-all;
                  background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:9px;cursor:pointer}
  .modelrow{display:flex;align-items:center;gap:10px;padding:7px 4px;border-bottom:1px solid #131c2c;font-family:var(--mono);font-size:12px}
  .modelrow .mid{flex:1}
  .sw{width:38px;height:20px;border-radius:999px;background:#26324a;position:relative;cursor:pointer;transition:.15s;flex:none}
  .sw.on{background:#1c5a3c}
  .sw i{position:absolute;top:2px;left:2px;width:16px;height:16px;border-radius:50%;background:#cdd6e6;transition:.15s}
  .sw.on i{left:20px;background:var(--green)}
  #testOut{margin-top:10px;background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:12px;
           font-family:var(--mono);font-size:12px;white-space:pre-wrap;max-height:280px;overflow:auto;min-height:40px;color:var(--text)}
  #testOut img{max-width:100%;border-radius:8px;margin-top:6px}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo"><span class="pirate">🏴‍☠️</span> <b>ripgpt</b> · console</div>
    <span class="spacer"></span>
    <span class="pill" id="clock">—</span>
    <span class="pill" id="pausePill" style="display:none">⏸ polling paused (tab hidden)</span>
    <a href="/admin/usage.csv"><button title="Download usage CSV">⭳ CSV</button></a>
    <button id="restartBtn" class="warn" title="Recreate the browser session">⟳ restart</button>
    <button id="logoutBtn" title="Sign out">⎋ logout</button>
  </header>

  <div id="banner"></div>

  <div id="hero">
    <div id="heroState"></div>
    <div id="heroLabel">live activity</div>
  </div>

  <!-- top health strip -->
  <div class="grid strip" style="margin-top:12px">
    <div class="tile"><div class="edge" id="t1e"></div><div class="k">Session</div><div class="v" id="t1v">—</div><div class="sub" id="t1s"></div></div>
    <div class="tile"><div class="edge" id="t2e"></div><div class="k">Queue depth</div><div class="v" id="t2v">—</div><div class="sub" id="t2s">in-flight</div></div>
    <div class="tile"><div class="edge" id="t3e"></div><div class="k">Error rate · 15m</div><div class="v" id="t3v">—</div><div class="sub" id="t3s"></div></div>
    <div class="tile"><div class="edge" id="t4e"></div><div class="k">Coût API évité</div><div class="v" id="t4v">—</div><div class="sub" id="t4s">total estimé</div></div>
  </div>

  <!-- charts -->
  <div class="grid row2" style="margin-top:12px">
    <div class="card"><h3>Requests over time · ok vs error (1-min)</h3><div class="chartbox"><canvas id="reqChart"></canvas></div></div>
    <div class="card"><h3>Latence par modèle · médiane &amp; 95ᵉ percentile (ms, dernière heure)</h3><div class="chartbox"><canvas id="latChart"></canvas></div></div>
  </div>

  <!-- API keys + models -->
  <div class="grid row3" style="margin-top:12px">
    <div class="card">
      <h3>API keys</h3>
      <div class="kform">
        <input id="keyName" placeholder="key name (e.g. mobile-app)" maxlength="80"/>
        <button class="go" id="createKeyBtn">+ create</button>
      </div>
      <div class="secretbox" id="secretBox">
        <div class="lbl">⚠ Copy this key now — it is shown only once.</div>
        <div class="val" id="secretVal" title="click to copy"></div>
      </div>
      <div class="tbl-wrap">
        <table>
          <thead><tr><th>name</th><th>prefix</th><th>requests</th><th>~API cost</th><th>last used</th><th></th></tr></thead>
          <tbody id="keysBody"><tr><td colspan="5" class="muted">loading…</td></tr></tbody>
        </table>
      </div>
    </div>
    <div class="card">
      <h3>Models <span class="act muted">enable / disable</span></h3>
      <div id="modelsBox"><div class="muted">loading…</div></div>
    </div>
  </div>

  <!-- test prompt -->
  <div class="card" style="margin-top:12px">
    <h3>Test prompt <span class="act muted">runs through the live browser</span></h3>
    <div class="kform">
      <select id="testModel" style="flex:none;min-width:160px"></select>
      <input id="testPrompt" placeholder="ask something…" maxlength="8000"/>
      <button class="go" id="testBtn">▷ run</button>
    </div>
    <div id="testOut" class="muted">—</div>
  </div>

  <!-- usage by model -->
  <div class="card" style="margin-top:12px">
    <h3>Usage by model · all-time <span id="usageTotals" class="muted" style="text-transform:none;letter-spacing:0;font-weight:400"></span></h3>
    <div class="usage-grid" id="usageGrid"><div class="muted">waiting for data…</div></div>
  </div>

  <!-- recent requests -->
  <div class="card" style="margin-top:12px">
    <h3>Recent requests</h3>
    <div class="tbl-wrap">
      <table>
        <thead><tr><th>when</th><th>model</th><th>status</th><th>latency</th><th>output</th></tr></thead>
        <tbody id="recentBody"><tr><td colspan="5" class="muted">waiting for data…</td></tr></tbody>
      </table>
    </div>
  </div>

  <div class="foot">refresh 4s · pauses when tab hidden · ⚠ single browser behind — keep the pace calm</div>
</div>

<script>
const $ = id => document.getElementById(id);
let reqChart, latChart, threeState = {target:0, cur:0, pulse:0};
let LAST_STATS = {};

function csrfToken(){ const m=document.cookie.match(/(?:^|; )ripgpt_csrf=([^;]+)/); return m?decodeURIComponent(m[1]):''; }
async function api(path, opts={}){
  opts.credentials='same-origin';
  opts.headers = Object.assign({}, opts.headers||{});
  const meth=(opts.method||'GET').toUpperCase();
  if(meth!=='GET'){ opts.headers['X-CSRF-Token']=csrfToken(); if(opts.body) opts.headers['Content-Type']='application/json'; }
  const r = await fetch(path, opts);
  if(r.status===401){ window.location='/login'; throw new Error('unauthorized'); }
  return r;
}
function esc(s){ return (s==null?'':String(s)).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function rel(ts){ if(!ts) return '—'; const s=Math.max(0,Math.floor(Date.now()/1000-ts));
  if(s<60)return s+'s ago'; if(s<3600)return Math.floor(s/60)+'m ago';
  if(s<86400)return Math.floor(s/3600)+'h ago'; return Math.floor(s/86400)+'d ago'; }
function setTile(n,val,sub,cls){ $('t'+n+'v').innerHTML=val; if(sub!==null)$('t'+n+'s').textContent=sub;
  $('t'+n+'v').className='v '+(cls||''); $('t'+n+'e').className='edge e-'+(cls||'dim'); }
function banner(msg,kind){ const b=$('banner'); if(!msg){b.style.display='none';return;}
  b.style.display='block'; b.textContent=msg;
  const c={bad:['#ff5c7c','#2a0f18'],warn:['#ffb454','#241a0c']}[kind]||['#56b6ff','#0c1b2a'];
  b.style.color=c[0]; b.style.borderColor=c[0]; b.style.background=c[1]; }

function render(s){
  LAST_STATS=s;
  $('clock').textContent = new Date().toLocaleTimeString();
  const st = s.live;
  const ssMap={logged_in:['LOGGED IN','ok'],logged_out:['LOGGED OUT','bad'],
               browser_dead:['BROWSER DEAD','bad'],starting:['STARTING','warn-c']};
  const ss=ssMap[st.session_state]||['?','dim'];
  setTile(1, ss[0], 'last ok '+rel(s.last_success_ts)+' · up '+(st.browser_uptime_s||0)+'s', ss[1]);
  let qc = st.queue_depth>=5?'bad':st.queue_depth>=2?'warn-c':'ok';
  const inf = st.in_flight ? (st.in_flight.model+' · '+st.in_flight.age_s+'s') : 'idle';
  setTile(2, st.queue_depth, inf, qc);
  const er=s.error_rate_15m, prev=s.error_rate_prev_15m;
  let ec = er>0.20?'bad':er>0.05?'warn-c':'ok';
  const spike = (prev>0 && er>=2*prev && er>0.05);
  setTile(3, (er*100).toFixed(0)+'%', s.req_15m+' req · '+(spike?'▲ spiking':'prev '+(prev*100).toFixed(0)+'%'), ec);
  const w=s.consecutive_empty_or_timeout;   // still tracked → drives the wedge banner below
  const costSaved=(s.lifetime&&s.lifetime.cost)||0;
  setTile(4, '~'+fmtMoney(costSaved), 'coût API évité · all-time', 'ok');

  if(st.session_state==='logged_out') banner('⚠ Session looks LOGGED OUT — refresh CHATGPT_COOKIES and restart. Nothing works until then.','bad');
  else if(st.session_state==='browser_dead') banner('⚠ Browser session is DEAD — hit “restart”.','bad');
  else if(w>=3) banner('⚠ Possible rate-limit WEDGE ('+w+' empty/timeout in a row) — back off / pause sending.','bad');
  else if(er>0.20) banner('Error rate high ('+(er*100).toFixed(0)+'% over 15m).','warn');
  else if(spike) banner('Error rate spiking vs previous window.','warn');
  else banner(null);

  drawReq(s.series); drawLat(s.by_model_latency); drawUsage(s); drawRecent(s.recent);
  renderKeysUsage();   // refresh per-key counters from the new stats snapshot

  let health = st.session_state==='logged_in' ? Math.min(1, er*2 + (w>=3?1:w*0.25)) : 1;
  threeState.target = health;
  const lastN = s.recent[0]; if(lastN){ const age=Date.now()/1000-lastN.ts; if(age<5) threeState.pulse=1; }
  $('heroState').innerHTML = ss[0]==='LOGGED IN'
    ? '<span class="ok">● online</span>' : '<span class="bad">● '+ss[0].toLowerCase()+'</span>';
}

function fmtN(n){ if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1000) return (n/1000).toFixed(n>=10000?0:1)+'k'; return ''+n; }
function fmtMoney(v){ v=v||0; if(v>=1000) return '$'+(v/1000).toFixed(2)+'k'; if(v>=1) return '$'+v.toFixed(2); if(v>=0.01) return '$'+v.toFixed(3); return v>0?('$'+v.toFixed(4)):'$0'; }
function drawUsage(s){
  const lt=s.lifetime||{requests:0,models:0,ctoks:0,cost:0};
  $('usageTotals').innerHTML = lt.requests
    ? '· '+lt.requests+' requests · '+lt.models+' models'+(lt.images?' · '+lt.images+' images':'')+' · ~'+fmtN(lt.ctoks||0)+' tokens out'
      +' · <b style="color:var(--green)">~'+fmtMoney(lt.cost)+'</b> coût API évité'
    : '';
  const arr=s.by_model_usage||[];
  if(!arr.length){ $('usageGrid').innerHTML='<div class="muted">no requests yet</div>'; return; }
  const total=arr.reduce((a,m)=>a+m.requests,0)||1;
  $('usageGrid').innerHTML = arr.map(m=>{
    const sr=Math.round(m.success_rate*100);
    const col = sr>=95?'var(--green)':sr>=80?'var(--amber)':'var(--red)';
    const share=Math.round(100*m.requests/total);
    const p95=m.p95_latency_ms?((m.p95_latency_ms/1000).toFixed(1)+'s'):'—';
    const tok=m.ctoks?('~'+fmtN(m.ctoks)):'—';
    return `<div class="ucard">
      <div class="um"><span>${esc(m.model)}</span><span class="share">${share}% of traffic</span></div>
      <div class="un">${m.requests}</div>
      <div class="ul">requests · ${sr}% success</div>
      <div class="bar"><i style="width:${sr}%;background:${col}"></i></div>
      <div class="ustats">
        <span><b class="ok">${m.ok}</b> ok</span>
        <span><b style="color:${m.err?'var(--red)':'var(--muted)'}">${m.err}</b> err</span>
        <span>p95 <b>${p95}</b></span>
        ${m.images?('<span><b class="ok">🖼 '+m.images+'</b> img</span>'):('<span>out <b>'+tok+'</b></span>')}
        <span>~<b style="color:var(--green)">${fmtMoney(m.cost)}</b> API</span>
        <span>last <b>${rel(m.last_ts)}</b></span>
      </div>
    </div>`;
  }).join('');
}

function statusCell(r){
  if(r.status==='ok') return '<span class="tag" style="color:var(--green);border-color:#1c3b2c">ok</span>';
  return '<span class="tag" style="color:var(--red);border-color:#3b1c28">'+esc(r.error_class||'error')+'</span>';
}
function drawRecent(recent){
  if(!recent||!recent.length){ $('recentBody').innerHTML='<tr><td colspan=5 class=muted>no requests yet</td></tr>'; return; }
  const bym={}; recent.forEach(r=>{ if(r.status==='ok'){(bym[r.model_res]=bym[r.model_res]||[]).push(r.latency_ms);} });
  const p95={}; for(const m in bym){ const a=bym[m].slice().sort((x,y)=>x-y); p95[m]=a[Math.floor(0.95*(a.length-1))]||0; }
  $('recentBody').innerHTML = recent.map(r=>{
    const slow = r.status==='ok' && p95[r.model_res] && r.latency_ms>p95[r.model_res];
    const mr = r.model_req!==r.model_res ? r.model_req+'→'+r.model_res : r.model_res;
    const lat = (r.latency_ms/1000).toFixed(1)+'s';
    return `<tr><td class=muted>${rel(r.ts)}</td><td>${esc(mr)}</td><td>${statusCell(r)}</td>
      <td style="color:${slow?'var(--amber)':'var(--text)'}">${r.status==='ok'?lat:'—'}</td>
      <td class=muted>${r.status!=='ok'?'—':(r.images?('🖼 '+r.images):('~'+(r.ptoks_est+r.ctoks_est)))}</td></tr>`;
  }).join('');
}

function drawReq(series){
  const labels=series.map(d=>new Date(d.t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}));
  const ok=series.map(d=>d.ok), err=series.map(d=>d.err);
  if(!reqChart){
    reqChart=new Chart($('reqChart'),{type:'line',
      data:{labels,datasets:[
        {label:'ok',data:ok,borderColor:'#39d98a',backgroundColor:'#39d98a22',fill:true,tension:.3,pointRadius:0,borderWidth:2},
        {label:'error',data:err,borderColor:'#ff5c7c',backgroundColor:'#ff5c7c22',fill:true,tension:.3,pointRadius:0,borderWidth:2}]},
      options:chartOpts()});
  } else { reqChart.data.labels=labels; reqChart.data.datasets[0].data=ok; reqChart.data.datasets[1].data=err; reqChart.update('none'); }
}
function drawLat(ml){
  const labels=ml.map(m=>m.model+' ('+m.count+')');
  if(!latChart){
    latChart=new Chart($('latChart'),{type:'bar',
      data:{labels,datasets:[
        {label:'médiane (p50)',data:ml.map(m=>m.p50),backgroundColor:'#56b6ff'},
        {label:'95ᵉ percentile (p95)',data:ml.map(m=>m.p95),backgroundColor:'#b58cff'}]},
      options:Object.assign(chartOpts(),{indexAxis:'y'})});
  } else { latChart.data.labels=labels; latChart.data.datasets[0].data=ml.map(m=>m.p50); latChart.data.datasets[1].data=ml.map(m=>m.p95); latChart.update('none'); }
}
function chartOpts(){return{responsive:true,maintainAspectRatio:false,animation:false,
  plugins:{legend:{labels:{color:'#5d6e8c',boxWidth:10,font:{size:10}}}},
  scales:{x:{ticks:{color:'#3f4d68',font:{size:9},maxRotation:0},grid:{color:'#131c2c'}},
          y:{ticks:{color:'#3f4d68',font:{size:9}},grid:{color:'#131c2c'},beginAtZero:true}}};}

/* ───────────── API keys ───────────── */
let KEYS_CACHE=[];
async function loadKeys(){
  try{ const j=await (await api('/admin/keys')).json(); KEYS_CACHE=j.keys||[]; renderKeysUsage(); }
  catch(e){}
}
function renderKeysUsage(){
  const usage={}; (LAST_STATS.by_key_usage||[]).forEach(k=>usage[k.key_id]=k);
  if(!KEYS_CACHE.length){ $('keysBody').innerHTML='<tr><td colspan=6 class=muted>no keys — create one</td></tr>'; return; }
  $('keysBody').innerHTML=KEYS_CACHE.map(k=>{
    const u=usage[k.id]||{requests:0,last_ts:null,cost:0};
    const rev=k.revoked;
    const nm=esc(k.name)+(rev?' <span class="tag" style="color:var(--red);border-color:#3b1c28">revoked</span>':'');
    const act=rev?'':`<button class="warn" onclick="revokeKey('${esc(k.id)}')">revoke</button>`;
    return `<tr style="${rev?'opacity:.5':''}">
      <td>${nm}</td><td class=muted>${esc(k.prefix)}…</td>
      <td>${u.requests||0}</td><td style="color:var(--green)">~${fmtMoney(u.cost)}</td>
      <td class=muted>${u.last_ts?rel(u.last_ts):(k.last_used?rel(k.last_used):'never')}</td>
      <td style="text-align:right">${act}</td></tr>`;
  }).join('');
}
async function createKey(){
  const name=$('keyName').value.trim()||'key';
  const btn=$('createKeyBtn'); btn.disabled=true;
  try{
    const j=await (await api('/admin/keys',{method:'POST',body:JSON.stringify({name})})).json();
    $('keyName').value='';
    const box=$('secretBox'), val=$('secretVal');
    val.textContent=j.key; box.style.display='block';
    val.onclick=()=>{ navigator.clipboard&&navigator.clipboard.writeText(j.key); val.textContent=j.key+'  ✓ copied'; };
    await loadKeys();
  }catch(e){}
  btn.disabled=false;
}
async function revokeKey(id){
  if(!confirm('Revoke this key? Clients using it will immediately get 401.')) return;
  try{ await api('/admin/keys/'+encodeURIComponent(id)+'/revoke',{method:'POST'}); await loadKeys(); }catch(e){}
}

/* ───────────── models ───────────── */
async function loadModels(){
  try{
    const j=await (await api('/admin/models')).json();
    const ms=j.models||[];
    $('modelsBox').innerHTML=ms.map(m=>`
      <div class="modelrow">
        <span class="mid">${esc(m.id)}${m.image?' <span class="muted">🖼</span>':''}${m.temporary?'':' <span class="muted" title="persistent chat">·persist</span>'}</span>
        <div class="sw ${m.enabled?'on':''}" onclick="toggleModel('${esc(m.id)}',${!m.enabled})"><i></i></div>
      </div>`).join('');
    const sel=$('testModel');
    sel.innerHTML=ms.filter(m=>m.enabled).map(m=>`<option value="${esc(m.id)}">${esc(m.id)}</option>`).join('');
  }catch(e){}
}
async function toggleModel(id, enabled){
  try{ await api('/admin/models/toggle',{method:'POST',body:JSON.stringify({model:id,enabled})}); await loadModels(); }catch(e){}
}

/* ───────────── test prompt ───────────── */
async function runTest(){
  const prompt=$('testPrompt').value.trim(); if(!prompt) return;
  const model=$('testModel').value||'auto';
  const out=$('testOut'); const btn=$('testBtn'); btn.disabled=true;
  out.className=''; out.textContent='running… (this drives the live browser, ~5–60s)';
  try{
    const r=await api('/admin/test',{method:'POST',body:JSON.stringify({model,prompt})});
    const j=await r.json();
    if(!r.ok){ out.textContent='Error: '+((j.error&&j.error.message)||r.status); btn.disabled=false; return; }
    // render image URLs as <img>, rest as text
    const ans=j.answer||'(empty)';
    const imgRe=/(https?:\/\/\S+\/images\/\S+)/g;
    if(imgRe.test(ans)){
      out.innerHTML=esc(ans).replace(imgRe, u=>`<img src="${u}"/>`)+`\n<span class="muted">· ${(j.latency_ms/1000).toFixed(1)}s</span>`;
    } else {
      out.textContent=ans+'  · '+(j.latency_ms/1000).toFixed(1)+'s';
    }
  }catch(e){ out.textContent='Network error'; }
  btn.disabled=false;
}

/* ───────────── actions ───────────── */
$('createKeyBtn').onclick=createKey;
$('keyName').addEventListener('keydown',e=>{ if(e.key==='Enter') createKey(); });
$('testBtn').onclick=runTest;
$('testPrompt').addEventListener('keydown',e=>{ if(e.key==='Enter') runTest(); });
$('restartBtn').onclick = async ()=>{
  if(!confirm('Recreate the browser session? (~20s)')) return;
  const b=$('restartBtn'); b.disabled=true; b.textContent='⟳ restarting…';
  try{ await api('/admin/restart-session',{method:'POST'}); }catch(e){}
  setTimeout(()=>{ b.disabled=false; b.textContent='⟳ restart'; tick(); }, 2000);
};
$('logoutBtn').onclick = async ()=>{
  try{ await api('/admin/logout',{method:'POST'}); }catch(e){}
  window.location='/login';
};

async function tick(){
  try{ const live = await (await api('/stats')).json(); render(live); }
  catch(e){ /* 401 redirects; ignore transient */ }
}

document.addEventListener('visibilitychange',()=>{ $('pausePill').style.display=document.hidden?'inline':'none'; });
setInterval(()=>{ if(!document.hidden) tick(); }, 4000);
tick(); loadKeys(); loadModels();

/* ───────────── three.js: particle sphere that breathes with traffic ───────────── */
(function(){
  const host=$('hero'); const W=host.clientWidth, H=host.clientHeight;
  const sc=new THREE.Scene();
  const cam=new THREE.PerspectiveCamera(60,W/H,0.1,100); cam.position.z=3.1;
  const rnd=new THREE.WebGLRenderer({antialias:true,alpha:true});
  rnd.setSize(W,H); rnd.setPixelRatio(Math.min(2,window.devicePixelRatio)); host.appendChild(rnd.domElement);
  const N=1400, pos=new Float32Array(N*3);
  for(let i=0;i<N;i++){ const u=Math.random(),v=Math.random();
    const th=2*Math.PI*u, ph=Math.acos(2*v-1), r=1.25;
    pos[i*3]=r*Math.sin(ph)*Math.cos(th); pos[i*3+1]=r*Math.sin(ph)*Math.sin(th); pos[i*3+2]=r*Math.cos(ph); }
  const g=new THREE.BufferGeometry(); g.setAttribute('position',new THREE.BufferAttribute(pos,3));
  const mat=new THREE.PointsMaterial({size:0.035,color:0x39d98a,transparent:true,opacity:0.9});
  const group=new THREE.Group(); sc.add(group);
  const pts=new THREE.Points(g,mat); group.add(pts);
  const wire=new THREE.Mesh(new THREE.IcosahedronGeometry(0.85,1),
    new THREE.MeshBasicMaterial({color:0x1d2940,wireframe:true,transparent:true,opacity:0.6}));
  group.add(wire);
  let mx=0,my=0,tmx=0,tmy=0;
  window.addEventListener('pointermove',e=>{
    tmx=(e.clientX/window.innerWidth)*2-1; tmy=(e.clientY/window.innerHeight)*2-1; });
  function lerp(a,b,t){return a+(b-a)*t;}
  function col(h){
    const g1=[0x39,0xd9,0x8a], a1=[0xff,0xb4,0x54], r1=[0xff,0x5c,0x7c];
    let c = h<0.5 ? a1.map((x,i)=>lerp(g1[i],x,h*2)) : r1.map((x,i)=>lerp(a1[i],x,(h-0.5)*2));
    return (c[0]<<16)|(c[1]<<8)|c[2]; }
  function loop(){
    requestAnimationFrame(loop);
    threeState.cur=lerp(threeState.cur,threeState.target,0.05);
    threeState.pulse=Math.max(0,threeState.pulse-0.02);
    const spin=0.0015 + threeState.cur*0.006 + threeState.pulse*0.02;
    pts.rotation.y+=spin; pts.rotation.x+=spin*0.4; wire.rotation.y-=spin*0.6;
    mx+=(tmx-mx)*0.06; my+=(tmy-my)*0.06;
    group.rotation.y += (mx*0.7 - group.rotation.y)*0.08;
    group.rotation.x += (my*0.5 - group.rotation.x)*0.08;
    cam.position.x += (mx*0.5 - cam.position.x)*0.05;
    cam.position.y += (-my*0.4 - cam.position.y)*0.05;
    cam.lookAt(0,0,0);
    const s=1+threeState.pulse*0.15+Math.sin(Date.now()/700)*0.01*(1+threeState.cur*4);
    pts.scale.set(s,s,s);
    const c=col(threeState.cur); mat.color.setHex(c); mat.opacity=0.55+threeState.pulse*0.4;
    rnd.render(sc,cam);
  }
  loop();
  window.addEventListener('resize',()=>{ const w=host.clientWidth,h=host.clientHeight;
    cam.aspect=w/h; cam.updateProjectionMatrix(); rnd.setSize(w,h); });
})();
</script>
</body>
</html>
"""
