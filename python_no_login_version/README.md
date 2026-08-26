# 로그인 없는 Python 릴스 재수집기

로그인 수집기가 **처음 저장한 릴스만** 임시 익명 브라우저로 다시 수집하는 버전입니다. 실행할 때마다 임시 브라우저 컨텍스트를 만들며 쿠키나 로그인 세션을 별도 프로필에 저장하지 않습니다.

신규 릴스를 탐색하거나 URL을 직접 입력해 수집하는 기능은 이 실행기에 없습니다. 수집·저장 엔진은 형제 폴더인 `python_version`의 검증된 코드를 공유하므로, 두 폴더의 상대 위치를 유지해야 합니다.

## 사용 순서

1. `python_version`에서 로그인해 신규 릴스를 수집합니다. 이때 `python_version\data_web\reels.xlsx`가 자동 생성됩니다.
2. 이 폴더에서 아래 명령으로 해당 신규 릴스 목록만 재수집합니다.

```powershell
cd C:\Instagram-crawling\python_no_login_version
.\.venv\Scripts\python.exe .\collect_public_reels.py refresh --background
```

`refresh`는 생략할 수 있습니다. 일부만 재수집하려면 `--limit`을 사용합니다. `0` 또는 생략은 전체를 의미합니다.

```powershell
.\.venv\Scripts\python.exe .\collect_public_reels.py --limit 50 --background
```

## 설치

```powershell
cd C:\Instagram-crawling\python_no_login_version
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

## 저장 위치와 구조

재수집 결과는 별도 폴더가 아니라 로그인 Python 수집기와 같은 `python_version\data_web`에 이어서 저장합니다. 같은 릴스의 2차 이후 값은 `2nd collect_*` 같은 새 열에서 확인할 수 있습니다.

| 파일 | 구조 |
|---|---|
| `reels.csv` | 릴스 하나당 한 행, 2차 이후 값은 새 열로 저장 |
| `reels.json` | CSV와 같은 구조 |
| `reels.xlsx` | 기본 결과이자 이 재수집기의 대상 목록 |

조회수는 `view_count` 열에 저장됩니다. `--interval-seconds`, `--background`, `--data-dir` 옵션을 사용할 수 있습니다. `--data-dir`을 주면 그 폴더의 `reels.xlsx`를 읽고 같은 폴더에 결과를 저장합니다.

## 제한 사항

- 공개 계정의 공개 릴스만 대상입니다.
- Instagram이 익명 방문자에게 로그인 화면을 강제하거나 요청을 제한하면 재수집할 수 없습니다.
- 로그인 우회, 차단 우회 또는 비공개 콘텐츠 접근 기능은 포함하지 않습니다.

## 테스트

```powershell
.\.venv\Scripts\python.exe -B -m unittest test_collect_public_reels
```
