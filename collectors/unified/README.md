# 웹 + Android Instagram Reels 수집기

이 폴더는 브라우저 기반 Python 수집기를 기준으로 Android Studio 에뮬레이터의 Instagram 앱 화면을 보강하는 통합 수집기입니다. 브라우저와 Android 앱은 각각 로그인 상태를 유지하며, 공개 결과는 이 폴더의 `data_web`에만 저장합니다.

수집 항목에는 조회수(`view_count`)가 포함됩니다. 릴스 공개 출력은 한 릴스가 한 행을 사용하고, 재수집 값은 `2nd collect_*` 같은 새 열에 저장됩니다. 사용자 공개 CSV·엑셀은 재수집마다 새 행을 추가합니다.

## 설치

PowerShell에서 이 폴더로 이동한 뒤 가상환경과 패키지를 설치합니다.

```powershell
cd C:\Instagram-crawling\collectors\unified
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

설치된 Microsoft Edge 또는 Google Chrome을 사용하므로 별도의 `playwright install` 명령은 필요하지 않습니다.

기존 `.venv`의 패키지 또는 Python 연결이 꼬였을 때는 다음 명령으로 Python 3.12 환경만 복구합니다. 수집 결과와 로그인 프로필은 유지됩니다.

```powershell
.\scripts\repair_venv.ps1
```

## 폴더 구조

| 경로 | 용도 |
|---|---|
| `reels` | 웹·팔로워 수집, Android 지표 보강, 일시정지·진단 코드 |
| `exporters` | CSV·JSON·XLSX 저장 코드 |
| `scripts` | 실행 목적별 진입점과 PowerShell 보조 스크립트 |
| `data_web` | 실제 수집 결과(처음 실행할 때 자동 생성) |
| `browser_profile/.instagram_chrome_profile` | Chrome 전용 로그인 프로필(자동 생성, 최초 실행 시 로그인 필요) |
| `browser_profile/.instagram_browser_profile` | 기존 브라우저 로그인 프로필 |
| `collector.ps1` | PowerShell 실행 진입점 |
| `scripts/start-android.ps1` | 에뮬레이터 준비 스크립트 |
| `scripts/repair_venv.ps1` | 가상환경 복구 스크립트 |

통합 수집기의 Android 보강 코드는 이 폴더의 `reels/android_reel_metrics.py`에 있습니다. 독립 Android 수집기는 `../android/`에서 별도로 관리합니다.

`reels/instagram_reels_browser.py`는 기존 CLI와 전체 실행 순서를 유지합니다. 순수 Reel 값·URL·파생 필드는 `reels/reel_records.py`, 원자적 저장 헬퍼는 `reels/reel_store.py`, Android 큐의 파일 배치는 `reels/android_metric_queue.py`, Playwright 실행 환경과 종료 처리는 `reels/browser_runtime.py`가 담당합니다. 테스트는 `tests/`에 모았습니다.

## 기본 실행

첫 실행 때는 로그인할 수 있도록 브라우저를 표시합니다.

```powershell
.\collector.ps1 --max-items 50
```

로그인 프로필이 저장된 뒤에는 백그라운드 실행도 가능합니다.

```powershell
.\collector.ps1 --max-items 50 --background
```

수집은 항상 기본 시작 URL인 `https://www.instagram.com/reels/`에서 시작합니다. `--start-url`을 지정하면 그 주소를 사용합니다.

기본 실행 결과는 다음과 같습니다.

| 파일 | 저장 구조 |
|---|---|
| `data_web\reels.csv` | `reels.xlsx`와 같은 열·재수집 표시를 사용하는 공개 CSV |
| `data_web\reels.json` | `reels.xlsx`와 같은 열·재수집 표시를 사용하는 공개 JSON |
| `data_web\reels.xlsx` | 기본 엑셀 결과이자 재수집 대상 목록 |
| `data_web\users.csv` | `users.xlsx`와 같은 수집 횟수별 행 이력 |
| `data_web\users.xlsx` | 수집 횟수별 행 이력과 직전 대비 팔로워 증감량 |
| `data_web\instagram_data.xlsx` | `hashtags`, `reels`, `users` 탭을 한 파일에 모은 통합 엑셀 |

모든 출력에는 `view_count`가 포함됩니다.

통합 엑셀은 아래 명령으로 최신 CSV 기준으로 다시 만듭니다.

```powershell
.\collector.ps1 xlsx
```

각 Reel은 먼저 브라우저 상세 페이지에서 URL·작성자·캡션·해시태그·위치·광고 여부·업로드일·영상 길이와 `repost_count`를 수집해 내부 이력에 저장합니다. 영상 길이는 Python 웹 응답을 우선하고, 없으면 현재 릴스의 HTML5 `video.duration`으로 수집합니다. URL은 영속 Android 작업 큐에 즉시 넣고 Python은 다음 Reel·다음 해시태그 작업으로 계속 진행합니다. 별도 Android 워커가 같은 URL을 열어 아래 Android 전용 필드만 같은 행에 나중에 보강합니다.

- `like_count`, `view_count`(play), `comment_count`, `share_count`, `saved_count`, `audio_name`

