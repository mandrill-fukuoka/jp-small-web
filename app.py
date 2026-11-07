"""検索画面（Streamlit）。索引は GitHub のリリースから取得する。"""

import sqlite3
import sys
import tempfile
from pathlib import Path

import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).parent / "scripts"))
from textindex import search  # noqa: E402

DB_URL = "https://github.com/mandrill-fukuoka/jp-small-web/releases/download/index/index.db"
LOCAL_DB = Path(__file__).parent / "data" / "index.db"
PER_PAGE = 20

st.set_page_config(page_title="jp-small-web", page_icon="🔎")


@st.cache_resource(ttl=60 * 60 * 6)
def get_db() -> sqlite3.Connection:
    path = LOCAL_DB
    if not path.exists():
        path = Path(tempfile.gettempdir()) / "jp-small-web-index.db"
        with requests.get(DB_URL, stream=True, timeout=120) as r:
            r.raise_for_status()
            with path.open("wb") as f:
                for chunk in r.iter_content(1 << 20):
                    f.write(chunk)
    return sqlite3.connect(path, check_same_thread=False)


st.title("jp-small-web")
st.caption("大手検索エンジンでは見つけにくい、日本語の小さなサイトの検索")

con = get_db()
pages, sites = con.execute(
    "SELECT (SELECT count(*) FROM pages), (SELECT count(DISTINCT host) FROM pages)").fetchone()

q = st.text_input("検索", placeholder="例：福岡 畳", label_visibility="collapsed")
st.caption(f"{sites:,} サイト・{pages:,} ページを収録")

if q:
    page = st.session_state.get("page", 1) if st.session_state.get("q") == q else 1
    st.session_state.update(q=q, page=page)
    results = search(con, q, limit=PER_PAGE * page)
    if not results:
        st.write("見つかりませんでした。")
    for r in results:
        badge = "　`Google拒否`" if r["googlebot_blocked"] else ""
        st.markdown(f"**[{r['title']}]({r['url']})**{badge}  \n"
                    f"<span style='color:#2a7a3a;font-size:0.85em'>{r['host']}</span>  \n"
                    f"<span style='font-size:0.9em'>{r['snippet']}</span>",
                    unsafe_allow_html=True)
    if len(results) == PER_PAGE * page:
        if st.button("もっと見る"):
            st.session_state.page = page + 1
            st.rerun()
