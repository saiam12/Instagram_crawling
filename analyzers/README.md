# Instagram Reels Analyzer

터미널에 Instagram Reel URL을 입력하면 영상·음성·썸네일을 분석해 JSON과 XLSX로 저장하는 CLI 도구입니다.

`analysis.korean`은 원본 캡션·해시태그에 한글이 있거나 대한민국 기반 계정으로 확인되면 `true`, 해외 기반 계정으로 확인되면 `false`, 국가를 알 수 없으면 `null`입니다. 수집기가 실행하는 분석에는 수집 기록의 판정값을 전달합니다. 독립 CLI에서는 다운로드 메타데이터에 실제로 제공된 원문·태그·명시적인 계정 국가만 사용하며, 영상 외모·음성·자막이나 생성된 한국어 요약으로 추정하지 않습니다. `COLLECTOR_REEL_KOREAN=true/false/null`로 전달한 값은 자동 판정보다 우선합니다. JSON의 바깥 `korean`에도 같은 값을 저장하며, XLSX 요약과 터미널에서 불명은 `-`, 확인된 값은 boolean으로 표시합니다.

분석 JSON의 `korean_source`는 국가 판정의 근거를 나타냅니다. 계정 국가를 확인한 결과와 근거가 없는 미확인 상태를 구분합니다.

## 설치

Windows PowerShell과 Python 3.10 이상을 준비합니다. 아래 첫 명령은 복제한 저장소 루트에서 실행합니다.
Python 3.14 환경에서 오프라인 테스트를 확인했습니다. 폴더 이름이나 드라이브 위치는 자유롭게 정할 수 있습니다.