좋아요·조회수는 `Likes and plays` 패널에 완전한 정수가 있으면 그 값을 우선합니다. 댓글 수가 화면에 없을 때 댓글 시트가 `No comments yet`이면 `0`으로 저장하며, 비활성화된 댓글은 빈 값으로 남깁니다. 앱에 `10K`·`1.2M`처럼 축약 표기만 있는 필드는 K=1,000, M=1,000,000 기준의 표시 환산값이며, 원래의 정확한 정수라고 주장하지 않습니다. 좋아요가 비공개면 `like_count`는 `X`입니다.

Android Studio 에뮬레이터를 켜고 Instagram 앱에 로그인한 상태에서 실행하세요. **시작 화면은 홈·검색·릴스 어느 곳이어도 됩니다.** 수집기가 저장된 Reel URL을 직접 열어 자동 이동합니다. 다만 에뮬레이터 화면 잠금을 해제하고, Instagram의 로그인·권한 요청·오류 팝업을 먼저 닫아 두어야 합니다. Android 보강은 기본 활성화되어 있고 앱이 잠시 사용 불가해도 웹 행은 유지됩니다. 기본 모드에서는 데이터셋마다 하나의 영속 큐 워커만 실행되어 Python 종료 뒤에도 남은 URL을 이어 처리하고 `reels.xlsx`를 갱신합니다. Android 워커는 Reel 250개마다 완료 결과를 먼저 저장한 뒤 에뮬레이터 게스트를 재부팅하며 사용자 데이터와 Instagram 로그인은 삭제하지 않습니다. 재부팅 중에는 다음 작업을 pending에 둔 채 부팅 완료를 30초마다 확인하고, 준비되면 남은 큐를 계속 처리합니다. 실제 휴대폰은 자동 재부팅하지 않습니다. ADB 장치가 `offline`·`unauthorized`·`not found`·`error: closed` 상태가 되거나 명령이 타임아웃되면 Reel과 Tags 작업을 큐에 되돌리고, 내부 ADB 드라이버를 새로 만든 뒤 30초마다 실제 연결을 다시 검사합니다. 이 경우를 Reel 수집 5회 연속 실패로 계산하지 않습니다. 일반 UI 인식 실패가 5개 Reel에서 연속 발생해도 Python 수집을 강제 종료하지 않고 Android 워커만 30초 쉬었다가 남은 큐를 계속 처리합니다. 앱 지표가 반드시 필요할 때는 `--android-metrics-required`를 사용하면 이전처럼 현재 명령이 Android 완료를 기다립니다. 브라우저만 쓰려면 `--no-android-metrics`를 붙입니다.

Android Reel URL은 Chrome이나 시스템 링크 선택기로 빠지지 않도록 Instagram 패키지로 강제 실행합니다. Reel 화면은 최대 5초 기다리고, 인식되지 않으면 URL을 한 번 다시 연 뒤 2초 더 확인합니다. 그래도 실패하면 로그인 화면·외부 브라우저·삭제된 Reel·Instagram 임시 오류·UIAutomator 빈 화면을 구분해 `Android metrics unavailable` 뒤에 원인을 표시합니다. 이미 실행 중이던 수집 프로세스에는 코드 변경이 반영되지 않으므로 변경 후에는 수집기를 다시 시작해야 합니다.

워커가 Reel을 여는 순간 Windows 이벤트 로그에 `qemu-system-x86_64.exe`, 예외 코드 `0xc0000005`가 남으며 에뮬레이터가 꺼지는 경우는 ADB 종료 명령이 아니라 AVD 그래픽/영상 렌더러 충돌입니다. Device Manager에서 해당 AVD의 Graphics를 `Software`에서 `Hardware` 또는 `Automatic`으로 변경하고 `Cold Boot Now`로 시작하세요. Quick Boot 스냅샷이 계속 같은 충돌 상태를 복원하면 `Wipe Data` 후 다시 로그인해야 합니다. 워커는 부팅 완료(`sys.boot_completed=1`) 전에는 URL을 열지 않고 작업을 pending에 유지합니다.

UIAutomator가 연속 두 번 빈 화면을 반환하면 남은 대기를 즉시 중단하고 `뒤로 가기 → Reel URL 다시 열기`로 화면을 강제 갱신합니다. 같은 Reel의 각 시도와 재시도 오류는 PowerShell에 `[ANDROID] attempt ...`, `[ANDROID] retry ...` 형식으로 바로 출력됩니다.

`View shop` 등 동적 CTA가 있는 Reel에서 UIAutomator가 접근성 XML을 만들지 못하면 Android activity의 CTA view를 보조 신호로 확인합니다. 이 유형은 `skipped`로 한 번만 기록하고, 장치에 남은 UIAutomator 프로세스를 정리한 뒤 다음 큐 항목으로 넘어갑니다. Python이 이미 수집한 값은 그대로 유지됩니다.

CTA가 없는 일반 Reel에서도 접근성 XML이 비면 잔류 UIAutomator를 정리하고 Instagram 앱만 강제 종료한 뒤 같은 URL을 다시 엽니다. 최종 실패 후에도 이 정리를 수행하므로 한 Reel의 UI 계층 고착이 다음 큐 항목으로 이어지지 않으며 Android 에뮬레이터 자체는 종료하지 않습니다.

