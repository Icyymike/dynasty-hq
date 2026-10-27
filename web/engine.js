const $=s=>document.querySelector(s);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const f1=x=>(x>=0?'+':'')+x.toFixed(1);
const fk=x=>Math.round(x||0).toLocaleString();
const pct=x=>x>=0.995?'>99%':x<0.005&&x>0?'<1%':Math.round(x*100)+'%';
const POS=['QB','RB','WR','TE'];
const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};

// lineup shape comes from the league (starters per position, flex, superflex); same rules as the pipeline
const LUC=()=>(D&&D.meta&&D.meta.lu)||{n:{QB:1,RB:2,WR:3,TE:1},flex:3,sf:1,managed:false};
function best(vals){const L=LUC(),by={QB:[],RB:[],WR:[],TE:[]};for(const [p,v] of vals) if(by[p]) by[p].push(v);for(const k in by) by[k].sort((a,b)=>b-a);
  let t=0;for(const p in by)for(let i=0;i<L.n[p]&&i<by[p].length;i++)t+=by[p][i];
  const rest=by.RB.slice(L.n.RB).concat(by.WR.slice(L.n.WR),by.TE.slice(L.n.TE)).sort((a,b)=>b-a);for(let i=0;i<L.flex&&i<rest.length;i++)t+=rest[i];
  if(L.sf){const sf=by.QB.slice(L.n.QB).concat(rest.slice(L.flex)).sort((a,b)=>b-a);for(let i=0;i<L.sf&&i<sf.length;i++)t+=sf[i]}return t}
// managed lineups: the starters a manager sets before the games (best by projection), same pick as the pipeline
function bestIds(items){const L=LUC(),by={QB:[],RB:[],WR:[],TE:[]};for(const it of items)if(by[it[0]])by[it[0]].push(it);for(const k in by)by[k].sort((a,b)=>b[1]-a[1]);
  const used=[];for(const p of ['QB','RB','WR','TE'])used.push(...by[p].slice(0,L.n[p]));
  const rest=by.RB.slice(L.n.RB).concat(by.WR.slice(L.n.WR),by.TE.slice(L.n.TE)).sort((a,b)=>b[1]-a[1]);used.push(...rest.slice(0,L.flex));
  if(L.sf)used.push(...by.QB.slice(L.n.QB).concat(rest.slice(L.flex)).sort((a,b)=>b[1]-a[1]).slice(0,L.sf));return used.map(x=>x[2])}
// fixed playoff bracket like Sleeper (no reseeding); byes for the top seeds when the field isn't 4/8/16 teams
function bracketRun(seeds,win){let B=1;while(B<seeds.length)B*=2;let order=[1,2];while(order.length<B){const n2=2*order.length+1;order=order.flatMap(s=>[s,n2-s])}
  let slots=order.map(i=>i<=seeds.length?seeds[i-1]:null),rnd=0;while(slots.length>1){const nx=[];for(let i=0;i<slots.length;i+=2){const a=slots[i],b=slots[i+1];nx.push(a==null?b:b==null?a:win(a,b,rnd))}slots=nx;rnd++}return slots[0]}
