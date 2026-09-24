# Instagram Reels Analyzer

터미널에 Instagram Reel URL을 입력하면 영상·음성·썸네일을 분석해 JSON과 XLSX로 저장하는 CLI 도구입니다.

## 설치

```powershell
cd C:\Instagram-crawling\analyzers\gemini
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

현재 환경은 Python 3.14를 사용합니다. 이후 명령의 `python`은 가상환경을 활성화했거나
`.\.venv\Scripts\python.exe`로 실행하는 것을 전제로 합니다.

## API 키 설정

1. `.env.example` 파일을 `.env` 로 복사합니다.
   ```bash
   cp .env.example .env
   ```
2. `.env` 파일의 `GEMINI_API_KEYS`를 큰따옴표로 감싸고, 중괄호 안에 키를 한 줄씩 입력합니다. 단일 키는 `GEMINI_API_KEY`도 지원합니다.

   ```dotenv
   GEMINI_API_KEYS="{acct1:키1,
   acct2:키2}"
   GEMINI_API_KEY_OWNERS="{acct1:담당자1,
   acct2:담당자2}"
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

기본값은 영상 1개당 Gemini 호출 1회입니다. 호출 횟수를 줄이기 위해 영상 2개를 한 요청으로 묶으려면:

```bash
python reel_analyzer.py --group-size 2
```

대화형 입력에서는 두 번째 URL이 들어오면 묶음 분석을 시작합니다. 홀수로 남은 URL은 종료할 때 단독 처리되며,
`flush`를 입력하면 현재 모인 URL을 즉시 처리합니다. XLSX 입력에도 같은 옵션을 사용할 수 있습니다.

분석 결과에는 훅/바디 구조, 씬별 상세 분석, 오디오, 썸네일, Selling Point와 영상 생성 프롬프트가 포함됩니다.
키/모델 풀을 사용해 사용 가능한 조합으로 자동 전환합니다.

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
python reel_analyzer.py -url SHORTCODE
```

`-url` 또는 `--url`에 shortcode만 입력하면 `https://www.instagram.com/reels/SHORTCODE/`로 자동 변환합니다.
대화형 입력과 XLSX의 `url`/`reel_url` 열에서도 shortcode만 사용할 수 있습니다.

XLSX의 모든 워크시트에서 `url` 또는 `reel_url` 열을 찾아 순차 분석하려면:

```bash
python reel_analyzer.py --xlsx "C:\path\to\reels.xlsx"
```

두 URL씩 한 번의 Gemini 호출로 분석하려면:

```bash
python reel_analyzer.py --xlsx "C:\path\to\reels.xlsx" --group-size 2
```

특정 모델만 사용하려면 `--model`을 함께 지정합니다. 생략하면 공용 풀이 모델을 자동 선택합니다.

```bash
python reel_analyzer.py --xlsx "C:\path\to\reels.xlsx" --model gemini-3.6-flash
```

같은 입력에서 최대한 비슷한 결과를 얻으려면 모델과 seed를 함께 고정합니다.

```bash
python reel_analyzer.py -url SHORTCODE --model gemini-3.7-flash --seed 42
python reel_analyzer.py --xlsx "C:\path\to\reels.xlsx" --group-size 2 --model gemini-3.7-flash --seed 42
```

`--group-size 2`에서는 두 영상이 하나의 요청을 공유하므로 seed도 묶음 전체에 하나가 적용됩니다.
재현성을 유지하려면 같은 두 URL을 같은 순서로 묶어야 합니다. 영상의 짝이나 순서, 모델, 프롬프트가 바뀌면
같은 seed여도 결과가 달라질 수 있으며, seed 고정은 완전히 동일한 출력을 보장하지는 않습니다.

지원 모델은 `gemini-3.5-flash`, `gemini-3.6-flash`, `gemini-3.7-flash`입니다.
`gemini-3.8-flash`는 운영 안정성 확인 전까지 비활성화되어 있습니다. 누적 결과의 공통점을 찾는 `reel_insights.py`는 기본적으로 `gemini-3.6-flash`와 공용 시트 풀을 사용합니다. XLSX의 한 URL이 실패해도 다음 URL 분석은 계속됩니다.

## 분석 결과

- `audio_analysis`: 시간대별 대사·내레이션 transcript, 효과음, BGM 분위기·속도·보컬 여부
- `thumbnail_analysis`: Instagram 메타데이터의 커버 이미지를 우선 분석하고, 커버를 가져오지 못하면 영상 첫 프레임을 분석
- `marketing_analysis.selling_points`: Selling Point와 시작·종료 시각, 종합 근거, 음성 근거,
  시각 근거, 화면 문구 근거, 매력 유형, `evidence_confidence`