```powershell
Set-Location .\analyzers
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

이후 명령은 이 폴더에서 `.\.venv\Scripts\python.exe`로 실행합니다. 수집기와 분석기의
가상환경은 각각 관리하며, 폴더를 옮긴 경우 `-m pip`로 패키지를 설치합니다.
다른 PC로 이동할 때는 `.venv`를 복사하지 말고 위 명령으로 새로 만드세요.
수집기와 분석기는 동일한 국가 판정 규칙을 공유하므로 저장소 전체 구조를 유지하세요.

## API 키 설정

1. `.env.example` 파일을 `.env` 로 복사합니다.
   ```powershell
   if (-not (Test-Path .env)) { Copy-Item .env.example .env }
   ```
2. `.env` 파일의 `GEMINI_API_KEYS`를 큰따옴표로 감싸고, 중괄호 안에 키를 한 줄씩 입력합니다. 단일 키는 `GEMINI_API_KEY`도 지원합니다.

   ```dotenv
   GEMINI_API_KEYS="{acct1:키1,
   acct2:키2}"
   GEMINI_API_KEY_OWNERS="{acct1:담당자1,
   acct2:담당자2}"
   ```
   키를 추가하거나 변경한 뒤 아래 명령으로 공용 풀을 동기화합니다. 로컬에서만 삭제한 공유 키는
   다음 동기화에서 다시 내려올 수 있으므로 제거·사용 중지 기준은 시트 연동 안내를 따릅니다.

   ```powershell
   .\.venv\Scripts\python.exe reel_analyzer.py --sync-key-pool
   ```

   동기화할 때 실제 키는 Apps Script의 `GEMINI_SHARED_KEYS` 스크립트 속성에 저장되고,
   공용 시트 셀에는 기록되지 않습니다. 같은 `GEMINI_POOL_TOKEN`을 가진 사용자는 공유 키를 내려받아
   로컬 `.env`에 자동으로 추가하거나 갱신합니다.
   - 키 발급: https://aistudio.google.com/apikey
3. `GEMINI_MODELS`에 사용할 모델을 우선순위 순서로 입력합니다. 복수 설정이 없으면 `GEMINI_MODEL`을 사용합니다.

공용 풀을 처음 사용한다면 관리자에게 같은 배포의 `/exec` URL과 토큰을 안전하게 전달받아
`GEMINI_POOL_URL`, `GEMINI_POOL_TOKEN`, 고유한 `GEMINI_POOL_USER`를 설정합니다.
자신의 키를 추가하지 않으면 `GEMINI_API_KEYS="{}"`로 두고 `--sync-key-pool`을 실행할 수 있습니다.
이 명령은 공용 서버와 로컬 `.env`를 실제로 갱신하므로 설정을 마친 뒤 실행하세요.

공용 시트 없이 혼자 실행하려면 `.env`에 아래 값을 설정하고 동기화 명령은 생략합니다.

```dotenv
GEMINI_POOL_MODE=local
GEMINI_API_KEY=본인의API키
GEMINI_API_KEYS=
```

단일 키를 쓸 때는 `GEMINI_API_KEYS` 줄을 제거하거나 빈 값으로 두세요.
`GEMINI_API_KEYS="{}"`는 공용 풀의 초기 내려받기 용도이며 단일 `GEMINI_API_KEY`보다 우선합니다.

실제 `.env`와 로컬 설정·사용량·출력은 저장소 루트 `.gitignore`에서 제외합니다. 공유용 `.env.example`에는 실제 키나 토큰을 넣지 않습니다.

## 실행

기본 실행은 **Google Sheets 공용 풀**입니다. 먼저 [시트 연동 설정](pool/apps_script/README.md)을 마치세요.
릴스마다 무작위 후보 2개를 함께 예약하고 당일 요청 수가 적은 조합을 선택합니다.
기본 모델은 Flash 3.6이며, Flash 3.8은 명시적으로 선택할 때만 사용합니다. Flash 3.5/3.7은 사용 대상에서 제외합니다. 연결 설정이 없으면 실행을 중단합니다.
로컬 단독 풀이 필요한 경우에만 `.env`에서 `GEMINI_POOL_MODE=local`로 지정하세요.

```powershell
.\.venv\Scripts\python.exe reel_analyzer.py
```

기본값은 영상 1개당 Gemini 호출 1회입니다. 호출 횟수를 줄이기 위해 영상 2개를 한 요청으로 묶으려면:

```powershell
.\.venv\Scripts\python.exe reel_analyzer.py --group-size 2
```

대화형 입력에서는 두 번째 URL이 들어오면 묶음 분석을 시작합니다. 홀수로 남은 URL은 종료할 때 단독 처리되며,
`flush`를 입력하면 현재 모인 URL을 즉시 처리합니다. XLSX 입력에도 같은 옵션을 사용할 수 있습니다.

분석 결과에는 훅/바디 구조, 씬별 상세 분석, 오디오, 썸네일, Selling Point와 영상 생성 프롬프트가 포함됩니다.
키/모델 풀을 사용해 사용 가능한 조합으로 자동 전환합니다.

콘텐츠 유형(`content_type`)은 응답 스키마의 허용 목록에서 선택합니다. `fashion_lookbook`·`daily_lookbook`·`outfit_lookbook`·`streetwear_lookbook`은 모두 `lookbook`으로 통일하며, 적절한 유형이 없으면 `other`를 사용합니다. 공통 규칙은 `collectors/src/analysis_categories.py`에서 관리합니다. 분석 시각화는 기존 결과와 브라우저 캐시에도 이 통합을 적용하며 원본 분석 파일을 다시 쓰거나 Gemini에 재분석을 요청하지 않습니다. 제품 시연+룩북 등 복합 유형은 유지합니다.

실행 후 프롬프트에 Reel URL을 입력하면 됩니다.

```
Reel URL > https://www.instagram.com/reels/DcSDbXNCtfd/
```

- 종료하려면 `quit`, `exit`, `q` 중 하나를 입력하세요.
- 분석 중에도 다음 URL을 계속 입력할 수 있습니다. URL은 대기열에 들어가며 공용 시트 모드에서는 2개 워커가 병렬로 처리합니다.
- `quit`, `exit`, `q`를 입력하면 이미 대기열에 넣은 분석을 모두 마친 뒤 종료합니다.

URL 한 건을 바로 분석하려면:

```powershell
.\.venv\Scripts\python.exe reel_analyzer.py --url "https://www.instagram.com/reel/SHORTCODE/"
.\.venv\Scripts\python.exe reel_analyzer.py -url SHORTCODE
```

`-url` 또는 `--url`에 shortcode만 입력하면 `https://www.instagram.com/reels/SHORTCODE/`로 자동 변환합니다.
대화형 입력과 XLSX의 `url`/`reel_url` 열에서도 shortcode만 사용할 수 있습니다.

