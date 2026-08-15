# 文化政策ニュース 共起ネットワーク

Googleニュース検索RSS から記事を毎朝集め、共起ネットワークを `out/` に出力する。

**まず [docs/handoff.md](docs/handoff.md) を読むこと。** 現況・踏んだ地雷・未解決の課題が
まとまっている。設定を変えたら [docs/decisions.md](docs/decisions.md) に記録すること。

## 絶対規則

- **共起の計算・カウント・統計に LLM を使わない。** 決定的な Python コードで行う。
  再現性が消えるうえ、精度でも負ける。LLM を足すなら「解釈（日次ブリーフの文章化）」の層だけ。
- **`dict/*.yaml` を自動更新しない。** 辞書が勝手に変わると時系列比較の基準が動き、
  過去の分析と繋がらなくなる。候補出しは `make review`、追加は人間が手で行う。
- **`dict/*.yaml` の `id` は一度決めたら変えない。** ラベル（表示名）は変えてよい。
- **`tier: secondary`（報道）の本文を取得・保存しない。** 見出し + フィードの要約 + URL のみ。
  全文をリポジトリに置くと再配布にあたる。`collect._should_fetch_body` が設定ミスを握り潰す。
  `tier: primary`（官公庁の公表資料）は本文まで保存してよい（`BODY_MAX_CHARS` で打ち切り）。
- **広いフィードは `title_pattern` で絞ってから入れる。** 省庁全体の新着情報のような
  フィードをそのまま入れると、本文2万字の無関係な議事録がコーパスを埋める。
  この場合 **`title_pattern` が検索語＝問いそのもの**なので、変更は decisions.md に記録する。
- **`seed` を外さない。** レイアウトもコミュニティ検出も seed 固定で、同じ入力なら同じ図が出る。
  これが崩れると「昨日と比べて変わった」が言えなくなる。
- **`out/network.html` に外部 CDN を参照させない。** オフラインでも開けることを維持する。
- 記事本文の扱いは `articles/README.md` の著作権の項に従う。

## コマンド

```bash
make setup        # uv sync
make collect      # sources/ のフィードを取得 → articles/ に追加
make collect-dry  # 取得せず件数だけ確認（新ソースの動作確認に使う）
make run          # articles/ を分析 → out/
make demo         # サンプル記事（架空）で動作確認 → out/demo/
make test         # pytest
make review       # 辞書に追加する候補を頻度順に出す
```

収集コマンドの補助オプション（新しいソースを足すときに使う）:

```bash
uv run python -m pipeline.collect --probe <URL>   # そのURLが使えるかだけ確認する
uv run python -m pipeline.collect --dry-run --show-all --only bunka_hodo
```

この開発環境からは `news.google.com` などの外部サイトに接続できない。
実データの確認は `collect.yml` を手動実行する（入力: `dry_run` / `show_all` / `only` / `probe_url`）。
`probe_url` は新しい監視対象の下見（フィードとして読めるか、フィードURLの候補は何か）。
collect と analyze は concurrency グループを共有しているので、**手動実行は1本ずつ**
（待機中の実行は次の実行が来るとキャンセルされる）。

## 構成

