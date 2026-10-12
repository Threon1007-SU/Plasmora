const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');

const source = fs.readFileSync('app.js', 'utf8');
const boundary = source.indexOf("$('#search').addEventListener");
assert.ok(boundary > 0, 'Could not isolate app initialization');
const context = {Intl};
vm.runInNewContext(`${source.slice(0, boundary)}\nglobalThis.hooks={state,matches,pickerMatches,filtered,sortedPlasmids,dragExportDescriptor,renderPrimers,renderPreviewMode,hitBadge};`, context);
const {state, matches, pickerMatches, filtered, sortedPlasmids, dragExportDescriptor, renderPrimers, renderPreviewMode, hitBadge} = context.hooks;

state.synonymClusters = [{id: 1, terms: ['ITPR1', 'IP3R1']}];
state.plasmids = [
  {id: 1, name: 'ITPR1 construct.dna', tags: [], note: '', favorite: true, lastViewedAt: '2026-09-28T08:00:00', groupIds: [], importedAt: '2026-09-27', size: 2},
  {id: 2, name: 'IP3R1 construct.dna', tags: [], note: '', favorite: false, lastViewedAt: '2026-09-28T09:00:00', groupIds: [], importedAt: '2026-09-27', size: 1},
  {id: 3, name: 'Other.dna', tags: ['Feature-X'], note: '待测序 ITPR1 control', favorite: false, lastViewedAt: null, groupIds: [], importedAt: '2026-09-27', size: 3},
];

state.searchDetails = true;
state.search = 'ITPR1';
assert.deepEqual(Array.from(filtered(), p => p.id), [1, 2, 3]);
state.searchDetails = false;
assert.deepEqual(Array.from(filtered(), p => p.id), [1, 2]);
assert.equal(matches(state.plasmids[2], 'Feature-X'), false);
state.searchDetails = true;
assert.equal(matches(state.plasmids[2], 'Feature-X'), true);
assert.equal(pickerMatches(state.plasmids[2], 'Feature-X', true), true);
assert.equal(pickerMatches(state.plasmids[2], 'Feature-X', false), false);
assert.equal(pickerMatches(state.plasmids[2], 'control', true), true);
assert.equal(pickerMatches(state.plasmids[2], 'control', false), false);
assert.equal(pickerMatches(state.plasmids[1], 'ITPR1', false), true);
assert.equal(state.searchPrimers, false, 'Primer search must default to off');
state.plasmids[2].primerNames = ['Unique-Primer', 'IP3R1'];
assert.equal(matches(state.plasmids[2], 'Unique-Primer'), false);
state.searchDetails = false;
state.searchPrimers = true;
assert.equal(matches(state.plasmids[2], 'Unique-Primer'), true);
assert.equal(matches(state.plasmids[2], 'Feature-X'), false, 'Primer search must not enable feature search');
assert.equal(matches(state.plasmids[2], 'control'), false, 'Primer search must not enable note search');
assert.equal(matches(state.plasmids[2], 'ITPR1'), true, 'Primer names support synonym clusters');
assert.equal(pickerMatches(state.plasmids[2], 'Unique-Primer', false, true), true);
assert.equal(pickerMatches(state.plasmids[2], 'Unique-Primer', true, false), false);
assert.match(hitBadge(state.plasmids[2], 'Unique-Primer'), /引物 · Unique-Primer/);
assert.match(renderPreviewMode({primers: []}), /data-preview-mode="plasmid" aria-pressed="true"/);
assert.match(renderPrimers({primers: []}), /未保存引物信息/);
const primerPreview = {primers: [{name: '<unsafe-name>', sequence: 'AaGC', length: 4, gcPercent: 50,
  description: '<script>bad</script>', bindingSites: [{start: 8, end: 2, strand: -1, meltingTemperature: null}]}]};
