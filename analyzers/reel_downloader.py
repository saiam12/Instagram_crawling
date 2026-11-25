"""
Instagram Reel Downloader (CLI)

터미널에 Instagram Reel URL을 입력하면 downloads/ 폴더에 mp4 파일로 저장합니다.
(Gemini 분석 없이 다운로드만 수행)

사용법:
    python reel_downloader.py
"""

import argparse
from contextlib import contextmanager, nullcontext
from http.cookiejar import Cookie
import os
import yt_dlp
from input_sources import normalize_reel_url


DOWNLOAD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "downloads")


@contextmanager
def foreground_login():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError("Playwright를 설치하세요: python -m pip install -r requirements.txt") from None

    profile = os.path.join(DOWNLOAD_DIR, ".edge-login")
    with sync_playwright() as playwright:
        context = playwright.chromium.launch_persistent_context(profile, channel="msedge", headless=False)
        try:
            if not any(cookie["name"] == "sessionid" and cookie["value"]
                       for cookie in context.cookies("https://www.instagram.com/")):
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.instagram.com/accounts/login/", wait_until="domcontentloaded")
                input("Edge에서 Instagram에 로그인한 뒤 이 터미널에서 Enter를 누르세요. ")
            yield context
        finally:
            context.close()


def download_reel(reel_url: str, cookies_browser: str | None = None, *, browser_context=None) -> str:
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
        if browser_context is not None:
            cookies = browser_context.cookies("https://www.instagram.com/")
            if not any(cookie["name"] == "sessionid" and cookie["value"] for cookie in cookies):
                raise RuntimeError("Edge에서 Instagram 로그인을 완료한 뒤 다시 입력하세요.")
            for item in cookies:
                domain = item["domain"]
                path = item["path"]
                expires = item["expires"]
                ydl.cookiejar.set_cookie(Cookie(
                    version=0, name=item["name"], value=item["value"], port=None, port_specified=False,
                    domain=domain, domain_specified=domain.startswith("."), domain_initial_dot=domain.startswith("."),
                    path=path, path_specified=True, secure=item["secure"],
                    expires=int(expires) if expires > 0 else None, discard=expires <= 0,
                    comment=None, comment_url=None, rest={}, rfc2109=False,
                ))
        info = ydl.extract_info(reel_url, download=True)
        filepath = ydl.prepare_filename(info)

    print(f"  - shortcode : {info.get('id')}")
    print(f"  - duration  : {info.get('duration')}초")
    print(f"  - 저장 위치 : {filepath}")

    return filepath


def main():
    parser = argparse.ArgumentParser(description="Instagram Reel Downloader")
    browser = parser.add_mutually_exclusive_group()
    browser.add_argument("--foreground", action="store_true", help="Edge 창에서 직접 로그인하고 해당 세션으로 다운로드합니다.")
    browser.add_argument(
        "--cookies-from-browser",
        choices=("brave", "chrome", "chromium", "edge", "firefox", "opera", "vivaldi", "whale"),
        help="선택한 브라우저의 Instagram 로그인 쿠키를 사용합니다.",
    )
    args = parser.parse_args()

    try:
        with foreground_login() if args.foreground else nullcontext() as context:
            interactive_downloads(args.cookies_from_browser, context)
    except (EOFError, KeyboardInterrupt):
        print("\n종료합니다.")
    except Exception as error:
        parser.exit(1, f"[오류] {error}\n")


def interactive_downloads(cookies_browser, context):
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
            download_reel(reel_url, cookies_browser, browser_context=context)
            print("[완료]\n")
        except yt_dlp.utils.DownloadError as e:
            if not cookies_browser and context is None and "isn't available to everyone" in str(e):
                print("[안내] 로그인 계정으로 다시 실행하세요: python reel_downloader.py --foreground")
            print(f"[오류] 영상을 가져오지 못했습니다: {e}")
            print("      비공개 계정이거나 삭제된 게시물일 수 있습니다.\n")
        except Exception as e:
            print(f"[오류] 예상치 못한 문제가 발생했습니다: {e}\n")


if __name__ == "__main__":
    main()
