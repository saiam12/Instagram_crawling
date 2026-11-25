const {test} = require('node:test');
const assert = require('node:assert/strict');
global.Utilities = {formatDate: (d, timeZone, format) => {
  const options = format === 'HH:mm:ss'
    ? {timeZone,hour:'2-digit',minute:'2-digit',second:'2-digit',hourCycle:'h23'}
    : {timeZone,year:'numeric',month:'2-digit',day:'2-digit'};
  const parts = new Intl.DateTimeFormat('en-CA', options).formatToParts(d);
  const get = name => parts.find(p => p.type === name).value;
  return format === 'HH:mm:ss'
    ? `${get('hour')}:${get('minute')}:${get('second')}`
    : `${get('year')}-${get('month')}-${get('day')}`;
}};
global.SpreadsheetApp = {flush() {}};
const api = require('../pool/apps_script/Code.js');
const now = Date.parse('2026-09-12T10:00:00Z');
const model = 'gemini-3.6-flash';
const configRows = [['a','',model,5,20,'사용','','',''], ['b','',model,5,20,'사용','','','']];
const config = () => api.settingsFrom(configRows);
const offered = [{key_label:'a',model}, {key_label:'b',model}];
const body = id => ({request_id:id.repeat(8)+'-1111-1111-1111-111111111111',user:'tester',candidates:offered});

