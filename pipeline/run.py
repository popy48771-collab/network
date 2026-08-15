"""パイプライン全体の実行。

    uv run python -m pipeline.run                 # articles/ を分析して out/ に出力
    uv run python -m pipeline.run --articles fixtures/sample_articles --out out/demo
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import cooccur, export, ingest, textproc
from .config import Config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="文化政策ニュースの共起ネットワークを作る")
    parser.add_argument("--articles", default="articles", help="記事を置いたディレクトリ")
    parser.add_argument("--out", default="out", help="出力先ディレクトリ")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dict", dest="dict_dir", default="dict")
    parser.add_argument("--stopwords", default="dict/stopwords.txt")
    parser.add_argument("--gexf-url", default="", help="レポートに載せる Gephi Lite のリンク先")
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    out_dir = Path(args.out)

    print(f"[1/5] 記事を読み込み: {args.articles}")
    docs = ingest.load_articles(args.articles)
    print(f"      {len(docs)} 件")
    if not docs:
        print("      記事がありません。articles/ にファイルを置いてください。")
        print("      対応形式: .txt .md .html .json .jsonl .csv .tsv")
        _write_empty(out_dir, args.gexf_url)
        return 0

    print("[2/5] 辞書と形態素解析器を準備")
    entities = textproc.load_entities(args.dict_dir)
    stopwords = textproc.load_stopwords(args.stopwords)
    print(f"      エンティティ {len(entities)} 件 / ストップワード {len(stopwords)} 件")
    analyzer = textproc.Analyzer(entities, stopwords, cfg)

    print(f"[3/5] テキスト解析（共起単位: {cfg.unit}）")
    baskets = textproc.build_baskets(docs, analyzer, cfg)
    print(f"      {len(baskets)} 単位")

    print("[4/5] 共起ネットワークを構築")
    graph, stats = cooccur.build_graph(baskets, analyzer.node_meta, cfg)
    print(f"      ノード {graph.number_of_nodes()} / エッジ {graph.number_of_edges()}")
    if graph.number_of_nodes() == 0:
        print("      ! 閾値が厳しすぎるか記事が少なすぎます。config.yaml を緩めてください。")

    print(f"[5/5] 出力: {out_dir}")
    payload = export.graph_payload(graph, stats, docs, baskets)
    out_dir.mkdir(parents=True, exist_ok=True)
    export.write_gexf(graph, out_dir / "network.gexf")
    export.write_csv(graph, out_dir)
    export.write_json(payload, out_dir / "graph.json")
    export.write_html(payload, out_dir / "network.html")
    export.write_articles_html(payload, out_dir / "articles.html")
    export.write_articles_csv(payload, out_dir / "articles.csv")
    export.write_report(graph, stats, docs, payload, out_dir / "report.md", args.gexf_url)
    for name in ("network.gexf", "network.html", "articles.html", "report.md",
                 "nodes.csv", "edges.csv", "articles.csv", "graph.json"):
        print(f"      - {out_dir / name}")
    return 0


def _write_empty(out_dir: Path, gexf_url: str) -> None:
    import networkx as nx

    from .cooccur import Stats

    graph = nx.Graph()
    stats = Stats(0, 0, 0, [])
    payload = export.graph_payload(graph, stats, [])
    out_dir.mkdir(parents=True, exist_ok=True)
    export.write_report(graph, stats, [], payload, out_dir / "report.md", gexf_url)


if __name__ == "__main__":
    sys.exit(main())