분리 Android 워커의 결과도 실행 중인 PowerShell에 `[ANDROID] collected | URL | view_count=...` 형식으로 즉시 출력합니다. 명령 실행 전에 이미 다른 터미널에서 워커가 처리 중이면 현재 수집기도 공용 `android.log`의 새 이벤트를 따라가서 표시합니다. 같은 내용의 상세 이벤트는 기존처럼 `.collector\android.log`에도 계속 기록됩니다.

Android 워커는 Reel 화면이 열린 직후 화면 중앙을 한 번 탭해 영상을 일시정지한 다음 지표 패널과 UI 계층을 읽습니다. Windows QEMU에서 영상 디코딩과 UIAutomator 검사가 겹치는 시간을 줄이기 위한 동작입니다. 마지막 작업 뒤 큐가 비어도 이미 일시정지한 영상을 다시 탭해 재생하지 않습니다.

```powershell
.\collector.ps1 --max-items 50 --background
.\collector.ps1 --max-items 50 --background --android-device-id emulator-5554
.\collector.ps1 --max-items 50 --background --android-metrics-required
.\collector.ps1 --max-items 50 --background --no-android-metrics
.\collector.ps1 fashion --collector-mode hybrid --background
.\collector.ps1 fashion --collector-mode web --background
.\collector.ps1 fashion --collector-mode android --background
.\collector.ps1 hashtag-posts --hashtag-query '오오티디 OR 패션'
.\collector.ps1 hashtag-posts --preset fashion
.\collector.ps1 hashtag-posts --preset beauty
.\collector.ps1 hashtag-posts --preset fashion-beauty
```

`--collector-mode`는 `hybrid`(기본값), `web`, `android`를 지원합니다. `hybrid`는 브라우저 수집 뒤 Android 지표를 보강하고, `web`은 수집기 전용 Playwright 브라우저만 사용하며 Android 에뮬레이터를 시작하지 않습니다. `android`는 unified 진입점에서 Android 앱 수집을 실행합니다. 기존 `--no-android-metrics`는 `web` 모드 별칭으로 유지됩니다.

`hashtag-posts`는 Android가 검색어 하나의 **Tags** 목록을 끝까지 수집하면, 다음 검색어로 넘어가기 전에 그 관련 태그들을 웹에서 하나씩 정확 일치 검색해 `media_count`를 보완합니다. 웹에 없는 태그는 Android의 축약 게시물 수를 유지하며, 이미 확인한 태그는 같은 실행 안에서 다시 웹 검색하지 않습니다. 결과는 `data_web\hashtags.csv`, `hashtags.json`, `hashtags.xlsx`에 누적됩니다.

해시태그 수집은 각 해시태그의 Reel URL 후보를 찾은 뒤 검사합니다. 카드가 늦게 로드되는 태그를 위해 초기 로딩과 스크롤 재시도를 늘렸고, 기본적으로 태그별 최대 50개 후보를 검사합니다. 더 넓게 찾으려면 `--hashtag-candidates-per-keyword 100`처럼 지정할 수 있습니다.

해시태그 `media_count` 수집은 기본적으로 실행하지 않습니다. 필요한 경우에만 `--collect-hashtag-media-count`를 붙이면 현재 키워드의 Android Instagram **Tags** 결과를 끝까지 스크롤하고 `data_web\hashtags.csv`, `hashtags.json`, `hashtags.xlsx`에 누적합니다. Tags 작업이 끝나면 추가 유휴 해시태그 작업을 만들지 않고 Reel 지표 큐를 우선 처리합니다. 이 옵션이 없으면 Tags 검색을 시작하지 않고 Reel 후보 처리로 바로 넘어갑니다. Android에 여러 에뮬레이터가 실행 중이면 `--android-device-id`가 필요합니다.

```powershell
.\collector.ps1 fashion --background --collect-hashtag-media-count
```

Tags 목록을 스크롤하는 동안에는 터미널에 새 줄이 잠시 출력되지 않을 수 있습니다. 에뮬레이터의 Instagram 검색 화면이 움직이고 있거나 아래 상태 파일의 `updated_at`·`captured` 값이 갱신되면 정상 진행 중입니다. Tags 수집이 끝난 뒤에는 저장한 Reel URL을 Android 영속 큐가 순서대로 보강하므로, 웹 수집 완료 수와 Android 완료 수가 일시적으로 다를 수 있습니다.

```powershell
Get-Content .\data_web\.datasets\fashion\collector_status.json
Get-Content .\data_web\.datasets\fashion\.collector\android_metric_queue\status.json
Get-Content .\data_web\.datasets\fashion\.collector\android.log -Tail 30
```

## 429 원인 분석 로그

`collect`, `refresh`, `fashion`, `beauty`, `fashion-beauty`와 Android metric worker는 수집 결과와 별도로 다음 파일을 생성합니다.

```text
data_web/
├── logs/instagram_collector_YYYY-MM-DD_HHMMSS.log
├── logs/instagram_events_YYYY-MM-DD_HHMMSS.jsonl
└── diagnostics/rate_limit_YYYY-MM-DD_HHMMSS.json
```

