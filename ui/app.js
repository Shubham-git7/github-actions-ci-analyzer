"use strict";
const $=id=>document.getElementById(id);
const qs=new URLSearchParams(location.search);
const DEMO=qs.has("demo");
const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const short=s=>String(s||"").slice(0,8);
const STAGES=[["detect","Detect"],["redact","Redact"],["analyze","Analyze"],["runbook","Runbook"],["propose","Propose"],["pr","PR"],["approve","Approve"],["execute","Execute"]];
let BANNER=false,CFG={},TOKEN="",me={sub:"",groups:[]},runs=[],sel=null,audit=[],timer=null,lastKey="";
const store={get:k=>{try{return sessionStorage.getItem(k)}catch{return null}},set:(k,v)=>{try{sessionStorage.setItem(k,v)}catch{}},clear:()=>{try{sessionStorage.clear()}catch{}}};

/* ---------- theme ---------- */
function setTheme(t){document.documentElement.dataset.theme=t;try{localStorage.setItem("theme",t)}catch{}}
try{const t=new URLSearchParams(location.search).get("theme")||localStorage.getItem("theme");if(t)setTheme(t)}catch{}
$("theme").onclick=()=>setTheme(document.documentElement.dataset.theme==="dark"?"light":"dark");

/* ---------- toast ---------- */
function toast(msg,kind=""){const d=document.createElement("div");d.className="toast "+kind;d.textContent=msg;$("toasts").append(d);setTimeout(()=>{d.style.opacity="0";d.style.transition="opacity .4s";setTimeout(()=>d.remove(),400)},4200)}

/* ---------- auth (Cognito hosted UI, code + PKCE) ---------- */
const b64u=b=>btoa(String.fromCharCode(...new Uint8Array(b))).replace(/\+/g,"-").replace(/\//g,"_").replace(/=+$/,"");
const redirectUri=location.origin+"/";
const AUTHB=()=>CFG.auth_base||("https://"+CFG.domain);
function parseJwt(t){try{const p=t.split(".")[1].replace(/-/g,"+").replace(/_/g,"/");return JSON.parse(atob(p+"=".repeat((4-p.length%4)%4)))}catch{return{}}}
function applyToken(t){TOKEN=t;const c=parseJwt(t);me={sub:c.sub||"",groups:c["cognito:groups"]||[]};store.set("tok",t)}
async function login(){
  const v=b64u(crypto.getRandomValues(new Uint8Array(32)));store.set("pkce",v);
  const ch=b64u(await crypto.subtle.digest("SHA-256",new TextEncoder().encode(v)));
  location.href=`${AUTHB()}/oauth2/authorize?response_type=code&client_id=${CFG.client_id}&redirect_uri=${encodeURIComponent(redirectUri)}&scope=openid+email&code_challenge_method=S256&code_challenge=${ch}`}
async function finishLogin(){
  const code=qs.get("code");if(!code)return;
  const r=await fetch(`${AUTHB()}/oauth2/token`,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body:new URLSearchParams({grant_type:"authorization_code",client_id:CFG.client_id,code,redirect_uri:redirectUri,code_verifier:store.get("pkce")||""})});
  if(r.ok){const j=await r.json();applyToken(j.access_token);if(j.refresh_token)store.set("rt",j.refresh_token)}else toast("Sign-in failed","bad");
  history.replaceState({},"",redirectUri)}
async function refreshToken(){
  const rt=store.get("rt");if(!rt||!(CFG.domain||CFG.auth_base))return false;
  const r=await fetch(`${AUTHB()}/oauth2/token`,{method:"POST",headers:{"Content-Type":"application/x-www-form-urlencoded"},
    body:new URLSearchParams({grant_type:"refresh_token",client_id:CFG.client_id,refresh_token:rt})});
  if(!r.ok)return false;applyToken((await r.json()).access_token);return true}
function logout(){store.clear();TOKEN="";me={sub:"",groups:[]};runs=[];sel=null;clearInterval(timer);paint()}

