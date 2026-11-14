"""Common Crawl から「小さなサイト」の候補を抜き出す（初期値は日本語、--langs で変更可）。

Common Crawl はリンクが集まりにくい（目立たない）サイトほど少ないページしか
収集しない。そこで「収集ページ数が少なく、半分以上が日本語のホスト」を
Google で見つけにくい小規模サイトの候補とみなす。

1クロールの索引は約300ファイル。実行するたびに数ファイルずつ処理し、
data/candidates/<クロールID>/part-XXXXX.csv に保存する。
進み具合は data/state.json に記録し、新しいクロールが出たら自動で切り替える。
"""

import argparse
import csv
import gzip
import json
import time
import urllib.request
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
STATE = DATA / "state.json"
EXCLUDE = ROOT / "config" / "exclude_domains.txt"
BASE = "https://data.commoncrawl.org/"
FIELDS = ["host", "registered_domain", "pages", "sample_url", "language"]


def fetch(url: str, tries: int = 5) -> bytes:
    """混雑で接続が切れることがあるので、待ちながら再試行する。"""
    for attempt in range(tries):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read()
        except Exception as e:
            if attempt == tries - 1:
                raise
            wait = 30 * (attempt + 1)
            print(f"{url} の取得に失敗（{type(e).__name__}）。{wait}秒後に再試行します")
            time.sleep(wait)


def latest_crawl() -> str:
    return json.loads(fetch("https://index.commoncrawl.org/collinfo.json"))[0]["id"]


def index_files(crawl: str) -> list[str]:
    raw = fetch(f"{BASE}crawl-data/{crawl}/cc-index-table.paths.gz")
    lines = gzip.decompress(raw).decode().split()
    return [BASE + p for p in lines if "subset=warc" in p]


def load_excludes() -> list[str]:
    if not EXCLUDE.exists():
        return []
    return [l.strip() for l in EXCLUDE.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def lang_condition(langs: list[str]) -> str:
    """主要言語（content_languages の先頭）が指定言語のどれかであるか。"""
    if langs == ["all"]:
        return "TRUE"
    return "(" + " OR ".join(f"content_languages LIKE '{l}%'" for l in langs) + ")"


def extract(con, url: str, max_pages: int, langs: list[str]) -> list[tuple]:
    # ホストの「全ページ数」で規模を判定し、指定言語のページが半分以上のホストだけ残す
    # （別言語の大きなサイトに、指定言語と判定されたページが混じるのを除くため）
    cond = lang_condition(langs)
    return con.execute(f"""
        SELECT url_host_name,
               any_value(url_host_registered_domain),
               count(*) AS pages,
               arg_min(url, length(url)) FILTER (WHERE {cond}),
               mode(split_part(content_languages, ',', 1))
        FROM read_parquet('{url}')
        WHERE fetch_status = 200
          AND content_mime_detected = 'text/html'
          AND url_host_registered_domain NOT IN (SELECT d FROM ex)
        GROUP BY 1
        HAVING count(*) <= {max_pages}
           AND count_if({cond}) * 2 >= count(*)
        ORDER BY 1
    """).fetchall()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--files", type=int, default=20, help="1回で処理する索引ファイル数")
    ap.add_argument("--max-pages", type=int, default=5, help="この数以下のホストを小規模とみなす")
    ap.add_argument("--langs", default="jpn",
                    help="対象言語（ISO 639-3、カンマ区切り）。例: jpn / jpn,eng,kor / all（全言語）")
    ap.add_argument("--crawl", default=None, help="例: CC-MAIN-2026-43（省略時は最新）")
    args = ap.parse_args()

    langs = [l.strip() for l in args.langs.split(",") if l.strip()]
    tag = "-".join(langs)
    state_file = STATE if tag == "jpn" else DATA / f"state-{tag}.json"
    try:
        crawl = args.crawl or latest_crawl()
        files = index_files(crawl)
    except Exception as e:
        print(f"Common Crawl に接続できないため、今回はスキップします（{type(e).__name__}）")
        return
    state = json.loads(state_file.read_text()) if state_file.exists() else {}
    if state.get("crawl") != crawl:
        state = {"crawl": crawl, "offset": 0, "hosts": 0}

    offset = state["offset"]
    if offset >= len(files):
        print(f"{crawl} は処理済みです。次のクロール公開を待ちます。")
        return

    con = duckdb.connect()
    con.execute("INSTALL httpfs; LOAD httpfs;")
    # Common Crawl は混雑時に接続を切ることがあるので、通信の再試行を多めにする
    con.execute("SET http_retries = 8; SET http_retry_wait_ms = 2000; "
                "SET http_retry_backoff = 2; SET http_timeout = 120000;")
    con.execute("CREATE TABLE ex(d VARCHAR)")
    excludes = load_excludes()
    if excludes:
        con.executemany("INSERT INTO ex VALUES (?)", [(d,) for d in excludes])

    outdir = DATA / "candidates" / crawl if tag == "jpn" else DATA / f"candidates-{tag}" / crawl
    outdir.mkdir(parents=True, exist_ok=True)
    end = min(offset + args.files, len(files))
    for i in range(offset, end):
        rows = None
        for attempt in range(4):
            try:
                rows = extract(con, files[i], args.max_pages, langs)
                break
            except duckdb.Error as e:
                wait = 60 * (attempt + 1)
                print(f"[{i + 1}] 取得に失敗（{str(e)[:80]}）。{wait}秒後に再試行します")
                time.sleep(wait)
        if rows is None:
            # ここまでの進み具合は保存済み。次回の実行でこのファイルから再開する
            print(f"[{i + 1}] 何度試しても取得できないため、今回はここで終了します")
            break
        with (outdir / f"part-{i:05d}.csv").open("w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(FIELDS)
            w.writerows(rows)
        state["offset"] = i + 1
        state["hosts"] += len(rows)
        state_file.write_text(json.dumps(state, ensure_ascii=False, indent=2))
        print(f"[{i + 1}/{len(files)}] {len(rows)} 件")

    print(f"{crawl}: 累計 {state['hosts']} 件（{state['offset']}/{len(files)} ファイル処理済み）")


if __name__ == "__main__":
    main()
