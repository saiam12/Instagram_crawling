# 웹 + Android Instagram Reels 수집기

이 폴더는 웹 전용(`web`)과 웹 + Android 지표 보강(`hybrid`, 기본값)을 지원하는 수집기입니다. 신규 탐색, 예약 재수집, Android 지표 보강과 결과 내보내기를 함께 수행합니다. 브라우저와 Android 앱은 각각 로그인 상태를 유지하며, 공개 결과는 기본적으로 이 폴더의 `data/collector`에 저장합니다. `--data-dir`을 지정하면 해당 경로에 저장합니다.

내부 구현은 `src/reels`의 `process_lock.py`(공통 잠금), `reel_history.py`(이력·출력), `android_metric_worker.py`(디스크 큐), `android_metric_pipeline.py`(순차 처리), `android_metric_policy.py`(공통 오류·비교 정책)로 나뉩니다. 기존 실행 명령과 `instagram_reels_browser.py`의 import 경로는 유지됩니다.

Android 연결·화면 읽기 장애는 릴스별 일반 실패 3회 제한과 별도로 재시도합니다. 메모리 파이프라인도 `.collector/android_pipeline_pending`에 미완료 작업을 보관하고 다음 실행에서 복원합니다. 지표가 이력에 저장된 뒤 체크포인트를 삭제합니다. 오디오 이름은 웹이 수집하며 Android 병합은 이를 변경하지 않습니다. 광고의 저장 수를 읽지 못했을 때도 빈값을 유지하며 비공개로 표시하지 않습니다.

수집 항목에는 조회수(`view_count`)가 포함됩니다. 릴스 공개 CSV·JSON·Excel은 한 릴스의 한 수집 회차를 한 행으로 저장하며, 재수집마다 새 행을 추가합니다. 회차별 열로 작성한 Excel 입력도 읽을 수 있습니다.

### 한국·외국 필터와 계정 국가

모니터에서 전체·한국·외국·미확인 릴스를 선택해서 볼 수 있습니다.
`korean`은 한국 관련 여부, `account_country`는 확인된 계정 국가입니다.
국가를 확인하지 못한 값은 빈칸으로 남습니다.

분류를 직접 바꾸려면 **실시간 데이터 → 릴스 제목 → 릴스 구분 → 구분 저장**을 사용하세요.
직접 지정한 분류는 같은 릴스의 모든 회차에 적용됩니다.

## 설치

개인용 웹 상태 화면은 [monitor/README.md](monitor/README.md)를 참고하세요.
`monitor/start.ps1`을 실행하면 현재 수집기와 데이터를 로컬 브라우저에서 확인하고,
옵션을 선택해 새 수집이나 Android 준비 작업을 실행할 수 있습니다.

### 1. Python과 브라우저 준비

Windows 10/11 + PowerShell을 기준으로 합니다. Python 3.12 이상(권장 3.12)과
Google Chrome 또는 Microsoft Edge를 설치하세요. 저장소를 받은 위치는 자유이며,
아래 명령은 저장소 루트에서 시작합니다.

```powershell
cd .\collectors
.\scripts\setup\repair_venv.ps1
```

이 스크립트는 `py -3`, 없으면 PATH의 `python`을 사용해 `.venv`를 생성/복구하고
`requirements.txt`를 설치합니다. Python 버전이 3.12 미만이면 설치를 중단합니다.
Python이 PATH에 없거나 여러 버전 중 하나를 선택하려면 실행 파일을 직접 지정하세요.

```powershell
# 예시 경로를 본인 Python 설치 경로로 바꾸세요.
.\scripts\setup\repair_venv.ps1 -PythonExecutable 'D:\Tools\Python312\python.exe'
```

이미 수집 중인 가상환경을 복구할 때는 해당 수집기·워커를 먼저 종료하세요. 수집 데이터와
로그인 프로필은 이 스크립트가 삭제하지 않습니다. `.venv`는 각자 생성하며 다른 PC에서 복사하지 않습니다.

Edge의 시스템 설치·사용자별 설치 위치와 PATH의 `msedge`를 먼저 탐색하고,
없으면 Chrome의 설치 위치와 PATH의 `chrome`을 탐색합니다. 별도의 `playwright install`은 필요하지 않습니다.
직접 선택하거나 비표준 설치 위치를 쓰려면 브라우저 실행 파일을 지정하세요.

```powershell
$env:INSTAGRAM_BROWSER_EXECUTABLE = 'D:\Apps\Chrome\Application\chrome.exe'
.\collector.ps1 --collector-mode web --max-items 50
```

위 경로는 예시입니다. 존재하지 않는 경로를 지정하면 오류로 안내합니다. 자동 탐색으로
되돌리려면 `Remove-Item Env:INSTAGRAM_BROWSER_EXECUTABLE -ErrorAction SilentlyContinue`를 실행합니다.
브라우저를 바꿀 때는 `--profile-dir .\runtime\browser_profile\edge`처럼 새 전용 프로필 경로를 지정하고
다시 로그인하세요. 개인 Chrome/Edge의 평소 사용 프로필을 직접 지정하지 마세요.

### 2. 먼저 웹 수집으로 로그인

```powershell
.\collector.ps1 --collector-mode web --max-items 50
```

브라우저 로그인과 Android 앱 로그인은 별개입니다. 첫 실행은 창을 표시한 상태로 로그인하고
터미널 안내에 따라 진행하세요. 저장된 프로필이 준비된 뒤부터 `--background`를 사용할 수 있습니다.
새 수집 데이터 폴더는 자동 생성됩니다. `refresh`는 본인이 이미 수집한 이력이나 입력 엑셀이 필요합니다.

### 3. Android Studio 설치 및 hybrid 준비

Android는 `hybrid` 모드에서 지표를 보완할 때 사용합니다. `web` 모드만 사용하면 이 단계를 건너뛰어도 됩니다.
기존 Android 기기를 USB로 연결해 사용할 경우에는 아래의 실제 기기 안내를 참고하세요.

#### Android Studio 설치

