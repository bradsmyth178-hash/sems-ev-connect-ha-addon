"""Local setup wizard and control console for direct Modbus and OCPP bridge modes."""
from __future__ import annotations

import asyncio
import secrets
import time
from datetime import datetime, timezone

from aiohttp import web

from . import config as C
from . import registers as R
from .modbus_link import ModbusLink
from .sems_link import SemsLink


PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SEMS EV CONNECT</title><style>
:root{--navy:#0d2b45;--blue:#0b5d72;--orange:#ef8200;--ink:#16202a;--mut:#667582;--line:#dce3e8;--ok:#16834a;--bad:#b3261e;--bg:#f3f6f8}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 Inter,system-ui,-apple-system,Segoe UI,sans-serif}.shell{max-width:980px;margin:auto;padding:24px 16px 60px}.top{display:flex;justify-content:space-between;gap:16px;align-items:center;margin-bottom:18px}.brand{display:flex;gap:12px;align-items:center}.mark{display:grid;place-items:center;width:46px;height:46px;border-radius:14px;background:linear-gradient(145deg,var(--navy),var(--blue));color:#fff;font-size:24px}.brand h1{margin:0;font-size:21px}.brand p{margin:0;color:var(--mut);font-size:12px}.card{background:#fff;border:1px solid var(--line);border-radius:16px;padding:20px;box-shadow:0 8px 28px #1832460c;margin-top:14px}h2,h3,p{margin-top:0}.lede{color:var(--mut);max-width:70ch}.decision{font-weight:750;margin:-5px 0 0;color:var(--navy)}.modes{display:grid;grid-template-columns:1fr 1fr;gap:12px}.mode{border:2px solid var(--line);border-radius:14px;padding:15px;cursor:pointer;background:#fff}.mode:has(input:checked){border-color:var(--orange);background:#fff8ee}.mode input{margin-right:7px}.mode b{font-size:16px}.mode small{display:block;color:var(--mut);margin:5px 0 0 23px}.grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.wide{grid-column:1/-1}label.field{display:grid;gap:5px;font-size:12px;font-weight:750;color:var(--mut)}input,select{font:inherit;padding:10px 11px;border:1px solid #bac6cf;border-radius:9px;background:#fff;width:100%}button{font:inherit;font-weight:800;border:0;border-radius:9px;padding:10px 14px;cursor:pointer}.primary{background:var(--orange);color:#fff}.secondary{background:#e8eff3;color:var(--navy)}.danger{background:#feeceb;color:var(--bad)}.actions{display:flex;gap:9px;flex-wrap:wrap;margin-top:14px}.out{display:none;margin-top:10px;padding:10px;border-radius:9px;font-size:13px}.out.show{display:block}.out.ok{background:#e9f7ef;color:var(--ok)}.out.bad{background:#fff0ef;color:var(--bad)}.pill{display:inline-flex;gap:6px;align-items:center;border-radius:999px;padding:5px 9px;font-size:12px;font-weight:800;background:#e8edf0;color:#5d6972}.pill.on{background:#e6f6ed;color:var(--ok)}.pill.off{background:#fff0ef;color:var(--bad)}.stats{display:grid;grid-template-columns:repeat(4,1fr);gap:9px;margin-top:14px}.stat{border:1px solid var(--line);border-radius:12px;padding:12px;background:#fafcfd}.stat .k{font-size:10px;text-transform:uppercase;letter-spacing:.07em;color:var(--mut);font-weight:800}.stat .v{font-size:19px;font-weight:850;margin-top:4px}.controls{display:grid;grid-template-columns:1fr 1fr;gap:12px}.notice{border-left:4px solid var(--orange);background:#fff8ed;padding:11px 13px;border-radius:8px;color:#6a481b}.map,.trace{font:12px/1.7 ui-monospace,SFMono-Regular,Consolas,monospace;background:#0d2233;color:#dbe8f2;border-radius:10px;padding:13px;overflow:auto}.trace{max-height:280px;white-space:pre-wrap;margin-top:12px}summary{cursor:pointer;font-weight:850;font-size:18px}.hidden{display:none!important}.opt{font-weight:400;color:#6b7a72;font-size:.85em}.advtoggle{background:none;border:0;padding:6px 0;color:#1E5A44;font:inherit;font-size:.85rem;text-decoration:underline;cursor:pointer}@media(max-width:700px){.modes,.grid,.controls{grid-template-columns:1fr}.wide{grid-column:auto}.stats{grid-template-columns:1fr 1fr}.top{align-items:flex-start}.card{padding:16px}}
</style></head><body><main class="shell"><div class="top"><div class="brand"><div class="mark">⚡</div><div><h1>SEMS EV CONNECT</h1><p>GoodWe charger · OCPP 1.6J + home control</p></div></div><span id="ver" class="pill">loading</span></div>
<section id="wizard"><form id="f"><div class="card"><h2>1 · Connect the GoodWe charger</h2><p class="lede">SEMS EV CONNECT checks the charger connection and prepares the available controls.</p><div class="modes">
<label class="mode"><input type="radio" name="charger_connection" value="sems" checked onchange="modeChanged()"><b>GoodWe Cloud</b><small>Use the SEMS Portal account that already shows this charger.</small></label>
<label class="mode"><input type="radio" name="charger_connection" value="modbus" onchange="modeChanged()"><b>Local network</b><small>HCA G2 chargers only — for a charger already enabled for a direct home-network connection.</small></label></div>
<div id="semsFields" class="grid" style="margin-top:14px">
<label class="field">SEMS Portal email<input name="sems_username" type="email" autocomplete="username" placeholder="name@example.com"></label>
<label class="field">SEMS Portal password<input name="sems_password" type="password" autocomplete="current-password" placeholder="Enter account password"></label>
<label class="field wide">Charger serial number<input name="wallbox_serial" autocomplete="off" placeholder="Filled in for you once you sign in above"></label>
<div class="field wide" style="padding:0;border:0;background:none">
<button type="button" class="secondary" onclick="findChargers()">Find my charger</button>
<span class="hint" style="margin-left:10px">Fills the serial in for you, using the account details above.</span>
<div id="oFind" class="out"></div></div></div>
<div id="modbusFields" class="grid hidden" style="margin-top:14px">
<label class="field wide">Charger IP address<input name="charger_host" placeholder="192.168.1.80" inputmode="decimal"></label>
<label class="field">Modbus TCP port<input name="charger_port" value="502" type="number" min="1" max="65535"></label>
<label class="field">Device ID<input name="charger_unit_id" value="247" type="number" min="1" max="247"></label></div>
<div class="grid" style="margin-top:12px"><label class="field advOnly hidden">Rated charger power<select name="charger_kw"><option value="7">7 kW</option><option value="11">11 kW</option><option value="22">22 kW</option></select></label>
<label class="field ocppOnly hidden">Supply phases<select name="phases"><option value="1">Single phase</option><option value="3">Three phase</option></select></label></div>
<div class="actions"><button type="button" class="secondary" onclick="testCharger()">Test charger connection</button></div><button type="button" class="advtoggle" onclick="toggleAdvanced()" id="advBtn">Advanced settings</button><div id="o1" class="out"></div></div>
<div class="card"><h2>2 · Choose the control experience</h2><div class="modes">
<label class="mode"><input type="radio" name="operating_mode" value="modbus" checked onchange="modeChanged()"><b>SEMS EV CONNECT control</b><small>Recommended. Controls on this page and on your own setup page. Choose this unless you have been told otherwise.</small></label>
<label class="mode"><input type="radio" name="operating_mode" value="ocpp" onchange="modeChanged()"><b>OCPP 1.6J</b><small>Advanced. Only if you already run an OCPP central system to connect the charger to.</small></label></div></div>
<div id="ocppStep" class="card"><h2>3 · OCPP central system</h2><div class="grid">
<label class="field wide">Central system WebSocket URL<input name="ocpp_url" value="ws://homeassistant.local:9000" placeholder="ws://homeassistant.local:9000"></label>
<label class="field">Charge point ID<input name="charge_point_id" value="goodwe-hca"></label><label class="field">Transaction idTag<input name="ocpp_id_tag" value="SUNLANDS"></label>
<label class="field">Basic auth username (optional)<input name="ocpp_basic_auth_user" autocomplete="username"></label><label class="field">Basic auth password (optional)<input name="ocpp_basic_auth_pass" type="password" autocomplete="current-password"></label></div>
<div class="actions"><button type="button" class="secondary" onclick="testOcpp()">Test OCPP endpoint</button></div><div id="o2" class="out"></div></div>
<div class="card"><h2><span id="behaviourNo">4</span> · Finish setup</h2><div class="grid">
<label class="field advOnly hidden">Charger update interval (seconds)<input name="poll_seconds" value="30" type="number" min="5" max="120"></label><label class="field ocppOnly hidden">OCPP meter interval (seconds)<input name="meter_seconds" value="30" type="number" min="5" max="3600"></label>
<label class="field wide">Local control PIN <span class="opt">optional</span><input name="control_pin" type="password" minlength="4" placeholder="Leave blank and we will make one for you"><span>Protects start, stop, power and mode changes on your LAN. Never returned by the API. Leave blank when changing settings to keep the current PIN.</span></label>
<label class="field wide hidden" id="pinConfirmRow">Confirm the control PIN<input name="control_pin_confirm" type="password" autocomplete="off" placeholder="Type the same PIN again"><span>Only needed if you chose your own above.</span></label>
<label class="field wide">Pairing code<input name="cloud_pairing" autocomplete="off" autocapitalize="characters" spellcheck="false" placeholder="The short code we gave you, e.g. K7P2-9QMX"><span>The code from your installer. It links this charger to your own page so you can watch and control it from your phone. Outbound-only — opens no ports.</span></label>
<label class="field wide ocppOnly hidden"><span><input style="width:auto" name="remote_start_sets_fast_mode" type="checkbox"> Force Fast mode when OCPP sends Remote Start</span></label></div>
<p id="connectionTip" class="notice" style="margin-top:14px">Use the same GoodWe account that already shows this charger in the SEMS app.</p>
<div class="actions"><button type="button" class="primary" onclick="saveConfig()">Save and start →</button></div>
<p class="hint" id="pinWarn">If you leave the PIN blank we will generate one and show it on the next screen. It protects the charging controls on your home network.</p><div id="o3" class="out"></div></div></form></section>
<section id="status" class="hidden"><div class="card"><div class="top"><div><h2 id="statusTitle">Charger status</h2><p id="statusLede" class="lede"></p><p id="statusDecision" class="decision"></p></div><div><span id="pCharger" class="pill">Charger</span> <span id="pOcpp" class="pill hidden">OCPP</span> <span id="pCloud" class="pill hidden">Connect</span></div></div><div id="stats" class="stats"></div><div id="last" class="out show ok"></div><p id="semsNotice" class="notice hidden" style="margin-top:12px">New setup: run the guided test below with the car plugged in before you rely on remote control.</p><p id="ltVerified" class="notice hidden" style="margin-top:12px;border-left-color:var(--ok);background:#e9f7ef;color:#12603a"></p></div>
<div id="liveTest" class="card hidden"><h2>First live test</h2><p class="lede">One short check that a command from here really reaches your charger: we change the charge mode, you confirm it changed, then we put it back. Nothing else is altered.</p><div id="ltBody" style="margin-top:14px"></div><div id="ltOut" class="out"></div></div>
<div id="directControls" class="card"><h2>Direct charger control</h2><div class="controls"><div><h3>Charging</h3><div class="actions"><button class="primary" onclick="control('start')">Start charging</button><button class="danger" onclick="control('stop')">Stop charging</button></div></div><div><h3>Charge mode</h3><div class="actions"><button class="secondary" onclick="control('mode',0)">Fast</button><button class="secondary" onclick="control('mode',1)">Solar only</button><button class="secondary" onclick="control('mode',2)">Solar + battery</button></div></div><label class="field wide">Maximum charge power (kW)<div class="actions" style="margin-top:0"><input id="powerLimit" type="number" min="1.4" step="0.1" style="max-width:180px"><button class="secondary" onclick="control('max_power',document.querySelector('#powerLimit').value)">Apply limit</button></div></label></div><p class="notice" style="margin-top:15px">Keep PV and battery behaviour in your GoodWe apps. Use this console for daily start, stop, charge-mode and power-limit control.</p></div>
<div id="ocppStatus" class="card hidden"><h2>OCPP bridge active</h2><p class="lede">The central system controls charging while SEMS EV CONNECT keeps charger status, power and energy in sync.</p><div class="map">Remote Start → Start charging<br>Remote Stop → Stop charging<br>Smart Charging → Apply power limit<br>Status Notification ← Charger state<br>Meter Values ← Power and energy</div></div>
<details id="traceCard" class="card" ontoggle="if(this.open)loadTrace()"><summary>Connection history</summary><p class="lede" style="margin-top:10px">The latest connection and command events, with account details removed.</p><div id="traceList" class="trace">Open this card to load the history.</div></details>
<div class="actions"><button class="secondary" onclick="reconfig()">Change settings</button></div></section></main>
<script>
const $=s=>document.querySelector(s),all=s=>[...document.querySelectorAll(s)];
function pin(){return sessionStorage.getItem('sunlands-control-pin')||''}function headers(){return {'content-type':'application/json','X-Sunlands-PIN':pin()}}
function form(){const d={};new FormData($('#f')).forEach((v,k)=>d[k]=v);d.operating_mode=document.querySelector('[name=operating_mode]:checked').value;d.charger_connection=document.querySelector('[name=charger_connection]:checked').value;d.remote_start_sets_fast_mode=$('[name=remote_start_sets_fast_mode]').checked;return d}
async function request(url,body,timeoutMs){const ctl=new AbortController();const t=timeoutMs?setTimeout(()=>ctl.abort(),timeoutMs):null;let r;try{r=await fetch(url,{method:'POST',headers:headers(),body:JSON.stringify(body),signal:ctl.signal})}catch(e){if(e.name==='AbortError')throw new Error('No reply after '+Math.round(timeoutMs/1000)+' seconds. Check the details and try again.');throw e}finally{if(t)clearTimeout(t)}let data={},text='';try{text=await r.text();data=JSON.parse(text)}catch{}if(!r.ok){const err=new Error((data&&data.error)||text||('Request failed '+r.status));err.status=r.status;throw err}return data}
async function withPin(fn){for(let i=0;i<2;i++){try{return await fn()}catch(e){if(e.status!==401||!/PIN/i.test(e.message))throw e;sessionStorage.removeItem('sunlands-control-pin');if(i===1)throw e;const p=prompt('That PIN was not accepted. Enter the current local control PIN');if(!p)throw e;sessionStorage.setItem('sunlands-control-pin',p)}}}
function show(id,ok,t){const e=$(id);e.className='out show '+(ok?'ok':'bad');e.textContent=t}
async function loadTrace(){const box=$('#traceList');box.textContent='Loading…';try{const data=await withPin(async()=>{const r=await fetch('/api/trace',{headers:headers()});let d={};try{d=await r.json()}catch{}if(!r.ok){const e=new Error(d.error||('Request failed '+r.status));e.status=r.status;throw e}return d});const entries=data.entries||[];box.textContent=entries.length?entries.map(x=>`${x.timestamp}  ${x.line}`).join('\n'):'No connection events yet.'}catch(e){box.textContent=e.message}}
function modeChanged(){const ocpp=document.querySelector('[name=operating_mode]:checked').value==='ocpp';const sems=document.querySelector('[name=charger_connection]:checked').value==='sems';$('#ocppStep').classList.toggle('hidden',!ocpp);all('.ocppOnly').forEach(x=>x.classList.toggle('hidden',!ocpp));$('#semsFields').classList.toggle('hidden',!sems);$('#modbusFields').classList.toggle('hidden',sems);$('#behaviourNo').textContent=ocpp?'4':'3';$('#connectionTip').textContent=sems?'Use the same GoodWe account that already shows this charger in the SEMS app. No router setup or SolarGo step is needed.':'HCA G2 chargers only: enable Modbus TCP in SolarGo before continuing, and give the charger a reserved IP address on your router.'}
async function testCharger(){const sems=document.querySelector('[name=charger_connection]:checked').value==='sems';show('#o1',true,sems?'Checking with GoodWe Cloud — this can take up to 45 seconds…':'Connecting…');try{const r=await withPin(()=>request('/api/test-charger',form(),45000));show('#o1',true,`GoodWe charger connected · ${r.status}`);if(r.detected){document.querySelector('[name=charger_kw]').value=String(r.kw);document.querySelector('[name=phases]').value=String(r.phases)}}catch(e){show('#o1',false,e.message)}}
async function testOcpp(){show('#o2',true,'Testing…');try{const r=await withPin(()=>request('/api/test-ocpp',form(),20000));show('#o2',true,`Reached ${r.url} · BootNotification ${r.boot}`)}catch(e){show('#o2',false,e.message)}}
let autoFindDone=false;
function pinRow(){
  /* The confirm box only earns its place once someone has chosen to type
     a PIN of their own; otherwise one is generated for them. */
  const typed=!!$('[name=control_pin]').value;
  $('#pinConfirmRow').classList.toggle('hidden',!typed);
}
function maybeAutoFind(){
  /* The serial is the one thing the customer would have to go and read off
     a unit in a garage, and we can already ask the account for it. Fire as
     soon as both credentials are present, once, and never fight a serial
     they typed themselves. */
  if(autoFindDone)return;
  const u=$('[name=sems_username]').value.trim(), p=$('[name=sems_password]').value;
  if(!u||!p||$('[name=wallbox_serial]').value.trim())return;
  if(document.querySelector('[name=charger_connection]:checked').value!=='sems')return;
  autoFindDone=true;
  findChargers();
}
function toggleAdvanced(){
  /* The rated power comes back from the charger and the update interval is
     clamped server-side, so neither belongs in the customer's path - but
     hiding a setting is only fair if it can still be reached. */
  const on=all('.advOnly').some(e=>e.classList.contains('hidden'));
  all('.advOnly').forEach(e=>e.classList.toggle('hidden',!on));
  $('#advBtn').textContent=on?'Hide advanced settings':'Advanced settings';
}
async function findChargers(){
  autoFindDone=true;   // an explicit press also satisfies the automatic one
  const d=form();
  if(!d.sems_username||(!d.sems_password&&!window.__cfgd)){show('#oFind',false,'Enter the SEMS Portal email and password first.');return}
  show('#oFind',true,'Signing in to GoodWe and looking for chargers…');
  try{
    const r=await withPin(()=>request('/api/find-chargers',d,50000));
    const list=r.chargers||[];
    if(!list.length){show('#oFind',false,'Signed in to GoodWe, but no EV charger was listed on this account. Type the serial from the charger label instead.');return}
    if(list.length===1){setSerial(list[0].serial);show('#oFind',true,'Found '+(list[0].model||'your charger')+' — serial filled in.');return}
    const box=$('#oFind');
    box.className='out show ok';
    box.textContent='More than one charger on this account. Choose the one you are setting up: ';
    const sel=document.createElement('select');
    list.forEach((c,i)=>{const o=document.createElement('option');o.value=c.serial;o.textContent=c.serial+(c.model?' — '+c.model:'')+(c.name?' ('+c.name+')':'');sel.appendChild(o)});
    sel.onchange=()=>setSerial(sel.value);
    box.appendChild(sel);
    setSerial(list[0].serial);
  }catch(e){show('#oFind',false,e.message)}
}
function setSerial(v){const f=$('[name=wallbox_serial]');if(f)f.value=v}
async function saveConfig(){const d=form();
  const p1=String(d.control_pin||''),p2=String(d.control_pin_confirm||'');delete d.control_pin_confirm;
  if(p1&&p1!==p2){show('#o3',false,'The two PINs do not match. Type the same one in both boxes.');return}
if(d.charger_connection==='sems'&&(!d.sems_username||(!d.sems_password&&!window.__cfgd)||!d.wallbox_serial)){show('#o3',false,'Enter the GoodWe account and charger serial number.');return}if(d.charger_connection==='modbus'&&!d.charger_host){show('#o3',false,'Enter the charger IP address.');return}const pl=String(d.control_pin||'').length;if(pl>0&&pl<4){show('#o3',false,'A PIN you choose needs at least 4 characters \u2014 or leave it blank and we will make one for you.');return}show('#o3',true,'Saving and connecting…');try{const r=await withPin(()=>request('/api/save',d));const pinNow=d.control_pin||(r&&r.generated_pin)||'';if(pinNow)sessionStorage.setItem('sunlands-control-pin',pinNow);if(r&&r.generated_pin)showGeneratedPin(r.generated_pin);loadOut=r&&r.generated_pin?null:'#o3';loadFails=0;loadMax=15;setTimeout(load,900)}catch(e){show('#o3',false,e.message)}}
function showGeneratedPin(p){
  /* This is the only time it is ever shown: it is redacted from the status
     API and every control needs it, so a customer who misses it here is
     locked out of their own charger. Deliberately not a toast. */
  const box=$('#o3');
  box.className='out show ok';
  box.innerHTML='<b>Your control PIN is '+p+'</b><br>Write it down now \u2014 it is not shown again, and you need it to change these settings or use the controls on this page. It is already saved on this device, so charging works straight away.';
}
async function control(action,value){if(!pin()){const p=prompt('Enter the local control PIN');if(!p)return;sessionStorage.setItem('sunlands-control-pin',p)}try{await request('/api/control',{action,value});await load()}catch(e){if(/PIN/i.test(e.message))sessionStorage.removeItem('sunlands-control-pin');alert(e.message)}}
function reconfig(){if(!pin()){const p=prompt('Enter the current local control PIN');if(!p)return;sessionStorage.setItem('sunlands-control-pin',p)}$('#status').classList.add('hidden');$('#wizard').classList.remove('hidden')}
let loadBusy=false,loadFails=0,loadMax=5,loadTimer=null,loadOut='#o1';
const LT_NAMES={0:'Fast',1:'Solar only',2:'Solar + battery'};
let lt={stage:'idle',original:null,target:null};
function ltName(n){return LT_NAMES[n]!==undefined?LT_NAMES[n]:('mode '+n)}
function ltBtn(label,fn,cls){return '<button class="'+(cls||'primary')+'" onclick="'+fn+'">'+label+'</button>'}
function ltRender(){
  const b=$('#ltBody');if(!b)return;
  if(lt.stage==='idle'){
    b.innerHTML='<label class="field wide"><span><input style="width:auto" type="checkbox" id="ltCar"> The car is plugged in and the charger is powered on</span></label>'+
      '<div class="actions">'+ltBtn('Start the test','ltBegin()')+'</div>';
  }else if(lt.stage==='began'){
    b.innerHTML='<p>Your charger is on <b>'+ltName(lt.original)+'</b>. We will switch it to <b>'+ltName(lt.target)+'</b>, then put it straight back.</p>'+
      '<div class="actions">'+ltBtn('Change the mode','ltChange()')+ltBtn('Cancel','ltAbandon()','secondary')+'</div>';
  }else if(lt.stage==='changed'){
    b.innerHTML='<p>The charger is now reporting <b>'+ltName(lt.target)+'</b>. Look at the charger or open your GoodWe app — did the mode change there too?</p>'+
      '<div class="actions">'+ltBtn('Yes, it changed','ltConfirm(true)')+ltBtn('No, nothing changed','ltConfirm(false)','danger')+'</div>';
  }else if(lt.stage==='confirmed'){
    b.innerHTML='<p>Last step: put it back to <b>'+ltName(lt.original)+'</b>.</p>'+
      '<div class="actions">'+ltBtn('Restore and finish','ltRestore()')+'</div>';
  }else if(lt.stage==='restore-only'){
    b.innerHTML='<p>Put the charger back to <b>'+ltName(lt.original)+'</b> before you leave it.</p>'+
      '<div class="actions">'+ltBtn('Restore the mode','ltRestore()')+'</div>';
  }else if(lt.stage==='done'){
    b.innerHTML='<p><b>Verified.</b> A command from this page reached your charger and it did what it was told.</p>';
  }else if(lt.stage==='failed'){
    b.innerHTML='<p>The test has not passed yet. Nothing is broken — your charger keeps working on its own.</p>'+
      '<div class="actions">'+ltBtn('Run it again','ltReset()','secondary')+'</div>';
  }
}
function ltReset(){lt={stage:'idle',original:null,target:null};$('#ltOut').className='out';ltRender()}
function ltApply(r){if(r.original!==undefined&&r.original!==null)lt.original=r.original;if(r.target!==undefined&&r.target!==null)lt.target=r.target;if(r.stage)lt.stage=r.stage;ltRender()}
async function ltStep(step,body){return withPin(()=>request('/api/live-test',Object.assign({step:step},body||{}),95000))}
async function ltBegin(){
  const box=$('#ltCar');
  if(!box||!box.checked){show('#ltOut',false,'Plug the car in first — the charger ignores mode changes when nothing is connected.');return}
  show('#ltOut',true,'Reading the charger…');
  try{const r=await ltStep('begin',{car_confirmed:true});ltApply(r);show('#ltOut',true,'Ready. Currently on '+ltName(r.original)+'.')}
  catch(e){show('#ltOut',false,e.message)}
}
async function ltChange(){
  show('#ltOut',true,'Sending the change and checking it stuck — this can take up to a minute…');
  try{const r=await ltStep('change');ltApply(r);
    if(r.ok){show('#ltOut',true,'The charger accepted it and is reporting '+ltName(r.seen)+'.')}
    else{lt.stage='restore-only';ltRender();show('#ltOut',false,r.reason||'The change did not stick.')}
  }catch(e){show('#ltOut',false,e.message)}
}
async function ltConfirm(saw){
  try{const r=await ltStep('confirm',{saw_change:!!saw});
    if(r.ok){ltApply(r);show('#ltOut',true,'Good. One more step to put it back.')}
    else{lt.stage='restore-only';ltRender();show('#ltOut',false,r.reason||'Not confirmed.')}
  }catch(e){show('#ltOut',false,e.message)}
}
async function ltRestore(){
  show('#ltOut',true,'Putting the mode back…');
  try{const r=await ltStep('restore');
    lt.stage=r.stage||'failed';ltRender();
    if(r.ok){show('#ltOut',true,'Done — the test passed and the mode is back to '+ltName(lt.original)+'.');await load()}
    else{show('#ltOut',false,r.reason||'The mode is back, but the test did not pass.')}
  }catch(e){show('#ltOut',false,e.message)}
}
async function ltAbandon(){try{await ltStep('abandon')}catch(e){}ltReset()}
async function load(){if(loadBusy)return;loadBusy=true;clearTimeout(loadTimer);let r;try{const resp=await fetch('/api/status',{headers:headers()});if(!resp.ok)throw new Error('status '+resp.status);r=await resp.json()}catch(err){loadFails++;loadBusy=false;$('#ver').textContent='offline';$('#ver').className='pill off';const target=$('#status').classList.contains('hidden')?loadOut:'#last';if(loadFails<loadMax){show(target,false,"Can't reach the bridge — retrying…");loadTimer=setTimeout(load,2000)}else{show(target,false,"Can't reach the bridge. Check that SEMS EV CONNECT is still running, then reload this page.")}return}loadBusy=false;loadFails=0;$('#ver').textContent='v'+r.version;$('#ver').className='pill';const prev=loadOut?$(loadOut):null;if(prev&&/reach the bridge/.test(prev.textContent))prev.className='out';for(const [k,v] of Object.entries(r.config)){const e=$(`[name=${k}]`);if(!e)continue;if(e.type==='radio')all(`[name=${k}]`).forEach(x=>x.checked=x.value===String(v));else if(e.type==='checkbox')e.checked=!!v;else e.value=v??''}modeChanged();
['[name=sems_username]','[name=sems_password]'].forEach(sel=>{
  const el=$(sel); if(el)el.addEventListener('blur',maybeAutoFind);
});
const pinEl=$('[name=control_pin]');
if(pinEl){['input','change','blur'].forEach(ev=>pinEl.addEventListener(ev,pinRow));setTimeout(pinRow,300);}window.__cfgd=!!r.config.configured;if(!r.config.configured)return;$('#wizard').classList.add('hidden');$('#status').classList.remove('hidden');const direct=r.config.operating_mode==='modbus';const sems=r.config.charger_connection==='sems';const s=r.snapshot||{};$('#directControls').classList.toggle('hidden',!direct);$('#ocppStatus').classList.toggle('hidden',direct);$('#pOcpp').classList.toggle('hidden',direct);$('#statusTitle').textContent=direct?'Charger control':'OCPP bridge status';let lede;if(direct){lede=r.charger_connected?(sems?'Connected through GoodWe Cloud':`Connected to ${r.config.charger_host}:${r.config.charger_port}`):(sems?'Trying to reach the charger through GoodWe Cloud…':`Trying to reach the charger at ${r.config.charger_host}:${r.config.charger_port}…`)}else{lede=`${r.config.charge_point_id} → ${r.config.ocpp_url}`+(r.charger_connected?'':' · trying to reach the charger…')}if(s.error)lede+=` — ${s.error}`;$('#statusLede').textContent=lede;$('#statusDecision').textContent=r.decision||'Watching the charger — no command in progress';var ltDone=r.first_live_test&&r.first_live_test.passed;$('#semsNotice').classList.toggle('hidden',ltDone||r.config.charger_connection!=='sems');$('#liveTest').classList.toggle('hidden',!r.config.configured);if(ltDone){var d=r.first_live_test.at?new Date(r.first_live_test.at):null;$('#ltVerified').textContent='Verified on your charger'+(d&&!isNaN(d)?' on '+d.toLocaleDateString('en-AU'):'')+'.';if(lt.stage==='idle')lt.stage='done';}$('#ltVerified').classList.toggle('hidden',!ltDone);ltRender();const st=[['Status',s.status_name||'—'],['Power',(s.power_kw??0)+' kW'],['Session',(s.session_kwh??0)+' kWh'],['Lifetime',(s.lifetime_kwh??0)+' kWh'],['Mode',s.mode_name||'—'],['Car',{0:'Unplugged',1:'Half connected',2:'Connected'}[s.car]||'—'],['Max power',(s.max_power_kw??0)+' kW'],['Voltage',(s.volt_a??0)+' V']];$('#stats').innerHTML=st.map(([k,v])=>`<div class="stat"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');$('#powerLimit').value=s.max_power_kw||r.config.charger_kw;$('#pCharger').className='pill '+(r.charger_connected?'on':'off');$('#pCharger').textContent=r.charger_connected?(sems?'GoodWe connected':'Charger connected'):'Charger reconnecting';$('#pOcpp').className='pill '+(r.ocpp_connected?'on':'off');$('#pOcpp').textContent=r.ocpp_connected?'OCPP connected':'OCPP reconnecting';$('#pCloud').classList.toggle('hidden',!r.cloud_enabled);if(r.cloud_enabled){$('#pCloud').className='pill '+(r.cloud_ok?'on':'off');$('#pCloud').textContent=r.cloud_ok?'SEMS EV CONNECT linked':'SEMS EV CONNECT reconnecting'}$('#last').className='out show '+(s.error?'bad':'ok');$('#last').textContent=(r.last_action?r.last_action+' · ':'')+(s.error?s.error:(s.faults&&s.faults.length?'Charger notice: '+s.faults.join(', '):'Charger ready'))}
load();setInterval(()=>{if(!$('#status').classList.contains('hidden'))load()},5000);
</script></body></html>"""


@web.middleware
async def _json_errors(request: web.Request, handler):
    # Validation helpers raise HTTPBadRequest(text=...); the wizard's client only
    # reads JSON {error}, so convert plain-text 400s into the JSON shape it expects.
    try:
        return await handler(request)
    except web.HTTPBadRequest as exc:
        return web.json_response({"error": exc.text or "bad request"}, status=400)


def build_app(cfg: C.Config, state, restart_cb, control_cb) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024, middlewares=[_json_errors])

    def public_config() -> dict:
        hidden = {
            "sems_password", "sems_api_base", "ocpp_basic_auth_pass", "control_pin",
            "cloud_device_key", "cloud_anon_key",
        }
        return {k: v for k, v in cfg.to_dict().items() if k not in hidden}

    def apply_config(target: C.Config, data: dict) -> None:
        for key, value in data.items():
            if not hasattr(target, key):
                continue
            if key == "sems_api_base":
                # This is where the GoodWe account credential gets POSTed, so it
                # is not a free-text field: a PIN holder could otherwise point
                # the bridge at their own server and collect the password.
                base = str(value).strip().rstrip("/")
                if base and not C.sems_base_allowed(base):
                    raise web.HTTPBadRequest(
                        text="that SEMS address is not one of the published GoodWe endpoints")
                setattr(target, key, base)
                continue
            current = getattr(target, key)
            if isinstance(current, bool):
                setattr(target, key, value if isinstance(value, bool) else str(value).lower() in ("1", "true", "on", "yes"))
            elif isinstance(current, int):
                try:
                    setattr(target, key, int(value))
                except (TypeError, ValueError):
                    raise web.HTTPBadRequest(text=f"invalid value for {key}")
            else:
                setattr(target, key, str(value).strip())

    # A LAN neighbour should not get free brute-force attempts at a short PIN.
    # A per-request sleep did not achieve that: a hundred concurrent requests
    # all sleep in parallel and all still get an answer. This locks the door
    # for a spell instead, so the attempt rate is bounded no matter how many
    # requests arrive at once.
    pin_failures = {"count": 0, "until": 0.0}
    PIN_LOCK_AFTER = 5
    PIN_LOCK_SECONDS = 30.0

    async def pin_ok(req: web.Request, *, count: bool = True) -> bool:
        """count=False for polling. The status page asks every few seconds
        whether it may see identifying fields; treating each of those as a
        failed PIN attempt burned the lockout budget and then rejected the
        correct PIN."""
        supplied = req.headers.get("X-Sunlands-PIN", "")
        now = time.monotonic()
        if now < pin_failures["until"]:
            return False   # locked out; no comparison performed at all
        # A PIN header with non-ASCII characters used to raise inside
        # compare_digest and surface as an unhandled 500.
        try:
            ok = bool(cfg.control_pin) and secrets.compare_digest(supplied, cfg.control_pin)
        except TypeError:
            ok = False
        if ok:
            pin_failures["count"] = 0
            pin_failures["until"] = 0.0
        elif not count:
            return False
        else:
            pin_failures["count"] += 1
            if pin_failures["count"] >= PIN_LOCK_AFTER:
                pin_failures["until"] = now + PIN_LOCK_SECONDS
                pin_failures["count"] = 0
        return ok

    async def json_data(req: web.Request) -> dict:
        try:
            data = await req.json()
        except Exception as exc:  # noqa: BLE001
            raise web.HTTPBadRequest(text="invalid JSON") from exc
        if not isinstance(data, dict):
            raise web.HTTPBadRequest(text="JSON object required")
        return data

    async def index(_: web.Request):
        return web.Response(text=PAGE, content_type="text/html", headers={"Cache-Control": "no-store"})

    async def status(req: web.Request):
        from . import __version__
        snap = state.snapshot
        # Anyone on the home network can reach this. With a PIN set, the
        # identifying details - the GoodWe login, the charger serial, the
        # charger's own identity - are held back until the PIN is presented.
        # Live status still shows, so the page works for the household.
        trusted = (not cfg.control_pin) or await pin_ok(req, count=False)
        cfgview = public_config()
        if not trusted:
            for k in ("sems_username", "wallbox_serial", "charger_host", "cloud_url",
                      "ocpp_url", "ocpp_basic_auth_user", "charge_point_id"):
                if k in cfgview:
                    cfgview[k] = ""
        return web.json_response({
            "version": __version__,
            "config": cfgview,
            "identified": bool(trusted),
            "snapshot": None if snap is None else (
                {**snap.__dict__} if trusted
                else {k: v for k, v in snap.__dict__.items() if k != "error"}),
            "modbus_connected": state.modbus_connected,
            "charger_connected": state.charger_connected,
            "ocpp_connected": state.ocpp_connected,
            "last_action": state.last_action,
            "decision": state.decision,
            "identity": state.identity if trusted else None,
            "control_locked": bool(cfg.control_pin),
            "cloud_enabled": bool(cfg.cloud_url and cfg.cloud_device_key),
            "cloud_ok": state.cloud_ok,
            "cloud_error": state.cloud_error,
            "first_live_test": {
                "passed": bool(cfg.first_live_test_passed),
                "at": cfg.first_live_test_at or None,
            },
        })

    async def trace(req: web.Request):
        if not await pin_ok(req):
            return web.json_response({"error": "valid local control PIN required"}, status=401)
        return web.json_response({"entries": state.trace_entries()})

    async def find_chargers(req: web.Request):
        """List the EV chargers on a GoodWe account, so the serial can be
        chosen rather than transcribed off a label in a dark garage."""
        if cfg.configured and not await pin_ok(req):
            return web.json_response({"error": "current control PIN required"}, status=401)
        data = await json_data(req)
        temp = C.Config()
        apply_config(temp, data)
        if not temp.sems_password and cfg.configured:
            temp.sems_password = cfg.sems_password
        if not temp.sems_username or not temp.sems_password:
            raise web.HTTPBadRequest(text="enter the SEMS Portal email and password first")
        link = SemsLink(
            temp.sems_username, temp.sems_password, temp.wallbox_serial or "unknown",
            charger_kw=temp.charger_kw, phases=temp.phases, api_base=temp.sems_api_base,
        )
        try:
            probe = await asyncio.wait_for(link.account_probe(), 45)
            return web.json_response({
                "ok": True,
                "chargers": probe.get("chargers") or [],
                "plants": probe.get("plants") or [],
            })
        except Exception as exc:  # noqa: BLE001
            return web.json_response(
                {"error": f"could not sign in to that GoodWe account ({exc})"}, status=502)
        finally:
            await link.close()

    async def test_charger(req: web.Request):
        if cfg.configured and not await pin_ok(req):
            return web.json_response({"error": "current control PIN required"}, status=401)
        data = await json_data(req)
        temp = C.Config()
        apply_config(temp, data)
        if temp.charger_connection == "sems":
            if not temp.sems_password and cfg.configured:
                temp.sems_password = cfg.sems_password
            if not temp.sems_username or not temp.sems_password or not temp.wallbox_serial:
                raise web.HTTPBadRequest(text="GoodWe account and charger serial are required")
            link = SemsLink(
                temp.sems_username, temp.sems_password, temp.wallbox_serial,
                charger_kw=temp.charger_kw, phases=temp.phases, api_base=temp.sems_api_base,
            )
            timeout = 40
        else:
            if not temp.charger_host:
                raise web.HTTPBadRequest(text="charger IP is required")
            link = ModbusLink(temp.charger_host, temp.charger_port, temp.charger_unit_id)
            timeout = 10
        try:
            identity = await asyncio.wait_for(link.identity(), timeout)
            snap = await asyncio.wait_for(link.snapshot(), timeout)
            if not snap.ok:
                raise ConnectionError(snap.error)
            # Only the local Modbus path truly reads kw/phases off the charger;
            # the SEMS path echoes the form values back, so don't claim detection.
            return web.json_response({
                "ok": True, **identity,
                "detected": temp.charger_connection == "modbus",
                "status": snap.status_name,
            })
        except Exception as exc:  # noqa: BLE001
            detail = str(exc) or type(exc).__name__
            hint = ""
            if temp.charger_connection == "sems":
                # Separate "we could not sign in" from "we signed in, but that
                # serial is not on this account" - the fixes are different, and
                # on an install call that distinction saves the visit.
                try:
                    probe = await asyncio.wait_for(link.account_probe(), 30)
                except Exception:  # noqa: BLE001
                    probe = None
                if probe is None:
                    hint = (" We could not sign in to that GoodWe account - "
                            "check the email and password.")
                elif probe.get("signed_in"):
                    sites = probe.get("plants") or []
                    where = f" This account has {probe.get('plant_count', 0)} site(s)"
                    if sites:
                        where += ": " + ", ".join(sites)
                    hint = (f" The GoodWe sign-in worked, so the account is fine - it is the "
                            f"charger serial that was not found.{where}. Check the serial on "
                            f"the charger label matches the one in your SEMS app.")
            return web.json_response({"error": detail + hint}, status=502)
        finally:
            await link.close()

    async def test_ocpp(req: web.Request):
        if cfg.configured and not await pin_ok(req):
            return web.json_response({"error": "current control PIN required"}, status=401)
        data = await json_data(req)
        temp = C.Config()
        apply_config(temp, data)
        if not temp.ocpp_basic_auth_pass and cfg.configured:
            temp.ocpp_basic_auth_pass = cfg.ocpp_basic_auth_pass
        if not temp.ocpp_url.startswith(("ws://", "wss://")):
            raise web.HTTPBadRequest(text="OCPP URL must start with ws:// or wss://")
        url = temp.ocpp_url.rstrip("/") + "/" + temp.charge_point_id
        try:
            import base64
            import websockets
            from ocpp.v16 import ChargePoint as OcppCP, call
            headers = {}
            if temp.ocpp_basic_auth_user:
                token = base64.b64encode(f"{temp.ocpp_basic_auth_user}:{temp.ocpp_basic_auth_pass}".encode()).decode()
                headers["Authorization"] = f"Basic {token}"
            async with websockets.connect(url, subprotocols=["ocpp1.6"], additional_headers=headers, open_timeout=8) as ws:
                cp = OcppCP(temp.charge_point_id, ws)
                task = asyncio.create_task(cp.start())
                response = await asyncio.wait_for(cp.call(call.BootNotification(
                    charge_point_model="HCA-G2-test", charge_point_vendor="SEMS EV CONNECT")), 8)
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                return web.json_response({"ok": True, "url": url, "boot": str(response.status)})
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"{type(exc).__name__}: {exc}"}, status=502)

    async def save(req: web.Request):
        if cfg.configured and not await pin_ok(req):
            return web.json_response({"error": "current control PIN required"}, status=401)
        data = await json_data(req)
        draft = C.Config(**cfg.to_dict())
        apply_config(draft, data)
        if draft.operating_mode not in ("modbus", "ocpp"):
            return web.json_response({"error": "choose Modbus TCP or OCPP mode"}, status=400)
        if draft.charger_connection not in ("sems", "modbus"):
            return web.json_response({"error": "choose a GoodWe charger connection"}, status=400)
        if not draft.sems_password and cfg.configured and cfg.sems_password:
            draft.sems_password = cfg.sems_password
        if not draft.ocpp_basic_auth_pass and cfg.configured and cfg.ocpp_basic_auth_pass:
            draft.ocpp_basic_auth_pass = cfg.ocpp_basic_auth_pass
        if draft.charger_connection == "sems" and not (
            draft.sems_username and draft.sems_password and draft.wallbox_serial
        ):
            return web.json_response({"error": "GoodWe account and charger serial are required"}, status=400)
        if draft.charger_connection == "modbus" and not draft.charger_host:
            return web.json_response({"error": "charger IP is required"}, status=400)
        if draft.charger_connection == "modbus" and (
            not 1 <= draft.charger_port <= 65535 or not 1 <= draft.charger_unit_id <= 247
        ):
            return web.json_response({"error": "charger port or device ID is outside the valid range"}, status=400)
        if not draft.control_pin and cfg.configured and cfg.control_pin:
            draft.control_pin = cfg.control_pin  # blank on reconfigure = keep the current PIN
        generated_pin = ""
        if not draft.control_pin:
            # Asking someone to invent a PIN, type it twice and write it on paper
            # was three actions for a secret nobody chose to have. Make one up,
            # show it once on the status page, and let them change it later.
            generated_pin = "".join(secrets.choice("0123456789") for _ in range(6))
            draft.control_pin = generated_pin
        if len(draft.control_pin) < 4:
            return web.json_response({"error": "control PIN must contain at least 4 characters"}, status=400)
        if draft.operating_mode == "ocpp" and not draft.ocpp_url.startswith(("ws://", "wss://")):
            return web.json_response({"error": "OCPP URL must start with ws:// or wss://"}, status=400)
        pairing = str(data.get("cloud_pairing", "")).strip()
        # apply_pairing_input makes a blocking HTTPS call. Run off the loop,
        # or status, charger polling, cloud sync and the OCPP heartbeat all
        # stall for as long as the network takes - up to twenty seconds.
        claimed = True
        if pairing:
            claimed = await asyncio.get_running_loop().run_in_executor(
                None, C.apply_pairing_input, draft, pairing)
        if pairing and not claimed:
            return web.json_response(
                {"error": "that pairing code was not recognised. Check it against the message "
                          "from your installer — codes are single-use and expire, so if it has "
                          "already been used, ask for a fresh one."}, status=400)
        draft.configured = True
        for key, value in draft.to_dict().items():
            setattr(cfg, key, value)
        C.save(cfg)
        await restart_cb()
        if generated_pin:
            state.trace("A control PIN was generated for this bridge")
        # Returned once, and only when we invented it: it is redacted from
        # /api/status, and every control needs it, so if the customer never
        # sees it they are locked out of their own charger.
        out = {"ok": True, "mode": cfg.operating_mode}
        if generated_pin:
            out["generated_pin"] = generated_pin
        return web.json_response(out)

    async def control(req: web.Request):
        if not await pin_ok(req):
            return web.json_response({"error": "valid local control PIN required"}, status=401)
        data = await json_data(req)
        try:
            await asyncio.wait_for(control_cb(str(data.get("action", "")), data.get("value")), 45)
            return web.json_response({"ok": True, "last_action": state.last_action})
        except (ValueError, RuntimeError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except Exception as exc:  # noqa: BLE001
            return web.json_response({"error": f"charger command failed: {exc}"}, status=502)


    # ── Guided first live test ───────────────────────────────────────────────
    # Proves remote control actually reaches THIS charger: read the mode, change
    # it, have a human confirm at the charger, put it back. Only a full clean
    # run stamps the config — a half-finished or failed run leaves the pending
    # posture exactly as it was. Step state lives here, not in the browser, so
    # the sequence cannot be skipped or replayed by a reload.
    live_test = {"original": None, "target": None, "changed": False, "confirmed": False}

    def lt_reset() -> None:
        live_test.update(original=None, target=None, changed=False, confirmed=False)

    def lt_mode_now():
        snap = state.snapshot
        return None if snap is None else snap.mode

    def lt_payload(**extra) -> dict:
        out = {
            "original": live_test["original"],
            "target": live_test["target"],
            "mode_names": R.CHARGE_MODES,
            "passed": bool(cfg.first_live_test_passed),
            "at": cfg.first_live_test_at or None,
        }
        out.update(extra)
        return out

    live_test_lock = asyncio.Lock()

    async def live_test_ep(req: web.Request):
        # One charger, one test. Two devices on the same page share a PIN, so
        # interleaved steps are the ordinary case, not an exotic one.
        async with live_test_lock:
            return await _live_test_step(req)

    async def _live_test_step(req: web.Request):
        if not await pin_ok(req):
            return web.json_response({"error": "valid local control PIN required"}, status=401)
        data = await json_data(req)
        step = str(data.get("step", ""))

        if step == "abandon":
            original = live_test["original"]
            lt_reset()
            return web.json_response(lt_payload(ok=True, original=original, stage="idle"))

        if step == "begin":
            if not data.get("car_confirmed"):
                return web.json_response({"error": "confirm the car is plugged in first"}, status=400)
            if not state.snapshot_fresh(120) or not state.charger_connected:
                return web.json_response(
                    {"error": "the charger is not answering right now — wait for it to come back, then start again"},
                    status=409)
            current = lt_mode_now()
            if current not in R.CHARGE_MODES:
                return web.json_response(
                    {"error": "the charger has not reported a charge mode yet — wait a moment and start again"},
                    status=409)
            # A previous run that was abandoned after the change left the charger
            # on the test mode. Reading it now as "original" would bake the test
            # mode in permanently and stamp PASS over the customer's real
            # setting, so recover the remembered one instead of starting fresh.
            stranded = int(cfg.live_test_pending_mode)
            if stranded in R.CHARGE_MODES and stranded != int(current):
                live_test.update(original=stranded, target=int(current),
                                 changed=True, confirmed=False)
                return web.json_response(lt_payload(
                    ok=True, stage="changed", seen=int(current), recovered=True,
                    reason="A previous test was left part-way through — your charger is still on "
                           f"{R.CHARGE_MODES.get(int(current))}. Confirm what you can see, then put it back."))
            live_test.update(original=int(current), target=1 if int(current) == 0 else 0,
                             changed=False, confirmed=False)
            return web.json_response(lt_payload(ok=True, stage="began"))

        if step == "change":
            if live_test["original"] is None:
                return web.json_response({"error": "start the test first"}, status=409)
            target = live_test["target"]
            try:
                await asyncio.wait_for(control_cb("mode", target), 90)
            except (ValueError, RuntimeError) as exc:
                return web.json_response(lt_payload(ok=False, stage="began", reason=str(exc)))
            except Exception as exc:  # noqa: BLE001
                return web.json_response(lt_payload(ok=False, stage="began",
                                                    reason=f"the charger did not accept the change: {exc}"))
            seen = lt_mode_now()
            if seen != target:
                # The write was accepted but the charger is not reporting it —
                # the exact "accepted then reverted" behaviour this test exists
                # to catch. Say so plainly rather than claiming success.
                return web.json_response(lt_payload(
                    ok=False, stage="began", seen=seen,
                    reason="the charger reported back "
                           f"{R.CHARGE_MODES.get(seen, 'an unknown mode')} instead of "
                           f"{R.CHARGE_MODES.get(target, target)} — the change did not stick"))
            live_test["changed"] = True
            cfg.live_test_pending_mode = int(live_test["original"])
            C.save(cfg)   # survives a browser that never comes back
            return web.json_response(lt_payload(ok=True, stage="changed", seen=seen))

        if step == "confirm":
            if not live_test["changed"]:
                return web.json_response({"error": "change the mode first"}, status=409)
            if not data.get("saw_change"):
                live_test["confirmed"] = False
                return web.json_response(lt_payload(
                    ok=False, stage="changed",
                    reason="the charger did not visibly change, so the test has not passed. "
                           "Put it back below, then check the charger is online in your GoodWe app."))
            live_test["confirmed"] = True
            return web.json_response(lt_payload(ok=True, stage="confirmed"))

        if step == "restore":
            if live_test["original"] is None:
                return web.json_response({"error": "start the test first"}, status=409)
            original = live_test["original"]
            try:
                await asyncio.wait_for(control_cb("mode", original), 90)
            except Exception as exc:  # noqa: BLE001
                return web.json_response(lt_payload(
                    ok=False, stage="changed",
                    reason=f"could not put the mode back ({exc}). Set it to "
                           f"{R.CHARGE_MODES.get(original, original)} in your GoodWe app."))
            restored = lt_mode_now() == original
            earned = bool(live_test["changed"] and live_test["confirmed"] and restored)
            if earned:
                cfg.first_live_test_passed = True
                cfg.first_live_test_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            if restored:
                cfg.live_test_pending_mode = -1   # nothing left outstanding
            if earned or restored:
                C.save(cfg)
            lt_reset()
            return web.json_response(lt_payload(
                ok=earned, stage="done" if earned else "failed", restored=restored,
                reason=None if earned else (
                    "the mode is back to normal, but the test did not pass — run it again when you are at the charger"
                    if restored else
                    f"the charger is still on {R.CHARGE_MODES.get(lt_mode_now(), 'the test setting')} — "
                    f"set it back to {R.CHARGE_MODES.get(original, 'its original mode')} in your GoodWe app, "
                    "or start the test again to retry")))

        return web.json_response({"error": "unknown step"}, status=400)

    app.router.add_get("/", index)
    app.router.add_get("/api/status", status)
    app.router.add_get("/api/trace", trace)
    app.router.add_post("/api/test-charger", test_charger)
    app.router.add_post("/api/find-chargers", find_chargers)
    app.router.add_post("/api/test-ocpp", test_ocpp)
    app.router.add_post("/api/save", save)
    app.router.add_post("/api/control", control)
    app.router.add_post("/api/live-test", live_test_ep)
    return app
