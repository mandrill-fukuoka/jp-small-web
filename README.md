# jp-small-web

Googleで見つけにくい「日本語の小さなサイト」を集める、独立系検索エンジンの第一段階。

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

## 出力

- `data/candidates/<クロールID>/part-XXXXX.csv` … 列：`host, registered_domain, pages, sample_url`
- `data/state.json` … 処理の進み具合

## 手元で動かす

```bash
pip install -r requirements.txt
python scripts/extract_candidates.py --files 2
```

## 次の段階

1. 候補サイトの robots.txt を確認し、許可されたページだけ本文を取得する
2. SQLite FTS5（trigram）で全文検索の索引を作る
3. Streamlit で検索画面を公開する