- `scene_details`: 0초부터 종료까지 이어지는 장면별 구도, 카메라 이동, 화면 문구, 오디오와 역할
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
python reel_insights.py --input "..\..\collectors\unified\data_web\fashion_reel_analyses.json" --output "..\..\collectors\unified\data_web\fashion_reel_insights.json" --report "..\..\collectors\unified\data_web\fashion_reel_insights.html" --state "..\..\collectors\unified\data_web\.datasets\fashion\.collector\fashion_reel_insights_state.json" --force
```

## 프로젝트 구조

```text
gemini/
├─ reel_analyzer.py       # CLI 진입점과 영상 분석
├─ input_sources.py       # URL 검증과 XLSX 입력
├─ pool/                  # 로컬·Google Sheets 키 풀
│  └─ apps_script/        # Apps Script 서버와 설치 문서
├─ tests/                 # Python·Apps Script 테스트
├─ output/                # 누적 분석 JSON과 사람이 읽기 쉬운 XLSX
└─ requirements.txt
```

## 동작 방식

1. `yt-dlp`로 Reel의 실제 mp4 URL과 커버 이미지 URL을 찾아 메모리로 다운로드 (원본 영상은 디스크에 저장하지 않음)
2. 오디오 트랙이 포함된 MP4 전체와 커버 이미지를 같은 Gemini 요청에 전달
3. 영상 용량이 18MB 이하면 base64로 바로 전송하고, 넘으면 Gemini Files API로 업로드한 뒤 ACTIVE 상태에서 분석
4. 모든 분석 결과를 `output/reel_analyses.json` 배열에 누적 저장하고, 사람이 읽기 쉬운 요약을
   `output/reel_analyses.xlsx`에 행 단위로 추가 저장
5. 터미널에 핵심 요약(요약문, Hook, 콘텐츠 유형)을 출력

XLSX는 JSON 전체를 복제하지 않고 URL, 분석 시각, 요약, Hook, 콘텐츠 유형, 장면 수, transcript,
BGM, Selling Point 근거, 인물 성별 표현, 장면별 착용 의상, 추천 대상을 행 단위로 정리한 요약본입니다.
기존 JSON 레코드는 XLSX에 자동으로 소급 변환되지 않습니다.

## Collector 연동

`collectors\unified`에서 `.\collector.ps1 fashion`을 실행하면 조건을 충족한 새 Fashion Reel에 대해
Analyzer가 자동 호출됩니다. 현재 자동 분석 대상은 `reaction_rate = 조회수 / 팔로워 수`가 `90` 이상,
즉 표시상 9000% 이상인 영상입니다.

Collector는 여러 Analyzer 프로세스가 같은 XLSX를 동시에 수정하는 충돌을 막기 위해 `GEMINI_OUTPUT_FILE`을
지정하고 JSON만 생성합니다. `reel_analyzer.py`를 직접 실행할 때는 `output/reel_analyses.json`과
`output/reel_analyses.xlsx`가 모두 생성됩니다.

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
- 503 등 재시도 가능한 API 오류도 최초 호출을 포함해 최대 5회까지만 시도합니다.
- 인증/권한 오류는 해당 키, 404는 해당 조합을 현재 실행에서 제외합니다. 잘못된 요청 등 그 외 오류는 즉시 표시합니다.
- 로컬 풀의 RPM/RPD는 `pool/local.py`의 추정치를 사용합니다. 별도 설정이 필요하면 `model_limits.json`을 직접 만들 수 있으며, 실행 중 자동 생성되지는 않습니다.
- 로컬 풀 사용량은 `output/gemini_usage_state.json`에 저장됩니다. 이전 버전이 일시적 429를 일일 소진으로 기록한 상태는 자동으로 판별할 수 없으므로 일일 초기화 전까지 남을 수 있습니다.
- 한도는 [프로젝트별로 공유](https://ai.google.dev/gemini-api/docs/rate-limits)됩니다. 같은 프로젝트의 키를 추가해도 한도가 늘지 않습니다. 별도 프로젝트가 반드시 별도 Google 계정일 필요는 없습니다.
- 키 풀은 무료 요금제를 강제하지 않습니다. 무료 사용이 목적이라면 AI Studio에서 프로젝트 요금제와 선택한 모델의 무료 제공 여부를 확인하세요.
- Windows 시간대 데이터는 `requirements.txt`의 `tzdata`로 설치합니다.

검증은 `analyzers/gemini`에서 `python -m unittest discover -s tests -p 'test*.py' -v`와
`node --test tests/test_apps_script.cjs`를 실행합니다. 실제 API는 호출하지 않습니다.
