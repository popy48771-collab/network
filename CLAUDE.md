# 文化政策ニュース 共起ネットワーク

`articles/` に置かれた記事を解析し、共起ネットワークを `out/` に出力する。
記事の収集は当面**人手**（GitHub に置く）。自動収集はフェーズ2以降（`docs/proposal.md`）。

## 絶対規則

- **共起の計算・カウント・統計に LLM を使わない。** 決定的な Python コードで行う。
  再現性が消えるうえ、精度でも負ける。LLM を足すなら「解釈（日次ブリーフの文章化）」の層だけ。
- **`dict/*.yaml` を自動更新しない。** 辞書が勝手に変わると時系列比較の基準が動き、
  過去の分析と繋がらなくなる。候補出しは `make review`、追加は人間が手で行う。
- **`dict/*.yaml` の `id` は一度決めたら変えない。** ラベル（表示名）は変えてよい。
- **`seed` を外さない。** レイアウトもコミュニティ検出も seed 固定で、同じ入力なら同じ図が出る。
  これが崩れると「昨日と比べて変わった」が言えなくなる。
- **`out/network.html` に外部 CDN を参照させない。** オフラインでも開けることを維持する。
- 記事本文の扱いは `articles/README.md` の著作権の項に従う（報道記事の全文はリポジトリに置かない）。

## コマンド

```bash
make setup    # uv sync
make run      # articles/ を分析 → out/
make demo     # サンプル記事（架空）で動作確認 → out/demo/
make test     # pytest
make review   # 辞書に追加する候補を頻度順に出す
```

## 構成

| パス | 役割 |
|---|---|
| `articles/` | 入力。人が記事を置く。形式は articles/README.md |
| `dict/` | 政策名・施設名・ストップワード。**人手管理** |
| `config.yaml` | 閾値。図の密度が気に入らないときはまずここ |
| `pipeline/ingest.py` | ファイル → 記事レコード |
| `pipeline/textproc.py` | 辞書マスク → 形態素解析 → 複合語結合 → 共起単位 |
| `pipeline/cooccur.py` | NPMI・コミュニティ・中心性・レイアウト |
| `pipeline/export.py` | GEXF / CSV / JSON / HTML / レポート |
| `pipeline/templates/viewer.html` | 単体HTMLビューア（外部依存なし） |
| `fixtures/sample_articles/` | 架空のサンプル。回帰テストの入力でもある |

## 解析まわりで踏んではいけない地雷

- **SudachiPy は Mode C でも「文化芸術基本法」を 文化/芸術/基本法 に割る。**
  だから `textproc.py` は「辞書エンティティを先に表層一致で拾い、その範囲をマスクしてから
  形態素解析する」順序になっている。この順序を入れ替えると政策名が壊れる。
  `tests/test_pipeline.py::test_policy_name_is_not_split` が番人。
- **複合語の結合はコーパス全体の頻度を見てから決める**（2周なめる）。
  1周で貪欲に結合すると、たまたま隣接しただけの名詞が語になる。
- **共起の重みは NPMI。** 生の共起回数を重みにすると「文化庁」が全ノードと繋がって毛玉になる。
- **辞書のエイリアスは長い順に当てる。** 「国立劇場おきなわ」が「国立劇場」に食われないようにするため。
  短くて汎用的なエイリアス（例:「基本法」）は登録しない。

## 出力の見方

- `out/network.gexf` … Gephi / Gephi Lite 用。`https://lite.gephi.org/?file=<GEXFのURL>` で直接開ける
- `out/network.html` … ブラウザで開くだけのビューア
- `out/report.md` … 頻出語・強い共起・急に強まった結びつき・施設/政策別の要約
- `out/nodes.csv` `out/edges.csv` … 表計算ソフトや Gephi へのインポート用

## 色の決まり（変更するときは根拠ごと）

語は中立グレー、色を持つのは**文化施設（青）と政策・制度（橙）だけ**。
種別は色に加えて形（●■◆▲）でも区別する＝色だけに意味を載せない。
この3色は light/dark 両モードで CVD 分離・コントラストの検証を通したもの
（詳細と数値は `pipeline/style.py` の docstring）。色を足したくなったら、
先に検証してから足すこと。
