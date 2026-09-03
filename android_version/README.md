# Android Emulator Instagram Reels 수집기

Android Studio 에뮬레이터에서 이미 로그인된 Instagram 앱의 **화면에 표시되는 정보만** 읽는 독립 수집기입니다. ADB와 UIAutomator로 앱을 열고, 검색·릴스 상세 화면·스크롤·공유 링크 복사만 자동화합니다. 좋아요, 팔로우, 댓글, 게시, 메시지 전송 같은 계정 행동은 수행하지 않습니다. 신규 릴스 수집과 URL이 있는 기존 릴스 재수집을 지원합니다.

`python_version`, `python_no_login_version`과 소스·로그인 프로필·출력 파일을 공유하지 않습니다.

## 설치

Android Studio의 에뮬레이터를 실행하고 Instagram 앱에서 먼저 로그인합니다. 이 폴더에서 전용 가상환경을 만듭니다.

```powershell
cd C:\Instagram-crawling\android_emulator_version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

수집기는 `C:\Users\<사용자>\AppData\Local\Android\Sdk\platform-tools\adb.exe`를 자동으로 찾습니다. SDK를 다른 위치에 설치했다면 각 명령에 `--adb-path 'D:\Android\Sdk\platform-tools\adb.exe'`를 추가하세요.

에뮬레이터가 두 개 이상 실행 중이면 `--device-id`를 지정합니다.

```powershell
& 'C:\Users\choi\AppData\Local\Android\Sdk\platform-tools\adb.exe' devices -l
.\collector.ps1 feed --device-id emulator-5554 --max-items 10
```

## 실행

`python_version`의 신규 수집 명령과 옵션 이름을 최대한 맞췄습니다. 명령어를 생략하면 `collect`입니다.

### 릴스 피드 신규 수집

현재 Instagram 앱을 열고 릴스 탭을 누른 뒤, 중복되지 않는 화면을 최대 지정 개수까지 스크롤 수집합니다.

```powershell
.\collector.ps1 feed --max-items 50
```

동일한 Python 호환 명령은 다음과 같습니다.

```powershell
.\collector.ps1 collect --max-items 50 --interval-seconds 0.4
```

### 해시태그 기반 릴스

`OR`로 여러 해시태그를 넣으면 같은 수집 상한 안에서 해시태그별로 나누어 수집합니다. `#`은 있어도 되고 없어도 됩니다. 수집기는 Instagram 해시태그 딥링크를 직접 열므로 Android 키보드의 한글 전환이나 클립보드 붙여넣기에 의존하지 않습니다.

```powershell
.\collector.ps1 collect --hashtag-query '패션 OR ootd OR 데일리룩' --max-items 50
```

기존 `hashtag --hashtag` 명령도 호환용 별칭으로 계속 사용할 수 있습니다.

내장 패션·뷰티 키워드 목록을 별도로 입력하지 않고 일회성 신규 수집에 사용할 수도 있습니다.

```powershell
.\collector.ps1 collect --fashion --max-items 50
.\collector.ps1 collect --beauty --max-items 50
.\collector.ps1 collect --fashion --beauty --max-items 100
.\collector.ps1 collect --fashion --keywords-per-run 3 --max-items 60
```

`--fashion`/`--beauty`는 한 번에 앞의 5개 내장 해시태그만 사용합니다. 따라서 `--max-items 50`이면 해시태그당 최대 약 10개의 신규 릴스를 연속으로 수집한 뒤 다음 해시태그로 넘어갑니다. 이미 저장된 릴스를 만나도 그 릴스는 재수집하지 않고 다음 릴스로 계속 넘깁니다. 같은 기존 릴스만 반복되는 경우에만 해당 해시태그를 끝내고 다음 태그로 이동합니다. `--keywords-per-run`으로 이번 실행에 사용할 내장 해시태그 수를 조절할 수 있으며, `--max-items`는 모든 해시태그를 합친 총 수집 한도입니다.

### 수집 필드 실시간 확인

`--verbose-progress`(별칭: `--show-collected-data`)를 붙이면 저장된 릴스마다 터미널에 모든 릴스·프로필·지표 필드를 출력합니다. 각 값은 `collected(...)`, 화면상 확인 불가인 경우 `unavailable`로 표시됩니다. `10K`·`1.2M` 같은 축약 표기는 각각 `10,000`·`1,200,000`으로 환산해 저장하므로, 정확 정수가 아닌 표시 기반 값일 수 있습니다.

```powershell
.\collector.ps1 collect --fashion --max-items 50 --verbose-progress
```

### 더 빠른 단일 에뮬레이터 수집

