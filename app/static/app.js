const $ = (id) => document.getElementById(id);
let platform = 'kwai';
let items = [];
let selected = new Set();
let lastRunning = false;
let currentPage = 1;
const pageSize = 100;
let statusBusy = false;
let resultsSignature = null;
let resultsFetchedAt = 0;

function fmt(n){ return n==null?'—':new Intl.NumberFormat('pt-BR',{notation:'compact',maximumFractionDigits:1}).format(n); }
function fmtDuration(n){
  if(n==null || !Number.isFinite(n) || n<=0) return '—';
  const seconds=Math.floor(n), minutes=Math.floor(seconds/60), hours=Math.floor(minutes/60);
  return (hours?hours+':'+String(minutes%60).padStart(2,'0'):String(minutes))+':'+String(seconds%60).padStart(2,'0');
}
function escapeHtml(s){ return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
async function api(url, options={}){
  if(platform!=='kwai') url += (url.includes('?')?'&':'?')+'platform='+platform;
  const res=await fetch(url,{headers:{'Content-Type':'application/json'},...options});
  if(!res.ok){ let msg='Erro'; try{ const j=await res.json(); msg=Array.isArray(j.detail)?j.detail.map(x=>`${x.loc.at(-1)}: ${x.msg}`).join('\n'):(j.detail||JSON.stringify(j));}catch{} throw new Error(msg); }
  return res.json();
}

async function openBrowser(){
  try{
    const r=await api('/api/browser/start',{method:'POST',body:JSON.stringify({profile_url:$('profileUrl').value.trim()||null})});
    $('browserState').textContent='Navegador: aberto';
    if(r.profile_url) $('profileUrl').value=r.profile_url;
  }catch(e){ alert(e.message); }
}

async function analyze(){
  try{
    const payload={
      profile_url:$('profileUrl').value.trim(),
      limit:Number($('limit').value)||100,
      min_views:Number($('minViews').value)||0,
      hashtag:$('hashtag').value.trim()||null,
      sort_by:$('sortBy').value,
      max_pages:500,
      top_n:Number($('topLinks').value)||20,
      auto_links:$('autoLinks').checked,
      turbo:$('turbo').checked
    };
    await api('/api/analyze',{method:'POST',body:JSON.stringify(payload)});
    $('rankingFilters').elements.namedItem('sort').value=payload.sort_by;
    selected.clear(); currentPage=1; renderResults();
  }catch(e){ alert(e.message); }
}
async function cancel(){ try{ await api('/api/cancel',{method:'POST',body:'{}'}); }catch(e){ alert(e.message); } }
async function updateDurations(){ try{ await api('/api/durations',{method:'POST',body:'{}'}); await refreshStatus(); await refreshResults(); }catch(e){ alert(e.message); } }
async function resolveLinks(){ try{ await api('/api/links',{method:'POST',body:JSON.stringify({top_n:Number($('topLinks').value)||20})}); }catch(e){ alert(e.message); } }

function visibleItems(){
  const filters=Object.fromEntries(new FormData($('rankingFilters')));
  filters.search=$('search').value;
  return filterRanking(items,filters);
}
function safeUrl(value){try{const u=new URL(value);return ['http:','https:'].includes(u.protocol)?u.href:'';}catch{return '';}}

function renderResults(){
  const body=$('resultsBody'); const visible=visibleItems();
  const pages=Math.max(1,Math.ceil(visible.length/pageSize)); currentPage=Math.min(currentPage,pages);
  const offset=(currentPage-1)*pageSize;
  $('pageMeta').textContent=`Página ${currentPage} de ${pages}`;
  $('previousPage').disabled=currentPage<=1; $('nextPage').disabled=currentPage>=pages;
  body.innerHTML=visible.slice(offset,offset+pageSize).map((x,i)=>`<tr>
    <td><input class="rowcheck" type="checkbox" data-key="${escapeHtml(x.key)}" ${selected.has(x.key)?'checked':''}></td>
    <td>${offset+i+1}</td>
    <td>${x.thumbnail?`<img class="thumb" src="${escapeHtml(safeUrl(x.thumbnail))}" loading="lazy" alt="Capa do vídeo">`:'—'}</td>
    <td><strong>${fmt(x.views)}</strong></td>
    <td>${fmt(x.likes)}</td>
    <td>${fmt(x.comments)}</td>
    <td title="${x.duration_seconds==null?'Duração não informada':escapeHtml(x.duration_seconds)+' segundos'}">${fmtDuration(x.duration_seconds)}</td>
    <td class="title-cell" title="${escapeHtml(x.title)}">${escapeHtml(x.title||'—')}</td>
    <td class="id-cell">${escapeHtml(x.photo_id||'—')}</td>
    <td>${x.permalink?`<a class="link" href="${escapeHtml(safeUrl(x.permalink))}" target="_blank" rel="noopener noreferrer">Abrir</a>`:`<span class="media-missing" title="${escapeHtml(x.link_error||'Link ainda não resolvido. Use Coletar / repetir Top.')}">pendente${x.link_attempts?' ↻':''}</span>`}</td>
    <td><span class="source-pill">${escapeHtml(x.source||'—')}${x.link_source?' / '+escapeHtml(x.link_source):''}</span></td>
    <td>${x.source==='youtube'?'<span class="media-ok">sob demanda</span>':x.media_url?'<span class="media-ok">direta</span>':'<span class="media-missing">resolver</span>'}</td>
  </tr>`).join('') || '<tr><td colspan="12" class="empty-state">'+(items.length?'Nenhum vídeo atende aos filtros. Ajuste ou limpe os filtros.':'Nenhum vídeo coletado. Inicie uma coleta para criar o ranking.')+'</td></tr>';
  body.querySelectorAll('.rowcheck').forEach(el=>el.addEventListener('change',()=>{el.checked?selected.add(el.dataset.key):selected.delete(el.dataset.key);updateSelection();}));
  $('resultMeta').textContent=`${items.length} vídeos no ranking • ${visible.length} nos filtros`;
  updateSelection();
}
function updateSelection(){ const visible=new Set(visibleItems().map(x=>x.key)); const hidden=[...selected].filter(k=>!visible.has(k)).length; $('selectionCount').textContent=`${selected.size} selecionados${hidden?` • ${hidden} fora dos filtros`:''}`; }
async function refreshResults(){ const requestedPlatform=platform; const r=await api('/api/results'); if(requestedPlatform!==platform)return; resultsFetchedAt=Date.now(); const signature=JSON.stringify(r.items||[]); if(signature===resultsSignature)return; resultsSignature=signature; items=r.items||[]; const keys=new Set(items.map(x=>x.key)); selected=new Set([...selected].filter(k=>keys.has(k))); renderResults(); }

async function refreshStatus(){
  if(statusBusy) return; statusBusy=true;
  try{
    const requestedPlatform=platform; const s=await api('/api/status'); if(requestedPlatform!==platform)return;
    $('browserState').textContent=platform==='youtube'?'YouTube • somente Shorts':`Navegador: ${s.browser_open?'aberto':'fechado'}`;
    $('message').textContent=s.message||'Pronto.';
    $('engine').textContent=s.engine||'—'; $('pages').textContent=s.pages||0; $('scanned').textContent=s.scanned||0; $('matched').textContent=s.matched||0;
    $('linksCount').textContent=s.links||0; $('feedSource').textContent=s.feed_source||'—'; $('cursor').textContent=s.cursor||'—';
    const lim=s.limit||Number($('limit').value)||100; const pct=s.reached_end?100:Math.min(100,Math.round(((s.matched||0)/lim)*100)); $('progressBar').style.width=pct+'%';
    $('openBrowser').disabled=!!s.running; $('analyze').disabled=!!s.running; $('cancel').disabled=!s.running; $('links').disabled=!!s.running||!(s.results_count>0);
    $('updateDurations').disabled=!!s.running||!(s.results_count>0);
    if(s.last_profile_url && !$('profileUrl').value) $('profileUrl').value=s.last_profile_url;
    if(!s.running && (lastRunning || items.length !== s.results_count)) await refreshResults();
    if(s.running && Date.now()-resultsFetchedAt>=2000 && ['scan','links','durations'].includes(s.phase)) await refreshResults();
    lastRunning=!!s.running;
  }catch(e){ $('message').textContent=e.message; }finally{statusBusy=false;}
}
async function refreshDownload(){
  try{
    const requestedPlatform=platform; const s=await api('/api/download/status'); if(requestedPlatform!==platform)return; $('cancelDownload').disabled=!s.running; $('downloadPill').textContent=s.running?'baixando':'parado'; $('downloadMessage').textContent=s.message||''; $('downloadSelected').disabled=!!s.running;
    $('downloadList').innerHTML=(s.items||[]).slice(-20).reverse().map(x=>`<div class="download-item"><span>${escapeHtml(x.photo_id||x.key)}</span><span class="${x.status==='done'?'ok':x.status==='failed'?'fail':''}">${escapeHtml(x.status)}${x.file?' • '+escapeHtml(x.file):''}${x.error?' • '+escapeHtml(x.error):''}</span></div>`).join('');
  }catch{}
}
async function copyLinks(){
  const chosen=(selected.size?items.filter(x=>selected.has(x.key)):visibleItems()).filter(x=>safeUrl(x.permalink)).map(x=>x.permalink);
  if(!chosen.length) return alert('Nenhum link disponível para copiar.');
  try{await navigator.clipboard.writeText(chosen.join('\n')); alert(`${chosen.length} links copiados.`);}catch(e){alert('Não foi possível copiar: '+e.message);}
}
async function downloadSelected(){
  if(!selected.size) return alert('Selecione pelo menos um vídeo.');
  try{ await api('/api/download',{method:'POST',body:JSON.stringify({keys:[...selected]})}); }catch(e){ alert(e.message); }
}

$('openBrowser').addEventListener('click',openBrowser); $('analyze').addEventListener('click',analyze); $('cancel').addEventListener('click',cancel); $('links').addEventListener('click',resolveLinks);
$('updateDurations').addEventListener('click',updateDurations);
$('rankingFilters').addEventListener('input',()=>{currentPage=1;renderResults();}); $('resetFilters').addEventListener('click',()=>{$('rankingFilters').reset();currentPage=1;renderResults();}); $('selectAll').addEventListener('click',()=>{visibleItems().forEach(x=>selected.add(x.key));renderResults();}); $('clearSelection').addEventListener('click',()=>{selected.clear();renderResults();}); $('copyLinks').addEventListener('click',copyLinks); $('downloadSelected').addEventListener('click',downloadSelected);

refreshResults().catch(()=>{}); refreshStatus(); refreshDownload(); setInterval(refreshStatus,800); setInterval(refreshDownload,1200);

$('previousPage').addEventListener('click',()=>{currentPage=Math.max(1,currentPage-1);renderResults();});
$('nextPage').addEventListener('click',()=>{currentPage++;renderResults();});
function exportRanking(format){
  const rows=visibleItems();
  const columns=['rank','key','photo_id','views','likes','comments','title','hashtags','permalink','thumbnail','media_url','source','duration_seconds'];
  const cell=v=>{let t=String(v??'');if(/^\s*[=+@-]/.test(t))t="'"+t;return '"'+t.replace(/"/g,'""')+'"';};
  const content=format==='json'?JSON.stringify({profile_url:$('profileUrl').value,filters:Object.fromEntries(new FormData($('rankingFilters'))),search:$('search').value,items:rows},null,2):'\uFEFF'+[columns.join(','),...rows.map((x,i)=>columns.map(k=>cell(k==='rank'?i+1:k==='hashtags'?(x.hashtags||[]).join(' '):x[k])).join(','))].join('\r\n');
  const url=URL.createObjectURL(new Blob([content],{type:format==='json'?'application/json':'text/csv;charset=utf-8'}));
  const a=document.createElement('a');a.href=url;a.download=platform+'-ranking-filtrado.'+format;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
}
$('exportCsv').addEventListener('click',e=>{e.preventDefault();exportRanking('csv');});
$('exportJson').addEventListener('click',e=>{e.preventDefault();exportRanking('json');});

$('platform').addEventListener('change', async ()=>{
  platform=$('platform').value;
  const youtube=platform==='youtube', tiktok=platform==='tiktok', instagram=platform==='instagram';
  const name=youtube?'YouTube':tiktok?'TikTok':instagram?'Instagram':'Kwai';
  $('platformHint').hidden=!youtube;
  $('tiktokHint').hidden=!tiktok;
  $('instagramHint').hidden=!instagram;
  for(const id of ['kwaiOptions','links','updateDurations','durationHint']) $(id).hidden=platform!=='kwai';
  $('openBrowser').hidden=youtube; $('openBrowser').textContent='Abrir navegador do '+name;
  $('cancelDownload').hidden=platform==='kwai';
  $('profileLabel').textContent=youtube?'URL do canal — somente Shorts':'URL do perfil';
  $('profileUrl').placeholder=youtube?'https://www.youtube.com/@canal/shorts':tiktok?'https://www.tiktok.com/@usuario':instagram?'https://www.instagram.com/usuario':'https://www.kwai.com/@usuario';
  $('profileUrl').value='';
  $('diagnostic').href='/api/diagnostic?platform='+platform;
  items=[]; selected.clear(); currentPage=1; resultsSignature=null; lastRunning=false;
  $('rankingFilters').reset();
  $('message').textContent='Carregando '+name+'…';
  renderResults();
  await Promise.all([refreshResults(),refreshStatus(),refreshDownload()]);
});
$('cancelDownload').addEventListener('click',async()=>{
  try{await api('/api/download/cancel',{method:'POST',body:'{}'});await refreshDownload();}catch(e){alert(e.message);}
});
