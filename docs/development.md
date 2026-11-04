# 폴더 정리와 실행 환경 이식성

## monitor 폴더 분류 (2026-10-31)

`backend/`에 Python 서버·상태·작업 실행·분석 연결 코드를, `web/`에 HTML·JavaScript·CSS를, `docs/`에 디자인·UX 계약·추천 설계·검증 자료를 모았다. 최상위 파일은 `README.md`, `start.ps1`, `stop.ps1`만 유지한다. 기존 URL과 실행·종료 명령은 그대로이며 데이터와 운영 기록은 기존 `data/`, `runtime/monitor/`를 사용한다.

백엔드 기준 경로·정적 파일 제공·작업 실행 경로·Gemini 어댑터 위치·테스트 import 경로를 함께 수정했다. 화면 파일 16개의 SHA-256이 모두 일치해 내용은 바뀌지 않았다. 모니터·데이터 연결·작업 실행 관련 Python 테스트 27개와 Node 산점도 테스트가 통과했다. 실제 실행기로 검증 포트에서 서버를 띄워 네 화면과 정적 자원의 HTTP 200, 분석 189건과 재수집 데이터셋 1개를 확인했다. 브라우저에서 실시간 연결·수집 기록 렌더링을 확인한 뒤 탭과 임시 서버를 종료했다. 기존 기본 포트에 새 서버를 남기거나 실제 수집·추천 생성 요청을 실행하지 않았다.

## 최상위 설정·설치 파일 위치 복원 (2026-10-31)

사용자 요청에 따라 설정 파일 두 개와 requirements.txt를 collectors 최상위로 복원했다. 설정값은 유지하고 설정 로더·설치 스크립트·Git 제외 규칙·README도 이전 경로로 돌렸다. src, monitor 등 다른 폴더 정리는 유지한다.

## collectors 폴더 구조 단순화 (2026-10-31)

최상위는 `src`, `scripts`, `monitor`, `data`, `runtime`, `tests`와 기존 `.venv`로 정리했다. 수집 코드는 `src/reels`, 출력 코드는 `src/exporters`로 묶었다. Python import 경로는 `src.reels`, `src.exporters`이며 분석기의 국가 판정 공유 import도 새 경로로 변경했다.

운영 파일은 `runtime/browser_profile`(로그인), `runtime/android-ui`(Appium 상태), `runtime/monitor`(모니터 실행 기록), `runtime/diagnostics`(검증·진단)로 모았다. Android npm 의존성은 실행 도구와 함께 `scripts/android/tools/android-ui`에 둔다. 기존 `collector.ps1`, `refresh.ps1`, `monitor/start.ps1` 및 설치·진단 도구의 실행 경로는 유지한다. `.venv`와 `data/collector`, `data/refresh`의 위치는 변경하지 않았다.

최상위 과거 `.collector`의 43개 파일은 `archives/collector-legacy-state-*.zip`에 압축하고 SHA-256을 검증한 뒤 정리했다. 실제 데이터 폴더 안의 `.collector` 이력과 큐는 그대로 유지한다. 테스트가 최상위 `unused`에 파일을 만들던 두 fixture는 자동 정리되는 임시 디렉터리로 변경했다. 이미 생성된 테스트 자료는 `runtime/diagnostics/test-artifacts`에 보관했다.

검증 결과:

- 전체 Python 테스트 766개 통과. fixture 정리 후 관련 70개 재검증 통과.
- 데이터·로그인·Android 설정 등 14,582개 파일 해시 일치(압축 보관 43개 포함).
- 이동된 Appium 3.9.0과 UiAutomator2 8.7.2 인식 확인. 이전 절대 경로를 담은 재생성 가능 캐시는 새 위치에서 다시 만들었다.
- 임시 로컬 HTTP 서버로 모니터 화면·health·분석 189건·재수집 데이터셋 1개 응답 확인 후 서버 종료.
- 기존 collector 실행기의 help, PowerShell 구문과 문서 링크 검사 통과.
- 실제 Instagram 수집·분석 요청·로그인 및 Google Sheets 배포는 실행하지 않았다.

## 데이터 하위 폴더명 단순화 (2026-10-31)

`collectors/data/collector/`와 `collectors/data/refresh/`로 폴더명을 정리했다. 실행 기본 경로, 통합 Excel 이름 선택, 모니터 데이터 탐색·분류와 문서를 함께 반영했다. 파일 4,719개의 SHA-256이 이동 전후 모두 일치하며 관련 테스트 59개가 통과했다. 통합 파일명은 각각 `collection_data.xlsx`, `refresh_data.xlsx`를 유지한다.

