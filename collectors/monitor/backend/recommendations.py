"""Local retrieval and isolated, explicit recommendation jobs; no collector writes."""
from __future__ import annotations

import collections
import json
import math
import os
import re
import secrets
import subprocess
import threading
import time
import unicodedata
from pathlib import Path
from datetime import datetime, timezone

try:
    from .classification import korean_value, korean_selection, matches_korean
except ImportError:
    from classification import korean_value, korean_selection, matches_korean
try:
    from collectors.src.reels.korean_classification import apply_korean_overrides
except ModuleNotFoundError:
    from src.reels.korean_classification import apply_korean_overrides

BASE = Path(__file__).resolve().parents[1]
PROJECT = BASE.parents[1]
ANALYZER = PROJECT / "analyzers"
DATASET_ID = "data/collector/reel_analysis.json"
DATASET_FILE = PROJECT / "collectors" / DATASET_ID
BRIEF_FIELDS = {"product": "제품", "target": "타겟", "purpose": "목적"}
OPTIONAL_BRIEF_FIELDS = {"selling_point": "핵심 소구점", "production_constraints": "제작 조건"}
METRICS = ("view_count", "like_count", "comment_count", "share_count", "repost_count", "saved_count", "follower_count")
ALIASES = (
    ("화장품", "뷰티", "메이크업", "스킨케어", "cosmetic", "beauty", "makeup", "skincare"),
    ("패션", "옷", "의류", "코디", "fashion", "outfit", "lookbook"),
    ("여성", "여자", "여성용", "female", "women"),
    ("남성", "남자", "남성용", "male", "men"),
    ("감성", "감성적", "차분", "따뜻", "cozy", "emotional", "warm", "calm"),
    ("신제품", "출시", "런칭", "인지", "신상", "launch", "awareness"),
    ("구매", "판매", "전환", "구매유도", "purchase", "conversion"),
)

PROVIDER_ERRORS = {
    "gemini_unavailable": "Gemini가 일시적으로 요청을 처리하지 못했습니다(HTTP 503). 잠시 후 다시 생성하세요. 자동 재전송하지 않았습니다.",
    "gemini_rate_limit": "Gemini 요청 한도에 도달했습니다(HTTP 429). 사용 한도를 확인한 뒤 다시 생성하세요.",
    "gemini_auth": "Gemini 키의 인증 또는 모델 접근 권한을 확인하세요(HTTP 401/403).",
    "gemini_model": "요청한 Gemini 모델을 사용할 수 없습니다(HTTP 404). 모델 설정을 확인하세요.",
    "gemini_request": "Gemini가 요청 형식을 거부했습니다(HTTP 400). 요청 스키마와 모델 옵션을 확인하세요.",
}


class RecommendationProviderError(ValueError):
    def __init__(self, code):
        super().__init__(PROVIDER_ERRORS[code])


def brief_values(payload):
    if not isinstance(payload, dict):
        raise ValueError("제품·타겟·목적을 입력하세요.")
    values = {}
    for key, label in BRIEF_FIELDS.items():
        value = payload.get(key)
        if not isinstance(value, str) or not 1 <= len(value.strip()) <= 200:
            raise ValueError(f"{label}은 1~200자로 입력하세요.")
        values[key] = value.strip()
    for key, label in OPTIONAL_BRIEF_FIELDS.items():
        value = payload.get(key, "")
        if not isinstance(value, str) or len(value.strip()) > 200:
            raise ValueError(f"{label}은 200자 이내로 입력하거나 비워 두세요.")
        if value.strip():
            values[key] = value.strip()
    follower = payload.get('follower_count','')
    if follower not in ('',None):
        cleaned=str(follower).replace(',','').strip()
        if isinstance(follower,bool) or not re.fullmatch(r'\d+',cleaned) or int(cleaned)>9_007_199_254_740_991:
            raise ValueError('내 계정 팔로워 수는 0 이상의 정수로 입력하세요.')
        values['follower_count']=int(cleaned)
    return values


