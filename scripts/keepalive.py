"""検索画面（Streamlit）が休止していたら起こす。

Streamlit Community Cloud の無料公開は、しばらくアクセスがないと休止する。
実際のブラウザで画面を開き、「起動する」ボタンがあれば押して、起動を待つ。
"""

import sys

from playwright.sync_api import sync_playwright

URL = "https://jp-small-web00.streamlit.app/"
WAKE = "Yes, get this app back up"


def find_wake_button(page):
    for frame in page.frames:
        btn = frame.get_by_role("button", name=WAKE)
        if btn.count():
            return btn.first
    return None


def is_running(page) -> bool:
    return any(f.get_by_text("ページを収録").count() for f in page.frames)


def main() -> int:
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        page.goto(URL, wait_until="domcontentloaded", timeout=120_000)
        page.wait_for_timeout(15_000)

        btn = find_wake_button(page)
        if btn:
            print("休止中だったので起動します")
            btn.click()
        # 起動（または通常の読み込み）を最大5分待つ
        for _ in range(30):
            if is_running(page):
                print("検索画面は動いています")
                browser.close()
                return 0
            page.wait_for_timeout(10_000)
        print("起動を確認できませんでした")
        page.screenshot(path="keepalive.png", full_page=True)
        browser.close()
        return 1


if __name__ == "__main__":
    sys.exit(main())