/* ---------- rpc (one endpoint: /invocations, same as AgentCore) ---------- */
async function rpc(action,body={},retry=true){
  if(DEMO)return demoRpc(action,body);
  const r=await fetch((CFG.api_url||"")+"/invocations",{method:"POST",headers:{
    "Authorization":"Bearer "+TOKEN,"Content-Type":"application/json",
    "X-Amzn-Bedrock-AgentCore-Runtime-Session-Id":("tws-ui-"+me.sub+"-0000000000000000").slice(0,48)},body:JSON.stringify({action,...body})});
  if(r.status===401&&retry&&await refreshToken())return rpc(action,body,false);
  if(r.status===401){logout();throw new Error("Session expired. Please sign in again.")}
  if((r.status===504||r.status===502)&&action==="trigger")throw new Error("Still analyzing. The run will appear in the list shortly.");
  if(!r.ok){const j=await r.json().catch(()=>({}));const m=String(j.detail||j.message||"");
    throw new Error(r.status===403?(m||"The server refused this action (not permitted)."):r.status===409?(m||"That run is no longer awaiting approval."):r.status===400?"The server rejected that request (check the repo is allowlisted).":r.status===503?(m||"The worker is unreachable. Try again shortly."):(m||"Request failed ("+r.status+")").slice(0,160))}
  return r.json()}

/* ---------- derived view-model ---------- */
const can={read:()=>me.groups.length>0,trigger:()=>me.groups.includes("admin"),approve:r=>(me.groups.includes("approver")||me.groups.includes("admin"))&&me.sub!==r.triggered_by};
function statusClass(s){return["awaiting_approval","executed","needs_human","rejected"].includes(s)?"s-"+s:"s-other"}
function statusLabel(s){return({analyzing:"Analyzing",approved:"Approved",awaiting_approval:"Awaiting approval",executed:"Fixed",needs_human:"Needs human",rejected:"Rejected"})[s]||s||"Working"}
function stages(r){
  const done={detect:true,redact:!!r.analysis||/guardrail|blocked/i.test(r.error||"")===false&&!!r.analysis,analyze:!!r.analysis,runbook:(r.runbook||[]).length>0,
    propose:!!r.pr||(r.proposal?.changes||[]).length>0,pr:!!r.pr,approve:["executed"].includes(r.status),execute:r.status==="executed"};
  done.redact=!!r.analysis||(r.status!=="needs_human"||!/guardrail/i.test(r.error||""));
  let idx=STAGES.findIndex(([k])=>!done[k]);if(idx<0)idx=STAGES.length;
  if(r.status==="awaiting_approval")idx=6;
  const out=STAGES.map(([k,l],i)=>({k,l,s:i<idx?"done":"pending"}));
  if(idx<STAGES.length){out[idx].s=(r.status==="needs_human"||r.status==="rejected")?"blocked":"current"}
  return{list:out,at:Math.min(idx,STAGES.length-1),finished:idx>=STAGES.length}}
function checks(r){
  const err=r.error||"",has=!!r.analysis,pr=!!r.pr,gdFail=/guardrail|safety check/i.test(err),citeFail=/cite/i.test(err),editFail=/edit|removes|exist|exactly|path|diff too|repo|branch/i.test(err);
  const st=(ok,fail)=>fail?"no":ok?"ok":"wait";
  return[
    ["Secrets redacted before the model saw the log",st(has,false)],
    ["Input guardrail passed (prompt-injection screen)",st(has,gdFail&&!has),gdFail&&!has?err:""],
    ["Runbook section retrieved from the knowledge base",(r.runbook||[]).length?"ok":(has?"no":"wait"),(!(r.runbook||[]).length&&has)?"No runbook section matched. Routed to a human.":""],
    ["Fix cites a retrieved runbook section",st(pr||(r.proposal?.changes||[]).length>0,citeFail),citeFail?err:""],
    ["Edits apply cleanly and stay minimal (server-computed diff)",st(pr,editFail&&!pr),editFail&&!pr?err:""],
    ["Output guardrail passed",st(pr,gdFail&&has),gdFail&&has?err:""],
    ["Allowlisted repo, branch ci-fix/*, path allowlist",st(pr,false)]]}
