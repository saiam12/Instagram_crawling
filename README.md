# Instagram crawling

Instagram 릴스 수집기, 영상 분석기와 다운로드 도구를 용도별로 관리합니다.

PC에 실행 환경을 설치하고 전용 로그인 프로필과 데이터 폴더를 사용합니다.
GitHub에는 실행 코드와 설정 예제를 공유하며, 수집 데이터·개인 로그인·실제 API 키는 포함하지 않습니다.

## 설치 및 실행 (Windows)

저장소는 원하는 폴더에 받아도 됩니다. `C:\Instagram-crawling`이나 특정 Windows 사용자명은
필수 경로가 아닙니다. 지원 실행 환경은 Windows 10/11 + PowerShell이며, macOS/Linux의
전체 수집·에뮬레이터 복구 동작은 지원 대상으로 검증하지 않았습니다.

1. [Python 3.12 이상](https://www.python.org/downloads/windows/)(권장 3.12)과 Google Chrome 또는 Microsoft Edge를 설치합니다.
2. 저장소 루트에서 아래 명령을 실행해 가상환경을 생성하고 패키지를 설치합니다.
3. 표시된 브라우저에서 각자 Instagram에 로그인합니다. 첫 실행에는 `--background`를 붙이지 않습니다.

```powershell
# 저장소 루트에서 실행
.\collectors\scripts\setup\repair_venv.ps1
.\collectors\collector.ps1 --collector-mode web --max-items 50
```

PowerShell이 스크립트 실행을 차단하면 현재 터미널에만 허용한 뒤 다시 실행합니다.

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

기본 `hybrid` 모드에는 Android SDK, Instagram 앱이 설치되고 로그인된 에뮬레이터 또는
USB 디버깅이 허용된 Android 기기와 Node.js 24 이상·Android 연결 도구가 추가로 필요합니다. 브라우저/SDK의 사용자 지정 경로,
AVD 선택, Python 경로 지정, Gemini 분석 설치는 [수집기 설치 안내](collectors/README.md#설치)를 따르세요.
설치·로그인·API 키 발급은 각자 진행해야 하며, 다른 사람의 `.venv`, 브라우저 프로필,
쿠키, `.env`를 복사해 공유하지 않습니다.

Android를 처음 준비한다면 [Android Studio 설치 및 에뮬레이터 설정](collectors/README.md#3-android-studio-설치-및-hybrid-준비)을
순서대로 진행하세요. SDK 설치, Pixel 8 이외의 AVD 선택, Instagram 로그인, ADB 연결 확인과 실행 명령을 안내합니다.

### 기능별로 필요한 설치

| 기능 | 설치·준비 사항 | 수동으로 해야 하는 일 |
|---|---|---|
| 웹 수집 | Python 3.12+, Chrome/Edge, 수집기 requirements | 각자 Instagram 로그인·인증 |
| hybrid 수집 | 웹 수집 환경 + Android SDK + Node.js 24+ + 로그인된 Android 기기 | AVD 생성 또는 USB 디버깅 승인, 앱 로그인, `collectors/scripts/setup/setup-android-ui.ps1` 실행 |
| 로컬 모니터 | 수집기 환경 | 아래 명령으로 실행 후 이 PC의 localhost 접속 |
| Gemini 분석·선택적 추천 | 별도 Gemini 가상환경과 requirements | API 키 발급, `.env` 설정; 공용 풀 사용 시 관리자의 시트·Apps Script 배포 |

설치된 Edge를 우선 사용하고, 없으면 Chrome을 사용합니다. 수집기에는 별도의 `playwright install`이 필요하지 않습니다.
Playwright의 패키지 설치와 브라우저 바이너리 설치는 별개입니다([공식 안내](https://playwright.dev/python/docs/browsers)).
웹만 수집하면 Android Studio와 Gemini 분석기 환경은 필요하지 않습니다.

배포하는 커밋에는 `collectors/collector.ps1`, `collectors/src/reels/`, `collectors/src/exporters/`,
`collectors/scripts/`, `collectors/requirements.txt`가 포함되어야 합니다. 로컬에만 있는
미추적 파일은 저장소를 clone하거나 pull해도 포함되지 않습니다. 수집 데이터와 로그인 파일은 배포 대상에서 제외합니다.

수집과 예약 재수집은 `collectors`, 영상 분석과 인사이트 생성은 `analyzers`에서 수행합니다. 모니터에서 결과를 조회·비교하고 실행 상태를 확인할 수 있습니다. 사용·설정 문서는 `docs`, 복구용 자료는 `archives`에서 관리합니다.

## 사용 안내

`collectors/data/collector/`와 `collectors/data/refresh/`의 Excel 파일은 같은 이름 기준을 사용합니다.

| 작업 | 데이터 활용 |
|---|---|
| 신규 수집 | `collectors/data/collector`에 릴스·사용자 이력을 누적하고 `collection_data.xlsx`에서 함께 검토합니다. |
| 재수집 | `collectors/data/refresh/input.xlsx`에 대상을 입력하고 `refresh_data.xlsx`에서 후속 회차를 비교합니다. |
| 개별 결과 조회 | 릴스와 사용자 이력을 CSV·JSON·Excel로 확인합니다. |

릴스의 공개 CSV·JSON·Excel에서는 `korean_source`, `view_count_observed_at`, `view_count_source`,
`android_mismatch`를 제외합니다. 판정·재시도에 필요한 내부 이력은 보존합니다.

| 도구 | 설치 및 실행 안내 |
|---|---|
| 수집기 (web·hybrid) | [collectors](collectors/README.md) |
| Gemini 분석기 | [analyzers](analyzers/README.md) |

수집기의 기본 모드는 `hybrid`(웹 + Android 지표 보강)입니다. 웹만 사용하려면
`--collector-mode web`을 지정합니다. Android 지표 보강은 hybrid 모드에서 사용합니다.

수집기 실행 예시:

```powershell
# 저장소 루트에서
cd .\collectors
.\collector.ps1 --collector-mode web --max-items 50
# Android SDK/ADB와 로그인된 기기가 준비된 경우
.\collector.ps1 --collector-mode hybrid --max-items 50
```

패션 예약 수집도 같은 실행기를 사용합니다. Android 준비가 끝난 뒤 `web`을 `hybrid`로 바꿀 수 있습니다.

```powershell
# 저장소 루트에서; 초기 수집을 포함한 총 회차는 --target-snapshots
.\collectors\collector.ps1 fashion --collector-mode web --duration-hours 48 --discovery-hours 4 --target-snapshots 12
.\collectors\monitor\start.ps1
```

모니터는 <http://127.0.0.1:8765>에서 실행합니다. 설치·포트·실행 버튼 설명은
[모니터 README](collectors/monitor/README.md)에 있습니다.

### PC마다 달라지는 값 지정

경로를 소스 코드에서 바꾸지 말고 환경 변수 또는 실행 옵션을 지정합니다.
수집기는 일반 `.env` 파일을 자동으로 읽지 않으며, 아래 PowerShell 환경 변수는 현재 터미널의 자식 프로세스에 전달됩니다.

| 값 | 지정 방법 |
|---|---|
| Chrome/Edge 실행 파일 | `INSTAGRAM_BROWSER_EXECUTABLE` |
| Android SDK 루트 | `ANDROID_HOME` 또는 `ANDROID_SDK_ROOT` |
| ADB 실행 파일 | `INSTAGRAM_ANDROID_ADB_PATH` 또는 `--android-adb-path` |
| 이미 연결된 기기의 serial | `INSTAGRAM_ANDROID_DEVICE_ID` 또는 `--android-device-id` |
| 자동 부팅할 AVD 이름 | `INSTAGRAM_ANDROID_AVD` 또는 PowerShell 실행기의 `-Avd` |
| 수집 출력·로그인 프로필 | `--data-dir`, `--profile-dir` |
| 설치에 사용할 Python | `collectors/scripts/setup/repair_venv.ps1 -PythonExecutable <python.exe 경로>` |
| Gemini 키·공용 풀·출력 경로 | `analyzers/.env.example`을 각자 `.env`로 복사해 설정 |

예를 들어 Android SDK를 기본 위치에 설치했다면 다음처럼 확인합니다.

```powershell
$env:ANDROID_HOME = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
& "$env:ANDROID_HOME\platform-tools\adb.exe" devices
& "$env:ANDROID_HOME\emulator\emulator.exe" -list-avds
```

기기가 여러 개면 `adb devices`에 나온 serial을 선택하고, AVD가 여러 개면 `-list-avds`에 나온 이름을 지정합니다.
에뮬레이터 그래픽은 AVD 설정을 따릅니다. GPU·가상화·드라이버 설정은 PC마다 달라 자동 설치나 고정값으로 해결하지 않습니다.
[Android Studio 공식 설치](https://developer.android.com/studio)와 [가속 설정](https://developer.android.com/studio/run/emulator-acceleration)을 확인하세요.

### 진단과 시각화 데이터 지정

Android 연결·설치와 읽기 전용 진단을 PowerShell에서 실행할 수 있습니다.

진단 캡처는 선택한 데이터셋의 조회수·팔로워 수집 현황을 저장합니다.
단독 시각화도 각자의 CSV를 지정할 수 있으며, `--csv`를 반복하면 여러 입력을 합칩니다.
아래 명령은 저장소 루트에서 실행합니다.

```powershell
# 선택한 데이터셋의 조회수·팔로워 수집 현황을 새 진단 폴더에 저장
.\collectors\.venv\Scripts\python.exe -B .\collectors\scripts\diagnostics\capture_view_count_evaluation.py --data-dir .\collectors\data\collector\.datasets\fashion

# 자신의 CSV로 영상 길이·조회수 시각화 생성
.\collectors\.venv\Scripts\python.exe -B .\collectors\scripts\diagnostics\visualize_duration_vs_play_count.py --csv .\collectors\data\collector\reels.csv --output .\outputs\duration-vs-play-count.html
```

운영 중 수집 데이터와 로그인 정보는 로컬에서 관리합니다.
Google Sheets 공용 키 풀은 팀이 같은 배포와 설정을 공유해 사용합니다.

### 설정만으로 바꿀 수 없는 부분

- Instagram의 버튼 식별자·페이지 구조는 현재 앱/웹 구조에 맞춘 코드입니다. UI가 바뀌면 해당 파서와
  회귀 테스트를 함께 수정해야 하며, 로그인·추가 인증·연령 제한을 코드가 대신 승인하지 않습니다.
- Gemini 모델은 프로젝트의 허용 목록 안에서 선택합니다. 새 모델 도입은 Python과 Apps Script의 모델 목록·스키마 호환성을
  함께 검토해야 합니다. 실제 모델 사용 권한·요금·한도는 각 Google 프로젝트에서 확인합니다.
- 공용 키 풀은 코드의 `SHEET_ID`에 지정된 동일한 시트를 사용합니다. 관리자는 해당 시트에 연결할 Apps Script를 배포합니다.
  시트를 변경할 때만 `Code.js`의 `SHEET_ID`를 수정하세요. 기존 시트·토큰·기록을 유지하는 순서는
  [공용 풀 설정 안내](analyzers/pool/apps_script/README.md)에 있습니다.

다운로드만 하려면 분석기 설치 후 다음을 실행합니다. 입력한 Reel의 영상은 `analyzers/downloads/`에 저장됩니다.

```powershell
# 저장소 루트에서
cd .\analyzers
.\.venv\Scripts\python.exe reel_downloader.py
```

## 관리 기준

- 각 도구의 가상환경, 로그인 프로필, 데이터와 결과물은 해당 도구 폴더에서 관리합니다.
- 수집기의 기본 출력은 `collectors/data/collector/`이며, 기존 다른 수집 데이터는 자동으로 합치지 않습니다.
- 수집기 내부 `src/reels/`, `src/exporters/`, `scripts/`는 Python 모듈과 실행 진입점입니다. 테스트는 `collectors/tests/`에 둡니다.
- 본인에게 기존 데이터가 있으면 해당 경로를 명시하세요. 서로 다른 데이터 폴더는 자동으로 합치지 않습니다.
- 완료된 임시 작업·과거 진단·이전 데이터는 `archives/repository-history-20261030.zip`에 압축 보관합니다. 재생성 가능한 설치 ZIP·캐시는 정리합니다.
- 실행에는 각 도구의 `.venv/Scripts/python.exe`를 사용하고 패키지는 `-m pip`로 설치합니다.

## 검증과 로컬 파일 관리

```powershell
# 저장소 루트에서
cd .\collectors
.\.venv\Scripts\python.exe -B -m unittest discover -s tests -q
```

테스트 통과는 실제 Instagram·Android·Gemini 실행 성공을 보장하지 않습니다.

각 도구의 테스트는 해당 폴더에서 실행합니다. Apps Script 테스트에는 별도로 Node.js가 필요합니다.
실제 로그인·연령 제한·추가 인증, 외부 서비스의 UI/응답 변경, API 모델 사용 권한·할당량은 각 계정에서 확인해야 합니다.
Excel이 잠겨 있으면 원본 대신 `_updated.xlsx` 대체 파일이 만들어질 수 있으므로 원본을 닫은 뒤 `collector.ps1 reconcile`로 반영합니다.

루트 [.gitignore](.gitignore)는 각 도구의 가상환경, 수집 데이터, 로그인 프로필,
쿠키·실제 `.env` 설정, 로그와 분석 결과를 제외합니다. `.env.example`은 공유 가능한
설정 예제로 유지합니다. `--data-dir`로 임의 이름의 폴더를 쓰면 `git check-ignore`로
제외 여부를 확인하세요. 이미 추적 중인 파일에는 새 ignore 규칙이 자동 적용되지 않습니다.

GitHub에 올리기 전에는 다음을 확인하세요. 이 점검 작업은 자동 commit·push를 하지 않습니다.

```powershell
git status --short
git check-ignore collectors/data/collector/reels.csv collectors/runtime/browser_profile/Default/Cookies analyzers/.env
# 현재 파일 목록에서 개인정보·실제 키·수집 결과가 없는지 확인
git ls-files
```

`.env.example`, `requirements.txt`, 실행 코드와 테스트는 포함하고 `.venv`, `data*`, 프로필·쿠키,
`diagnostics`, 모니터 `runtime`, 분석 `output/downloads/results`, 로컬 문서 보관 폴더는 제외합니다.
사용자가 다른 이름으로 만든 출력 폴더와 쿠키 파일은 기본 제외 규칙에 맞지 않을 수 있으므로 별도로 확인합니다.
이미 Git 이력에 실제 키를 올린 적이 있다면 삭제 커밋만으로 비밀이 사라지지 않으므로 키를 폐기·재발급하고 저장소 관리자와 이력 정리를 진행합니다.

수집·재수집 결과는 각 데이터 영역에서 관리하며 진단 기록과 로그인 상태는 실행 중에도 보존합니다.
