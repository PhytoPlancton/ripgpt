"""Self-contained monitoring dashboard served at GET / and /dashboard.

Polls /stats (protected by the API key) and renders the panels the observability spec
called for: a top health strip (session · queue · error rate · wedge), per-model
latency, requests over time, and a recent-requests table — plus a Three.js particle
sphere that breathes with traffic and turns red when the session is unhealthy.
"""

DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>ripgpt · console</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
<script src="https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js"></script>
<style>
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
  .wrap{max-width:1180px;margin:0 auto;padding:18px 20px 60px}
  header{display:flex;align-items:center;gap:14px;flex-wrap:wrap}
  .logo{font-family:var(--mono);font-weight:700;letter-spacing:.5px;font-size:20px}
  .logo .pirate{filter:drop-shadow(0 0 6px #39d98a55)}
  .logo b{color:var(--green)}
  .spacer{flex:1}
  .pill{font-family:var(--mono);font-size:11px;color:var(--muted);border:1px solid var(--line);
        padding:4px 9px;border-radius:999px;background:var(--panel2)}
  button{font-family:var(--mono);font-size:12px;cursor:pointer;border:1px solid var(--line);
         background:var(--panel);color:var(--text);padding:7px 12px;border-radius:8px;transition:.15s}
  button:hover{border-color:#34507f;background:#16203200}
  button.warn{border-color:#5a2a3a;color:var(--red)}
  button:disabled{opacity:.5;cursor:not-allowed}
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
  .card h3{margin:0 0 10px;font-size:12px;text-transform:uppercase;letter-spacing:.8px;color:var(--muted);font-weight:600}
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
  .bar{height:6px;border-radius:4px;background:#19233500;background-color:#19233a;margin:10px 0 8px;overflow:hidden}
  .bar > i{display:block;height:100%;border-radius:4px;transition:width .4s}
  .ustats{display:flex;flex-wrap:wrap;gap:3px 12px;font-family:var(--mono);font-size:11px;color:var(--muted)}
  .ustats b{color:var(--text);font-weight:600}
  #keybar{display:none;margin-top:40px;text-align:center}
  input{font-family:var(--mono);background:var(--panel2);border:1px solid var(--line);color:var(--text);
        padding:9px 11px;border-radius:8px;width:340px;max-width:80%}
  .foot{margin-top:22px;font-family:var(--mono);font-size:11px;color:var(--muted);text-align:center}
</style>
</head>
<body>
<div class="wrap">
  <header>
    <div class="logo"><span class="pirate">🏴‍☠️</span> <b>ripgpt</b> · console</div>
    <span class="spacer"></span>
    <span class="pill" id="clock">—</span>
    <span class="pill" id="pausePill" style="display:none">⏸ polling paused (tab hidden)</span>
    <button id="restartBtn" class="warn" title="Recreate the browser session">⟳ restart session</button>
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
    <div class="tile"><div class="edge" id="t4e"></div><div class="k">Wedge risk</div><div class="v" id="t4v">—</div><div class="sub" id="t4s">consecutive empty/timeout</div></div>
  </div>

  <!-- charts -->
  <div class="grid row2" style="margin-top:12px">
    <div class="card"><h3>Requests over time · ok vs error (1-min)</h3><div class="chartbox"><canvas id="reqChart"></canvas></div></div>
    <div class="card"><h3>Latence par modèle · médiane &amp; 95ᵉ percentile (ms, dernière heure)</h3><div class="chartbox"><canvas id="latChart"></canvas></div></div>
  </div>

  <!-- usage by model (Perplexity-style cards) -->
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

  <div id="keybar">
    <p class="muted">Enter your ripgpt API key to view the console</p>
    <input id="keyInput" type="password" placeholder="API key (Bearer)" />
    <button onclick="saveKey()">connect</button>
  </div>
</div>

<script>
const $ = id => document.getElementById(id);
let KEY = localStorage.getItem('ripgpt_key') || '';
let reqChart, latChart, threeState = {target:0, cur:0, pulse:0};

function saveKey(){ KEY = $('keyInput').value.trim(); localStorage.setItem('ripgpt_key', KEY); $('keybar').style.display='none'; tick(); }
function needKey(){ $('keybar').style.display='block'; }

async function api(path, opts={}){
  opts.headers = Object.assign({'Authorization':'Bearer '+KEY}, opts.headers||{});
  const r = await fetch(path, opts);
  if(r.status===401){ needKey(); throw new Error('unauthorized'); }
  return r;
}

function rel(ts){ if(!ts) return '—'; const s=Math.max(0,Math.floor(Date.now()/1000-ts));
  if(s<60)return s+'s ago'; if(s<3600)return Math.floor(s/60)+'m ago'; return Math.floor(s/3600)+'h ago'; }
function setTile(n,val,sub,cls){ $('t'+n+'v').innerHTML=val; if(sub!==null)$('t'+n+'s').textContent=sub;
  $('t'+n+'v').className='v '+(cls||''); $('t'+n+'e').className='edge e-'+(cls||'dim'); }

function banner(msg,kind){ const b=$('banner'); if(!msg){b.style.display='none';return;}
  b.style.display='block'; b.textContent=msg;
  const c={bad:['#ff5c7c','#2a0f18'],warn:['#ffb454','#241a0c']}[kind]||['#56b6ff','#0c1b2a'];
  b.style.color=c[0]; b.style.borderColor=c[0]; b.style.background=c[1]; }

function render(s){
  $('clock').textContent = new Date().toLocaleTimeString();
  const st = s.live;
  // ── session tile ──
  const ssMap={logged_in:['LOGGED IN','ok'],logged_out:['LOGGED OUT','bad'],
               browser_dead:['BROWSER DEAD','bad'],starting:['STARTING','warn-c']};
  const ss=ssMap[st.session_state]||['?','dim'];
  setTile(1, ss[0], 'last ok '+rel(s.last_success_ts)+' · up '+(st.browser_uptime_s||0)+'s', ss[1]);
  // ── queue ──
  let qc = st.queue_depth>=5?'bad':st.queue_depth>=2?'warn-c':'ok';
  const inf = st.in_flight ? (st.in_flight.model+' · '+st.in_flight.age_s+'s') : 'idle';
  setTile(2, st.queue_depth, inf, qc);
  // ── error rate ──
  const er=s.error_rate_15m, prev=s.error_rate_prev_15m;
  let ec = er>0.20?'bad':er>0.05?'warn-c':'ok';
  const spike = (prev>0 && er>=2*prev && er>0.05);
  setTile(3, (er*100).toFixed(0)+'%', s.req_15m+' req · '+(spike?'▲ spiking':'prev '+(prev*100).toFixed(0)+'%'), ec);
  // ── wedge ──
  const w=s.consecutive_empty_or_timeout;
  let wc=w>=3?'bad':w>=2?'warn-c':'ok';
  setTile(4, w, w>=3?'⚠ back off / cooldown':'healthy', wc);

  // ── alerts ──
  if(st.session_state==='logged_out') banner('⚠ Session looks LOGGED OUT — refresh CHATGPT_COOKIES and restart. Nothing works until then.','bad');
  else if(st.session_state==='browser_dead') banner('⚠ Browser session is DEAD — hit “restart session”.','bad');
  else if(w>=3) banner('⚠ Possible rate-limit WEDGE ('+w+' empty/timeout in a row) — back off / pause sending.','bad');
  else if(er>0.20) banner('Error rate high ('+(er*100).toFixed(0)+'% over 15m).','warn');
  else if(spike) banner('Error rate spiking vs previous window.','warn');
  else banner(null);

  // ── charts ──
  drawReq(s.series); drawLat(s.by_model_latency);
  drawUsage(s);
  drawRecent(s.recent);

  // ── three.js state: health 0(calm green)..1(red), pulse on traffic ──
  let health = st.session_state==='logged_in' ? Math.min(1, er*2 + (w>=3?1:w*0.25)) : 1;
  threeState.target = health;
  const lastN = s.recent[0]; if(lastN){ const age=Date.now()/1000-lastN.ts; if(age<5) threeState.pulse=1; }
  $('heroState').innerHTML = ss[0]==='LOGGED IN'
    ? '<span class="ok">● online</span>' : '<span class="bad">● '+ss[0].toLowerCase()+'</span>';
}

function fmtN(n){ if(n>=1e6) return (n/1e6).toFixed(1)+'M'; if(n>=1000) return (n/1000).toFixed(n>=10000?0:1)+'k'; return ''+n; }
function drawUsage(s){
  const lt=s.lifetime||{requests:0,models:0,ctoks:0};
  $('usageTotals').textContent = lt.requests
    ? '· '+lt.requests+' requests · '+lt.models+' models'+(lt.images?' · '+lt.images+' images':'')+' · ~'+fmtN(lt.ctoks||0)+' tokens out'
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
      <div class="um"><span>${m.model}</span><span class="share">${share}% of traffic</span></div>
      <div class="un">${m.requests}</div>
      <div class="ul">requests · ${sr}% success</div>
      <div class="bar"><i style="width:${sr}%;background:${col}"></i></div>
      <div class="ustats">
        <span><b class="ok">${m.ok}</b> ok</span>
        <span><b style="color:${m.err?'var(--red)':'var(--muted)'}">${m.err}</b> err</span>
        <span>p95 <b>${p95}</b></span>
        ${m.images?('<span><b class="ok">🖼 '+m.images+'</b> img</span>'):('<span>out <b>'+tok+'</b></span>')}
        <span>last <b>${rel(m.last_ts)}</b></span>
      </div>
    </div>`;
  }).join('');
}

function statusCell(r){
  if(r.status==='ok') return '<span class="tag" style="color:var(--green);border-color:#1c3b2c">ok</span>';
  return '<span class="tag" style="color:var(--red);border-color:#3b1c28">'+(r.error_class||'error')+'</span>';
}
function drawRecent(recent){
  if(!recent||!recent.length){ $('recentBody').innerHTML='<tr><td colspan=5 class=muted>no requests yet</td></tr>'; return; }
  // p95 per model for relative latency colouring
  const bym={}; recent.forEach(r=>{ if(r.status==='ok'){(bym[r.model_res]=bym[r.model_res]||[]).push(r.latency_ms);} });
  const p95={}; for(const m in bym){ const a=bym[m].slice().sort((x,y)=>x-y); p95[m]=a[Math.floor(0.95*(a.length-1))]||0; }
  $('recentBody').innerHTML = recent.map(r=>{
    const slow = r.status==='ok' && p95[r.model_res] && r.latency_ms>p95[r.model_res];
    const mr = r.model_req!==r.model_res ? r.model_req+'→'+r.model_res : r.model_res;
    const lat = (r.latency_ms/1000).toFixed(1)+'s';
    return `<tr><td class=muted>${rel(r.ts)}</td><td>${mr}</td><td>${statusCell(r)}</td>
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

async function tick(){
  if(!KEY){ needKey(); return; }
  try{
    const live = await (await api('/stats')).json();
    render(live);
  }catch(e){ /* 401 handled; ignore transient */ }
}

$('restartBtn').onclick = async ()=>{
  if(!confirm('Recreate the browser session? (~20s)')) return;
  const b=$('restartBtn'); b.disabled=true; b.textContent='⟳ restarting…';
  try{ await api('/admin/restart-session',{method:'POST'}); }catch(e){}
  setTimeout(()=>{ b.disabled=false; b.textContent='⟳ restart session'; tick(); }, 2000);
};

document.addEventListener('visibilitychange',()=>{ $('pausePill').style.display=document.hidden?'inline':'none'; });
setInterval(()=>{ if(!document.hidden) tick(); }, 4000);
tick();

/* ───────────── three.js: particle sphere that breathes with traffic ───────────── */
(function(){
  const host=$('hero'); const W=host.clientWidth, H=host.clientHeight;
  const sc=new THREE.Scene();
  const cam=new THREE.PerspectiveCamera(60,W/H,0.1,100); cam.position.z=3.1;
  const rnd=new THREE.WebGLRenderer({antialias:true,alpha:true});
  rnd.setSize(W,H); rnd.setPixelRatio(Math.min(2,window.devicePixelRatio)); host.appendChild(rnd.domElement);
  // particles on a sphere
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
  // cursor tracking — anywhere on the page (normalised to the whole window), so the
  // sphere leans toward the pointer wherever it is, not only over the hero box.
  let mx=0,my=0,tmx=0,tmy=0;
  window.addEventListener('pointermove',e=>{
    tmx=(e.clientX/window.innerWidth)*2-1; tmy=(e.clientY/window.innerHeight)*2-1; });
  function lerp(a,b,t){return a+(b-a)*t;}
  function col(h){ // green -> amber -> red
    const g1=[0x39,0xd9,0x8a], a1=[0xff,0xb4,0x54], r1=[0xff,0x5c,0x7c];
    let c = h<0.5 ? a1.map((x,i)=>lerp(g1[i],x,h*2)) : r1.map((x,i)=>lerp(a1[i],x,(h-0.5)*2));
    return (c[0]<<16)|(c[1]<<8)|c[2]; }
  function loop(){
    requestAnimationFrame(loop);
    threeState.cur=lerp(threeState.cur,threeState.target,0.05);
    threeState.pulse=Math.max(0,threeState.pulse-0.02);
    const spin=0.0015 + threeState.cur*0.006 + threeState.pulse*0.02;
    pts.rotation.y+=spin; pts.rotation.x+=spin*0.4; wire.rotation.y-=spin*0.6;
    // ease toward the cursor and lean the whole sphere that way + parallax the camera
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
