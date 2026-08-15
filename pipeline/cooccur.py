"""共起の集計とネットワーク構築。ここに LLM は一切関与しない（再現性のため）。

重みは NPMI を主に使う。生の共起回数だと「文化庁」のような高頻度語が
何とでも繋がってしまい、図が毛玉になるため。
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from itertools import combinations
from math import log

import networkx as nx

from .config import Config
from .textproc import Basket, NodeMeta


@dataclass
class Stats:
    n_baskets: int
    n_nodes_before_filter: int
    n_edges_before_filter: int
    dates: list[str]


def _count(baskets: list[Basket]) -> tuple[Counter, Counter, int]:
    """単位数・語の出現単位数(df)・ペアの共起単位数を数える。"""
    df: Counter = Counter()
    pairs: Counter = Counter()
    for basket in baskets:
        nodes = sorted(basket.nodes)
        df.update(nodes)
        pairs.update(combinations(nodes, 2))
    return df, pairs, len(baskets)


def _npmi(cooc: int, df_a: int, df_b: int, total: int) -> float:
    p_ab = cooc / total
    if p_ab >= 1.0:
        return 1.0
    pmi = log(p_ab / ((df_a / total) * (df_b / total)))
    return pmi / -log(p_ab)


def _edge_table(df: Counter, pairs: Counter, total: int, cfg: Config) -> dict[tuple[str, str], dict]:
    edges: dict[tuple[str, str], dict] = {}
    for (a, b), cooc in pairs.items():
        if cooc < cfg.min_cooc or df[a] < cfg.min_df or df[b] < cfg.min_df:
            continue
        npmi = _npmi(cooc, df[a], df[b], total)
        if npmi < cfg.min_npmi:
            continue
        edges[(a, b)] = {
            "cooc": cooc,
            "npmi": round(npmi, 4),
            "jaccard": round(cooc / (df[a] + df[b] - cooc), 4),
        }
    return edges


def _trend(baskets: list[Basket], cfg: Config, edges: dict) -> None:
    """直近とそれ以前で NPMI を比べ、エッジに surprise を付ける。

    「今日の頻出語」だけだと毎日同じ顔ぶれになる。直近で急に強まった結びつきを
    上位に出すために、ベースラインとの差を持たせておく。
    """
    dates = sorted({b.date for b in baskets if b.date})
    if not cfg.trend_enabled or len(dates) < 2:
        return
    cutoff = dates[-cfg.trend_recent_days] if len(dates) > cfg.trend_recent_days else dates[-1]
    recent = [b for b in baskets if b.date and b.date >= cutoff]
    base = [b for b in baskets if not b.date or b.date < cutoff]
    if len(recent) < 5 or len(base) < 5:
        return

    df_r, pairs_r, n_r = _count(recent)
    df_b, pairs_b, n_b = _count(base)
    for (a, b), attrs in edges.items():
        c_r = pairs_r.get((a, b), 0)
        c_b = pairs_b.get((a, b), 0)
        npmi_r = _npmi(c_r, df_r[a], df_r[b], n_r) if c_r and df_r[a] and df_r[b] else 0.0
        npmi_b = _npmi(c_b, df_b[a], df_b[b], n_b) if c_b and df_b[a] and df_b[b] else 0.0
        attrs["npmi_recent"] = round(npmi_r, 4)
        attrs["npmi_baseline"] = round(npmi_b, 4)
        attrs["surprise"] = round(npmi_r - npmi_b, 4)
        attrs["is_new"] = c_b == 0 and c_r > 0


def build_graph(
    baskets: list[Basket], node_meta: dict[str, NodeMeta], cfg: Config
) -> tuple[nx.Graph, Stats]:
    df, pairs, total = _count(baskets)
    if total == 0:
        return nx.Graph(), Stats(0, 0, 0, [])

    edges = _edge_table(df, pairs, total, cfg)
    stats = Stats(
        n_baskets=total,
        n_nodes_before_filter=len(df),
        n_edges_before_filter=len(pairs),
        dates=sorted({b.date for b in baskets if b.date}),
    )
    _trend(baskets, cfg, edges)

    # NPMI 上位から採用。同点は共起回数の多い方を優先
    ranked = sorted(edges.items(), key=lambda kv: (kv[1]["npmi"], kv[1]["cooc"]), reverse=True)
    ranked = ranked[: cfg.max_edges]

    graph = nx.Graph()
    for (a, b), attrs in ranked:
        graph.add_edge(a, b, weight=attrs["npmi"], **attrs)

    # ノードが多すぎる場合は重み付き次数の上位だけ残す
    if graph.number_of_nodes() > cfg.max_nodes:
        strength = {n: sum(d["weight"] for _, _, d in graph.edges(n, data=True)) for n in graph}
        keep = {n for n, _ in sorted(strength.items(), key=lambda kv: kv[1], reverse=True)[: cfg.max_nodes]}
        graph = graph.subgraph(keep).copy()
    graph.remove_nodes_from(list(nx.isolates(graph)))

    for node in graph.nodes:
        meta = node_meta.get(node)
        graph.nodes[node]["label"] = meta.label if meta else node
        graph.nodes[node]["kind"] = meta.kind if meta else "term"
        graph.nodes[node]["df"] = df[node]

    _annotate(graph, cfg)
    return graph, stats


def _annotate(graph: nx.Graph, cfg: Config) -> None:
    """コミュニティ・中心性・座標を付ける。seed 固定で毎回同じ結果になるように。"""
    if graph.number_of_nodes() == 0:
        return

    communities = nx.community.louvain_communities(graph, weight="weight", seed=42)
    # 大きいコミュニティから 0,1,2... と番号を振る（色の安定のため）
    for index, members in enumerate(sorted(communities, key=len, reverse=True)):
        for node in members:
            graph.nodes[node]["community"] = index

    degree = dict(graph.degree())
    strength = {n: sum(d["weight"] for _, _, d in graph.edges(n, data=True)) for n in graph}
    if graph.number_of_nodes() <= cfg.betweenness_max_nodes:
        betweenness = nx.betweenness_centrality(graph)
    else:
        betweenness = {n: 0.0 for n in graph}

    for node in graph.nodes:
        graph.nodes[node]["degree"] = degree[node]
        graph.nodes[node]["strength"] = round(strength[node], 4)
        graph.nodes[node]["betweenness"] = round(betweenness.get(node, 0.0), 5)

    for node, (x, y) in _layout(graph, cfg).items():
        graph.nodes[node]["x"] = round(x, 2)
        graph.nodes[node]["y"] = round(y, 2)


def _layout(graph: nx.Graph, cfg: Config) -> dict[str, tuple[float, float]]:
    """連結成分ごとにレイアウトして、円としてパッキングする。

    グラフ全体に spring_layout を一度かけると、小さな成分が遠くに飛ばされて
    主要成分が中央で潰れる。成分ごとに配置してから並べると図が読める密度になる。
    """
    components = sorted(nx.connected_components(graph), key=len, reverse=True)
    blobs: list[tuple[dict, float]] = []
    for comp in components:
        sub = graph.subgraph(comp)
        n = len(comp)
        if n == 1:
            blobs.append(({next(iter(comp)): (0.0, 0.0)}, 1.0))
            continue
        pos = nx.spring_layout(
            sub, weight="weight", seed=42, iterations=cfg.layout_iterations,
            k=2.2 / (n ** 0.5), scale=1.0,
        )
        pos = {k: (float(v[0]), float(v[1])) for k, v in pos.items()}
        cx = sum(p[0] for p in pos.values()) / n
        cy = sum(p[1] for p in pos.values()) / n
        pos = {k: (p[0] - cx, p[1] - cy) for k, p in pos.items()}
        radius = max((p[0] ** 2 + p[1] ** 2) ** 0.5 for p in pos.values()) or 1.0
        # ノードが多い成分ほど広く取る（描画時にラベルが入る余地を作るため）
        target = 1.0 + 1.15 * (n ** 0.5)
        pos = {k: (p[0] / radius * target, p[1] / radius * target) for k, p in pos.items()}
        blobs.append((pos, target))

    placed: list[tuple[float, float, float]] = []
    out: dict[str, tuple[float, float]] = {}
    for pos, radius in blobs:
        ox, oy = _find_slot(placed, radius)
        placed.append((ox, oy, radius))
        for node, (x, y) in pos.items():
            out[node] = (x + ox, y + oy)

    span = max((abs(v) for xy in out.values() for v in xy), default=1.0) or 1.0
    return {k: (x / span * 1000.0, y / span * 1000.0) for k, (x, y) in out.items()}


def _find_slot(placed: list[tuple[float, float, float]], radius: float) -> tuple[float, float]:
    """既に置いた円と重ならない位置を、原点から外向きに探す。"""
    if not placed:
        return 0.0, 0.0
    from math import cos, pi, sin

    gap = 0.8
    step = max(0.6, radius * 0.5)
    ring = step
    while ring < 500:
        count = max(8, int(2 * pi * ring / step))
        for i in range(count):
            angle = 2 * pi * i / count
            x, y = ring * cos(angle), ring * sin(angle)
            if all(((x - px) ** 2 + (y - py) ** 2) ** 0.5 >= radius + pr + gap for px, py, pr in placed):
                return x, y
        ring += step
    return ring, 0.0
