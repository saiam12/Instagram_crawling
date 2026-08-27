# Python Instagram Reels 수집기

기존 JavaScript·PowerShell 구현은 상위 프로젝트 폴더에 그대로 두고, Python 구현만 이 폴더에 분리했습니다. Python 수집기는 자체 로그인 프로필과 `data_web` 출력 폴더를 사용하므로 기존 버전의 데이터와 섞이지 않습니다.

수집 항목에는 조회수(`view_count`)가 포함됩니다. 릴스 공개 출력은 한 릴스가 한 행을 사용하고, 재수집 값은 `2nd collect_*` 같은 새 열에 저장됩니다. 사용자 공개 CSV·엑셀은 재수집마다 새 행을 추가합니다.

## 설치

PowerShell에서 이 폴더로 이동한 뒤 가상환경과 패키지를 설치합니다.

```powershell
cd C:\Instagram-crawling\python_version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

설치된 Microsoft Edge 또는 Google Chrome을 사용하므로 별도의 `playwright install` 명령은 필요하지 않습니다.

기존 `.venv`의 패키지 또는 Python 연결이 꼬였을 때는 다음 명령으로 Python 3.12 환경만 복구합니다. 수집 결과와 로그인 프로필은 유지됩니다.

```powershell
.\repair_venv.ps1
```

## 폴더 구조

| 경로 | 용도 |
|---|---|
| `collectors` | Python 릴스 및 팔로워 수집 코드 |
| `exporters` | CSV·JSON·XLSX 저장 코드 |
| `scripts` | 실행 목적별 진입점 |
| `data_web` | 실제 수집 결과(처음 실행할 때 자동 생성) |
| `.instagram_browser_profile` | Python 버전 전용 로그인 프로필(자동 생성) |
| `examples` | 출력 예시와 검증 자료 |

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

모든 출력에는 `view_count`가 포함됩니다.

조회수·팔로워 수·좋아요·댓글·리포스트는 Instagram Response의 원본 정수만 저장합니다.
화면의 `2.4만`, `36K`, `1.6M` 같은 축약 표기는 손실된 자릿수를 복원할 수 없으므로 사용하지 않습니다.
각 릴스는 상세 화면이 안정될 때까지 기본 3초를 기다린 뒤 로그인된 브라우저 안에서 Instagram 내부 `media info` 읽기 요청을 실행합니다.
지표가 빠졌거나 일시적 요청 오류가 나면 기본 3회까지 `2초`, `4초` 간격으로 다시 확인합니다.
`--direct-reel-info-wait-seconds 3`과 `--exact-metric-attempts 3`처럼 조절할 수 있으며, 대기 시간을 `0`으로 설정하면 즉시 재시도합니다.
여기서 대상 shortcode의 `play_count`, 좋아요·댓글·리포스트 원본 정수를 모두 받으면 느린 상세 페이지
재탐색과 작성자 Reels 스크롤을 생략합니다. 이 요청은 유료 Graph API가 아니므로 API 키나 건당 요금이
없지만, Instagram의 요청 제한은 적용될 수 있습니다. 직접 요청이 실패하거나 필드가 빠지면 작성자
Reels GraphQL과 릴스 상세 응답을 사용하는 기존 방식으로 자동 전환합니다. 프로필 팔로워 조회의 일시적 웹 오류도
동일한 횟수만큼 새 페이지에서 재시도합니다. 401·403·429·로그인·챌린지 제한은 반복 요청하지 않습니다. `media info`의
`item.user.follower_count`가 정확한 0 이상의 정수로 확인되면 그 값을 릴스 저장에 바로 사용합니다.
저장된 작성자는 백그라운드 `web_profile_info` 조회로 정확 팔로워 수, 전체 `post_count`(게시물 수),
`biography`(프로필 소개글), `profile_category`(예: `의류(브랜드)`)를 `users.csv`·`users.xlsx`에 기록합니다.
이 추가 조회가 실패해도 프로필 화면에 축약되지 않은 전체 정수(예: `팔로워 3230`, `게시물 2078`)가 보이면 그 값을 자동으로 사용합니다.
`1.6만`·`3.2K`처럼 원래 정수를 알 수 없는 축약 표기는 사용하지 않습니다.
정확값을 얻지 못한 후보는 저장하거나 `--max-items` 완료 개수에 포함하지 않고 다음 후보로 넘어갑니다.
터미널의 릴스 진행 표시는 실제로 저장된 릴스만 `[현재 저장 수/목표] URL` 형식으로 카운트합니다.
`[METRIC]` 디버그 줄은 콘솔에 출력하지 않습니다. 원본 필드 검증은 수집 내부에서 유지합니다.
`collection_label` 열은 만들지 않습니다. 같은 `data_web` 폴더에서 여러 수집기를 동시에 실행하면 파일 잠금 오류가 발생하도록 보호되어 있습니다.

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

기본 실행 시간은 16시간입니다. 처음 8시간 동안 각 활성 30분 창에서 내장 키워드 5개를 순환해
검색합니다. `fashion`과 `beauty`는 해당 도메인을 매 창 수집하고, `fashion-beauty`는 패션과
화장품·뷰티를 창마다 교대합니다. 각 키워드에서 릴스 후보를 최대 50개 확보하며, 신규 수집은 창마다 최대 300개까지 저장합니다.
최초 수집 후보에는 업로드 후
30일 이내 필터를 적용합니다. 각 Reel은 최초 수집과 `+30분`, `+1시간`, `+2시간`, `+4시간`,
`+8시간` 재수집을 합쳐 최대 여섯 개 스냅샷을 남깁니다. 나머지 8시간에는 신규 탐색 없이 기한이 된
재수집을 마무리합니다. 옵션을 모두 명시한 같은 실행은 다음과 같습니다.

```powershell
.\collector.ps1 fashion-beauty --duration-hours 16 --discovery-hours 8 --new-items-per-window 300 --max-new-items-per-window 300 --max-upload-age-days 30 --discovery-interval-minutes 30
```

패션·뷰티 내장 키워드(각 48개)를 유지한 채 **6시간 동안 신규 Reel만** 수집하고, 재수집 없이
기본 `data_web\reels.*`와 `data_web\users.*`에 바로 누적하려면 아래의 단일 옵션을 사용합니다.
최근 365일 이내 업로드된 후보만 대상으로 하며, 한 활성 30분 창에서 키워드 5개를 검색합니다.
키워드당 최대 50개(최대 250개) 후보를 모두 조건 검사하고, 조건을
통과한 신규 Reel을 최대 250개 저장합니다. 패션과 뷰티는 30분마다 교대하며, 각 도메인의 다음
5개 키워드 그룹으로 넘어가므로 활성 창마다 먼저 검색하는 키워드도 순환합니다.

```powershell
.\collector.ps1 fashion-beauty --six-hour-new-only --background
```

`--six-hour-new-only`는 `--duration-hours 6 --new-only --base-output --maxdays 365 --new-items-per-window 250 --max-new-items-per-window 250`를 한 번에 적용합니다.
이 프리셋에서는 `+30분`·`+1시간` 등의 재수집 작업을 만들거나 실행하지 않습니다. `--background`는
저장된 로그인 프로필이 있을 때만 추가하세요. 처음 로그인할 때는 빼고 실행하면 됩니다.
정확 지표는 기본 3회 확인하며, 필요하면 `--exact-metric-attempts 5 --exact-metric-retry-delay-seconds 3`을
명령 끝에 붙여 더 보수적으로 설정할 수 있습니다.
Instagram 응답에 리포스트 집계 필드가 없으면 리포스트 수는 `0`으로 저장하며, 조회·좋아요·댓글·팔로워 수의
정확한 정수값이 없을 때만 해당 Reel 저장을 건너뜁니다.

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
이번 실행에서 최초 수집한 Reel을 `+0.5`, `+1`, `+2`, `+4`, `+8시간`에 재수집합니다. Ctrl+C를 한 번 누르면 실행을 중단하고
마지막 체크포인트까지 저장된 출력을 보존합니다.

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

`--followers-after-reels`는 기존 명령 호환을 위해 허용하지만, 정확값 전용 모드에서는 저장 전에 조회수와 팔로워 수를 모두 확인합니다.
`--direct-concurrency`도 기존 명령 호환을 위해 허용하지만, 정확한 shortcode 연결을 위해 실제 재수집 동시성은 1로 고정됩니다.
`--hashtag-query`, `--urls-file`, `--followers-only`, `--max-upload-age-days`, `--direct-reel-info-wait-seconds`, `--page-recycle-items`, `--checkpoint-items` 등의 옵션도 사용할 수 있습니다.

## 로그인 없이 기존 릴스 재수집

기존 로그인 수집 이력이 있는 URL만 공개 임시 브라우저로 다시 측정하려면 다음 명령을 사용합니다.

```powershell
.\collector.ps1 refresh --no-login --background
```

이 모드는 같은 URL의 기존 `user_id`, 캡션, 해시태그, BGM, 위치, 광고 여부, 업로드 시각을 보존하고,
새 `collected_at`과 공개 Network Response에서 확인된 조회·좋아요·댓글·리포스트 원본 정수만 새 수집 열에 기록합니다.
`reels.xlsx`에는 기존과 새 수집의 경과일 및 `1,100(+100)` 같은 증감 표기가 자동으로 보입니다.
공개 원본 정수를 확인하지 못한 지표는 이전 값을 복사하거나 축약 표기를 풀지 않고 새 수집 칸을 비워 둔 채 다음 URL로 넘어갑니다.

팔로워는 `media info`의 `item.user.follower_count`가 정확한 0 이상의 정수이면 바로 기록하며 프로필 조회를 생략합니다.
이 값이 없거나 정확한 정수가 아닐 때만 `web_profile_info`의 `edge_followed_by.count`를 확인합니다. 이 요청이 실패해도
프로필 화면에 축약되지 않은 전체 팔로워 정수가 보이면 자동으로 그 값을 사용합니다. 무로그인 권한으로
확인할 수 없으면 `web_error`일 때만 최대 3회 재시도한 뒤 해당 새 수집 칸을 비워 두고 다음 릴스로 계속 진행합니다.
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

## 테스트

```powershell
.\.venv\Scripts\python.exe -m unittest collectors.test_instagram_reels_browser exporters.test_instagram_collector
```