1. [Android Studio 공식 다운로드](https://developer.android.com/studio)에서 Windows 설치 파일을 받습니다.
2. 설치 파일을 실행해 Android Studio를 설치하고, 첫 실행의 Setup Wizard를 진행합니다.
   처음 설치할 때는 기본 설치 방식인 **Standard**를 선택하고 필요한 SDK 구성요소와 라이선스를 확인해 설치합니다.
3. 시작 화면의 **More Actions → SDK Manager**를 엽니다. 프로젝트를 이미 열었다면
   **Settings**에서 `Android SDK`를 검색해도 됩니다. 수집용 에뮬레이터를 만들기 위해 새 앱 프로젝트를 생성할 필요는 없습니다.
4. SDK Manager의 **Android SDK Location**을 확인해 둡니다. 아래 ADB 명령에서 이 SDK 경로를 사용합니다.

설치 요구 사항과 설치 화면은 [공식 설치 안내](https://developer.android.com/studio/install)를 참고하세요.

#### SDK 구성요소 설치

SDK Manager에서 다음 항목을 확인하고 **Apply → OK**로 설치합니다.

| 위치 | 항목 | 용도 |
|---|---|---|
| SDK Tools | Android SDK Platform-Tools | 앱 화면 조작·연결 확인에 사용하는 `adb.exe` |
| SDK Tools | Android Emulator | 가상 Android 기기 실행 |
| SDK Tools | Android SDK Build-Tools | Android 연결 도구의 보조 앱 설치 준비 |
| SDK Platforms 또는 AVD 생성 화면 | 선택한 Android 버전의 Google Play 시스템 이미지 | Instagram을 설치할 에뮬레이터 운영체제 |

Python의 `.venv`에 패키지를 설치하는 것만으로 Android SDK나 시스템 이미지가 설치되지는 않습니다.

#### Android 연결 도구 설치

[Node.js 24 이상](https://nodejs.org/en/download)을 설치하고 PowerShell을 새로 연 뒤, **저장소 루트에서** 실행합니다.

```powershell
.\collectors\scripts\setup\setup-android-ui.ps1
```

이 명령은 수집기에 필요한 Android 연결 도구를 이 저장소 안에 설치합니다. Python 가상환경과 별도로 한 번 준비하며, 저장소를 다른 PC로 옮겼다면 그 PC에서도 실행합니다. 이후 `hybrid` 수집과 Android 워커는 필요한 서버를 자동으로 시작하고 연결합니다. `web` 전용 수집에는 이 설치가 필요 없습니다.

Java는 Android Studio에 포함된 `jbr`을 사용합니다. 설치 위치를 자동으로 찾지 못하면 `JAVA_HOME`에 해당 `jbr` 폴더를 지정한 뒤 실행하세요.
연결 준비가 실패하면 표시된 오류를 해결한 뒤 다시 실행합니다. 대기 중인 Android 수집 작업은 유지됩니다.

수집을 종료하면 기기의 연결 세션은 정리됩니다. 재사용을 위해 남아 있는 로컬 연결 서버도 종료하려면 **저장소 루트에서** 다음 명령을 실행합니다. 에뮬레이터는 종료하지 않습니다.

```powershell
.\collectors\scripts\android\stop-android-ui.ps1
```

다른 Appium 서버를 직접 운영하는 경우 실행 전에 `INSTAGRAM_APPIUM_URL`에 해당 주소를 지정할 수 있습니다. 기본 주소는 `http://127.0.0.1:4723`입니다.
직접 운영하는 Appium 3 서버에는 세션 정리에 필요한 `--allow-insecure=*:session_discovery` 옵션을 지정합니다. 자동으로 시작되는 로컬 서버에는 이 설정이 포함됩니다.

#### 가상 기기(AVD) 만들기

1. 시작 화면에서 **More Actions → Virtual Device Manager**를 엽니다.
   프로젝트 화면에서는 **View → Tool Windows → Device Manager**로 엽니다.
2. **+ → Create Virtual Device** 또는 **Create Device**를 누릅니다.
3. **Phone**에서 Play Store 로고가 있는 기기를 선택합니다. **Pixel 8일 필요는 없습니다.**
4. Google Play가 포함되고 PC 아키텍처와 호환되는 시스템 이미지를 선택합니다.
   아직 다운로드하지 않았다면 **Download**로 설치한 뒤 다음 단계로 진행합니다.
5. AVD 이름을 정하고 **Finish**를 누릅니다. 이름에는 영문·숫자·밑줄·점·하이픈을 사용하세요.
   아래 실행 예시의 `My_AVD`는 이 이름으로 바꿉니다.
6. Device Manager 목록에서 해당 기기의 **▶** 버튼을 눌러 부팅하고 Android 홈 화면이 나올 때까지 기다립니다.

기기와 시스템 이미지 선택은 [공식 AVD 생성 안내](https://developer.android.com/studio/run/managing-avds)를 참고하세요.
다른 기기·해상도·Instagram 버전에서는 화면 구성이 달라질 수 있으므로 처음에는 소량 수집으로 확인하세요.

#### Instagram 설치 및 로그인

1. 에뮬레이터의 Play Store를 열어 Google 계정으로 로그인합니다.
2. Instagram을 설치하고, 수집 대상에 접근할 수 있는 본인 Instagram 계정으로 로그인합니다.
3. 추가 인증·권한 안내를 완료하고 화면 잠금을 풀어 둡니다. 브라우저의 로그인과 앱의 로그인은 별개입니다.
4. 에뮬레이터에서 일반 릴스가 정상적으로 열리는지 확인합니다. 첫 로그인은 기기 창을 보면서 진행하고,
   로그인과 연결을 확인한 다음부터 `--background`를 사용하세요.

#### 연결 확인 및 수집기 실행

아래 명령은 **저장소 루트에서** 실행합니다. 기본 SDK 위치를 사용했다면 다음과 같습니다.

```powershell
$env:ANDROID_HOME = Join-Path $env:LOCALAPPDATA 'Android\Sdk'
& "$env:ANDROID_HOME\platform-tools\adb.exe" devices
& "$env:ANDROID_HOME\emulator\emulator.exe" -list-avds
.\collectors\collector.ps1 -Avd 'My_AVD' --collector-mode hybrid --max-items 10
```

`adb devices`에서 기기 serial 옆에 `device`가 표시되면 연결된 상태입니다.
`emulator-5554`는 serial의 예시이며 실제 값은 실행 환경에 따라 달라집니다.
`-list-avds`에 표시된 이름과 `adb devices`의 serial은 서로 다른 값입니다.

| 확인 결과 | 조치 |
|---|---|
| `adb.exe`를 찾을 수 없음 | SDK Location과 Platform-Tools 설치 여부 확인 |
| 기기 목록이 비어 있음 | Device Manager에서 AVD를 실행하고 부팅 완료 후 다시 확인 |
| `offline` | 부팅 완료·ADB 연결 상태를 확인한 뒤 다시 실행 |
| 실제 기기가 `unauthorized` | 휴대폰 잠금을 풀고 USB 디버깅 허용 창 승인 |
| 온라인 기기가 여러 개 | `--android-device-id`에 사용할 기기의 serial 지정 |
| 에뮬레이터 부팅·그래픽 오류 | PC 가상화 지원과 해당 AVD의 그래픽 설정 확인 |

가상화가 꺼져 있으면 BIOS/UEFI의 가상화 옵션과 Windows 하이퍼바이저 설정이 필요할 수 있습니다.
그래픽 모드는 AVD 설정을 따르며, 소스 코드에서 특정 GPU 모드를 강제하지 않습니다.
PC별 설정은 [공식 에뮬레이터 가속 안내](https://developer.android.com/studio/run/emulator-acceleration)를 참고하세요.

이하 명령은 **`collectors/` 폴더에서** 실행하는 예시입니다.
기본 SDK 위치는 `%LOCALAPPDATA%\Android\Sdk`입니다. 다른 폴더에 설치했다면
**`platform-tools` 폴더가 아닌 SDK 루트**를 지정하세요.

```powershell
$env:ANDROID_HOME = 'D:\Android\Sdk'
& "$env:ANDROID_HOME\platform-tools\adb.exe" devices
& "$env:ANDROID_HOME\emulator\emulator.exe" -list-avds
.\collector.ps1 -Avd 'My_AVD' --collector-mode hybrid --max-items 50
```

`My_AVD`는 목록에 나온 실제 이름으로 바꿉니다. ADB는 명시한 경로,
`INSTAGRAM_ANDROID_ADB_PATH`, `ANDROID_HOME`, `ANDROID_SDK_ROOT`, 기본 SDK 위치,
PATH 순서로 탐색합니다. 이미 온라인인 기기가 하나면 해당 기기를 사용합니다.
온라인 기기가 없고 실행 중인 에뮬레이터도 없으면 `-Avd` 또는 `INSTAGRAM_ANDROID_AVD`로 지정한 AVD를 시작하며,
화면을 표시하는 실행에서는 첫 AVD를 자동 선택합니다. 백그라운드에서 AVD가 여러 개이면 사용할 이름을 지정해야 합니다.
기기가 이미 실행 중이면 부팅을 기다리며 `-Avd`로 다른 기기로 전환하지 않습니다. 기기를 구분할 때는 serial을 지정하세요.

```powershell
# ADB 경로와 온라인 기기를 직접 지정하는 예시
.\collector.ps1 --android-adb-path 'D:\Android\Sdk\platform-tools\adb.exe' --android-device-id 'emulator-5554' --max-items 50
```

온라인 기기가 여러 개이면 `--android-device-id`가 필요합니다. 실제 휴대폰은 개발자 옵션의
USB 디버깅을 켜고 PC 연결 승인까지 완료하세요. 실제 기기를 사용하면 Android Emulator 설치는 필수가 아닙니다.
선택된 ADB와 기기 serial은 Python과 별도 Android 워커에도 동일하게 전달됩니다.
오래된 serial 설정을 해제하려면 `Remove-Item Env:INSTAGRAM_ANDROID_DEVICE_ID -ErrorAction SilentlyContinue`를 실행합니다.

Android 읽기 전용 진단 기록기도 `-DeviceId` 또는 `INSTAGRAM_ANDROID_DEVICE_ID`를 우선 사용합니다.
설정이 없으면 온라인 기기가 정확히 하나일 때만 자동 선택하며, 0개·여러 개이면 오류로 중단합니다.
임의의 `emulator-5554` 기기를 선택하지 않습니다.

### 4. 설정 유지와 선택 분석 기능

위 `$env:...` 설정은 현재 PowerShell에서 실행하는 프로세스에만 전달됩니다. 계속 사용하려면
Windows **환경 변수 편집 → 사용자 변수**에 등록하고 새 터미널을 여세요.
수집기는 일반 `.env` 파일을 자동으로 읽지 않습니다.

### 실행·저장 실패 시 확인

- 같은 데이터 폴더를 쓰는 수집기는 한 번에 하나만 실행합니다. 서로 다른 작업을 병행하려면
  데이터 폴더와 브라우저 로그인 프로필을 각각 분리하세요.
- `collector.lock.json`이 비어 있거나 손상되면 자동으로 잠금을 가져오지 않습니다. 다른 수집기가
  초기화 중일 수 있기 때문입니다. 오류에 표시된 데이터 폴더를 확인하고 관련 프로세스가 모두 종료된 것을
  확인한 후에만 해당 잠금 파일을 백업·제거하고 다시 실행하세요. 실행 중인 잠금은 삭제하지 마세요.
- Excel이 원본을 잠근 경우 `reels_updated.xlsx` 대체 파일을 보존합니다. Excel을 닫고
  `.\collector.ps1 reconcile --data-dir <본인 데이터 폴더>`로 반영하세요. 잠금이 계속되면 대체 파일을 유지하며
  원본 반영이 끝난 경우에만 정리합니다.
- 설치 위치·기기 선택 오류는 위 환경 변수/실행 옵션으로 수정합니다. 로그인·추가 인증·연령 제한·앱 설치는
  각 계정과 기기에서 직접 완료해야 합니다. GPU/가상화 오류는 Android Studio의 해당 AVD 설정과 PC 드라이버를 확인합니다.

| 설정 | 용도 |
|---|---|
| `INSTAGRAM_BROWSER_EXECUTABLE` | 사용할 Chrome/Edge 실행 파일의 전체 경로 |
| `ANDROID_HOME` / `ANDROID_SDK_ROOT` | 기본 위치와 다른 Android SDK 루트 |
| `INSTAGRAM_ANDROID_AVD` | 온라인 기기가 없을 때 시작할 AVD 이름 (`-Avd`로 변경 가능) |
| `INSTAGRAM_ANDROID_ADB_PATH` | 사용할 `adb.exe` 전체 경로 (`--android-adb-path`로 변경 가능) |
| `INSTAGRAM_ANDROID_DEVICE_ID` | 사용할 온라인 기기의 serial (`--android-device-id`로 변경 가능) |
| `FASHION_ANALYZER_PYTHON` | 별도 Gemini 분석용 Python 실행 파일의 전체 경로 |

Fashion 자동 Gemini 분석까지 사용하려면 `analyzers/`에 별도 `.venv`를 만들고
그 폴더의 `requirements.txt`와 `.env`를 준비하세요. 자세한 순서는
[Gemini README](../analyzers/README.md)를 따릅니다. 기본 공용 풀 모드에는 관리자에게 제공받은
풀 URL·토큰과 키 설정이 필요합니다. 개인 실행은 해당 안내에 따라 `GEMINI_POOL_MODE=local`을 지정합니다.
분석기 기본 Python 경로는 `analyzers/.venv/Scripts/python.exe`이며, 다른 환경은
`FASHION_ANALYZER_PYTHON`으로 지정합니다. 이 선택 분석이 실패해도 수집된 Reel은 유지됩니다.

Windows 의존 실행/복구 스크립트가 있으므로 macOS/Linux의 전체 동작은 지원 대상으로
검증하지 않았습니다. OS, Python, 브라우저, Android SDK·AVD·Instagram 설치, 각자 로그인,
Gemini API 키/공용 풀 권한은 저장소 pull만으로 제공되지 않습니다.

## 수집과 데이터 관리

웹에서 탐색한 릴스를 회차별 이력에 누적하고, hybrid 모드에서는 Android 작업 큐로 지표를 보완합니다. 저장 결과와 모니터의 진행 정보는 이 이력을 기준으로 확인합니다.

| 기능 | 운영 방식 |
|---|---|
| 신규 수집 | 키워드와 실행 시간을 정해 `data/collector`에 기록합니다. |
| 재수집 | `data/refresh/input.xlsx`에서 대상을 접수하고 시간·횟수 조건에 맞춰 후속 회차를 저장합니다. |
| 결과 활용 | CSV·JSON·Excel에서 릴스와 사용자 이력을 비교합니다. |
| 상태 관찰 | 웹·Android 진행 상태, 큐, 오류와 저장 기록을 확인합니다. |
| 실행 환경 유지 | 로그인·기기 연결 상태를 보존하고 진단 기록으로 문제를 확인합니다. |

설정은 환경 변수와 실행 옵션으로 지정하며, 시간당 접수 상한은 최상위 `refresh.local.json`에서 관리합니다.

### 개인 데이터로 진단·시각화 실행

아래 명령은 `collectors` 폴더에서 실행합니다. 진단은 `.collector/reels_history_active.csv`가 있는
데이터셋을 지정하며, 로그 파일이 없어도 현재 수집 현황을 저장합니다. 과거 패치 파일은 필요하지 않습니다.

```powershell
.\.venv\Scripts\python.exe -B .\scripts\diagnostics\capture_view_count_evaluation.py --data-dir .\data\collector\.datasets\fashion
.\.venv\Scripts\python.exe -B .\scripts\diagnostics\visualize_duration_vs_play_count.py --csv .\data\collector\reels.csv --output ..\outputs\duration-vs-play-count.html
```

캡처의 `--output`은 아직 없는 새 폴더를 지정합니다. 생략하면 선택한 데이터셋의 `diagnostics/view_count_evaluation/`에
시각별 폴더를 만듭니다. 시각화 입력에는 `video_duration_seconds`, `view_count` 열이 필요하며,
`--csv`를 반복해 여러 CSV를 합칠 수 있습니다. 입력을 생략하면 기본 데이터 폴더의 기존 `*_reels.csv`와
`reels.csv`를 사용합니다.

다른 PC에서는 `.venv`, `node_modules`, 로그인 프로필을 복사하지 말고 설치 절차를 다시 수행합니다.
저장소 경로·SDK·브라우저·기기는 각 PC의 설치 위치와 환경 변수/실행 옵션을 사용합니다.
Google Sheets와 공용 키 풀의 팀 공유 설정은 변경하지 않았습니다. 변경 범위와 검증은
[개발 기록](../docs/development.md)을 참고하세요.

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

| 활용 목적 | 제공 방식 |
|---|---|
| 릴스 회차 비교 | 수집 회차·시각·경과 시간·지표와 변화량을 CSV·JSON·Excel로 확인합니다. |
| 사용자 추이 확인 | 사용자별 회차·팔로워 수·직전 대비 증감을 확인합니다. |
| 한 번에 검토 | 신규 수집은 `collection_data.xlsx`, 재수집은 `refresh_data.xlsx`의 `reels`·`users` 시트를 사용합니다. |

릴스 공개 출력에는 `view_count`가 포함됩니다. 사용자·해시태그 출력은 각각 별도 스키마를 사용합니다.

통합 엑셀은 아래 명령으로 최신 CSV 기준으로 다시 만듭니다.

```powershell
.\collector.ps1 xlsx
```

수집 결과에는 릴스 정보, 조회수·좋아요·댓글·공유·리포스트·저장 수, 팔로워 수와 영상 길이 등이 포함됩니다.
접근 권한이나 게시물 상태에 따라 일부 값은 비어 있을 수 있습니다.
hybrid에서는 Android 지표가 나중에 반영될 수 있으므로 모니터에서 남은 작업을 확인하세요.

Android를 사용할 때는 앱에 로그인하고 화면 잠금과 권한 팝업을 해제해 두세요.
브라우저만 사용하려면 `--collector-mode web`을 지정합니다.

```powershell
.\collector.ps1 --max-items 50 --background
.\collector.ps1 --max-items 50 --background --android-device-id emulator-5554
.\collector.ps1 --max-items 50 --background --android-metrics-required
.\collector.ps1 --max-items 50 --background --no-android-metrics
.\collector.ps1 fashion --collector-mode hybrid --background
.\collector.ps1 fashion --collector-mode web --background
.\collector.ps1 hashtag-posts --hashtag-query '오오티디 OR 패션'
.\collector.ps1 hashtag-posts --preset fashion
.\collector.ps1 hashtag-posts --preset beauty
.\collector.ps1 hashtag-posts --preset fashion-beauty
```

`--collector-mode hybrid`는 웹과 Android를 함께 사용하며, `web`은 브라우저만 사용합니다.
Android 전용 수집 모드는 지원하지 않습니다.

해시태그 게시물 수도 필요하면 `--collect-hashtag-media-count`를 추가하세요.
이 작업은 시간이 더 걸릴 수 있습니다.

```powershell
.\collector.ps1 fashion --background --collect-hashtag-media-count
```

## 오류와 진행 상태 확인

진행 상황과 오류는 터미널 또는 [모니터](monitor/README.md)에서 확인할 수 있습니다.
Android의 `[현재/전체]`는 실제 처리 시도 순서입니다. 새 작업이나 재시도가 추가되면 전체 수가 늘어납니다. `status=requeued`는 재시도 대기 상태이며, `retry_in=30s`는 30초 후부터 다시 시도할 수 있다는 뜻입니다.
로그인·추가 인증·접근 제한이 표시되면 해당 브라우저나 앱에서 상태를 확인하세요.
429 등 이용 제한은 프로그램이 해제할 수 없으므로 터미널에 표시된 대기 안내를 따릅니다.

문제 내용을 확인하거나 문의할 때는 실행 명령과 오류 시각, 다음 로그를 함께 확인하세요.
`--data-dir`을 지정했다면 아래 경로를 해당 폴더로 바꿉니다.

```powershell
Get-Content .\data\collector\logs\instagram_collector_*.log -Tail 80
```

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

최초 수집할 Reel의 업로드 경과일은 `--max-upload-days`로 바꿉니다. 기존 `--maxdays`도 별칭으로 사용할 수 있습니다. 예를 들어 최근 14일 이내만
수집하려면 `.\collector.ps1 fashion-beauty --background --max-upload-days 14`를 실행합니다.

기본 실행 시간은 16시간입니다. 신규 탐색 시간은 전체 실행 시간에서 12시간을 뺀 값이며,
기본 실행에서는 처음 4시간 동안 해시태그를 하나씩 순서대로 탐색합니다.
해시태그당 기본 200개 후보를 검사하며, 정상 종료되면 시간 구간이 끝나기를 기다리지 않고
다음 해시태그로 이동합니다. 마지막 해시태그까지 끝나면 처음부터 다시 순환합니다.
별도의 해시태그 묶음이나 세트는 없습니다. 실패나 429 재시도에서는 현재 해시태그를 유지합니다.
`fashion`과 `beauty`는 해당 도메인을 탐색하고, `fashion-beauty`는 패션과 뷰티를 30분마다 교대합니다.
신규 저장 개수 제한은 없으며 실제 저장 개수는 중복·업로드 기간 조건과 남은 시간에 따라 달라집니다.
최초 수집 후보에는 업로드 후
30일 이내 필터를 적용합니다. 각 Reel은 최초 수집 후 `+4시간`, `+8시간`, `+12시간`에 재수집하여
최초 수집을 포함한 총 4개 시점의 스냅샷을 만듭니다. 나머지 시간에는 신규 탐색 없이 기한이 된
재수집을 마무리합니다. 같은 도메인의 기한 재수집 URL은 최대 50개씩 한 브라우저 세션에서 처리하고,
패션·뷰티의 기한이 겹치면 로그인 브라우저의 독립 탭에서 두 묶음을 병렬 처리합니다. 이번 실행에서
생긴 모든 Reel의 예약된 4시간 간격 재수집이 끝나면 16시간을 기다리지 않고 정상 종료합니다.
옵션을 모두 명시한 같은 실행은 다음과 같습니다.

```powershell
.\collector.ps1 fashion-beauty --duration-hours 16 --discovery-hours 4 --max-upload-days 30 --discovery-interval-minutes 30
```

현재 기본값을 요약하면 다음과 같습니다.

| 항목 | 기본값 |
|---|---:|
| 전체 실행 시간 | 16시간 |
| 신규 탐색 시간 | 전체 실행 시간 - 12시간 (기본 16시간 실행 시 4시간) |
| 신규 탐색 간격 | 30분 |
| 해시태그 처리 | 하나씩 순서대로 탐색 후 다음 해시태그로 이동 |
| 해시태그당 검사 후보 | 기본 200개 |
| 신규 저장 개수 | 별도 제한 없음 (탐색 시간 내 수집) |
| 최초 수집 업로드 범위 | 최근 30일 |
| 같은 Reel 재수집 | 최초 수집 후 +4시간, +8시간, +12시간(최초 포함 총 4회) |

예를 들어 아래 명령은 **전체 16시간 실행은 유지하면서 시작 후 4시간 동안만 신규 Reel을 탐색**합니다. 별도의 터미널을 30분마다 여는 방식이 아니라 하나의 실행 프로세스가 30분 창을 순환하며, 신규 탐색이 끝난 뒤에도 실행 종료 전까지 예약된 4시간 간격 재수집을 계속합니다.

```powershell
.\collector.ps1 fashion --background --discovery-hours 4
```

키워드당 후보 수는 `--hashtag-candidates-per-keyword`, 최초 수집 업로드 범위는 `--max-upload-days`로 바꿀 수 있습니다.

```powershell
.\collector.ps1 fashion --background --hashtag-candidates-per-keyword 30 --max-upload-days 14
```

### `fashion` 예약 수집 옵션 조합

아래 시간·검색 옵션은 `beauty`와 `fashion-beauty`의 하이브리드·웹 모드에도 적용됩니다.

신규 저장 개수의 목표와 시간 구간별 상한은 없습니다. 설정한 신규 탐색 시간과 전체 실행 시간 안에서
발견한 릴스를 계속 수집합니다. 해시태그별 검사 후보 수, 업로드 기간 필터, 재수집 예약은 별도로 적용됩니다.

| 옵션 | 기본값 | 의미와 제약 |
|---|---:|---|
| `--fashion-hashtag-query '패션'` | 내장 패션 키워드 | 패션 검색 키워드를 교체합니다. 여러 개는 `'패션 OR 데일리룩'`처럼 지정합니다. |
| `--beauty-hashtag-query '뷰티'` | 내장 뷰티 키워드 | 뷰티 검색 키워드를 교체합니다. `beauty`와 `fashion-beauty`에서 사용합니다. |
| `--hashtag-candidates-per-keyword` | 200 | 활성 해시태그 하나에서 검사할 Reel URL 후보 수입니다. 저장 개수 제한이 아닌 검색 후보 수입니다. 양의 정수여야 합니다. |
| `--discovery-interval-minutes` | 30 | 한 탐색 창의 길이입니다. 양수여야 합니다. |
| `--discovery-hours` | 실행 시간 - 재수집 기간 | 실행 시작 후 신규 탐색할 시간입니다. 양수이며 아래 시간 조건을 만족해야 합니다. |
| `--duration-hours` | 16 | 전체 실행 시간의 상한입니다. 0은 시간 제한 없이 실행합니다. |
| `--recollection-interval-minutes` | 240 | 같은 Reel의 예약 재수집 간격입니다. 양수여야 합니다. |
| `--target-snapshots` | 4 | 최초 수집을 포함해 목표로 하는 스냅샷 수입니다. 양의 정수여야 합니다. |
| `--max-upload-days` (`--maxdays`) | 30 | 최초 수집 대상의 업로드 경과일 상한입니다. 0은 기간 필터를 끕니다. |
| `--data-dir` | `data/collector` | 출력과 예약 수집 상태를 저장할 루트 디렉터리입니다. |

시간 제한 없이 신규 탐색과 예약 재수집을 계속하려면 다음처럼 실행합니다. `Ctrl+C`로 중지합니다.

```powershell
.\collector.ps1 fashion --duration-hours 0 --background
```

0을 지정하고 `--discovery-hours`를 생략하면 신규 탐색도 계속합니다. `--discovery-hours 4`를 함께
지정하면 신규 탐색은 4시간에 종료하고 남은 재수집을 마친 뒤 정상 완료할 수 있습니다.
`--six-hour-new-only`는 6시간 전용이므로 `--duration-hours 0`과 함께 사용할 수 없습니다.
이 옵션은 `fashion`, `beauty`, `fashion-beauty` 예약 수집에 적용됩니다. `refresh.ps1`의 구간별
재수집 일정과 168시간 종료 규칙은 별개입니다.

시간 제한이 있는 실행에서 재수집을 켜면 `--discovery-hours`가
`--duration-hours - (--target-snapshots - 1) × --recollection-interval-minutes / 60` 이하이어야 합니다.
`--duration-hours`는 오른쪽의 재수집 기간보다 길어야 합니다. 예를 들어 45분 실행,
4분 간격, 최초 포함 3회 스냅샷이면 재수집 기간은 8분이므로 신규 탐색은 최대 37분입니다.
아래 15분 탐색 설정은 이 조건을 만족합니다.

탐색 창이나 `--discovery-hours`가 끝나면 신규 Reel 탐색을 멈추고, 이미 저장한 Reel의
대기 중인 팔로워·`play_count` 보완과 결과 저장을 마친 뒤 예약 작업을 계속합니다.
이 후속 처리 때문에 탐색 종료 시각보다 늦게 작업이 마무리될 수 있습니다.
Android 지표 큐도 남은 저장 대상의 처리를 계속합니다.

예약 수집기는 신규 탐색 기간이 끝나고, 현재 및 종료 시각 전 예약된 재수집과 Android 큐·출력 저장 작업이 모두 없으면 `--duration-hours` 전에 정상 완료합니다. 현재 큐가 비었어도 앞으로 실행할 재수집 예약이 남아 있으면 기다립니다. 조기 완료 시 상태에 `completion_reason=all_collection_work_finished`와 실제 `finished_at`을 기록합니다. 일시적인 저장·브라우저 오류로 감독 루프를 다시 만들 때도 원래 시작·종료 시각, 작업별 재시도 기록, HTTP 429 대기 시각을 유지합니다. 감독 루프 오류는 최대 30초 대기 후 재시도하며 `retry_wait`와 원본 traceback을 기록합니다. Ctrl+C 등 사용자가 요청한 중단은 재시작하지 않습니다.

일반 작업이 연속 5회 실패해도 전체 중지 신호를 만들지 않으며 웹은 5초, 분리된 Android 워커는 30초 대기 후 계속합니다. Android의 일반 릴스 지표 수집은 릴스별 최대 3회 시도 후 해당 작업의 실패를 기록하고 다음 작업으로 넘어갑니다. 화면 읽기·장치 연결 오류는 일반 실패 한도와 구분하여 작업을 보존하고, 화면 읽기 오류에는 작업별 재시도 간격 증가를 적용합니다. 웹 탭 복구 실패도 해당 릴스를 건너뛰거나 탭을 재생성하는 범위에서 처리합니다. 팔로워 조회·접근 제한으로 현재 배치가 끝나도 예약 수집기의 중지 신호로 전달하지 않습니다.

```powershell
# 저장소 루트에서
cd .\collectors
.\collector.ps1 fashion --fashion-hashtag-query '패션' --hashtag-candidates-per-keyword 50 --discovery-hours 0.25 --duration-hours 0.75 --recollection-interval-minutes 4 --target-snapshots 3 --data-dir .\data_fashion_45min_test --max-upload-days 3000
```

테스트 데이터를 분리하려면 사용하지 않은 `--data-dir` 경로를 지정하세요. 상대 경로는 명령을
실행한 폴더를 기준으로 하며, 새 데이터 경로를 써도 브라우저 로그인 프로필은 유지됩니다.
같은 데이터 경로를 재사용하면 기존 수집 이력과 예약 상태가 적용되어 이미 수집한 Reel은
신규로 저장되지 않을 수 있습니다.
`fashion` 예약 재수집은 이번 실행에서 처음 수집한 Reel을 대상으로 합니다.
저장된 로그인으로 창 없이 실행하려면 명령 끝에 `--background`를 추가합니다.

프로필 이동·지표 수집 로직을 시험할 때만 패션 해시태그 하나로 고정하려면 아래처럼 실행합니다. 이 옵션은 첫 번째 해시태그만 반복해서 검사하는 테스트용입니다. 일반 실행은 전체 목록을 하나씩 순환합니다.

```powershell
.\collector.ps1 fashion --test-single-hashtag --background
```

패션·뷰티 내장 키워드(각 48개)를 유지한 채 **6시간 동안 신규 Reel만** 수집하고, 재수집 없이
기본 `data/collector\reels.*`와 `data/collector\users.*`에 바로 누적하려면 아래의 단일 옵션을 사용합니다.
최근 365일 이내 업로드된 후보만 대상으로 하며, 해시태그 하나당 기본 200개 후보를 검사합니다.
현재 해시태그가 끝나면 다음 해시태그로 이동합니다. 패션과 뷰티는 30분마다 교대하며,
각 도메인은 다음 차례에 자신의 해시태그 순서를 이어갑니다. 조건을 통과한 신규 릴스를
탐색 시간 안에서 저장하며 별도의 저장 개수 제한은 없습니다.

```powershell
.\collector.ps1 fashion-beauty --six-hour-new-only --background
```

`--six-hour-new-only`는 `--duration-hours 6 --new-only --base-output --max-upload-days 365`를 한 번에 적용합니다.
이 프리셋에서는 재수집 작업을 만들거나 실행하지 않습니다. `--background`는
저장된 로그인 프로필이 있을 때만 추가하세요. 처음 로그인할 때는 빼고 실행하면 됩니다.
Android 보강 모드에서는 웹에서 확인한 Reel을 먼저 저장합니다. 로그인한 신규 수집의 팔로워 조회 실패는 Reel 저장을 취소하지 않고 위의 프로필 재시도 기록으로 남깁니다. 리포스트 집계가 없는 경우에는 빈 값으로 보존합니다.

필요하면 같은 동작을 세부 옵션으로도 지정할 수 있습니다.

```powershell
.\collector.ps1 fashion-beauty --duration-hours 6 --new-only --base-output --max-upload-days 365 --background
```

`--fashion-hashtag-query`와 `--beauty-hashtag-query`로 선택한 도메인의 내장 키워드를 바꿀 수 있습니다.
`--base-output`을 지정하지 않은 예약 수집은 선택한 도메인의 `data/collector\reels.xlsx`, `data/collector\collection_data.xlsx`,
`data/collector\beauty_reels.xlsx`, `data/collector\beauty_users.csv` 같은 CSV·JSON·XLSX 및 상태 파일만
게시하며, 기존 기본 `reels.*`와 `users.*`는 건드리지 않습니다. 도메인별 공개 출력의
Reel 파일은 최초 수집과 재수집을 각각 별도 행으로 추가합니다. 각 행의 Android 지표와 후속 보완 값은 나중에 갱신될 수 있습니다. 재수집 경과 시간은
`hours_since_previous`로 표시됩니다. `reels.csv`, `reels.json`, `reels.xlsx`와 통합 엑셀의 `hours_since_upload`는 각 행의 수집 시각에서 업로드 시각을 뺀 시간(소수 둘째 자리)입니다. 시각이 없거나 잘못됐거나 업로드보다 수집 시각이 빠르면 빈 값으로 둡니다. 새 실행은 이전 실행에서 이미 기한을 넘긴 재수집을 즉시 연속 처리하지 않고,
이번 실행에서 최초 수집한 Reel을 `+4시간` 간격으로 재수집합니다. Ctrl+C를 한 번 누르면 실행을 중단하고
마지막으로 저장된 결과를 보존합니다.

`fashion`·`beauty`의 hybrid 예약 재수집은 저장된 Reel의 제목·해시태그·오디오 등 정적 정보를 재사용합니다.
웹은 작성자 프로필에서 팔로워 수를 읽고, 프로필 릴스 탭의 `play_count`에서 조회수를 읽습니다. 조회수를 위해 릴스 상세 페이지를 먼저 열지 않습니다. Android는 릴스 화면의 `content-desc`에서 좋아요·댓글·공유·재게시·저장 수를 읽습니다.
hybrid 예약 재수집에서는 릴스 상세 페이지를 열지 않습니다. Android에 축약형만 표시된 수치는 표시 환산값을 유지하며 정확한 정수로 간주하지 않습니다. 진행 로그는 최초 수집과 같은 `[Follower …] …, view_count=…` 및 `[Android]` 형식을 사용합니다.
조회수 누락으로 팔로워 큐 마지막에서 다시 조회할 때는 페이지 콘텐츠 배율을 25%로 낮춥니다. 최초·재시도 모두 조회수 탐색 1회에 60초 제한을 적용합니다(페이지 이동·초기 로딩 포함, 종료 정리·저장은 별도). 스크롤은 화면 높이의 80%(최대 900px)씩 내리며 이동 후 최대 1초 동안 정확값을 확인합니다. 실제 화면 안에서 대상을 확인하면 그 대상의 정확값을 최대 5초 기다립니다. 미해결 대상을 모두 확인했으면 추가 스크롤 없이 반환하고, 아직 못 찾은 대상이 있으면 전체 60초 예산 안에서 계속 찾습니다. 목록이 20회 연속 정체되면 제한보다 일찍 종료합니다. 제한시간 내 확보한 값은 유지하고 조회가 끝나면 콘텐츠 배율을 복원합니다. 최초 조회의 스크롤 상한은 350단계이지만 60초 제한도 함께 적용합니다.
팔로워 로그의 여러 조회수는 `view_count=643,451 / 37,426`처럼 `/`로 구분합니다. hybrid 재수집도 같은 실행 묶음의 작성자별로 프로필과 대상 조회수를 한 번에 읽고 한 줄로 출력하며, Android 작업과 Reel 스냅샷 저장은 영상별로 수행합니다.
Android 작업이 끝나기 전에는 새 행의 해당 수치가 빈 값으로 표시됩니다.

일시적 수집 오류로 예약 작업이 실패하면 같은 요청을 즉시 반복하지 않고 `5분`, `10분`, `20분`, `30분`
순서로 대기합니다. 같은 작업이 한 실행에서 5회 연속 실패하면 남은 실행 동안 재시도를 보류합니다.
실행 중 Instagram `429`가 감지되면 예약 재수집도 전체 일시정지에 포함됩니다.
일시정지 중에는 예약 호출의 제한 시간 때문에 브라우저를 닫지 않습니다.
터미널의 `resume` + Enter 입력으로 수집 동작을 다시 허용합니다. 재개 뒤 429가 재발하면 현재 배치만 종료하고, 예약 수집기는 최소 20분 또는 서버 `Retry-After` 중 더 긴 시간 동안 대기합니다. 다른 URL과 감독 루프의 오류 재시작도 이 대기를 건너뛰지 않습니다. 종료가 확정된 브라우저 컨텍스트는 반환된 배치를 정리한 뒤 닫고, 대기가 끝난 다음 배치에서 새로 만듭니다. 첫 429로 사용자 입력을 기다리는 브라우저는 자동으로 닫거나 재개하지 않습니다.

## 공개 출력 동기화

Excel을 열어 둔 동안 `reels_updated.xlsx`가 생성됐다면, Excel을 닫고 다음 명령으로 결과 파일을 동기화하세요.

```powershell
.\collector.ps1 reconcile
```

## 기존 데이터 정확값 재수집

기존 파일의 릴스를 다시 수집하면 최신 수치가 새 수집 회차의 행으로 추가됩니다.
재수집할 파일과 데이터 폴더를 선택하는 방법은 아래 예시를 참고하세요.

```powershell
.\collector.ps1 refresh --background
.\collector.ps1 followers
```

미확인 계정의 국가만 갱신하려면 다음 명령을 사용합니다.
기존 팔로워 수집 회차는 추가하지 않으며, 중단 후 같은 명령을 다시 실행해 이어갈 수 있습니다.

```powershell
.\collector.ps1 followers --account-country-only --data-dir .\data\collector\.datasets\fashion

# 브라우저 창을 표시하면서 국가 확인
.\collector.ps1 followers --account-country-only --foreground --data-dir .\data\collector\.datasets\fashion
# 동작 확인용: 미확인 계정 1개만 검사
.\collector.ps1 followers --account-country-only --account-country-limit 1 --data-dir .\data\collector\.datasets\fashion
```

특정 Excel 분석 파일의 행만 재수집하려면 `refresh.ps1`을 사용합니다. `--start-row`와 `--end-row`는
헤더 행을 포함한 Excel 화면의 행 번호이며 양 끝을 포함합니다. 선택된 행의 `url` 또는
`reel_url` 열에서 릴스 URL을 읽고, 중복 URL은 한 번만 재수집합니다. 범위를 생략하면 파일의
모든 릴스 URL을 사용합니다.

```powershell
.\refresh.ps1 --input-xlsx .\analysis.xlsx --start-row 10 --end-row 13 --data-dir .\data\collector --background
.\refresh.ps1 --data-dir .\data --collection-count 4 --interval-hours 4 --start-row 2 --end-row 100 --background
```

### 재수집 실행 시간

`refresh.ps1`의 `--duration-hours` 기본값은 **0(무제한)**입니다. 시간 제한을 두려면 예를 들어 `--duration-hours 8`을 지정하세요. 제한에 도달하면 진행 중인 수집 작업을 마무리한 뒤 다음 작업을 시작하지 않고 종료하며, 저장 이력과 대기열은 유지합니다. 무제한이어도 테스트·수집 횟수 조건이나 로그인 오류 등의 종료 조건은 적용됩니다.

### 재수집 시각 기준

`--uploaded`가 기본값입니다. `--uploaded`와 `--last-collected`는 값 없이 사용하며 동시에 지정할 수 없습니다. 업로드 시각을 기준으로 수집 시각을 맞춥니다.
초기 간격은 2시간으로 업로드 후 2·4·6시간에 수집합니다. 업로드 1시간 뒤 수집된 데이터를
중간에 추가해 저장하면 다음 예약은 업로드 후 2시간, 즉 약 1시간 뒤입니다.

```powershell
# 업로드 시각 기준 (기본)
.\refresh.ps1 --input-xlsx .\external.xlsx --data-dir .\data\refresh --uploaded --background
# 마지막 수집 시각부터 간격 계산
.\refresh.ps1 --input-xlsx .\external.xlsx --data-dir .\data\refresh --last-collected --background
```

`last-collected`에서는 마지막 수집이 업로드 1시간 뒤이고 초기 간격이 2시간이면, 다음은 업로드 후 3시간입니다.
두 방식 모두 `--interval-hours`로 지정한 고정 간격과 나이별 간격 설정에 적용됩니다.
`--uploaded`는 업로드 후 경과 시간으로 구간을 판단합니다. `--last-collected`는 첫 수집 후 경과 시간으로 구간을 판단하고, 마지막 수집 시각에서 간격을 계산합니다. 업로드 기준 예약에는 `uploaded_at`이 필요합니다. `--age-basis`로 구간 기준을 따로 지정할 수 있습니다.
늦게 추가했거나 작업이 지연되어 예약 시각이 지났으면 가능한 시점에 한 번 수집하고, 이후 업로드 기준
다음 시각을 따릅니다. 과거 회차를 가짜로 채우지 않으며 다른 작업 중이면 실제 시작은 늦어질 수 있습니다.
Python 명령에서는 `--uploaded` 또는 `--last-collected`를 사용합니다.

이 스크립트는 기존 `collector.ps1 refresh`의 웹·Android 재수집 경로를 호출합니다.
기본 실행은 게시 후 경과 시간에 따라 영상별 간격을 자동 전환하고, 168시간 이상인 영상은 재수집을 종료합니다.
프로그램은 계속 실행하면서 저장된 Excel의 새 행을 감시합니다(Ctrl+C로 종료).

| 게시 후 경과 시간 | 기본 간격 | PowerShell 옵션 |
| --- | --- | --- |
| 0~24시간 | 2시간 | `--initial-interval-hours` |
| 24~96시간 | 3시간 | `--middle-interval-hours` |
| 96~120시간 | 6시간 | `--late1-interval-hours` |
| 120~168시간 | 12시간 | `--late2-interval-hours` |

```powershell
.\refresh.ps1 --data-dir .\data --background
.\refresh.ps1 --data-dir .\data --initial-interval-hours 2 --middle-interval-hours 3 --late1-interval-hours 6 --late2-interval-hours 12 --background
```

테스트할 때 오래된 게시물에도 일정을 적용하려면 `--age-basis first-collected`를 사용합니다.

```powershell
.\refresh.ps1 --data-dir .\data --age-basis first-collected --background
# 빠른 재수집 확인: 모든 구간을 0.05시간(3분) 간격으로 설정
.\refresh.ps1 --data-dir .\data --age-basis first-collected --initial-interval-hours 0.05 --middle-interval-hours 0.05 --late1-interval-hours 0.05 --late2-interval-hours 0.05 --background
```

기본값 `--age-basis Uploaded`는 게시 시각 기준입니다. `FirstCollected`는 영상별 기존 기록에서
가장 이른 `collected_at`을 기준으로 24·96·120·168시간 구간을 계산합니다.
프로그램 실행 시각 기준으로 초기화하지 않으며, 이력에 남아 있는 첫 수집이 168시간을 지났다면
테스트 모드에서도 만료됩니다. 새 URL은 최초 조회로 저장된 수집 시각을 기준으로 시작합니다.
게시 시각과 기존 이력은 수정하지 않습니다. `--age-basis`는 수집 간격을 고르는 영상 나이의 기준이고, `--schedule-basis`는 실제 예약 시각을 정하는 기준입니다.
Python 옵션은 `--age-schedule --age-basis first-collected`입니다.
`--interval-hours`는 고정 간격 방식이므로 `--age-basis first-collected`와 함께 사용할 수 없습니다.

각 간격은 양수 시간이며 소수도 허용합니다. 구간 경계는 게시 시각 기준으로 고정됩니다.
선택한 Excel 파일의 URL을 재수집합니다. 기존 기록이 없는 새 URL은 먼저 1회 조회해 게시 시각을 확보한 뒤
구간별 간격을 적용합니다. 기존 이력의 게시 시각이 없거나 수집 시각이 잘못되면 오류로 중지하며 추정하지 않습니다.
다음 수집은 마지막 실제 수집 시각에 현재 구간 간격을 더한 시점입니다.
대기 중에도 24·96·120·168시간 경계에서 일어나 구간과 간격을 다시 계산합니다.
이미 기한이 지난 영상은 즉시 수집하지만 누락된 횟수를 연속으로 따라잡지는 않습니다.
168시간 이상인 영상은 새 수집을 시작하지 않습니다. 이미 시작된 수집은 완료될 수 있습니다.
선택한 영상이 모두 168시간에 도달해도 새 행 추가를 기다립니다.

실행 중 원본 Excel의 `url` 또는 `reel_url` 열에 행을 추가하고 **저장**하면 대기 중 최대 30초 이내에
새 URL을 발견합니다. 수집 중 저장한 행은 현재 수집 묶음이 끝난 뒤 확인합니다.
처음에 URL이 없는 파일도 헤더가 있으면 감시를 시작할 수 있습니다.
이미 등록한 URL은 중복 등록하지 않으며, Excel에서 행을 삭제해도 기존에 등록된 대상은 유지합니다.
`--start-row`/`--end-row`를 지정했다면 새 행도 해당 범위 안에 있어야 합니다.
파일이 저장 중이거나 잠겨 읽지 못하면 30초 후 다시 시도합니다.
수동 입력이 사라지지 않도록 감시 중인 원본 파일은 수집 결과로 덮어쓰지 않습니다.
예를 들어 `reels.xlsx`를 감시하면 입력 원본과 출력이 같은 경우 `reels_updated.xlsx`에 결과를 저장하고,
원본과 다른 CSV/JSON 및 내부 이력은 기존 위치에 갱신합니다.
별도 `analysis.xlsx`를 감시하면 기존 수집 결과 파일들은 원래 위치에 저장됩니다.
`--collection-count`를 명시하면 최초 수집을 포함한 총 수집 횟수 상한도 적용합니다(기본 상한 없음).
기존 고정 간격 방식은 `--collection-count 4 --interval-hours 4`로 사용합니다.
`--interval-hours`를 명시하면 구간별 일정 대신 고정 간격 방식으로 동작하며,
횟수를 생략했을 때는 기존처럼 총 4회를 적용합니다.
Python에서 직접 실행할 때는 `collector.ps1 refresh --age-schedule`과
`--initial-interval-hours`, `--middle-interval-hours`, `--late1-interval-hours`,
`--late2-interval-hours`를 사용합니다. `collector.ps1 refresh`만 실행하면 기존 1회 실행입니다.
반복 실행 중 일시 오류로 새 이력이 저장되지 않은 URL은 5분부터 대기 시간을 늘려 최초 시도 포함 최대 5회 시도합니다.
이미 저장된 URL은 같은 배치의 실패 때문에 다시 수집하지 않습니다. 로그인·인증이 필요하거나 중지 요청이 있으면 종료합니다.
`--start-row`와 `--end-row`는 Excel에 보이는 행 번호이며 양 끝을 포함합니다. 생략하면 전체 URL을 사용합니다.
`--data-dir .\data` 아래에 Fashion 내부 이력만 있으면 `.datasets\fashion`의 이력을 이어 쓰고
`reels.*`와 `users.*`를 갱신합니다. Fashion과 Beauty가 모두 있으면
`--input-xlsx .\data\reels.xlsx` 또는 `beauty_reels.xlsx`로 선택합니다.
같은 `--data-dir`에서 예약 수집기가 실행 중이면 먼저 해당 예약 수집기를 중지해야 합니다.
선택한 범위의 URL을 재수집합니다. `--max-items`를 지정하면 해당 개수까지만 처리합니다.
이전 수집 회차를 이어 쓰려면 `--data-dir`에 기존 수집 폴더를 지정하세요.
외부 XLSX도 `data/collector`과 같은 컬럼 형식이면 기존 스냅샷을 재수집 기준으로 가져옵니다.
행 형태와 회차별 가로 컬럼 형태를 지원하며, 선택한 URL의 과거 회차를 보존해 다음 회차를 추가합니다.
동일 URL·수집 시각은 중복으로 넣지 않고, 이미 저장된 값이 있으면 기존 값을 유지합니다.
URL만 있는 파일은 과거 스냅샷을 만들지 않고 첫 조회로 필요한 정보를 확보합니다.
입력 XLSX는 덮어쓰지 않습니다. 결과 경로가 입력과 같으면 `_refresh.xlsx`로 저장합니다.
외부 파일 이름이 패션·뷰티 출력 이름이 아니면 기존 도메인을 임의로 선택하지 않고 `--data-dir`에 저장합니다.
`-Avd`는 `collector.ps1`과 동일하게 사용할 에뮬레이터 이름을 지정합니다.

```powershell
.\refresh.ps1 --input-xlsx 'D:\import\reels.xlsx' --data-dir .\data\refresh -Avd '내AVD이름' --background
```

브라우저 선택, Android 준비·릴스 검증·지표 수집은 `collector.ps1`과 같은 경로를 사용합니다.
기본 게시 나이별 재수집 간격과 168시간 종료 조건도 유지됩니다. 기존 횟수 포함 목표 수집 횟수와 고정 간격을
원하면 `--collection-count 4 --interval-hours 4`처럼 지정하세요.

## 팔로워 정보 갱신

저장된 작성자의 팔로워 정보를 갱신하려면 다음 명령을 실행합니다.
기존 데이터가 다른 폴더에 있다면 `--data-dir`도 지정하세요.

```powershell
.\collector.ps1 followers
```

## 통합 실행 스크립트

명령은 이 폴더에서 `collector.ps1` 뒤에 지정합니다. 명령을 생략하거나 첫 인수가 옵션이면
`collect`가 실행됩니다. `collector.ps1`은 옵션 검사 전에 Android 에뮬레이터를 시작할 수
있으므로 도움말만 볼 때는 Python 진입점을 사용하세요.

```powershell
.\.venv\Scripts\python.exe .\scripts\instagram_reels_python.py --help
.\.venv\Scripts\python.exe .\scripts\instagram_reels_python.py fashion --help
.\.venv\Scripts\python.exe .\scripts\instagram_reels_python.py collect --help
```

| 명령 | 용도 | 주로 쓰는 옵션 |
|---|---|---|
| `collect` | Reel 신규 수집 | `--hashtag-query`, `--max-items`, `--max-upload-days`, `--start-url` |
| `refresh` | 기존 Excel의 Reel 재수집 | `--input-xlsx`, `--start-row`, `--end-row` |
| `followers` | 저장된 사용자 팔로워 갱신 | `--follower-interval-seconds`, `--data-dir` |
| `fashion`, `beauty`, `fashion-beauty` | 도메인별 예약 신규 탐색과 재수집 | `--duration-hours`, `--discovery-hours` |
| `hashtag-posts` | Android Tags 검색과 웹의 정확 게시물 수 보완 | `--hashtag-query` 또는 `--preset` |
| `android-worker` | 남은 Android 지표 큐 처리 | `--data-dir`, `--android-device-id` |
| `xlsx`, `reconcile` | 공개 Excel 동기화, 내부 이력과 공개 출력 대조 | `--data-dir` |

### 공통 실행·저장 옵션

| 옵션 | 적용 명령 | 의미 |
|---|---|---|
| `--data-dir` | 모든 명령 | 출력과 이력의 루트 디렉터리. 기본값은 신규 수집 `data/collector`, 재수집 `data/refresh`입니다. 기존 작업을 이어갈 때는 해당 경로를 사용합니다. |
| `--collector-mode` | `collect`, `refresh`, 예약 수집 | `hybrid`(기본값), `web` 중 선택합니다. `--no-android-metrics`는 `web`, `--android-metrics`는 `hybrid` 별칭입니다. |
| `--background` | 수집·재수집·예약 수집 | 웹 모드에서는 저장된 로그인으로 브라우저 창 없이 실행합니다. 새 Android 에뮬레이터를 시작할 때는 `-no-window`를 사용하며, 이미 열려 있는 에뮬레이터 창은 그대로 둡니다. 첫 로그인에는 빼세요. |
| `-Avd` | `collector.ps1` 실행 | 온라인 기기가 없을 때 시작할 AVD 이름입니다. `INSTAGRAM_ANDROID_AVD` 또는 유일한 설치 AVD를 사용하며, 여러 개면 직접 선택합니다. `--android-device-id`의 ADB serial과 다릅니다. |
| `--android-device-id`, `--android-adb-path` | Android가 필요한 명령 | 각각 사용할 ADB 장치 serial과 `adb.exe` 경로를 지정합니다. |
| `--android-ui-delay-seconds` | Android 지표 워커·Tags 작업 | Android UI 동작 간격입니다. 일반 하이브리드·`android-worker` 기본 0.35초, `hashtag-posts` 기본 0.2초입니다. |
| `--collect-hashtag-media-count` | 하이브리드 `collect`, 예약 수집 | 탐색 중 Android Tags의 게시물 수도 수집합니다. 기본값은 꺼짐이며 예약 수집의 `web` 모드에서는 사용할 수 없습니다. |

일반 `collect`·`refresh`에서 Android 결과까지 기다려야 하면 `--android-metrics-required`를
추가합니다.
일반 `collect`에서 Reel 큐가 비었을 때 별도 Tags 검색을 예약하려면
`--android-idle-hashtag-query '패션 OR 뷰티'`를 사용합니다.



### 일반 `collect`·`refresh`·`followers` 옵션

| 옵션 | 기본값 | 의미와 제약 |
|---|---:|---|
| `--max-items` | 50 | 일반 `collect`의 저장 목표입니다. 해시태그 수집에서 0은 후보를 계속 탐색합니다. `refresh`에서는 처리 URL 수를 제한합니다. |
| `--hashtag-query '패션 OR 데일리룩'` | 없음 | 일반 `collect`에서 검색할 해시태그입니다. 예약 수집의 도메인별 `--fashion-hashtag-query`와 다릅니다. |
| `--hashtag-candidates-per-keyword` | 0 (자동) | 일반 `collect`에서 해시태그당 검사할 URL 후보 수입니다. 0 이상이어야 하며 저장 목표와 별개입니다. 예약 수집의 기본값은 200입니다. |
| `--max-upload-days` | 0 (필터 없음) | 신규 Reel의 업로드 경과일 상한입니다. 0 이상이어야 합니다. 예약 수집에서는 기본 30일이고 `--maxdays` 별칭도 지원합니다. |
| `--interval-seconds` | 5 | 일반 웹 수집의 처리 간격입니다. 1초 이상이어야 합니다. |
| `--start-url` | Instagram Reels 홈 | 일반 수집의 시작 주소입니다. HTTPS Instagram 주소를 사용합니다. |
| `--urls-file` | 없음 | `collect`에서 URL을 한 줄씩 담은 파일을 직접 재수집할 때 사용합니다. |
| `--input-xlsx`, `--start-row`, `--end-row` | 데이터 폴더의 `reels.xlsx`, 전체 행 | `refresh`에서 Excel URL과 포함할 행 범위를 고릅니다. 기본 파일이 없으면 `refresh_data.xlsx`를 사용합니다. |
| `--follower-interval-seconds` | 8초 | 팔로워 프로필 방문 간격입니다. 1초 이상이며 `followers`에서도 조절할 수 있습니다. |
| `--followers-after-reels` | 꺼짐 | 일반 신규 수집에서 Reel 탐색이 끝난 뒤 작성자 프로필을 조회합니다. |
| `--manual` | 꺼짐 | 일반 웹 수집을 수동 진행합니다. `--background`와 함께 쓸 수 없습니다. |
| `--checkpoint-items`, `--page-recycle-items` | 100, 200 | 각각 저장 체크포인트와 브라우저 페이지 교체 간격입니다. 전자는 1 이상, 후자는 0 이상입니다. |
| `--profile-dir`, `--output-stem` | `runtime\browser_profile\.instagram_chrome_profile`, `reels` | 각각 저장된 브라우저 로그인 경로와 일반 공개 출력 파일의 접두어입니다. |
| `--transition-timeout-seconds` | 3 | 일반 Reel 전환을 기다리는 시간입니다. 0.5~30초 범위입니다. |

`--exact-metric-attempts`(기본 3회)와 `--exact-metric-retry-delay-seconds`(기본 2초)는 지표 재시도에 사용합니다.
`--direct-reel-info-wait-seconds`와 `--direct-concurrency`는 기존 명령 호환용이며 도움말에서 숨깁니다.

예약 수집에는 위 [예약 수집 옵션 조합](#fashion-예약-수집-옵션-조합)의 표 외에도
`--new-only`(재수집 없이 신규만), `--base-output`(도메인 접두어 대신 기본 `reels.*`·`users.*`),
`--resume-started-at`(시간대를 포함한 ISO 8601 원래 시작 시각으로 재개)을 사용할 수 있습니다.
`--shared-data`는 `--base-output`과 같고, `beauty`·`fashion-beauty`에는
`--beauty-hashtag-query`로 뷰티 키워드를 바꿀 수 있습니다.
`--six-hour-new-only`는 `fashion-beauty` 전용 6시간 신규 수집 프리셋입니다.
`--test-single-hashtag`는 시험 실행에서 각 도메인의 첫 키워드 하나만 사용합니다.
`hashtag-posts`에는 `--hashtag-query '패션 OR 데일리룩'` 또는
`--preset fashion`, `beauty`, `fashion-beauty` 중 하나를 반드시 지정합니다.

```powershell
.\collector.ps1 collect --hashtag-query '패션 OR 데일리룩' --max-items 50 --max-upload-days 7 --background
.\collector.ps1 refresh --input-xlsx .\analysis.xlsx --start-row 10 --end-row 13 --background
.\collector.ps1 hashtag-posts --preset fashion-beauty
.\collector.ps1 android-worker --data-dir .\data\collector\.datasets\fashion
```

조회수 Android 보조 작업은 일반 릴 지표보다 낮은 우선순위로 실행됩니다.
실패하면 10초·20초 뒤 큐의 뒤쪽에서 다시 시도하며, 최초 시도 포함 최대 3회입니다.
다른 지표 작업에 양보하거나 동일 수집 행의 조회수가 웹에서 채워지면 재시도 횟수를 소비하지 않습니다.
각 탐색의 기존 60초 제한과 화면 정체 종료 조건을 유지하며, 대상 URL을 직접 열고 작성자를 확인한 뒤 Likes 화면의 정확한 정수 조회수만 반영합니다. 공유 버튼과 Copy link는 사용하지 않습니다.
최종 실패는 `.datasets/<도메인>/.collector/android_metric_queue/view_failed/`에 대상 수집 시각·작성자·실패 원인·시도 횟수와 함께 보존합니다.
이 기록은 자동으로 무한 재실행되지 않습니다. 이미 끝난 과거 수집 행에 현재 조회수를 소급해서 채우는 작업도 수행하지 않습니다.


### 공통 실행 예시

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
.\collector.ps1 --hashtag-query '패션 OR 데일리룩 OR 오오티디 OR ootd OR 코디추천 OR 패션스타그램' --max-items 0 --max-upload-days 7 --background --interval-seconds 1
```

첫 로그인 전에는 `--background`를 빼고 실행합니다.

포그라운드 예약 수집은 실행 후 처음 표시되는 로그인 준비 확인에서만 Enter를 누르면 됩니다. 같은 Python 프로세스가 계속 실행되는 동안에는 이후 수집 창, 브라우저 재생성, 429 대기 후 재개와 공유 브라우저 작업이 최초 승인을 자동으로 재사용합니다. 프로그램을 완전히 종료한 뒤 새로 실행하면 로그인 상태 확인을 위해 Enter를 다시 한 번 요청합니다.

## Android 연결이 끊긴 경우

에뮬레이터가 꺼졌다면 Device Manager에서 다시 켜고 Instagram 로그인 상태를 확인하세요.
기기가 연결되면 남은 작업을 이어 처리할 수 있습니다.

```powershell
# 지정한 데이터 폴더의 남은 Android 작업 처리
.\collector.ps1 android-worker --data-dir .\data\collector\.datasets\fashion
```

문제가 반복되면 진단 기록을 남길 수 있습니다. `emulator-5554`는 실제 기기 serial로 바꾸세요.

```powershell
.\scripts\diagnostics\watch-android-diagnostics.ps1 -Background -DeviceId emulator-5554 -DataDir .\data\collector\.datasets\fashion
.\scripts\diagnostics\watch-android-diagnostics.ps1 -Status
# 진단 기록기만 종료
.\scripts\diagnostics\watch-android-diagnostics.ps1 -Stop
```

## 선택 기능: 패션 릴스 분석

Fashion 수집 후 조회수 대비 팔로워 비율이 높은 릴스는 Gemini 자동 분석 대상이 될 수 있습니다.
기준은 `reaction_rate` 9000% 이상이며, 분석을 사용하려면 [분석기 설치와 API 설정](../analyzers/README.md)이 필요합니다.
API 사용량과 비용은 사용 중인 프로젝트의 조건을 확인하세요.
분석 설정이 없거나 분석에 실패해도 수집된 릴스는 유지됩니다.

결과는 `data/collector/reel_analysis.json`, 요약은 `reel_insights.json`과
`reel_insights.html`에서 확인할 수 있습니다.

## 실행 옵션 표기

| 공식 사용법 | 유지하는 이전 별칭 |
|---|---|
| `--collector-mode web` | `--no-android-metrics` |
| `--collector-mode hybrid` | `--android-metrics` |
| 예약 수집의 `--max-upload-days` | `--maxdays` |
| `--base-output` | `--shared-data` |

`fashion`은 패션 예약 수집을 선택하는 명령입니다. `--fashion-hashtag-query`는
그 명령의 내장 패션 검색 키워드를 바꾸는 옵션이며 `fashion-beauty`에서도 사용할 수 있습니다.

```powershell
.\collector.ps1 fashion --collector-mode web --fashion-hashtag-query '패션 OR 데일리룩'
```

로그인한 계정으로 수집하며 팔로워 갱신에는 `followers` 명령을 사용합니다.
팔로워만 갱신할 때는 `.\collector.ps1 followers`를 사용합니다. 방문 간격은 기본 8초이며
`--follower-interval-seconds 3`처럼 조절할 수 있습니다.


## 실행 종료와 결과값 확인

- 종료하려면 터미널에서 **Ctrl+C**를 누르세요. 저장 작업이 마무리될 때까지 기다립니다.
- 설정한 실행 시간이 끝나도 진행 중인 수집과 저장이 완료될 때까지 더 걸릴 수 있습니다.
- 코드 업데이트 후에는 실행 중인 수집기와 Android 작업기를 다시 시작해야 변경 사항이 적용됩니다.
- 결과의 `0`은 0으로 확인된 값, 빈칸은 미확인·미수집 값, `X`는 비공개 또는 비활성 표시입니다.
- 영상 길이는 소수점 한 자리, 수집 시각은 초 단위, 반응률과 증감률은 퍼센트로 표시합니다.
- 공개 CSV·JSON의 수집 시각은 한국 표준시(`+09:00`), Excel의 날짜 셀도 한국 표준시입니다. 내부 이력은 원래 시각과 정밀도를 유지합니다.
- `users.csv`와 통합 파일의 `users` 시트는 같은 수집 차수·수집 간격·팔로워 증감값을 표시합니다.
- 계정 국가는 같은 작성자의 모든 회차에 동일하게 표시합니다. 국가가 확인되지 않으면 빈칸으로 남습니다.

`refresh.ps1`도 `collector.ps1`과 동일하게 `--input-xlsx`, `--uploaded`, `--background`처럼 소문자·하이픈 옵션을 사용합니다. PowerShell 매개변수 표기도 지원합니다. 에뮬레이터 선택은 두 명령 모두 `-Avd`를 사용합니다.

재수집 기준은 `--uploaded`(기본) 또는 `--last-collected`로 선택하고, 창 숨김 실행은 `--background`로 지정합니다. `--schedule-basis`로도 실행 기준을 지정할 수 있습니다.

## 소량 수집 테스트

```powershell
.\collector.ps1 fashion --test --background
.\refresh.ps1 --input-xlsx .\external.xlsx --test --background
```

`--test`는 실제 Instagram 수집을 진행합니다. 예약 수집은 신규 릴스 최대 10개를 약 5분 동안
탐색하고, 최초 포함 최대 4회까지 5분 간격으로 수집합니다. 전체 실행 상한은 20분입니다.
검색 결과·수집 속도·접근 제한에 따라 10개보다 적거나 일부 회차를 완료하지 못할 수 있습니다.
`refresh`는 선택한 URL 중 앞의 최대 10개에 대해 기존 이력에 3회 추가 수집한 뒤 종료합니다.
마지막 수집부터 5분 간격을 적용하며, 이미 기한이 지났으면 첫 회는 즉시 시작합니다.
테스트에서는 일반 나이별 간격 대신 5분 간격을 적용합니다.
저장 폴더를 생략하면 각각 `data_test`, `data/refresh_test`를 사용합니다.
`--data-dir`을 직접 지정하면 해당 폴더에 저장합니다. 입력 XLSX는 보호합니다.

## refresh 입력파일과 데이터파일 분리

`refresh.ps1`은 입력함 모드로 동작합니다. `--input-xlsx`는 외부에서 수집한 행을 넣는 입력파일이고,
`--data-dir` 안의 `reels.xlsx`, `reels.csv`, `reels.json`은 수집 이력이 계속 쌓이는 데이터파일입니다.
입력파일에 데이터파일을 지정하지 마세요. 기본 입력 이름은 `input.xlsx`입니다.

```powershell
.\refresh.ps1 --input-xlsx .\data\refresh_test\input.xlsx --data-dir .\data\refresh_test --test --background
```

입력파일에 행을 붙여 넣고 **저장한 뒤 Excel 파일을 닫으세요**. 백그라운드 감시 작업이 약 1초 간격으로
닫힘·파일 잠금을 확인합니다. 큐와 복구용 원본 복사본을 먼저 디스크에 저장한 후 접수한 행만 입력파일에서
제거합니다. `INPUT_CLOSED -> QUEUED` 메시지와 `refresh_inbox_queued` 로그가 남습니다.
미접수 행, 다른 시트와 헤더는 유지합니다. `--test`에서는 최대 10개 URL만 접수하며 나머지는 입력에 남습니다.

실제 수집기는 현재 작업을 마친 뒤 새 큐를 읽습니다. 외부 1차 이력을 데이터파일에 먼저 기록한 뒤
업로드/마지막 수집 기준의 예약 시각에 다음 회차를 추가합니다. 입력을 비운 뒤 재시작해도
`.collector/refresh_inbox/queue.json`과 배치 XLSX에서 복구합니다. 이 폴더는 큐와 복구 사본이므로
수집 중 삭제하지 마세요. 일반 모드는 새 입력을 계속 기다리며, 테스트 모드는 접수된 URL의 추가 수집이
완료되면 종료합니다.

기존 `collector.ps1 refresh` 직접 호출은 읽기 전용 입력 경로를 유지합니다. 그 명령에서 입력함을 쓰려면
`--inbox`를 추가합니다. 위 입력함 모드에서는 이전 설명의 ‘입력 XLSX 보호’ 대신 **접수 완료 행 제거**가
적용됩니다. 프로그램은 입력파일과 데이터파일이 같은 파일이면 실행을 거부합니다.

### shortcode로 수집 종료

입력파일에 **수집종료** 시트를 만들고 A1을 `shortcode`로 적습니다. A2부터 중지할 shortcode를
한 줄에 하나씩 입력하세요. 릴스 URL 전체도 사용할 수 있습니다. 테스트 입력파일에는 이 시트가 준비되어 있습니다.
저장한 뒤 Excel 파일을 닫으면 취소 요청이 영속 큐에 기록되고 접수된 요청 행이 비워집니다.
`COLLECTION_CANCELLED` 메시지와 `refresh_inbox_cancelled` 로그로 확인할 수 있습니다.
형식이 잘못된 요청은 지우지 않고 입력에 남깁니다.

해당 URL은 미처리 입력 배치 및 이후 재수집 대상에서 제외됩니다. 이미 저장한 데이터는 삭제하지 않습니다.
현재 진행 중인 웹·Android 수집 회차는 즉시 강제 중단하지 않으므로 일부 값이 더 저장될 수 있습니다.
그 회차를 마친 뒤부터 예약 목록에서 빠집니다. 취소는 재시작 후에도 유지하며, 같은 입력 배치에서
추가와 취소가 함께 요청되면 취소를 우선합니다. 취소 기록이 남은 URL은 자동 재등록되지 않습니다.

### 실측 처리량에 따른 입력 접수 제한

입력함 모드는 실제 수집 배치의 실행 시간과 새 스냅샷 저장 완료 건수를 측정합니다. 입력으로 가져온
과거 행은 측정 건수에 포함하지 않습니다. 예약을 기다린 시간도 제외합니다. 최근 1시간의 측정값을
합산한 처리 속도의 80%를 시간당 접수 용량으로 사용합니다. 실패한 배치의 실행 시간도 반영하므로
실패가 늘면 추정 용량이 줄어듭니다. 이는 보수적인 추정치이며 처리 속도를 보장하지는 않습니다.

충분한 측정값이 없을 때는 최대 10개 URL만 우선 접수합니다. 실제 작업 시간이 60초 이상 쌓이거나
10건 이상 저장되면 측정 용량을 적용합니다. 재수집 예약과 아직 메인 수집기가 반영하지 않은 입력 큐도
용량을 차지합니다. 5분 간격이라면 한 릴스가 시간당 최대 12회분을 사용하며, 목표 수집 횟수가 더 적으면
남은 횟수까지만 예약합니다. 한 시간보다 긴 간격도 접수 시 최소 1회분을 확보합니다.

용량이 부족한 행은 큐에 넣거나 지우지 않고 입력파일에 그대로 남깁니다. 처리 완료·취소 또는 새 측정으로
여유가 생기면 파일을 다시 저장하지 않아도 접수를 재검토합니다. 입력파일이 열려 있으면 닫을 때까지
대기합니다. `CAPACITY_MEASURED`, `CAPACITY_FULL` 메시지로 확인하며 측정값과 예약 부하는
`데이터폴더/.collector/refresh_inbox/capacity.json`에 저장합니다. 재시작 시 실제 대기 작업을 다시 계산합니다.
`--test`의 최대 10개 제한은 처리량 제한과 별도로 유지됩니다.

### PC별 시간당 접수 상한

`collectors/refresh.local.json`에서 이 PC의 상한을 지정합니다.

```json
{
  "max_collections_per_hour": 700
}
```

기존 예약량과 새 입력의 예상 재수집 횟수를 합쳐 **시간당 700건을 넘는 입력은 접수하지 않습니다**.
정확히 700건까지 허용하며, 초과 행은 입력파일에 남깁니다. 릴스 개수 자체의 제한은 아닙니다.
예를 들어 5분 간격의 릴스는 시간당 최대 12회분으로 계산합니다.
자동 측정 용량이 설정값보다 낮으면 더 낮은 값을 적용하고, 측정 전 최대 10개 우선 접수 제한도 유지합니다.
이미 등록된 작업은 한도를 낮춰도 삭제하지 않으며, 여유가 생기기 전까지 신규 접수만 막습니다.

다른 PC에서는 `refresh.example.json`을 `refresh.local.json`으로 복사한 뒤 숫자를 조정하세요.
`null` 또는 설정 파일 생략은 수동 상한 없이 자동 측정 제한만 사용한다는 뜻입니다.
`refresh.local.json`은 Git에서 제외하므로 이 PC의 700건 설정이 다른 PC에 배포되지 않습니다.

실행 중 파일의 숫자를 바꾸면 다음 입력 확인 시 반영되며, 프로그램을 재시작하거나 입력 Excel을 다시
저장할 필요는 없습니다. 설정 파일 형식이 잘못되면 신규 접수를 보류하고 입력 행을 보존합니다.
이 기능을 추가하기 전에 실행한 프로세스에는 코드 변경을 적용하기 위한 재시작이 한 번 필요합니다.

Foreground Android 준비는 온라인 기기가 하나 있으면 재사용하고, 없으면 설치된 AVD 목록의 첫 기기를 자동 실행합니다. 여러 AVD 중 특정 기기를 사용하려면 `-Avd 이름`을 지정하세요. `refresh.ps1`도 같은 준비 코드를 사용합니다. Background에서 여러 AVD가 있으면 기존처럼 `-Avd` 지정이 필요합니다. 부팅 완료와 Instagram 설치를 확인한 뒤 진행하며 로그인 데이터는 초기화하지 않습니다.

## 데이터 저장과 결과 확인

신규 수집은 `data/collector`, 재수집은 `data/refresh`에서 관리합니다. 재수집 대상은 `input.xlsx`로 입력하고 릴스와 사용자 결과는 각각 CSV·JSON·Excel로 확인합니다. 통합 결과는 신규 수집의 `collection_data.xlsx`, 재수집의 `refresh_data.xlsx`입니다.

통합 파일의 `reels` 시트는 `reels.xlsx`와 같은 열 순서를 사용합니다. 두 번째 열 `hours_since_previous`는 동일 릴스의 직전 수집 이후 경과 시간이며 최초 수집은 빈칸입니다. 사용자 이력에서는 회차별 팔로워 수와 증감을 확인합니다.

공개 릴스 결과에는 `korean_source`, `view_count_observed_at`, `view_count_source`, `android_mismatch`를 표시하지 않습니다. 판정 근거와 재시도 정보는 내부 이력에 보존합니다. 데이터 영역의 `.collector`와 `.datasets`는 이력·큐·예약 상태에 사용하며, 로그인과 모니터·Android 운영 상태는 `runtime`에서 관리합니다.