XLSX의 모든 워크시트에서 `url` 또는 `reel_url` 열을 찾아 순차 분석하려면:

```powershell
.\.venv\Scripts\python.exe reel_analyzer.py --xlsx "C:\path\to\reels.xlsx"
```

두 URL씩 한 번의 Gemini 호출로 분석하려면:

```powershell
.\.venv\Scripts\python.exe reel_analyzer.py --xlsx "C:\path\to\reels.xlsx" --group-size 2
```

특정 모델만 사용하려면 `--model`을 함께 지정합니다. 생략하면 공용 풀이 모델을 자동 선택합니다.

```powershell
.\.venv\Scripts\python.exe reel_analyzer.py --xlsx "C:\path\to\reels.xlsx" --model gemini-3.6-flash
```

같은 입력에서 최대한 비슷한 결과를 얻으려면 모델과 seed를 함께 고정합니다.

```powershell
.\.venv\Scripts\python.exe reel_analyzer.py -url SHORTCODE --model gemini-3.6-flash --seed 42
.\.venv\Scripts\python.exe reel_analyzer.py --xlsx "C:\path\to\reels.xlsx" --group-size 2 --model gemini-3.6-flash --seed 42
```

`--group-size 2`에서는 두 영상이 하나의 요청을 공유하므로 seed도 묶음 전체에 하나가 적용됩니다.
재현성을 유지하려면 같은 두 URL을 같은 순서로 묶어야 합니다. 영상의 짝이나 순서, 모델, 프롬프트가 바뀌면
같은 seed여도 결과가 달라질 수 있으며, seed 고정은 완전히 동일한 출력을 보장하지는 않습니다.

지원 분석 모델은 `gemini-3.6-flash`(기본), `gemini-3.8-flash`입니다.
이 목록은 현재 코드의 허용 목록입니다. 실제 프로젝트의 모델 제공 여부와 한도는 별도로 확인해야 합니다.
누적 결과의 공통점을 찾는 `reel_insights.py`도 기본적으로 `gemini-3.6-flash`와 공용 시트 풀을 사용합니다. XLSX의 한 URL이 실패해도 다음 URL 분석은 계속됩니다.
`reel_insights.py`도 명시한 `GEMINI_POOL_MODE=local`을 유지합니다.
설정 오류·입력 오류·단건 분석 실패와 XLSX 일부 실패는 종료 코드 `1`, 정상 완료는 `0`입니다.
대화형 실행도 대기열을 모두 처리한 뒤 한 건이라도 실패했다면 `1`로 종료합니다.

## 분석 결과

- `audio_analysis`: 시간대별 대사·내레이션 transcript, 효과음, BGM 분위기·속도·보컬 여부
- `thumbnail_analysis`: Instagram 메타데이터의 커버 이미지를 우선 분석하고, 커버를 가져오지 못하면 영상 첫 프레임을 분석
- `marketing_analysis.selling_points`: Selling Point와 시작·종료 시각, 종합 근거, 음성 근거,
  시각 근거, 화면 문구 근거, 매력 유형, `evidence_confidence`
- `scene_details`: 0초부터 종료까지 이어지는 장면별 구도, 카메라 이동, 화면 문구, 오디오와 역할
- `scene_details[].subject_orientations`: 인물별 몸통 방향·머리 방향·눈의 시선·시선 대상과 근거.
  몸통/머리는 좌우 약한 사선·사선·강한 사선·측면·뒤 사선·정면·후면으로 구분하며 숫자 각도는 저장하지 않습니다.
