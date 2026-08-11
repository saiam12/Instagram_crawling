# Instagram Reels 웹 수집기

로그인된 Instagram 웹 화면을 순회해 공개 릴스와 작성자 팔로워 수를 수집하고, CSV와 XLSX로 저장하는 Windows용 도구입니다.

- Meta Graph API, 액세스 토큰, API 키를 사용하지 않습니다.
- Instagram 비밀번호를 파일에 저장하지 않습니다.
- 최초 로그인 후에는 로컬 브라우저 프로필을 재사용합니다.

## 실행 전 준비

1. PowerShell을 열고 프로젝트 폴더로 이동합니다.

   ```powershell
   cd C:\instagram_data_set
   ```

2. 처음 한 번은 `-Background` 없이 실행합니다. 열린 Edge 또는 Chrome 창에서 Instagram에 직접 로그인하고, 릴스 화면이 보이면 PowerShell로 돌아와 Enter를 누릅니다.

3. 로그인 후에는 브라우저 프로필이 `.instagram_browser_profile`에 저장됩니다. 이 폴더에는 로그인 세션이 있으므로 절대 공유하거나 GitHub에 올리면 안 됩니다.

## 프로젝트 구조

```text
instagram_data_set/
├── collectors/  # Instagram 웹 화면 순회와 팔로워 수 수집
├── exporters/   # CSV를 XLSX로 정리하고 재수집 URL을 읽는 도구
├── scripts/     # PowerShell 실행 명령
├── tests/       # 개발용 자동 테스트 (GitHub에는 올리지 않음)
├── data_web/    # 실제 수집 결과 (GitHub에는 올리지 않음)
└── README.md
```

## 수집 결과 파일

모든 수집 결과는 `data_web` 폴더에 저장됩니다. 이 폴더는 `.gitignore`로 제외되어 GitHub에 올라가지 않습니다.

| 파일 | 용도 |
|---|---|
| `reels_web.csv` | 릴스별 기본 정보와 반복 수집 시계열 데이터 |
| `users.csv` | 릴스 작성자별 최신 팔로워 수와 조회 상태 |
| `follower_lookups.csv` | 팔로워 수 조회 이력과 오류 기록 |
| `instagram_data.xlsx` | 위 CSV를 시트별로 모아 보기 좋게 정리한 Excel 파일 |

### `reels_web.csv` 열

| 열 | 설명 |
|---|---|
| `url` | 표준화된 릴스 URL |
| `collected_at` | 최초 수집 시각(UTC 원본, XLSX에서는 한국 시간으로 표시) |
| `user_id` | 릴스 응답에서 얻은 Instagram 사용자 ID |
| `username` | 작성자 사용자명 |
| `title` | 해시태그를 제외한 캡션 앞 300자. 길면 `...` 처리 |
| `hashtags` | 전체 캡션에서 추출한 해시태그 |
| `audio_name` | 사용 음원명 또는 원본 오디오 정보 |
| `location_name` | 화면 또는 릴스 정보에 위치가 있는 경우의 위치명 |
| `ad` | 명시적 광고·협찬 표시가 감지되면 `true`, 아니면 `false` |
| `uploaded_at` | 릴스 업로드 시각 |
| `days_since_upload` | 수집 시점 기준 업로드 경과 시간. XLSX에서 `+20hours`, `+12day`처럼 표시 |
| `like_count` | 좋아요 수 |
| `comment_count` | 댓글 수 |
| `repost_count` | 리포스트 수 |
| `follower_count` | 해당 작성자의 팔로워 수 |
| `reaction_rate` | `like_count / follower_count`. XLSX에서는 백분율 표시 |
| `follower_count_collected_at` | 팔로워 수를 조회한 시각 |
| `follower_lookup_status` | 팔로워 조회 상태 (`success`, `web_error` 등) |

같은 릴스를 다시 수집하면 행을 추가하지 않습니다. 대신 `2nd collect_*`, `3rd collect_*`처럼 수집 차수별 열이 추가됩니다. 재수집 열에는 좋아요·댓글·리포스트·팔로워 수와 반응률만 저장됩니다.