`--fast`는 단순히 대기 시간을 낮추는 옵션이 아닙니다. 같은 실행 중 같은 작성자가 다시 나오면 이미 읽은 프로필·팔로워·계정 국가 값을 재사용하여 프로필과 `About this account` 화면을 다시 열지 않습니다. 또한 릴스와 좋아요·조회수 패널의 PNG 캡처 ADB 왕복을 생략하고, 검증에 필요한 원본 UI XML은 그대로 저장합니다. 좋아요·조회수 상세 패널, 공유 링크, 캡션·업로드 날짜, 댓글 확인은 계속 수행합니다.

```powershell
.\collector.ps1 collect --fashion --max-items 50 --fast --verbose-progress
```

따라서 같은 작성자가 많은 해시태그 수집에서 특히 빠릅니다. 기본 모드(옵션 없음)는 XML과 PNG를 모두 보존합니다. 한 대의 Android 앱 화면은 순서대로만 조작할 수 있으므로, 여러 에뮬레이터를 동시에 쓰는 진짜 병렬 수집은 별도 장치별 작업 분할이 필요합니다.

### 패션·뷰티 신규 수집 예약

```powershell
.\collector.ps1 fashion --background
.\collector.ps1 beauty --background
.\collector.ps1 fashion-beauty --six-hour-new-only --background
```

`fashion`, `beauty`, `fashion-beauty`는 `python_version`의 키워드 목록을 사용해 지정 간격마다 **신규 릴스만** 탐색합니다. 기본 실행은 16시간·30분 간격이며, `--duration-hours`, `--discovery-interval-minutes`, `--new-items-per-window`, `--max-new-items-per-window`, `--keywords-per-window`, `--fashion-hashtag-query`, `--beauty-hashtag-query`, `--test-single-hashtag`, `--base-output`을 지원합니다. `--six-hour-new-only`는 6시간·창당 250개·기본 `reels.*` 출력 프리셋입니다.

### URL 기반 재수집

`refresh`는 기존 `reels.xlsx`(없으면 `instagram_data.xlsx`)의 `url` 열에서 Instagram Reel URL을 읽어 앱에서 하나씩 다시 엽니다. 재수집 결과는 기존 행을 덮어쓰지 않고 같은 URL의 다음 `collection_number` 행으로 추가되며, 조회·좋아요·댓글·리포스트·팔로워 증감값도 계산됩니다. 공유 링크의 `?igsh=...` 같은 쿼리가 달라도 같은 Reel로 연결합니다.

```powershell
.\collector.ps1 refresh --max-items 50 --verbose-progress
```

URL이 비어 있는 과거 행은 Android가 같은 Reel을 안정적으로 다시 열 수 없으므로 건너뜁니다. Android 신규 수집과 `refresh`를 같은 출력 폴더에서 동시에 실행하지 마세요.

### 출력 재생성

```powershell
.\collector.ps1 reconcile
.\collector.ps1 xlsx
```

`reconcile`은 Android 내부 관측 이력으로 공개 CSV·JSON·XLSX를 다시 만들고, `xlsx`는 `reels.csv`와 `users.csv`를 한 통합 `instagram_data.xlsx`로 만듭니다. 세 Excel 파일 모두 Python 버전과 같은 청록색 헤더, 필터, 첫 행 고정, 날짜·숫자 표시 형식을 사용합니다.

## 저장 결과

모든 결과는 `data_android`에 저장됩니다.

| 경로 | 내용 |
| --- | --- |
| `reels.csv` | `python_version`과 같은 공개 릴스 관측 이력 |
| `reels.json` | `python_version`과 같은 열·자료형의 공개 JSON 이력 |
| `reels.xlsx` | Python 호환 공개 Excel 수집 이력 |
| `users.csv`, `users.json`, `users.xlsx` | 수집된 릴스 작성자 기준의 사용자 관측 이력 |
| `.collector\android_observations.json` | Android 전용 원문 지표·수집 방식·XML/PNG 증빙 경로 |
| `evidence\000001.xml` | 해당 화면의 원본 UIAutomator XML |
| `evidence\000001.png` | 해당 화면의 원본 스크린샷 |
| `evidence\000001.likes_and_plays.xml` | 좋아요·조회수 상세 패널의 UIAutomator XML |
| `evidence\000001.likes_and_plays.png` | 좋아요·조회수 상세 패널의 스크린샷 |

공개 파일의 열 순서는 `collection_number`, `days_since_previous`를 포함해 `python_version`의 `reels.*`와 같습니다. 접근성 텍스트의 정확한 `Like number`, `Comment number`, `Reposted`, `Reshare number`를 우선 읽습니다. 화면이 `15.6K`·`1.2M`처럼 축약값만 보일 때는 각각 곱셈 단위(K=1,000, M=1,000,000)를 적용한 표시 기반 정수를 저장합니다. 좋아요 수 버튼을 열면 표시되는 `Likes and plays` 상세 패널에서 정확한 `like_count`와 `view_count`를 읽고 바로 원래 릴스로 돌아옵니다.

