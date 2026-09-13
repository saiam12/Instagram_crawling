// Google Apps Script V8. 모든 클라이언트는 이 배포 한 곳만 사용해야 한다.
// API 키는 받지 않는다. POOL_TOKEN은 스크립트 속성에 별도로 설정한다.
const SHEET_ID = '1g9sq7tuoiz-WE1OZNbN48M_KA0FY7MXgssE1_ev_-O8';
const FIRST_ROW = 6;
const LOG_WIDTH = 17;
const BUSY = new Set(['후보 예약', '사용중']);
const COUNTED = new Set(['사용중', '성공', '실패', '결과 미확인']);
const FLASH_MODELS = new Set(['gemini-3.5-flash', 'gemini-3.6-flash', 'gemini-3.7-flash', 'gemini-3.8-flash']);

function dayAt(now) {
  return Utilities.formatDate(new Date(now), 'America/Los_Angeles', 'yyyy-MM-dd');
}
function groupKey(project, model) { return JSON.stringify([project, model]); }
function clean(value) {
  const s = String(value == null ? '' : value);
  if (s.length > 200 || /^[=+@\-]/.test(s)) throw new Error('별칭/사용자 값이 올바르지 않습니다.');
  return s;
}
function readRows(sheet, width) {
  const count = sheet.getLastRow() - FIRST_ROW + 1;
  if (count <= 0) return [];
  const range = sheet.getRange(FIRST_ROW, 1, count, width);
  const rows = range.getValues();
  // 요청 기록의 집계일은 시트 시간대에서 보이는 날짜 문자열을 기준으로 삼는다.
  if (width >= 15) {
    const displayed = range.getDisplayValues();
    rows.forEach((row, i) => { if (row[14] instanceof Date) row[14] = displayed[i][14]; });
  }
  return rows;
}
function settingsFrom(rows) {
  const config = [], labels = new Map(), groups = new Map(), seen = new Set();
  rows.forEach(r => {
    if (!r.slice(0, 8).some(v => v !== '')) return;
    if (r[6] !== '사용') return;
    const [project, label, , model, rpm, rpd] = r;
    if (!FLASH_MODELS.has(model)) throw new Error('3.5/3.6/3.7/3.8 Flash만 사용 가능합니다. Lite는 제외하세요.');
    if (![project, label, model].every(v => typeof v === 'string' && v.trim()) ||
        !Number.isInteger(rpm) || rpm < 0 || !Number.isInteger(rpd) || rpd < 0)
      throw new Error('사용 설정 행에는 별칭/모델과 정수 RPM/RPD를 입력하세요.');
    [project, label, model].forEach(clean);
    if (labels.has(label) && labels.get(label) !== project) throw new Error('같은 키 별칭이 여러 프로젝트에 연결되었습니다.');
    labels.set(label, project);
    const key = groupKey(project, model), pair = groupKey(label, model);
    if (seen.has(pair)) throw new Error('키 별칭 + 모델 설정이 중복되었습니다.');
    seen.add(pair);
    const baselineDay = r[8] instanceof Date ? Utilities.formatDate(r[8], 'America/Los_Angeles', 'yyyy-MM-dd') : (r[8] || '');
    const baseline = r[9] === '' || r[9] == null ? 0 : r[9];
    if (!Number.isInteger(baseline) || baseline < 0 || (baseline > 0 && !/^\d{4}-\d{2}-\d{2}$/.test(baselineDay)))
      throw new Error('초기 사용량은 0 이상 정수, 초기 집계일은 YYYY-MM-DD로 입력하세요.');
    if (groups.has(key) && (groups.get(key).rpm !== rpm || groups.get(key).rpd !== rpd ||
        groups.get(key).baseline !== baseline || groups.get(key).baselineDay !== baselineDay))
      throw new Error('같은 프로젝트 + 모델의 RPM/RPD가 서로 다릅니다.');
    const c = {project, key_label: label, model, rpm, rpd, baseline, baselineDay};
    config.push(c); groups.set(key, c);
  });
  return config;
}
function statsFor(c, logs, now) {
  const day = dayAt(now);
  const rows = logs.filter(r => r[2] === c.project && r[4] === c.model);
  // Sheets가 YYYY-MM-DD 문자열을 Date로 자동 변환해도 같은 태평양시 날짜로 집계한다.
  const today = rows.filter(r => (r[14] instanceof Date ? dayAt(r[14]) : String(r[14])) === day);
  return {
    count: today.filter(r => COUNTED.has(r[7])).length + (c.baselineDay === day ? c.baseline : 0),
    // 날짜가 바뀌어도 실제 진행 중인 분석은 계속 사용중이다.
    busy: rows.some(r => BUSY.has(r[7])),
    daily: today.some(r => r[12] === '일일'),
    retry: Math.max(0, ...rows.map(r => Date.parse(r[13]) || 0)),
    rpm: rows.filter(r => COUNTED.has(r[7]) && Date.parse(r[5]) > now - 60000).length,
  };
}
function sampleCandidates(config, logs, offered, now, random) {
  const allowed = new Set(offered.map(c => groupKey(c.key_label, c.model)));
  const unique = new Map();
  config.forEach(c => {
    if (!allowed.has(groupKey(c.key_label, c.model))) return;
    const s = statsFor(c, logs, now);
    if (!s.busy && !s.daily && s.count < c.rpd && s.rpm < c.rpm && s.retry <= now)
      if (!unique.has(groupKey(c.project, c.model))) unique.set(groupKey(c.project, c.model),
        {...c, day_count: s.count, remaining: c.rpd - s.count});
  });
  const list = [...unique.values()], sample = [];
  while (list.length && sample.length < 2) sample.push(list.splice(Math.floor(random() * list.length), 1)[0]);
  return sample;
}
function selectRows(rows) {
  // 잔여량이 많은 후보를 고르고, 동률이면 높은 Flash 모델을 쓴다.
  const modelVersion = row => Number(String(row[4]).match(/^gemini-(\d+(?:\.\d+)?)-flash$/)?.[1] || 0);
  const remaining = row => row[16] === '' || row[16] == null ? -Number(row[15]) : Number(row[16]);
  const winner = rows.reduce((a, b) => remaining(a) > remaining(b) ||
    (remaining(a) === remaining(b) && modelVersion(a) >= modelVersion(b)) ? a : b);
  rows.forEach(r => { r[7] = r === winner ? '사용중' : '선택 해제'; });
  return winner;
}
function replyFrom(rows) {
  const winner = rows.find(r => r[7] === '사용중');
  if (!winner) throw new Error('이미 종료된 예약입니다. 새 요청 ID가 필요합니다.');
  return {
    ok: true,
    selected: {request_id: winner[0], key_label: winner[3], model: winner[4], project: winner[2]},
    sampled: rows.map(r => ({key_label: r[3], model: r[4], day_count: r[15], remaining: r[16]})),
  };
}
function acquire(sheet, config, logs, body, now) {
  if (!/^[a-f0-9-]{36}$/.test(body.request_id || '') || !Array.isArray(body.candidates))
    throw new Error('예약 요청 형식이 올바르지 않습니다.');
  const prior = logs.filter(r => String(r[0]).startsWith(body.request_id + ':'));
  if (prior.length) {
    if (prior.some(r => r[1] !== body.user)) throw new Error('예약 소유자가 다릅니다.');
    if (prior.every(r => r[7] === '후보 예약')) {
      selectRows(prior);
      // 1차 기록 직후 응답 유실도 같은 요청 ID로 복구한다.
      sheet.getRange(FIRST_ROW + logs.indexOf(prior[0]), 1, prior.length, LOG_WIDTH).setValues(prior);
      SpreadsheetApp.flush();
    }
    return replyFrom(prior);
  }
  const sample = sampleCandidates(config, logs, body.candidates, now, Math.random);
  if (!sample.length) {
    const allowed = new Set(body.candidates.map(c => groupKey(c.key_label, c.model)));
    const relevant = config.filter(c => allowed.has(groupKey(c.key_label, c.model)));
    const terminal = !relevant.some(c => {
      const s = statsFor(c, logs, now);
      return !s.daily && s.count < c.rpd && c.rpm > 0;
    });
    return {ok: true, selected: null, terminal,
      reason: '사용 가능한 조합 없음: 별칭/모델 설정, 일일 한도, 사용중 예약 및 대기 시간을 확인하세요.'};
  }
  const iso = new Date(now).toISOString();
  const rows = sample.map((c, i) => [body.request_id + ':' + i, clean(body.user), c.project,
    c.key_label, c.model, iso, '', '후보 예약', '', '', '', '', '', '', dayAt(now), c.day_count, c.remaining]);
  const range = sheet.getRange(FIRST_ROW + logs.length, 1, rows.length, LOG_WIDTH);
  range.setValues(rows); // 두 후보를 한 번의 쓰기로 함께 예약
  SpreadsheetApp.flush();
  selectRows(rows);
  range.setValues(rows); // 미선택 후보 즉시 해제, 선택 후보만 사용중
  SpreadsheetApp.flush();
  return replyFrom(rows);
}
function finish(sheet, logs, body, now) {
  const index = logs.findIndex(r => r[0] === body.request_id && r[1] === body.user);
  if (index < 0) throw new Error('요청 ID 또는 예약 소유자를 확인하세요.');
  const row = logs[index];
  if (['성공', '실패', '결과 미확인'].includes(row[7])) return {ok: true};
  if (row[7] !== '사용중') throw new Error('선택된 예약만 종료할 수 있습니다.');
  if (!['성공', '실패', '결과 미확인'].includes(body.status)) throw new Error('처리 상태가 올바르지 않습니다.');
  row[7] = body.status; row[6] = new Date(now).toISOString();
  ['input_tokens', 'output_tokens', 'total_tokens'].forEach((name, i) => {
    const n = body[name];
    if (n != null && (!Number.isInteger(n) || n < 0)) throw new Error('토큰 수가 올바르지 않습니다.');
    row[8 + i] = n == null ? '' : n;
  });
  row[11] = Number.isInteger(body.error_code) ? body.error_code : '';
  row[12] = ['일일', '일시'].includes(body.limit) ? body.limit : '없음';
  row[13] = row[12] === '일시' ? new Date(now + Math.max(1, Math.min(86400, Number(body.retry_seconds) || 60)) * 1000).toISOString() : '';
  sheet.getRange(FIRST_ROW + index, 1, 1, LOG_WIDTH).setValues([row]);
  SpreadsheetApp.flush();
  return {ok: true};
}
function refreshOverview(book, config, logs, now) {
  const sheet = book.getSheetByName('사용 현황');
  if (!sheet) return;
  const groups = [...new Map(config.map(c => [groupKey(c.project, c.model), c])).values()];
  const rows = groups.map(c => {
    const s = statsFor(c, logs, now);
    const status = s.busy ? '사용중' : s.daily || s.count >= c.rpd ? '일일 소진' :
      s.retry > now || s.rpm >= c.rpm ? '일시 대기' : '사용 가능';
    return [dayAt(now), c.project, c.model, s.count, s.busy ? 1 : 0,
      s.daily ? 0 : Math.max(0, c.rpd - s.count), status,
      s.retry > now ? new Date(s.retry).toISOString() : '', new Date(now).toISOString()];
  });
  sheet.getRange('A2').setValue('공용 기록 집계 — 요청 수는 예약 시 보수적으로 차감합니다. 외부 사용량은 미반영입니다.');
  const height = Math.max(rows.length, sheet.getLastRow() - FIRST_ROW + 1);
  if (height > 0) sheet.getRange(FIRST_ROW, 1, height, 9).setValues(
    Array.from({length: height}, (_, i) => rows[i] || Array(9).fill('')));
  return rows;
}
function doPost(e) {
  let lock;
  let result;
  try {
    const body = JSON.parse(e.postData.contents);
    const token = PropertiesService.getScriptProperties().getProperty('POOL_TOKEN');
    if (!token || token.length < 32 || body.token !== token) throw new Error('인증 실패');
    if (!body.user || typeof body.user !== 'string') throw new Error('사용자 별칭이 필요합니다.');
    clean(body.user);
    if (!['acquire', 'finish', 'status'].includes(body.action)) throw new Error('지원하지 않는 요청');
    lock = LockService.getScriptLock();
    lock.waitLock(10000);
    const book = SpreadsheetApp.openById(SHEET_ID);
    const settings = book.getSheetByName('프로젝트 설정'), log = book.getSheetByName('요청 기록');
    if (!settings || !log) throw new Error('설정/요청 기록 탭을 확인하세요.');
    const logs = readRows(log, LOG_WIDTH), now = Date.now();
    // 설정 오류가 생겨도 이미 진행 중인 예약은 해제할 수 있어야 한다.
    if (body.action === 'finish') result = finish(log, logs, body, now);
    else {
      const config = settingsFrom(readRows(settings, 10));
      result = body.action === 'acquire' ? acquire(log, config, logs, body, now) : {ok: true};
    }
    // 요청 기록이 원본이다. 현황 갱신 실패가 성공한 예약/종료를 취소하지 않는다.
    try {
      const config = settingsFrom(readRows(settings, 10));
      result.rows = refreshOverview(book, config, readRows(log, LOG_WIDTH), now);
      log.getRange('P5').setValue('후보 선택 전 요청 수');
      log.getRange('Q5').setValue('후보 선택 전 잔여량');
    } catch (_) { result.overview_pending = true; }
  } catch (err) {
    result = {ok: false, error: String(err.message)};
  } finally {
    if (lock && lock.hasLock()) lock.releaseLock();
  }
  return ContentService.createTextOutput(JSON.stringify(result)).setMimeType(ContentService.MimeType.JSON);
}

// Node의 모의 시트 테스트에서만 사용. Apps Script에서는 실행되지 않는다.
if (typeof module !== 'undefined') module.exports = {
  readRows, settingsFrom, statsFor, sampleCandidates, selectRows, acquire, finish, doPost,
};
