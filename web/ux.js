/* Less scrolling, on every page:
   1. Cards fold to their question and one-line answer. On each tab the first two cards start open, the rest folded;
      tap the title to open or close. Your choices are remembered on this device.
   2. Long lists and tables show the first rows with a "Show all" button.
   3. On scroll, the title and league switcher slide away; the section tabs stay pinned. */
(function(){
  const store={get(k){try{return localStorage.getItem(k)}catch(e){return null}},set(k,v){try{localStorage.setItem(k,v)}catch(e){}}};
  const PAGE=location.pathname.replace(/index\.html$/,'');
  const OPEN_N=2,LIST_SHOW=5,LIST_MIN=7,ROWS_SHOW=12,ROWS_MIN=16;
  const keyOf=(card,head)=>'fold:'+PAGE+':'+(card.id||((head.querySelector('h2,h3')||head).textContent||'').replace(/\d+/g,'#').trim().slice(0,40));
  function headOf(card){const c=card.firstElementChild;if(!c)return null;
    if(/^H[23]$/.test(c.tagName)||c.tagName==='HEADER')return c;
    if(c.querySelector&&c.querySelector(':scope > h2, :scope > h3, :scope > div > h2, :scope > div > h3'))return c;return null}
  function sectionCards(sec){return [...sec.querySelectorAll('.card')].filter(c=>!c.hidden&&!c.closest('dialog')&&!c.classList.contains('nofold')&&!c.hasAttribute('aria-live')&&!c.matches('details')&&headOf(c)&&!(c.parentElement&&c.parentElement.closest('.card')))}
  function setFold(card,folded){card.classList.toggle('folded',folded);const b=card.querySelector(':scope > * .fold-btn, :scope > .fold-btn');if(b){b.setAttribute('aria-expanded',String(!folded));b.title=folded?'Show':'Hide'}}
  function prepCard(card,idx){const head=headOf(card);if(!head)return;
    card.classList.add('foldable');
    if(!head.querySelector('.fold-btn')){const b=document.createElement('button');b.type='button';b.className='fold-btn';b.setAttribute('aria-label','Show or hide this section');head.appendChild(b)}
    head.classList.add('fold-head');
    const k=keyOf(card,head);card.dataset.foldKey=k;
    const saved=store.get(k);
    const want=saved!=null?saved==='1':(card.dataset.foldDefault?card.dataset.foldDefault==='1':idx>=OPEN_N);
    if(card.dataset.foldInit!==k){card.dataset.foldInit=k;setFold(card,want)}else setFold(card,card.classList.contains('folded'))}
  function capLists(root){
    root.querySelectorAll('.list').forEach(l=>{const n=l.children.length-(l.nextElementSibling&&l.nextElementSibling.classList.contains('more-btn')?0:0);
      const items=[...l.children].filter(x=>!x.classList.contains('more-btn'));
      if(items.length>=LIST_MIN&&!l.dataset.capped){l.dataset.capped='1';l.classList.add('capped');
        const b=document.createElement('button');b.type='button';b.className='more-btn';b.dataset.n=items.length;b.textContent=`Show all ${items.length}`;l.after(b)}
      else if(items.length<LIST_MIN&&l.dataset.capped){l.classList.remove('capped');delete l.dataset.capped;const b=l.nextElementSibling;if(b&&b.classList.contains('more-btn'))b.remove()}
      else if(l.dataset.capped){const b=l.nextElementSibling;if(b&&b.classList.contains('more-btn')&&+b.dataset.n!==items.length){b.dataset.n=items.length;if(l.classList.contains('capped'))b.textContent=`Show all ${items.length}`}}});
    root.querySelectorAll('table').forEach(t=>{const tb=t.tBodies[0];if(!tb||t.closest('.lu')||t.classList.contains('nocap'))return;const n=tb.rows.length;const w=t.closest('.tbl-wrap')||t;
      if(n>=ROWS_MIN&&!t.dataset.capped){t.dataset.capped='1';t.classList.add('capped');const b=document.createElement('button');b.type='button';b.className='more-btn';b.dataset.n=n;b.textContent=`Show all ${n} rows`;w.after(b)}
      else if(t.dataset.capped){const b=w.nextElementSibling;if(b&&b.classList.contains('more-btn')&&+b.dataset.n!==n){b.dataset.n=n;if(t.classList.contains('capped'))b.textContent=`Show all ${n} rows`}}})}
  // long explanations: two lines, tap to read the rest
  function clampSubs(root){root.querySelectorAll('.card > p.sub, .card > div > p.sub, .card > .explain').forEach(p=>{
    if(p.dataset.cl)return;if((p.textContent||'').length<170){p.dataset.cl='0';return}
    p.dataset.cl='1';p.classList.add('clamp');p.setAttribute('role','button');p.tabIndex=0;p.title='Tap to read more'})}
  let busy=false;
  function run(){if(busy)return;busy=true;
    try{document.querySelectorAll('main section, main#hub, main > section, #hub').forEach(sec=>{sectionCards(sec).forEach((c,i)=>prepCard(c,i))});capLists(document);clampSubs(document)}finally{setTimeout(()=>{busy=false},0)}}
  let t=null;const mo=new MutationObserver(()=>{clearTimeout(t);t=setTimeout(run,60)});
  mo.observe(document.body,{childList:true,subtree:true});
  document.addEventListener('click',e=>{
    const cp=e.target.closest('p.clamp');if(cp&&!e.target.closest('a,button,.pl')){cp.classList.toggle('open');return}
    const mb=e.target.closest('.more-btn');
    if(mb){const tgt=mb.previousElementSibling;const el=tgt&&(tgt.matches('table,.list')?tgt:tgt.querySelector('table'));if(!el)return;
      const on=el.classList.toggle('capped');mb.textContent=on?(el.matches('table')?`Show all ${mb.dataset.n} rows`:`Show all ${mb.dataset.n}`):'Show fewer';
      if(on)mb.previousElementSibling.scrollIntoView({block:'nearest'});return}
    const head=e.target.closest('.fold-head');if(!head)return;
    if(e.target.closest('a,select,input,textarea,label,button:not(.fold-btn),.pl,.word'))return;
    const card=head.closest('.card.foldable');if(!card||card.parentElement.closest('.card'))return;
    const f=!card.classList.contains('folded');setFold(card,f);store.set(card.dataset.foldKey,f?'1':'0');
    if(!f){const r=card.getBoundingClientRect();if(r.top<0)card.scrollIntoView({block:'start'})}});
  // open a folded card when something inside it is jumped to (e.g. a "Check this trade" button shows the lab)
  window.uxOpen=sel=>{const c=document.querySelector(sel);if(c&&c.classList.contains('folded'))setFold(c,false)};
  // header: title and switcher scroll away, tabs stay pinned
  function pin(){const h=document.querySelector('header.board'),tabs=h&&h.querySelector('nav.tabs');if(!h)return;
    const off=tabs?h.offsetHeight-tabs.offsetHeight-2:0;h.style.top=`calc(env(safe-area-inset-top,0px) - ${Math.max(0,off)}px)`}
  window.addEventListener('resize',pin);document.addEventListener('DOMContentLoaded',()=>{pin();run()});setTimeout(()=>{pin();run()},300);
  if(document.readyState!=='loading'){pin();run()}
})();
