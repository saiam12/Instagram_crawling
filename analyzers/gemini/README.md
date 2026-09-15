# Instagram Reels Analyzer

터미널에 Instagram Reel URL을 입력하면 영상을 분석해서 JSON으로 저장하는 CLI 도구입니다.

## 설치

```bash
cd C:\Instagram-crawling\analyzers\gemini
python -m pip install -r requirements.txt
```

## API 키 설정

1. `.env.example` 파일을 `.env` 로 복사합니다.
   ```bash
   cp .env.example .env
   ```
2. `.env` 파일의 `GEMINI_API_KEYS`를 큰따옴표로 감싸고, 중괄호 안에 키를 한 줄씩 입력합니다. 단일 키는 `GEMINI_API_KEY`도 지원합니다.

   ```dotenv
   GEMINI_API_KEYS="{acct1:키1,
   acct2:키2}"
   ```
   키를 추가하거나 제거한 뒤 아래 명령으로 공용 시트에 별칭을 동기화합니다.

   ```powershell
   python reel_analyzer.py --sync-key-pool
   ```

   동기화할 때 실제 키는 Apps Script의 `GEMINI_SHARED_KEYS` 스크립트 속성에 저장되고,
   공용 시트 셀에는 기록되지 않습니다. 같은 `GEMINI_POOL_TOKEN`을 가진 팀원은 공유 키를 내려받아
   로컬 `.env`에 자동으로 추가하거나 갱신합니다.
   - 키 발급: https://aistudio.google.com/apikey
3. `GEMINI_MODELS`에 사용할 모델을 우선순위 순서로 입력합니다. 복수 설정이 없으면 `GEMINI_MODEL`을 사용합니다.

`.env` 파일은 절대 git에 커밋하지 마세요. (`.gitignore`에 추가 권장)

## 실행

기본 실행은 **Google Sheets 공용 풀**입니다. 먼저 [시트 연동 설정](pool/apps_script/README.md)을 마치세요.
릴스마다 무작위 후보 2개를 함께 예약하고 당일 요청 수가 적은 조합을 선택합니다.
Flash 3.5/3.6/3.7만 허용하며 3.8은 비활성화되어 있습니다. 연결 설정이 없으면 실행을 중단합니다.
로컬 단독 풀이 필요한 경우에만 `.env`에서 `GEMINI_POOL_MODE=local`로 지정하세요.

```bash
python reel_analyzer.py
```

분석 결과에는 훅/바디 구조, 씬별 상세 분석, 영상 생성 프롬프트가 포함됩니다. 키/모델 풀을 사용해 사용 가능한 조합으로 자동 전환합니다.

실행 후 프롬프트에 Reel URL을 입력하면 됩니다.

```
Reel URL > https://www.instagram.com/reels/DcSDbXNCtfd/
```

- 종료하려면 `quit`, `exit`, `q` 중 하나를 입력하세요.
- 분석 중에도 다음 URL을 계속 입력할 수 있습니다. URL은 대기열에 들어가며 공용 시트 모드에서는 2개 워커가 병렬로 처리합니다.
- `quit`, `exit`, `q`를 입력하면 이미 대기열에 넣은 분석을 모두 마친 뒤 종료합니다.

URL 한 건을 바로 분석하려면:

```bash
python reel_analyzer.py --url "https://www.instagram.com/reel/SHORTCODE/"
```

XLSX의 모든 워크시트에서 `url` 또는 `reel_url` 열을 찾아 순차 분석하려면:

```bash
python reel_analyzer.py --xlsx "C:\path\to\reels.xlsx"
```

특정 모델만 사용하려면 `--model`을 함께 지정합니다. 생략하면 공용 풀이 모델을 자동 선택합니다.

```bash
python reel_analyzer.py --xlsx "C:\path\to\reels.xlsx" --model gemini-3.6-flash
```

지원 모델은 `gemini-3.5-flash`, `gemini-3.6-flash`, `gemini-3.7-flash`입니다.
`gemini-3.8-flash`는 운영 안정성 확인 전까지 비활성화되어 있습니다. XLSX의 한 URL이 실패해도 다음 URL 분석은 계속됩니다.