function diffHtml(d){return String(d||"").split("\n").map(l=>{if(!l&&l!=="")return"";const c=l.startsWith("+++")||l.startsWith("---")?"hk":l.startsWith("@@")?"hk":l.startsWith("+")?"add":l.startsWith("-")?"del":"ctx";return`<span class="l ${c}">${esc(l)||" "}</span>`}).join("")}
function when(ts){const s=Math.max(1,Math.round(Date.now()/1000-ts));return s<60?s+"s ago":s<3600?Math.round(s/60)+"m ago":Math.round(s/3600)+"h ago"}

/* ---------- render ---------- */
const TRAIN=`<svg viewBox="0 0 44 28" aria-hidden="true"><rect x="2" y="9" width="26" height="12" rx="3" fill="#E9EDF4"/><rect x="22" y="3" width="12" height="18" rx="3" fill="#C9D1DE"/><rect x="26" y="6" width="6" height="6" rx="1.4" fill="#0B0E13"/><rect x="34" y="12" width="8" height="9" rx="2.5" fill="#FFB638"/><circle class="lamp" cx="40.5" cy="15" r="2" fill="#FFF3CF"/><rect x="6" y="5" width="5" height="6" rx="1.5" fill="#8793A6"/><circle cx="10" cy="23" r="3.3" fill="#0B0E13" stroke="#8793A6" stroke-width="1.5"/><circle cx="22" cy="23" r="3.3" fill="#0B0E13" stroke="#8793A6" stroke-width="1.5"/><circle cx="33" cy="23" r="3.3" fill="#0B0E13" stroke="#8793A6" stroke-width="1.5"/></svg>`;
const EMPTY=`<svg viewBox="0 0 160 70" aria-hidden="true"><path d="M70 6 L30 66 M90 6 L130 66" stroke="#8793A6" stroke-width="3" stroke-linecap="round" fill="none"/><path d="M63 16H97 M54 30H106 M44 46H116 M34 62H126" stroke="#2D3748" stroke-width="4" stroke-linecap="round"/><circle cx="80" cy="6" r="4" fill="#FFB638"><animate attributeName="opacity" values="1;.3;1" dur="2s" repeatCount="indefinite"/></circle></svg>`;

function paintHeader(){
  const signed=DEMO||!!TOKEN;
  $("login").hidden=signed;$("logout").hidden=!signed||DEMO;$("persona").hidden=!DEMO;$("banner").classList.toggle("on",DEMO||BANNER);
  $("who").hidden=!signed;
  const g=me.groups.includes("admin")?"admin":me.groups.includes("approver")?"approver":me.groups.includes("viewer")?"viewer":"no role";
  $("whotxt").textContent=signed?`${DEMO?me.sub:short(me.sub)} · ${g}`:"";
  $("newcard").hidden=!(signed&&can.trigger())}
function countTo(el,n){const from=+el.textContent||0;if(from===n)return;const t0=performance.now();const step=t=>{const p=Math.min(1,(t-t0)/600);el.textContent=Math.round(from+(n-from)*(1-Math.pow(1-p,3)));if(p<1)requestAnimationFrame(step)};requestAnimationFrame(step)}
function paintStats(){countTo($("k-total"),runs.length);countTo($("k-wait"),runs.filter(r=>r.status==="awaiting_approval").length);countTo($("k-fixed"),runs.filter(r=>r.status==="executed").length);countTo($("k-human"),runs.filter(r=>["needs_human","rejected"].includes(r.status)).length)}
function paintRuns(){
  const el=$("runs");
  if(!(DEMO||TOKEN)){el.innerHTML=`<div class="empty">${EMPTY}<div>Sign in to see runs.</div></div>`;return}
  if(!runs.length){el.innerHTML=`<div class="empty">${EMPTY}<div>No runs yet.<br><span class="hint">Trigger an analysis of a failed run.</span></div></div>`;return}
  el.innerHTML=runs.map((r,i)=>`<button class="run" role="option" style="--i:${i}" data-id="${esc(r.id)}" aria-selected="${r.id===sel}">
    <div class="r1"><span class="repo">${esc(r.repo)}</span><span class="badge ${statusClass(r.status)}">${esc(statusLabel(r.status))}</span></div>
    <div class="r2"><span>${r.pr?"PR #"+esc(r.pr.number):"no PR"}</span><span>${esc(short(r.id))}</span></div></button>`).join("")}
