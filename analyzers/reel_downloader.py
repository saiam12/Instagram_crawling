"""
Instagram Reel Downloader (CLI)

터미널에 Instagram Reel URL을 입력하면 downloads/ 폴더에 mp4 파일로 저장합니다.
(Gemini 분석 없이 다운로드만 수행)

사용법:
    python reel_downloader.py
"""

import argparse
import os
import yt_dlp
from gemini.input_sources import normalize_reel_url


DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


def download_reel(reel_url: str, cookies_browser: str | None = None) -> str:
    """Instagram Reel을 downloads/ 폴더에 mp4로 저장하고 저장된 파일 경로를 반환한다."""
    os.makedirs(DOWNLOAD_DIR, exist_ok=True)

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "format": "best[ext=mp4]/best",
        "outtmpl": os.path.join(DOWNLOAD_DIR, "%(id)s.%(ext)s"),
    }

    if cookies_browser:
        ydl_opts["cookiesfrombrowser"] = (cookies_browser, None, None, None)

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(reel_url, download=True)
        filepath = ydl.prepare_filename(info)

    print(f"  - shortcode : {info.get('id')}")
    print(f"  - duration  : {info.get('duration')}초")
    print(f"  - 저장 위치 : {filepath}")

    return filepath


def main():
    parser = argparse.ArgumentParser(description="Instagram Reel Downloader")
    parser.add_argument(
        "--cookies-from-browser",
        choices=("brave", "chrome", "chromium", "edge", "firefox", "opera", "vivaldi", "whale"),
        help="선택한 브라우저의 Instagram 로그인 쿠키를 사용합니다.",
    )
    args = parser.parse_args()

    print("Instagram Reel Downloader")
    print("Reel URL을 입력하세요. 종료하려면 'quit' 또는 'exit' 입력.\n")

    while True:
        try:
            reel_url = input("Reel URL > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n종료합니다.")
            break

        if not reel_url:
            continue
        if reel_url.lower() in ("quit", "exit", "q"):
            print("종료합니다.")
            break
        reel_url = normalize_reel_url(reel_url)
        if not reel_url:
            print("[경고] Instagram URL이 아닌 것 같습니다. 다시 확인해주세요.\n")
            continue

        try:
            download_reel(reel_url, args.cookies_from_browser)
            print("[완료]\n")
        except yt_dlp.utils.DownloadError as e:
            if not args.cookies_from_browser and "isn't available to everyone" in str(e):
                print("[안내] 로그인 계정으로 다시 실행하세요: python reel_downloader.py --cookies-from-browser edge")
            print(f"[오류] 영상을 가져오지 못했습니다: {e}")
            print("      비공개 계정이거나 삭제된 게시물일 수 있습니다.\n")
        except Exception as e:
            print(f"[오류] 예상치 못한 문제가 발생했습니다: {e}\n")


if __name__ == "__main__":
    main()
