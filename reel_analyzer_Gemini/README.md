# Instagram Reels Analyzer

터미널에 Instagram Reel URL을 입력하면 영상을 분석해서 JSON으로 저장하는 CLI 도구입니다.

## 설치

```bash
pip install -r requirements.txt
```

## API 키 설정

1. `.env.example` 파일을 `.env` 로 복사합니다.
   ```bash
   cp .env.example .env
   ```
2. `.env` 파일을 열어 `GEMINI_API_KEY` 값을 본인의 키로 채웁니다.
   - 키 발급: https://aistudio.google.com/apikey
3. (선택) 다른 모델을 쓰고 싶다면 `GEMINI_MODEL` 값을 수정합니다. 기본값은 `gemini-3.8-flash` 입니다.

`.env` 파일은 절대 git에 커밋하지 마세요. (`.gitignore`에 추가 권장)

## 실행

```bash
python reel_analyzer.py
```

실행 후 프롬프트에 Reel URL을 입력하면 됩니다.

```
Reel URL > https://www.instagram.com/reels/DcSDbXNCtfd/
```

- 종료하려면 `quit`, `exit`, `q` 중 하나를 입력하세요.
- 계속해서 다른 URL을 입력하며 여러 개를 연속으로 분석할 수 있습니다.

## 동작 방식

1. `yt-dlp`로 Reel의 실제 mp4 URL을 추출해 메모리로 다운로드 (디스크에 원본 저장 안 함)
2. 영상 용량이 18MB 이하면 base64로 바로 Gemini에 전송 (inline)
3. 영상 용량이 18MB를 넘으면 Gemini Files API로 업로드 후, 처리(ACTIVE)가 끝날 때까지 대기했다가 분석 (큰 영상도 자동 처리됨 — 기존 코드처럼 에러로 중단되지 않음)
4. 분석 결과 JSON을 `output/{shortcode}_{시각}.json` 으로 저장
5. 터미널에 핵심 요약(요약문, Hook, 콘텐츠 유형)을 출력

## 주의사항

- 비공개 계정의 Reel은 다운로드할 수 없습니다. 필요하면 코드 내 `cookiesfrombrowser` 옵션 주석을 해제해서 로그인된 브라우저 쿠키를 사용하세요.
- Instagram의 페이지 구조 변경으로 `yt-dlp`가 간헐적으로 실패할 수 있습니다. 이 경우 `pip install -U yt-dlp` 로 최신 버전으로 업데이트하세요.
- Gemini API 사용량에는 비용이 발생할 수 있습니다. 가격 정책은 https://ai.google.dev/gemini-api/docs/pricing 참고하세요.