function paintDetail(){
  const box=$("detail"),r=runs.find(x=>x.id===sel);
  if(!r){box.innerHTML=`<div class="empty" style="padding:70px 20px">${EMPTY}<div style="font:700 20px var(--display);color:var(--text)">Select a run</div><div>The harness shows every stage, check and decision for it.</div></div>`;return}
  const S=stages(r),pct=((S.finished?STAGES.length-1:S.at)+.5)/STAGES.length*100,moving=!S.finished&&S.list[S.at].s==="current"&&r.status!=="awaiting_approval";
  const gateOn=r.status==="awaiting_approval";
  const rail=`<div class="rail ${moving?"":"paused"}">
    <div class="bed"></div><div class="rl a"></div><div class="rl b"></div>
    <div class="lit" style="width:${pct}%;--g:${S.list.some(s=>s.s==="blocked")?"var(--red)":gateOn?"var(--amber)":"var(--green)"}"></div>
    ${S.list.map((s,i)=>`<div class="st ${s.s}" style="left:${(i+.5)/STAGES.length*100}%"><div class="dot"></div><div class="lbl">${s.l}</div></div>`).join("")}
    <svg class="gate ${gateOn?"on":""}" style="left:${(6.5)/STAGES.length*100}%" viewBox="0 0 20 42" aria-hidden="true"><rect x="8.5" y="14" width="3" height="28" fill="#5B6679"/><rect x="2" y="0" width="16" height="16" rx="5" fill="#0B0E13" stroke="#2D3748"/><circle class="bulb" cx="10" cy="8" r="4" fill="#FF5D5D"/></svg>
    <div class="train" style="left:${gateOn?pct-4.2:pct}%">${TRAIN}</div></div>`;
  const ck=checks(r).map(([t,s,why],i)=>`<li class="${s}" style="--i:${i}"><span class="ic">${s==="ok"?"✓":s==="no"?"✕":"·"}</span><span>${esc(t)}${why?`<small>${esc(why)}</small>`:""}</span></li>`).join("");
  const files=(r.proposal?.changes||[]).map(c=>`<figure class="diff"><div class="fh"><span>${esc(c.path)}</span><span>server-computed diff</span></div><pre>${diffHtml(c.diff)}</pre></figure>`).join("");
  let gate="";
  if(r.status==="awaiting_approval"){
    const ok=can.approve(r),why=!can.read()?"You have no role on this system.":me.sub===r.triggered_by?"You triggered this run, so you cannot approve it (separation of duties).":!(me.groups.includes("approver")||me.groups.includes("admin"))?"Viewers are read-only. An approver must clear this signal.":"";
    gate=`<div class="gatebox"><div><b>Signal is red: waiting for an approver</b><p>${ok?"Approving merges the PR and starts CI again on the fixed branch.":esc(why)}</p></div>
      <div class="acts"><button class="btn bad" data-act="reject" ${ok?"":"disabled"}>Reject</button><button class="btn ok" data-act="approve" ${ok?"":"disabled"}>Approve and execute</button></div></div>`}
  else if(r.status==="executed")gate=`<div class="gatebox done"><div><b>Signal green: fix merged and CI started on the new head</b><p>Approved by an authorized user. Recorded in the audit trail.</p></div>${r.pr?`<a class="btn" href="${esc(r.pr.url)}" target="_blank" rel="noopener">View PR #${esc(r.pr.number)}</a>`:""}</div>`;
  else if(r.status==="needs_human"||r.status==="rejected")gate=`<div class="gatebox stop"><div><b>${r.status==="rejected"?"Rejected by an approver":"Stopped safely: needs a human"}</b><p>${esc(r.error||"No runbook section matched this failure.")}</p></div></div>`;
  const log=(audit||[]).map(e=>{const k=e.event==="fix_executed"||(e.event==="decision"&&e.approved)?"good":/blocked|rejected|self_approval/.test(e.event)||(e.event==="decision"&&!e.approved)?"warn":e.event==="pr_opened"?"hot":"";return`<li class="${k}"><b>${esc(String(e.event).replace(/_/g," "))}</b> · ${esc(when(e.ts))}${e.approver?" · "+esc(short(e.approver)):""}${e.by?" · "+esc(short(e.by)):""}${e.reason?" · "+esc(e.reason).slice(0,90):""}</li>`}).join("");
  box.innerHTML=`<div class="dh"><div><h3>${esc(r.repo)}</h3><div class="meta"><span class="badge ${statusClass(r.status)}">${esc(statusLabel(r.status))}</span><span class="chip">run ${esc(short(r.id))}</span><span class="chip">by ${esc(DEMO?r.triggered_by:short(r.triggered_by))}</span>${r.pr?`<a class="chip" href="${esc(r.pr.url)}" target="_blank" rel="noopener">PR #${esc(r.pr.number)} ↗</a>`:""}</div></div></div>
    <div class="track">${rail}</div>
    <div class="cols">
      <div class="box"><h4>Root cause</h4><p>${esc(r.analysis||"Analyzing the failed log…")}</p>${r.proposal?.runbook_section?`<span class="cite">§ ${esc(r.proposal.runbook_section)}</span>`:""}</div>
      <div class="box"><h4>Safety ledger</h4><ul class="checks">${ck}</ul></div>
      ${files?`<div class="box full"><h4>Proposed change ${r.diff_hash?`· <button class="hash" data-copy="${esc(r.diff_hash)}" title="Copy diff hash">${esc(r.diff_hash.slice(0,12))}… copy</button>`:""}</h4>${files}${r.proposal?.summary?`<p style="margin-top:12px;color:var(--mute)">${esc(r.proposal.summary)}</p>`:""}</div>`:""}
      ${log?`<div class="box full"><h4>Activity</h4><ul class="log">${log}</ul></div>`:""}
    </div>${gate}`}
function paint(){paintHeader();paintStats();paintRuns();paintDetail();paintFoot()}
let staleShown=false;
function paintFoot(){const el=$("ver");if(!CFG.dev_auth||!sel){return}
  el.innerHTML=`<a href="http://localhost:8233/namespaces/default/workflows/${encodeURIComponent(sel)}" target="_blank" rel="noopener">Open this run in the Temporal UI ↗</a>`}

/* ---------- data ---------- */
async function load(keep=true){
  if(!(DEMO||TOKEN)){paint();return}
  try{
    runs=await rpc("list");
    const anyStale=runs.some(r=>r.stale);if(anyStale&&!staleShown)toast("Worker offline: runs are safe in Temporal and resume when it returns.","bad");staleShown=anyStale;
    runs.sort((a,b)=>(b.id>a.id?1:-1));
    if(!runs.find(r=>r.id===sel))sel=runs[0]?.id||null;
    if(sel)try{audit=await rpc("audit",{id:sel})}catch{audit=[]}
    const key=JSON.stringify(runs.map(r=>[r.id,r.status,r.pr?.number]))+sel+audit.length;if(key!==lastKey||!keep){lastKey=key;paint()}
  }catch(e){toast(e.message,"bad");paint()}}
$("runs").addEventListener("click",async e=>{const b=e.target.closest(".run");if(!b)return;sel=b.dataset.id;try{audit=await rpc("audit",{id:sel})}catch{audit=[]}paint()});
$("refresh").onclick=()=>load(false);
$("login").onclick=()=>login();$("logout").onclick=logout;
$("newform").addEventListener("submit",async e=>{e.preventDefault();const b=$("go");b.disabled=true;b.textContent="Analyzing…";
  try{const r=await rpc("trigger",{repo:$("repo").value.trim(),run_id:+$("rid").value});sel=r.id;toast("Workflow started. It runs durably on its own; this page just watches.","good");await load(false)}
  catch(err){toast(err.message,"bad")}finally{b.disabled=false;b.textContent="Analyze failure"}});
$("detail").addEventListener("click",e=>{
  const c=e.target.closest("[data-copy]");if(c){navigator.clipboard?.writeText(c.dataset.copy);toast("Diff hash copied");return}
  const b=e.target.closest("[data-act]");if(!b)return;const r=runs.find(x=>x.id===sel);if(!r)return;confirmDecision(r,b.dataset.act==="approve")});
function confirmDecision(r,approve){
  $("dt").textContent=approve?"Approve and execute?":"Reject this fix?";
  $("dp").textContent=approve?`This merges PR #${r.pr?.number} into ${r.repo} and starts CI again on the fixed branch. Your approval is bound to this exact diff.`:"The PR stays open and nothing is merged. The rejection is recorded.";
  $("dh").textContent=r.diff_hash;$("dok").className="btn "+(approve?"ok":"bad");$("dok").textContent=approve?"Approve and execute":"Reject";
  $("dlg").showModal();
  $("dok").onclick=async()=>{$("dok").disabled=true;try{const out=await rpc("decision",{id:r.id,approved:approve,diff_hash:r.diff_hash});
    $("dlg").close();toast(out.status==="executed"?"Fix merged. CI started on the fixed branch.":"Decision recorded: "+statusLabel(out.status),out.status==="executed"?"good":"");sel=r.id;await load(false)}
    catch(err){$("dlg").close();toast(err.message,"bad")}finally{$("dok").disabled=false}}}
