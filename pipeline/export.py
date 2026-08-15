"""出力: GEXF（Gephi Lite 用）・CSV・JSON・単体HTMLビューア・レポート。"""

from __future__ import annotations

import csv
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import networkx as nx

from .cooccur import Stats
from .ingest import Doc
from .style import GROUP_STYLE, group_of

TEMPLATE = Path(__file__).parent / "templates" / "viewer.html"


def _size_of(graph: nx.Graph, node) -> float:
    df_max = max((d.get("df", 1) for _, d in graph.nodes(data=True)), default=1)
    df = graph.nodes[node].get("df", 1)
    return round(6.0 + 22.0 * (df / df_max) ** 0.5, 2)


def write_gexf(graph: nx.Graph, path: Path) -> None:
    """Gephi / Gephi Lite で開ける GEXF。座標・色・サイズも埋め込む。"""
    out = graph.copy()
    for node, data in out.nodes(data=True):
        style = GROUP_STYLE[group_of(data.get("kind", "term"))]
        r, g, b = style["rgb"]
        data["viz"] = {
            "color": {"r": r, "g": g, "b": b, "a": 1.0},
            "size": _size_of(graph, node),
            "position": {"x": float(data.get("x", 0.0)), "y": float(data.get("y", 0.0)), "z": 0.0},
        }
        # 座標は viz に入るので属性からは外す（Gephi 側で二重に見えないように）
        data.pop("x", None)
        data.pop("y", None)
    for _, _, data in out.edges(data=True):
        if "is_new" in data:
            data["is_new"] = bool(data["is_new"])
    path.parent.mkdir(parents=True, exist_ok=True)
    nx.write_gexf(out, path, encoding="utf-8", prettyprint=True, version="1.2draft")


