function formatVersionTime(value){
 const date=new Date(value);
 return Number.isNaN(date.getTime())?String(value||'—'):date.toLocaleString('zh-CN',{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false});
}

function renderVersionEntries(items,selected){
 return items.map(version=>`<button type="button" class="history-card ${version.id===selected?'selected':''}" data-version-id="${version.id}"><time>${esc(formatVersionTime(version.time))}</time><strong>${esc(version.name)}</strong><span>${version.latest?'最新版本 · ':''}${version.original?'导入原件':'日副本'} · ${(version.size/1024).toFixed(1)} KB</span></button>`).join('');
}

function drawHistoryList(){
 $('#history-title').textContent=state.plasmids.find(p=>p.id===state.historyPlasmid)?.name||'文件副本';
 $('#history-count').textContent=`${state.historyItems.length} 个`;
 $('#history-list').innerHTML=renderVersionEntries(state.historyItems,state.historyVersion);
 $('#history-list').querySelectorAll('[data-version-id]').forEach(button=>button.onclick=()=>{
  state.historyVersion=Number(button.dataset.versionId);state.activeFeature=null;state.featureQuery='';state.primerQuery='';
  drawHistoryList();renderDetail();
 });
}

async function openHistory(id,versionId=null){
 closeContextMenu();
 try{
  const result=await api(`/api/plasmids/${id}/versions`);
  if(state.view!=='history')state.historyReturn={view:state.view,groupFilter:state.groupFilter,selected:state.selected};
  state.historyPlasmid=id;state.historyItems=result.versions;
  state.historyVersion=result.versions.some(v=>v.id===versionId)?versionId:(result.versions.find(v=>v.latest)||result.versions[0])?.id??null;
  state.view='history';state.activeFeature=null;state.featureQuery='';state.primerQuery='';
  $('#detail-panel').replaceChildren();drawHistoryList();render();
 }catch(error){toast(error.message)}
}

async function loadVersionPreview(id,versionId){
 const key=`${id}:${versionId}`;
 try{state.versionPreviews[key]=await api(`/api/plasmids/${id}/versions/${versionId}/preview`)}
 catch(error){state.versionPreviews[key]={error:error.message}}
 if(state.view==='history'&&state.historyPlasmid===id&&state.historyVersion===versionId)renderDetail();
}

async function refreshHistory(){
 const id=state.historyPlasmid,result=await api(`/api/plasmids/${id}/versions`);
 if(state.view!=='history'||state.historyPlasmid!==id)return;
 state.historyItems=result.versions;
 for(const key of Object.keys(state.versionPreviews))if(key.startsWith(`${id}:`))delete state.versionPreviews[key];
 if(!result.versions.some(v=>v.id===state.historyVersion))state.historyVersion=(result.versions.find(v=>v.latest)||result.versions[0])?.id??null;
 drawHistoryList();renderDetail();
}

function attachVersionSubmenu(id){
 const menu=$('#context-menu'),button=menu.querySelector('[data-action="history"]');
 if(!button)return;
 const branch=document.createElement('div');branch.className='context-branch';button.replaceWith(branch);branch.append(button);
 button.setAttribute('aria-haspopup','menu');button.setAttribute('aria-expanded','false');
 button.insertAdjacentHTML('beforeend','<span class="context-chevron" aria-hidden="true">›</span>');
 let submenu=null,request=0;
 async function show(){
  if(submenu)return;
  const ticket=++request;submenu=document.createElement('div');submenu.className='context-submenu';submenu.setAttribute('role','menu');
  submenu.innerHTML='<div class="history-menu-caption">正在读取副本…</div>';branch.append(submenu);button.setAttribute('aria-expanded','true');place();
  try{
   const result=await api(`/api/plasmids/${id}/versions`);
   if(ticket!==request||!button.isConnected||!submenu)return;
   submenu.innerHTML='<div class="history-menu-caption">最近五条 · 时间，文件名</div>'+result.versions.slice(0,5).map(v=>`<button type="button" class="history-menu-item" role="menuitem" data-version-id="${v.id}"><time>${esc(formatVersionTime(v.time))}</time><span>${esc(v.name)}</span></button>`).join('')+'<div class="history-menu-caption">单击“查看该文件所有副本”进入完整列表</div>';
   submenu.querySelectorAll('[data-version-id]').forEach(item=>item.onclick=e=>{e.stopPropagation();openHistory(id,Number(item.dataset.versionId))});place();
  }catch(error){if(submenu)submenu.textContent=error.message}
 }
 function place(){
  if(!submenu)return;
  const rect=button.getBoundingClientRect();submenu.style.left='0px';submenu.style.top='0px';
  const size=submenu.getBoundingClientRect(),gap=8;
  const right=rect.right+size.width+gap<=window.innerWidth;
  submenu.style.left=`${Math.max(gap,right?rect.right-2:rect.left-size.width+2)}px`;
  submenu.style.top=`${Math.max(gap,Math.min(rect.top,window.innerHeight-size.height-gap))}px`;
 }
 branch.onpointerenter=show;button.onfocus=show;
 branch.onpointerleave=()=>{request++;submenu?.remove();submenu=null;button.setAttribute('aria-expanded','false')};
 button.onkeydown=async e=>{if(e.key==='ArrowRight'){e.preventDefault();await show();submenu?.querySelector('button')?.focus()}};
}

$('#history-back').onclick=()=>{
 const previous=state.historyReturn||{view:'library',groupFilter:null,selected:state.historyPlasmid};
 Object.assign(state,previous);$('#history-detail-panel').replaceChildren();render();
};
$('#daily-versions').onchange=async e=>{
 const checkbox=e.target,requested=checkbox.checked;checkbox.disabled=true;
 $('#daily-versions-status').textContent=requested?'正在保护现有原件，请稍候…':'正在保存设置…';
 try{
  const result=window.pywebview?.api?.set_daily_versions?await window.pywebview.api.set_daily_versions(requested):await api('/api/settings/daily_versions',{method:'POST',body:JSON.stringify({enabled:requested})});
  if(result.error)throw new Error(result.error);
  if(result.cancelled){checkbox.checked=state.dailyVersions;return}
  state.dailyVersions=result.enabled;checkbox.checked=result.enabled;
  toast(result.enabled?'已启用按日副本，原件已受保护':'已关闭按日副本，历史版本仍保留');
 }catch(error){checkbox.checked=state.dailyVersions;toast(error.message)}
 finally{checkbox.disabled=false;$('#daily-versions-status').textContent='启用时先保护现有原件；备份和目录迁移会包含历史副本。'}
};