$("dc").onclick=()=>$("dlg").close();

/* ---------- demo backend (sample data only) ---------- */
let demoRuns=[],demoAudit={};
function demoInit(){
  const ago=m=>Date.now()/1000-m*60;
  demoRuns=[
    {id:"7b86fef9deb8",repo:"LondheShubham153/ci-analyzer-demo",status:"awaiting_approval",triggered_by:"alice",
     analysis:"The npm run build step failed: package.json defines no build script, so npm exits with 'Missing script: build'.",
     runbook:[{section:"s3://tws-runbooks/ci-fixes.md"}],proposal:{runbook_section:"## Missing build script",summary:"Add the missing build script as the runbook requires.",
     changes:[{path:"package.json",diff:'--- a/package.json\n+++ b/package.json\n@@ -2,6 +2,7 @@\n   "name": "ci-demo",\n   "version": "1.0.0",\n   "scripts": {\n-    "test": "echo ok"\n+    "test": "echo ok",\n+    "build": "echo build"\n   }\n }\n'}]},
     pr:{number:5,url:"#",branch:"ci-fix/run-43688035350"},diff_hash:"ce48ca60d309d7f0d0c96751c14450f58d58aadd7a13b0c8e9b21fe067fe275f"},
    {id:"3c19a07be214",repo:"acme/payments-api",status:"executed",triggered_by:"alice",
     analysis:"pip install failed: urllib3 2.3.0 dropped a symbol that requests 2.31 imports. The latest upstream release broke compatibility.",
     runbook:[{section:"s3://tws-runbooks/ci-fixes.md"}],proposal:{runbook_section:"## Unpinned dependency breaks install",summary:"Pin urllib3 to the last working version.",
     changes:[{path:"requirements.txt",diff:'--- a/requirements.txt\n+++ b/requirements.txt\n@@ -3,3 +3,4 @@\n requests==2.31.0\n+urllib3==2.2.3\n pytest==8.3.2\n'}]},
     pr:{number:212,url:"#",branch:"ci-fix/run-9921"},diff_hash:"a41d0c7e5b9f"+"0".repeat(52)},
    {id:"b8e2d4f61a07",repo:"acme/web-storefront",status:"needs_human",triggered_by:"alice",
     analysis:"The build failed in the lint step because the shared eslint config was renamed upstream.",
     runbook:[{section:"s3://tws-runbooks/ci-fixes.md"}],proposal:null,pr:null,diff_hash:null,
     error:"fix removes 4 lines from package.json (max 1)"},
    {id:"5d70c3a9e182",repo:"acme/infra-live",status:"needs_human",triggered_by:"dave",
     analysis:"Terraform plan failed: provider checksum mismatch in the lock file.",runbook:[],proposal:null,pr:null,diff_hash:null,error:null}];
  const t=(m,e,x={})=>({ts:ago(m),event:e,...x});
  demoAudit={"7b86fef9deb8":[t(9,"triggered",{by:"alice"}),t(8.5,"pr_opened")],
    "3c19a07be214":[t(190,"triggered",{by:"alice"}),t(189,"pr_opened"),t(176,"decision",{approved:true,approver:"bob"}),t(175,"fix_executed")],
    "b8e2d4f61a07":[t(60,"triggered",{by:"alice"}),t(59,"fix_rejected_by_policy",{reason:"fix removes 4 lines from package.json (max 1)"})],
    "5d70c3a9e182":[t(30,"triggered",{by:"dave"})]}}
