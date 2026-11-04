"""Cached Embedding 2 retrieval; shared quota reservations and no generation calls."""
from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import time
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

import requests
from pool import SheetsKeyPool
from pool.local import EMBEDDING_MODEL

DIMENSIONS = 768
BATCH_SIZE = 8
# Conservative UTF-8 byte budgets, not an exact tokenizer or a Google quota claim.
BATCH_INPUT_BUDGET = 12000
MINUTE_INPUT_BUDGET = 24000
CACHE_PATH = Path(__file__).resolve().parent / 'output' / 'reel_embeddings.sqlite3'


def clip(value, size=7000):
    return str(value).encode('utf-8')[:size].decode('utf-8', errors='ignore')


def document_text(document):
    fields = [('product', '제품'), ('recommended_audience', '추천 타겟'), ('summary', '요약'),
              ('hook', '훅'), ('body_structure', '구성'), ('marketing_analysis', '소구'),
              ('editing', '편집'), ('audio_analysis', '오디오')]
    body = '\n'.join(f'{label}: {document["analysis"][key]}' for key, label in fields if key in document['analysis'])
    title = clip(document.get('title') or 'none', 600)
    return clip(f'title: {title} | text: {body}\n해시태그: {document.get("hashtags", "")}')


def query_text(brief):
    fields = [('product', '제품'), ('target', '타겟'), ('purpose', '목적'), ('selling_point', '핵심 소구점')]
    return 'task: search result | query: ' + '\n'.join(f'{label}: {brief[key]}' for key, label in fields if brief.get(key))


def normalize(vector):
    if not isinstance(vector, list) or len(vector) != DIMENSIONS or any(type(x) not in (int, float) or not math.isfinite(x) for x in vector):
        raise ValueError('invalid embedding vector')
    norm = math.sqrt(sum(x*x for x in vector))
    if norm == 0 or not math.isfinite(norm):
        raise ValueError('invalid embedding norm')
    return [x / norm for x in vector]


def input_budget(texts):
    return sum(len(text.encode('utf-8')) + 256 for text in texts)


def batches(keys, texts):
    batch = []
    for key in keys:
        if input_budget([texts[key]]) > MINUTE_INPUT_BUDGET:
            raise ValueError('embedding text exceeds local input budget')
        if batch and (len(batch) >= BATCH_SIZE or input_budget([texts[k] for k in batch]+[texts[key]]) > BATCH_INPUT_BUDGET):
            yield batch
            batch = []
        batch.append(key)
    if batch:
        yield batch


def reserve_input(connection, cost):
    """Shared rolling window across workers using this on-disk cache."""
    now = time.time()
    connection.execute('DELETE FROM embedding_requests WHERE sent_at <= ?', (now-60,))
    rows = connection.execute('SELECT sent_at, cost FROM embedding_requests ORDER BY sent_at').fetchall()
    used = sum(row[1] for row in rows)
    if used + cost <= MINUTE_INPUT_BUDGET:
        connection.execute('INSERT INTO embedding_requests VALUES (?,?)', (now,cost))
        return 0
    for sent_at, previous_cost in rows:
        used -= previous_cost
        if used + cost <= MINUTE_INPUT_BUDGET:
            return max(.01, sent_at+60-now)


def request_embeddings(texts):
    pool = SheetsKeyPool(models=[EMBEDDING_MODEL])
    combo = pool.acquire_blocking(max_wait_sec=0)
    response = error = None
    try:
        result = requests.post(f'https://generativelanguage.googleapis.com/v1beta/models/{EMBEDDING_MODEL}:batchEmbedContents',
            headers={'x-goog-api-key': combo.api_key, 'Content-Type': 'application/json'},
            json={'requests': [{'model': f'models/{EMBEDDING_MODEL}', 'content': {'parts': [{'text': text}]},
                                'embedContentConfig': {'outputDimensionality': DIMENSIONS}} for text in texts]},
            timeout=(15, 60))
        result.raise_for_status()
        data = result.json()
        usage = data.get('usageMetadata') or {}
        response = SimpleNamespace(usage_metadata=SimpleNamespace(prompt_token_count=usage.get('promptTokenCount'),
            candidates_token_count=0, total_token_count=usage.get('totalTokenCount')))
        values = data.get('embeddings', [])
        if len(values) != len(texts):
            raise ValueError('embedding response count mismatch')
        return [normalize(item.get('values')) for item in values]
    except Exception as failure:
        error = failure
        if isinstance(failure, requests.HTTPError):
            error = SimpleNamespace(code=failure.response.status_code)
            if error.code in (429, 500, 502, 503, 504):
                pool.mark_cooldown(combo, 60)
        raise
    finally:
        pool.finish(combo, response=response, error=error)


def vectors(texts, cache_path=CACHE_PATH):
    cache_path = Path(cache_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    keys = [hashlib.sha256(f'{EMBEDDING_MODEL}|{DIMENSIONS}|search-v1|{text}'.encode()).hexdigest() for text in texts]
    unique = dict(zip(keys, texts))
    found, requested = {}, 0
    with closing(sqlite3.connect(cache_path, timeout=75)) as connection:
        connection.execute('CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT NOT NULL)')
        connection.execute('CREATE TABLE IF NOT EXISTS embedding_requests (sent_at REAL NOT NULL, cost INTEGER NOT NULL)')
        def read(key):
            row = connection.execute('SELECT vector FROM vectors WHERE key=?', (key,)).fetchone()
            if row:
                try:
                    return normalize(json.loads(row[0]))
                except (ValueError, TypeError):
                    pass
            return None
        for key in unique:
            value = read(key)
            if value is not None:
                found[key] = value
        missing = [key for key in unique if key not in found]
        for candidates in batches(missing, unique):
            while True:
                delay, failure = 0, None
                # Release the lock while waiting; recheck the cache before sending.
                with connection:
                    connection.execute('BEGIN IMMEDIATE')
                    batch = []
                    for key in candidates:
                        value = read(key)
                        if value is None:
                            batch.append(key)
                        else:
                            found[key] = value
                    if batch:
                        delay = reserve_input(connection,input_budget([unique[key] for key in batch]))
                        if not delay:
                            try:
                                generated = request_embeddings([unique[key] for key in batch])
                            except Exception as error:
                                failure = error  # Commit the reservation even if API processing is uncertain.
                            else:
                                for key, vector in zip(batch, generated):
                                    found[key] = vector
                                    connection.execute('INSERT OR REPLACE INTO vectors VALUES (?,?)', (key, json.dumps(vector)))
                                requested += len(batch)
                if failure is not None:
                    raise failure
                if not delay:
                    break
                time.sleep(min(delay,30))
    return [found[key] for key in keys], {'requested': requested, 'cached': len(unique)-requested}


def semantic_search(documents, brief, cache_path=CACHE_PATH):
    if not documents:
        return {'semantic': [], 'model': EMBEDDING_MODEL, 'dimensions': DIMENSIONS, 'cache': {'requested': 0, 'cached': 0}}
    values, stats = vectors([document_text(document) for document in documents] + [query_text(brief)], cache_path)
    query = values[-1]
    semantic = [{'reel_id': document['reel_id'], 'cosine_similarity': max(-1., min(1., sum(a*b for a, b in zip(vector, query))))}
                for document, vector in zip(documents, values[:-1])]
    semantic.sort(key=lambda item: item['cosine_similarity'], reverse=True)
    return {'semantic': semantic, 'model': EMBEDDING_MODEL, 'dimensions': DIMENSIONS, 'cache': stats}
