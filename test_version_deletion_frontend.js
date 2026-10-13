const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');

async function verifyDeletion(){
 const source=fs.readFileSync('history.js','utf8');
 const elements=new Map(),calls=[],dialogs=[],messages=[];
 let menu,select,closed=0;
 const state={view:'history',historyPlasmid:7,historyVersion:12,historyItems:[
  {id:11,name:'old.dna',time:'2026-10-12'}, {id:12,name:'latest.dna',time:'2026-10-13',latest:true}],
  versionPreviews:{'7:12':{old:true},'17:1':{keep:true}},previews:{7:{old:true}},noteDrafts:{7:'draft'}};
 const scope={state,Date,esc:String,toast:m=>messages.push(m),
  $:id=>{if(!elements.has(id))elements.set(id,{});return elements.get(id)},
  openContextMenu:(event,items,callback)=>{menu=items;select=callback},
  openActionDialog:(...args)=>dialogs.push(args),closeActionDialog:()=>closed++,
  loadPlasmids:async()=>{},renderList:()=>{},renderDetail:()=>{},
  api:async(path,options)=>{
   calls.push([path,options?.method]);
   return options?.method==='DELETE'?{ok:true}:{versions:[state.historyItems[0]]};
  }
 };
 vm.runInNewContext(source.slice(0,source.indexOf("$('#history-back').onclick")),scope);
 scope.drawHistoryList=()=>{};
 scope.openVersionActions(7,12,{});
 assert.equal(menu.find(item=>item[0]==='delete')[1],'删除副本');
 await select('delete');
 assert.equal(calls.length,0,'No deletion before secondary confirmation');
 assert.equal(dialogs.at(-1)[0],'确认删除副本');
 assert.match(dialogs.at(-1)[1],/latest.dna/);
 assert.match(dialogs.at(-1)[1],/切换到最近保留/);
 elements.get('#cancel-version-delete').onclick();
 assert.equal(calls.length,0,'Cancel must preserve the version');
 scope.openDeleteVersion(7,12);
 await elements.get('#confirm-version-delete').onclick();
 assert.deepEqual(calls[0],['/api/plasmids/7/versions/12','DELETE']);
 assert.equal(state.versionPreviews['7:12'],undefined);
 assert.equal(state.versionPreviews['17:1'].keep,true);
 assert.equal(state.previews[7],undefined);
 assert.equal(state.historyVersion,11);
 assert.equal(state.noteDrafts[7],'draft');
 assert.match(messages.at(-1),/移入回收站/);
 const before=calls.length;
 scope.openDeleteVersion(7,11);
 assert.equal(calls.length,before,'The last retained version cannot be deleted');
 assert.match(messages.at(-1),/至少保留一个/);
 state.historyItems=[{id:11,name:'old.dna'}, {id:12,name:'latest.dna'}];
 scope.api=async()=>{throw new Error('无法保留回收站副本')};
 scope.openDeleteVersion(7,12);
 const beforeClosed=closed;
 await elements.get('#confirm-version-delete').onclick();
 assert.equal(closed,beforeClosed,'Failed deletion keeps the dialog open');
 assert.equal(elements.get('#confirm-version-delete').disabled,false);
 assert.equal(messages.at(-1),'无法保留回收站副本');
 console.log('Version deletion menu, confirmation, cancel, last-version guard, cache refresh and errors: OK');
}
verifyDeletion().catch(error=>{console.error(error);process.exitCode=1});
