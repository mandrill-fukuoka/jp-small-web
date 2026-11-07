"""Bluesky・Mastodon の日本語の投稿に貼られたリンクから、新しいサイトの候補を集める。

Common Crawl に入る前の新しいサイトや、SNSで少人数にだけ共有されているサイトを拾う。
たくさんの人が共有しているリンク（大手・話題のサイト）は除き、
共有者が少ないホストだけを data/social/<日付>.csv に保存する。
"""

import argparse
import asyncio
import csv
import json
import re
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urlsplit

import websockets

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "social"
EXCLUDE = ROOT / "config" / "exclude_domains.txt"
JETSTREAM = ("wss://jetstream2.us-east.bsky.network/subscribe"
             "?wantedCollections=app.bsky.feed.post")
MASTODON = ["mstdn.jp", "fedibird.com", "mastodon-japan.net"]
# アダルト・アフィリエイト系のホストを除く（ホスト名の一部で判定）
NG_HOST = re.compile(r"(^|[.-])(ero|eros|adult|porn|xxx|hentai|av|sex|nsfw)([.-]|$)"
                     r"|erosite|fanza|duga|dlsite|avtuber|^(click|al|af|aff|affiliate|track)\.")
UA = "jp-small-web-bot/0.1 (+https://github.com/mandrill-fukuoka/jp-small-web)"
FIELDS = ["host", "registered_domain", "pages", "sample_url", "language"]
SECOND_LEVEL = {"co", "or", "ne", "ac", "ad", "ed", "go", "gr", "lg", "com", "net", "org"}


def registered_domain(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and parts[-1] == "jp" and parts[-2] in SECOND_LEVEL:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def load_excludes() -> list[str]:
    return [l.strip() for l in EXCLUDE.read_text().splitlines()
            if l.strip() and not l.startswith("#")]


def excluded(host: str, excludes: list[str]) -> bool:
    return any(host == d or host.endswith("." + d) for d in excludes)


# --- Bluesky -----------------------------------------------------------------

async def bluesky(minutes: float) -> list[tuple[str, str]]:
    """Jetstream（公開の投稿の流れ）を一定時間読み、日本語の投稿のリンクを集める。"""
    found, end = [], time.time() + minutes * 60
    while time.time() < end:
        try:
            async with websockets.connect(JETSTREAM, max_size=2 ** 22) as ws:
                while time.time() < end:
                    msg = await asyncio.wait_for(ws.recv(), 30)
                    d = json.loads(msg)
                    rec = (d.get("commit") or {}).get("record") or {}
                    if "ja" not in (rec.get("langs") or []):
                        continue
                    if (rec.get("labels") or {}).get("values"):  # 閲覧注意などのラベル付き
                        continue
                    urls = [f["uri"] for fa in rec.get("facets") or []
                            for f in fa.get("features", []) if f.get("uri")]
                    ext = (rec.get("embed") or {}).get("external") or {}
                    if ext.get("uri"):
                        urls.append(ext["uri"])
                    found += [(u, "bsky:" + d.get("did", "")) for u in urls]
        except Exception as e:  # 切断されたらつなぎ直す
            print("Bluesky 再接続:", type(e).__name__)
            await asyncio.sleep(5)
    return found


# --- Mastodon ----------------------------------------------------------------

class Links(HTMLParser):
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        cls = a.get("class") or ""
        if tag == "a" and a.get("href") and "mention" not in cls and "hashtag" not in cls:
            self.urls.append(a["href"])


def mastodon(instance: str, hours: int, max_pages: int) -> list[tuple[str, str]]:
    """サーバーのローカル公開タイムラインを、指定時間分さかのぼって読む。"""
    found, max_id = [], None
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    for _ in range(max_pages):
        url = f"https://{instance}/api/v1/timelines/public?local=true&limit=40"
        if max_id:
            url += f"&max_id={max_id}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as r:
                statuses = json.load(r)
        except Exception as e:
            print(f"{instance}: 取得できません（{type(e).__name__}）")
            break
        if not statuses:
            break
        for s in statuses:
            if s.get("language") not in (None, "ja"):
                continue
            if s.get("sensitive") or s["account"].get("bot"):  # 閲覧注意・自動投稿
                continue
            p = Links()
            p.feed(s.get("content") or "")
            urls = p.urls + ([s["card"]["url"]] if s.get("card") else [])
            found += [(u, f"{instance}:{s['account']['id']}") for u in urls]
        max_id = statuses[-1]["id"]
        if datetime.fromisoformat(statuses[-1]["created_at"].replace("Z", "+00:00")) < since:
            break
        time.sleep(1)
    return found


# -----------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=20, help="Bluesky を読む時間（分）")
    ap.add_argument("--hours", type=int, default=24, help="Mastodon をさかのぼる時間")
    ap.add_argument("--max-sharers", type=int, default=3,
                    help="これより多くの人が共有しているホストは「目立つサイト」として除く")
    args = ap.parse_args()

    links = asyncio.run(bluesky(args.minutes))
    print(f"Bluesky: リンク {len(links)} 件")
    for inst in dict.fromkeys(MASTODON):
        got = mastodon(inst, args.hours, max_pages=100)
        print(f"{inst}: リンク {len(got)} 件")
        links += got

    excludes = load_excludes()
    hosts: dict[str, dict] = {}
    for url, who in links:
        sp = urlsplit(url)
        host = (sp.hostname or "").lower()
        if sp.scheme not in ("http", "https") or not host or "." not in host:
            continue
        if excluded(host, excludes) or NG_HOST.search(host) or re.fullmatch(r"[\d.]+", host):
            continue
        h = hosts.setdefault(host, {"url": url, "who": set()})
        h["who"].add(who)

    rows = [(h, registered_domain(h), len(v["who"]), v["url"], "")
            for h, v in sorted(hosts.items()) if len(v["who"]) <= args.max_sharers]
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{datetime.now(timezone(timedelta(hours=9))):%Y-%m-%d}.csv"
    old = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            old = {r["host"]: r for r in csv.DictReader(f)}
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(FIELDS)
        for row in rows:
            old.pop(row[0], None)
            w.writerow(row)
        for r in old.values():
            w.writerow([r[k] for k in FIELDS])
    print(f"ホスト {len(hosts)} 件のうち、共有者 {args.max_sharers} 人以下の {len(rows)} 件を保存 → {path.name}")


if __name__ == "__main__":
    main()
