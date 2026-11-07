"""候補サイトの本文を取得して検索索引（data/index.db）に追加する。

- robots.txt を守る（取得できない・サーバーエラーのときは取得しない）
- noindex（meta タグ / X-Robots-Tag）のページは登録しない
- 1サイトにつきトップページと候補ページの最大2ページだけ取得する
- robots.txt で Googlebot だけを拒否しているサイトは印を付ける
"""

import argparse
import csv
import random
import re
import time
import urllib.robotparser
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

import requests
from bs4 import BeautifulSoup

from textindex import add_page, open_db

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA_NAME = "jp-small-web-bot"
UA = f"{UA_NAME}/0.1 (+https://github.com/mandrill-fukuoka/jp-small-web)"
TIMEOUT = 10
MAX_BYTES = 2_000_000
MAX_BODY = 4000  # 1ページに保存する本文の最大文字数


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_candidates() -> dict[str, str]:
    """全CSVから {host: sample_url} を作る。"""
    cands = {}
    for path in sorted(DATA.glob("candidates/*/part-*.csv")):
        with path.open(encoding="utf-8") as f:
            for row in csv.DictReader(f):
                if row.get("sample_url"):
                    cands.setdefault(row["host"], row["sample_url"])
    return cands


def get(session, url):
    r = session.get(url, timeout=TIMEOUT, stream=True, allow_redirects=True)
    content = r.raw.read(MAX_BYTES, decode_content=True)
    r.close()
    return r, content


def is_noindex(value: str) -> bool:
    v = value.lower()
    return "noindex" in v or "none" in v.split(",")


def fetch_page(session, url):
    r, content = get(session, url)
    if r.status_code != 200 or "html" not in r.headers.get("Content-Type", "html"):
        return None
    for v in r.headers.get("X-Robots-Tag", "").split(";"):
        if is_noindex(v) and (":" not in v or UA_NAME in v.lower()):
            return "noindex"
    soup = BeautifulSoup(content, "lxml")
    for m in soup.find_all("meta", attrs={"name": re.compile("^(robots|" + UA_NAME + ")$", re.I)}):
        if is_noindex(m.get("content", "")):
            return "noindex"
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg"]):
        tag.decompose()
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:MAX_BODY]
    return r.url, title, body


def crawl_host(host: str, sample_url: str) -> dict:
    session = requests.Session()
    session.headers["User-Agent"] = UA
    scheme = urlsplit(sample_url).scheme or "https"
    base = f"{scheme}://{host}"
    result = {"host": host, "status": "error", "googlebot_blocked": 0, "pages": []}
    try:
        r, content = get(session, base + "/robots.txt")
        rp = urllib.robotparser.RobotFileParser()
        if r.status_code >= 500:
            return result
        rp.parse(content.decode("utf-8", "ignore").splitlines() if r.status_code == 200 else [])
        result["googlebot_blocked"] = int(not rp.can_fetch("Googlebot", base + "/")
                                          and rp.can_fetch(UA_NAME, base + "/"))
        urls = [u for u in dict.fromkeys([base + "/", sample_url]) if rp.can_fetch(UA_NAME, u)]
        if not urls:
            result["status"] = "robots_denied"
            return result
        delay = rp.crawl_delay(UA_NAME) or 1
        noindex = False
        for i, u in enumerate(urls):
            if i:
                time.sleep(min(delay, 10))
            page = fetch_page(session, u)
            if page == "noindex":
                noindex = True
            elif page:
                result["pages"].append(page)
        result["status"] = "ok" if result["pages"] else ("noindex" if noindex else "error")
    except Exception as e:
        result["error"] = type(e).__name__
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hosts", type=int, default=2000, help="1回で取得するサイト数")
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--db", default=str(DATA / "index.db"))
    args = ap.parse_args()

    con = open_db(Path(args.db))
    done = {h for (h,) in con.execute("SELECT host FROM hosts")}
    todo = [(h, u) for h, u in load_candidates().items() if h not in done]
    # 候補はドメイン順に並んでいるので、同じサービス（Blogger等）に
    # 同時アクセスが集中しないよう順番をばらす
    random.shuffle(todo)
    todo = todo[: args.hosts]
    print(f"未取得 {len(todo)} サイトを取得します")

    counts = {}
    with ThreadPoolExecutor(args.workers) as ex:
        futures = [ex.submit(crawl_host, h, u) for h, u in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            ts = now()
            for url, title, body in res["pages"]:
                add_page(con, url, res["host"], title, body, ts)
            con.execute("INSERT OR REPLACE INTO hosts VALUES (?,?,?,?)",
                        (res["host"], res["status"], res["googlebot_blocked"], ts))
            key = res["status"] + (f"({res['error']})" if res.get("error") else "")
            counts[key] = counts.get(key, 0) + 1
            if n % 200 == 0:
                con.commit()
                print(f"  {n}/{len(todo)} {counts}")
    con.commit()
    total = con.execute("SELECT count(*) FROM pages").fetchone()[0]
    gb = con.execute("SELECT count(*) FROM hosts WHERE googlebot_blocked=1").fetchone()[0]
    print(f"結果 {counts}／索引の総ページ数 {total}／Googlebotだけ拒否 {gb} サイト")
    con.execute("VACUUM")
    con.close()


if __name__ == "__main__":
    main()
