# 引き継ぎ — このリポジトリの現在地

新しいセッション（または新しい人）が最初に読む文書。5分で現状を掴めるように書いてある。
最終更新: 2026-08-15

規約は [CLAUDE.md](../CLAUDE.md)、使い方は [README.md](../README.md)、
設計の経緯は [docs/proposal.md](proposal.md) と [docs/ingestion.md](ingestion.md)、
いつ何を変えたかは [docs/decisions.md](decisions.md)。

---

## 1. いま動いているもの

**毎朝 06:00 JST に、記事を集めて共起ネットワークを作り直し、リポジトリに書き戻す。**
人手の操作は不要で、止まっていない限り勝手に更新される。

| 項目 | 現況（2026-08-15） |
|---|---|
| 記事 | 390件（Googleニュース検索RSS 7本から自動収集） |
| 共起単位 | 426文 |
| ネットワーク | 58ノード / 73エッジ（絞り込み前 1599 / 10335） |
| 辞書 | 政策・制度 43件 / 施設・組織 40件 / 除外語 180語 |
| テスト | 33件（`make test`） |
| リポジトリ | **public**（2026-08-15 に private から変更） |

図を見る:

- Gephi Lite … <https://lite.gephi.org/?file=https://raw.githubusercontent.com/popy48771-collab/network/main/out/network.gexf>
- 単体HTML … `out/network.html` をブラウザで開く（外部依存なし）
- レポート … `out/report.md`

---

## 2. 全体の流れ

```
sources/*.yaml   ← 何を集めるか（検索語 = 問いそのもの）
      ↓ pipeline/collect.py（毎朝 / 手動）
articles/YYYY/MM/*.md   ← 1記事1ファイル。冪等（正規化URLのsha256をファイル名に埋めてある）
      ↓ pipeline/ingest.py
記事レコード
      ↓ pipeline/textproc.py
辞書マスク → 形態素解析 → 複合語結合 → 共起単位（既定は文）
      ↓ pipeline/cooccur.py
NPMI → 閾値 → Louvain → 中心性 → レイアウト
      ↓ pipeline/export.py
out/network.gexf / network.html / nodes.csv / edges.csv / graph.json / report.md
```

**LLM はこの流れのどこにも入っていない。** 共起の計算は全て決定的な Python コード。

### ワークフローは2つ

| ファイル | いつ走るか | 何をするか |
|---|---|---|
| `.github/workflows/collect.yml` | 毎朝 06:00 JST / 手動 | 収集 → **分析まで** → `articles/` と `out/` をコミット |
| `.github/workflows/analyze.yml` | `articles/` `dict/` `config.yaml` `pipeline/` への push | 分析 → `out/` をコミット |

collect が分析まで担当しているのは意図的（理由は §4-④）。
両者は `concurrency: repo-write-*` を共有していて、同時には走らない。
書き戻しはどちらも `.github/scripts/commit-and-push.sh` を使う。

---

## 3. 壊してはいけない不変条件

これを破ると、直したつもりで静かに壊れる。

1. **共起の計算・カウント・統計に LLM を使わない。** 再現性が消え、精度でも負ける。
   LLM を足すなら「解釈（日次ブリーフの文章化）」の層だけ。
2. **`dict/*.yaml` を自動更新しない。`id` は変えない。** 辞書が勝手に変わると時系列比較の
   基準が動き、過去の分析と繋がらなくなる。候補出しは `make review`、追加は人間。
   ラベル（表示名）は変えてよい。
3. **seed を外さない。** レイアウトもコミュニティ検出も seed 固定。同じ入力なら同じ図が出る。
   `tests/test_pipeline.py::test_build_is_deterministic` が番人。
4. **`tier: secondary`（報道）の本文を取得・保存しない。** 設定に `fetch_body: true` と
   書かれていても `collect._should_fetch_body` が握り潰す。人間の注意力に頼らない。
   `tests/test_collect.py::test_secondary_tier_never_fetches_body` が番人。
5. **`out/network.html` に外部CDNを参照させない。** オフラインで開けることを維持する。
6. **辞書マッチ → 形態素解析の順序を入れ替えない。**（理由は §4-①）

---

## 4. 実際に踏んだ地雷

**この節がこの文書で一番価値がある。** 同じ穴を掘り直さないこと。

### ① SudachiPy は Mode C でも「文化芸術基本法」を割る

`文化 / 芸術 / 基本法` に分解される（実測）。だから `textproc.py` は
**辞書エンティティを先に表層一致で拾い、その範囲をマスクしてから形態素解析する**
順序になっている。逆にすると政策名がネットワークから消える。
`tests/test_pipeline.py::test_policy_name_is_not_split` が番人。