상세 패널에 “이 릴스의 총 좋아요 수는 …만 볼 수 있습니다” 또는 같은 의미의 영문 문구가 있으면 `.collector\android_observations.json`의 `like_count_is_private`에 `true`로 기록합니다. 정확한 수치가 표시되면 `false`, 패널을 열 수 없거나 문구를 판별할 수 없으면 `null`입니다. 화면에 댓글 수가 없을 때는 댓글 시트를 열어 `No comments yet`면 `comment_count=0`으로 기록하고, 댓글이 꺼졌거나 제한되었다는 화면 문구면 공개 `comment_count`는 빈 값으로 두되 터미널에는 `unavailable(disabled|limited)`로 표시합니다. 화면 우하단의 독립 `Ad`/`광고` 라벨은 공개 파일의 `ad=true`로, 캡션의 `#협찬`은 `ad=협찬`으로 기록합니다. 둘 다 있으면 명시적 `Ad`를 우선합니다. 신규 릴스마다 작성자 프로필을 열어 공개된 `biography`, `profile_category`, `post_count`, `following_count`, `follower_count`를 읽고, 옵션 메뉴의 `About this account`에서 공개된 `Account based in` 국가를 `account_country`로 저장한 뒤 릴스로 복귀합니다. `About`에서 Back을 눌렀을 때 실제로 옵션 메뉴가 남은 경우에만 한 번 더 Back을 눌러, 릴스가 해시태그 그리드로 빠지는 일을 막습니다.

Android 화면 기반 방식에서는 숫자 `user_id`, 업로드 시각(시간 단위), 영상 길이는 화면에 공개되지 않으면 빈 값으로 둡니다. 릴스의 공유 버튼에서 `Copy link`를 누른 뒤 읽은 Instagram Reel URL은 `url`에 저장합니다. 이 과정은 Android 클립보드 내용을 해당 링크로 바꾸며, Android 이미지가 클립보드 읽기를 허용하지 않으면 URL만 빈 값이고 수집은 계속됩니다. 위치는 현재 Reel UI XML에 `location_name`/`위치:`가 노출되는 경우에만 저장합니다. 캡션 상세에 보이는 게시 날짜는 `uploaded_at`에 `YYYY-MM-DD`로 저장합니다. 예를 들어 `April 28`처럼 연도가 생략되면 수집 연도(2026)를 적용하고, `April 28, 2024`처럼 연도가 표시되면 해당 연도를 그대로 사용합니다. `days_since_upload`는 그 날짜와 한국 시간(KST) 기준 수집 날짜의 달력상 일수 차이입니다. 기존 증빙에 캡션 상세 화면이 없는 과거 행은 이 두 열이 빈 값으로 남고, 새 수집부터 채워집니다. 캡션 본문은 현재 펼치기 전 화면에 보이는 부분만 읽습니다.

이미 만들어진 기존 Android 형식의 `reels.json`이 있더라도 다음 수집 시 자동으로 새 공개 양식으로 변환하고, 기존 Android 원본 필드는 `.collector\android_observations.json`으로 옮겨 보존합니다. 이미 저장된 화면 지문과 같은 릴스는 신규 행으로 추가하지 않습니다.

신규 수집 중복 판단은 정규화한 `username + caption + audio_name`의 화면 지문으로 합니다. URL이 새로 확보되어도 신규 수집에서는 기존 지문을 다시 저장하지 않습니다. 최신 수치가 필요하면 Android의 `refresh` 또는 `python_version`의 `refresh`를 사용하세요.

`--checkpoint-items`, `--progress-offset`, `--manual`, `--start-url`, `--output-stem`, `--new-only`, `--background`도 지원합니다. 기본 UI 대기 상한은 0.4초이며, 빠른 에뮬레이터에서는 `--interval-seconds 0.25`를 사용해도 됩니다. `--background`는 브라우저를 숨기는 옵션이 아니라 이미 로그인된 Android 앱을 사용하는 호환 옵션입니다. Android `refresh`는 화면에 표시되는 값만 다시 읽고, `followers`, `--no-login`, `--followers-only`, `--max-upload-age-days` 등 브라우저 응답 전용 기능은 계속 지원하지 않습니다.

## 중단 및 확인이 필요한 경우

로그인, 본인 인증, CAPTCHA, 일시 제한 화면이 보이면 수집기는 우회하지 않고 코드 2로 멈춥니다. Android 앱 업데이트로 UI 구조가 바뀌면 해당 항목의 XML·PNG 증빙을 확인해 파서 레이블을 추가하세요. 수집 중 에뮬레이터에서 직접 Instagram을 조작하지 않는 편이 안전합니다.
