# Instagram crawling

기본 구현, 로그인 Python 구현, 무로그인 Python 구현을 각각 독립된 폴더로 정리했습니다.

| 버전 | 폴더 | 설명 |
|---|---|---|
| 기본 버전 | `default_version` | JavaScript 수집기와 PowerShell 실행 스크립트 |
| Python 버전 | `python_version` | 조회수를 포함하고 재수집 값을 새 열에 누적하는 Python 수집기 |
| 무로그인 Python 버전 | `python_no_login_version` | 로그인 수집으로 저장한 신규 릴스만 임시 브라우저로 재수집하며 로그인 프로필을 저장하지 않음 |
| Android 에뮬레이터 버전 | `android_emulator_version` | Android Studio 에뮬레이터의 로그인된 Instagram 앱 화면을 ADB/UIAutomator로 읽는 수집기 |

```text
Instagram-crawling/
├── default_version/  # 기존 코드, 데이터, 로그인 프로필, 결과물
├── python_version/   # Python 코드와 전용 데이터·로그인 프로필
├── python_no_login_version/  # 공개 릴스용 무로그인 실행기
├── android_emulator_version/ # Android Studio 에뮬레이터 전용 수집기
└── README.md         # 각 버전의 시작 안내
```

## 기본 버전 실행

```powershell
cd C:\Instagram-crawling\default_version
.\scripts\start_reels_web.ps1
```

자세한 사용법은 `default_version\README.md`를 확인하세요.

## Python 버전 실행

```powershell
cd C:\Instagram-crawling\python_version
.\.venv\Scripts\python.exe .\collectors\instagram_reels_browser.py --max-items 50
```

설치와 출력 형식은 `python_version\README.md`에 정리되어 있습니다.

## 무로그인 Python 버전 실행

```powershell
cd C:\Instagram-crawling\python_no_login_version
.\.venv\Scripts\python.exe .\collect_public_reels.py
```

무로그인 재수집은 `python_version\data_web\reels.xlsx`를 대상으로 같은 데이터 폴더에 결과를 이어서 저장합니다. 공개 콘텐츠 제한과 사용법은 `python_no_login_version\README.md`에 정리되어 있습니다.

## Android 에뮬레이터 수집

Android Studio 에뮬레이터에서 Instagram에 직접 로그인한 뒤, 화면에 보이는 Reel 지표를 ADB/UIAutomator로 수집합니다. 이 버전은 웹 수집기와 데이터·로그인 정보를 공유하지 않으며 좋아요·팔로우·댓글 등 계정 행동을 자동화하지 않습니다.

```powershell
cd C:\Instagram-crawling\android_emulator_version
.\collector.ps1 feed --max-items 50
```

해시태그 검색과 기존 URL XLSX 재수집도 지원합니다. 설치와 전체 사용법은 `android_emulator_version\README.md`를 확인하세요.