- `scene_details[].camera_view`: 수평 또는 아주 약한/약한/보통/강한/매우 강한/수직 상하 시점,
  조준 대상과 근거. 판단 신뢰도는 `매우 높음`·`높음`·`보통`·`낮음`·`확인 불가` 텍스트입니다.
  공통 분류는 `scene_directions.py`에서 관리하고 가이드에도 같은 텍스트로 전달합니다.
  이 항목들은 새 분석부터 기록되며, 가려진 얼굴·눈이나 근거 없는 시점을 정면/수평으로 보충하지 않습니다.
  기존 분석 파일은 자동 재분석하거나 변경하지 않습니다.
- `subjects.people`: 인물 A·인물 B별 화면상 성별 표현(`male`/`female`/`unknown`)
- `scene_details[].worn_outfits`: 장면별 실제 착용자 이름표, 성별 표현, 착용한 옷·신발·액세서리.
  플랫레이나 마네킹처럼 착용자가 없는 장면은 빈 목록
- `recommended_audience`: 영상에서 제안하는 대상별 연령대(`20s`, `30s`, `20s_30s` 등),
  성별(`male`/`female`/`all`/`unknown`), 판단 근거. 근거가 없으면 빈 목록
- `generation_prompts`: 원본을 재현하기 위한 영문 영상 생성 프롬프트와 그래픽 후처리 메모

인물의 성별 표현은 화면에서 확인되는 표현이며 실제 성별·성 정체성 판정이 아닙니다.
`recommended_audience`는 실제 시청자 통계가 아닌 영상 내용에 근거한 추천 대상입니다.
20대인지 확실하지 않지만 20~30대 대상이라는 근거가 있으면 `20s_30s`, 연령 근거가 없으면 `unknown`을 사용합니다.

`evidence_confidence`는 판매 성과나 구매 전환 확률이 아니라, 관찰된 음성·화면·문구가 해당 해석을
얼마나 명확하게 뒷받침하는지를 나타내는 0~1 값입니다. 0.5 미만인 Selling Point는 저장 전에 제외합니다.
노래 가사는 전사하지 않으며, 들리지 않는 대사·판독되지 않는 문구·브랜드·인물 신원은 추측하지 않습니다.

각 레코드의 `analyzed_at`은 한국 표준시로 저장합니다.

```text
2026-09-19, 23:57:23
```

누적 결과를 즉시 다시 분석하고 HTML 보고서를 만들려면 다음 명령을 사용합니다. 자동 실행은 새 결과가 10건 이상 쌓이면 한 번에 10건씩 처리합니다.

```powershell
.\.venv\Scripts\python.exe reel_insights.py --input "..\collectors\data\collector\reel_analysis.json" --output "..\collectors\data\collector\reel_insights.json" --report "..\collectors\data\collector\reel_insights.html" --state "..\collectors\data\collector\.datasets\fashion\.collector\fashion_reel_insights_state.json" --force
```

## 분석 기능 구성

분석 입력은 Reel URL 또는 Excel에서 선택합니다. 영상과 커버 이미지에서 관찰 가능한 근거를 추출하고, 장면·음성·문구·제품 특징을 구조화합니다. 다운로드만 수행하거나, 누적 분석 결과를 인사이트 보고서로 정리하는 작업도 선택할 수 있습니다.

| 기능 | 처리 방식 |
|---|---|
| 개별·묶음 분석 | 한 번에 1~2개 Reel을 분석해 구조화된 결과를 저장합니다. |
| 결과 축적 | 독립 실행에서는 분석 JSON과 Excel 요약을 누적하며, 수집기 연동에서는 선택한 수집 데이터 영역에 저장합니다. |
| 키·사용량 관리 | 팀 공용 풀 또는 명시적으로 선택한 로컬 풀에서 한도·예약·실패를 관리합니다. |
| 인사이트 생성 | 누적 결과를 다시 분석해 요약 JSON과 HTML 보고서를 만듭니다. |