## 사용자 개별 Excel·JSON 복구 (2026-10-31)

최신 사용자 요청에 따라 사용자 전용 Excel 생성 중단 결정을 변경했다. `collector`와 `refresh`에 `users.xlsx`, `users.json`을 생성하고 수집·예약 수집의 자동 갱신에 다시 포함했다. 기존 긴 이름 대신 `users.xlsx`를 사용하며 통합 Excel의 `users` 시트도 유지한다. 신규 수집은 7,537행, 재수집은 59행이고 두 폴더 모두 CSV와 같은 12개 열이다. CSV 원본 해시는 변하지 않았으며 JSON 전체 값, Excel 행 수·헤더·사용자 ID·사용자명·팔로워 수 일치를 확인했다. 직접 생성 및 예약 수집 공개 반영 테스트를 통과했다.

## 신규 수집 통합 파일명 구분 (2026-10-31)

`collector`의 통합 파일과 내부 `.datasets` 통합 파일은 `collection_data.xlsx`로 변경했다. `refresh`의 `refresh_data.xlsx`는 유지한다. 파일명만 변경했으며 원본·변경 파일의 SHA-256이 일치한다. 이후 통합 파일 생성, Excel 잠금 대체 파일, 재수집 입력 보호와 통합 파일 탐색에도 폴더별 이름을 반영했다.

## 최종 데이터 파일명 정리 (2026-10-31)

- 재수집 입력 목록은 `input.xlsx`로 변경했다.
- 통합 Excel은 신규 수집·재수집 폴더 모두 `refresh_data.xlsx`로 변경했다.
- `recollection_results.xlsx`는 삭제하고 해당 이름으로 별도 결과를 생성하던 코드도 제거했다.
- `reels.xlsx`와 기존 데이터는 유지한다. 사용자가 결과 파일을 직접 입력으로 지정하는 이전 방식에서는 원본 보호를 위해 기존 `_updated.xlsx` 대체 파일 규칙을 사용한다. 일반 입력은 `input.xlsx`를 사용한다.
- 실제 Excel 4개는 내용 변경 없이 이름만 변경했고 SHA-256 일치를 확인했다. 삭제 파일 1개는 변경 전 검증한 로컬 압축본에 보관했다.
- 실행 기본값·모니터 명령 예시·상태 표시·README·테스트에 새 이름을 반영했다.
- 수집·재수집·통합 Excel·표시 형식 관련 회귀 테스트 421개 통과.

## 사용자 Excel 중복 제거와 통합 파일 열 순서 (2026-10-31)

사용자 요청에 따라 `collection_results_users.xlsx`의 독립 생성과 공개 복사를 중단하고 기존 파일을 정리했다. 사용자 정보는 `users.csv`와 `refresh_data.xlsx`의 `users` 시트에 유지한다. 기존 함수 이름은 호출 호환성을 위해 유지하되 통합 파일 경로를 반환한다.

통합 파일과 `reels.xlsx`의 열 집합은 원래 동일했으나 `hours_since_previous`가 통합 파일의 32번째 열에 있었다. 회차별 릴스 출력에는 같은 행 기반 투영을 사용해 두 파일의 32개 열 순서를 일치시키고, 경과 시간을 두 번째 열로 배치했다. 최초 수집의 경과 시간은 빈 값이다. 신규 수집은 실제 6,644행(원본 Excel에 추가 서식 빈 행 202개), 재수집은 76행으로 데이터 누락이 없다. 경과 시간의 유효 값 4,983개/57개, URL·수집 시각·조회수는 원본과 일치한다.


## Excel 파일 역할과 공개 열 정리 (2026-10-31)