const bracketShape=n=>{let B=1;while(B<n)B*=2;return {bye:B-n,rounds:Math.max(1,Math.round(Math.log2(B)))}};
const value=s=>PL[s]?(PL[s].val||PL[s].ktc||PL[s].fc||0):0;
function proj(sid,k,src){const p=PL[sid];if(!p||p.slot)return 0;let v=(p[src]||p.pr)[k];if(v==null)v=p.ou[k];return v||0}
function roster(r){return Object.keys(PL).filter(s=>PL[s].r===r&&!PL[s].gone)}
function teamName(r){const t=TEAMS.find(t=>t.id===r);return t?t.name:'Free agent'}
function hash(a,b,c){let h=2166136261^a;h=Math.imul(h^b,16777619);h=Math.imul(h^c,16777619);h^=h>>>13;h=Math.imul(h,0x5bd1e995);h^=h>>>15;return (h>>>0)/4294967296}
function z(sid,s,w){const u1=Math.max(1e-9,hash(+sid||7,s*31+1,w*7+3)),u2=hash(+sid||7,s*17+5,w*13+11);return Math.sqrt(-2*Math.log(u1))*Math.cos(6.283185307*u2)}
// ---- simulation model: identical to pipeline/build.py (same hash, same draws), so every page shows the same odds
const RHO=0.15,P_MISS=0.05,WI=Math.sqrt(1-RHO),WT=Math.sqrt(RHO),P_MISS_STATUS={Questionable:0.25};
function teamNum(t){let n=0;for(const ch of (t||'FA'))n=n*37+ch.charCodeAt(0);return 900000+n}
const TZ=new Map();function teamZ(t,s,w){const k=t+'|'+s+'|'+w;let v=TZ.get(k);if(v===undefined){v=z(teamNum(t),s,w);TZ.set(k,v)}return v}
function cvOf(p){const base=Math.max(p.ou.find(x=>x>0)||1,1);return Math.min(1.2,Math.max(0.25,p.sd/base))}
// measured score shape (pipeline: SHAPE in build.py): quantiles of score/projection by position and projection level
const CV0={QB:0.42,RB:0.55,WR:0.62,TE:0.65};
const SHP=()=>(D&&D.meta&&D.meta.shape)||{};
const lvl=m=>m<4?0:m<8?1:m<13?2:3;
function phi(x){const y=Math.abs(x)/1.4142135623730951,t=1/(1+0.3275911*y),e=1-(((((1.061405429*t-1.453152027)*t)+1.421413741)*t-0.284496736)*t+0.254829592)*t*Math.exp(-y*y);return 0.5*(1+(x>=0?e:-e))}
function shapeRatio(c,f,zz){const q=c[0],mu=c[1],K=q.length;const x=Math.min(Math.max(phi(zz)*K-0.5,0),K-1),i=Math.floor(x);const r=i>=K-1?q[i]:q[i]+(q[i+1]-q[i])*(x-i);return Math.max(0,mu+(r-mu)*f)}
function simPts(m,cv,sid,team,s,w,pm,pos){if(m<=0)return 0;const a=+sid||7;if(hash(a,s*37+9,w*41+17)<pm)return 0;const zz=WI*z(a,s,w)+WT*teamZ(team,s,w);const c=pos&&SHP()[pos+'|'+lvl(m)];if(c)return m*shapeRatio(c,cv/(CV0[pos]||0.6),zz);const sig=Math.sqrt(Math.log(1+cv*cv));return m*Math.exp(sig*zz-sig*sig/2)}
const ABS_START=0.055,ABS_DUR=[[1,.31],[2,.20],[3,.10],[4,.06],[5,.04],[6,.07],[7,.06],[8,.04],[10,.04],[17,.08]];
// multi-week absences (same as the pipeline): a new absence starts in ~5.5% of healthy weeks and lasts as long as last season's did
function absences(a,s){const out=new Uint8Array(NW);let until=0;for(let k=1;k<NW;k++){if(k<=until){out[k]=1;continue}const w=FUT[k];
  if(hash(a,s*37+9,w*41+17)<ABS_START){const u=hash(a,s*53+3,w*59+7);let acc=0,d=ABS_DUR[ABS_DUR.length-1][0];for(const [dur,pr] of ABS_DUR){acc+=pr;if(u<acc){d=dur;break}}until=k+d-1;out[k]=1}}return out}
const VC={};
function draws(sid,src){const S=D.meta.sims;VC[src]=VC[src]||new Map();let a=VC[src].get(sid);if(a)return a;const p=PL[sid];a=new Float64Array(S*NW);if(p){const cv=cvOf(p);
  for(let s=0;s<S;s++){const ab=absences(+sid||7,s);for(let k=0;k<NW;k++){if(k===0&&p.lk){a[s*NW]=p.lk[0]+(p.lk[1]>0?simPts(p.pr[0]*p.lk[1],cv,sid,p.t,s,FUT[0],0,p.p):0);continue}if(ab[k]){a[s*NW+k]=0;continue}const pm=k===0?(P_MISS_STATUS[p.inj]??P_MISS):0;a[s*NW+k]=simPts(proj(sid,k,src),cv,sid,p.t,s,FUT[k],pm,p.p)}}}
  VC[src].set(sid,a);return a}
