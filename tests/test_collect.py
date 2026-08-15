"""収集まわりの回帰テスト。ネットワークには一切アクセスしない（fixtures/feeds/ を使う）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import collect

ROOT = Path(__file__).resolve().parents[1]
FEEDS = ROOT / "fixtures" / "feeds"


@pytest.fixture(scope="module")
def gnews_items():
    return collect.parse_feed((FEEDS / "google_news.xml").read_bytes())


# ---- フィード解析 ----


def test_rss_items_are_parsed(gnews_items):
    assert len(gnews_items) == 2


def test_trailing_publisher_is_split_from_title(gnews_items):
    """Google ニュースの「見出し - 媒体名」を分解できること。"""
    item = gnews_items[0]
    assert item.title == "文化財修理の補助拡充へ 文化庁が方針"
    assert item.publisher == "サンプル新聞"


def test_tracking_params_are_stripped(gnews_items):
    url = gnews_items[0].url
    assert "utm_source" not in url and "utm_medium" not in url
    assert url.endswith("id=7"), "追跡以外のクエリまで落としている"


def test_pubdate_becomes_iso(gnews_items):
    assert gnews_items[1].published == "2026-08-13"


def test_summary_that_only_repeats_the_title_is_dropped(gnews_items):
    assert gnews_items[0].summary == ""
    assert "文化審議会" in gnews_items[1].summary


def test_atom_feed_is_parsed():
    items = collect.parse_feed((FEEDS / "atom.xml").read_bytes())
    assert len(items) == 1
    assert items[0].url.endswith("1234.html")
    assert items[0].published == "2026-08-11"
    assert "文化観光推進法" in items[0].summary


def test_doc_id_follows_the_canonical_url():
    a = collect.Item(title="x", url=collect.canonical_url("https://e.com/a?utm_source=x"))
    b = collect.Item(title="x", url=collect.canonical_url("https://e.com/a"))
    assert a.doc_id == b.doc_id, "追跡パラメータ違いが別記事として登録される"


# ---- 著作権ルールの強制 ----


def test_secondary_tier_never_fetches_body():
    """報道記事の本文取得は、設定に書いてあってもコード側で握り潰すこと。"""
    source = collect.Source(id="s", name="n", url="u", tier="secondary", fetch_body=True)
    assert collect._should_fetch_body(source) is False

    primary = collect.Source(id="s", name="n", url="u", tier="primary", fetch_body=True)
    assert collect._should_fetch_body(primary) is True


def test_secondary_article_stores_only_headline_and_summary(tmp_path, gnews_items):
    source = collect.Source(id="gn", name="Googleニュース", url="u", tier="secondary")
    path = collect.write_article(gnews_items[1], source, tmp_path)
    text = path.read_text(encoding="utf-8")
    assert "登録博物館制度の運用見直し" in text
    assert "tier: secondary" in text
    assert 'url: "https://news.example.org/2026/08/13/hakubutsukan"' in text


# ---- 冪等性 ----


def test_existing_doc_ids_are_detected(tmp_path, gnews_items):
    source = collect.Source(id="gn", name="Googleニュース", url="u")
    collect.write_article(gnews_items[0], source, tmp_path)
    assert gnews_items[0].doc_id in collect.existing_doc_ids(tmp_path)


def test_written_files_are_readable_by_the_analyzer(tmp_path, gnews_items):
    """収集した記事が、そのまま ingest で読めること（形式のずれを防ぐ）。"""
    from pipeline import ingest

    source = collect.Source(id="gn", name="Googleニュース", url="u", tier="secondary")
    for item in gnews_items:
        collect.write_article(item, source, tmp_path)

    docs = ingest.load_articles(tmp_path)
    assert len(docs) == 2
    assert all(d.date and d.title and d.url for d in docs)
    assert {d.source for d in docs} == {"サンプル新聞", "架空タイムス"}


def test_titles_with_quotes_survive_the_front_matter(tmp_path):
    item = collect.Item(
        title='「文化資源」の"活用"をめぐって', url="https://e.com/1", published="2026-08-01"
    )
    source = collect.Source(id="gn", name="n", url="u")
    from pipeline import ingest

    collect.write_article(item, source, tmp_path)
    docs = ingest.load_articles(tmp_path)
    assert docs[0].title == '「文化資源」の"活用"をめぐって'


# ---- ソース定義 ----


def test_shipped_sources_are_valid():
    sources = collect.load_sources(ROOT / "sources")
    assert sources, "sources/ にソースが1つもない"
    assert len({s.id for s in sources}) == len(sources), "ソースIDが重複している"
    for source in sources:
        assert source.tier in {"primary", "secondary"}
        assert source.url.startswith("https://")
        # 報道ソースが本文取得を有効にしていないこと
        if source.tier == "secondary":
            assert not source.fetch_body


def test_template_is_not_loaded_as_a_source():
    ids = {s.id for s in collect.load_sources(ROOT / "sources")}
    assert "my_source" not in ids


# ---- 見出しの重複 ----


def test_same_headline_from_another_outlet_is_skipped(tmp_path):
    """同じ記事が複数媒体に配信されると URL は違う。見出しで落とせること。"""
    a = "学芸員になりきって展示資料PR　延岡城・内藤記念博物館で体験イベント"
    b = "学芸員になりきって展示資料PR 延岡城・内藤記念博物館で体験イベント"
    assert collect.title_key(a) == collect.title_key(b)


def test_different_headlines_keep_different_keys():
    assert collect.title_key("文化庁が補助を拡充") != collect.title_key("文化庁が補助を縮小")