## 동작 방식

1. `yt-dlp`로 Reel의 실제 mp4 URL과 커버 이미지 URL을 찾아 메모리로 다운로드 (원본 영상은 디스크에 저장하지 않음)
2. 오디오 트랙이 포함된 MP4 전체와 커버 이미지를 같은 Gemini 요청에 전달
3. 영상 용량이 18MB 이하면 base64로 바로 전송하고, 넘으면 Gemini Files API로 업로드한 뒤 ACTIVE 상태에서 분석
4. 모든 분석 결과를 `output/reel_anaylsis.json` 배열에 누적 저장하고, 사람이 읽기 쉬운 요약을
   `output/reel_anaylsis.xlsx`에 행 단위로 추가 저장
5. 터미널에 핵심 요약(요약문, Hook, 콘텐츠 유형)을 출력

XLSX는 JSON 전체를 복제하지 않고 URL, 분석 시각, 요약, Hook, 콘텐츠 유형, 장면 수, transcript,
BGM, Selling Point 근거, 인물 성별 표현, 장면별 착용 의상, 추천 대상을 행 단위로 정리한 요약본입니다.
Excel 요약은 분석 결과를 저장할 때 함께 작성합니다.

## Collector 연동

`collectors`에서 `.\collector.ps1 fashion`을 실행하면 조건을 충족하고 누적 분석 결과에 아직 없는 Fashion Reel에 대해
Analyzer가 자동 호출됩니다. 현재 자동 분석 대상은 `reaction_rate = 조회수 / 팔로워 수`가 `90` 이상,
즉 표시상 9000% 이상인 영상입니다.

Collector는 여러 Analyzer 프로세스가 같은 XLSX를 동시에 수정하는 충돌을 막기 위해 `GEMINI_OUTPUT_FILE`을
지정하고 JSON만 생성합니다. `reel_analyzer.py`를 직접 실행할 때는 `output/reel_anaylsis.json`과
`output/reel_anaylsis.xlsx`가 모두 생성됩니다. Collector는 완료된 결과를 개별 저장하고
`collectors/data/collector/reel_analysis.json`에 누적합니다. `--data-dir`를 지정하면 그 루트에 저장하며,
성공 상태가 있어도 누적 결과에서 사라진 URL은 다시 분석합니다. 수집기와 별도 Android 워커의 지표 보강은
분석기와 독립적으로 진행됩니다.

독립 실행의 JSON 위치를 바꾸려면 `$env:GEMINI_OUTPUT_FILE = "D:\analysis\result.json"`처럼 지정합니다.
대상 폴더는 자동 생성되며 이 설정이 있으면 XLSX 요약은 작성하지 않습니다.

## 주의사항

- 비공개 계정의 Reel은 다운로드할 수 없습니다. 접근 가능한 공개 Reel을 입력하세요. 다운로드 전용 실행에서는 `--cookies-from-browser`로 브라우저를 지정할 수 있습니다.
- Instagram의 페이지 구조 변경으로 `yt-dlp`가 간헐적으로 실패할 수 있습니다. 이 경우 `pip install -U yt-dlp` 로 최신 버전으로 업데이트하세요.
- Gemini API 사용량에는 비용이 발생할 수 있습니다. 가격 정책은 https://ai.google.dev/gemini-api/docs/pricing 참고하세요.

## 키/모델 전환

아래 로컬 카운트와 순차 전환 설명은 `GEMINI_POOL_MODE=local`에 해당합니다.
공용 모드의 예약·선택·복구 규칙은 [시트 연동 설정](pool/apps_script/README.md)을 따릅니다.

