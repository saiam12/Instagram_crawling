"use strict";
const $ = (id) => document.getElementById(id);
const token = document.querySelector('meta[name="monitor-token"]').content;
const params = new URLSearchParams(location.search);
const missingFields=["view_count","like_count","comment_count","share_count","repost_count","saved_count"];
function initialFilter(value){if(["metrics-available","metrics-missing"].includes(value))return value;if(value?.startsWith("missing-")){const fields=value.slice(8).split(",");if(fields.length && fields.every(field=>missingFields.includes(field)))return "missing-"+[...new Set(fields)].join(",");}return "all";}
const commandAnchorRequested=["#controls","#commands"].includes(location.hash);
const state = {source:params.get("source")==="refresh"?"refresh":"collector",korean:["true","false","unknown"].includes(params.get("korean"))?params.get("korean"):"all",round:/^(?:[1-9][0-9]*|unknown)$/.test(params.get("round") || "")?params.get("round"):"all",dataset:params.get("dataset") || "",page:Math.max(1,Number(params.get("page")) || 1),size:[20,50,100].includes(Number(params.get("size")))?Number(params.get("size")):20,q:params.get("q") || "",filter:initialFilter(params.get("filter")),date_from:params.get("date_from") || "",date_to:params.get("date_to") || "",live:true,online:false,busy:false,acting:false,rows:[],events:[],pages:1,composing:false};
let polling=false, previewSequence=0, previewTimer, savedAvd="";
let dataController, dataSequence=0, eventSequence=0, pollTimer, searchTimer, toastTimer;
const integer = new Intl.NumberFormat("ko-KR");
const dateFormat = new Intl.DateTimeFormat("ko-KR",{timeZone:"Asia/Seoul",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const longDate = new Intl.DateTimeFormat("ko-KR",{timeZone:"Asia/Seoul",year:"numeric",month:"2-digit",day:"2-digit",hour:"2-digit",minute:"2-digit",second:"2-digit",hour12:false});
const labels = {url:"릴스 주소",username:"작성자",user_id:"사용자 ID",title:"내용",hashtags:"해시태그",collected_at:"수집 시각",uploaded_at:"업로드 시각",collection_number:"수집 차수",view_count:"조회수",like_count:"좋아요",comment_count:"댓글",share_count:"공유",repost_count:"리포스트",saved_count:"저장",follower_count:"팔로워",audio_name:"오디오",video_duration_seconds:"영상 길이(초)",location_name:"위치",reaction_rate:"조회수 / 팔로워",android_status:"Android 처리 결과",ad:"광고 여부",korean:"릴스 구분",korean_source:"분류 근거",account_country:"계정 국가",days_since_previous:"직전 수집과 간격(일)",days_since_upload:"게시 후 경과(일)"};
const element = (tag, className, text) => {const node=document.createElement(tag);if(className)node.className=className;if(text!==undefined)node.textContent=text;return node;};
function date(value, full=false){const parsed=new Date(value);return value && !Number.isNaN(parsed.valueOf())?(full?longDate:dateFormat).format(parsed):"기록 없음";}
function relative(value){if(!value)return "아직 기록 없음";const seconds=Math.max(0,Math.floor((Date.now()-new Date(value).valueOf())/1000));if(!Number.isFinite(seconds))return "기록 없음";if(seconds<60)return `${seconds}초 전`;if(seconds<3600)return `${Math.floor(seconds/60)}분 전`;if(seconds<86400)return `${Math.floor(seconds/3600)}시간 전`;return `${Math.floor(seconds/86400)}일 전`;}
function metric(value){if(value===null || value===undefined || value==="")return "—";if(value==="X")return "비공개";const number=Number(String(value).replaceAll(",",""));return Number.isFinite(number)?integer.format(number):String(value);}
function badge(label,tone="info"){return element("span",`badge ${tone}`,label);}
function notify(message){clearTimeout(toastTimer);$("toast").textContent=message;$("toast").hidden=false;toastTimer=setTimeout(()=>{$("toast").hidden=true;},4500);}
function saveURL(){const next=new URLSearchParams();for(const key of ["source","dataset","q","filter","round","korean","page","size","date_from","date_to"]){if(state[key]!=="")next.set(key,state[key]);}history.replaceState(null,"",`${location.pathname}?${next}${location.hash}`);}
async function api(path,body,signal){const controller=signal?null:new AbortController();const timer=controller?setTimeout(()=>controller.abort(),path==="/api/korean"?90000:30000):null;try{const response=await fetch(path,{cache:"no-store",signal:signal || controller.signal,method:body?"POST":"GET",headers:body?{"Content-Type":"application/json","X-Monitor-Token":token}:{},body:body?JSON.stringify(body):undefined});const data=await response.json();if(!response.ok)throw new Error(data.error || `요청 실패 (${response.status})`);return data;}finally{if(timer)clearTimeout(timer);}}
function processText(info){return info.alive===true?`실행 중 · PID ${info.pid}`:info.alive===false?`종료됨 · PID ${info.pid}`:"확인할 프로세스 없음";}
function durationText(milliseconds){
  if(!Number.isFinite(milliseconds))return "—";
  const seconds=Math.floor(Math.max(0,milliseconds)/1000);
  const hours=Math.floor(seconds/3600), minutes=Math.floor(seconds%3600/60);
  return `${integer.format(hours)}시간 ${minutes}분 ${seconds%60}초`;
}
function renderRuntime(schedule, serverTime, runtime){
  if(state.source==="refresh"){
    runtime=runtime || {};
    document.querySelector(".runtime-panel").hidden=false;
    $("runtime-heading").textContent=runtime.scope==="batch"?"최근 수집 작업 시간":"재수집 실행 시간";
    $("runtime-total-label").textContent="시작 시각";
    $("runtime-remaining-label").textContent="실행 상태";
    $("runtime-total").textContent=date(runtime.started_at);
    const ended=runtime.finished_at || (runtime.alive===true?serverTime:null);
    $("runtime-elapsed").textContent=durationText(Date.parse(ended)-Date.parse(runtime.started_at));
    $("runtime-remaining").textContent=runtime.finished_at?({completed:"종료",completed_with_pending_android:"종료 · 앱 대기",stopped:"중지",failed:"실패"}[runtime.state] || "종료"):runtime.alive===true?"실행 중":runtime.started_at?"종료 시각 미확인":"실행 기록 없음";
    $("schedule").textContent=runtime.scope==="batch"?"이전 실행은 전체 실행 시간 기록이 없어 최근 수집 작업의 시간만 표시합니다.":`시작 ${date(runtime.started_at,true)} · 종료 ${runtime.finished_at?date(runtime.finished_at,true):"미정"} · 대기·재시도 포함`;
    renderRefreshTiming(runtime);
    return;
  }
  document.querySelector(".runtime-panel").hidden=!schedule.started_at && !schedule.ends_at;
  const start=Date.parse(schedule.started_at), end=Date.parse(schedule.ends_at), current=Date.parse(serverTime);
  $("runtime-total").textContent=durationText(end-start);
  $("runtime-elapsed").textContent=durationText(current-start);
  $("runtime-remaining").textContent=durationText(end-current);
}
function renderRefreshTiming(runtime){
  $("refresh-timing").hidden=false;
  const saved=runtime.scope==="run";
  const hours=value=>value<1?`${Number((value*60).toFixed(2))}분마다`:`${value}시간마다`;
  const body=$("refresh-timing-rows");
  body.replaceChildren();
  const add=(age,gap)=>{const row=element("tr");row.append(element("td","",age),element("td","",gap));body.append(row);};
  const basis=runtime.age_basis==="first-collected"?"첫 수집 후 경과 시간":"업로드 후 경과 시간";
  $("refresh-timing-basis").textContent=saved?(runtime.age_intervals?basis:runtime.schedule_basis==="last-collected"?"마지막 수집 기준":"업로드 기준"):"기본 설정 · 업로드 기준";
  if(saved && !runtime.age_intervals){
    add("전체 구간",hours(runtime.interval_hours));
  }else{
    const gaps=saved?runtime.age_intervals:[2,3,6,12];
    ["0~24시간","24~96시간 (4일)","96~120시간 (5일)","120~168시간 (7일)"].forEach((age,index)=>add(age,hours(gaps[index])));
    add("168시간 이후","수집 종료");
  }
  $("refresh-timing-status").textContent=!saved?"실행 설정 기록이 없어 기본 간격을 표시합니다.":runtime.test_mode?"테스트 모드 · 릴스당 추가 3회":runtime.schedule_basis==="last-collected"?"다음 수집은 마지막 수집 시각 기준":"다음 수집은 업로드 시각 기준";
}
function renderAndroidAlert(component){
  const health=component.device_health;
  const confirmed=health && health.consecutive_failures>=2 && ["disconnected","input_blocked","app_error"].includes(health.state);
  const message=confirmed?(component.alert || ""):"";
  const panel=document.querySelector(".android-panel");
  panel.classList.toggle("android-interrupted",Boolean(message));
  const notice=$("android-alert");
  if(notice.textContent!==message)notice.textContent=message;
  notice.hidden=!message;
  if(message){$("android-status").textContent="보강 중단 / 확인 필요";$("android-status").className="badge danger";}
}
function renderRounds(rounds){
  const selector=$("round-filter");
  const options=[{value:"all",label:"전체 회차"},...(rounds || []).map(record=>({value:record.round===null?"unknown":String(record.round),label:record.round===null?"회차 미지정":`${record.round}차 수집`}))];
  if(state.round!=="all" && !options.some(option=>option.value===state.round))options.push({value:state.round,label:state.round==="unknown"?"회차 미지정":`${state.round}차 수집`});
  if(Array.from(selector.options).map(option=>option.value).join("|")!==options.map(option=>option.value).join("|"))selector.replaceChildren(...options.map(item=>{const option=element("option","",item.label);option.value=item.value;return option;}));
  selector.value=state.round;
  const list=$("round-counts");
  if(!rounds?.length){const item=element("div");item.append(element("dt","","수집 회차"),element("dd","","아직 수집 기록 없음"));list.replaceChildren(item);return;}
  list.replaceChildren(...rounds.map(record=>{const item=element("div");item.append(element("dt","",record.round===null?"회차 미지정":`${record.round}차 수집`),element("dd","",`${metric(record.count)}건`));return item;}));
}
function chart(id,values,maximum,times){
  const clock=new Intl.DateTimeFormat("ko-KR",{timeZone:"Asia/Seoul",hour:"2-digit",minute:"2-digit",hour12:false});
  const timeLabel=seconds=>clock.format(new Date(seconds*1000));
  const step=maximum/2;
  const axis=element("div","chart-axis");
  axis.setAttribute("aria-hidden","true");
  axis.append(...[maximum,step,0].map(value=>element("span","",integer.format(value))));
  const bars=element("div","chart-bars");
  bars.setAttribute("aria-hidden","true");
  bars.append(...values.map((count,index)=>{
    const bar=element("span",count?"":"zero");
    bar.style.height=`${Math.min(count/maximum,1)*100}%`;
    bar.title=`${timeLabel(times[index])}~${timeLabel(times[index]+300)}: ${integer.format(count)}건${index===values.length-1?" (집계 중)":""}`;
    return bar;
  }));
  const unit=element("div","chart-unit","건 / 5분");
  const ticks=element("div","chart-times");
  ticks.setAttribute("aria-hidden","true");
  ticks.append(...times.map((start,index)=>element("span","",index%3===0 || index===times.length-1?timeLabel(start):"")));
  $(id).setAttribute("aria-label",`${id.startsWith("web")?"웹":"Android"} 고정 5분 구간 처리량: ${values.map((value,index)=>`${timeLabel(times[index])}~${timeLabel(times[index]+300)} ${value}건${index===values.length-1?" 집계 중":""}`).join(", ")}. Y축: 0, ${step}, ${maximum}건.`);
  $(id).replaceChildren(unit,axis,bars,ticks);
}
function renderSummary(data){state.busy=data.collector_busy;const selector=$("dataset");const old=Array.from(selector.options).map(option=>option.value).join("|");const keys=data.datasets.map(item=>item.id).join("|");if(old!==keys){selector.replaceChildren(...data.datasets.map(item=>{const option=element("option","",`${item.label} · ${item.id.split('/')[0]}`);option.value=item.id;return option;}));}if(!data.datasets.some(item=>item.id===state.dataset)){state.dataset=data.datasets[0]?.id || "";state.page=1;saveURL();}selector.value=state.dataset;selector.disabled=!data.datasets.length;$("sync-time").textContent=`${date(data.updated_at)} · 한국 시간`;const selected=data.datasets.find(item=>item.id===state.dataset);if(!selected){$("notice").textContent="수집 데이터가 아직 없습니다. 명령어 안내를 참고해 터미널에서 수집을 시작하면 여기에 표시됩니다.";$("notice").className="";return;}$("notice").textContent=data.error || selected.warning || "";$("notice").className=data.error || selected.warning?"error":"";for(const [key,value] of [["recollection-pending",selected.schedule.due_recollections],["snapshots",selected.summary.snapshots],["last-hour",selected.summary.last_hour],["pending",selected.summary.queue.pending]])$(key).textContent=metric(value);$("recollection-pending").title=`재수집 예정 시각이 지났으나 아직 저장되지 않은 릴스입니다. 진행 중인 미저장 항목이 포함될 수 있습니다. 집계 ${date(selected.schedule.updated_at,true)}`;renderRounds(selected.summary.rounds);$("queue-detail").textContent=`처리 중 ${metric(selected.summary.queue.working)} · 결과 반영 대기 ${metric(selected.summary.queue.completed)}`;for(const source of ["web","android"]){const component=selected[source];$(source+"-status").textContent=component.label;$(source+"-status").className=`badge ${component.tone}`;$(source+"-process").textContent=processText(component.process);$(source+"-success").textContent=relative(component.last_success);$(source+"-success").title=date(component.last_success,true);$(source+"-error").textContent=component.error;$(source+"-error").hidden=!component.error;chart(source+"-chart",component.bins,source==="web"?40:70,selected.chart_times);}renderAndroidAlert(selected.android);$("web-response").textContent=relative(new Date(selected.web.last_activity || 0)>new Date(selected.web.updated_at || 0)?selected.web.last_activity:selected.web.updated_at);$("android-processed").textContent=`${metric(selected.android.processed)}건 (전체)`;renderRuntime(selected.schedule,data.server_time,selected.refresh?.runtime);document.querySelector(".session-new").hidden=state.source==="refresh" || selected.summary.session_new_count==null;$("session-new-count").textContent=selected.summary.session_new_count==null?"—":metric(selected.summary.session_new_count)+"건";if(state.source!=="refresh")$("schedule").textContent=`시작 ${date(selected.schedule.started_at,true)}   /   종료 예정 ${date(selected.schedule.ends_at,true)}   /   신규 탐색 종료 ${date(selected.schedule.discovery_ends_at)}`;}
function renderRuns(jobs){if($("runs").contains(document.activeElement) || $("dialog").open)return;const open=new Set(Array.from($("runs").querySelectorAll("details[open]")).map(node=>node.dataset.runid));if(!jobs.length){$("runs").replaceChildren(element("p","empty",state.busy?"터미널에서 실행 중입니다.":"실행 이력이 없습니다."));return;}const names={queued:"실행 요청",starting:"환경 준비 중",running:"실행 중",stopping:"저장 후 종료 대기",stopped:"사용자 중지",completed:"완료",failed:"실행 실패",exit_unknown:"종료 감지 · 종료 코드 없음"};const nodes=jobs.map(job=>{const item=element("article","run-item");const top=element("div","run-top");const text=element("div");text.append(element("div","run-title",job.kind==="android"?"Android 준비":`${{"fashion":"패션","beauty":"뷰티","fashion-beauty":"패션 + 뷰티"}[job.command]} · ${job.mode==="web"?"웹":"웹 + Android"}`),element("div","run-meta",`${date(job.created_at)} · ${job.id.slice(0,8)}${job.exit_code!==undefined?` · 종료 코드 ${job.exit_code}`:""}${job.finished_at?` · 종료 ${date(job.finished_at)}`:""}`));const actions=element("div","run-actions");actions.append(badge(names[job.state] || job.state,job.state==="failed"?"danger":job.state==="running"?"good":"info"));if(job.kind==="collector" && ["starting","running","stopping"].includes(job.state)){const stop=element("button","button secondary compact",job.state==="stopping"?"종료 대기":"수집 중지");stop.type="button";stop.disabled=job.state==="stopping" || job.alive!==true;stop.addEventListener("click",()=>stopJob(job));actions.append(stop);}top.append(text,actions);item.append(top);if(job.error)item.append(element("p","inline-error",job.error));const details=element("details");details.dataset.runid=job.id;details.open=open.has(job.id);details.append(element("summary","","실행 로그 보기"),element("pre","",job.output || "출력을 기다리고 있습니다."));item.append(details);return item;});$("runs").replaceChildren(...nodes);}
function updateRange(data){state.page=data.page;state.pages=data.pages;$("first").disabled=$("previous").disabled=data.page<=1;$("last").disabled=$("next").disabled=data.page>=data.pages;$("page-label").textContent=`${data.page} / ${integer.format(data.pages)}`;const first=data.total?(data.page-1)*state.size+1:0;const last=Math.min(data.page*state.size,data.total);$("data-range").textContent=`${integer.format(first)}–${integer.format(last)} / ${integer.format(data.total)}개 기록`;}
async function loadData(){if(dataController)dataController.abort();dataController=new AbortController();const sequence=++dataSequence;const query=new URLSearchParams({source:state.source,dataset:state.dataset,q:state.q,filter:state.filter,round:state.round,korean:state.korean,page:state.page,size:state.size,date_from:state.date_from,date_to:state.date_to});try{const data=await api("/api/data?"+query,undefined,dataController.signal);if(sequence!==dataSequence)return;state.rows=data.items;updateRange(data);for(const button of document.querySelectorAll("[data-missing]")){button.querySelector("strong").textContent=integer.format(data.missing_counts?.[button.dataset.missing] ?? 0);button.setAttribute("aria-pressed",String(state.filter.startsWith("missing-") && state.filter.slice(8).split(",").includes(button.dataset.missing)));}saveURL();$("data-feedback").textContent=data.warning || (state.live?"":"자동 갱신 일시정지");if(!data.items.length){const row=element("tr");const cell=element("td","empty",state.q || state.date_from || state.date_to || state.filter!=="all" || state.round!=="all" || state.korean!=="all"?"조건에 맞는 기록이 없습니다. 검색어와 필터를 변경해 보세요.":"아직 수집된 기록이 없습니다. 수집이 진행되면 자동으로 표시됩니다.");cell.colSpan=9;row.append(cell);$("data-rows").replaceChildren(row);return;}const rows=data.items.map(record=>{const row=element("tr");const title=element("td");const button=element("button","record-title",record.title || record.url || "릴스 상세");button.type="button";button.addEventListener("click",()=>showRecord(record));title.append(button,element("span","record-meta",`@${record.username || "작성자 미확인"} · ${record.collection_number || "?"}차 · ${window.CollectorKorean.label(record)}`));row.append(title,element("td","time-cell",date(record.collected_at)));for(const name of missingFields)row.append(element("td","numeric",metric(record[name])));const available=element("td");const missing=Object.entries({view_count:"조회수",like_count:"좋아요",comment_count:"댓글",share_count:"공유",repost_count:"리포스트",saved_count:"저장"}).filter(([key])=>record[key]==null || record[key]==="").map(([,label])=>label);available.append(badge(missing.length===missingFields.length?"모든 지표 누락":missing.length?missing.join(" · "):"수집 완료",missing.length?"info":"good"));row.append(available);return row;});$("data-rows").replaceChildren(...rows);}catch(error){if(error.name!=="AbortError")$("data-feedback").textContent=`데이터를 갱신하지 못했습니다. 직전 기록을 유지합니다. ${error.message}`;}}
const eventNames={worker_ui_unreadable:"Android 화면 읽기 재시도",collector_started:"웹 수집 작업 시작",emulator_restart_requested:"정기 에뮬레이터 재부팅",scheduled_app_restart_started:"예약 앱 재시작",scheduled_app_restart_completed:"예약 앱 재시작 완료",recollection_android_jobs_preserved:"재수집 대기 작업 보존",recollection_android_wait_deferred:"Android 대기 후 웹 수집 계속",worker_started:"Android 워커 시작",worker_stopped:"Android 워커 종료",worker_launch_requested:"Android 워커 실행 요청",device_recovery_requested:"Android 연결 복구 시도",device_recovery_failed:"Android 연결 복구 실패",scheduled_app_restart_expired:"지난 앱 재시작 예약 정리",collection_stop_requested:"사용자 중지 요청",reel_metric_attempt_failed:"릴스 지표 수집 실패",reel_metric_retry:"릴스 지표 재시도"};
const eventPageSize=10;
let eventPage=1;
function renderEvents(){const expanded=new Set(Array.from($("event-list").querySelectorAll("details[open]")).map(node=>node.dataset.eventId));const selected=state.events.filter(event=>$("event-filter").value!=="problems" || ["warning","danger"].includes(event.tone));const pages=Math.max(1,Math.ceil(selected.length/eventPageSize));eventPage=Math.min(eventPage,pages);$("event-page-label").textContent=`${eventPage} / ${pages}`;$("event-previous").disabled=eventPage===1;$("event-next").disabled=eventPage===pages;const start=(eventPage-1)*eventPageSize;if(!selected.length){$("event-list").replaceChildren(element("li","empty","표시할 이력이 없습니다."));return;}$("event-list").replaceChildren(...selected.slice(start,start+eventPageSize).map(event=>{const item=element("li",`event-item ${event.tone}`);const at=element("time","",date(event.at));at.dateTime=event.at;at.title=date(event.at,true);const source=element("span","event-source",{python:"웹",web:"웹",android:"Android",monitor:"모니터"}[event.source] || event.source);const body=element("div");const detail=element("details");detail.dataset.eventId=`${event.source}:${event.at}:${event.message}`;detail.open=expanded.has(detail.dataset.eventId);const summary=element("summary","",eventNames[event.event] || event.event);detail.append(summary,element("p","",event.message));body.append(detail);if(event.event==="상태 감지")at.title="모니터가 상태 변경을 감지한 시각";item.append(at,source,body);return item;}));}
async function loadEvents(){const sequence=++eventSequence;try{const data=await api("/api/events?"+new URLSearchParams({source:state.source,dataset:state.dataset}));if(sequence===eventSequence){state.events=data.items;renderEvents();}}catch(error){if(!state.events.length)$("event-list").replaceChildren(element("li","empty",`이력을 불러오지 못했습니다. ${error.message}`));}}
async function poll(manual=false){if(manual===true)$("refresh").disabled=true;if(polling)return;polling=true;const overviewKorean=state.korean;clearTimeout(pollTimer);try{const data=await api("/api/overview?source="+state.source+"&korean="+encodeURIComponent(overviewKorean));if(overviewKorean!==state.korean)return;state.online=true;$("connection").textContent="실시간 연결";$("connection").className="connection";renderSummary(data);renderRuns(data.jobs);const interacting=$("dialog").open || $("data-rows").contains(document.activeElement);await Promise.all([state.live && !interacting?loadData():Promise.resolve(),loadEvents()]);}catch(error){state.online=false;$("connection").textContent="연결 끊김";$("connection").className="connection offline";$("notice").textContent="모니터와 연결이 끊겼습니다. 마지막 화면을 유지하며 자동으로 다시 연결합니다. start.ps1 실행 상태를 확인하세요.";$("notice").className="error";}finally{polling=false;buttons();$("refresh").disabled=false;pollTimer=setTimeout(poll,overviewKorean!==state.korean?0:document.hidden?10000:3000);}}
function safeLink(value){try{const url=new URL(value);return url.protocol==="https:" && (url.hostname==="instagram.com" || url.hostname.endsWith(".instagram.com"))?url.href:null;}catch{return null;}}
function showRecord(record){
  $("dialog-title").textContent=`@${record.username || "작성자 미확인"} · 수집 기록`;
  const body=$("dialog-body"), dataset=state.dataset;
  body.replaceChildren();
  const url=safeLink(record.url);
  if(url){const link=element("a","","Instagram에서 릴스 열기 ↗");link.href=url;link.target="_blank";link.rel="noopener noreferrer";body.append(link);}
  body.append(element("p","record-caption",record.title || "본문 없음"));
  const editor=element("div","record-classification"), field=element("div","field"), label=element("label","","릴스 구분"), select=element("select"), save=element("button","button primary compact","구분 저장"), status=element("p","classification-status");
  select.id="record-korean"; label.htmlFor=select.id; select.setAttribute("aria-describedby","record-korean-status");
  status.id="record-korean-status";status.setAttribute("role","status");status.textContent="같은 릴스의 모든 수집 회차에 적용됩니다. 미확인은 -로 표시됩니다.";
  for(const [value,text] of [["true","한국"],["false","외국"],["unknown","미확인 (-)"]]){const option=element("option","",text);option.value=value;select.append(option);}
  const current=window.CollectorKorean.value(record);select.value=current===null?"unknown":String(current);save.type="button";
  field.append(label,select);editor.append(field,save,status);body.append(editor);
  const details=element("dl","record-details");let classification;
  for(const [key,value] of Object.entries(record)){if(["title","android_available","android_mismatch"].includes(key))continue;const rendered=element("dd","",key==="account_country"?String(value || ""):key==="korean"?window.CollectorKorean.label(record):key.endsWith("_at")?date(value,true):value===null || value===""?"—":String(value));if(key==="korean")classification=rendered;details.append(element("dt","",labels[key] || (key.endsWith("_change")?`${labels[key.slice(0,-7)] || key} 변화`:key)),rendered);}
  save.addEventListener("click",async()=>{
    if(save.disabled)return;
    select.disabled=save.disabled=true;editor.setAttribute("aria-busy","true");status.classList.remove("field-error");status.textContent="릴스 구분을 저장하고 있습니다…";
    try{
      const result=await api("/api/korean",{dataset,url:record.url,korean:select.value==="unknown"?null:select.value==="true"});
      record.korean=result.korean;record.korean_source="manual";
      if(classification)classification.textContent=window.CollectorKorean.label(record);
      status.textContent=result.exported===false?"구분을 저장했습니다. 사용 중인 내보내기 파일은 다음 저장 때 갱신됩니다.":result.message;
      notify(status.textContent);await loadData();await poll();
    }catch(error){status.classList.add("field-error");status.textContent=`${error.message} 연결이 끊겼다면 새로고침으로 저장 상태를 확인한 뒤 다시 저장하세요.`;}
    finally{select.disabled=save.disabled=false;editor.setAttribute("aria-busy","false");}
  });
  body.append(details);$("dialog-actions").replaceChildren();$("dialog").showModal();
}
function confirmAction(title,message,label,danger=false){$("dialog-title").textContent=title;$("dialog-body").replaceChildren(element("p","dialog-copy",message));const cancel=element("button","button secondary","취소");const proceed=element("button",`button ${danger?"danger":"primary"}`,label);cancel.type=proceed.type="button";$("dialog-actions").replaceChildren(cancel,proceed);return new Promise(resolve=>{let accepted=false;proceed.addEventListener("click",()=>{accepted=true;$("dialog").close();});cancel.addEventListener("click",()=>$("dialog").close());$("dialog").addEventListener("close",()=>resolve(accepted),{once:true});$("dialog").showModal();cancel.focus();});}
function buttons(){
  $("start-collector").disabled=!state.online || state.busy || state.acting;
  $("prepare-android").disabled=!state.online || state.acting;
  $("busy-message").hidden=!state.busy;
  $("run-form").setAttribute("aria-busy",String(state.acting));
}
function formPayload(){return Object.fromEntries(new FormData($("run-form")));}
function validate(){
  let first=null;
  for(const input of $("run-form").querySelectorAll("input")){
    input.removeAttribute("aria-invalid");
    $(input.id+"-error")?.remove();
    input.removeAttribute("aria-describedby");
    let message="";
    if(input.type==="number"){
      const value=Number(input.value);
      if(!input.value.trim() || !Number.isFinite(value))message="숫자를 입력하세요.";
      else if(value<Number(input.min) || value>Number(input.max))message=`${input.min}~${input.max} 범위로 입력하세요.`;
      else if(input.step==="1" && !Number.isInteger(value))message="정수로 입력하세요.";
    }
    if(input.name==="discovery" && Number(input.value)+(Number($("run-snapshots").value)-1)*Number($("run-interval").value)/60>Number($("run-duration").value))message="전체 실행 시간 안에 신규 탐색과 목표 횟수의 재수집이 끝나도록 시간을 조정하세요.";
    if(input.name==="data_dir" && !input.value.trim())message="저장 폴더를 입력하세요.";
    if(message){input.setAttribute("aria-invalid","true");const help=element("small","field-error",message);help.id=input.id+"-error";input.after(help);input.setAttribute("aria-describedby",help.id);first??=input;}
  }
  if(first){first.focus();$("form-message").textContent="표시한 입력 항목을 확인하세요.";return false;}
  $("form-message").textContent="";
  return true;
}
async function preview(){
  const sequence=++previewSequence;
  try{const data=await api("/api/preview",formPayload());if(sequence===previewSequence)$("command-preview").textContent=data.command;}
  catch(error){if(sequence===previewSequence)$("command-preview").textContent=error.message;}
  try{localStorage.setItem("collector-monitor-options",JSON.stringify(formPayload()));}catch{}
}
async function loadEnvironment(){
  $("refresh-devices").disabled=true;$("device-status").textContent="기기 정보를 확인하는 중입니다.";
  try{
    const data=await api("/api/environment"), chosen=$("avd").value || savedAvd;savedAvd="";
    $("avd").replaceChildren(element("option","","연결 기기 / 유일한 AVD 자동 선택"));$("avd").firstChild.value="";
    for(const name of data.avds){const option=element("option","",name);option.value=name;$("avd").append(option);}
    $("avd").value=data.avds.includes(chosen)?chosen:"";
    $("device-list").replaceChildren(...data.devices.map(serial=>{const option=element("option");option.value=serial;return option;}));
    $("device-status").textContent=data.error || (data.adb_available?`온라인 기기 ${data.devices.length}개 · 설치된 AVD ${data.avds.length}개`:"ADB를 찾지 못했습니다. SDK 설치 경로를 확인하세요.");preview();
  }catch(error){$("device-status").textContent=`기기 정보를 불러오지 못했습니다. ${error.message}`;}
  finally{$("refresh-devices").disabled=false;}
}
async function act(kind){
  if(state.acting || !state.online || (kind==="start" && (state.busy || !validate())))return;
  const payload=formPayload(), start=kind==="start";
  const message=start?`${{"fashion":"패션","beauty":"뷰티","fashion-beauty":"패션 + 뷰티"}[payload.command]} · ${payload.mode==="web"?"웹 전용":"웹 + Android"}\n전체 ${payload.duration}시간 / 신규 탐색 ${payload.discovery}시간\n저장 폴더: ${payload.data_dir}\n저장된 로그인으로 백그라운드 실행합니다.`:"연결된 기기를 사용하거나 선택한 에뮬레이터를 시작합니다. Instagram 설치와 로그인은 직접 완료하세요.";
  if(!await confirmAction(start?"수집을 시작할까요?":"Android를 준비할까요?",message,start?"수집 시작":"Android 준비"))return;
  if(state.acting || !state.online || (start && state.busy))return;
  state.acting=true;buttons();$("form-message").textContent="실행 요청을 보내고 있습니다.";
  try{const data=await api(start?"/api/start":"/api/android",payload);$("form-message").textContent="";notify(data.message);await poll();}
  catch(error){$("form-message").textContent=`${error.message} 연결이 끊겼다면 아래 실행 작업을 확인한 뒤 다시 시도하세요.`;}
  finally{state.acting=false;buttons();}
}
$("run-form").addEventListener("submit",event=>{event.preventDefault();act("start");});
$("run-form").addEventListener("input",()=>{clearTimeout(previewTimer);previewTimer=setTimeout(preview,300);});
$("run-form").addEventListener("change",()=>{clearTimeout(previewTimer);preview();});
$("prepare-android").addEventListener("click",()=>act("android"));
$("refresh-devices").addEventListener("click",loadEnvironment);
try{const saved=JSON.parse(localStorage.getItem("collector-monitor-options") || "{}");savedAvd=saved.avd || "";for(const input of $("run-form").elements)if(input.name && input.name!=="avd" && saved[input.name]!==undefined)input.value=saved[input.name];}catch{}
if(state.source==="collector"){preview();loadEnvironment();}
async function stopJob(job){if(!await confirmAction("수집을 중지할까요?",`실행 ${job.id.slice(0,8)}의 신규 작업과 Android 큐 처리를 중지하고 현재 저장을 마무리합니다.\n수집 데이터와 대기 큐는 삭제하지 않습니다.\n다른 터미널에서 시작한 수집기는 중지하지 않습니다.`,"수집 중지",true))return;try{const data=await api("/api/stop",{id:job.id});notify(data.message);await poll();}catch(error){notify(error.message);}}
$("dialog-close").addEventListener("click",()=>$("dialog").close());
$("refresh").addEventListener("click",()=>{if(!$("refresh").disabled)poll(true);});
$("dataset").addEventListener("change",()=>{state.dataset=$("dataset").value;state.page=1;saveURL();poll();});
$("live-toggle").addEventListener("click",()=>{state.live=!state.live;$("live-toggle").textContent=state.live?"데이터 갱신 일시정지":"데이터 갱신 재개";$("live-toggle").setAttribute("aria-pressed",String(!state.live));$("data-feedback").textContent=state.live?"":"자동 갱신 일시정지";if(state.live)loadData();});
function searchChanged(){if(state.composing)return;clearTimeout(searchTimer);$("clear-search").hidden=!$("search").value;searchTimer=setTimeout(()=>{state.q=$("search").value.trim();state.page=1;saveURL();loadData();},300);}
$("search").addEventListener("input",searchChanged);$("search").addEventListener("compositionstart",()=>{state.composing=true;clearTimeout(searchTimer);});$("search").addEventListener("compositionend",()=>{state.composing=false;searchChanged();});$("search").addEventListener("keydown",event=>{if(event.key==="Enter" && !event.isComposing){clearTimeout(searchTimer);state.q=$("search").value.trim();state.page=1;saveURL();loadData();}});
$("clear-search").addEventListener("click",()=>{clearTimeout(searchTimer);$("search").value="";state.q="";state.page=1;$("clear-search").hidden=true;saveURL();loadData();$("search").focus();});
function changeKorean(value){state.korean=value;state.page=1;$("korean-filter").value=$("data-korean").value=value;saveURL();loadData();poll();}
$("korean-filter").value=state.korean;
$("data-korean").value=state.korean;
$("korean-filter").addEventListener("change",()=>changeKorean($("korean-filter").value));
$("data-korean").addEventListener("change",()=>changeKorean($("data-korean").value));
$("round-filter").addEventListener("change",()=>{state.round=$("round-filter").value;state.page=1;saveURL();loadData();});
for(const [id,key] of [["date-from","date_from"],["date-to","date_to"]]){
  const input=$(id);
  input.value=state[key];
  input.addEventListener("change",()=>{
    state[key]=input.value;
    if(state.date_from && state.date_to && state.date_from>state.date_to){
      const [otherId,otherKey]=key==="date_from"?["date-to","date_to"]:["date-from","date_from"];
      state[otherKey]=input.value;
      $(otherId).value=input.value;
    }
    state.page=1;
    saveURL();
    loadData();
  });
}
$("data-filter").addEventListener("change",()=>{state.filter=$("data-filter").value;state.page=1;saveURL();loadData();});$("page-size").addEventListener("change",()=>{state.size=Number($("page-size").value);state.page=1;saveURL();loadData();});
document.querySelectorAll("[data-missing]").forEach(button=>button.addEventListener("click",()=>{const selected=new Set(state.filter.startsWith("missing-")?state.filter.slice(8).split(","):[]);if(selected.has(button.dataset.missing))selected.delete(button.dataset.missing);else selected.add(button.dataset.missing);state.filter=selected.size?"missing-"+missingFields.filter(field=>selected.has(field)).join(","):"all";button.setAttribute("aria-pressed",String(selected.has(button.dataset.missing)));$("data-filter").value="all";state.page=1;saveURL();loadData();}));
$("first").addEventListener("click",()=>{if(state.page>1){state.page=1;saveURL();loadData();}});$("last").addEventListener("click",()=>{if(state.page<state.pages){state.page=state.pages;saveURL();loadData();}});
$("previous").addEventListener("click",()=>{if(state.page>1){state.page--;saveURL();loadData();}});$("next").addEventListener("click",()=>{if(state.page<state.pages){state.page++;saveURL();loadData();}});$("event-filter").addEventListener("change",()=>{eventPage=1;renderEvents();});
$("event-previous").addEventListener("click",()=>{eventPage--;renderEvents();});
$("event-next").addEventListener("click",()=>{eventPage++;renderEvents();});
for(const button of document.querySelectorAll("[data-copy-command]")){button.disabled=false;button.addEventListener("click",async()=>{try{await navigator.clipboard.writeText($(button.dataset.copyCommand).textContent);notify("명령어를 복사했습니다. collectors 폴더의 PowerShell에서 실행하세요.");}catch{notify("복사 권한을 확인하거나 표시된 명령어를 직접 선택해 복사하세요.");}});}
function updateNavigation(){for(const link of document.querySelectorAll(".nav-link")){if(link.hasAttribute("data-overview-nav"))link.setAttribute("aria-current","page");else link.removeAttribute("aria-current");}}window.addEventListener("hashchange",updateNavigation);document.addEventListener("visibilitychange",()=>{if(!document.hidden)poll();});
$("search").value=state.q;$("clear-search").hidden=!state.q;$("data-filter").value=state.filter.startsWith("missing-")?"all":state.filter;$("page-size").value=String(state.size);for(const id of ["refresh","live-toggle","clear-search","dialog-close"])$(id).disabled=false;if(location.hash==="#controls")history.replaceState(null,"",location.pathname+location.search+"#commands");updateNavigation();poll().then(()=>{if(commandAnchorRequested && location.hash==="#commands")$("commands").scrollIntoView({behavior:"instant",block:"start"});});

// Shared, persisted panel split; changes only presentation, never collection state.
for(const id of ["data-history-layout","management-layout"]){
  const panel=$(id), divider=panel.querySelector(".panel-divider");
  const initial=id==="management-layout"?50:65;
  let percent=initial;
  const apply=value=>{percent=Math.max(30,Math.min(75,value));panel.style.setProperty("--split-left",`${percent}fr`);panel.style.setProperty("--split-right",`${100-percent}fr`);divider.setAttribute("aria-valuenow",String(Math.round(percent)));divider.setAttribute("aria-valuetext",`왼쪽 ${Math.round(percent)}%, 오른쪽 ${Math.round(100-percent)}%`);};
  const save=()=>{try{localStorage.setItem(`collector-monitor-split-${id}`,String(percent));}catch{}};
  try{const saved=localStorage.getItem(`collector-monitor-split-${id}`);if(saved!==null && Number.isFinite(Number(saved)))percent=Number(saved);}catch{}
  apply(percent);
  divider.addEventListener("pointerdown",event=>{if(event.button!==0)return;event.preventDefault();divider.focus();divider.setPointerCapture(event.pointerId);panel.classList.add("resizing");});
  divider.addEventListener("pointermove",event=>{if(!divider.hasPointerCapture(event.pointerId))return;const rect=panel.getBoundingClientRect();apply((event.clientX-rect.left-7)/(rect.width-14)*100);});
  const finish=event=>{if(!divider.hasPointerCapture(event.pointerId))return;divider.releasePointerCapture(event.pointerId);panel.classList.remove("resizing");save();};
  divider.addEventListener("pointerup",finish);divider.addEventListener("pointercancel",finish);
  divider.addEventListener("lostpointercapture",()=>panel.classList.remove("resizing"));
  divider.addEventListener("keydown",event=>{const next={ArrowLeft:percent-2,ArrowRight:percent+2,Home:30,End:75}[event.key];if(next===undefined)return;event.preventDefault();apply(next);save();});
  divider.addEventListener("dblclick",()=>{apply(initial);save();});
}

function configureSourceView(){
  const refresh=state.source==="refresh";
  document.body.classList.toggle("refresh-view",refresh);
  $("collector-tab").toggleAttribute("aria-current",!refresh);
  $("refresh-tab").toggleAttribute("aria-current",refresh);
  (refresh?$("refresh-tab"):$("collector-tab")).setAttribute("aria-current","page");
  $("refresh-guidance").hidden=!refresh;
  $("management-layout").hidden=refresh;
  updateNavigation();
}
configureSourceView();