エイリアスは**長い順に当てる**。「国立劇場おきなわ」が「国立劇場」に食われないため。
短くて汎用的なエイリアス（例:「基本法」）は登録しない。

### ② ElementTree の Element は子を持たないと偽になる

```python
node.find("title") or node.find("atom:title", NS)   # ← 中身のある要素が握り潰される
```

これで RSS のパースが全件0になった。必ず `None` 判定で拾うこと。
いまは名前空間を無視してローカル名で引く `collect._find` に統一してある。

### ③ 生成物の書き戻しで rebase が衝突して push できなくなる

collect と analyze が同時に `out/` を書き戻し、`git pull --rebase` が `out/report.md` で衝突。
一度 rebase が止まると作業ツリーが未解決のまま残り、**以降のリトライが全部失敗**する。
記事391件を収集済みだったのに1件もコミットされなかった。

直し方は2つ。**(a)** 両ワークフローの `concurrency` グループを統一して同時実行を止めた。
**(b)** rebase をやめ、`git reset --mixed origin/<branch>` で HEAD と index だけ
リモートに合わせ、作り直した生成物を載せ直す方式にした（`out/` は `articles/` から
決定的に作り直せるので、衝突という概念が存在しない）。

### ④ GITHUB_TOKEN による push は別のワークフローを起動しない

GitHub の仕様（ワークフローの無限連鎖を防ぐため）。だから collect が `articles/` を
push しても analyze は発火しない。**collect が分析まで担当している**のはこのため。
人が手で記事を置いたときは push イベントで analyze が発火する。

### ⑤ spring_layout を全体に一発でかけると図が潰れる

小さな連結成分が遠くに飛ばされ、主要成分が中央で団子になる。
`cooccur._layout` は**連結成分ごとにレイアウトして円としてパッキング**している。

### ⑥ min_df=2 だと単発記事の2語が NPMI 満点で上位を独占する

1本の記事にしか出ない2語は必ず NPMI = 1.0 になる。実データ390件では
「AGU — ライフ」「トレード — 総合文化政策学部フェア」が上位を占めた。
`min_df: 4` / `min_cooc: 3` に上げて解消。記事が増えたら再調整の余地あり。

### ⑦ 検索語は無関係な固有名詞を大量に拾う

「文化政策」が青山学院大学の**総合文化政策学部**をヒットさせていた。
Googleニュース検索は `-除外語` が使える。`sources/gnews_bunkaseisaku.yaml` 参照。

### ⑧ Googleニュースの見出しは「見出し - 媒体名」形式

末尾の媒体名を分解して `source` に寄せている。要約（description）は見出しの
繰り返しでしかないことが多く、その場合は捨てている。

### ⑨ 同じ記事が複数媒体に配信されると URL が違う

`doc_id`（正規化URL由来）では落とせない。記号と空白を無視した見出しキー
（`collect.title_key`）で、1回の実行内の重複を排除している。
実行をまたぐ分は URL 側で落ちる。

---

## 5. 未解決の課題

優先度順。上ほど効く。

### ① 古い記事が混ざっている（対象期間が 2017-07-21 〜 になっている）

Googleニュース検索は古い記事も返す。あいちトリエンナーレ補助金不交付（2019年）などが
入っている。内容は文化政策として的確だが、**「直近で急に強まった結びつき」（surprise）の
基準が9年分に薄まる**。

直し方: `Source` に `max_age_days` を足し、`collect_source` で古いアイテムを捨てる。
既存の古い記事を消すかどうかは別途判断が要る（消すと母集団が変わる → decisions.md に記録）。

### ② コーパスが見出しだけなので、文脈が見えない（構造的制約）

1記事 ≒ 1文なので、`unit: sentence` と `unit: doc` が実質同じ。いまの図は
「**同じ見出しに一緒に置かれた語**」の図であって、文の中の関係ではない。
見出しは編集判断の凝縮なのでこれ自体は筋が良いが、「文化資源」が保存の文脈で
語られたのか観光の文脈なのかは分からない。

直し方はひとつだけ: **本文を保存できる一次情報（`tier: primary`）を足す。**
文化庁の報道発表、文化審議会の議事録、パブリックコメント。議事録は特に語彙が濃い。

### ③ 文化庁のスクレイプが未実装

`sources/bunka_hodo.yaml` は `enabled: false`。`kind: html_list` のパーサが無い。
実装するときは `fixtures/` に固定HTMLを置いて回帰テストを付けること
（サイト改修で静かに壊れるため）。

### ④ 文部科学省のフィードURLが未確認

`sources/mext_hodo.yaml` は `enabled: false`。<https://www.mext.go.jp/rss.html> で
配信URLを確認して差し替える。開発環境からは外部サイトに接続できないので、
GitHub Actions 側で確認するか、人が見て貼る。