def reel_id(value):
    match = re.search(r"(?:instagram\.com/(?:reel|reels|p)/)([A-Za-z0-9_-]+)", str(value))
    if match:
        return match[1]
    return str(value) if re.fullmatch(r"[A-Za-z0-9_-]{5,80}", str(value)) else ""


def words(value):
    value = unicodedata.normalize("NFKC", str(value)).casefold()
    tokens = re.findall(r"[a-z0-9]+|[가-힣]+", value)
    return [part for token in tokens for part in
            ([token] + ["~" + token[i:i + 2] for i in range(len(token) - 1)] if re.search("[가-힣]", token) else [token])]


def text(value, limit=1200):
    if isinstance(value, str):
        return value[:limit]
    return json.dumps(value, ensure_ascii=False)[:limit] if value is not None else ""


def metric(value):
    try:
        result = float(str(value).replace(",", ""))
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def record_time(row):
    try:
        value=datetime.fromisoformat(str(row.get('collected_at','')).replace('Z','+00:00'))
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError,TypeError,OverflowError):
        return 0


def collected_metrics(reel_ids, data_root=None):
    """Read existing exports only; never rewrite analysis or collector history."""
    root=Path(data_root) if data_root is not None else DATASET_FILE.parent
    roots=[root,root/'.datasets'/'fashion',root.parent/'refresh']
    latest,users,sources,warnings={},{},[],[]
    def read(path, excel=False):
        if not path.is_file(): return []
        try:
            if path.stat().st_size>32*1024*1024: raise ValueError('size')
            raw=path.read_bytes()
            if excel:
                try: from .visualization_data import read_visualization_file
                except ImportError: from visualization_data import read_visualization_file
                rows=[row for dataset in read_visualization_file(raw,path.name)['datasets'] for row in dataset['rows']]
            else:
                rows=json.loads(raw.decode('utf-8-sig'))
                if not isinstance(rows,list): raise ValueError('rows')
            try: label=path.relative_to(PROJECT).as_posix()
            except ValueError: label=path.name
            sources.append(label)
            return [{**row,'_metric_source':label} for row in rows[:100000] if isinstance(row,dict)]
        except (OSError,ValueError,UnicodeError):
            warnings.append(f'{path.name} 수집 지표를 읽지 못했습니다.')
            return []
    for directory in roots:
        for row in read(directory/'reels.json'):
            key=reel_id(row.get('url') or row.get('reel_url') or row.get('reel_id'))
            if key in reel_ids and (key not in latest or record_time(row)>record_time(latest[key])): latest[key]=row
        for row in read(directory/'users.json'):
            username=str(row.get('username','')).strip().casefold()
            if username and (username not in users or record_time(row)>record_time(users[username])):users[username]=row
    if reel_ids-latest.keys():
        for directory in roots:
            for row in read(directory/'reels.xlsx',excel=True):
                key=reel_id(row.get('url'))
                if key in reel_ids and (key not in latest or record_time(row)>record_time(latest[key])): latest[key]=row
    return latest,users,sources,warnings


def metric_projection(record, snapshot, users):
    values={field:metric(snapshot.get(field)) for field in METRICS}
    available={field:value for field,value in values.items() if value is not None}
    username=str(snapshot.get('username') or record.get('username') or '').strip()
    account=users.get(username.casefold(),{})
    follower_time=str(snapshot.get('collected_at',''))
    follower_source=snapshot.get('_metric_source','analysis')
    if metric(account.get('follower_count')) is not None and (record_time(account)>=record_time(snapshot) or 'follower_count' not in available):
        available['follower_count']=metric(account['follower_count'])
        follower_time=str(account.get('collected_at',''))
        follower_source=account['_metric_source']
    return {"username":username,"metrics":available,
            "missing_metrics":[field for field in METRICS if field not in available],
            "private_metrics":['like_count'] if str(snapshot.get('like_count','')).strip().upper()=='X' else [],
            "metric_source":snapshot.get('_metric_source','analysis'),
            "follower_source":follower_source,"follower_collected_at":follower_time}