브라우저 수집기는 Instagram 페이지가 스스로 발생시킨 Playwright 응답만 수동 관찰합니다. request body, cookie, authorization/session token은 기록하지 않고 URL query도 제거합니다. 실제 응답 status가 429일 때만 `HTTP_429_CONFIRMED`를 기록합니다. 문서·Fetch·XHR 응답과 오류 응답의 host, path, status, resource type, 확인 가능한 duration은 최초 제한 snapshot에 최근 50건까지 함께 저장됩니다.

웹 Reel 탐색 시작은 60초당 최대 12개로 제한됩니다. HTTP 429가 확인되면 브라우저와 프로그램을 열린 상태로 유지하고 전체 수집을 일시정지합니다. 신규 탐색, 프로필 조회, 예약 재수집과 연결된 Android 워커의 조작도 함께 대기합니다. 후속조치를 마친 뒤 실행 터미널에 `resume`을 입력하고 Enter를 눌러야 한 번 재개합니다. 재개 후 다시 429가 발생하면 추가 대기 없이 해당 실행을 종료하고, 그 시점까지 저장된 행만 남겨 `refresh`로 재수집할 수 있게 합니다. 이미 전송된 요청과 Instagram 앱 자체의 백그라운드 통신까지 되돌리거나 중단하는 기능은 아닙니다.

Android worker는 ADB/UIAutomator로 앱 화면만 읽기 때문에 `NETWORK_STATUS_UNAVAILABLE`을 기록합니다. 앱 화면에서 `429`, `Too Many Requests`, `rate limit`, `throttled`, `Please wait a few minutes`, `Try again later`, `잠시 후 다시` 등의 문구를 발견하면 HTTP status로 단정하지 않고 `RATE_LIMIT_SUSPECTED`로 기록합니다.

미디어별 로그에는 `OPEN_REEL`, `WAIT_FOR_RENDER`, `READ_USERNAME`, 지표 읽기, `SAVE_RESULT`, `SCROLL_NEXT` 단계와 성공·실패·timeout, retry 횟수와 간격, 최근 1분·5분 처리량, foreground/background 실행 모드가 포함됩니다. 정상·오류 종료 모두 collector summary를 출력합니다. 브라우저와 Android worker가 별도 프로세스로 실행되면 같은 초의 파일명에는 충돌 방지 숫자 suffix가 붙으며 각 이벤트의 `component`로 구분할 수 있습니다.

```powershell
Get-Content .\data_web\logs\instagram_collector_*.log -Tail 80
Get-Content .\data_web\logs\instagram_events_*.jsonl -Tail 20
Get-Content .\data_web\diagnostics\rate_limit_*.json
```

`beauty` 실행은 위 경로의 `fashion`을 `beauty`로 바꿔 확인합니다. `UIAutomator returned an empty or unreadable screen` 또는 `ADB command timed out`가 반복되면 에뮬레이터 잠금, Instagram 팝업, 앱 로그인 상태와 ADB 연결을 확인합니다.

웹 수집 단계는 각 Reel 상세 페이지의 embedded JSON·완전한 DOM 정수·작성자 프로필을 이용해 가능한 필드를 먼저 모두 수집합니다. Python이 얻은 완전한 정수와 문자열은 그대로 유지하며, `view_count`, `like_count`, `comment_count`, `share_count`, `saved_count`, `audio_name` 중 값이 없거나 `1.2K`·`3만` 같은 축약 표시뿐인 필드만 Android 결과로 보강합니다. `repost_count`는 Python이 담당하며 Android가 덮어쓰지 않습니다. Android 보강이 켜진 경우에는 웹 응답의 `play_count` 누락만으로 릴스를 버리지 않습니다. 양쪽 모두 값을 노출하지 않으면 `0`으로 추정하지 않고 빈 값으로 둡니다. `--no-android-metrics` 브라우저 전용 모드에서는 기존처럼 정확한 웹 지표가 없는 후보를 저장하지 않습니다.

작성자 팔로워 수와 프로필 정보는 Python 웹 프로필 수집기가 담당합니다. Android 보강은 사용자 프로필을 읽거나 웹의 URL·캡션·해시태그·위치·업로드일을 덮어쓰지 않습니다. Instagram이 리포스트·공유·저장 집계를 표시하지 않으면 `0`으로 추정하지 않고 빈 값으로 둡니다.

일반 로그인 신규 Reel 수집은 저장 조건을 통과한 릴스를 먼저 보존한 뒤, 현재 Reel의 작성자 프로필 링크(프로필 사진 또는 작성자 제목)를 클릭합니다. 캡션의 계정 태그는 클릭 후보에서 제외합니다. 이동한 프로필 URL과 응답의 user ID가 작성자와 일치할 때 기존 정확값 판독기로 팔로워 수 및 users 정보를 저장하고 다음 릴스로 진행합니다. 같은 실행에서 검증한 동일 ID·username의 프로필 결과는 재사용합니다. 프로필 확인이 실패해도 저장한 Reel과 Android 작업은 유지됩니다. 프로필 응답에서 ID를 검증할 수 없는 경우에도 추정값을 쓰지 않습니다. 패션·뷰티 예약 수집은 5개 해시태그 묶음의 Reel 수집을 먼저 마친 뒤, 그 묶음에서 저장한 Reel 작성자를 중복 제거해 팔로워 수를 수집하고 결과를 저장한 다음 다음 해시태그 묶음으로 넘어갑니다.