assert.match(renderPrimers(primerPreview), /&lt;unsafe-name&gt;/);
assert.doesNotMatch(renderPrimers(primerPreview), /<script>/);
assert.match(renderPrimers(primerPreview), /跨越起点/);
assert.doesNotMatch(renderPrimers(primerPreview), /Tm null/);
state.primerQuery = 'missing';
assert.match(renderPrimers(primerPreview), /没有匹配的引物/);
state.primerQuery = '';
state.searchPrimers = false;
state.view = 'favorites';
state.search = '';
assert.deepEqual(Array.from(filtered(), p => p.id), [1]);
state.view = 'recent';
assert.deepEqual(Array.from(sortedPlasmids(filtered()), p => p.id), [2, 1]);
assert.equal(dragExportDescriptor({id: 7, name: 'JH:sample.dna'}, 'http://127.0.0.1:1234'), 'application/octet-stream:JH_sample.dna:http://127.0.0.1:1234/api/file/7');
console.log('Frontend search scopes and collections: OK');

const historySource = fs.readFileSync('history.js','utf8');
const historyContext = {Date,esc:context.hooks.esc||((s)=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])))};
vm.runInNewContext(historySource.slice(0,historySource.indexOf("$('#history-back').onclick"))+'\nglobalThis.renderEntries=renderVersionEntries;globalThis.versionActions=openVersionActions;', historyContext);
const historyMarkup=historyContext.renderEntries([{id:3,name:'<unsafe>.dna',time:'2026-10-08T12:00:00+08:00',latest:true,original:false,size:2048}],3);
assert.match(historyMarkup,/&lt;unsafe&gt;.dna/);
assert.match(historyMarkup,/history-card selected/);
assert.match(historyMarkup,/最新版本/);
console.log('History list escaping, selection and latest marker: OK');

function makeHistoryScope(extra){
  const scope={Date,esc:historyContext.esc,...extra};
  vm.runInNewContext(historySource.slice(0,historySource.indexOf("$('#history-back').onclick"))+
    '\nglobalThis.historyHooks={invalidateVersionPreviews,resetHistoryState,loadVersionPreview,openHistory};',scope);
  return scope;
}

async function verifySelectedVersionLaunch(){
  const calls=[],messages=[];
  let select;
  historyContext.openContextMenu=(event,items,onSelect)=>{select=onSelect};
  historyContext.window={pywebview:{api:{}}};
  historyContext.window.pywebview.api.open_in_snapgene=async (...args)=>{calls.push(args);return {ok:true,method:'snapgene'}};
  historyContext.toast=message=>messages.push(message);
  historyContext.versionActions(7,12,{});
  await select('snapgene');
  assert.deepEqual(calls,[[7,12]],'History actions must pass both the family and selected revision');
  assert.match(messages.pop(),/所选副本/);
  historyContext.window.pywebview.api.open_in_snapgene=async()=>({error:'副本已不存在'});
  await select('snapgene');
  assert.equal(messages.pop(),'副本已不存在');
  console.log('Selected history version launch and errors: OK');
}
verifySelectedVersionLaunch().catch(error=>{console.error(error);process.exitCode=1});

