# 文化政策ニュース共起ネットワーク基盤 — 設計提案

対象: 日本の文化政策（フェーズ1）→ 各国の文化政策（フェーズ3）
実装手段: Claude Code（開発・運用の両方）+ Python
版: v1 / 2026-08-15

---

## 0. この提案の要点

やりたいことを分解すると、実は性質の異なる4つの仕事が混ざっています。

| 層 | 仕事 | 誰がやるべきか |
|---|---|---|
| 収集 | 各国・各機関のニュースを毎日集める | 決定的なコード（LLM 不使用） |
| 抽出 | 本文を語・施設名・政策名に分解する | 形態素解析 + 辞書 + **Claude（曖昧な部分だけ）** |
| 集計 | 共起を数え、ネットワークを組む | 決定的なコード（LLM 不使用） |
| 解釈 | 「今日何が起きたか」を言葉にする | **Claude** |

**設計原則: 数えるのは決定的コード、意味づけだけ LLM。**
共起の数値を LLM に出させると再現性が消え、コストが跳ね上がり、しかも精度で負けます。逆に「この記事の"文化資源"は観光文脈か保存文脈か」を正規表現でやると破綻します。この線引きが本提案の背骨です。

Claude Code は「パイプラインを書く道具」であると同時に「パイプラインの一部として毎日走るランタイム」でもあります（後述 §6）。

---

## 1. 全体アーキテクチャ

```
                    ┌────────────────────────────────────────┐
   [ 収集 ]         │ sources/*.yaml  で宣言的にソース定義    │
   毎日 06:00 JST   │  - RSS / Atom                          │
                    │  - HTML 一覧ページの差分               │
                    │  - GDELT DOC 2.0 API（多言語・国際）   │
                    └────────────────┬───────────────────────┘
                                     ↓  raw/YYYY-MM-DD/*.json
   [ 正規化 ]        本文抽出・重複排除（URL正規化 + SimHash）・言語判定
                                     ↓  docs.parquet / SQLite
   [ 抽出 ]          ① 形態素解析（GiNZA/SudachiPy）→ 名詞句・複合語
                     ② 辞書マッチ（施設マスタ・政策マスタ）→ canonical_id
                     ③ Claude 構造化抽出（辞書外の政策・主体・金額・地域・スタンス）
                                     ↓  terms / entities テーブル
   [ 集計 ]          文単位の共起カウント → NPMI / Jaccard → 閾値 → グラフ
                     コミュニティ検出（Louvain）・中心性・時系列サプライズ
                                     ↓  edges.parquet / graph.json
   [ 出力 ]          ① 日次ブリーフ（Claude が数値を読んで文章化）
                     ② インタラクティブ共起ネットワーク（HTML 単体ファイル）
                     ③ 施設×キーワード / 政策×キーワードの二部グラフ
```

各層はファイルで疎結合にします（前の層の出力ファイルが次の層の入力）。こうしておくと、
「抽出だけやり直す」「過去 90 日分を新しい辞書で再集計する」が数分で回り、開発中の試行錯誤が段違いに速くなります。

---

## 2. データソース（フェーズ1: 日本の文化政策）

### 2.1 一次情報（政策そのもの）

| ソース | 取得方式 | 備考 |
|---|---|---|
| 文化庁「広報・報道・お知らせ」 | HTML 一覧の差分 | RSS が確認できないため一覧ページをスクレイプ |
| 文部科学省 報道発表 | **RSS あり** | 文化行政は MEXT 側にも出る |
| 文化審議会・各分科会 議事録/配布資料 | HTML + PDF | 議事録は語彙の宝庫。PDF は別処理 |
| e-Gov パブリックコメント | HTML | 政策の"入口"を捉えられる |
| 都道府県・政令市の文化振興課 | RSS/HTML | 地方文化政策。まず 5 自治体から |
| 独法・基盤機関（芸術文化振興会、国際交流基金 等） | RSS/HTML | 助成・事業の実行主体 |