| パス | 役割 |
|---|---|
| `sources/` | 何を集めるか。**検索語 = 問いそのもの**。増やすのは YAML だけ。<br>`kind: rss`（RSS/RDF/Atom）と `kind: html_list`（一覧ページ）がある |
| `articles/` | 入力。自動収集が書く。人が置いてもよい（形式は articles/README.md） |
| `dict/` | 政策名・施設名・ストップワード。**人手管理** |
| `config.yaml` | 閾値。図の密度が気に入らないときはまずここ |
| `pipeline/collect.py` | フィード取得 → 記事ファイル。冪等 |
| `pipeline/ingest.py` | ファイル → 記事レコード |
| `pipeline/textproc.py` | 辞書マスク → 形態素解析 → 複合語結合 → 共起単位 |
| `pipeline/cooccur.py` | NPMI・コミュニティ・中心性・レイアウト |
| `pipeline/export.py` | GEXF / CSV / JSON / HTML / 記事一覧 / レポート |
| `pipeline/style.py` | ノードの色と形（配色の根拠は docstring に） |
| `pipeline/templates/viewer.html` | 図の単体HTMLビューア（外部依存なし） |
| `pipeline/templates/articles.html` | 記事一覧の単体HTMLビューア（同上） |
| `.github/workflows/collect.yml` | 毎朝の収集 → 分析 → コミット。手動実行の入力は下記 |
| `.github/workflows/analyze.yml` | push 起点の分析 → コミット |
| `.github/workflows/pages.yml` | collect / analyze の完了後に `out/` を GitHub Pages へ公開 |
| `.github/pages/index.html` | 公開サイトの入口ページ（図と記事一覧への導線） |
| `.github/scripts/commit-and-push.sh` | 生成物の書き戻し（衝突しない方式） |
| `fixtures/` | 回帰テストの入力（架空のサンプル記事・フィード・HTMLページ） |

## 踏んではいけない地雷（詳細は docs/handoff.md §4）

- **SudachiPy は Mode C でも「文化芸術基本法」を 文化/芸術/基本法 に割る。**
  だから `textproc.py` は「辞書エンティティを先に表層一致で拾い、その範囲をマスクしてから
  形態素解析する」順序になっている。入れ替えると政策名が壊れる。
  `tests/test_pipeline.py::test_policy_name_is_not_split` が番人。
- **ElementTree の Element は子を持たないと偽になる。** `node.find("a") or node.find("b")` と
  書くと中身のある要素が握り潰される。`collect._find` を使う。
- **辞書のエイリアスは長い順に当てる。** 「国立劇場おきなわ」が「国立劇場」に食われないため。
  短くて汎用的なエイリアス（例:「基本法」）は登録しない。
- **複合語の結合はコーパス全体の頻度を見てから決める**（2周なめる）。
  1周で貪欲に結合すると、たまたま隣接しただけの名詞が語になる。
- **共起の重みは NPMI。** 生の共起回数を重みにすると「文化庁」が全ノードと繋がって毛玉になる。
- **`min_df` を下げすぎない。** 1本の記事にしか出ない2語は必ず NPMI = 1.0 になり、上位を占める。
- **生成物の書き戻しで rebase しない。** `out/` は `articles/` から作り直せるので、
  `commit-and-push.sh` の方式（リモートに合わせ直してから載せ直す）を使う。
- **GITHUB_TOKEN による push は別のワークフローを起動しない。** だから collect.yml は
  分析まで自分で担当している。

## 出力の見方

- `out/network.gexf` … Gephi / Gephi Lite 用。`https://lite.gephi.org/?file=<GEXFのURL>` で直接開ける
- `out/network.html` … ブラウザで開くだけのビューア
- `out/articles.html` … 集めた記事の一覧。図のノードと `#node=<id>` で相互に行き来できる
- `out/report.md` … 頻出語・強い共起・急に強まった結びつき・施設/政策別の要約・最近の記事
- `out/nodes.csv` `out/edges.csv` `out/articles.csv` … 表計算ソフトや Gephi へのインポート用

図と一覧は同じ payload（`graph.json`）から作る。記事とノードの相互参照を payload に
持たせてあるので、**図で見つけた語から記事本体に必ず辿れる**。この往復が切れると、
共起の意味を取り違えたまま解釈することになる。

## 色の決まり（変更するときは根拠ごと）

語は中立グレー、色を持つのは**文化施設（青）と政策・制度（橙）だけ**。
種別は色に加えて形（●■◆▲）でも区別する＝色だけに意味を載せない。
この3色は light/dark 両モードで CVD 分離・コントラストの検証を通したもの
（詳細と数値は `pipeline/style.py` の docstring）。色を足したくなったら、
先に検証してから足すこと。
