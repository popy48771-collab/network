# network — 文化政策ニュースの共起ネットワーク

毎朝ニュースを自動で集め、共起ネットワークを作って `out/` に出力します。
Gephi Lite でそのまま開ける GEXF と、ブラウザで開くだけの単体 HTML が出ます。
記事を手で追加することもできます。

対象はまず**日本の文化政策**。

```
毎朝 06:00 JST  sources/*.yaml のフィードを取得 → articles/ に記事を追加
   （手で置きたいときは articles/ に直接ファイルを置いて push でもよい）
        ↓  GitHub Actions
辞書マッチ → 形態素解析 → 複合語結合 → 共起（文単位）→ NPMI → コミュニティ検出
        ↓
out/network.gexf   Gephi Lite で開く
out/network.html   ブラウザで開く（外部依存なし）
out/articles.html  集めた記事の一覧。図のノードと相互に行き来できる
out/report.md      頻出語・強い共起・急に強まった結びつき・最近の記事
out/nodes.csv      表として使う
out/edges.csv
out/articles.csv
```

## 使い方

### 1. 記事が集まる

毎朝 06:00 JST に `sources/*.yaml` のフィードから自動で集まります。設定は不要です。

手で追加したいときは `articles/` に置いて push するだけです。`.md` `.txt` `.html` `.json` `.jsonl` `.csv` に対応。

```markdown
---
title: 文化審議会が博物館法の運用見直しを議論
date: 2026-08-12
source: 文化庁
url: https://example.com/news/12345
---

文化審議会博物館部会は12日、登録博物館制度の運用について議論した。
学芸員の配置要件を巡り、地方の中小館から負担を懸念する声が上がった。
```

フロントマターは省略できます。詳しくは [articles/README.md](articles/README.md)。

### 2. 自動で分析される

push すると `.github/workflows/analyze.yml` が走り、`out/` に結果を書き戻します。
Actions のサマリにレポート全文と Gephi Lite へのリンクが出ます。

### 3. 見る

**Gephi Lite**（ブラウザ版 Gephi）で開きます。GEXF には座標・色・サイズ・コミュニティ・
中心性が入っているので、開いた直後から図になっています。Gephi Lite 側でレイアウトや
色分けをやり直すこともできます。

このリンクをそのまま開けます（毎朝の更新後も同じ URL のまま使えます）。

<https://lite.gephi.org/?file=https://raw.githubusercontent.com/popy48771-collab/network/main/out/network.gexf>

同じ URL は Actions の実行サマリにも毎回出力されます。

**単体 HTML** で開く場合は `out/network.html` をブラウザにドロップするだけです
（外部 CDN を一切参照しないので、オフラインでも開けます）。

**GitHub Pages** に公開すると、ブックマークできる URL になります。

| | URL |
|---|---|
| 入口 | <https://popy48771-collab.github.io/network/> |
| 図 | <https://popy48771-collab.github.io/network/network.html> |
| 記事一覧 | <https://popy48771-collab.github.io/network/articles.html> |

公開は `.github/workflows/pages.yml` が collect / analyze の完了後に自動で行います。
**最初の1回だけ設定が要ります**: Settings → Pages → Source を `GitHub Actions` に変更。
GITHUB_TOKEN には Pages サイトを新規作成する権限が無いため、ここだけ人手が要ります
（一度作られれば以降は自動）。

### 4. 記事そのものを読む

`out/articles.html` を開くと、集めた記事が新しい順に並びます。見出し・出典・日付・抜粋と、
その記事に出た語（施設は青、政策は橙）が付きます。見出しをクリックすると元記事が開きます。

**図と一覧は往復できます。** 図でノードを選ぶとサイドパネルにその語が出た記事が並び、
「すべて読む」で一覧の絞り込みに飛べます。逆に一覧で語のチップを押すと、
「図でこの語を見る」から `network.html` の同じノードに戻れます。

検索（見出し・抜粋・語）、出典、期間（直近7/30/90日）、並び替えで絞り込めます。
表計算ソフトで見たいときは `out/articles.csv` です。

## 手元で動かす

```bash
make setup        # uv sync
make collect      # sources/ のフィードを取得 → articles/ に追加
make collect-dry  # 取得せず件数だけ確認
make demo         # 架空のサンプル記事で動作確認 → out/demo/
make run          # articles/ を分析 → out/
make test         # pytest
make review       # 辞書に追加する候補を頻度順に出す
```

## 図が思ったようにならないとき

**上流ほど効きます。閾値から触るのが一番効きません**（図は綺麗になりますが、問いには近づきません）。

| 症状 | 対処 |
|---|---|
| 見たい話題が入っていない | `sources/*.yaml` の**検索語**を足す。ここで図の8割が決まる |
| 施設名・政策名が分解される | `dict/facilities.yaml` `dict/policies.yaml` に登録（`make review` で候補が出る）。**辞書に無い概念は図に出ません** |
| 意味のない語が目立つ | `dict/stopwords.txt` に足す |
| 単発記事の2語が上位を独占する | `config.yaml` の `min_df` `min_cooc` を上げる |
| 毛玉になる | `min_npmi` を上げる。`max_edges` を減らす |
| ノードがほとんど出ない | `min_df` を下げる。`min_npmi` も 0.15 くらいに |