const STC=new Map();
function lineupAt(ids,s,k,src){if(LUC().managed){const key=src+'|'+k+'|'+ids.join(',');let st=STC.get(key);if(!st){st=bestIds(ids.filter(x=>PL[x]&&!PL[x].slot).map(x=>[PL[x].p,proj(x,k,src),x]));STC.set(key,st)}
  let t=0;for(const x of st)t+=draws(x,src)[s*NW+k];return t}
  const v=[];for(const sid of ids){const p=PL[sid];if(!p||p.slot)continue;const x=draws(sid,src)[s*NW+k];if(x>0)v.push([p.p,x])}return best(v)}
function simWeeks(ids,src,sims){const out=new Array(NW).fill(0);sims=Math.min(sims,D.meta.sims);for(let s=0;s<sims;s++)for(let k=0;k<NW;k++)out[k]+=lineupAt(ids,s,k,src);return out.map(x=>x/sims)}
function detWeeks(ids,src){const out=[];for(let k=0;k<NW;k++)out.push(best(ids.map(s=>[PL[s].p,proj(s,k,src)])));return out}
function posTag(p){return `<span class="pos ${p}" style="display:inline-block;width:26px">${p}</span>`}
function injTag(p){return p.inj?` <span class="tag t-exp" title="Injury status">${esc(p.inj)}</span>`:''}
function val(k){if(k.startsWith('pk:')){const [,y,rd,o]=k.split(':');const p=PICKS.find(q=>q.y==y&&q.rd==rd&&q.o==o);return p?p.v:0}return value(k)}
function pastImpact(giveIds,getIds){let dPts=0,dW=0;const adj={};
  for(let w=1;w<=D.meta.done;w++){const ids=D.m26[w]||[];if(!ids.length||!D.weeks[w])continue;const a=k=>{const p=PL[k];return p&&p.a[w-1]!=null?p.a[w-1]:0};
    const act=best(ids.filter(k=>PL[k]).map(k=>[PL[k].p,a(k)]));const nids=ids.filter(k=>!giveIds.includes(k)).concat(getIds.filter(k=>!ids.includes(k)));
    const cf=best(nids.filter(k=>PL[k]).map(k=>[PL[k].p,a(k)]));const wk=D.weeks[w];const mine=wk.find(x=>x[0]===ME);if(!mine)continue;const opp=wk.find(x=>x[1]===mine[1]&&x[0]!==ME);
    const others=wk.filter(x=>x[0]!==ME).map(x=>x[2]);const res=s=>{const all=others.concat([s]).sort((x,y)=>x-y);const n=all.length,med=n%2?all[(n-1)/2]:(all[n/2-1]+all[n/2])/2;return (opp&&s>opp[2]?1:0)+(D.meta.median&&s>med?1:0)};
    const nw=res(mine[2]+cf-act)-res(mine[2]);dPts+=cf-act;dW+=nw;
    if(opp){const h0=mine[2]>opp[2],h1=mine[2]+cf-act>opp[2];if(h0!==h1)adj[opp[0]]=(adj[opp[0]]||0)+(h0?1:-1)}}
  return {dPts,dW,adj}}
