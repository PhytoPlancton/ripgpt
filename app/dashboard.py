"""Self-contained admin console served at GET / and /dashboard.

Access is gated behind an admin login (username + password → signed HttpOnly session
cookie). The page polls /stats and drives the admin endpoints (keys, models, settings,
password, session controls) using the session cookie + a CSRF double-submit token. No API
key is ever stored in the browser.

Three exports:
  * SETUP_HTML     — shown when no admin credentials are configured yet.
  * LOGIN_HTML     — the login screen.
  * DASHBOARD_HTML — the authenticated, tabbed console.
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
  <div class="foot" id="foot">authorised access only</div>
</div>
<script>
const $=id=>document.getElementById(id);
if(location.hash==='#changed'){ $('foot').textContent='✓ mot de passe changé — reconnectez-vous'; }
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
       <code>.env</code>, then <code>docker compose up -d</code>. Once logged in you can
       change the password from the <b>Security</b> tab — no more .env editing. The API
       itself keeps working; only this console is locked until then.</p>
  </div>
</div>
</body></html>
"""

# ── dashboard (authenticated, tabbed) ─────────────────────────────────────────────
DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ripgpt · console</title>
<script src="/vendor/chart.js"></script>
<style>
""" + _BASE_CSS + r"""
  .wrap{max-width:1180px;margin:0 auto;padding:16px 20px 60px}
  header{display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  .logo{font-family:var(--mono);font-weight:700;letter-spacing:.5px;font-size:19px}
  .logo b{color:var(--green)}
  .spacer{flex:1}
  .pill{font-family:var(--mono);font-size:11px;color:var(--muted);border:1px solid var(--line);
        padding:4px 9px;border-radius:999px;background:var(--panel2)}
  /* tabs */
  nav.tabs{display:flex;gap:6px;margin:14px 0 12px;overflow-x:auto;white-space:nowrap;
           border-bottom:1px solid var(--line);padding-bottom:0}
  nav.tabs button{border:1px solid transparent;border-bottom:none;border-radius:8px 8px 0 0;
                  background:transparent;color:var(--muted);padding:9px 14px;font-size:13px}
  nav.tabs button:hover{color:var(--text)}
  nav.tabs button.active{color:var(--green);border-color:var(--line);background:var(--panel);
                         position:relative;top:1px}
  .tab{display:none}
  .tab.active{display:block;animation:fade .15s ease}
  @keyframes fade{from{opacity:.4}to{opacity:1}}
  #banner{display:none;margin:0 0 12px;padding:11px 14px;border-radius:10px;font-family:var(--mono);
          font-size:13px;border:1px solid;}
  .grid{display:grid;gap:12px}
  .strip{grid-template-columns:repeat(4,1fr)}
  @media(max-width:760px){.strip{grid-template-columns:repeat(2,1fr)}}
  .row2{grid-template-columns:1fr 1fr}
  @media(max-width:760px){.row2{grid-template-columns:1fr}}
  .tile{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 15px;position:relative;overflow:hidden}
  .tile .k{font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted)}
  .tile .v{font-family:var(--mono);font-size:24px;margin-top:6px;font-weight:600}
  .tile .sub{font-family:var(--mono);font-size:11px;color:var(--muted);margin-top:3px}
  .tile .edge{position:absolute;left:0;top:0;bottom:0;width:4px;background:var(--muted)}
  .ok{color:var(--green)} .warn-c{color:var(--amber)} .bad{color:var(--red)} .dim{color:var(--muted)}
  .e-ok{background:var(--green)} .e-warn{background:var(--amber)} .e-bad{background:var(--red)} .e-dim{background:var(--muted)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px 16px;margin-bottom:12px}
  .card h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);font-weight:600;
           display:flex;align-items:center;gap:10px}
  .card h3 .act{margin-left:auto;font-weight:400;text-transform:none;letter-spacing:0}
  .hero{background:linear-gradient(180deg,#0c1c14,#0d1420)}
  .hero .heroNum{font-family:var(--mono);font-size:48px;font-weight:700;color:var(--green);line-height:1.05;margin:4px 0}
  @media(max-width:760px){.hero .heroNum{font-size:36px}}
  .chartbox{position:relative;height:200px;width:100%}
  .chartbox.sm{height:170px}
  table{width:100%;border-collapse:collapse;font-family:var(--mono);font-size:12px}
  th{text-align:left;color:var(--muted);font-weight:500;padding:6px 8px;border-bottom:1px solid var(--line)}
  td{padding:6px 8px;border-bottom:1px solid #131c2c;white-space:nowrap}
  .tbl-wrap{max-height:360px;overflow:auto}
  .tag{font-size:10px;padding:1px 6px;border-radius:5px;border:1px solid var(--line)}
  .muted{color:var(--muted)}
  .usage-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(215px,1fr));gap:10px}
  .ucard{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:12px 13px}
  .ucard .um{font-family:var(--mono);font-size:13px;color:var(--cyan);font-weight:600;display:flex;justify-content:space-between;align-items:baseline;gap:8px}
  .ucard .um .share{font-size:10px;color:var(--muted);font-weight:400}
  .ucard .un{font-family:var(--mono);font-size:28px;font-weight:600;line-height:1.1;margin:7px 0 1px}
  .ucard .ul{font-size:10px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted)}
  .bar{height:6px;border-radius:4px;background-color:#19233a;margin:10px 0 8px;overflow:hidden}
  .bar > i{display:block;height:100%;border-radius:4px;transition:width .4s}
  .ustats{display:flex;flex-wrap:wrap;gap:3px 12px;font-family:var(--mono);font-size:11px;color:var(--muted)}
  .ustats b{color:var(--text);font-weight:600}
  .kform{display:flex;gap:8px;margin-bottom:12px;flex-wrap:wrap}
  .kform input{flex:1;min-width:160px}
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
  /* forms (settings + security) */
  .srow{display:grid;grid-template-columns:200px 130px 1fr;gap:10px;align-items:center;padding:9px 0;border-bottom:1px solid #131c2c}
  @media(max-width:640px){.srow{grid-template-columns:1fr}}
  .srow label{font-family:var(--mono);font-size:12px;color:var(--text)}
  .srow input{width:100%}
  .srow .shelp{font-size:11px;color:var(--muted)}
  .srow .suse{font-family:var(--mono);font-size:11px;color:var(--muted)}
  .formmsg{font-family:var(--mono);font-size:12px;min-height:16px;margin-top:10px}
  .fld{margin-bottom:12px;max-width:360px}
  .fld label{display:block;font-size:11px;text-transform:uppercase;letter-spacing:.6px;color:var(--muted);margin-bottom:5px}
  .fld input{width:100%}
  .kv{display:grid;grid-template-columns:160px 1fr;gap:6px 12px;font-family:var(--mono);font-size:12px}
  .kv .kk{color:var(--muted)}
  .foot{margin-top:20px;font-family:var(--mono);font-size:11px;color:var(--muted);text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo"><span>🏴‍☠️</span> <b>ripgpt</b> · console</div>
    <span class="spacer"></span>
    <span class="pill" id="clock">—</span>
    <span class="pill" id="ratePill" title="protection anti-ban : requêtes acceptées aujourd'hui">—</span>
    <span class="pill" id="pausePill" style="display:none">⏸ tab hidden</span>
    <button id="logoutBtn" title="Se déconnecter">⎋ logout</button>
  </header>

  <nav class="tabs" id="tabs">
    <button data-tab="overview" class="active">Vue d'ensemble</button>
    <button data-tab="usage">Usage &amp; Coût</button>
    <button data-tab="keys">Clés API</button>
    <button data-tab="models">Modèles</button>
    <button data-tab="settings">Réglages</button>
    <button data-tab="security">Sécurité</button>
    <button data-tab="session">Session</button>
  </nav>

  <div id="banner"></div>

  <!-- ── Overview ── -->
  <section id="tab-overview" class="tab active">
    <div class="card hero">
      <div class="k" style="font-size:11px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted)">Coût API évité · total</div>
      <div class="heroNum" id="heroCost">—</div>
      <div class="sub muted" id="heroSub"></div>
    </div>
    <div class="grid strip">
      <div class="tile"><div class="edge" id="t1e"></div><div class="k">Session</div><div class="v" id="t1v">—</div><div class="sub" id="t1s"></div></div>
      <div class="tile"><div class="edge" id="t2e"></div><div class="k">Queue depth</div><div class="v" id="t2v">—</div><div class="sub" id="t2s">in-flight</div></div>
      <div class="tile"><div class="edge" id="t3e"></div><div class="k">Error rate · 15m</div><div class="v" id="t3v">—</div><div class="sub" id="t3s"></div></div>
      <div class="tile"><div class="edge" id="t4e"></div><div class="k">Anti-ban · jour</div><div class="v" id="t4v">—</div><div class="sub" id="t4s">requêtes / limite</div></div>
    </div>
    <div class="card"><h3>Requêtes dans le temps · ok vs erreur (1 min)</h3><div class="chartbox"><canvas id="reqChart"></canvas></div></div>
  </section>

  <!-- ── Usage & Cost ── -->
  <section id="tab-usage" class="tab">
    <div class="grid row2">
      <div class="card"><h3>Répartition du coût par modèle</h3><div class="chartbox sm"><canvas id="costChart"></canvas></div></div>
      <div class="card"><h3>Latence par modèle · médiane &amp; p95 (ms, 1h)</h3><div class="chartbox sm"><canvas id="latChart"></canvas></div></div>
    </div>
    <div class="card">
      <h3>Usage par modèle · all-time <span id="usageTotals" class="muted" style="text-transform:none;letter-spacing:0;font-weight:400"></span></h3>
      <div class="usage-grid" id="usageGrid"><div class="muted">waiting…</div></div>
    </div>
    <div class="card">
      <h3>Requêtes récentes <span class="act"><a href="/admin/usage.csv"><button>⭳ CSV</button></a></span></h3>
      <div class="tbl-wrap"><table>
        <thead><tr><th>quand</th><th>modèle</th><th>statut</th><th>latence</th><th>sortie</th></tr></thead>
        <tbody id="recentBody"><tr><td colspan="5" class="muted">waiting…</td></tr></tbody>
      </table></div>
    </div>
  </section>

  <!-- ── API Keys ── -->
  <section id="tab-keys" class="tab">
    <div class="card">
      <h3>Clés API</h3>
      <div class="kform">
        <input id="keyName" placeholder="nom de la clé (ex: tailr-prospection)" maxlength="80"/>
        <button class="go" id="createKeyBtn">+ créer</button>
      </div>
      <div class="secretbox" id="secretBox">
        <div class="lbl">⚠ Copie cette clé maintenant — elle n'est affichée qu'une seule fois.</div>
        <div class="val" id="secretVal" title="cliquer pour copier"></div>
      </div>
      <div class="tbl-wrap"><table>
        <thead><tr><th>nom</th><th>préfixe</th><th>requêtes</th><th>~coût API</th><th>utilisée</th><th></th></tr></thead>
        <tbody id="keysBody"><tr><td colspan="6" class="muted">chargement…</td></tr></tbody>
      </table></div>
    </div>
  </section>

  <!-- ── Models ── -->
  <section id="tab-models" class="tab">
    <div class="card">
      <h3>Modèles <span class="act muted">activer / désactiver</span></h3>
      <div id="modelsBox"><div class="muted">chargement…</div></div>
    </div>
    <div class="card">
      <h3>Test prompt <span class="act muted">passe par le navigateur live</span></h3>
      <div class="kform">
        <select id="testModel" style="flex:none;min-width:160px"></select>
        <input id="testPrompt" placeholder="pose une question…" maxlength="8000"/>
        <button class="go" id="testBtn">▷ run</button>
      </div>
      <div id="testOut" class="muted">—</div>
    </div>
  </section>

  <!-- ── Settings ── -->
  <section id="tab-settings" class="tab">
    <div class="card">
      <h3>Protection anti-ban &amp; débit <span class="act muted">appliqué à chaud, sans redémarrage</span></h3>
      <div id="settingsForm"><div class="muted">chargement…</div></div>
      <div style="margin-top:12px;display:flex;gap:10px;align-items:center">
        <button class="go" id="saveSettingsBtn">Enregistrer</button>
        <span class="formmsg" id="settingsMsg"></span>
      </div>
    </div>
  </section>

  <!-- ── Security ── -->
  <section id="tab-security" class="tab">
    <div class="card">
      <h3>Changer le mot de passe admin</h3>
      <div class="fld"><label>mot de passe actuel</label><input id="pwCurrent" type="password" autocomplete="current-password"/></div>
      <div class="fld"><label>nouveau nom d'utilisateur (optionnel)</label><input id="pwUser" autocomplete="username" placeholder="laisser vide = inchangé"/></div>
      <div class="fld"><label>nouveau mot de passe (≥ 8 car.)</label><input id="pwNew" type="password" autocomplete="new-password"/></div>
      <div class="fld"><label>confirmer</label><input id="pwConfirm" type="password" autocomplete="new-password"/></div>
      <button class="go" id="changePwBtn">Changer le mot de passe</button>
      <span class="formmsg" id="pwMsg"></span>
      <p class="muted" style="font-size:12px;margin-top:14px">Changer le mot de passe déconnecte toutes les sessions.</p>
    </div>
    <div class="card">
      <h3>Sessions</h3>
      <button class="warn" id="logoutAllBtn">Déconnecter partout</button>
      <span class="muted" style="font-size:12px;margin-left:10px">invalide tous les cookies de session en cours</span>
    </div>
  </section>

  <!-- ── Session ── -->
  <section id="tab-session" class="tab">
    <div class="card">
      <h3>État du navigateur ChatGPT</h3>
      <div class="kv" id="sessionKv"><div class="kk">—</div><div>—</div></div>
    </div>
    <div class="card">
      <h3>Contrôles</h3>
      <div style="display:flex;gap:10px;flex-wrap:wrap">
        <button id="pauseBtn" title="mettre en pause / reprendre le proxy">⏸ pause</button>
        <button class="warn" id="restartBtn" title="recréer la session navigateur (~20s)">⟳ restart</button>
      </div>
      <span class="formmsg" id="sessionMsg"></span>
    </div>
  </section>

  <div class="foot">refresh 4s · pauses when tab hidden · ⚠ single browser behind — keep the pace calm</div>
</div>

<script>
const $ = id => document.getElementById(id);
let reqChart, latChart, costChart;
let LAST_STATS = {}, KEYS_CACHE = [], SETTINGS_BOUNDS = null, SETTINGS_LOADED = false;

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
  if(s<60)return s+'s'; if(s<3600)return Math.floor(s/60)+'m'; if(s<86400)return Math.floor(s/3600)+'h'; return Math.floor(s/86400)+'j'; }
function fmtN(n){ if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1000) return (n/1000).toFixed(n>=10000?0:1)+'k'; return ''+n; }
function fmtMoney(v){ v=v||0; if(v>=1000) return '$'+(v/1000).toFixed(2)+'k'; if(v>=1) return '$'+v.toFixed(2); if(v>=0.01) return '$'+v.toFixed(3); return v>0?('$'+v.toFixed(4)):'$0'; }
function setTile(n,val,sub,cls){ $('t'+n+'v').innerHTML=val; if(sub!==null)$('t'+n+'s').textContent=sub;
  $('t'+n+'v').className='v '+(cls||''); $('t'+n+'e').className='edge e-'+(cls||'dim'); }
function banner(msg,kind){ const b=$('banner'); if(!msg){b.style.display='none';return;}
  b.style.display='block'; b.textContent=msg;
  const c={bad:['#ff5c7c','#2a0f18'],warn:['#ffb454','#241a0c']}[kind]||['#56b6ff','#0c1b2a'];
  b.style.color=c[0]; b.style.borderColor=c[0]; b.style.background=c[1]; }

/* ── tabs ── */
function switchTab(name){
  document.querySelectorAll('nav.tabs button').forEach(b=>b.classList.toggle('active', b.dataset.tab===name));
  document.querySelectorAll('.tab').forEach(s=>s.classList.toggle('active', s.id==='tab-'+name));
  location.hash = name;
  if(name==='keys') loadKeys();
  if(name==='models') loadModels();
  if(name==='settings') loadSettings();
  setTimeout(()=>{ [reqChart,latChart,costChart].forEach(c=>{ try{c&&c.resize();}catch(e){} }); }, 60);
}
document.querySelectorAll('nav.tabs button').forEach(b=> b.onclick=()=>switchTab(b.dataset.tab));

/* ── main render (Overview + Usage + Session live) ── */
function render(s){
  LAST_STATS=s;
  $('clock').textContent = new Date().toLocaleTimeString();
  const st = s.live||{}, rate = s.rate||{}, lt = s.lifetime||{};
  // hero cost
  $('heroCost').textContent = '~'+fmtMoney(lt.cost);
  $('heroSub').textContent = (lt.requests||0)+' requêtes · '+(lt.models||0)+' modèles · ~'+fmtN(lt.ctoks||0)+' tokens out · borne haute (sans cache)';
  // rate pill
  if(rate.per_day){ $('ratePill').textContent='🛡 '+rate.per_day.used+'/'+(rate.per_day.cap||'∞')+' /j'; }
  // tiles
  const ssMap={logged_in:['LOGGED IN','ok'],logged_out:['LOGGED OUT','bad'],browser_dead:['BROWSER DEAD','bad'],starting:['STARTING','warn-c']};
  const ss=ssMap[st.session_state]||['?','dim'];
  setTile(1, ss[0], 'ok '+rel(s.last_success_ts)+' · up '+(st.browser_uptime_s||0)+'s', ss[1]);
  const qc = st.queue_depth>=5?'bad':st.queue_depth>=2?'warn-c':'ok';
  setTile(2, st.queue_depth!=null?st.queue_depth:'—', st.in_flight?(st.in_flight.model+' · '+st.in_flight.age_s+'s'):'idle', qc);
  const er=s.error_rate_15m||0, prev=s.error_rate_prev_15m||0, spike=(prev>0 && er>=2*prev && er>0.05);
  setTile(3, (er*100).toFixed(0)+'%', (s.req_15m||0)+' req · '+(spike?'▲ spiking':'prev '+(prev*100).toFixed(0)+'%'), er>0.20?'bad':er>0.05?'warn-c':'ok');
  if(rate.cooldown_active){ setTile(4,'COOLDOWN', rate.cooldown_remaining_s+'s restants','bad'); }
  else { const d=rate.per_day||{used:0,cap:0}; const pct=d.cap?d.used/d.cap:0;
         setTile(4, d.used+'/'+(d.cap||'∞'), 'requêtes / limite', pct>=0.8?'warn-c':'ok'); }
  // banners (visible on every tab)
  const w=s.consecutive_empty_or_timeout||0;
  if(st.session_state==='logged_out') banner('⚠ Session LOGGED OUT — rafraîchir CHATGPT_COOKIES et restart. Rien ne marche tant que ce n\'est pas fait.','bad');
  else if(st.session_state==='browser_dead') banner('⚠ Session navigateur MORTE — onglet Session → restart.','bad');
  else if(rate.cooldown_active) banner('🛡 Protection anti-ban : COOLDOWN actif ('+rate.cooldown_remaining_s+'s) — requêtes en pause pour laisser le compte ChatGPT respirer.','warn');
  else if(w>=3) banner('⚠ Possible WEDGE ('+w+' vides/timeouts d\'affilée) — lever le pied.','bad');
  else if(rate.per_day && rate.per_day.cap && rate.per_day.used>=0.8*rate.per_day.cap) banner('⚠ Proche de la limite quotidienne anti-ban ('+rate.per_day.used+'/'+rate.per_day.cap+').','warn');
  else if(er>0.20) banner('Taux d\'erreur élevé ('+(er*100).toFixed(0)+'% sur 15m).','warn');
  else banner(null);
  // charts + lists
  drawReq(s.series||[]);
  drawUsage(s); drawCost(s.by_model_usage||[]); drawLat(s.by_model_latency||[]); drawRecent(s.recent||[]);
  renderKeysUsage();
  renderSession(st, s);
  updateSettingsLive(rate);
}

/* ── Usage & Cost ── */
function drawUsage(s){
  const lt=s.lifetime||{requests:0,models:0,ctoks:0,cost:0};
  $('usageTotals').innerHTML = lt.requests
    ? '· '+lt.requests+' requêtes · '+lt.models+' modèles · ~'+fmtN(lt.ctoks||0)+' tokens out · <b style="color:var(--green)">~'+fmtMoney(lt.cost)+'</b> évités'
    : '';
  const arr=s.by_model_usage||[];
  if(!arr.length){ $('usageGrid').innerHTML='<div class="muted">aucune requête</div>'; return; }
  const total=arr.reduce((a,m)=>a+m.requests,0)||1;
  $('usageGrid').innerHTML = arr.map(m=>{
    const sr=Math.round(m.success_rate*100), col=sr>=95?'var(--green)':sr>=80?'var(--amber)':'var(--red)';
    const share=Math.round(100*m.requests/total), p95=m.p95_latency_ms?((m.p95_latency_ms/1000).toFixed(1)+'s'):'—';
    return `<div class="ucard">
      <div class="um"><span>${esc(m.model)}</span><span class="share">${share}%</span></div>
      <div class="un">${m.requests}</div><div class="ul">requêtes · ${sr}% ok</div>
      <div class="bar"><i style="width:${sr}%;background:${col}"></i></div>
      <div class="ustats"><span><b class="ok">${m.ok}</b> ok</span><span><b style="color:${m.err?'var(--red)':'var(--muted)'}">${m.err}</b> err</span>
        <span>p95 <b>${p95}</b></span><span>~<b style="color:var(--green)">${fmtMoney(m.cost)}</b></span><span>${rel(m.last_ts)}</span></div>
    </div>`;
  }).join('');
}
function drawCost(arr){
  const withCost=arr.filter(m=>m.cost>0);
  const labels=withCost.map(m=>m.model), data=withCost.map(m=>+m.cost.toFixed(4));
  const palette=['#39d98a','#56b6ff','#b58cff','#ffb454','#ff5c7c','#5d6e8c','#2ec4b6'];
  if(!withCost.length){ if(costChart){costChart.destroy();costChart=null;} return; }
  if(!costChart){
    costChart=new Chart($('costChart'),{type:'doughnut',
      data:{labels,datasets:[{data,backgroundColor:labels.map((_,i)=>palette[i%palette.length]),borderColor:'#0d1420',borderWidth:2}]},
      options:{responsive:true,maintainAspectRatio:false,plugins:{legend:{position:'right',labels:{color:'#5d6e8c',boxWidth:10,font:{size:10}}},
        tooltip:{callbacks:{label:c=>c.label+': $'+(+c.raw).toFixed(2)}}}}});
  } else { costChart.data.labels=labels; costChart.data.datasets[0].data=data;
           costChart.data.datasets[0].backgroundColor=labels.map((_,i)=>palette[i%palette.length]); costChart.update('none'); }
}
function statusCell(r){ return r.status==='ok'
  ? '<span class="tag" style="color:var(--green);border-color:#1c3b2c">ok</span>'
  : '<span class="tag" style="color:var(--red);border-color:#3b1c28">'+esc(r.error_class||'error')+'</span>'; }
function drawRecent(recent){
  if(!recent.length){ $('recentBody').innerHTML='<tr><td colspan=5 class=muted>aucune requête</td></tr>'; return; }
  $('recentBody').innerHTML = recent.map(r=>{
    const mr=r.model_req!==r.model_res?r.model_req+'→'+r.model_res:r.model_res, lat=(r.latency_ms/1000).toFixed(1)+'s';
    return `<tr><td class=muted>${rel(r.ts)}</td><td>${esc(mr)}</td><td>${statusCell(r)}</td>
      <td>${r.status==='ok'?lat:'—'}</td><td class=muted>${r.status!=='ok'?'—':(r.images?('🖼 '+r.images):('~'+(r.ptoks_est+r.ctoks_est)))}</td></tr>`;
  }).join('');
}
function drawReq(series){
  const labels=series.map(d=>new Date(d.t*1000).toLocaleTimeString([], {hour:'2-digit',minute:'2-digit'}));
  const ok=series.map(d=>d.ok), err=series.map(d=>d.err);
  if(!reqChart){ reqChart=new Chart($('reqChart'),{type:'line',
    data:{labels,datasets:[{label:'ok',data:ok,borderColor:'#39d98a',backgroundColor:'#39d98a22',fill:true,tension:.3,pointRadius:0,borderWidth:2},
      {label:'erreur',data:err,borderColor:'#ff5c7c',backgroundColor:'#ff5c7c22',fill:true,tension:.3,pointRadius:0,borderWidth:2}]},options:chartOpts()});
  } else { reqChart.data.labels=labels; reqChart.data.datasets[0].data=ok; reqChart.data.datasets[1].data=err; reqChart.update('none'); }
}
function drawLat(ml){
  const labels=ml.map(m=>m.model+' ('+m.count+')');
  if(!latChart){ latChart=new Chart($('latChart'),{type:'bar',
    data:{labels,datasets:[{label:'médiane',data:ml.map(m=>m.p50),backgroundColor:'#56b6ff'},{label:'p95',data:ml.map(m=>m.p95),backgroundColor:'#b58cff'}]},
    options:Object.assign(chartOpts(),{indexAxis:'y'})});
  } else { latChart.data.labels=labels; latChart.data.datasets[0].data=ml.map(m=>m.p50); latChart.data.datasets[1].data=ml.map(m=>m.p95); latChart.update('none'); }
}
function chartOpts(){return{responsive:true,maintainAspectRatio:false,animation:false,
  plugins:{legend:{labels:{color:'#5d6e8c',boxWidth:10,font:{size:10}}}},
  scales:{x:{ticks:{color:'#3f4d68',font:{size:9},maxRotation:0},grid:{color:'#131c2c'}},
          y:{ticks:{color:'#3f4d68',font:{size:9}},grid:{color:'#131c2c'},beginAtZero:true}}};}

/* ── Keys ── */
async function loadKeys(){ try{ const j=await (await api('/admin/keys')).json(); KEYS_CACHE=j.keys||[]; renderKeysUsage(); }catch(e){} }
function renderKeysUsage(){
  const usage={}; (LAST_STATS.by_key_usage||[]).forEach(k=>usage[k.key_id]=k);
  if(!KEYS_CACHE.length){ $('keysBody').innerHTML='<tr><td colspan=6 class=muted>aucune clé — créez-en une</td></tr>'; return; }
  $('keysBody').innerHTML=KEYS_CACHE.map(k=>{
    const u=usage[k.id]||{requests:0,last_ts:null,cost:0}, rev=k.revoked;
    const nm=esc(k.name)+(rev?' <span class="tag" style="color:var(--red);border-color:#3b1c28">révoquée</span>':'');
    const act=rev?'':`<button class="warn" onclick="revokeKey('${esc(k.id)}')">révoquer</button>`;
    return `<tr style="${rev?'opacity:.5':''}"><td>${nm}</td><td class=muted>${esc(k.prefix)}…</td>
      <td>${u.requests||0}</td><td style="color:var(--green)">~${fmtMoney(u.cost)}</td>
      <td class=muted>${u.last_ts?rel(u.last_ts):(k.last_used?rel(k.last_used):'jamais')}</td>
      <td style="text-align:right">${act}</td></tr>`;
  }).join('');
}
async function createKey(){
  const name=$('keyName').value.trim()||'key'; const btn=$('createKeyBtn'); btn.disabled=true;
  try{ const j=await (await api('/admin/keys',{method:'POST',body:JSON.stringify({name})})).json();
    $('keyName').value=''; const box=$('secretBox'), val=$('secretVal'); val.textContent=j.key; box.style.display='block';
    val.onclick=()=>{ navigator.clipboard&&navigator.clipboard.writeText(j.key); val.textContent=j.key+'  ✓ copié'; };
    await loadKeys();
  }catch(e){} btn.disabled=false;
}
async function revokeKey(id){ if(!confirm('Révoquer cette clé ? Les clients qui l\'utilisent reçoivent 401 immédiatement.')) return;
  try{ await api('/admin/keys/'+encodeURIComponent(id)+'/revoke',{method:'POST'}); await loadKeys(); }catch(e){} }

/* ── Models ── */
async function loadModels(){
  try{ const j=await (await api('/admin/models')).json(); const ms=j.models||[];
    $('modelsBox').innerHTML=ms.map(m=>`<div class="modelrow"><span class="mid">${esc(m.id)}${m.image?' <span class="muted">🖼</span>':''}${m.temporary?'':' <span class="muted">·persist</span>'}</span>
      <div class="sw ${m.enabled?'on':''}" onclick="toggleModel('${esc(m.id)}',${!m.enabled})"><i></i></div></div>`).join('');
    $('testModel').innerHTML=ms.filter(m=>m.enabled).map(m=>`<option value="${esc(m.id)}">${esc(m.id)}</option>`).join('');
  }catch(e){}
}
async function toggleModel(id,enabled){ try{ await api('/admin/models/toggle',{method:'POST',body:JSON.stringify({model:id,enabled})}); await loadModels(); }catch(e){} }
async function runTest(){
  const prompt=$('testPrompt').value.trim(); if(!prompt) return;
  const model=$('testModel').value||'auto', out=$('testOut'), btn=$('testBtn'); btn.disabled=true;
  out.className=''; out.textContent='running… (navigateur live, ~5–60s)';
  try{ const r=await api('/admin/test',{method:'POST',body:JSON.stringify({model,prompt})}); const j=await r.json();
    if(!r.ok){ out.textContent='Erreur: '+((j.error&&j.error.message)||r.status); btn.disabled=false; return; }
    const ans=j.answer||'(vide)', imgRe=/(https?:\/\/\S+\/images\/\S+)/g;
    if(imgRe.test(ans)) out.innerHTML=esc(ans).replace(imgRe,u=>`<img src="${u}"/>`)+`\n<span class="muted">· ${(j.latency_ms/1000).toFixed(1)}s</span>`;
    else out.textContent=ans+'  · '+(j.latency_ms/1000).toFixed(1)+'s';
  }catch(e){ out.textContent='Network error'; } btn.disabled=false;
}

/* ── Settings ── */
async function loadSettings(){
  if(SETTINGS_LOADED) return;
  try{
    const j=await (await api('/admin/settings')).json(); SETTINGS_BOUNDS=j.bounds;
    const order=['rate_per_min','rate_per_hour','rate_per_day','rate_min_interval_s','breaker_threshold','breaker_cooldown_s','max_queue_depth'];
    $('settingsForm').innerHTML=order.filter(k=>j.bounds[k]).map(k=>{
      const b=j.bounds[k], step=b.type==='int'?'1':'any', v=j.values[k];
      return `<div class="srow"><label title="${esc(k)}">${esc(b.label)}</label>
        <input type="number" id="set_${k}" min="${b.min}" max="${b.max}" step="${step}" value="${v}"/>
        <span class="shelp">${esc(b.help||'')} <span class="suse" id="use_${k}"></span></span></div>`;
    }).join('');
    SETTINGS_LOADED=true; updateSettingsLive(LAST_STATS.rate||{});
  }catch(e){}
}
function updateSettingsLive(rate){
  if(!SETTINGS_LOADED) return;
  const map={rate_per_min:rate.per_min,rate_per_hour:rate.per_hour,rate_per_day:rate.per_day};
  for(const k in map){ const el=$('use_'+k); if(el && map[k]) el.textContent='· actuel '+map[k].used+'/'+(map[k].cap||'∞'); }
  const cd=$('use_breaker_cooldown_s'); if(cd) cd.textContent = rate.cooldown_active?('· ⏸ cooldown '+rate.cooldown_remaining_s+'s'):('· '+(rate.breaker_trips||0)+' déclenchements');
}
async function saveSettings(){
  if(!SETTINGS_BOUNDS) return;
  const patch={}; for(const k in SETTINGS_BOUNDS){ const el=$('set_'+k); if(el) patch[k]=Number(el.value); }
  const btn=$('saveSettingsBtn'), msg=$('settingsMsg'); btn.disabled=true; msg.textContent=''; msg.style.color='';
  try{ const r=await api('/admin/settings',{method:'POST',body:JSON.stringify(patch)}); const j=await r.json();
    if(r.ok){ msg.style.color='var(--green)'; msg.textContent='✓ enregistré (appliqué à chaud)'; }
    else { msg.style.color='var(--red)'; msg.textContent=(j.error&&j.error.message)||('Erreur '+r.status); }
  }catch(e){ msg.style.color='var(--red)'; msg.textContent='Network error'; } btn.disabled=false;
}

/* ── Security ── */
async function changePassword(){
  const cur=$('pwCurrent').value, nw=$('pwNew').value, cf=$('pwConfirm').value, user=$('pwUser').value.trim();
  const msg=$('pwMsg'); msg.style.color=''; msg.textContent='';
  if(nw.length<8){ msg.style.color='var(--red)'; msg.textContent='Le nouveau mot de passe doit faire ≥ 8 caractères.'; return; }
  if(nw!==cf){ msg.style.color='var(--red)'; msg.textContent='La confirmation ne correspond pas.'; return; }
  const btn=$('changePwBtn'); btn.disabled=true;
  try{ const r=await api('/admin/password',{method:'POST',body:JSON.stringify({current_password:cur,new_password:nw,username:user||undefined})});
    if(r.ok){ window.location='/login#changed'; return; }
    const j=await r.json().catch(()=>({})); msg.style.color='var(--red)'; msg.textContent=(j.error&&j.error.message)||('Erreur '+r.status);
  }catch(e){ msg.style.color='var(--red)'; msg.textContent='Network error'; } btn.disabled=false;
}
async function logoutEverywhere(){ if(!confirm('Déconnecter toutes les sessions ?')) return;
  try{ await api('/admin/logout',{method:'POST'}); }catch(e){} window.location='/login'; }

/* ── Session ── */
function renderSession(st, s){
  const rows=[['état', (st.session_state||'—')],['uptime', (st.browser_uptime_s||0)+'s'],
    ['queue', (st.queue_depth!=null?st.queue_depth:'—')],['in-flight', st.in_flight?(st.in_flight.model+' · '+st.in_flight.age_s+'s'):'idle'],
    ['dernier succès', rel(s.last_success_ts)],['requêtes 15m', (s.req_15m||0)]];
  $('sessionKv').innerHTML=rows.map(r=>`<div class="kk">${r[0]}</div><div>${esc(''+r[1])}</div>`).join('');
}
async function doRestart(){ if(!confirm('Recréer la session navigateur ? (~20s)')) return;
  const b=$('restartBtn'), msg=$('sessionMsg'); b.disabled=true; b.textContent='⟳ restart…'; msg.textContent='';
  try{ await api('/admin/restart-session',{method:'POST'}); msg.style.color='var(--green)'; msg.textContent='✓ relancé'; }catch(e){}
  setTimeout(()=>{ b.disabled=false; b.textContent='⟳ restart'; tick(); }, 2000);
}
async function doPause(){ const b=$('pauseBtn'), msg=$('sessionMsg'); b.disabled=true;
  try{ const j=await (await api('/admin/pause',{method:'POST',body:JSON.stringify({})})).json();
    b.textContent=j.paused?'▶ reprendre':'⏸ pause'; msg.style.color='var(--amber)'; msg.textContent=j.paused?'proxy en pause':'proxy actif';
  }catch(e){} b.disabled=false;
}

/* ── wiring ── */
$('createKeyBtn').onclick=createKey; $('keyName').addEventListener('keydown',e=>{if(e.key==='Enter')createKey();});
$('testBtn').onclick=runTest; $('testPrompt').addEventListener('keydown',e=>{if(e.key==='Enter')runTest();});
$('saveSettingsBtn').onclick=saveSettings;
$('changePwBtn').onclick=changePassword; $('logoutAllBtn').onclick=logoutEverywhere;
$('restartBtn').onclick=doRestart; $('pauseBtn').onclick=doPause;
$('logoutBtn').onclick=async()=>{ try{ await api('/admin/logout',{method:'POST'}); }catch(e){} window.location='/login'; };

async function tick(){ try{ const s=await (await api('/stats')).json(); render(s); }catch(e){} }
document.addEventListener('visibilitychange',()=>{ $('pausePill').style.display=document.hidden?'inline':'none'; });
setInterval(()=>{ if(!document.hidden) tick(); }, 4000);

// initial tab from hash, then first poll + lazy loads
const initTab=(location.hash||'#overview').slice(1);
if(document.getElementById('tab-'+initTab)) switchTab(initTab);
tick(); loadKeys(); loadModels();
</script>
</body></html>
"""