XLSX에서는 재수집 수치가 이전 실제 수치와 비교되어 `21(+4)`처럼 표시됩니다. 재수집 시각은 최초 수집 기준 경과 시간과 함께 `2026-08-10 14:00:00 (+2Hour)`처럼 표시됩니다.

### `users.csv` 열

| 열 | 설명 |
|---|---|
| `user_id` | Instagram 사용자 ID |
| `username` | 사용자명 |
| `first_seen_at`, `last_seen_at` | 해당 사용자를 처음·마지막으로 릴스에서 확인한 시각 |
| `follower_count` | 최신 팔로워 수 |
| `follower_count_collected_at` | 최신 팔로워 수 수집 시각 |
| `lookup_status` | 최신 팔로워 조회 상태 |
| `last_lookup_at` | 마지막 조회 시각 |
| `last_error` | 마지막 조회 오류 메시지 |

### `follower_lookups.csv` 열

| 열 | 설명 |
|---|---|
| `collected_at` | 팔로워 수 조회 시각 |
| `user_id`, `username` | 조회한 사용자 |
| `follower_count` | 조회 성공 시 팔로워 수 |
| `error` | 실패 시 오류 메시지 |

## 명령어

### 1. 릴스 신규 수집

브라우저를 보면서 릴스 50개를 수집합니다.

```powershell
.\scripts\start_reels_web.ps1 -MaxItems 50 -IntervalSeconds 2 -FollowerIntervalSeconds 8
```

| 옵션 | 기본값 | 설명 |
|---|---:|---|
| `-MaxItems` | `50` | 수집할 최대 릴스 수. `0`이면 릴스 피드를 사용자가 멈출 때까지 계속 수집 |
| `-IntervalSeconds` | `5` | 릴스 정보가 누락되거나 수집에 실패한 뒤 다음 릴스로 넘어가기 전 대기 시간 |
| `-FollowerIntervalSeconds` | `8` | 팔로워 수가 누락·실패한 뒤 다음 계정 조회 전 대기 시간 |
| `-FollowerCacheHours` | `1` | 이 시간 안에 조회한 사용자는 팔로워 수를 다시 조회하지 않음 |
| `-Manual` | 없음 | 자동 이동 대신 Enter를 누를 때마다 현재 릴스 수집 |
| `-Background` | 없음 | 로그인 세션이 저장된 뒤 브라우저 창을 보이지 않게 실행 |
| `-HashtagQuery` | 없음 | 해시태그 OR 조건 지정 |
| `-StartUrl` | 릴스 피드 | 시작할 Instagram 릴스 URL |
| `-DataDir` | `data_web` | 결과를 저장할 폴더 |

릴스의 URL·작성자·업로드 시각·좋아요·댓글·리포스트가 모두 수집되면 다음 릴스는 0.5초 뒤에 진행됩니다. 팔로워 수를 성공적으로 가져온 경우도 다음 계정 조회까지 0.5초만 대기합니다.

같은 URL이 알고리즘에 다시 나타나도 가장 최근의 최초·재수집 시각으로부터 1시간이 지나지 않았다면 저장하지 않고 다음 릴스로 넘어갑니다. 이 경우 재수집 열도 추가되지 않으며 `-MaxItems`의 수집 개수에도 포함되지 않습니다. 정확히 1시간이 지난 시점부터는 정상 재수집됩니다.

### 멈출 때까지 연속 수집

`-MaxItems 0`은 일반 릴스 피드에서만 사용할 수 있으며, 해시태그 수집에는 사용할 수 없습니다. 첫 `Ctrl+C`는 새 릴스 수집만 멈추고, 이미 대기열에 들어간 팔로워 수 조회가 끝난 뒤 CSV와 XLSX를 저장합니다. 강제 종료가 필요할 때만 두 번째 `Ctrl+C`를 누르세요.

```powershell
.\scripts\start_reels_web.ps1 -MaxItems 0 -IntervalSeconds 2 -FollowerIntervalSeconds 8 -Background
```

