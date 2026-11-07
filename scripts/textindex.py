"""検索索引（SQLite FTS5）の共通処理。

日本語は単語の区切りがないので、文字を2文字ずつ重ねて区切る「bi-gram」方式で
索引を作る（例：「福岡市内」→「福岡 岡市 市内」）。形態素解析が要らず、
2文字の語（「福岡」「花火」など）も検索できる。英数字は単語ごとに扱う。
"""

import re
import sqlite3
from pathlib import Path

# 漢字・ひらがな・カタカナ・全角記号などの「続き文字」
CJK = r"぀-ヿ㐀-䶿一-鿿豈-﫿ｦ-ﾟ々〆ヵヶー"
TOKEN = re.compile(rf"[{CJK}]+|[0-9A-Za-zÀ-ɏ]+")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    id INTEGER PRIMARY KEY,
    url TEXT UNIQUE,
    host TEXT,
    title TEXT,
    body TEXT,
    fetched_at TEXT
);
CREATE TABLE IF NOT EXISTS hosts (
    host TEXT PRIMARY KEY,
    status TEXT,            -- ok / robots_denied / noindex / error
    googlebot_blocked INTEGER,  -- robots.txt で Googlebot だけ拒否しているか
    checked_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS pages_fts USING fts5(
    title, body, content='', tokenize='unicode61'
);
"""


def bigrams(text: str) -> str:
    out = []
    for m in TOKEN.finditer(text.lower()):
        t = m.group()
        if re.match(rf"[{CJK}]", t):
            if len(t) == 1:
                out.append(t)
            else:
                out.extend(t[i:i + 2] for i in range(len(t) - 1))
        else:
            out.append(t)
    return " ".join(out)


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(path)
    con.executescript(SCHEMA)
    return con


def add_page(con, url, host, title, body, fetched_at) -> None:
    cur = con.execute(
        "INSERT OR IGNORE INTO pages(url, host, title, body, fetched_at) VALUES (?,?,?,?,?)",
        (url, host, title, body, fetched_at))
    if cur.rowcount:
        con.execute("INSERT INTO pages_fts(rowid, title, body) VALUES (?,?,?)",
                    (cur.lastrowid, bigrams(title), bigrams(body)))


def to_query(q: str) -> str:
    """検索語を FTS5 の検索式にする。空白区切りの語はすべて含む（AND）。"""
    parts = []
    for word in q.split():
        b = bigrams(word)
        if b:
            parts.append('"' + b.replace('"', "") + '"')  # 連続した並びで一致
    return " AND ".join(parts)


def snippet(body: str, q: str, width: int = 70) -> str:
    for word in q.split():
        i = body.lower().find(word.lower())
        if i >= 0:
            s = max(0, i - width // 2)
            return ("…" if s else "") + body[s:s + width] + "…"
    return body[:width] + "…"


def search(con, q: str, limit: int = 20, offset: int = 0) -> list[dict]:
    fq = to_query(q)
    if not fq:
        return []
    rows = con.execute("""
        SELECT p.url, p.host, p.title, p.body, h.googlebot_blocked
        FROM pages_fts f
        JOIN pages p ON p.id = f.rowid
        LEFT JOIN hosts h ON h.host = p.host
        WHERE pages_fts MATCH ?
        ORDER BY bm25(pages_fts, 5.0, 1.0)
        LIMIT ? OFFSET ?
    """, (fq, limit, offset)).fetchall()
    return [{"url": u, "host": h, "title": t or u, "snippet": snippet(b, q),
             "googlebot_blocked": bool(g)} for u, h, t, b, g in rows]