async function demoRpc(action,b){
  await new Promise(r=>setTimeout(r,action==="decision"?700:120));
  if(action==="list")return JSON.parse(JSON.stringify(demoRuns));
  if(action==="audit")return demoAudit[b.id]||[];
  if(action==="decision"){const r=demoRuns.find(x=>x.id===b.id);if(me.sub==="carol")throw new Error("The server refused this action (not permitted).");
    if(me.sub===r.triggered_by)throw new Error("The server refused this action (not permitted).");
    r.status=b.approved?"executed":"rejected";(demoAudit[r.id]=demoAudit[r.id]||[]).push({ts:Date.now()/1000,event:"decision",approved:b.approved,approver:me.sub},...(b.approved?[{ts:Date.now()/1000,event:"fix_executed"}]:[]));return r}
  if(action==="trigger"){throw new Error("Demo mode: triggering is disabled.")}}
function demoPersona(p){me={sub:p,groups:p==="bob"?["approver"]:p==="carol"?["viewer"]:["admin"]};paint()}

/* ---------- boot ---------- */
(async function boot(){
  $("runs").innerHTML='<div class="skel"></div><div class="skel"></div><div class="skel"></div>';
  if(DEMO){demoInit();const p=qs.get("as")||"bob";$("persona").value=p;demoPersona(p);
    $("persona").onchange=e=>{demoPersona(e.target.value);paint()};
    sel=qs.get("run")||demoRuns[0].id;demoRuns=demoRuns;audit=demoAudit[sel]||[];runs=await demoRpc("list");paint();
    if(qs.has("approve")){const r=runs.find(x=>x.id===sel);setTimeout(()=>confirmDecision(r,true),300)};return}
  try{CFG=await (await fetch("config.json",{cache:"no-store"})).json()}catch{CFG={}}
  if(CFG.dev_auth){BANNER=true;const m=CFG.llm?.backend==="scripted"?"scripted model (no LLM)":"model "+(CFG.llm?.model||"");
    $("banner").textContent="Local demo: dev sign-in, "+m+". Durable execution by Temporal.";}
  if(CFG.demo?.repo){$("repo").value=CFG.demo.repo;if(CFG.demo.run_id)$("rid").value=CFG.demo.run_id}
  await finishLogin();
  const t=store.get("tok");if(t&&!TOKEN)applyToken(t);
  paint();if(TOKEN){await load(false);timer=setInterval(()=>{if(!document.hidden)load(true)},3000)}
})();