def corpus():
    try:
        if DATASET_FILE.stat().st_size > 32 * 1024 * 1024:
            raise ValueError("분석 파일은 32 MiB 이하여야 합니다.")
        data = json.loads(DATASET_FILE.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise ValueError("collectors/data/collector/reel_analysis.json을 읽지 못했습니다. 파일 경로와 JSON 형식을 확인하세요.") from None
    if not isinstance(data, list):
        raise ValueError("분석 데이터셋은 JSON 배열이어야 합니다.")
    analyses, warnings = {}, []
    classified = apply_korean_overrides([row for row in data[-10000:] if isinstance(row,dict)],DATASET_FILE.parent)
    for record in reversed(classified):
        if not isinstance(record, dict) or not isinstance(record.get("analysis"), dict) or not record["analysis"]:
            continue
        key = reel_id(record.get("reel_id") or record.get("reel_url"))
        if key:
            analyses.setdefault(key, record)
    documents = []
    # ponytail: cap the first local index at 5,000 Reels; use a persisted index if it grows.
    keys = list(analyses)
    if len(keys) > 5000:
        warnings.append("검색 부하를 줄이기 위해 지정 분석 파일 중 최대 5,000개 릴스를 사용합니다.")
    collected,users,metric_sources,metric_warnings=collected_metrics(set(keys[:5000]))
    warnings.extend(metric_warnings)
    for key in keys[:5000]:
        record = analyses[key]
        snapshot=collected.get(key,record)
        metric_info=metric_projection(record,snapshot,users)
        analysis = record.get("analysis", {})
        selected = {field: text(analysis[field], 900) for field in
                    ("summary", "hook", "body_structure", "product", "recommended_audience", "marketing_analysis", "audio_analysis", "editing")
                    if field in analysis}
        duration = metric(snapshot.get("video_duration_seconds")) or metric(record.get("video_duration_seconds"))
        duration_source = ("collector_record" if key in collected else "analysis_record") if duration and duration > 0 else ""
        if not duration_source:
            scenes = analysis.get("scene_details", [])
            duration = max((metric(scene.get("end_second")) or 0 for scene in scenes if isinstance(scene, dict)), default=0) if isinstance(scenes, list) else 0
            duration_source = "analysis_scene_end" if duration > 0 else ""
        item = {"reel_id": key, "url": f"https://www.instagram.com/reel/{key}/",
                "korean": korean_value(record),
                "title": text(record.get("title", ""), 800), "hashtags": text(record.get("hashtags", ""), 240),
                **metric_info,
                "scene_details": analysis.get("scene_details", []) if isinstance(analysis.get("scene_details"), list) else [],
                "collected_at": text(snapshot.get("collected_at", ""), 50),
                "analyzed_at": text(record.get("analyzed_at", ""), 50), "analysis": selected,
                "video_duration_seconds": duration if duration_source else None, "duration_source": duration_source}
        if item["title"] or selected:
            documents.append(item)
    return documents, [DATASET_FILE.relative_to(PROJECT).as_posix(),*metric_sources], warnings


def retrieve(documents, brief, limit=6):
    # Filming restrictions may contain negation; pass them only to the planner.
    query = " ".join(brief.get(key, "") for key in (*BRIEF_FIELDS, "selling_point"))
    expanded = [query]
    for group in ALIASES:
        if any((term in query.casefold() if re.search("[가-힣]", term) else re.search(r"\b" + re.escape(term) + r"\b", query.casefold())) for term in group):
            expanded.extend(group)
    query_terms = set(words(" ".join(expanded)))
    counters = [collections.Counter(words(" ".join([d["title"], d["hashtags"], *d["analysis"].values()]))) for d in documents]
    frequencies = collections.Counter(term for counter in counters for term in counter if term in query_terms)
    average = sum(sum(counter.values()) for counter in counters) / max(1, len(counters)) or 1
    ranked = []
    for document, counter in zip(documents, counters):
        score = 0.0
        length = sum(counter.values())
        matched = query_terms & counter.keys()
        for term in matched:
            inverse = math.log(1 + (len(documents) - frequencies[term] + .5) / (frequencies[term] + .5))
            count = counter[term]
            score += inverse * count * 2.2 / (count + 1.2 * (.25 + .75 * length / average))
        if score > 0:
            ranked.append({**document, "score": round(score, 2),
                           "matched_terms": sorted(term for term in matched if not term.startswith("~"))[:12]})
    ranked.sort(key=lambda item: item["score"], reverse=True)
    return [{**item, "reference_id": f"R{index}"} for index, item in enumerate(ranked[:limit], 1)]


def hybrid_retrieve(documents, brief, semantic, limit=6):
    lexical = retrieve(documents, brief, limit=len(documents))
    similarities = {}
    for item in semantic:
        value = item.get("cosine_similarity")
        if type(value) not in (int, float) or not math.isfinite(value) or not -1 <= value <= 1:
            raise ValueError("invalid similarity")
        similarities[item["reel_id"]] = value
    if set(similarities) != {document["reel_id"] for document in documents}:
        raise ValueError("incomplete semantic results")
    scores = collections.defaultdict(float)
    bm25 = {item["reel_id"]: item["score"] for item in lexical}
    semantic_order = sorted(similarities, key=similarities.get, reverse=True)
    for order in [[item["reel_id"] for item in lexical], [key for key in semantic_order if similarities[key] > 0]]:
        for rank, key in enumerate(order, 1):
            scores[key] += 1 / (60 + rank)
    ranked = sorted((document for document in documents if document["reel_id"] in scores),
                    key=lambda document: scores[document["reel_id"]], reverse=True)
    return [{**document, "reference_id": f"R{index}", "score": round(scores[document["reel_id"]], 6),
             "bm25_score": bm25.get(document["reel_id"], 0),
             "cosine_similarity": round(similarities[document["reel_id"]], 4)} for index, document in enumerate(ranked[:limit], 1)]


def rank_followers(references, brief, limit=6):
    target=brief.get('follower_count')
    if target is None or not references:
        return references[:limit]
    # Compare scale on a log axis within the 30 strongest content matches.
    candidates=references[:30]
    maximum=max(item['score'] for item in candidates) or 1
    ranked=[]
    for item in candidates:
        candidate=dict(item)
        followers=metric(item.get('metrics',{}).get('follower_count'))
        similarity=1/(1+abs(math.log2((followers+1)/(target+1)))) if followers is not None else 0
        if followers is not None: candidate['follower_similarity']=round(similarity,4)
        candidate['recommendation_score']=round(.7*item['score']/maximum+.3*similarity,6)
        ranked.append(candidate)
    ranked.sort(key=lambda item:item['recommendation_score'],reverse=True)
    return [{**item,'reference_id':f'R{index}'} for index,item in enumerate(ranked[:limit],1)]


class Recommendations:
    def __init__(self):
        self.lock = threading.RLock()
        self.searches = {}
        self.jobs = {}
        self.running = False
        self.config_cache = (0, {})

    @staticmethod
    def dataset():
        return {"id": DATASET_ID, "label": DATASET_FILE.name,
                "path": DATASET_FILE.relative_to(PROJECT).as_posix(), "available": DATASET_FILE.is_file()}

    def worker(self, payload=None, status=False, guide_cache=False, retrieval=False):
        executable = ANALYZER / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if not executable.is_file():
            raise ValueError("Analyzer Python 환경이 없습니다. analyzers/README.md의 설치 절차를 진행하세요.")
        command = [str(executable), "-B", str(BASE / "backend" / "gemini_recommend.py")]
        if status:
            command.append("--status")
        if guide_cache:
            command.append("--guide-cache")
        if retrieval:
            command.append("--retrieve")
        try:
            process = subprocess.run(command, input=json.dumps(payload, ensure_ascii=False) if payload else "",
                                     capture_output=True, encoding="utf-8", errors="replace", timeout=3600 if retrieval else 15 if status or guide_cache else 900,
                                     cwd=ANALYZER, env={**os.environ, "PYTHONIOENCODING": "utf-8"},
                                     creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            result = json.loads(process.stdout)
        except subprocess.TimeoutExpired:
            raise ValueError("요청 시간이 초과됐습니다. API 처리 여부가 불확실하므로 자동 재전송하지 않습니다.") from None
        except (OSError, ValueError):
            raise ValueError("추천 어댑터를 실행하지 못했습니다. Analyzer 의존성과 .env 설정을 확인하세요.") from None
        if isinstance(result, dict) and result.get("error_code") in PROVIDER_ERRORS:
            raise RecommendationProviderError(result["error_code"])
        if not isinstance(result, dict) or process.returncode or result.get("error"):
            raise ValueError(str(result.get("error", "추천 응답 형식이 올바르지 않습니다.")) if isinstance(result, dict) else "추천 응답 형식이 올바르지 않습니다.")
        videos = result.get("guide", {})
        if not status and not guide_cache and not retrieval and not (isinstance(result.get("plan"), dict) or
                isinstance(videos, dict) and videos.get("kind") == "videos" and isinstance(videos.get("videos"), list)):
            raise ValueError("Gemini 시트 풀 설정 또는 생성 결과를 확인하세요.")
        return result

    def guide(self, filename):
        if not isinstance(filename, str) or not re.fullmatch(r"[a-f0-9]{32}\.png", filename):
            raise ValueError("저장된 가이드 파일 이름을 확인하세요.")
        return self.worker({"filename": filename}, guide_cache=True)

    def video(self, filename):
        if not isinstance(filename, str) or not re.fullmatch(r"[a-f0-9]{32}\.mp4", filename):
            raise ValueError("저장된 가이드 영상 파일 이름을 확인하세요.")
        path = ANALYZER / "output" / "guides" / filename
        if not path.is_file():
            raise ValueError("저장된 가이드 영상이 없습니다.")
        return path

    def video_plan(self, filename):
        path=self.video(filename).with_suffix('.video.json')
        if not path.is_file() or path.stat().st_size > 2*1024*1024:
            raise ValueError('저장된 영상의 장면 구성을 확인하지 못했습니다.')
        metadata=json.loads(path.read_text(encoding='utf-8'))['metadata']
        if metadata.get('filename') != filename:
            raise ValueError('영상과 장면 구성 정보가 일치하지 않습니다.')
        fields=('state','index','mode','filename','title','duration_seconds','reference_ids','features','scenes','warning')
        return {'video':{key:metadata[key] for key in fields if key in metadata}}

    def configuration(self):
        with self.lock:
            if time.monotonic() - self.config_cache[0] < 30:
                return self.config_cache[1]
            try:
                result = self.worker(status=True)
            except ValueError as error:
                result = {"ready": False, "message": str(error)}
            self.config_cache = (time.monotonic(), result)
            return result

    def search(self, payload):
        if payload.get("dataset") != DATASET_ID:
            raise ValueError("참고 데이터셋이 변경됐습니다. 화면을 새로고침하세요.")
        brief = brief_values(payload.get("brief"))
        country = payload.get("country","all")
        if not isinstance(country,str):
            raise ValueError('참고 사례 구분을 확인하세요.')
        country = korean_selection(country)
        documents, sources, warnings = corpus()
        total_corpus_count = len(documents)
        documents = [document for document in documents if matches_korean(document,country)]
        references = retrieve(documents, brief,limit=len(documents))
        method, embedding = "bm25", {}
        if documents:
            try:
                embedding = self.worker({"documents": documents, "brief": brief}, retrieval=True)
                references = hybrid_retrieve(documents, brief, embedding["semantic"],limit=len(documents))
                method = "hybrid"
            except Exception:
                warnings.append("의미 검색을 사용할 수 없어 단어 검색(BM25) 결과를 표시합니다. 임베딩 연결·시트 한도를 확인하세요.")
        references=rank_followers(references,brief)
        if 'follower_count' in brief and references and not any('follower_count' in item.get('metrics',{}) for item in references):
            warnings.append('이 조건의 참고 사례에는 수집된 팔로워 수가 없어 계정 규모를 비교하지 못했습니다. 내용 검색 결과를 표시합니다.')
        result = {"id": secrets.token_urlsafe(18), "brief": brief, "dataset": DATASET_ID,
                  "metric_coverage":{"collected":sum(bool(item.get('metrics')) for item in documents),"total":len(documents)},
                  "country":country,"total_corpus_count":total_corpus_count,
                  "search_method": method, "embedding_model": embedding.get("model") if method == "hybrid" else None,
                  "embedding_cache": embedding.get("cache", {}) if method == "hybrid" else {},
                  "references": references, "sources": sources, "warnings": warnings,
                  "corpus_count": len(documents), "analyzed_count": sum(bool(d["analysis"]) for d in documents),
                  "created_at": time.time()}
        with self.lock:
            self.searches = {key: value for key, value in self.searches.items() if time.time() - value["created_at"] < 3600}
            if len(self.searches) >= 20:
                self.searches.pop(next(iter(self.searches)))
            self.searches[result["id"]] = result
        return result

    def start(self, payload):
        search_id, request_id = payload.get("search_id"), payload.get("request_id")
        if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{16,80}", request_id):
            raise ValueError("요청 ID를 확인하세요. 화면을 새로고침하세요.")
        with self.lock:
            if request_id in self.jobs:
                if self.jobs[request_id]["search_id"] != search_id:
                    raise ValueError("기존 요청과 검색 조건이 다릅니다.")
                return dict(self.jobs[request_id])
            search = self.searches.get(search_id) if isinstance(search_id, str) else None
            if not search or time.time() - search["created_at"] > 3600:
                raise ValueError("참고 사례가 만료됐습니다. 사례를 다시 검색하세요.")
            if not search["references"]:
                raise ValueError("조건과 맞는 참고 사례가 없습니다. 입력 범위를 넓혀 검색하세요.")
            if self.running:
                raise ValueError("이미 기획안을 생성 중입니다. 진행 중인 요청이 끝난 뒤 시도하세요.")
            self.jobs = {key: value for key, value in self.jobs.items() if time.time() - value["created_at"] < 3600}
            if len(self.jobs) >= 20:
                self.jobs.pop(next(iter(self.jobs)))
            job = {"id": request_id, "search_id": search_id, "state": "running", "created_at": time.time()}
            self.jobs[request_id] = job
            self.running = True
            threading.Thread(target=self._generate, args=(request_id, search), daemon=True).start()
            return dict(job)

    def _generate(self, request_id, search):
        try:
            result = self.worker({"brief": search["brief"], "references": search["references"]})
            patch = {"state": "completed", "result": result, "retrieval": search, "brief": search["brief"]}
        except RecommendationProviderError as error:
            patch = {"state": "failed", "error": str(error)}
        except Exception:
            # Never return raw SDK exceptions, request URLs, pool tokens or keys.
            patch = {"state": "failed", "error": "생성하지 못했습니다. Analyzer 의존성·시트 풀 설정·사용 한도를 확인하고 다시 시도하세요. 자동 재전송은 하지 않았습니다."}
        with self.lock:
            self.jobs[request_id].update(patch)
            self.running = False

    def job(self, request_id):
        with self.lock:
            job = self.jobs.get(request_id)
            if not job:
                raise ValueError("작업이 없거나 모니터가 재시작됐습니다. 사례를 다시 검색하세요.")
            return dict(job)
