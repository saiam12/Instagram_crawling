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
function log(project, status, day='2026-09-12', limit='') {
  return ['old','other',project,'a',model,'2026-09-12T08:00:00Z','',status,'','','','',limit,'',day,0];
}
function sheet(initial=[]) {
  return {rows:structuredClone(initial), writes:[], getRange(row,col,height,width) {
    return {setValues: values => {
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
  const low=log('p1','후보 예약'); low[0]='request:0'; low[4]='gemini-3.5-flash'; low[15]=3; low[16]=17;
  const high=log('p2','후보 예약'); high[0]='request:1'; high[4]='gemini-3.6-flash'; high[15]=3; high[16]=17;
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
  const displayed=[values[0].map((v,i)=>i===14?'2026-09-12':String(v ?? ''))];
  const source={getLastRow:()=>6,getRange:()=>({getValues:()=>values,getDisplayValues:()=>displayed})};
  assert.equal(api.readRows(source,17)[0][14],'2026-09-12');
});
test('baseline, RPM and temporary cooldown affect eligibility',()=>{
  const c=api.settingsFrom([['a','',model,5,20,'사용','','2026-09-12',20]]);
  assert.equal(api.sampleCandidates(c,[],offered,now,()=>0).length,0);
  const logs=Array.from({length:5},()=>{const r=log('a','성공');r[5]=new Date(now-1000).toISOString();return r;});
  assert.equal(api.sampleCandidates(config(),logs,offered,now,()=>0)[0].project,'b');
  const r=log('a','실패');r[13]=new Date(now+60000).toISOString();
  assert.equal(api.sampleCandidates(config(),[r],offered,now,()=>0)[0].project,'b');
});
test('Lite and duplicate mappings are rejected',()=>{
  assert.throws(()=>api.settingsFrom([['k','','gemini-3.5-flash-lite',5,20,'사용']]));
  assert.throws(()=>api.settingsFrom([...configRows,configRows[0]]));
});
test('3.8 settings are ignored while other models stay available',()=>{
  const rows=[...configRows,['c','','gemini-3.8-flash',5,20,'사용','','','']];
  assert.deepEqual(api.settingsFrom(rows).map(c=>c.model),[model,model]);
});
test('unfinished two-candidate reservation is recovered on same ID',()=>{
  const b=body('a'); const rows=config().map((c,i)=>[b.request_id+':'+i,b.user,c.project,c.key_label,c.model,new Date(now).toISOString(),'','후보 예약','','','','','','','2026-09-12',i]);
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
    ['kept','','gemini-3.8-flash',5,20,'사용','manual','',''],
    ['removed','tester',model,5,20,'사용','manual','',''],
    ['other','someone-else',model,5,20,'사용','manual','',''],
  ]);
  const shared=[{key_label:'kept',owner:'tester'},{key_label:'new',owner:'tester'}];
  const result=api.syncSettings(settings,settings.rows,shared);
  assert.deepEqual(result,{ok:true,added:5,updated:2,enabled:1,stopped:1});
  assert.ok(settings.rows.filter(r=>r[0]==='kept').every(r=>r[1]==='tester'));
  assert.equal(settings.rows.find(r=>r[2]==='gemini-3.8-flash')[5],'중지');
  assert.equal(settings.rows.find(r=>r[0]==='removed')[5],'사용');
  assert.equal(settings.rows.find(r=>r[0]==='other')[5],'사용');
  assert.deepEqual(settings.rows.filter(r=>r[0]==='new').map(r=>r[2]),[
    'gemini-3.5-flash','gemini-3.6-flash','gemini-3.7-flash',
  ]);
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
  assert.equal(logs.rows[0][6],new Date(now).toISOString());
});
test('sync migrates request log project names without losing history',()=>{
  const rows=[log('project-acct1','성공'),log('other','성공')];
  rows[0][3]='acct1'; rows[1][3]='other';
  const logs=sheet(rows);
  const updated=api.syncLogProjects(logs,logs.rows,[{key_label:'acct1',owner:'tester'}]);
  assert.equal(updated,1);
  assert.equal(logs.rows[0][2],'acct1');
  assert.equal(logs.rows[1][2],'other');
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