- `reels.xlsx`는 신규 수집·재수집 폴더에서 모두 유지한다.
- 사용자 출력은 `collection_results_users.xlsx`, 통합 출력은 `refresh_data.xlsx`를 사용한다.
- 재수집 입력은 `input.xlsx`, 입력 원본 보호를 위한 별도 결과는 `recollection_results.xlsx`로 구분한다.
- 예약 수집의 공개 파일 갱신 시 신규 수집 폴더의 통합 Excel도 함께 갱신한다. 통합 파일은 해당 폴더의 CSV를 기준으로 생성한다.
- 공개 릴스 CSV·JSON·개별/통합 Excel에서 `korean_source`, `view_count_observed_at`, `view_count_source`, `android_mismatch` 및 해당 회차 접두어 열을 제외한다. 내부 이력·판정 근거·큐의 필드는 유지한다.
- 기존 Excel 열 삭제는 원본 OOXML의 값·서식을 유지한다. 날짜와 숫자를 재해석하지 않는다. 변경 전 출력은 `archives/public-workbooks-before-columns-*.zip`으로 검증해 보관했다.
- 통합 파일 검증: 신규 수집 릴스 6,644행·사용자 7,537행, 재수집 릴스 76행·사용자 59행. 개별/통합 Excel의 수집 시각도 일치한다.

## 작업 범위

용도가 같은 보조 스크립트를 묶고, 다른 Windows 사용자가 자신의 설치 위치와 데이터로 실행할 수 있게 정리한다. 수집 데이터, 로그인 프로필, 실제 `.env`, 재수집 설정, 진행 중인 큐와 기존 변경 사항은 보존한다.

**Google Sheets 공용 키 풀은 팀원들이 같은 시트를 공유하므로 변경하지 않는다.** `analyzers/pool/apps_script/Code.js`의 시트 ID, 인증, 키 공유 방식과 배포 설정을 유지한다. 각 팀원은 자신의 로컬 `.env`에 관리자가 전달한 URL·토큰과 고유 사용자 별칭을 설정한다.

## 폴더별 책임

| 경로 | 용도 |
|---|---|
| `collectors/src/reels/` | 웹 수집, Android 지표 보강, 예약·재수집 처리 |
| `collectors/src/exporters/` | CSV·JSON·Excel 출력 |
| `collectors/monitor/` | 로컬 모니터 서버와 화면 |
| `collectors/scripts/` | 수집 CLI 및 기존 출력 형식 호환 진입점 |
| `collectors/scripts/android/` | Android 시작·재시작·UI 서버 종료 |
| `collectors/scripts/setup/` | Python 가상환경 및 Android UI 의존성 설치 |
| `collectors/scripts/diagnostics/` | 읽기 전용 상태 기록, 데이터 증거 저장, 시각화 |
| `collectors/scripts/android/tools/android-ui/` | 프로젝트에 고정된 Appium 실행 의존성 |
| `collectors/data/collector/` | 신규 수집 데이터 |
| `collectors/data/refresh/` | 재수집 전용 입력·출력 |
| `analyzers/` | Gemini 분석·인사이트·영상 다운로드 |
| `analyzers/pool/` | 로컬 키 풀과 팀 공유 키 풀 |
| `docs/` | 개발·사용 문서 |
| `archives/` | SHA-256으로 검증한 과거 자료 압축본; Git 제외 |

주 실행 명령 `collector.ps1`, `refresh.ps1`, `monitor/start.ps1`과 Python 수집 CLI 경로는 유지한다. 보조 스크립트의 새 위치는 관련 코드·테스트·README에 함께 반영한다.

## 로컬 의존성 처리

| 항목 | 처리 기준 |
|---|---|
| 저장소 위치 | 스크립트 자신의 위치로 경로를 계산한다. 특정 드라이브·Windows 사용자명에 의존하지 않는다. |
| Android SDK·ADB | `ANDROID_HOME`, `ANDROID_SDK_ROOT`, `INSTAGRAM_ANDROID_ADB_PATH`, 실행 옵션 또는 PATH로 찾는다. |
| Android 기기·AVD | 연결된 기기를 재사용한다. 기기·AVD 선택 옵션을 유지하고, 임의의 개인 AVD 이름을 고정하지 않는다. |
| 브라우저 | 설치된 Edge/Chrome을 찾거나 `INSTAGRAM_BROWSER_EXECUTABLE`로 지정한다. |
| Java | `JAVA_HOME`, `ANDROID_STUDIO_JAVA_HOME` 또는 Android Studio의 JBR을 사용한다. |
| 진단 캡처 | 입력 데이터 경로를 받는다. 특정 날짜의 패치 기록이나 과거 실험 수치를 필수 입력으로 사용하지 않는다. |
| 단독 시각화 | CSV 경로를 지정할 수 있게 한다. 특정 개인 데이터 파일 두 개를 필수로 요구하지 않는다. |
| Python·Node 의존성 | 각자의 PC에서 requirements와 npm lock 파일로 설치한다. `.venv`·`node_modules`를 다른 PC에 복사하지 않는다. |
| localhost 주소 | 모니터·Appium의 로컬 통신 기본값이므로 유지한다. |
| Google Sheets | 팀 공용 설정을 유지한다. 이번 변경 대상에서 제외한다. |