실패한 프로필 조회는 데이터셋의 `.collector/author_profile_pending.json`에 원본 Reel URL, 실패 이유, 시도 횟수와 재시도 시각을 저장합니다. 다음 신규 수집 호출 시작 시 도래한 작업을 최대 5건 재시도합니다. 일반 실패는 60초 이후 다시 시도하며 3회 실패하면 `needs_review`로 남깁니다. 429 uses the manual pause described above; enter `resume` to continue. 이 파일은 프로그램 재시작 후에도 유지되며 독립적인 예약 워커는 아닙니다. 기존에 잘못 저장된 행 전체를 자동 복구하는 기능은 아닙니다. +4시간 URL 재수집과 `followers` 명령은 기존 users 조회 경로를 유지합니다.
터미널의 릴스 진행 표시는 실제로 저장된 릴스만 `[현재 저장 수/목표] URL` 형식으로 카운트합니다.
`[METRIC]` 디버그 줄은 콘솔에 출력하지 않습니다. 원본 필드 검증은 수집 내부에서 유지합니다.
`collection_label` 열은 만들지 않습니다. 같은 `data_web` 폴더에서 여러 수집기를 동시에 실행하면 파일 잠금 오류가 발생하도록 보호되어 있습니다.

패션 고반응 릴스 자동 분석

패션 데이터셋의 `reaction_rate`가 9000% 이상(내부 저장값 90.0 이상)인 릴스는 수집이 끝난 뒤 Gemini 분석기로 자동 전달됩니다. 분석 작업은 최대 4개까지 병렬 실행되며 수집 스케줄을 막지 않습니다. 같은 URL은 성공한 분석 결과를 다시 실행하지 않습니다.

분석 결과와 처리 상태는 다음 파일에 저장됩니다.

```text
data_web/fashion_reel_analyses.json
data_web/.datasets/fashion/.collector/fashion_analyzer_state.json
data_web/fashion_reel_insights.json
data_web/fashion_reel_insights.html
data_web/.datasets/fashion/.collector/fashion_reel_insights_state.json
```

누적 분석 결과 중 새 영상이 기본 10개 이상 쌓이면 `gemini-3.6-flash` 메타 분석이 공용 시트 풀을 통해 10개씩 실행됩니다. 영상별 특징과 scene·movement·hook 공통점을 정규화하고, 건수와 비율을 막대그래프로 표시한 HTML 보고서를 생성합니다. 이미 처리한 영상은 원본 분석이 바뀌지 않는 한 다시 전송하지 않습니다. 실행 간격은 `FASHION_INSIGHTS_BATCH_SIZE`, 모델은 `FASHION_INSIGHTS_MODEL` 환경 변수로 변경할 수 있습니다. 메타 분석 실패는 수집 및 개별 Reel 분석을 중단시키지 않습니다.

기본 `fashion --background` 실행에 이 동작이 포함됩니다. Gemini 분석기 가상환경을 별도로 사용하려면 `FASHION_ANALYZER_PYTHON` 환경 변수에 해당 Python 실행 파일 경로를 지정하세요. API 키·모델 풀 설정은 `analyzers/gemini/.env`를 사용합니다.

## 패션·뷰티 승인 수집

세 가지 예약 수집 명령을 제공합니다.

```powershell
.\collector.ps1 fashion          # 패션만 30분마다 수집
.\collector.ps1 beauty           # 화장품·뷰티만 30분마다 수집
.\collector.ps1 fashion-beauty   # 패션과 화장품·뷰티를 30분마다 교대 수집
```

저장된 Instagram 로그인 프로필을 사용해 브라우저 창 없이 실행하려면 각 명령 뒤에 `--background`를 붙입니다.
처음 로그인할 때는 이 옵션을 빼고 실행한 뒤, 이후 실행부터 사용합니다.

```powershell
.\collector.ps1 fashion --background
.\collector.ps1 beauty --background
.\collector.ps1 fashion-beauty --background
```

최초 수집할 Reel의 업로드 경과일을 바꾸려면 `--maxdays`를 사용합니다. 예를 들어 최근 14일 이내만
수집하려면 `.\collector.ps1 fashion-beauty --background --maxdays 14`를 실행합니다.