### 2. 백그라운드 릴스 수집

처음 로그인한 뒤에는 창을 띄우지 않고 실행할 수 있습니다.

```powershell
.\scripts\start_reels_web.ps1 -MaxItems 50 -IntervalSeconds 2 -FollowerIntervalSeconds 8 -Background
```

로그인 세션이 만료되었거나 `-Background` 실행이 실패하면, `-Background` 없이 다시 실행해 로그인 상태를 갱신하세요.

### 3. 해시태그 OR 조건 수집

`맛집` 또는 `서울맛집`을 포함하는 해시태그가 하나라도 있는 릴스만 저장합니다. 부분 일치도 포함하므로 `#강남맛집`, `#서울맛집추천`도 조건에 맞습니다.

```powershell
.\scripts\start_reels_web.ps1 -HashtagQuery '"맛집" OR "서울맛집"' -MaxItems 50 -IntervalSeconds 2 -Background
```

### 4. 기존 릴스 재수집

`instagram_data.xlsx`의 `reels_web` 시트에 저장된 URL을 다시 순회합니다. 기본 정보는 중복 저장하지 않고 반응 수치의 변화를 새 수집 차수 열에 기록합니다.

```powershell
.\scripts\refresh_reels_xlsx.ps1 -IntervalSeconds 2 -FollowerIntervalSeconds 8 -Background
```

| 옵션 | 기본값 | 설명 |
|---|---:|---|
| `-IntervalSeconds` | `2` | 정보가 누락·실패한 릴스의 다음 진행 대기 시간 |
| `-FollowerIntervalSeconds` | `8` | 팔로워 수 누락·실패 시 다음 사용자 조회 대기 시간 |
| `-FollowerCacheHours` | `1` | 팔로워 수 캐시 유지 시간 |
| `-Manual` | 없음 | Enter를 누를 때마다 다음 URL 재수집 |
| `-Background` | 없음 | 브라우저 창 없이 실행 |
| `-DataDir` | `data_web` | 결과 폴더 |

실행 전에는 `instagram_data.xlsx`를 Excel에서 닫아야 합니다.

### 5. 팔로워 수만 다시 수집

릴스는 수집하지 않고 저장된 사용자 목록의 팔로워 수만 조회합니다.

```powershell
.\scripts\update_followers.ps1 -IntervalSeconds 8 -CacheHours 1
```

| 옵션 | 기본값 | 설명 |
|---|---:|---|
| `-IntervalSeconds` | `8` | 팔로워 수 누락·실패 시 다음 사용자 조회 대기 시간 |
| `-CacheHours` | `1` | 이 시간 안에 조회한 사용자는 건너뜀 |
| `-Force` | 없음 | 캐시와 관계없이 모든 사용자를 다시 조회 |
| `-DataDir` | `data_web` | 결과 폴더 |

모든 사용자를 다시 조회하려면 다음처럼 실행합니다.

```powershell
.\scripts\update_followers.ps1 -IntervalSeconds 8 -CacheHours 0 -Force
```

## Excel 파일이 열려 있을 때

CSV는 먼저 저장됩니다. 다만 `instagram_data.xlsx`가 Excel에서 열려 있으면 XLSX 갱신이 실패하거나 `instagram_data_updated.xlsx`로 별도 저장될 수 있습니다. Excel을 닫고 같은 명령을 다시 실행하면 최신 CSV 내용으로 XLSX를 다시 만들 수 있습니다.

## GitHub에 올리면 안 되는 파일

`.gitignore`는 아래 항목을 제외하도록 설정되어 있습니다.

- 실제 수집 결과와 백업: `data/`, `data_web/`, `data_new_test/`, `backups/`
- Instagram 로그인 세션: `.instagram_browser_profile/`
- 개인 환경파일: `.env`, `.env.*`
- 개인 테스트 대상 목록: `targets_new_test.csv`
- 테스트·캐시·검사 산출물: `tests/`, `__pycache__/`, `.pytest_cache/`, `.diagnostics/`, `.xlsx_verify*/`
