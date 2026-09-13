# Instagram crawling

Instagram 릴스 수집기와 영상 분석기를 용도별로 관리합니다.

```text
Instagram-crawling/
├── collectors/          # 수집 도구
│   ├── web/             # 브라우저 수집
│   ├── android/         # Android 앱 수집
│   └── unified/         # 웹 + Android 통합 수집
├── analyzers/           # 영상 분석 도구
│   ├── gemini/          # Gemini API 분석
│   └── local_llm/       # 로컬 LLM 분석
├── docs/                # 설계·구현 계획·리뷰
├── data_web/            # 기존 루트 데이터
└── temp/                # 임시 작업 및 이전 구현 보관
```

## 사용 안내

| 도구 | 설치 및 실행 안내 |
|---|---|
| 웹 수집기 | [collectors/web](collectors/web/README.md) |
| Android 수집기 | [collectors/android](collectors/android/README.md) |
| 통합 수집기 | [collectors/unified](collectors/unified/README.md) |
| Gemini 분석기 | [analyzers/gemini](analyzers/gemini/README.md) |
| 로컬 LLM 분석기 | [analyzers/local_llm](analyzers/local_llm/README.md) |

통합 수집기 실행 예시:

```powershell
cd C:\Instagram-crawling\collectors\unified
.\collector.ps1 --max-items 50
```

## 관리 기준

- 각 도구의 가상환경, 로그인 프로필, 데이터와 결과물은 해당 도구 폴더에서 관리합니다.
- 수집기 내부 `collectors/`, `android_collector/`, `exporters/`, `scripts/`는 Python 모듈과 실행 진입점입니다.
- `examples/`와 `data_web_test/`는 예제·검증 자료입니다.
- `temp/`에는 이전 `default_version`과 임시 작업 자료가 보존되어 있습니다.
- 과거 설계·계획 문서의 경로는 작성 당시 기준이며, 현재 실행 경로는 위 표를 따릅니다.
- 이동한 가상환경은 `.venv\Scripts\python.exe`로 직접 실행하고, 패키지 설치에는 `-m pip`를 사용합니다. 기존 활성화 스크립트나 pip 실행 파일에 이전 경로가 남아 있을 수 있습니다.