async function verifyBackgroundRepositoryUpdates() {
  const listeners = [];
  const timers = [];
  const messages = [];
  let manualSyncs = 0, notifications = {updated: [], errors: []};
  let listRenders = 0, detailRenders = 0, historyRefreshes = 0, progressHidden = true;
  const elements = new Map();
  const backgroundState = {storage: {}, selected: 2, activeFeature: 3,
    previews: {1: {cached: true}, 2: {cached: true}}, noteDrafts: {2: 'unsaved note'},
    plasmids: [{id: 2}], view:'library', historyPlasmid:null,
    versionPreviews:{'1:2':{primer:'Old-F'},'1:3':{loading:true},'2:4':{cached:true},'10:1':{cached:true}}};
  const savedNotes = [];
  let noteSaveFails = false;
  const background = {
    window: {addEventListener: (event, handler) => listeners.push([event, handler]), pywebview: {api: {
      sync_repository: async () => {manualSyncs++; return {updated: [], errors: []};},
      take_repository_updates: async () => notifications,
    }}},
    document: {body: {inert: false}, querySelector: selector => {
      if (!elements.has(selector)) elements.set(selector, {classList: {contains: () => progressHidden}});
      return elements.get(selector);
    }},
    state: backgroundState, toast: message => messages.push(message),
    api: async (path, options) => {
      if (noteSaveFails) throw new Error('保存失败');
      savedNotes.push([path, JSON.parse(options.body).note]);
      return {};
    },
    loadPlasmids: async () => {}, renderList: () => listRenders++, renderDetail: () => detailRenders++,
    setInterval: (handler, delay) => timers.push([handler, delay]),
  };
  const enhancement = fs.readFileSync('enhancements.js', 'utf8');
  const backgroundHistory=makeHistoryScope({state:backgroundState});
  background.invalidateVersionPreviews=backgroundHistory.historyHooks.invalidateVersionPreviews;
  background.refreshHistory=async()=>historyRefreshes++;
  vm.runInNewContext(enhancement.slice(0, enhancement.indexOf('async function showTrash()')), background);
  assert.equal(listeners.filter(([event]) => event === 'focus').length, 0);
  assert.equal(timers.length, 1);
  await timers[0][0]();
  assert.equal(manualSyncs, 0);
  assert.equal(messages.length, 0);
  notifications = {updated: [1], errors: []};
  await timers[0][0]();
  assert.equal(listRenders, 1);
  assert.equal(detailRenders, 0, 'Unrelated edits should not replace the open preview');
  assert.equal(backgroundState.previews[1], undefined);
  assert.equal(backgroundState.previews[2].cached, true);
  assert.equal(backgroundState.versionPreviews['1:2'],undefined,'Updates must clear historical previews while outside history');
  assert.equal(backgroundState.versionPreviews['1:3'],undefined);
  assert.equal(backgroundState.versionPreviews['2:4'].cached,true);
  assert.equal(backgroundState.versionPreviews['10:1'].cached,true,'Family prefixes must not collide');
  assert.equal(backgroundState.noteDrafts[2], 'unsaved note');
  notifications = {updated: [2], errors: []};
  progressHidden = false;
  await timers[0][0]();
  assert.equal(detailRenders, 0, 'Defer notifications during a foreground operation');
  progressHidden = true;
  await timers[0][0]();
  assert.equal(detailRenders, 1);
  assert.equal(backgroundState.activeFeature, null);
  assert.equal(backgroundState.noteDrafts[2], 'unsaved note');
  backgroundState.view='history';backgroundState.historyPlasmid=2;
  backgroundState.versionPreviews['1:2']={primer:'Old-F'};
  notifications={updated:[1],errors:[]};
  await timers[0][0]();
  assert.equal(backgroundState.versionPreviews['1:2'],undefined,'Clear updated family even when another history is visible');
  assert.equal(historyRefreshes,0);
  assert.equal(detailRenders,1,'Other history updates must not redraw the visible preview');
  notifications={updated:[2],errors:[]};
  await timers[0][0]();
  assert.equal(historyRefreshes,1,'Refresh visible history after invalidating its cache');
  await elements.get('#sync-repository').onclick();
  assert.equal(manualSyncs, 1, 'Manual full synchronization remains available');
  const prepared = await background.window.plasmoraPrepareForUpdate();
  assert.equal(prepared.ready, true);
  assert.deepEqual(savedNotes, [['/api/plasmids/2/note', 'unsaved note']]);
  assert.equal(backgroundState.noteDrafts[2], undefined);
  assert.equal(background.document.body.inert, true, 'Freeze editing until shutdown finishes');
  background.window.plasmoraCancelUpdate();
  assert.equal(background.document.body.inert, false);
  backgroundState.noteDrafts[2] = 'keep draft if saving fails';
  noteSaveFails = true;
  const failed = await background.window.plasmoraPrepareForUpdate();
  assert.equal(failed.ready, false);
  assert.equal(failed.failed, true);
  assert.equal(backgroundState.noteDrafts[2], 'keep draft if saving fails');
  assert.equal(background.document.body.inert, false);
  console.log('Background repository notifications and manual sync: OK');
  console.log('Update shutdown saves notes and preserves failed drafts: OK');
}
verifyBackgroundRepositoryUpdates().catch(error => {console.error(error); process.exitCode = 1;});