// full rest-of-season simulation for every team (same model as the pipeline) -> playoff, bye, title odds
function seasonOdds(rosterOf,adjW,adjPF,src,sims){
  sims=Math.min(sims||D.meta.sims,D.meta.sims);
  const kReg=FUT.map((w,k)=>w<=D.meta.reg_end?k:-1).filter(k=>k>=0),kPo=FUT.map((w,k)=>w>D.meta.reg_end?k:-1).filter(k=>k>=0);
  const nP=D.meta.n_playoff||6,out={},T=TEAMS.map(t=>t.id),tv=new Uint8Array(sims);T.forEach(r=>out[r]={po:0,bye:0,title:0});
  for(let s=0;s<sims;s++){
    const cache={};const sc=(r,k)=>{const key=r+':'+k;if(key in cache)return cache[key];return cache[key]=lineupAt(rosterOf[r],s,k,src)};
    const W={},PF={};TEAMS.forEach(t=>{W[t.id]=t.w+(adjW[t.id]||0);PF[t.id]=t.pf+(adjPF[t.id]||0)});
    for(const k of kReg){const wk=FUT[k],pairs=D.sched[wk]||[];const vals={};T.forEach(r=>vals[r]=sc(r,k));const all=T.map(r=>vals[r]).sort((a,b)=>a-b),n=all.length,med=n%2?all[(n-1)/2]:(all[n/2-1]+all[n/2])/2;
      for(const [r,m] of pairs){if(!(r in vals))continue;const o=pairs.find(p=>p[1]===m&&p[0]!==r);const v=vals[r];if(o&&v>vals[o[0]])W[r]++;if(D.meta.median&&v>med)W[r]++;PF[r]+=v}}
    const seeds=T.slice().sort((a,b)=>W[b]-W[a]||PF[b]-PF[a]).slice(0,nP);
    const BS=bracketShape(nP);seeds.forEach((r,i)=>{out[r].po++;if(i<BS.bye)out[r].bye++});
    // playoff round i: the real score if that week has been played, otherwise simulated
    const POA=D.po_actual||{},psc=(r,i)=>{const wk=D.meta.reg_end+1+i;if(POA[wk])return POA[wk][r]||0;const k=FUT.indexOf(wk);return k>=0?sc(r,k):0};
    if(seeds.length>=2&&kPo.length+Object.keys(POA).length>=BS.rounds){const ch=bracketRun(seeds,(x,y,i)=>psc(x,i)>=psc(y,i)?x:y);out[ch].title++;if(ch===ME)tv[s]=1}}
  for(const r in out)for(const k in out[r])out[r][k]/=sims;out.tv=tv;return out}
// how sure: standard error of the difference between two runs on the same draws
function diffSE(a,b){const n=a.length;let m=0;for(let i=0;i<n;i++)m+=b[i]-a[i];m/=n;let v=0;for(let i=0;i<n;i++){const d=b[i]-a[i]-m;v+=d*d}return Math.sqrt(v/(n-1)/n)}
// counter-offer search: smallest add from their side (or removal from yours) that makes the deal work for you
function counters(giveIds,getIds,gv,rv,dm0){
  const CS=150,sum=a=>a.reduce((x,y)=>x+y,0),mine=roster(ME),theirs=roster(st.partner),b0=sum(simWeeks(mine,st.src,CS)),t0=sum(simWeeks(theirs,st.src,CS));
  const evalSet=(give,get)=>{const gi=give.filter(k=>!k.startsWith('pk:')),ge=get.filter(k=>!k.startsWith('pk:'));
    const dm=(sum(simWeeks(mine.filter(s=>!gi.includes(s)).concat(ge),st.src,CS))-b0)/NW,dt=(sum(simWeeks(theirs.filter(s=>!ge.includes(s)).concat(gi),st.src,CS))-t0)/NW;
    const g=give.reduce((a,k)=>a+val(k),0),r=get.reduce((a,k)=>a+val(k),0);return {dm,dt,g,r}};
  const give=[...st.give],get=[...st.get],out=[];
  const ok=e=>e.r>=e.g*0.95&&e.dm>=Math.min(dm0,1.5)-0.3&&e.dt>=-3;
  const theirKeys=theirs.filter(s=>!st.get.has(s)).concat(PICKS.filter(p=>p.own===st.partner).map(p=>`pk:${p.y}:${p.rd}:${p.o}`).filter(k=>!st.get.has(k)));
  for(const k of theirKeys){const v=val(k);if(!v||v>Math.max(gv-rv,0)*2.2+600)continue;const e=evalSet(give,get.concat([k]));if(ok(e))out.push({kind:'add',k,e,cost:v})}
  if(give.length>1)for(const k of give){const e=evalSet(give.filter(x=>x!==k),get);if(ok(e))out.push({kind:'drop',k,e,cost:val(k)})}
  out.sort((a,b)=>a.cost-b.cost);return out.slice(0,3)}