- 성공한 키/모델을 계속 사용하다가 사용 불가 시 같은 키의 다음 모델, 다음 키 순서로 전환합니다.
- 429 응답에 일일 제한이 명시된 경우 해당 조합을 태평양시 자정까지 소진 처리합니다. 분당/토큰 제한 또는 원인이 불명확한 429는 응답의 재시도 시간(없으면 60초) 동안 대기 상태로 둡니다.
- 큰 영상도 같은 전환 로직을 사용하며, 키를 전환하면 해당 키로 파일을 다시 업로드합니다.
- 503 등 재시도 가능한 API 오류도 최초 호출을 포함해 최대 5회까지만 시도합니다.
- 인증/권한 오류는 해당 키, 404는 해당 조합을 현재 실행에서 제외합니다. 잘못된 요청 등 그 외 오류는 즉시 표시합니다.
- 로컬 풀의 RPM/RPD는 `pool/local.py`의 추정치를 사용합니다. 별도 설정이 필요하면 `model_limits.json`을 직접 만들 수 있으며, 실행 중 자동 생성되지는 않습니다.
- 로컬 풀 사용량은 `output/gemini_usage_state.json`에 저장됩니다. 제한 상태는 일일 초기화와 재시도 가능 시각에 따라 관리합니다.
- 한도는 [프로젝트별로 공유](https://ai.google.dev/gemini-api/docs/rate-limits)됩니다. 같은 프로젝트의 키를 추가해도 한도가 늘지 않습니다. 별도 프로젝트가 반드시 별도 Google 계정일 필요는 없습니다.
- 키 풀은 무료 요금제를 강제하지 않습니다. 무료 사용이 목적이라면 AI Studio에서 프로젝트 요금제와 선택한 모델의 무료 제공 여부를 확인하세요.
- Windows 시간대 데이터는 `requirements.txt`의 `tzdata`로 설치합니다.

검증은 `analyzers`에서 `.\.venv\Scripts\python.exe -B -m unittest discover -s tests -p 'test*.py' -v`와
`node --test tests/test_apps_script.cjs`를 실행합니다. 실제 API는 호출하지 않습니다.

저장소 전체 구조와 수집기 실행은 [루트 안내](../README.md)와 [수집기 안내](../collectors/README.md)를 참고하세요.

## 촬영 가이드 도식 테스트

모니터의 영상 기획 추천은 `guide_videos.py`로 Antigravity 가이드 애니메이션 MP4 6개를 만듭니다. 분석과 scene_details를 전달하며 참고 형식 3개·특징 혼합 3개를 별도 병렬 요청합니다. MP4와 응답/장면 설계는 `output/guides`에 보관합니다. 아래 `guide_diagram.py` 명령은 기존 PNG 도식의 단독 진단·재변환용으로 유지합니다.

테스트 전에 `pool/apps_script/Code.js`를 기존 Apps Script 프로젝트에 반영하고 **배포 관리 → 기존 웹 앱 수정 → 새 버전**으로 배포한 뒤 아래 명령으로 모델 행을 동기화하세요. 배포 URL을 그대로 유지해야 모든 PC가 같은 풀을 사용합니다.

```powershell
# 저장소 루트에서 실행
Set-Location .\analyzers
.\.venv\Scripts\python.exe reel_analyzer.py --sync-key-pool
```

기존 3.5/3.7 행은 삭제하지 않고 중지하며, 3.8 및 Antigravity 행을 사용합니다. RPM/RPD 기본값은 보수적인 로컬 설정값이며 실제 프로젝트 한도는 AI Studio에서 확인해 시트에 반영하세요. API 키는 교체하지 않습니다. 실제 API 접근 권한과 생성 결과는 생성 버튼을 눌러 확인합니다.

모니터에서는 Flash 기획 요청 없이 Antigravity가 영상별 장면 구성·대본·촬영 구도를 직접 설계합니다. 각 MP4와 함께 장면별 대본을 저장하고 웹에서 영상별로 펼쳐 봅니다. 대본의 음성 합성은 포함하지 않습니다. 공용 시트의 Antigravity 행을 사용하며 분석용 Flash와 검색용 Embedding 요청은 기존대로 유지합니다.

### Antigravity 단독 연결 테스트

3.6 분석·기획 요청 없이 고정된 한 장면으로 Antigravity만 호출하려면 저장소 루트에서 실행합니다. 별도 Gemini 모델을 지정하지 않고 Antigravity 기본 모델을 사용합니다. 기존 공유 키 풀에 Antigravity 예약·사용량을 기록합니다.

```powershell
.\analyzers\.venv\Scripts\python.exe .\analyzers\guide_diagram.py --test
```

결과는 `analyzers/output/antigravity_test.png`에 저장됩니다. Antigravity가 거부하는 `store=false`는 전송하지 않으며 API 기본 저장 정책을 사용합니다. SVG 주석은 제거하고 스크립트·외부 참조는 계속 거부합니다.

단독 테스트는 SVG 검증 전에 `antigravity_test.<실행ID>.response.json`에 최종 텍스트·상태·토큰 사용량을 보관합니다. API 키는 저장하지 않습니다. 변환 오류가 나면 표시된 응답 경로로 `--replay`를 실행하면 API를 호출하지 않고 다시 변환합니다.

```powershell
.\analyzers\.venv\Scripts\python.exe .\analyzers\guide_diagram.py --replay "응답파일의 실제 경로"
```

키 풀의 성공은 API 응답과 로컬 검증이 모두 성공한 경우에 기록합니다. 응답 수신 뒤 로컬 검증이 실패하면 실패로 기록하면서 반환된 토큰 사용량은 유지합니다. 기존 성공 기록은 소급 변경하지 않습니다.

모니터 참고 사례 검색은 `embedding_search.py`의 Gemini Embedding 2 텍스트 임베딩을 사용합니다.
공용 키 풀에 `gemini-embedding-2`를 별도 작업으로 등록하며 `GEMINI_MODELS`의 분석용 Flash 설정은 유지합니다.
문서별 요청을 묶어 768차원 벡터를 받고 정규화하여 코사인 검색에 사용합니다. 모델·차원·입력문별
SQLite 캐시는 `output/reel_embeddings.sqlite3`에 저장됩니다. 기존 문서와 동일 검색어는 API를 호출하지
않으며 변경된 분석문과 새 검색어만 처리합니다. API 오류는 자동 재전송하지 않습니다.
새 입력은 최대 8개씩 순차 처리하며, 긴 분석문은 묶음의 입력량에 따라 더 작게 나눕니다.
UTF-8 바이트 수와 문서별 여유분을 합산하여 묶음 12,000, 최근 60초 24,000의 보수적
로컬 전송량 기준을 적용합니다. 이는 정확한 토큰 계측값이나 Google의 실제 한도가 아닙니다.
필요하면 잠시 대기하며 대기 중 DB 잠금은 해제합니다. 전송량 이력은 같은 SQLite 캐시를
사용하는 워커끼리 공유합니다. 완료된 묶음은 즉시 저장하므로 실패 후 다시 검색해도 재사용합니다.
초기 색인은 오래 걸릴 수 있어 모니터의 임베딩 검색 대기 한도는 최대 1시간입니다.
공용 키 풀 배포를 갱신한 뒤 `reel_analyzer.py --sync-key-pool`로 모델 행을 추가해야 합니다.

### 원본 장면별 촬영 구도

가이드의 각 장면은 `reference_id`와 `source_scene`으로 원본 장면을 연결합니다. 원본에서 확인한 구도는 `reference_framing.json`의 릴스 ID·장면 번호별 기록을 우선하며, 다른 장면으로 복사하지 않습니다. 검토된 6개 참고 영상의 92개 장면은 실제 원본 프레임의 화면 비율·여백·신체 범위·배치를 대조한 기록입니다. 새 분석은 장면마다 `camera_frame`에 실제 화면 비율·신체 크롭·인물 크기를 기록하고, 확인 불가 항목은 unknown/null로 남깁니다. 기존 분석 원본은 자동 재작성하지 않습니다. 얼굴 제외 조건으로 하반신까지 잘라내지 않으며, 거울 셀카·상품 컷·플랫레이·소매 디테일을 각각 구분합니다.