### 2.2 二次情報（報道・言説）

| ソース | 取得方式 | 備考 |
|---|---|---|
| GDELT DOC 2.0 API | JSON API | `sourcelang` / `sourcecountry` で言語・国を絞れる。国際展開の主力。無料・ライセンス的に安全 |
| 各社 RSS（全国紙・地方紙の文化面） | RSS | **全文は取らない。見出し＋要約＋URL のみ**（§8 参照） |
| 業界メディア（美術手帖、ぴあ 等） | RSS | 施設・展覧会の話題が濃い |

ソースは全て `sources/*.yaml` で宣言します。コードを書かずにソースを増やせる形にするのが重要です:

```yaml
# sources/bunka_hodo.yaml
id: bunka_hodo
name: 文化庁 広報・報道・お知らせ
country: JP
lang: ja
tier: primary          # primary=一次情報 / secondary=報道
kind: html_list
url: https://www.bunka.go.jp/koho_hodo_oshirase/
list_selector: "main ul li a"     # 一覧のリンク
date_selector: "main ul li time"
body_selector: "main"
fetch_body: true       # 一次情報は本文を取る（官公庁の公表資料）
robots: respect
rate_limit_sec: 3
```

`kind` は `rss` / `html_list` / `gdelt` / `pdf_list` の 4 種類だけ用意し、パーサはそれぞれ 1 本。新ソース追加は YAML 1 枚 + セレクタ確認で終わります（§6 の `/add-source` コマンドで半自動化）。

---

## 3. データモデル

SQLite（開発）→ Parquet（分析）の二本立て。規模的に数年分でも数 GB に収まるので、DB サーバは不要です。

```sql
-- 記事
CREATE TABLE docs (
  doc_id      TEXT PRIMARY KEY,   -- sha256(canonical_url)[:16]
  source_id   TEXT, country TEXT, lang TEXT, tier TEXT,
  url         TEXT, canonical_url TEXT,
  title       TEXT, body TEXT,     -- body は tier=primary のみ保持
  published_at TEXT, fetched_at TEXT,
  simhash     INTEGER              -- 近似重複排除用
);

-- 語（形態素解析結果、内容語のみ）
CREATE TABLE terms (
  doc_id TEXT, sent_id INTEGER, pos_in_sent INTEGER,
  surface TEXT, lemma TEXT, pos TEXT      -- 名詞/動詞/形容詞 + 複合名詞
);

-- 正規化済みエンティティ（キーワードと「施設・政策」を結ぶ肝）
CREATE TABLE entities (
  doc_id TEXT, sent_id INTEGER,
  canonical_id TEXT,      -- e.g. "facility:national_theatre", "policy:bunka_geijutsu_kihonho"
  etype TEXT,             -- facility | policy | law | program | org | region | person
  surface TEXT,
  method TEXT,            -- dict | llm
  confidence REAL
);

-- 共起エッジ（日次で作り直す。窓・単位はメタに持つ）
CREATE TABLE edges (
  date TEXT, scope TEXT,           -- scope: all | tier:primary | region:kansai ...
  node_a TEXT, node_b TEXT,        -- lemma または canonical_id
  cooc INTEGER, df_a INTEGER, df_b INTEGER,
  npmi REAL, jaccard REAL,
  surprise REAL                    -- 直近30日移動平均からの乖離
);
```

**ポイント**: ノードは「語」と「正規化エンティティ」を同じ空間に置きます。そうすると
「"インバウンド" と `facility:kokuritsu_gekijo` が今日強く結びついた」
が 1 本のエッジとして自然に出ます。ご質問の「どの文化施設や政策に強く結びついているのか」はこの設計で直接答えられます。

---

## 4. テキスト解析の設計

### 4.1 形態素解析と語の作り方