기본 실행 시간은 16시간입니다. 신규 탐색 시간은 전체 실행 시간에서 12시간을 뺀 값이며,
기본 실행에서는 처음 4시간 동안 내장 키워드를 5개씩 묶어 순환 검색합니다. 한 묶음에서
해시태그당 최대 50개 후보 검사가 정상 종료되면 30분 창이 끝나기를 기다리거나 같은 묶음을 반복하지 않고
즉시 다음 5개 키워드 묶음으로 이동합니다. 수집 실패나 429 재시도에서는 현재 묶음을 유지합니다.
`fashion`과 `beauty`는 해당 도메인을 매 창 수집하고, `fashion-beauty`는 패션과
화장품·뷰티를 창마다 교대합니다. 각 키워드에서 릴스 후보를 최대 50개 확보하며, 신규 수집은 창마다 최대 300개까지 저장합니다.
최초 수집 후보에는 업로드 후
30일 이내 필터를 적용합니다. 각 Reel은 최초 수집 후 `+4시간`, `+8시간`, `+12시간`에 재수집하여
최초 수집을 포함한 총 4개 시점의 스냅샷을 만듭니다. 나머지 시간에는 신규 탐색 없이 기한이 된
재수집을 마무리합니다. 같은 도메인의 기한 재수집 URL은 최대 50개씩 한 브라우저 세션에서 처리하고,
패션·뷰티의 기한이 겹치면 로그인 브라우저의 독립 탭에서 두 묶음을 병렬 처리합니다. 이번 실행에서
생긴 모든 Reel의 예약된 4시간 간격 재수집이 끝나면 16시간을 기다리지 않고 정상 종료합니다.
옵션을 모두 명시한 같은 실행은 다음과 같습니다.

```powershell
.\collector.ps1 fashion-beauty --duration-hours 16 --discovery-hours 4 --new-items-per-window 300 --max-new-items-per-window 300 --max-upload-age-days 30 --discovery-interval-minutes 30
```

현재 기본값을 요약하면 다음과 같습니다.

| 항목 | 기본값 |
|---|---:|
| 전체 실행 시간 | 16시간 |
| 신규 탐색 시간 | 전체 실행 시간 - 12시간 (기본 16시간 실행 시 4시간) |
| 신규 탐색 간격 | 30분 |
| 활성 키워드 | 5개씩 처리 후 다음 묶음으로 즉시 이동 |
| 키워드당 후보 | 최대 50개 |
| 창당 신규 저장 | 최대 300개 |
| 최초 수집 업로드 범위 | 최근 30일 |
| 같은 Reel 재수집 | 최초 수집 후 +4시간, +8시간, +12시간(최초 포함 총 4회) |

예를 들어 아래 명령은 **전체 16시간 실행은 유지하면서 시작 후 4시간 동안만 신규 Reel을 탐색**합니다. 별도의 터미널을 30분마다 여는 방식이 아니라 하나의 실행 프로세스가 30분 창을 순환하며, 신규 탐색이 끝난 뒤에도 실행 종료 전까지 예약된 4시간 간격 재수집을 계속합니다.

```powershell
.\collector.ps1 fashion --background --discovery-hours 4
```

키워드당 후보 수는 `--hashtag-candidates-per-keyword`, 최초 수집 업로드 범위는 `--maxdays`로 바꿀 수 있습니다.

```powershell
.\collector.ps1 fashion --background --hashtag-candidates-per-keyword 30 --maxdays 14
```

프로필 이동·지표 수집 로직을 시험할 때만 패션 해시태그 하나로 고정하려면 아래처럼 실행합니다. 이 옵션은 기본 예약 수집의 5개 해시태그 순환을 바꾸지 않습니다.

```powershell
.\collector.ps1 fashion --test-single-hashtag --background
```

패션·뷰티 내장 키워드(각 48개)를 유지한 채 **6시간 동안 신규 Reel만** 수집하고, 재수집 없이
기본 `data_web\reels.*`와 `data_web\users.*`에 바로 누적하려면 아래의 단일 옵션을 사용합니다.
최근 365일 이내 업로드된 후보만 대상으로 하며, 키워드를 5개씩 검색하고 후보 검사가 끝나면 다음 5개로 이동합니다.
키워드당 최대 50개(최대 250개) 후보를 모두 조건 검사하고, 조건을
통과한 신규 Reel을 최대 250개 저장합니다. 패션과 뷰티는 30분마다 교대하며, 각 도메인의 다음
5개 키워드 그룹으로 넘어가므로 각 도메인의 검색 키워드도 순환합니다.

```powershell
.\collector.ps1 fashion-beauty --six-hour-new-only --background
```

`--six-hour-new-only`는 `--duration-hours 6 --new-only --base-output --maxdays 365 --new-items-per-window 250 --max-new-items-per-window 250`를 한 번에 적용합니다.
이 프리셋에서는 재수집 작업을 만들거나 실행하지 않습니다. `--background`는
저장된 로그인 프로필이 있을 때만 추가하세요. 처음 로그인할 때는 빼고 실행하면 됩니다.
Android 보강 모드에서는 웹에서 확인한 Reel을 먼저 저장합니다. 로그인한 신규 수집의 팔로워 조회 실패는 Reel 저장을 취소하지 않고 위의 프로필 재시도 기록으로 남깁니다. 리포스트 집계가 없는 경우에는 빈 값으로 보존합니다.

필요하면 같은 동작을 세부 옵션으로도 지정할 수 있습니다.

```powershell
.\collector.ps1 fashion-beauty --duration-hours 6 --new-only --base-output --maxdays 365 --new-items-per-window 250 --max-new-items-per-window 250 --background
```

