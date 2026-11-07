# jp-small-web

Googleで見つけにくい「日本語の小さなサイト」を集める、独立系検索エンジン。

**検索画面：https://jp-small-web00.streamlit.app/**

## しくみ

[Common Crawl](https://commoncrawl.org/)（無料の公開クロールデータ）の索引から、次の条件に当てはまるホストを抜き出します。

- 収集されたページ数が少ない（初期値：5ページ以下）
  - Common Crawl はリンクが集まりにくいサイトほど少ししか集めないため、「目立たないサイト」の目安になる
- ページの半分以上が日本語
- 大手サイト・大型プラットフォーム（`config/exclude_domains.txt`）ではない

費用はかかりません（Common Crawl も GitHub Actions も無料）。

## 自動実行

`.github/workflows/extract.yml` が毎日 3:17（日本時間）に動き、索引ファイルを20個ずつ処理して結果をコミットします。1クロール（約300ファイル）を約2週間で処理し終え、新しいクロールが公開されると自動で切り替わります。

手動で動かすには、GitHub の **Actions → 候補サイトの抽出 → Run workflow**。

## SNSから集める

`.github/workflows/social.yml` が毎日 2:17（日本時間）に動き、Bluesky（20分間の公開投稿の流れ）と Mastodon の日本のサーバー（直近24時間の公開投稿）から、日本語の投稿に貼られたリンクを集めます。Common Crawl に入る前の新しいサイトや、少人数にだけ共有されているサイトを拾うためです。

- 4人以上が共有しているホスト（話題のサイト・大手）は除く
- 閲覧注意の投稿・自動投稿（bot）・アダルト／アフィリエイト系のホストは除く
- 結果は `data/social/<日付>.csv`。本文の取得（第二段階）で Common Crawl の候補と一緒に扱われる

## 日本語以外も対象にする

`--langs` で言語を指定できます（ISO 639-3 の3文字コード）。

```bash
python scripts/extract_candidates.py --langs jpn,eng,kor   # 日本語・英語・韓国語
python scripts/extract_candidates.py --langs all           # 全言語
```

日本語以外の結果は `data/candidates-<言語>/` と `data/state-<言語>.json` に別に保存されます。

**全言語は量に注意**：索引1ファイルあたり約3.6万件（日本語の約90倍）、1クロールで約1,000万件・1GB前後になり、GitHubリポジトリに置くには大きすぎます。数言語に絞るのがおすすめです。除外リストも日本向けなので、他言語では各国の大手サイトを足してください。

## 出力

- `data/candidates/<クロールID>/part-XXXXX.csv` … 列：`host, registered_domain, pages, sample_url, language`
- `data/state.json` … 処理の進み具合

## 手元で動かす

```bash
pip install -r requirements-batch.txt
python scripts/extract_candidates.py --files 2
```

## 本文の取得と索引（第二段階）

`.github/workflows/crawl.yml` が毎日 4:17（日本時間）に動き、まだ取得していない候補サイトを3,000件ずつ取得して索引に加えます。

- robots.txt を守り、noindex のページは登録しない
- 1サイトにつき最大5ページ。トップページから同じサイト内のリンクをたどる（登録済みのサイトにも毎回1,000サイトずつページを追加）
- ひらがな・カタカナがほとんどないページ（中国語など）と、中身が重複するページは登録しない
- robots.txt で **Googlebot だけを拒否している** サイトには印を付ける（検索画面に「Google拒否」と表示）
- 索引（`index.db`、SQLite FTS5）はリポジトリではなく、GitHub のリリース「index」に保存

日本語は2文字ずつ重ねて区切る bi-gram 方式で索引を作るので、形態素解析なしで「福岡」「花火」のような2文字の語も検索できます。

## 検索画面

```bash
pip install -r requirements.txt
streamlit run app.py
```

Streamlit Community Cloud に、このリポジトリの `app.py` を指定すれば無料で公開できます。索引はリリースから自動で読み込みます。

## ファイル

- `scripts/extract_candidates.py` … 候補の抽出（第一段階）
- `scripts/crawl.py` … 本文の取得と索引への追加
- `scripts/textindex.py` … 索引の作成・検索の共通処理
- `app.py` … 検索画面