- **SudachiPy（Mode C = 最長単位）+ GiNZA** を採用。`文化芸術基本法` を `文化/芸術/基本/法` に割らないことが決定的に重要です。
- 内容語（名詞・動詞・形容詞）のみ採用、`ストップワード`（する・こと・ため・年度・実施 …）を除去。
- 連続名詞の複合語化（`地域 + 文化 + 創生` → `地域文化創生`）を後処理で実施。
- ユーザー辞書に文化政策語彙を登録（`dict/sudachi_user.csv`）。

### 4.2 エンティティ辞書（人手管理 + LLM 提案）

`dict/policies.yaml` / `dict/facilities.yaml` を YAML で人手管理します。

```yaml
- id: policy:bunka_geijutsu_kihonho
  label: 文化芸術基本法
  etype: law
  aliases: [文化芸術振興基本法, 文化芸術基本法（改正）, 基本法]
  since: 2001-12-07
  notes: 2017年に「文化芸術振興基本法」から改称

- id: facility:tokyo_national_museum
  label: 東京国立博物館
  etype: facility
  aliases: [東博, トーハク, Tokyo National Museum]
  region: 13   # 都道府県コード
  operator: org:national_institutes_for_cultural_heritage
```

辞書は必ず腐ります。そこで **「Claude が候補を提案 → 人間が承認」** のループを用意します（§6 の `/entity-review`）。
LLM に辞書を直接書き換えさせないのは、時系列比較の基準が勝手に動くと分析が壊れるからです。

### 4.3 Claude による構造化抽出

辞書で拾えない部分だけを Claude（Haiku で十分）に投げます。1 記事 1 リクエスト、JSON schema 固定:

```json
{
  "policies": [{"surface": "文化観光拠点施設整備", "etype": "program", "evidence": "…"}],
  "facilities": [{"surface": "県立美術館", "resolved_hint": "群馬県立近代美術館"}],
  "actors": [{"surface": "文化庁", "role": "実施主体"}],
  "money": [{"amount": 320000000, "currency": "JPY", "purpose": "改修費"}],
  "regions": ["群馬県", "高崎市"],
  "frame": "promotion|preservation|tourism|education|industry|international|labor",
  "stance_to_policy": "supportive|critical|neutral|descriptive"
}
```

`frame`（どの文脈で語られているか）が効きます。同じ「文化資源」でも観光振興フレームと保存フレームでは共起相手が全く違い、これを層として持つと「今日の"文化資源"は観光文脈に寄っている」という所見が出せます。

コスト感: 1 日 150 記事 × 平均 2,500 トークン ≒ 40 万トークン/日。Haiku なら日次で数十円規模です。

### 4.4 共起ネットワークの構成

```python
# analyze/cooccur.py（概念コード）
def build(docs, unit="sentence", min_df=5, min_npmi=0.25, top_edges=1500):
    # 1) 出現: 同一単位内の集合（重複語は1回）
    #    → 「同じ語を連呼する記事」で重みが暴れるのを防ぐ
    baskets = [set(t.node for t in unit_terms) for unit_terms in split(docs, unit)]

    df   = Counter(n for b in baskets for n in b)
    cooc = Counter(pair for b in baskets for pair in combinations(sorted(b), 2))
    N    = len(baskets)

    for (a, b), c in cooc.items():
        if df[a] < min_df or df[b] < min_df or c < 3:
            continue
        pmi  = log((c * N) / (df[a] * df[b]))
        npmi = pmi / -log(c / N)          # -1..1 に正規化
        jac  = c / (df[a] + df[b] - c)
        yield Edge(a, b, cooc=c, npmi=npmi, jaccard=jac)
```

決めておくべきパラメータと推奨初期値:

