"""パイプラインの回帰テスト。壊れたときに静かに空の図を出さないための保険。"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pipeline import cooccur, export, ingest, textproc
from pipeline.config import Config

ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "fixtures" / "sample_articles"


@pytest.fixture(scope="module")
def cfg() -> Config:
    c = Config.load(ROOT / "config.yaml")
    c.min_df = 2  # サンプルは記事が少ないので緩める
    c.min_cooc = 2
    return c


@pytest.fixture(scope="module")
def docs():
    return ingest.load_articles(SAMPLES)


@pytest.fixture(scope="module")
def built(docs, cfg):
    entities = textproc.load_entities(ROOT / "dict")
    stopwords = textproc.load_stopwords(ROOT / "dict" / "stopwords.txt")
    analyzer = textproc.Analyzer(entities, stopwords, cfg)
    baskets = textproc.build_baskets(docs, analyzer, cfg)
    graph, stats = cooccur.build_graph(baskets, analyzer.node_meta, cfg)
    return graph, stats, baskets


# ---- 読み込み ----


def test_samples_are_loaded(docs):
    assert len(docs) == 10
    assert all(d.date for d in docs), "ファイル名から日付が取れていない"
    assert all(d.title for d in docs)


def test_title_is_included_in_analysis_text(docs):
    doc = next(d for d in docs if "登録博物館制度" in d.title)
    assert doc.title in doc.text


def test_front_matter_and_body_are_separated(docs):
    doc = next(d for d in docs if "文化財防災" in d.title)
    assert "note:" not in doc.body, "フロントマターが本文に混ざっている"


def test_date_normalization():
    assert ingest.normalize_date("2026年8月12日") == "2026-08-12"
    assert ingest.normalize_date("2026/8/12") == "2026-08-12"
    assert ingest.normalize_date("なし") == ""


def test_csv_and_jsonl_round_trip(tmp_path):
    (tmp_path / "a.csv").write_text(
        "title,body,date\n見出しA,文化庁は文化財の保存を進める。,2026-08-01\n", encoding="utf-8"
    )
    (tmp_path / "b.jsonl").write_text(
        json.dumps({"title": "見出しB", "body": "劇場法の見直しを議論する。", "date": "2026-08-02"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    docs = ingest.load_articles(tmp_path)
    assert {d.title for d in docs} == {"見出しA", "見出しB"}


def test_duplicate_bodies_are_merged(tmp_path):
    for name in ("x.md", "y.md"):
        (tmp_path / name).write_text("同じ本文です。文化庁が発表した。", encoding="utf-8")
    assert len(ingest.load_articles(tmp_path)) == 1


# ---- 解析 ----


def test_policy_name_is_not_split(built):
    """「文化芸術基本法」が 文化/芸術/基本法 に割れていないこと。

    これが壊れると政策名がネットワークから消えるので、最も重要な回帰テスト。
    """
    graph, _, _ = built
    labels = {d.get("label") for _, d in graph.nodes(data=True)}
    assert "文化観光推進法" in labels
    assert "劇場法" in labels


def test_compound_nouns_are_merged(built):
    graph, _, _ = built
    labels = {d.get("label") for _, d in graph.nodes(data=True)}
    assert "地方公共団体" in labels, "名詞の連続が複合語にまとまっていない"
    assert "地方" not in labels


def test_stopwords_are_removed(built):
    graph, _, _ = built
    labels = {d.get("label") for _, d in graph.nodes(data=True)}
    assert not labels & {"こと", "実施", "今回", "方針"}


def test_entities_are_typed(built):
    graph, _, _ = built
    kinds = {d.get("label"): d.get("kind") for _, d in graph.nodes(data=True)}
    assert kinds.get("文化庁") == "org"
    assert kinds.get("文化観光推進法") == "law"


# ---- ネットワーク ----


def test_graph_is_not_empty(built):
    graph, _, _ = built
    assert graph.number_of_nodes() >= 20
    assert graph.number_of_edges() >= 20


def test_npmi_is_in_range(built):
    graph, _, _ = built
    for _, _, data in graph.edges(data=True):
        assert -1.0 <= data["npmi"] <= 1.0
        assert data["cooc"] >= 1


def test_every_node_has_layout_and_community(built):
    graph, _, _ = built
    for _, data in graph.nodes(data=True):
        assert "x" in data and "y" in data
        assert "community" in data


def test_trend_is_computed_when_dates_span(built):
    graph, _, _ = built
    assert any(d.get("surprise") is not None for _, _, d in graph.edges(data=True))


def test_build_is_deterministic(docs, cfg):
    """同じ入力なら毎回同じ図になること（seed 固定の担保）。"""
    def run():
        analyzer = textproc.Analyzer(
            textproc.load_entities(ROOT / "dict"),
            textproc.load_stopwords(ROOT / "dict" / "stopwords.txt"),
            cfg,
        )
        graph, _ = cooccur.build_graph(textproc.build_baskets(docs, analyzer, cfg), analyzer.node_meta, cfg)
        return sorted((a, b, d["npmi"]) for a, b, d in graph.edges(data=True))

    assert run() == run()


# ---- 出力 ----


@pytest.fixture(scope="module")
def payload(built, docs):
    graph, stats, baskets = built
    return export.graph_payload(graph, stats, docs, baskets)


def test_outputs_are_written(built, docs, tmp_path):
    graph, stats, baskets = built
    payload = export.graph_payload(graph, stats, docs, baskets)
    export.write_gexf(graph, tmp_path / "network.gexf")
    export.write_csv(graph, tmp_path)
    export.write_json(payload, tmp_path / "graph.json")
    export.write_html(payload, tmp_path / "network.html")
    export.write_articles_html(payload, tmp_path / "articles.html")
    export.write_articles_csv(payload, tmp_path / "articles.csv")
    export.write_report(graph, stats, docs, payload, tmp_path / "report.md")

    gexf = (tmp_path / "network.gexf").read_text(encoding="utf-8")
    assert "viz:position" in gexf and "文化庁" in gexf

    html = (tmp_path / "network.html").read_text(encoding="utf-8")
    assert "__GRAPH_DATA__" not in html, "テンプレートにデータが埋め込まれていない"
    assert "http://" not in html and "https://" not in html.split("<script>")[0], "外部参照が混ざっている"

    assert "共起ネットワーク 分析レポート" in (tmp_path / "report.md").read_text(encoding="utf-8")
    assert (tmp_path / "nodes.csv").exists() and (tmp_path / "edges.csv").exists()
    assert (tmp_path / "articles.csv").exists()


# ---- 記事一覧 ----


def test_articles_are_listed_newest_first(payload):
    dates = [a["date"] for a in payload["articles"] if a["date"]]
    assert len(payload["articles"]) == 10
    assert dates == sorted(dates, reverse=True)


def test_articles_only_link_to_nodes_that_survived_the_filter(payload):
    """一覧のチップから図に飛べること。閾値で消えた語をチップにすると押しても図に無い。"""
    ids = {n["id"] for n in payload["nodes"]}
    for article in payload["articles"]:
        assert set(article["nodes"]) <= ids


def test_node_and_article_reference_each_other(payload):
    """図 → 記事 → 図 の往復ができること。"""
    articles = payload["articles"]
    node = max(payload["nodes"], key=lambda n: len(n["articles"]))
    assert node["articles"], "どのノードも記事に紐づいていない"
    for i in node["articles"]:
        assert node["id"] in articles[i]["nodes"]


def test_snippet_does_not_repeat_the_title(payload):
    for article in payload["articles"]:
        assert not article["snippet"].startswith(article["title"])


def test_articles_html_is_standalone(payload, tmp_path):
    export.write_articles_html(payload, tmp_path / "articles.html")
    html = (tmp_path / "articles.html").read_text(encoding="utf-8")
    assert "__GRAPH_DATA__" not in html, "テンプレートにデータが埋め込まれていない"
    assert "http://" not in html.split("<script>")[0], "外部参照が混ざっている"
    assert "https://" not in html.split("<script>")[0], "外部参照が混ざっている"
    assert "記事一覧" in html


def test_report_links_to_the_articles(built, docs, payload, tmp_path):
    graph, stats, _ = built
    export.write_report(graph, stats, docs, payload, tmp_path / "report.md")
    report = (tmp_path / "report.md").read_text(encoding="utf-8")
    assert "## 最近の記事" in report
    assert payload["articles"][0]["title"] in report


def test_empty_input_does_not_crash(tmp_path):
    from pipeline.run import main

    assert main(["--articles", str(tmp_path / "nothing"), "--out", str(tmp_path / "out")]) == 0
    assert (tmp_path / "out" / "report.md").exists()
