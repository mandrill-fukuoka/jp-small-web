"""候補サイトの本文を取得して検索索引（data/index.db）に追加する。

- robots.txt を守る（取得できない・サーバーエラーのときは取得しない）
- noindex（meta タグ / X-Robots-Tag）のページは登録しない
- 1サイトにつき最大 --pages ページ（初期値5）。トップページと候補ページから
  同じサイト内のリンクをたどる。登録済みのサイトにも少しずつページを追加する（--deepen）
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
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

import hashlib

from textindex import add_page, is_japanese, open_db, remove_page

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
UA_NAME = "jp-small-web-bot"
UA = f"{UA_NAME}/0.1 (+https://github.com/mandrill-fukuoka/jp-small-web)"
TIMEOUT = 10
MAX_BYTES = 2_000_000
MAX_BODY = 2500  # 1ページに保存する本文の最大文字数
SKIP_EXT = re.compile(r"\.(jpe?g|png|gif|webp|svg|ico|pdf|zip|gz|mp[34]|mov|avi|docx?|xlsx?|pptx?|css|js|xml|rss|txt)$", re.I)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def load_candidates() -> dict[str, str]:
    """Common Crawl と SNS の候補CSVから {host: sample_url} を作る。"""
    cands = {}
    paths = sorted(DATA.glob("candidates/*/part-*.csv")) + sorted(DATA.glob("social/*.csv"))
    for path in paths:
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
    for m in soup.find_all("meta", attrs={"name": re.compile("^rating$", re.I)}):
        if re.search(r"adult|mature|RTA-5042", m.get("content", ""), re.I):
            return "noindex"  # 成人向けと自己申告しているページは登録しない
    for m in soup.find_all("meta", attrs={"name": re.compile("^(robots|" + UA_NAME + ")$", re.I)}):
        if is_noindex(m.get("content", "")):
            return "noindex"
    links = []
    host = urlsplit(r.url).hostname
    for a in soup.find_all("a", href=True):
        u = urljoin(r.url, a["href"]).split("#")[0]
        sp = urlsplit(u)
        if (sp.scheme in ("http", "https") and sp.hostname == host and not sp.query
                and not SKIP_EXT.search(sp.path) and "/feeds/" not in sp.path):
            links.append(u)
    for tag in soup(["script", "style", "noscript", "nav", "header", "footer", "form", "svg"]):
        tag.decompose()
    title = (soup.title.get_text(" ", strip=True) if soup.title else "")[:200]
    body = re.sub(r"\s+", " ", soup.get_text(" ", strip=True))[:MAX_BODY]
    return r.url, title, body, links


def same(u: str) -> str:
    """http/https・末尾の / の違いを無視して同じページか判定するためのキー。"""
    sp = urlsplit(u)
    return (sp.hostname or "") + (sp.path.rstrip("/") or "/")


def crawl_host(host: str, sample_url: str, max_pages: int, known: set[str]) -> dict:
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
        if not rp.can_fetch(UA_NAME, base + "/") and not rp.can_fetch(UA_NAME, sample_url):
            result["status"] = "robots_denied"
            return result
        delay = min(rp.crawl_delay(UA_NAME) or 1, 10)
        queue = [base + "/", sample_url]
        seen = set()
        bodies = set()
        not_ja = False
        need = max_pages - len(known)
        noindex = False
        tries = 0
        while queue and len(result["pages"]) < need and tries < max_pages * 2:
            u = queue.pop(0)
            key = same(u)
            if key in seen or not rp.can_fetch(UA_NAME, u):
                continue
            seen.add(key)
            if tries:
                time.sleep(delay)
            tries += 1
            page = fetch_page(session, u)
            if page == "noindex":
                noindex = True
                continue
            if not page:
                continue
            url, title, body, links = page
            seen.add(same(url))
            if not is_japanese(title + body):
                not_ja = True
                continue
            digest = hashlib.md5(body.encode()).hexdigest()
            if same(url) not in known and digest not in bodies:
                result["pages"].append((url, title, body))
            bodies.add(digest)
            known.add(same(url))
            queue.extend(l for l in links if same(l) not in seen)
        if result["pages"] or known:
            result["status"] = "ok"
        else:
            result["status"] = "noindex" if noindex else ("not_japanese" if not_ja else "error")
    except Exception as e:
        result["error"] = type(e).__name__
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--hosts", type=int, default=2000, help="1回で取得するサイト数")
    ap.add_argument("--deepen", type=int, default=1000,
                    help="ページ数が上限に届いていない登録済みサイトに、1回で追加取得するサイト数")
    ap.add_argument("--pages", type=int, default=5, help="1サイトあたりの最大ページ数")
    ap.add_argument("--workers", type=int, default=24)
    ap.add_argument("--db", default=str(DATA / "index.db"))
    args = ap.parse_args()

    con = open_db(Path(args.db))

    # 日本語でないページ・同じサイト内で中身が重複するページを索引から除く
    removed, seen_body = 0, set()
    for pid, host, title, body in con.execute(
            "SELECT id, host, title, body FROM pages ORDER BY id").fetchall():
        key = (host, hashlib.md5(body.encode()).hexdigest())
        if not is_japanese(title + body) or key in seen_body:
            remove_page(con, pid, title, body)
            removed += 1
        seen_body.add(key)
    con.execute("""UPDATE hosts SET status = 'not_japanese'
                   WHERE status = 'ok' AND host NOT IN (SELECT host FROM pages)""")
    con.commit()
    print(f"日本語でない・重複したページ {removed} 件を除きました")
    done = {h for (h,) in con.execute("SELECT host FROM hosts")}
    todo = [(h, u) for h, u in load_candidates().items() if h not in done]
    # 候補はドメイン順に並んでいるので、同じサービス（Blogger等）に
    # 同時アクセスが集中しないよう順番をばらす
    random.shuffle(todo)
    todo = [(h, u, set()) for h, u in todo[: args.hosts]]

    # 登録済みでページ数が上限未満のサイト（古い方から）
    known = {}
    for host, url in con.execute("SELECT host, url FROM pages"):
        known.setdefault(host, set()).add(same(url))
    shallow = con.execute("""
        SELECT h.host, min(p.url) FROM hosts h JOIN pages p ON p.host = h.host
        WHERE h.status = 'ok' AND coalesce(h.deepened, 0) < ?
        GROUP BY h.host HAVING count(*) < ? ORDER BY h.checked_at LIMIT ?
    """, (args.pages, args.pages, args.deepen)).fetchall()
    todo += [(h, u, known.get(h, set())) for h, u in shallow]
    print(f"新規 {len(todo) - len(shallow)} サイト＋ページ追加 {len(shallow)} サイトを取得します")

    counts = {}
    with ThreadPoolExecutor(args.workers) as ex:
        futures = [ex.submit(crawl_host, h, u, args.pages, set(k)) for h, u, k in todo]
        for n, fut in enumerate(as_completed(futures), 1):
            res = fut.result()
            ts = now()
            for url, title, body in res["pages"]:
                add_page(con, url, res["host"], title, body, ts)
            con.execute("""INSERT OR REPLACE INTO hosts(host, status, googlebot_blocked, checked_at, deepened)
                           VALUES (?,?,?,?,?)""",
                        (res["host"], res["status"], res["googlebot_blocked"], ts, args.pages))
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
    print(f"索引のサイズ {Path(args.db).stat().st_size / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