| 項目 | 推奨 | 理由 |
|---|---|---|
| 共起の単位 | **文**（`unit="sentence"`）を既定、記事単位も併算 | 記事単位は「同じ記事に出た」だけで繋がり、密になりすぎる |
| 重み | NPMI 主、生共起は太さの参考 | 生の共起頻度は「文化庁」のような高頻度語が全部と繋がる |
| 閾値 | df ≥ 5, cooc ≥ 3, NPMI ≥ 0.25 | 日次 100〜200 記事規模での経験則。要調整 |
| エッジ上限 | 1,500（描画時は 300） | 可読性 |
| コミュニティ検出 | Louvain（`networkx` / `python-igraph`） | 「文化財保存」「インバウンド観光」「劇場支援」のような話題塊が出る |
| 中心性 | 次数 + 媒介中心性 | 媒介が高い語＝異なる話題を橋渡しする語（政策上おもしろい） |

**時系列サプライズ**が本命の指標です。

```
surprise(a,b,今日) = npmi(a,b,今日) − mean(npmi(a,b, 直近30日))
```

「今日どのキーワードが頻出か」だけだと毎日「文化庁・文化財・支援」が上位に来て退屈です。
「昨日まで繋がっていなかった語が今日繋がった」を上位に出すと、日報として読む価値が生まれます。

---

## 5. 出力

1. **日次ブリーフ（Markdown / HTML）** — Claude が edges・surprise・記事メタを読んで 600〜1,000 字。
   構成は固定: ①今日の要点3行 ②新しく強まった語のペア（surprise 上位、各々に根拠記事リンク）③施設・政策別の話題 ④先週からの変化 ⑤要注視。
   **必ず根拠記事の URL を併記**させること。数値と記事に紐づかない記述を書かせないのが品質の生命線です。
2. **共起ネットワーク図** — 単体 HTML（`vis-network` 或いは `d3` を埋め込み、外部 CDN なし）。ノード色＝コミュニティ、太さ＝NPMI、大きさ＝df。日付スライダで推移。
3. **二部グラフビュー** — 施設/政策 × キーワード。「この施設は今どんな語と共に語られているか」を見る専用ビュー。
4. **配信** — GitHub Pages に静的公開、または MCP 経由で Gmail / Drive へ。

---

## 6. Claude Code をどう組み込むか

ここが本提案の中心です。Claude Code を**開発ツール**と**運用ランタイム**の両方で使います。

### 6.1 開発時

**`CLAUDE.md`（リポジトリ規約）** — これを最初に書くことが最大の投資です。

```markdown
# 文化政策ニュース共起ネットワーク

## 絶対規則
- 共起の計算・カウント・統計に LLM を使わない。決定的な Python コードで行う。
- 辞書ファイル（dict/*.yaml）を自動更新しない。提案は proposals/ に出し、人間が承認する。
- 記事本文を tier=secondary のソースから保存しない（見出し・要約・URL のみ）。
- 新しいソースは sources/*.yaml で宣言する。パーサ本体は増やさない。
- スキーマ変更時は migrations/ に SQL を追加し、既存データの再構築手順を書く。

## コマンド
- 収集: `uv run python -m pipeline.collect --date today`
- 抽出: `uv run python -m pipeline.extract --date today`
- 集計: `uv run python -m pipeline.analyze --date today --unit sentence`
- 全部: `make daily`
- テスト: `uv run pytest`（fixtures/ の固定 HTML でパーサを検証）
```

**Skills / スラッシュコマンド**（`.claude/skills/`）で反復作業を型にします。

| コマンド | 中身 |
|---|---|
| `/add-source <URL>` | ページを取得 → 一覧・日付・本文のセレクタを推定 → `sources/*.yaml` を生成 → 3 件試験取得して差分を見せる → `fixtures/` にテスト用 HTML を保存 |
| `/entity-review` | 直近の未解決エンティティ（`method=llm` かつ辞書未登録）を頻度順に提示し、辞書追加・別名統合・却下を対話で決めて `dict/*.yaml` にパッチを当てる |
| `/daily-brief [date]` | 集計結果を読んでブリーフを生成。テンプレと禁止事項（根拠なし断定の禁止）をスキル内に固定 |
| `/tune-threshold` | 閾値を振ってネットワークの密度・モジュラリティを比較し、推奨値を提案 |