`--fashion-hashtag-query`와 `--beauty-hashtag-query`로 선택한 도메인의 내장 키워드를 바꿀 수 있습니다.
각 명령은 선택한 도메인의 `data_web\fashion_reels.xlsx`, `data_web\fashion_users.xlsx`,
`data_web\beauty_reels.xlsx`, `data_web\beauty_users.xlsx` 같은 CSV·JSON·XLSX 및 상태 파일만
게시하며, 기존 기본 `reels.*`와 `users.*`는 건드리지 않습니다. 도메인별 공개 출력의
Reel 파일은 최초 수집과 재수집을 각각 별도 행으로 추가하므로, 기존 행은 바뀌지 않습니다. 재수집 경과 시간은
`hours_since_previous`로 표시됩니다. 새 실행은 이전 실행에서 이미 기한을 넘긴 재수집을 즉시 연속 처리하지 않고,
이번 실행에서 최초 수집한 Reel을 `+4시간` 간격으로 재수집합니다. Ctrl+C를 한 번 누르면 실행을 중단하고
마지막 체크포인트까지 저장된 출력을 보존합니다.

일시적 수집 오류로 예약 작업이 실패하면 같은 요청을 즉시 반복하지 않고 `5분`, `10분`, `20분`, `30분`
순서로 대기합니다. 같은 작업이 한 실행에서 5회 연속 실패하면 남은 실행 동안 재시도를 보류합니다.
실행 중 Instagram `429`가 감지되면 예약 재수집도 전체 일시정지에 포함됩니다.
일시정지 중에는 예약 호출의 제한 시간 때문에 브라우저를 닫지 않습니다.
터미널의 `resume` + Enter 입력으로 한 번만 수집 동작을 다시 허용합니다. 재개 뒤 429가 재발하면 실행을 종료하며, 직전까지 저장된 행은 `refresh` 명령으로만 재수집할 수 있습니다.

## 공개 출력 동기화

`reels.xlsx`가 열려 있어 생성된 `reels_updated.xlsx`가 남아 있으면 아래 명령으로 내부 원본 이력과 비교합니다. 누락된 릴스·수집 시점·빈 값만 보완한 뒤 `reels.csv`·`reels.json`·`reels.xlsx`와 사용자 공개 파일을 다시 만들고, 성공한 경우에만 `reels_updated.xlsx`를 삭제합니다.

```powershell
.\collector.ps1 reconcile
```

## 기존 데이터 정확값 재수집

기존 `reels.xlsx`의 모든 릴스를 로그인 세션으로 다시 조회하면 새 `2nd collect_*` 등의 열에 정확한 최신 정수가 기록됩니다. 기존 축약값은 원래 자릿수를 역산할 수 없으므로 재수집해야 합니다.

```powershell
.\collector.ps1 refresh --background
.\collector.ps1 followers
```

`--followers-after-reels`는 신규 Reel 수집 중 작성자를 기록해 두었다가 Reel 탐색이 끝난 뒤 중복 제거하여 일괄 조회합니다. 패션·뷰티 예약 수집은 이 옵션을 사용하므로 기본 5개 해시태그 묶음마다 실행됩니다. URL 재수집에서는 기존 의미를 유지합니다.
`--direct-concurrency`도 기존 명령 호환을 위해 허용하지만, 정확한 shortcode 연결을 위해 실제 재수집 동시성은 1로 고정됩니다.
`--hashtag-query`, `--urls-file`, `--followers-only`, `--max-upload-age-days`, `--page-recycle-items`, `--checkpoint-items` 등의 옵션도 사용할 수 있습니다. `--direct-reel-info-wait-seconds`와 정확 지표 재시도 옵션은 이전 수집기 호환을 위해 남아 있지만 기본 수집 경로에서는 별도 endpoint를 호출하지 않습니다.

## 로그인 없이 기존 릴스 재수집

기존 로그인 수집 이력이 있는 URL만 공개 임시 브라우저로 다시 측정하려면 다음 명령을 사용합니다.

```powershell
.\collector.ps1 refresh --no-login --background
```

이 모드는 같은 URL의 기존 `user_id`, 캡션, 해시태그, BGM, 위치, 광고 여부, 업로드 시각을 보존하고,
새 `collected_at`과 공개 Network Response에서 확인된 조회·좋아요·댓글·리포스트 원본 정수만 새 수집 열에 기록합니다.
`reels.xlsx`에는 기존과 새 수집의 경과일 및 `1,100(+100)` 같은 증감 표기가 자동으로 보입니다.
공개 원본 정수를 확인하지 못한 지표는 이전 값을 복사하거나 축약 표기를 풀지 않고 새 수집 칸을 비워 둔 채 다음 URL로 넘어갑니다.

팔로워도 현재 Reel 페이지의 embedded/DOM 데이터 또는 같은 페이지의 Response에 정확한 정수가 있을 때만 기록합니다.
해당 페이지에 정확값이 없으면 별도 프로필 요청을 만들지 않고 새 수집 칸을 비운 채 다음 Reel로 넘어갑니다.
로그인 필요·403·429·필드 누락은 재시도해도 권한이 생기지 않으므로 즉시 빈칸 처리합니다. 이 모드는 BGM을 새로 수집하지 않고
기존 값을 유지하며, 이전 내부 이력(`data_web\.collector\reels_history_active.csv`)이 없는 URL은 보존할 원본 정보가 없어 저장하지 않습니다.