test('request time display uses KST while retaining complete timestamps and blanks',()=>{
  const started='2026-11-17T14:59:59.123Z', ended='2026-11-17T15:00:01.456Z';
  let values=[[started,ended],[new Date(started),'']], format, headers, zone;
  const book={setSpreadsheetTimeZone:value=>{zone=value;}};
  const times={getValues:()=>values,setValues:next=>{values=next;}};
  const source={getLastRow:()=>7,getRange:(row)=> row==='F6:G'
    ? {setNumberFormat:value=>{format=value;}}
    : row==='F5:G5' ? {setValues:value=>{headers=value;}} : times};
  api.formatRequestTimes(book,source);
  assert.equal(zone,'Asia/Seoul'); assert.equal(format,'HH:mm:ss');
  assert.equal(values[0][0].getTime(),Date.parse(started));
  assert.equal(values[0][1].getTime(),Date.parse(ended));
  assert.equal(api.timeAt(values[0][0]),'23:59:59');
  assert.equal(api.timeAt(values[0][1]),'00:00:01');
  assert.equal(values[1][1],'');
  assert.deepEqual(headers,[['요청 시각(KST)','완료/갱신 시각(KST)']]);
  const running=log('a','사용중'); running[5]=new Date(now-1000); running[6]=new Date(now);
  assert.equal(api.statsFor(config()[0],[running],now).busy,true);
  assert.equal(api.statsFor(config()[0],[running],now).rpm,1);
});
test('explicit retired-record cleanup preserves all other rows',t=>{
  const previous={SpreadsheetApp:global.SpreadsheetApp,LockService:global.LockService};
  t.after(()=>Object.assign(global,previous));
  const makeSheet=column=>{
    const rows=['gemini-3.5-flash','gemini-3.8-flash','gemini-3.7-flash','gemini-3.7-flash','antigravity-preview-09-2026'].map(model=>{
      const row=Array(17).fill(''); row[column]=model; return row;
    });
    return {rows,getLastRow:()=>rows.length+5,getRange:()=>({getValues:()=>rows,getDisplayValues:()=>rows,getNotes:()=>rows.map(row=>row.map(()=>''))}),
      deleteRows(start,count){rows.splice(start-6,count);}};
  };
  const sheets={'프로젝트 설정':makeSheet(2),'사용 현황':makeSheet(2),'요청 기록':makeSheet(3)};
  global.SpreadsheetApp={flush(){},openById:()=>({getSheetByName:name=>sheets[name]})};
  let released=false;
  global.LockService={getScriptLock:()=>({waitLock(){},releaseLock(){released=true;}})};
  assert.deepEqual(api.removeRetiredModelRecords(),{'프로젝트 설정':3,'사용 현황':3,'요청 기록':3});
  assert.deepEqual(sheets['요청 기록'].rows.map(row=>row[3]),['gemini-3.8-flash','antigravity-preview-09-2026']);
  assert.equal(released,true);
});
function log(project, status, day='2026-09-12', limit='') {
  return ['old','other',project,model,day,'2026-09-12T08:00:00Z','',status,'','','','',limit,20];
}
function sheet(initial=[]) {
  return {rows:structuredClone(initial), writes:[], getRange(row,col,height,width) {
    return {getNote:()=>'',setNote:()=>{},setValues: values => {
      this.writes.push(structuredClone(values));
      values.forEach((v,i) => {this.rows[row-6+i] = [...v];});
    }};
  }};
}
test('reserves both, chooses lower daily count, immediately releases loser', () => {
  const s=sheet([log('a','성공'),log('a','성공')]);
  const result=api.acquire(s,config(),s.rows,body('a'),now);
  assert.equal(result.selected.key_label,'b');
  assert.deepEqual(s.writes[0].map(r=>r[7]), ['후보 예약','후보 예약']);
  assert.equal(s.writes[1].filter(r=>r[7]==='사용중').length,1);
  assert.equal(s.writes[1].filter(r=>r[7]==='선택 해제').length,1);
});
test('equal remaining quota chooses the higher Flash model',()=>{
  const low=log('p1','후보 예약'); low[0]='request:0'; low[3]='gemini-3.6-flash'; low[13]=17;
  const high=log('p2','후보 예약'); high[0]='request:1'; high[3]='gemini-3.8-flash'; high[13]=17;
  api.selectRows([low,high]);
  assert.equal(low[7],'선택 해제'); assert.equal(high[7],'사용중');
});
test('next client cannot select active project/model',()=>{
  const s=sheet(); const first=api.acquire(s,config(),s.rows,body('a'),now);
  const second=api.acquire(s,config(),s.rows,body('b'),now);
  assert.notEqual(first.selected.project,second.selected.project);
  assert.equal(api.acquire(s,config(),s.rows,body('c'),now).selected,null);
});
test('reservation retry and finish retry do not double count',()=>{
  const s=sheet(); const b=body('a'); const first=api.acquire(s,config(),s.rows,b,now);
  assert.deepEqual(api.acquire(s,config(),s.rows,b,now),first);
  assert.equal(s.rows.length,2);
  const f={user:'tester',request_id:first.selected.request_id,status:'성공',input_tokens:12};
  api.finish(s,s.rows,f,now+1000); api.finish(s,s.rows,f,now+2000);
  assert.equal(s.rows.filter(r=>r[7]==='성공').length,1);
  assert.throws(()=>api.acquire(s,config(),s.rows,b,now));
});
test('active key and model are excluded from candidates',()=>{
  const active=log('a','사용중'); active[6]=new Date(now).toISOString();
  const sample=api.sampleCandidates(config(),[active],offered,now,()=>0);
  assert.deepEqual(sample.map(c=>c.project),['b']);
});
test('daily exhaustion resets at Korean midnight, active lock survives',()=>{
  const c=config()[0]; const logs=[log('a','실패','2026-09-12','일일')];
  assert.equal(api.statsFor(c,logs,now).daily,true);
  assert.equal(api.statsFor(c,logs,Date.parse('2026-09-12T14:59:59Z')).daily,true);
  assert.equal(api.statsFor(c,logs,Date.parse('2026-09-12T15:00:00Z')).daily,false);
  const afterMidnight=Date.parse('2026-09-12T15:00:00Z');
  const running=log('a','사용중'); running[6]=new Date(afterMidnight).toISOString(); logs.push(running);
  const active=api.statsFor(c,logs,afterMidnight);
  assert.equal(active.busy,true);
  assert.deepEqual(active.users,['other']);
  assert.equal(api.statsFor(c,logs,afterMidnight+10*60*1000+1).busy,false);
});
test('overview shows the last use and refresh as Korean times without dates',()=>{
  const overview={
    getLastRow:()=>5,
    headers:{},
    getRange:(...args)=>args.length===1 ? {setValue(value) { overview.headers[args[0]]=value; }} : {setValues() {}},
  };
  const book={getSheetByName:name=>name==='사용 현황'?overview:null};
  const rows=api.refreshOverview(book,config(),[log('a','성공')],Date.parse('2026-09-12T15:01:02Z'));
  assert.equal(rows[0][7],'17:00:00');
  assert.equal(rows[0][8],'00:01:02');
  assert.equal(rows[0][9],'');
  assert.equal(overview.headers.H5,'마지막 사용 시각(KST)');
});
test('last use ignores unselected candidates and keeps the newest counted request',()=>{
  const old=log('a','성공'); old[5]='2026-09-12T08:00:00Z';
  const unselected=log('a','선택 해제'); unselected[5]='2026-09-12T09:00:00Z';
  const latest=log('a','실패'); latest[5]='2026-09-12T08:30:00Z';
  assert.equal(api.statsFor(config()[0],[old,unselected,latest],now).lastUsed,
    Date.parse('2026-09-12T08:30:00Z'));
});
test('Date-valued sheet days count both successful and failed requests',()=>{
  const c=config()[0];
  const sheetDate=new Date('2026-09-12T12:00:00+09:00');
  const logs=[log('a','성공',sheetDate),log('a','실패',sheetDate),log('a','선택 해제',sheetDate)];
  assert.equal(api.statsFor(c,logs,now).count,2);
});
test('readRows keeps the sheet-displayed day when Sheets coerces it to Date',()=>{
  const values=[log('p1','성공',new Date('2026-09-11T15:00:00Z'))];
  const displayed=[values[0].map((v,i)=>i===4?'2026-09-12':String(v ?? ''))];
  const source={getLastRow:()=>6,getRange:()=>({getValues:()=>values,getDisplayValues:()=>displayed,getNotes:()=>values.map(row=>row.map(()=>''))})};
  assert.equal(api.readRows(source,14)[0][4],'2026-09-12');
});
test('baseline, RPM and temporary cooldown affect eligibility',()=>{
  const c=api.settingsFrom([['a','',model,5,20,'사용','','2026-09-12',20]]);
  assert.equal(api.sampleCandidates(c,[],offered,now,()=>0).length,0);
  const logs=Array.from({length:5},()=>{const r=log('a','성공');r[5]=new Date(now-1000).toISOString();return r;});
  assert.equal(api.sampleCandidates(config(),logs,offered,now,()=>0)[0].project,'b');
  const r=log('a','실패');r.retryAt=new Date(now+60000).toISOString();
  assert.equal(api.sampleCandidates(config(),[r],offered,now,()=>0)[0].project,'b');
});
test('3.5 Flash Lite is allowed and duplicate mappings are rejected',()=>{
  assert.equal(api.settingsFrom([['k','','gemini-3.5-flash-lite',5,500,'사용']])[0].rpd,500);
  assert.throws(()=>api.settingsFrom([...configRows,configRows[0]]));
});
test('retired settings are ignored while 3.8 stays available',()=>{
  const rows=[...configRows,...['gemini-3.5-flash','gemini-3.7-flash','gemini-3.8-flash'].map(m=>['c','',m,5,20,'사용','','',''])];
  assert.deepEqual(api.settingsFrom(rows).map(c=>c.model),[model,model,'gemini-3.8-flash']);
});
test('unfinished two-candidate reservation is recovered on same ID',()=>{
  const b=body('a'); const rows=config().map((c,i)=>[b.request_id+':'+i,b.user,c.key_label,c.model,'2026-09-12',new Date(now).toISOString(),'','후보 예약','','','','','',20-i]);
  const s=sheet(rows); const result=api.acquire(s,config(),s.rows,b,now);
  assert.equal(result.selected.key_label,'a');
  assert.equal(s.rows[1][7],'선택 해제');
});
test('empty config is terminal and does not fabricate quota',()=>{
  const s=sheet(); const r=api.acquire(s,[],[],body('a'),now);
  assert.equal(r.terminal,true); assert.equal(s.rows.length,0);
});
test('sync adds shared aliases, normalizes owners, and leaves unrelated rows alone',()=>{
  const settings=sheet([
    ['kept','',model,7,30,'중지','manual','',''],
    ['kept','','gemini-3.8-flash',5,20,'중지','manual','',''],
    ['kept','','gemini-3.5-flash',5,20,'사용','manual','',''],
    ['removed','tester',model,5,20,'사용','manual','',''],
    ['other','someone-else',model,5,20,'사용','manual','',''],
  ]);
  const shared=[{key_label:'kept',owner:'tester'},{key_label:'new',owner:'tester'}];
  const result=api.syncSettings(settings,settings.rows,shared);
  assert.deepEqual(result,{ok:true,added:8,updated:3,enabled:1,stopped:1});
  assert.equal(settings.rows.find(r=>r[0]==='kept' && r[2]===model)[5],'중지');
  assert.ok(settings.rows.filter(r=>r[0]==='kept').every(r=>r[1]==='tester'));
  assert.equal(settings.rows.find(r=>r[2]==='gemini-3.8-flash')[5],'사용');
  assert.equal(settings.rows.find(r=>r[2]==='gemini-3.5-flash')[5],'중지');
  assert.equal(settings.rows.find(r=>r[0]==='removed')[5],'사용');
  assert.equal(settings.rows.find(r=>r[0]==='other')[5],'사용');
  assert.deepEqual(settings.rows.filter(r=>r[0]==='new').map(r=>r[2]),[
    'gemini-3.6-flash','gemini-3.8-flash','gemini-3.5-flash-lite','antigravity-preview-09-2026','gemini-embedding-2',
  ]);
});
test('sync applies workload daily limits without changing RPM, status or counted history',()=>{
  const models=['gemini-3.6-flash','gemini-3.8-flash','gemini-embedding-2','antigravity-preview-09-2026','gemini-3.5-flash-lite'];
  const settings=sheet(models.map(model=>['kept','tester',model,7,1,'중지','manual','2026-11-01',9]));
  api.syncSettings(settings,settings.rows,[{key_label:'kept',owner:'tester'}]);
  assert.deepEqual(models.map(model=>settings.rows.find(row=>row[2]===model)[4]),[20,20,1000,100,500]);
  assert.ok(settings.rows.filter(row=>row[0]).every(row=>row[3]===7 && row[5]==='중지' && row[7]==='2026-11-01' && row[8]===9));
  assert.deepEqual(settings.rows.map(row=>row[2]),['gemini-3.6-flash','gemini-3.8-flash','','gemini-3.5-flash-lite','','antigravity-preview-09-2026','','gemini-embedding-2']);
});
test('deployment always opens the shared spreadsheet from the source constant',t=>{
  const previous={PropertiesService:global.PropertiesService,LockService:global.LockService,
    ContentService:global.ContentService,SpreadsheetApp:global.SpreadsheetApp};
  t.after(()=>Object.assign(global,previous));
  const token='test-token-'.repeat(4);
  global.PropertiesService={getScriptProperties:()=>({getProperty:key=>({POOL_TOKEN:token,SHEET_ID:'team-sheet-id'})[key]})};
  global.LockService={getScriptLock:()=>({waitLock(){},hasLock:()=>true,releaseLock(){}})};
  global.ContentService={MimeType:{JSON:'json'},createTextOutput:text=>({setMimeType:()=>JSON.parse(text)})};
  let opened;
  global.SpreadsheetApp={openById:id=>{opened=id;return {getSheetByName:()=>null};}};
  api.doPost({postData:{contents:JSON.stringify({token,user:'tester',action:'status'})}});
  assert.equal(opened,'1g9sq7tuoiz-WE1OZNbN48M_KA0FY7MXgssE1_ev_-O8');
});
test('shared spreadsheet does not require a spreadsheet Script Property',t=>{
  const previous={PropertiesService:global.PropertiesService,LockService:global.LockService,
    ContentService:global.ContentService,SpreadsheetApp:global.SpreadsheetApp};
  t.after(()=>Object.assign(global,previous));
  const token='test-token-'.repeat(4);
  global.PropertiesService={getScriptProperties:()=>({getProperty:key=>key==='POOL_TOKEN'?token:null})};
  global.LockService={getScriptLock:()=>({waitLock(){},hasLock:()=>true,releaseLock(){}})};
  global.ContentService={MimeType:{JSON:'json'},createTextOutput:text=>({setMimeType:()=>JSON.parse(text)})};
  let accessed=false;
  global.SpreadsheetApp={openById:()=>{accessed=true;return {getSheetByName:()=>null};}};
  const result=api.doPost({postData:{contents:JSON.stringify({token,user:'tester',action:'status'})}});
  assert.equal(result.ok,false);
  assert.match(result.error,/설정\/요청 기록 탭/);
  assert.equal(accessed,true);
});
test('shared key merge lets only the original owner update a key',()=>{
  const first=api.mergeSharedKeys([], [{key_label:'a',api_key:'owner-key'}], 'owner', now);
  assert.equal(first.vault_added,1);
  const teammate=api.mergeSharedKeys(first.keys,[
    {key_label:'a',api_key:'stale-key'},{key_label:'b',api_key:'team-key'},
  ],'teammate',now+1);
  assert.equal(teammate.keys.find(k=>k.key_label==='a').api_key,'owner-key');
  assert.equal(teammate.keys.find(k=>k.key_label==='b').owner,'teammate');
  const updated=api.mergeSharedKeys(teammate.keys,[{key_label:'a',api_key:'rotated-key'}],'owner',now+2);
  assert.equal(updated.vault_updated,1);
  assert.equal(updated.keys.find(k=>k.key_label==='a').api_key,'rotated-key');
});
test('shared key merge assigns and updates a distinct owner for each key',()=>{
  const first=api.mergeSharedKeys([], [
    {key_label:'a',api_key:'key-a',owner:'alice'},
    {key_label:'b',api_key:'key-b',owner:'bob'},
  ],'operator',now);
  assert.deepEqual(first.keys.map(k=>[k.key_label,k.owner]),[['a','alice'],['b','bob']]);
  const reassigned=api.mergeSharedKeys(first.keys,[
    {key_label:'a',api_key:'key-a',owner:'charlie'},
    {key_label:'b',api_key:'key-b',owner:'bob'},
  ],'operator',now+1);
  assert.equal(reassigned.owner_renamed,1);
  assert.equal(reassigned.keys.find(k=>k.key_label==='a').owner,'charlie');
});
test('single owner can change only the display name while keeping identical keys',()=>{
  const stored=[
    {key_label:'a',api_key:'key-a',owner:'hunseok'},
    {key_label:'b',api_key:'key-b',owner:'hunseok'},
  ];
  const submitted=stored.map(({key_label,api_key})=>({key_label,api_key}));
  const merged=api.mergeSharedKeys(stored,submitted,'훈석',now);
  assert.equal(merged.owner_renamed,2);
  assert.ok(merged.keys.every(item=>item.owner==='훈석'));
});
test('heartbeat refreshes only the matching active reservation',()=>{
  const row=log('a','사용중'); row[0]='request:0'; row[1]='tester';
  const logs=sheet([row]);
  assert.deepEqual(api.heartbeat(logs,logs.rows,{request_id:'request:0',user:'tester'},now),{ok:true});
  assert.equal(logs.rows[0][6].getTime(),now);
});
test('old project and key alias columns migrate into one key alias column',()=>{
  const grid=[
    ['프로젝트 별칭','키 별칭','담당자','모델','RPM 한도','RPD 한도','사용 여부','비고','초기 집계일','초기 사용량'],
    ['project-acct1','acct1','tester',model,5,20,'사용','memo','',''],
  ];
  const source={
    getLastRow:()=>6,
    getRange(row,col,height,width) {
      const values=Array.from({length:height},(_,i)=>grid[row-5+i].slice(col-1,col-1+width));
      return {
        getValues:()=>structuredClone(values), getDisplayValues:()=>structuredClone(values),
        setValues:next=>next.forEach((items,i)=>items.forEach((value,j)=>{grid[row-5+i][col-1+j]=value;})),
      };
    },
    deleteColumn(col) { grid.forEach(row=>row.splice(col-1,1)); },
  };
  assert.equal(api.migrateSettingsSchema(source),true);
  assert.deepEqual(grid[0],['키 별칭','담당자','모델','RPM 한도','RPD 한도','사용 여부','비고','초기 집계일','초기 사용량']);
  assert.deepEqual(grid[1],['acct1','tester',model,5,20,'사용','memo','','']);
});