def write_csv(graph: nx.Graph, out_dir: Path) -> None:
    """Gephi / 表計算ソフトにそのまま読ませられる表形式（= 表ビュー）。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    node_fields = ["Id", "Label", "kind", "group", "df", "degree", "strength", "betweenness", "community"]
    with (out_dir / "nodes.csv").open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(node_fields)
        for node, data in sorted(graph.nodes(data=True), key=lambda kv: -kv[1].get("strength", 0)):
            writer.writerow([
                node, data.get("label", node), data.get("kind", ""), group_of(data.get("kind", "term")),
                data.get("df", 0), data.get("degree", 0), data.get("strength", 0),
                data.get("betweenness", 0), data.get("community", 0),
            ])

    edge_fields = ["Source", "Target", "Type", "Weight", "cooc", "npmi", "jaccard", "surprise", "is_new"]
    with (out_dir / "edges.csv").open("w", encoding="utf-8-sig", newline="") as fp:
        writer = csv.writer(fp)
        writer.writerow(edge_fields)
        for a, b, data in sorted(graph.edges(data=True), key=lambda kv: -kv[2].get("npmi", 0)):
            writer.writerow([
                a, b, "Undirected", data.get("npmi", 0), data.get("cooc", 0), data.get("npmi", 0),
                data.get("jaccard", 0), data.get("surprise", ""), data.get("is_new", ""),
            ])


def graph_payload(graph: nx.Graph, stats: Stats, docs: list[Doc]) -> dict:
    nodes = []
    for node, data in graph.nodes(data=True):
        nodes.append({
            "id": node,
            "label": data.get("label", node),
            "kind": data.get("kind", "term"),
            "group": group_of(data.get("kind", "term")),
            "df": data.get("df", 0),
            "degree": data.get("degree", 0),
            "strength": data.get("strength", 0),
            "betweenness": data.get("betweenness", 0),
            "community": data.get("community", 0),
            "x": data.get("x", 0.0),
            "y": data.get("y", 0.0),
            "size": _size_of(graph, node),
        })
    edges = [
        {
            "source": a, "target": b,
            "npmi": data.get("npmi", 0), "cooc": data.get("cooc", 0),
            "jaccard": data.get("jaccard", 0),
            "surprise": data.get("surprise"), "is_new": data.get("is_new", False),
        }
        for a, b, data in graph.edges(data=True)
    ]
    dates = [d.date for d in docs if d.date]
    return {
        "nodes": nodes,
        "edges": edges,
        "meta": {
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "n_docs": len(docs),
            "n_units": stats.n_baskets,
            "n_nodes": graph.number_of_nodes(),
            "n_edges": graph.number_of_edges(),
            "period": f"{min(dates)} 〜 {max(dates)}" if dates else "日付情報なし",
            "has_trend": any(e.get("surprise") is not None for e in edges),
        },
    }


def write_json(payload: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


def write_html(payload: dict, path: Path) -> None:
    """外部CDNに依存しない単体HTML。オフラインでもそのまま開ける。"""
    template = TEMPLATE.read_text(encoding="utf-8")
    html = template.replace("/*__GRAPH_DATA__*/null", json.dumps(payload, ensure_ascii=False))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html, encoding="utf-8")


# ---- レポート -------------------------------------------------------------


def _fmt_edge(graph: nx.Graph, a: str, b: str, data: dict) -> str:
    la = graph.nodes[a].get("label", a)
    lb = graph.nodes[b].get("label", b)
    return f"{la} — {lb}"


def write_report(
    graph: nx.Graph, stats: Stats, docs: list[Doc], payload: dict, path: Path, gexf_url: str = ""
) -> None:
    meta = payload["meta"]
    lines: list[str] = []
    add = lines.append

    add("# 共起ネットワーク 分析レポート")
    add("")
    add(f"生成: {meta['generated_at']}")
    add("")
    add("| 項目 | 値 |")
    add("|---|---|")
    add(f"| 記事数 | {meta['n_docs']} |")
    add(f"| 対象期間 | {meta['period']} |")
    add(f"| 共起単位数（文） | {meta['n_units']} |")
    add(f"| ノード数 | {meta['n_nodes']}（絞り込み前 {stats.n_nodes_before_filter}） |")
    add(f"| エッジ数 | {meta['n_edges']}（絞り込み前 {stats.n_edges_before_filter}） |")
    add("")
    if gexf_url:
        add(f"[Gephi Lite で開く]({gexf_url})")
        add("")

    if graph.number_of_nodes() == 0:
        add("## 結果なし")
        add("")
        add("ノードが1つも残りませんでした。記事が少ないか、閾値が厳しすぎます。")
        add("`config.yaml` の `min_df` / `min_cooc` / `min_npmi` を下げて試してください。")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return

    # 頻出語
    add("## 頻出ノード（出現文数）")
    add("")
    add("| # | ノード | 種別 | 出現文数 | 次数 | 媒介中心性 |")
    add("|---:|---|---|---:|---:|---:|")
    top_nodes = sorted(graph.nodes(data=True), key=lambda kv: (-kv[1].get("df", 0), kv[0]))[:30]
    for i, (node, data) in enumerate(top_nodes, 1):
        add(
            f"| {i} | {data.get('label', node)} | {GROUP_STYLE[group_of(data.get('kind','term'))]['label']} "
            f"| {data.get('df',0)} | {data.get('degree',0)} | {data.get('betweenness',0):.4f} |"
        )
    add("")

    # 橋渡し語
    add("## 話題を橋渡ししているノード（媒介中心性 上位）")
    add("")
    add("異なる話題の塊をつなぐ位置にある語。政策の文脈では、ここに注目すべき語が出やすい。")
    add("")
    add("| # | ノード | 媒介中心性 | 出現文数 |")
    add("|---:|---|---:|---:|")
    bridges = sorted(graph.nodes(data=True), key=lambda kv: -kv[1].get("betweenness", 0))[:15]
    for i, (node, data) in enumerate(bridges, 1):
        add(f"| {i} | {data.get('label', node)} | {data.get('betweenness',0):.4f} | {data.get('df',0)} |")
    add("")

    # 強い共起
    add("## 結びつきの強いペア（NPMI 上位）")
    add("")
    add("| # | ペア | NPMI | 共起文数 | Jaccard |")
    add("|---:|---|---:|---:|---:|")
    strong = sorted(graph.edges(data=True), key=lambda kv: -kv[2].get("npmi", 0))[:30]
    for i, (a, b, data) in enumerate(strong, 1):
        add(f"| {i} | {_fmt_edge(graph, a, b, data)} | {data.get('npmi',0):.3f} | {data.get('cooc',0)} | {data.get('jaccard',0):.3f} |")
    add("")

    # 急に強まったペア
    if meta["has_trend"]:
        add("## 直近で急に強まった結びつき（surprise 上位）")
        add("")
        add("直近とそれ以前で NPMI を比べた差。「今日何が新しいか」はここに出る。")
        add("")
        add("| # | ペア | surprise | 直近NPMI | 以前NPMI | 新規 |")
        add("|---:|---|---:|---:|---:|:--:|")
        trend = [e for e in graph.edges(data=True) if e[2].get("surprise") is not None]
        trend.sort(key=lambda kv: -kv[2]["surprise"])
        for i, (a, b, data) in enumerate(trend[:20], 1):
            mark = "★" if data.get("is_new") else ""
            add(
                f"| {i} | {_fmt_edge(graph, a, b, data)} | {data['surprise']:+.3f} "
                f"| {data.get('npmi_recent',0):.3f} | {data.get('npmi_baseline',0):.3f} | {mark} |"
            )
        add("")

    # 施設・政策との結びつき
    entity_nodes = [n for n, d in graph.nodes(data=True) if group_of(d.get("kind", "term")) in {"facility", "policy"}]
    if entity_nodes:
        add("## 文化施設・政策と結びついている語")
        add("")
        for node in sorted(entity_nodes, key=lambda n: -graph.nodes[n].get("strength", 0))[:20]:
            label = graph.nodes[node].get("label", node)
            kind_label = GROUP_STYLE[group_of(graph.nodes[node].get("kind", "term"))]["label"]
            neighbors = sorted(graph.edges(node, data=True), key=lambda kv: -kv[2].get("npmi", 0))[:8]
            items = ", ".join(
                f"{graph.nodes[b if b != node else a].get('label', b)}({d.get('npmi',0):.2f})"
                for a, b, d in neighbors
            )
            add(f"- **{label}**（{kind_label}, 出現{graph.nodes[node].get('df',0)}文）: {items}")
        add("")

    # コミュニティ
    groups = defaultdict(list)
    for node, data in graph.nodes(data=True):
        groups[data.get("community", 0)].append((data.get("strength", 0), data.get("label", node)))
    add("## 話題のかたまり（Louvain コミュニティ）")
    add("")
    for cid, members in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:10]:
        members.sort(reverse=True)
        head = "、".join(label for _, label in members[:12])
        add(f"- **クラスタ {cid}**（{len(members)}語）: {head}")
    add("")

    add("---")
    add("")
    add("### 読み方の注意")
    add("")
    add("- 共起は**相関であって因果ではない**。「A と B が繋がった」は「A が B を引き起こした」ではない。")
    add("- 出現頻度は媒体の取材傾向を反映する。ソースの偏りはそのまま図の偏りになる。")
    add("- NPMI は「他の語と比べてどれだけ一緒に出やすいか」。低頻度語ほど値が高く出やすいので、")
    add("  `min_df` で足切りしている（現在の設定は config.yaml を参照）。")
    add("- 媒介中心性は重みを見ない（本数ベース）で計算している。")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