## 통합 실행 스크립트

```powershell
.\collector.ps1 --max-items 50 --background
.\collector.ps1 refresh --background
.\collector.ps1 followers
.\collector.ps1 fashion-beauty
```

## 해시태그 직접 수집 예시

해시태그는 `OR`로 묶어 입력합니다. 패션과 화장품을 한 번에 섞기보다 아래처럼 주제별로
나눠 실행하면 각 분야의 수집량을 조절하기 쉽습니다.

```powershell
# 패션 기본
.\collector.ps1 --hashtag-query '패션 OR 데일리룩 OR 오오티디 OR ootd OR 코디추천 OR 패션스타그램' --max-items 100 --background --interval-seconds 1

# 패션 스타일·상황별 코디
.\collector.ps1 --hashtag-query '여자코디 OR 남자코디 OR 출근룩 OR 하객룩 OR 미니멀룩 OR 스트릿패션' --max-items 100 --background --interval-seconds 1

# 화장품·메이크업
.\collector.ps1 --hashtag-query '화장품 OR 뷰티 OR 메이크업 OR 화장품추천 OR 뷰티스타그램 OR kbeauty' --max-items 100 --background --interval-seconds 1

# 스킨케어·제품 추천
.\collector.ps1 --hashtag-query '스킨케어 OR 기초화장품 OR 피부관리 OR 수분크림 OR 선크림추천 OR 클렌징' --max-items 100 --background --interval-seconds 1
```

후보가 끝난 뒤에도 새 릴스를 계속 찾으려면 `--max-items 0`을 사용합니다. 예를 들어
최근 7일 이내 후보를 계속 저장하려면 다음처럼 실행합니다. 현재 후보를 모두 처리하면 60초 뒤
같은 해시태그를 다시 검색하며, 실행 중 이미 시도한 URL은 건너뜁니다. `--background` 실행 창에서
`q`를 입력하고 Enter를 누르거나 Ctrl+C를 한 번 누르면 현재까지의 결과를 저장하고 중단합니다.

해시태그 후보는 상세 페이지를 열기 전에 자동으로 사전 필터링됩니다. 기존 수집 이력상
재수집 대기 중인 URL과 기존 수집 이력의 `uploaded_at`으로 업로드 기간 초과가 확인된 URL은 즉시 제외합니다.
검색 화면에서 실제로 보이는 릴스 카드만 후보로 삼습니다. 검색 카드에 노출된 사실을 분류 기준으로 사용하므로,
상세 페이지 캡션의 해시태그가 질의와 달라도 후보에서 제외하지 않습니다.
새 후보는 캡션 **더보기**를 연 뒤 화면에 표시되는 시간을 우선 사용합니다. 화면 시간 요소가 없거나 해석할 수 없으면 해당 릴스의 Network Response 날짜를 보조값으로 사용합니다.
화면 시간과 Network Response 날짜를 모두 확인할 수 없으면 `업로드 날짜 미확인으로 저장 안 함`으로 표시합니다.

```powershell
.\collector.ps1 --hashtag-query '패션 OR 데일리룩 OR 오오티디 OR ootd OR 코디추천 OR 패션스타그램' --max-items 0 --max-upload-age-days 7 --background --interval-seconds 1
```

첫 로그인 전에는 `--background`를 빼고 실행합니다.

포그라운드 예약 수집은 실행 후 처음 표시되는 로그인 준비 확인에서만 Enter를 누르면 됩니다. 같은 Python 프로세스가 계속 실행되는 동안에는 이후 수집 창, 브라우저 재생성, 429 대기 후 재개와 공유 브라우저 작업이 최초 승인을 자동으로 재사용합니다. 프로그램을 완전히 종료한 뒤 새로 실행하면 로그인 상태 확인을 위해 Enter를 다시 한 번 요청합니다.

## 테스트

### 에뮬레이터 재실행 (Windows)

`collector.ps1`은 명령 실행 시 온라인 상태인 Android 에뮬레이터를 자동으로
인식해 사용합니다. `Pixel_8`과 `Pixel_8_clean`을 명령어로 구분할 필요가 없습니다.
온라인 장치가 없을 때만 `Pixel_8_clean`을 기본 AVD로 실행하며 사용자 데이터와
Instagram 로그인을 초기화하지 않습니다.

에뮬레이터 실행, 부팅 대기, Instagram 확인, pending 큐 처리를 한 명령으로 실행:

```powershell
.\collector.ps1 android-worker --data-dir .\data_web\.datasets\fashion
```

```powershell
.\scripts\start-android.ps1
```

두 명령은 대안이며 동시에 실행하지 않습니다. 2026-09-07 검증에서 일부 릴스의
수집은 성공했으나 장시간 안정성은 아직 검증 중입니다. 장치 연결 대기 중에는
pending 작업을 꺼내거나 해당 작업의 재시도 횟수를 소진하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -B -m unittest tests.test_instagram_reels_browser tests.test_instagram_collector
```