考え方の全体像（7つのレバーと進め方）は [docs/handoff.md](docs/handoff.md) §6 に。
変更したら [docs/decisions.md](docs/decisions.md) に記録してください。母集団が変わると
過去との比較ができなくなるので、「いつから変わったか」が後から分かる必要があります。

## 仕組みのポイント

- **数えるのは決定的コード、意味づけだけ LLM。** 共起の計算に LLM は一切使っていません。
  再現性が消えるうえ、精度でも負けるためです。
- **辞書マッチが先、形態素解析が後。** SudachiPy は Mode C でも「文化芸術基本法」を
  文化/芸術/基本法 に割ります。先に政策名を拾ってマスクしてから解析しています。
- **重みは NPMI。** 生の共起回数だと「文化庁」が全ノードと繋がって図が潰れます。
- **語と施設・政策を同じ空間に置く。** だから「"インバウンド" と "日本遺産" が結びついた」が
  1本のエッジとして出ます。
- **日付があると surprise が出せる。** 直近とそれ以前で NPMI を比べ、
  「急に強まった結びつき」を上位に出します。毎日同じ顔ぶれを眺めずに済みます。

## ドキュメント

- **[docs/handoff.md](docs/handoff.md) — 引き継ぎ文書。まずこれを読む。**
  現況・踏んだ地雷・未解決の課題・図を変えたいときどこを触るか
- [docs/decisions.md](docs/decisions.md) — 決定の記録。設定を変えたらここに1行足す
- [docs/proposal.md](docs/proposal.md) — 全体設計。多言語展開を含む将来像とロードマップ
- [docs/ingestion.md](docs/ingestion.md) — 記事の投入口の設計。RSS 自動収集 / Issue 投函 / CSV 一括
- [CLAUDE.md](CLAUDE.md) — 開発規約。触る前に読むこと
- [articles/README.md](articles/README.md) — 記事の置き方と著作権の扱い

## 収集する対象を変える

`sources/*.yaml` を足すだけです。コードは触りません。

Googleニュースの検索結果は**アカウント登録も認証も不要**で RSS として受け取れます。
`sources/_template.yaml` をコピーして、URL の検索語を差し替えるだけで監視対象が増えます。

```
https://news.google.com/rss/search?q=<検索語をURLエンコード>&hl=ja&gl=JP&ceid=JP:ja
```

要約が欲しい場合は [Googleアラート](https://www.google.com/alerts) の RSS 配信も同じ
`kind: rss` で読めます（歯車アイコンから「ダイジェスト」を外さないと RSS を選べません）。

`tier` で本文を保存するかどうかが決まります。`secondary`（報道）は見出しと要約と URL しか
保存せず、`fetch_body: true` と書いても収集コード側で無視されます。
`primary`（官公庁の公表資料）は本文まで保存します（20000字で打ち切り）。

RSS を出していないサイトは `kind: html_list` で一覧ページから拾えます。
CSS セレクタではなく **`link_pattern`（記事URLの正規表現）** を指定します
（セレクタはサイト改修で壊れますが、URL の形はそう変わりません）。
日付はリンクの前後の文脈から探します。例は `sources/bunka_hodo.yaml`。

### 新しいソースを足すときの手順

この開発環境からは外部サイトに接続できないので、確認は Actions 側で行います。
`collect` ワークフローを手動実行するときの入力:

| 入力 | 用途 |
|---|---|
| `probe_url` | URL の下見。フィードとして読めるか、読めなければフィードURLの候補とページ内リンクを出す |
| `dry_run` | 書き込まずに件数だけ確認。**新しいソースを足したら必ず最初にこれ** |
| `show_all` | 取れたアイテムを全件、日付付きで表示（日付の拾い方の確認） |
| `only` | そのソースIDだけ取得（例: `bunka_hodo`）|

## 現状

2026-08-15 時点。記事390件 / 58ノード / 73エッジ。

- ✅ 記事の自動収集（Googleニュース RSS 7本）— 毎朝 06:00 JST
- ✅ 解析 → GEXF / HTML / CSV / レポート の自動出力
- ✅ 政策・制度43件 / 施設・組織40件の辞書、複合語結合、NPMI、Louvain、時系列 surprise
- ✅ **記事一覧ビュー**（`out/articles.html`）— 図のノードと相互に行き来できる
- ✅ **一次情報の本文取り込み** — 文化庁（`kind: html_list`）と文部科学省（RDF）を有効化。
  次の収集から本文が入る（`tier: primary` のみ。報道の本文は保存しない）
- ⬜ 本文が入った後の閾値と除外語の見直し — **これが次の一手**
- ⬜ 古い記事の混入を止める（`max_age_days`）
- ⬜ 文化審議会の議事録（語彙が濃い）
- ⬜ GDELT による多言語収集 — `docs/proposal.md` のフェーズ3
- ⬜ LLM による日次ブリーフ生成 — 同フェーズ2

課題の詳細と直し方は [docs/handoff.md](docs/handoff.md) §5 にあります。