**サブエージェント**の使いどころは主に2つ。
①ソース追加時、複数サイトのセレクタ推定を並列で走らせる。
②パーサが壊れた（サイト改修）ときの原因切り分け。
逆に、日次の記事抽出をサブエージェントでやるのは非効率です（単純な API バッチで十分）。

### 6.2 運用時

**推奨: GitHub Actions で定期実行。**

```yaml
# .github/workflows/daily.yml
name: daily
on:
  schedule: [{cron: "0 21 * * *"}]   # 06:00 JST
  workflow_dispatch:
jobs:
  run:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: astral-sh/setup-uv@v5
      - run: uv sync
      - run: uv run python -m pipeline.collect --date today
      - run: uv run python -m pipeline.extract --date today   # Claude API 使用
        env: {ANTHROPIC_API_KEY: "${{ secrets.ANTHROPIC_API_KEY }}"}
      - run: uv run python -m pipeline.analyze --date today
      - name: brief
        run: |
          npx -y @anthropic-ai/claude-code -p \
            "$(cat prompts/daily_brief.md)" \
            --allowedTools "Read,Bash(uv run:*)" > out/brief-$(date +%F).md
        env: {ANTHROPIC_API_KEY: "${{ secrets.ANTHROPIC_API_KEY }}"}
      - run: |
          git add data out && git -c user.name=bot -c user.email=bot@local \
            commit -m "daily: $(date +%F)" && git push
```

`claude -p`（ヘッドレスモード）でブリーフ生成だけ LLM に任せ、他は素の Python です。
Claude Code on the web の Routine（cron トリガー）でも同じことができますが、
**外部ネットワークの許可設定に注意**が要ります。実測として、このセッションの環境ポリシーは
`bunka.go.jp` / `api.gdeltproject.org` への接続を 403 で拒否しました（パッケージレジストリと Anthropic のみ許可）。
収集ジョブは、外向き通信を許可した環境か GitHub Actions 上で回してください。

---

## 7. リポジトリ構成

```
network/
├── CLAUDE.md                    # 規約（§6.1）
├── Makefile                     # make daily / make rebuild
├── pyproject.toml               # uv 管理
├── sources/                     # ★ソース定義 YAML（増やすのはここだけ）
│   ├── bunka_hodo.yaml
│   ├── mext_rss.yaml
│   └── gdelt_jp_culture.yaml
├── dict/                        # ★人手管理の語彙資源
│   ├── policies.yaml
│   ├── facilities.yaml
│   ├── stopwords.txt
│   └── sudachi_user.csv
├── pipeline/
│   ├── collect.py               # 取得（kind 別パーサ4本）
│   ├── normalize.py             # 本文抽出・重複排除
│   ├── extract.py               # 形態素解析 + 辞書 + Claude 抽出
│   ├── analyze.py               # 共起・NPMI・コミュニティ・surprise
│   └── render.py                # HTML ネットワーク図
├── prompts/
│   ├── extract_entities.md
│   └── daily_brief.md
├── .claude/
│   ├── skills/{add-source,entity-review,daily-brief,tune-threshold}/
│   └── settings.json
├── .github/workflows/daily.yml
├── data/                        # SQLite / Parquet（大きいものは LFS か外部）
├── fixtures/                    # パーサ回帰テスト用の固定 HTML
├── out/                         # 日次ブリーフ・グラフ HTML
└── tests/
```

---

## 8. 法務・倫理・作法（先に決めておくべきこと）