지원 범위는 Windows PowerShell이다. macOS/Linux 전체 실행·복구 지원으로 확대하는 작업은 포함하지 않는다. 각자의 로그인, Instagram 앱 설치, API 키와 공유 풀 권한은 별도로 준비한다.

## 검증

- 변경 전 Android 실행·복구·UI 관련 테스트 21개 통과.
- 변경 후 Python 테스트 56개 통과: Android 실행·복구·UI·경로·진단 24개, 데이터 폴더·수집 모드·모니터·국가 판정·분석 데이터 연결 32개.
- 새 진단 테스트는 저장소 바깥 작업 디렉터리와 공백이 있는 사용자 데이터 경로에서 실행했다. 과거 패치 파일·로그 없이 캡처, 입력 누락 시 오류, 사용자 CSV로 시각화 생성을 확인했다. Android 실행 테스트도 다른 경로로 복사한 스크립트와 가상 SDK·AVD로 검증했다.
- PowerShell 보조 스크립트 6개의 구문 검사와 Node 산점도 테스트 통과.
- 보존 대상으로 기록한 데이터·설정 파일 4,647개는 SHA-256이 모두 일치했다.
- 실행 중인 모니터에 활성 수집 작업이 없음을 확인한 뒤 재시작했다. `/api/health` 정상 응답, `/api/analysis` 189건을 확인했다.
- `README.md`, `collectors/README.md`, `collectors/monitor/README.md`에 새 경로와 사용법을 반영했다.
- 테스트 로그는 로컬 `collectors/runtime/diagnostics/portability-after.log`, `portability-monitor-tests.log`에 보관한다. 샌드박스 안에서 멈춘 추가 검증은 해당 테스트 프로세스만 종료하고 제한 시간을 지정해 재실행했으며 정상 통과했다.
- 실제 Instagram 수집, Google Sheets 호출·배포와 공유 키 동기화는 이 정리 작업에서 실행하지 않는다.

## 실행 예시

저장소 루트에서 실행한다. 설치된 Python·Node·SDK와 각자의 로그인은 사전에 준비한다.

```powershell
# 수집기 환경 설치·복구
.\collectors\scripts\setup\repair_venv.ps1

# hybrid 모드에 필요한 Android UI 의존성 설치
.\collectors\scripts\setup\setup-android-ui.ps1

# Android 자동 선택·준비; 여러 기기/AVD가 있으면 해당 선택 옵션 지정
.\collectors\scripts\android\start-android.ps1

# 자신의 데이터셋 현재 상태 저장
.\collectors\.venv\Scripts\python.exe -B .\collectors\scripts\diagnostics\capture_view_count_evaluation.py --data-dir .\collectors\data\collector\.datasets\fashion

# 자신의 CSV로 시각화 생성
.\collectors\.venv\Scripts\python.exe -B .\collectors\scripts\diagnostics\visualize_duration_vs_play_count.py --csv .\collectors\data\collector\reels.csv --output .\outputs\duration-vs-play-count.html
```

다른 사용자에게 배포할 때는 이동된 스크립트 하위 폴더와 수정된 호출 코드를 함께 포함한다. 실행 중인 구버전 모니터는 새 코드의 경로를 사용하도록 재시작한다. 보조 스크립트를 직접 호출하는 개인 예약 작업도 새 경로로 갱신한다.

## 데이터 구조 변경 (2026-10-30)

신규 수집과 재수집 폴더를 `collectors/data/collector/`, `collectors/data/refresh/`로 이동했다. 신규 수집 공개 파일의 `fashion_` 접두어를 제거해 `reels.*`, `users.*`, `collector_status.json`, `reel_analysis.json`, `reel_insights.*`로 저장한다. 내부 `.datasets/fashion`의 이력·큐는 보존한다. Beauty는 별도 파일명으로 유지해 덮어쓰기를 방지한다. 실행 기본값·모니터·분석·진단·README·Git 제외 규칙을 함께 변경했다. 이동 전후 4,719개 파일의 SHA-256이 모두 일치했고, 관련 회귀 테스트 124개가 통과했다.
