# network — 文化政策ニュースの共起ネットワーク

記事を GitHub に置くと、自動で共起ネットワークが作られて `out/` に出力されます。
Gephi Lite でそのまま開ける GEXF と、ブラウザで開くだけの単体 HTML が出ます。

対象はまず**日本の文化政策**。

```
articles/ に記事を置いて push
        ↓  GitHub Actions
辞書マッチ → 形態素解析 → 複合語結合 → 共起（文単位）→ NPMI → コミュニティ検出
        ↓
out/network.gexf   Gephi Lite で開く
out/network.html   ブラウザで開く（外部依存なし）
out/report.md      頻出語・強い共起・急に強まった結びつき
out/nodes.csv      表として使う
out/edges.csv
```

## 使い方

### 1. 記事を置く

`articles/` に置いて push するだけです。`.md` `.txt` `.html` `.json` `.jsonl` `.csv` に対応。

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

- **このリポジトリが private の場合**（現状はこちら）:
  GitHub から `out/network.gexf` をダウンロードし、[lite.gephi.org](https://lite.gephi.org/) の
  画面にドラッグ&ドロップしてください。Gephi Lite は GitHub にログインできないので、
  URL 指定では private リポジトリのファイルを読めません。
- **public にした場合**: URL を渡すだけで直接開けます。
  ```
  https://lite.gephi.org/?file=https://raw.githubusercontent.com/<owner>/<repo>/<branch>/out/network.gexf
  ```
  この URL は Actions の実行サマリにも毎回出力されます。

**単体 HTML** で開く場合は `out/network.html` をブラウザにドロップするだけです
（外部 CDN を一切参照しないので、オフラインでも開けます）。

## 手元で動かす

```bash
make setup   # uv sync
make demo    # 架空のサンプル記事で動作確認 → out/demo/
make run     # articles/ を分析 → out/
make test    # pytest
make review  # 辞書に追加する候補を頻度順に出す
```

## 図が思ったようにならないとき

`config.yaml` を触ります。効くのはこの3つです。

| 症状 | 対処 |
|---|---|
| ノードがほとんど出ない | `min_df` を 2 に下げる（記事30本未満なら必須）。`min_npmi` も 0.15 くらいに |
| 毛玉になる | `min_npmi` を 0.3〜0.4 に上げる。`max_edges` を減らす |
| 意味のない語が目立つ | `dict/stopwords.txt` に足す |
| 施設名・政策名が分解される | `dict/facilities.yaml` `dict/policies.yaml` に登録する（`make review` で候補が出る） |

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

- [docs/proposal.md](docs/proposal.md) — 全体設計。自動収集・多言語展開を含む将来像とロードマップ
- [docs/ingestion.md](docs/ingestion.md) — **記事の投入口の設計**。RSS 自動収集 / Issue 投函 / CSV 一括
- [CLAUDE.md](CLAUDE.md) — 開発規約。触る前に読むこと
- [articles/README.md](articles/README.md) — 記事の置き方と著作権の扱い

## 現状

- ✅ 記事の投入（手動）→ 解析 → GEXF / HTML / CSV / レポート の自動出力
- ✅ 政策・施設辞書（80件）、複合語結合、NPMI、Louvain、時系列 surprise
- ⬜ ニュースの自動収集（RSS / GDELT）— `docs/proposal.md` のフェーズ1
- ⬜ LLM による日次ブリーフ生成 — 同フェーズ2