const keyName=k=>{if(k.startsWith('pk:')){const [,y,rd,o]=k.split(':');const p=PICKS.find(q=>q.y==y&&q.rd==rd&&q.o==o);return p?p.label:k}return PL[k]?PL[k].n:k};
const norm=t=>String(t).toLowerCase().normalize('NFD').replace(/[\u0300-\u036f]/g,'').replace(/[^a-z0-9 ]+/g,' ').replace(/\b(jr|sr|ii|iii|iv)\b/g,'').replace(/\s+/g,' ').trim();
function parseOffer(text){
  const ids=Object.keys(PL).filter(s=>!PL[s].gone&&PL[s].r);const full={},last={};
  for(const s of ids){const n=norm(PL[s].n);full[n]=s;const ln=n.split(' ').slice(1).join(' ');if(ln){(last[ln]=last[ln]||[]).push(s)}}
  const halves=String(text).split(/\bfor\b|<->|<=>|⇄|↔|\bin exchange\b|\bi get\b|\byou get\b|\bi receive\b|\bgetting\b/i);
  const found=[];
  halves.forEach((h,hi)=>{let t=' '+norm(h)+' ';
    for(const n of Object.keys(full).sort((a,b)=>b.length-a.length)){if(t.includes(' '+n+' ')){found.push({k:full[n],h:hi});t=t.replace(' '+n+' ',' ')}}
    for(const ln of Object.keys(last).sort((a,b)=>b.length-a.length)){if(last[ln].length===1&&ln.length>3&&t.includes(' '+ln+' ')){found.push({k:last[ln][0],h:hi});t=t.replace(' '+ln+' ',' ')}}
    const re=/(20\d\d)\s*(?:early |mid |late )?(?:round\s*)?(1st|2nd|3rd|4th|first|second|third|fourth|[1-4])\b/g;let m;
    while((m=re.exec(t))){const rd={first:1,second:2,third:3,fourth:4}[m[2]]||parseInt(m[2]);found.push({pk:[+m[1],rd],h:hi})}});
  let partner=null;const cnt={};found.filter(f=>f.k&&PL[f.k].r!==ME).forEach(f=>cnt[PL[f.k].r]=(cnt[PL[f.k].r]||0)+1);
  partner=+Object.keys(cnt).sort((a,b)=>cnt[b]-cnt[a])[0]||st.partner;
  const sideOfHalf={};found.filter(f=>f.k).forEach(f=>{sideOfHalf[f.h]=PL[f.k].r===ME?'give':'get'});
  const give=new Set(),get=new Set(),miss=[];
  found.filter(f=>f.k).forEach(f=>(PL[f.k].r===ME?give:get).add(f.k));
  for(const f of found.filter(f=>f.pk)){
    let side=sideOfHalf[f.h];if(!side){const other=Object.values(sideOfHalf)[0];side=other?(other==='give'?'get':'give'):'get'}
    const owner=side==='give'?ME:partner;const opts=PICKS.filter(p=>p.own===owner&&p.y===f.pk[0]&&p.rd===f.pk[1]);
    const lower=norm(text);const pick=opts.find(p=>lower.includes(norm(teamName(p.o)).split(' ')[0]))||opts.find(p=>p.o===owner)||opts[0];
    if(pick)(side==='give'?give:get).add(`pk:${pick.y}:${pick.rd}:${pick.o}`);else miss.push(`${f.pk[0]} round ${f.pk[1]} (${side==='give'?'you':'they'} don't own one)`)}
  return {partner,give,get,miss}}