async function verifyHistoryCacheAndRecovery(){
  const state={view:'library',selected:1,groupFilter:null,versionPreviews:{'1:2':{primer:'Old-F'},'2:7':{keep:true}}};
  let requested=0,pendingPreview;
  const scope=makeHistoryScope({state,closeContextMenu:()=>{},toast:message=>{throw new Error(message)},
    $:()=>({replaceChildren:()=>{}}),renderDetail:()=>{},
    api:async path=>{
      if(path.endsWith('/preview')){requested++;return {primer:'New-F'}}
      return {versions:[{id:2,latest:true}]};
    }});
  scope.drawHistoryList=()=>{};
  scope.render=()=>{pendingPreview=scope.historyHooks.loadVersionPreview(1,2)};
  await scope.historyHooks.openHistory(1,2);
  await pendingPreview;
  assert.equal(requested,1,'Re-entering history must request fresh content');
  assert.equal(state.versionPreviews['1:2'].primer,'New-F');
  assert.equal(state.versionPreviews['2:7'].keep,true);

  let resolveOld;
  scope.api=()=>new Promise(resolve=>{resolveOld=resolve});
  delete state.versionPreviews['1:2'];
  const oldRequest=scope.historyHooks.loadVersionPreview(1,2);
  scope.historyHooks.invalidateVersionPreviews(1);
  scope.api=async()=>({primer:'Newest-F'});
  await scope.historyHooks.loadVersionPreview(1,2);
  resolveOld({primer:'Stale-F'});await oldRequest;
  assert.equal(state.versionPreviews['1:2'].primer,'Newest-F','Late responses cannot repopulate invalidated caches');

  const elements=new Map(),dialogs=[];
  scope.document={querySelector:selector=>{
    if(!elements.has(selector))elements.set(selector,{});
    return elements.get(selector);
  }};
  scope.window={pywebview:{api:{restore_backup:async()=>({ok:true,count:1,rollbackIncomplete:true,
    preservedFileCount:2,unavailableFileCount:1,rollbackPath:'C:\\local\\Before-restore-example.recovery.zip'})}}};
  scope.openActionDialog=(...args)=>dialogs.push(args);
  scope.closeActionDialog=()=>{};scope.toast=()=>{};
  for(const name of ['loadSettings','loadPlasmids','loadGroups','loadSynonymClusters'])scope[name]=async()=>{};
  scope.render=()=>{};scope.renderSynonymClusters=()=>{};scope.renderThemes=()=>{};
  state.noteDrafts={1:'old'};state.previews={1:{old:true}};state.historyItems=[{id:2}];state.historyReturn={view:'history'};
  const enhancement=fs.readFileSync('enhancements.js','utf8');
  vm.runInNewContext(enhancement.slice(enhancement.indexOf('function confirmRestore('),enhancement.indexOf('function openEnhancedDataTools(')),scope);
  scope.api=()=>new Promise(resolve=>{resolveOld=resolve});
  delete state.versionPreviews['1:2'];
  const preRestoreRequest=scope.historyHooks.loadVersionPreview(1,2);
  scope.confirmRestore({count:1,createdAt:'2026-10-12'});
  await elements.get('#confirm-restore').onclick();
  resolveOld({primer:'Before-restore-F'});await preRestoreRequest;
  assert.equal(Object.keys(state.versionPreviews).length,0,'Restore clears all version caches, including in-flight results');
  assert.equal(state.historyItems.length,0);
  assert.equal(state.historyPlasmid,null);
  assert.equal(state.historyVersion,null);
  assert.equal(state.historyReturn,undefined);
  assert.equal(state.view,'library');
  assert.equal(dialogs.at(-1)[0],'恢复完成：原仓库不完整');
  assert.match(dialogs.at(-1)[2],/不能作为完整备份直接恢复/);
  assert.equal(typeof elements.get('#open-recovery').onclick,'function');
  console.log('History re-entry, in-flight invalidation, restore reset and incomplete recovery notice: OK');
}
verifyHistoryCacheAndRecovery().catch(error=>{console.error(error);process.exitCode=1});