test('request columns migrate safely, resume after interruption and retain cooldown notes',()=>{
  const headers=['요청 ID','사용자','프로젝트 별칭','키 별칭','모델','요청 시각(KST)','완료/갱신 시각(KST)','처리 상태','입력 토큰','출력 토큰','총 토큰','오류 코드','제한 종류','재시도 가능 시각(UTC)','집계일(KST)','후보 선택 전 요청 수','후보 선택 전 잔여량'];
  const started=new Date(now-1000),ended=new Date(now);
  const old=['legacy','tester','old-project','a',model,started,ended,'실패',12,5,17,503,'일시',new Date(now+60000).toISOString(),'2026-09-12',3,17];
  const grid=[headers,old],notes=[headers.map(()=>''),old.map(()=>'')]; notes[1][12]='사용자 메모';
  let interrupted=false;
  const source={
    getLastRow:()=>6,getMaxRows:()=>1000,getMaxColumns:()=>grid[0].length,
    getRange(row,col,height,width) {
      if (typeof row==='string') return {setNumberFormat(){}};
      const range={column:col,
        getValues:()=>grid.slice(row-5,row-5+height).map(r=>r.slice(col-1,col-1+width)),
        getDisplayValues:()=>grid.slice(row-5,row-5+height).map(r=>r.slice(col-1,col-1+width).map(String)),
        getNotes:()=>notes.slice(row-5,row-5+height).map(r=>r.slice(col-1,col-1+width)),
        setValues:values=>{values.forEach((r,i)=>r.forEach((v,j)=>{grid[row-5+i][col-1+j]=v;}));return range;},
        setNotes:values=>{values.forEach((r,i)=>r.forEach((v,j)=>{notes[row-5+i][col-1+j]=v;}));return range;},
      }; return range;
    },
    deleteColumn(col) {
      grid.forEach(r=>r.splice(col-1,1));notes.forEach(r=>r.splice(col-1,1));
      if (!interrupted) {interrupted=true;throw new Error('simulated interrupted migration');}
    },
    moveColumns(range,destination) {
      for (const rows of [grid,notes]) rows.forEach(r=>r.splice(destination-1,0,...r.splice(range.column-1,1)));
    },
  };
  assert.throws(()=>api.migrateRequestSchema(source),/interrupted/);
  assert.equal(api.migrateRequestSchema(source),true);
  assert.equal(api.migrateRequestSchema(source),false);
  assert.deepEqual(grid[0],['요청 ID','사용자','키 별칭','모델','집계일(KST)','요청 시각(KST)','완료/갱신 시각(KST)','처리 상태','입력 토큰','출력 토큰','총 토큰','오류 코드','제한 종류','후보 선택 전 잔여량']);
  assert.deepEqual(grid[1],['legacy','tester','a',model,'2026-09-12',started,ended,'실패',12,5,17,503,'일시',17]);
  const migrated=api.readRows(source,14);
  assert.match(notes[1][12],/사용자 메모/);
  assert.equal(api.statsFor(config()[0],migrated,now).retry,now+60000);
  assert.equal(api.statsFor(config()[0],migrated,now).count,1);
  assert.equal(api.statsFor(config()[0],migrated,now).rpm,1);
});
