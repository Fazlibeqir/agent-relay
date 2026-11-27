/* The Plan tab: the AIs plan a project together, and review this app's own design. Uses $, get, post, esc, sleep and showTab from index.html. */
const PL={opts:null,kind:"plan",run:null,doc:null,docText:"",timer:null,busy:false,form:{source:"project",goal:"",remote:"",notes:"",drafter:"",reviewers:null,rounds:2,uxGoal:"Find what is confusing, hard to use or plain wrong in this app's screens. The reader is a busy owner who is not a programmer, on a computer and on a phone."}};
const PL_STATE={running:"working",done:"finished",failed:"failed",stopped:"stopped"};

async function drawPlanTab(){
  const box=$("#tab-plan");if(!box)return;
  if(!PL.opts){box.innerHTML='<p class="mute" role="status">Loading…</p>';try{PL.opts=await get("/api/council/options")}catch(e){box.innerHTML='<p class="bad" role="alert">Could not read the AIs on this computer. Try again.</p>';return}}
  const o=PL.opts,f=PL.form;
  if(!f.drafter||!o.ais.some(a=>a.id===f.drafter))f.drafter=o.ais.some(a=>a.id===o.dispatcher)?o.dispatcher:(o.ais[0]||{}).id||"";
  if(!f.reviewers)f.reviewers=o.ais.map(a=>a.id).filter(x=>x!==f.drafter);
  f.reviewers=f.reviewers.filter(x=>x!==f.drafter&&o.ais.some(a=>a.id===x));
  const ux=PL.kind==="ux",noAI=o.ais.length<2;
  const aiName=id=>({claude:"Claude",codex:"Codex",copilot:"GitHub Copilot",opencode:"OpenCode"}[id]||id);
  const calls=planCalls(f.reviewers.length,f.rounds);
  box.innerHTML=`
  <p class="mute" style="margin:0 0 12px">One AI writes the plan, the others read it and say what is missing, what could be better and what cannot be done yet. It repeats until they agree or the round limit is reached. <b>Nothing in your project is changed.</b></p>
  <div class="card">
    <div role="group" aria-label="What to do" class="row2" style="margin-bottom:10px">
      <button class="${ux?"":"pri"}" data-pk="plan" aria-pressed="${!ux}">Plan a project</button>
      <button class="${ux?"pri":""}" data-pk="ux" aria-pressed="${ux}">Review this app's design</button>
    </div>
    ${ux?`<label class="l" for="pl-goal" style="margin-top:0">What should they look for?</label>
      <textarea id="pl-goal" rows="3">${esc(f.uxGoal)}</textarea>
      <p class="mute small" style="margin:6px 0 0">The page measures itself first (the text and controls of each tab, small buttons, low contrast, overflow at phone width) and gives the AIs those facts and its source files. They cannot see the screen, so they will say where a finding needs your eyes.</p>`
    :`<fieldset style="border:0;padding:0;margin:0"><legend class="l" style="padding:0;margin-top:0">Where are the plans?</legend>
      <label class="opt"><input type="radio" name="pl-src" value="project" ${f.source==="project"?"checked":""}> <span><b>This project</b>${o.project?" ("+esc(o.project)+")":""}: its Markdown files (README, docs, plans, tasks)</span></label>
      <label class="opt"><input type="radio" name="pl-src" value="remote" ${f.source==="remote"?"checked":""}> <span><b>A repository online</b>: copied read-only, never changed</span></label>
      <label class="opt"><input type="radio" name="pl-src" value="notes" ${f.source==="notes"?"checked":""}> <span><b>Only my notes</b>: paste them below</span></label></fieldset>
    <div id="pl-remote-row" ${f.source==="remote"?"":"hidden"}><label class="l" for="pl-remote">Repository (owner/name, or an https:// address)</label><input id="pl-remote" type="text" value="${esc(f.remote)}" placeholder="owner/repository" style="width:100%"></div>
    <label class="l" for="pl-goal">What do you want planned? <span class="mute small" style="font-weight:400">(a game, a web app, the next milestone, anything)</span></label>
    <textarea id="pl-goal" rows="3" placeholder="For example: plan the next three months of this game, and tell me what is missing.">${esc(f.goal)}</textarea>
    <details ${f.notes?"open":""} style="margin-top:8px"><summary>Add notes or plans from Notion (optional)</summary>
      <p class="mute small" style="margin:6px 0">Paste text from your Notion pages or export them as Markdown. The AIs cannot open Notion from here.</p>
      <label class="l" for="pl-notes" style="margin-top:0">Your notes</label><textarea id="pl-notes" rows="5">${esc(f.notes)}</textarea></details>`}
    <div class="cols" style="margin:14px 0 0"><div>
      <label class="l" for="pl-drafter" style="margin-top:0">${ux?"Writes the findings":"Writes the plan"}</label>
      <select id="pl-drafter">${o.ais.map(a=>`<option value="${esc(a.id)}" ${a.id===f.drafter?"selected":""}>${esc(aiName(a.id))} (${esc(a.model)})</option>`).join("")}</select>
      <label class="l" for="pl-rounds">Rounds, at most</label>
      <select id="pl-rounds">${[1,2,3,4].map(n=>`<option ${n===f.rounds?"selected":""}>${n}</option>`).join("")}</select>
    </div><fieldset style="border:0;padding:0;margin:0"><legend class="l" style="padding:0;margin-top:0">${ux?"Use the app and review it":"Reviewing it"}</legend>
      ${o.ais.filter(a=>a.id!==f.drafter).map(a=>`<label class="opt"><input type="checkbox" data-rv="${esc(a.id)}" ${f.reviewers.includes(a.id)?"checked":""}> <span>${esc(aiName(a.id))} <span class="mute small">${esc(a.model)}</span></span></label>`).join("")||'<p class="mute small">No other AI is installed and signed in.</p>'}
    </fieldset></div>
    <p class="mute small" role="note" style="margin:10px 0 0">About ${calls[0]} to ${calls[1]} AI calls, a few minutes each. They count against each AI's usage limit, Claude's included.</p>
    <div class="row2" style="margin-top:10px"><button class="go" id="pl-start" ${noAI||!f.reviewers.length||PL.busy?"disabled":""}>${ux?"Start the design review":"Start planning"}</button><span id="pl-msg" class="small" role="status"></span></div>
  </div>
  <div id="pl-run"></div><div id="pl-past"></div>`;
  box.querySelectorAll("[data-pk]").forEach(b=>b.onclick=()=>{readPlanForm();PL.kind=b.dataset.pk;drawPlanTab()});
  box.querySelectorAll("[name=pl-src]").forEach(r=>r.onchange=()=>{readPlanForm();drawPlanTab()});
  box.querySelectorAll("[data-rv]").forEach(c=>c.onchange=()=>{readPlanForm();drawPlanTab()});
  const dr=$("#pl-drafter"),rd=$("#pl-rounds");dr.onchange=()=>{readPlanForm();f.reviewers=null;drawPlanTab()};rd.onchange=()=>{readPlanForm();drawPlanTab()};
  $("#pl-start").onclick=startPlan;
  drawRun();loadPast();
}
function planCalls(n,r){return[1+n,1+r*n+(r-1)]}
function readPlanForm(){
  const f=PL.form;if(!$("#pl-goal"))return;
  if(PL.kind==="ux")f.uxGoal=$("#pl-goal").value;else{f.goal=$("#pl-goal").value;const s=document.querySelector("[name=pl-src]:checked");if(s)f.source=s.value;if($("#pl-remote"))f.remote=$("#pl-remote").value;if($("#pl-notes"))f.notes=$("#pl-notes").value}
  f.drafter=$("#pl-drafter").value;f.rounds=+$("#pl-rounds").value||2;
  const cs=[...document.querySelectorAll("[data-rv]")];if(cs.length)f.reviewers=cs.filter(c=>c.checked).map(c=>c.dataset.rv);
}
async function startPlan(){
  readPlanForm();const f=PL.form,ux=PL.kind==="ux",msg=$("#pl-msg");
  if(!ux&&f.source==="remote"&&!f.remote.trim()){msg.className="small bad";msg.textContent="Type the repository first.";$("#pl-remote").focus();return}
  if(!ux&&f.source==="notes"&&!f.notes.trim()){msg.className="small bad";msg.textContent="Paste your notes first.";return}
  PL.busy=true;$("#pl-start").disabled=true;msg.className="small mute";msg.textContent=ux?"Measuring the page…":"Preparing the project files…";
  let body={kind:PL.kind,drafter:f.drafter,reviewers:f.reviewers,rounds:f.rounds};
  try{
    if(ux){body.goal=f.uxGoal;body.source="app";body.snapshot=await collectUiSnapshot()}
    else{body.goal=f.goal;body.source=f.source;body.remote=f.remote;body.notes=f.notes}
    const r=await post("/api/council/start",body);
    if(!r.ok){msg.className="small bad";msg.textContent=r.error||"Could not start.";PL.busy=false;$("#pl-start").disabled=false;return}
    PL.run=null;PL.doc=null;PL.docText="";msg.textContent="";PL.busy=false;PL.runId=r.run;await refreshRun();pollRun();
  }catch(e){msg.className="small bad";msg.textContent="Could not start: "+e.message;PL.busy=false;$("#pl-start").disabled=false}
}
function pollRun(){clearInterval(PL.timer);PL.timer=setInterval(async()=>{if(TAB!=="plan"){return}await refreshRun();if(!PL.run||PL.run.state!=="running")clearInterval(PL.timer)},4000)}
async function refreshRun(){
  if(!PL.runId)return;
  try{const r=await get("/api/council/status?run="+encodeURIComponent(PL.runId));if(r.ok){PL.run=r;if(!PL.doc&&r.final)PL.doc=r.final}}catch(e){}
  if(PL.doc&&PL.run&&!PL.docText||PL.docFor!==PL.doc+PL.runId)await loadDoc(PL.doc);
  drawRun();
}
async function loadDoc(name){
  PL.doc=name;PL.docFor=name+PL.runId;PL.docText="";
  if(!name||!PL.runId)return;
  try{const r=await get("/api/council/doc?run="+encodeURIComponent(PL.runId)+"&name="+encodeURIComponent(name));PL.docText=r.ok?r.text:(r.error||"")}catch(e){PL.docText="Could not read this document."}
}
function docLabel(n){
  const m=/rounds\/(\d+)\/(.+)\.md$/.exec(n);if(!m)return n;const r=m[1],w=m[2];
  return w==="draft"?"Plan, round "+r:w==="decisions"?"What was accepted or rejected, round "+r:"Review by "+w.replace("review-","")+", round "+r;
}
function drawRun(){
  const box=$("#pl-run");if(!box)return;const r=PL.run;
  if(!r){box.innerHTML="";return}
  const running=r.state==="running",ux=r.kind==="ux";
  const cls=r.state==="failed"?"stop":running?"wait":r.state==="stopped"?"stop":"run";
  const rounds=(r.rounds||[]).map(rd=>`<div style="margin:6px 0"><b>Round ${rd.n}</b>${rd.revise?' <span class="mute small">(then revised)</span>':""}<div class="row2" style="margin-top:4px">${rd.reviews.map(v=>{
    const t=v.state==="done"?(v.verdict==="APPROVE"?'<span class="ok">approves</span>':'<span class="warnc">wants '+(v.points||"some")+' change'+(v.points===1?"":"s")+"</span>"):v.state==="skipped"?'<span class="mute" title="'+esc(v.note)+'">skipped</span>':'<span class="mute">'+(v.state==="reading"?"reading…":"waiting")+"</span>";
    return`<span class="card" style="margin:0;padding:6px 10px;font-size:13px"><b>${esc(v.provider)}</b> ${t}${v.seconds?' <span class="mute small">'+Math.round(v.seconds/60*10)/10+" min</span>":""}</span>`}).join("")}</div>${rd.reviews.filter(v=>v.state==="skipped"&&v.note).map(v=>`<div class="mute small">${esc(v.provider)}: ${esc(v.note)}</div>`).join("")}</div>`).join("");
  const docs=(r.docs||[]).slice().sort((a,b)=>a.localeCompare(b,undefined,{numeric:true}));
  box.innerHTML=`<div class="card">
    <div class="row"><h2 style="margin:0">${ux?"Design review":"Plan"}: ${esc(PL_STATE[r.state]||r.state)}</h2><span class="pill ${cls}">${esc((r.state||"").toUpperCase())}</span></div>
    <p id="pl-step" class="big" style="font-size:16px;margin:8px 0" role="status" aria-live="polite">${running?esc(r.step||"Working…"):esc(r.outcome||r.error||"")}</p>
    ${running?`<p class="mute small" style="margin:0 0 6px">${esc(r.drafter)} writes, ${esc((r.reviewers||[]).join(", "))} review. You can leave this page; it keeps going while the app is open.</p>`:""}
    ${r.error?`<p class="bad" role="alert">${esc(r.error)}</p>`:""}
    ${r.draft&&r.draft.state==="writing"?'<p class="mute small">The first draft is being written…</p>':""}
    ${rounds}
    <div class="row2" style="margin:10px 0">
      ${running?'<button id="pl-stop">Stop</button>':`<button id="pl-more" ${r.final?"":"disabled"}>One more round</button>${ux?"":'<button id="pl-save" '+(r.final?"":"disabled")+'>Save into the project as PLAN-REVIEW.md</button>'}<button id="pl-copy" ${PL.docText?"":"disabled"}>Copy this document</button>`}
      <span id="pl-act" class="small" role="status"></span></div>
    ${docs.length?`<div class="row2" role="group" aria-label="Documents" style="margin-bottom:6px">${r.final?`<button data-doc="${esc(r.final)}" class="${PL.doc===r.final?"pri":""}">${ux?"Final findings":"Final plan"}</button>`:""}${docs.filter(d=>d!==r.final).map(d=>`<button data-doc="${esc(d)}" class="${PL.doc===d?"pri":""}">${esc(docLabel(d))}</button>`).join("")}</div>
      <pre class="doc" tabindex="0" aria-label="Document">${esc(PL.docText||"Pick a document above.")}</pre>`:""}
    <p class="mute small" style="margin:8px 0 0">${r.pack?esc(r.pack.files+" files ("+r.pack.kilobytes+" KB) were given to the AIs"+(r.pack.skipped?"; "+r.pack.skipped+" left out for size":"")+"."):""}</p>
  </div>`;
  box.querySelectorAll("[data-doc]").forEach(b=>b.onclick=async()=>{await loadDoc(b.dataset.doc);drawRun()});
  const st=$("#pl-stop");if(st)st.onclick=async()=>{await post("/api/council/stop",{run:r.id});await refreshRun()};
  const mo=$("#pl-more");if(mo)mo.onclick=async()=>{const a=await post("/api/council/extend",{run:r.id});if(!a.ok){$("#pl-act").className="small bad";$("#pl-act").textContent=a.error;return}PL.doc=null;await refreshRun();pollRun()};
  const sv=$("#pl-save");if(sv)sv.onclick=async()=>{const a=await post("/api/council/save",{run:r.id});$("#pl-act").className="small "+(a.ok?"ok":"bad");$("#pl-act").textContent=a.ok?"Saved as "+a.path+" in your project. It is not committed.":a.error};
  const cp=$("#pl-copy");if(cp)cp.onclick=async()=>{try{await navigator.clipboard.writeText(PL.docText);$("#pl-act").className="small ok";$("#pl-act").textContent="Copied."}catch(e){$("#pl-act").className="small bad";$("#pl-act").textContent="Could not copy: select the text and copy it."}};
}
async function loadPast(){
  const box=$("#pl-past");if(!box)return;let r;try{r=await get("/api/council/runs")}catch(e){return}
  if(!r.ok||!r.runs.length)return;
  box.innerHTML=`<div class="card"><h2>Earlier runs</h2><ul>${r.runs.map(x=>`<li><button data-run="${esc(x.id)}" style="border:0;padding:2px 0;text-align:left;background:none;text-decoration:underline">${esc(new Date(x.started*1000).toLocaleString())}</button> <span class="mute small">${esc(x.state)}${x.goal?": "+esc(x.goal):""}</span></li>`).join("")}</ul></div>`;
  box.querySelectorAll("[data-run]").forEach(b=>b.onclick=async()=>{PL.runId=b.dataset.run;PL.doc=null;PL.docText="";await refreshRun();if(PL.run&&PL.run.state==="running")pollRun();$("#pl-run").scrollIntoView({block:"start"})});
}

/* ------------------------------------------------------------ what the page measures about itself, for the design review */
function lum(c){const m=/rgba?\((\d+)[ ,]+(\d+)[ ,]+(\d+)(?:[ ,/]+([\d.]+))?/.exec(c);if(!m)return null;const a=m[4]===undefined?1:+m[4];const f=v=>{v/=255;return v<=0.03928?v/12.92:Math.pow((v+0.055)/1.055,2.4)};return{l:0.2126*f(+m[1])+0.7152*f(+m[2])+0.0722*f(+m[3]),a}}
function bgOf(el,doc){let e=el;while(e&&e.nodeType===1){const c=getComputedStyle(e).backgroundColor,l=lum(c);if(l&&l.a>0.95)return l.l;e=e.parentElement}const b=lum(getComputedStyle(doc.body).backgroundColor);return b&&b.a>0.5?b.l:1}
function nameOf(el){return(el.getAttribute("aria-label")||(el.labels&&el.labels[0]&&el.labels[0].textContent)||el.textContent||el.title||el.getAttribute("placeholder")||el.value||"").trim().replace(/\s+/g," ").slice(0,60)}
function visible(el){const r=el.getBoundingClientRect(),s=getComputedStyle(el);return r.width>0&&r.height>0&&s.visibility!=="hidden"&&s.display!=="none"}
function describeRoot(root,win,label){
  const doc=root.ownerDocument,out=["## "+label,""];
  const hs=[...root.querySelectorAll("h1,h2,h3")].filter(visible).map(h=>h.textContent.trim()).filter(Boolean);
  if(hs.length)out.push("Headings: "+hs.slice(0,25).join(" | "));
  const ctr=[...root.querySelectorAll("button,a[href],input,select,textarea,summary,[role=button]")].filter(visible);
  out.push("Visible controls: "+ctr.length+". Buttons and links: "+ctr.filter(c=>/^(BUTTON|A|SUMMARY)$/.test(c.tagName)||c.getAttribute("role")==="button").map(nameOf).filter(Boolean).slice(0,40).join(" | "));
  const unnamed=ctr.filter(c=>!nameOf(c));out.push("Controls with no accessible name: "+unnamed.length+unnamed.slice(0,6).map(c=>" ["+c.tagName.toLowerCase()+(c.id?"#"+c.id:"")+"]").join(""));
  const noLabel=ctr.filter(c=>/^(INPUT|SELECT|TEXTAREA)$/.test(c.tagName)&&c.type!=="hidden"&&!(c.labels&&c.labels.length)&&!c.getAttribute("aria-label"));out.push("Inputs with no label (placeholder or title only): "+noLabel.length+noLabel.slice(0,6).map(c=>" ["+c.tagName.toLowerCase()+(c.id?"#"+c.id:"")+(c.placeholder?" placeholder="+JSON.stringify(c.placeholder):"")+"]").join(""));
  const small=ctr.filter(c=>{const r=c.getBoundingClientRect();return r.width<24||r.height<24});out.push("Controls smaller than 24x24 px: "+small.length+small.slice(0,8).map(c=>" ["+nameOf(c)+" "+Math.round(c.getBoundingClientRect().width)+"x"+Math.round(c.getBoundingClientRect().height)+"]").join(""));
  const bad=new Map();
  root.querySelectorAll("*").forEach(el=>{if(!visible(el))return;const own=[...el.childNodes].some(n=>n.nodeType===3&&n.textContent.trim());if(!own)return;const s=getComputedStyle(el),fg=lum(s.color);if(!fg)return;const bg=bgOf(el,doc);const L1=Math.max(fg.l,bg),L2=Math.min(fg.l,bg),ratio=(L1+0.05)/(L2+0.05);const px=parseFloat(s.fontSize),bold=parseInt(s.fontWeight)>=700,large=px>=24||(px>=18.66&&bold);if(ratio<(large?3:4.5)){const k=el.textContent.trim().slice(0,40);if(!bad.has(k))bad.set(k,ratio.toFixed(2))}});
  out.push("Text below WCAG AA contrast (4.5, or 3 for large text): "+bad.size+[...bad.entries()].slice(0,10).map(([k,v])=>" ["+JSON.stringify(k)+" "+v+":1]").join(""));
  const text=(root.innerText||"").replace(/\n{3,}/g,"\n\n").trim();out.push("","Text on screen:","```",text.slice(0,3500),"```","");return out;
}
async function responsiveChecks(){
  const out=["## Layout at other widths (the app loaded in a frame)",""];
  for(const w of [375,768]){
    const fr=document.createElement("iframe");fr.setAttribute("aria-hidden","true");fr.tabIndex=-1;fr.style.cssText="position:fixed;left:-20000px;top:0;border:0;width:"+w+"px;height:900px";fr.src="/";document.body.appendChild(fr);
    await new Promise(r=>{fr.onload=r;setTimeout(r,8000)});await sleep(3000);
    try{const iw=fr.contentWindow,d=fr.contentDocument;
      for(const t of ["overview","tasks","setup"]){try{iw.showTab(t)}catch(e){}await sleep(700);
        const over=[...d.querySelectorAll("body *")].filter(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.right>w+1&&getComputedStyle(e).position!=="fixed"}).slice(0,6).map(e=>e.tagName.toLowerCase()+(e.id?"#"+e.id:"")+(e.className&&typeof e.className==="string"?"."+e.className.split(" ")[0]:"")+" ("+Math.round(e.getBoundingClientRect().right)+"px)");
        out.push("- "+w+"px wide, "+t+" tab: page scroll width "+d.documentElement.scrollWidth+"px"+(d.documentElement.scrollWidth>w+1?" (SIDEWAYS SCROLL)":" (fits)")+(over.length?"; wider than the screen: "+over.join(", "):""))}
    }catch(e){out.push("- "+w+"px: could not be measured ("+e.message+")")}
    fr.remove();
  }
  return out;
}
async function collectUiSnapshot(){
  const keep=TAB,out=["# What the page measured about itself","","Screen "+innerWidth+"x"+innerHeight+" px, "+(matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light")+" theme. Measured in a real browser; nobody looked at the pictures.",""];
  out.push("## Page-wide",""+"Page title: "+document.title,"Language attribute on <html>: "+(document.documentElement.lang||"(none)"),"Landmarks: main="+document.querySelectorAll("main,[role=main]").length+", nav="+document.querySelectorAll("nav,[role=navigation]").length+", header="+document.querySelectorAll("header").length,"");
  const names={overview:"Overview tab",plan:"Plan tab",tasks:"Tasks tab",setup:"AIs & settings tab"};
  for(const t of ["overview","plan","tasks","setup"]){showTab(t);await sleep(900);out.push(...describeRoot($("#tab-"+t),window,names[t]))}
  showTab(keep);
  out.push(...await responsiveChecks());
  return out.join("\n").slice(0,190000);
}