## 프로젝트 구조

```text
gemini/
├─ reel_analyzer.py       # CLI 진입점과 영상 분석
├─ input_sources.py       # URL 검증과 XLSX 입력
├─ pool/                  # 로컬·Google Sheets 키 풀
│  └─ apps_script/        # Apps Script 서버와 설치 문서
├─ tests/                 # Python·Apps Script 테스트
├─ output/                # 누적 분석 JSON
└─ requirements.txt
```

## 동작 방식

1. `yt-dlp`로 Reel의 실제 mp4 URL을 추출해 메모리로 다운로드 (디스크에 원본 저장 안 함)
2. 영상 용량이 18MB 이하면 base64로 바로 Gemini에 전송 (inline)
3. 영상 용량이 18MB를 넘으면 Gemini Files API로 업로드 후, 처리(ACTIVE)가 끝날 때까지 대기했다가 분석 (큰 영상도 자동 처리됨 — 기존 코드처럼 에러로 중단되지 않음)
4. 모든 분석 결과를 `output/reel_analyses.json` 배열에 누적 저장
5. 터미널에 핵심 요약(요약문, Hook, 콘텐츠 유형)을 출력

## 주의사항

- 비공개 계정의 Reel은 다운로드할 수 없습니다. 필요하면 코드 내 `cookiesfrombrowser` 옵션 주석을 해제해서 로그인된 브라우저 쿠키를 사용하세요.
- Instagram의 페이지 구조 변경으로 `yt-dlp`가 간헐적으로 실패할 수 있습니다. 이 경우 `pip install -U yt-dlp` 로 최신 버전으로 업데이트하세요.
- Gemini API 사용량에는 비용이 발생할 수 있습니다. 가격 정책은 https://ai.google.dev/gemini-api/docs/pricing 참고하세요.

## 키/모델 전환

아래 로컬 카운트와 순차 전환 설명은 `GEMINI_POOL_MODE=local`에 해당합니다.
공용 모드의 예약·선택·복구 규칙은 [시트 연동 설정](pool/apps_script/README.md)을 따릅니다.

- 성공한 키/모델을 계속 사용하다가 사용 불가 시 같은 키의 다음 모델, 다음 키 순서로 전환합니다.
- 429 응답에 일일 제한이 명시된 경우 해당 조합을 태평양시 자정까지 소진 처리합니다. 분당/토큰 제한 또는 원인이 불명확한 429는 응답의 재시도 시간(없으면 60초) 동안 대기 상태로 둡니다.
- 큰 영상도 같은 전환 로직을 사용하며, 키를 전환하면 해당 키로 파일을 다시 업로드합니다.
- 인증/권한 오류는 해당 키, 404는 해당 조합을 현재 실행에서 제외합니다. 잘못된 요청 등 그 외 오류는 즉시 표시합니다.
- `model_limits.json`의 RPM/RPD는 로컬 추정치입니다. 본인 프로젝트의 [AI Studio 한도](https://aistudio.google.com/usage?tab=rate-limit)에 맞춰 수정하세요.
- 사용량은 `gemini_usage_state.json`에 저장됩니다. 이전 버전이 일시적 429를 일일 소진으로 기록한 상태는 자동으로 판별할 수 없으므로 일일 초기화 전까지 남을 수 있습니다.
- 한도는 [프로젝트별로 공유](https://ai.google.dev/gemini-api/docs/rate-limits)됩니다. 같은 프로젝트의 키를 추가해도 한도가 늘지 않습니다. 별도 프로젝트가 반드시 별도 Google 계정일 필요는 없습니다.
- 키 풀은 무료 요금제를 강제하지 않습니다. 무료 사용이 목적이라면 AI Studio에서 프로젝트 요금제와 선택한 모델의 무료 제공 여부를 확인하세요.
- Windows 시간대 데이터는 `requirements.txt`의 `tzdata`로 설치합니다.

검증은 `analyzers/gemini`에서 `python -m unittest discover -s tests -p 'test*.py' -v`와
`node --test tests/test_apps_script.cjs`를 실행합니다. 실제 API는 호출하지 않습니다.