- **著作権**: 日本の著作権法 30 条の 4 により情報解析目的の複製は広く認められますが、**本文の再配布**は別問題です。方針: 一次情報（官公庁の公表資料）は本文保持、報道は**見出し＋リード＋URL のみ**保持し、公開リポジトリには派生統計（語・共起・カウント）だけを置く。
- **robots.txt / 利用規約**を各ソースの YAML に記録し、収集器が必ず参照する。`rate_limit_sec` 既定 3 秒、User-Agent に連絡先を明示。
- **API 優先**: 取れるものは GDELT や公式 RSS から。スクレイピングは最後の手段。
- **分析上の注意を出力に明記**: 共起は相関であって因果ではないこと、頻度は媒体の取材傾向を反映すること。ブリーフのフッタに定型で入れます。
- **多言語展開時**: 翻訳してから共起を取ると語の粒度が壊れます。**原語のまま解析し、`canonical_id` レベルで国際比較**するのが正解です（「文化政策」と "cultural policy" と "Kulturpolitik" を同じ概念 ID に束ねる）。

---

## 9. ロードマップ

| フェーズ | 期間目安 | 成果物 | 完了条件 |
|---|---|---|---|
| **P0 骨格** | 0.5 日 | MEXT RSS 1 本で 収集→抽出→共起→HTML が通る | `make daily` が最後まで走る |
| **P1 日本の文化政策** | 2〜3 日 | ソース 8〜12 本、辞書 v1（政策 60 / 施設 200 語）、NPMI + surprise、日次ブリーフ、GH Actions 定期実行 | 30 日分を再構築して、ブリーフが人間の読むに耐える |
| **P2 深掘り** | 1 週 | 施設×キーワード二部グラフ、フレーム分類、日付スライダ付きダッシュボード、`/entity-review` 運用 | 「ある施設の語られ方の変化」を図で説明できる |
| **P3 国際比較** | 2 週〜 | GDELT で 10 か国、概念 ID による横断比較、国別ネットワークの差分 | 「日本と韓国で"文化産業"の共起相手がどう違うか」が出る |

P1 の終わりに一度立ち止まって、**「この日報を毎朝読みたいか」**を判定してください。読みたくないなら、原因はほぼ常に「サプライズ指標が弱い」か「辞書が薄い」のどちらかで、ソースを増やしても解決しません。

---

## 10. 想定される失敗と対策

| 失敗 | 兆候 | 対策 |
|---|---|---|
| ネットワークが毛玉になる | エッジ数千、コミュニティが 2 個 | 単位を文に、NPMI 閾値を上げ、高 df 語（文化庁・実施・支援）をストップワード化 |
| 毎日同じ図が出る | surprise 上位が固定 | 前 30 日ベースラインを導入済みか確認。記事数が少なすぎる（< 30/日）なら週次に切替 |
| 表記ゆれで分断 | 「東京国立博物館」と「東博」が別ノード | `/entity-review` を週 1 で回す。エイリアスは辞書の資産 |
| パーサが静かに壊れる | ある日から特定ソースの記事が 0 件 | `fixtures/` 回帰テスト + 「ソース別取得件数が前週比 0 件」で Actions を失敗させる |
| LLM 抽出のブレ | 同じ記事で結果が変わる | `temperature=0`、JSON schema 固定、抽出結果を doc_id でキャッシュして再実行しない |
| コスト超過 | 月額が読めない | 抽出は Haiku、ブリーフのみ上位モデル。日次の記事数に上限を設け、超過分は翌日回し |

---

## 11. 次の一手

P0 を実装します。具体的には —

1. `pyproject.toml`（sudachipy, ginza, networkx, anthropic, httpx, feedparser, selectolax, duckdb）
2. `sources/mext_rss.yaml` 1 本
3. `pipeline/` 4 本の最小実装
4. `make daily` と、出力される単体 HTML のネットワーク図
5. `CLAUDE.md` と `/add-source` スキル

ここまでで、実データの共起ネットワークが手元に出ます。閾値や辞書の議論は、実物を見てからの方が圧倒的に速く収束します。