### ⑤ `out/` をコミットしているのでブランチ間で衝突しやすい

生成物をリポジトリに置くのは「GEXF に URL でアクセスできる」ためで、意図的。
ただしブランチで作業していると `out/report.md` が毎回衝突する。
生成物なので、衝突したら**作り直した側を採ればよい**（`git checkout --theirs` など）。

---

## 6. 図を思うように変えたいとき、どこを触るか

**上流ほど効く。閾値から触るのが一番効かない。**（図は綺麗になるが問いには近づかない）

| # | レバー | 何を決めるか |
|---|---|---|
| 1 | `sources/*.yaml` の**検索語** | 母集団。**検索語は問いそのもの**。図の8割がここで決まる |
| 2 | `dict/*.yaml` の**エンティティ** | 解像度。**辞書に無い概念は図に絶対出ない** |
| 3 | `dict/stopwords.txt` | 図の「地」。「支援」「推進」を残すか捨てるか |
| 4 | `config.yaml` の `unit` | **「関係」の定義**。文＝強い結合 / 記事＝緩い連想（※いまは §5-② の制約で効きが薄い） |
| 5 | `min_df` / `min_npmi` / `max_edges` | 何を見るに値するとするか。発見ではなく**編集**の話 |
| 6 | `trend.recent_days` | 「新しさ」の定義。何と比べて「新しい」と言うのか |
| 7 | `pipeline/style.py` | 何を主役にするか。いまは施設（青）と政策（橙）だけ色付き |

進め方:

1. **問いを1つ、文で書く**（例「文化観光は誰の言葉で語られているか」）
2. その問いに必要な**母集団**を決める（検索語）
3. その問いで実体として扱いたいものを**辞書**に入れる
4. 図を見て、**問いに答えられたかだけを判定する**（綺麗かどうかは見ない）
5. 答えられなければ **1か2に戻る**。閾値は最後

変えたら [docs/decisions.md](decisions.md) に記録すること。母集団が変わると
過去との比較ができなくなるので、「いつから変わったか」が後から分かる必要がある。

### 検討中の案: 問いごとにプロファイルを分ける

いまは `config.yaml` が1つで図も1つ。問いが複数あるなら、
`profiles/観光.yaml` → `out/観光/` のように「使うソース・辞書・閾値」を束ねて
別々の図を出すほうが読める。未実装。

---

## 7. 開発の作法

```bash
make setup        # uv sync
make collect      # sources/ のフィードを取得 → articles/ に追加
make collect-dry  # 取得せず件数だけ確認（新ソースの動作確認に使う）
make run          # articles/ を分析 → out/
make demo         # 架空のサンプル記事で動作確認 → out/demo/
make test         # pytest（33件）
make review       # 辞書に追加する候補を頻度順に出す
```

- 作業ブランチ: `claude/culture-policy-cooccurrence-network-xxzzcl`
- `main` へは PR を作ってマージ（これまで #1〜#5）
- **テストは番人**。特に `test_policy_name_is_not_split`、`test_build_is_deterministic`、
  `test_secondary_tier_never_fetches_body` は、壊れたら設計が壊れている合図
- 外部ネットワーク: この開発環境からは `news.google.com` などに接続できない。
  実フィードの確認は GitHub Actions の `collect.yml` を `dry_run: true` で手動実行する

### 色を変えるとき

語は中立グレー、色を持つのは文化施設（青 `#2a78d6`）と政策・制度（橙 `#eb6834`）だけ。
種別は色に加えて形（●■◆▲）でも区別する＝色だけに意味を載せない。
この3色は light/dark 両モードで CVD 分離（ΔE 9.8以上）とコントラスト（3:1以上）の
検証を通したもの。**色を足したくなったら、先に検証してから足すこと。**
根拠と数値は `pipeline/style.py` の docstring にある。

---

## 8. 用語

| 語 | 意味 |
|---|---|
| 共起単位 | 共起を数える箱。既定は1文。この中に一緒に出た語のペアを1回と数える |
| df | その語が出現した共起単位の数（document frequency） |
| NPMI | 正規化相互情報量。-1〜1。「他の語と比べてどれだけ一緒に出やすいか」。生の共起回数を重みにすると「文化庁」が全ノードと繋がって毛玉になるので、こちらを使う |
| surprise | 直近の NPMI − それ以前の NPMI。「今日何が新しいか」を出す指標 |
| tier | `primary`＝官公庁の公表資料（本文保存可） / `secondary`＝報道（見出しのみ） |
| doc_id | 追跡パラメータを落とした正規化URLの sha256 先頭12桁。ファイル名に埋めてある |
