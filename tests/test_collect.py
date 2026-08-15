"""収集まわりの回帰テスト。ネットワークには一切アクセスしない（fixtures/feeds/ を使う）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from pipeline import collect

ROOT = Path(__file__).resolve().parents[1]
FEEDS = ROOT / "fixtures" / "feeds"
PAGES = ROOT / "fixtures" / "pages"
LIST_BASE = "https://www.example.go.jp/koho_hodo_oshirase/"
LIST_PATTERN = r"/hodohappyo/\d+\.html$"


@pytest.fixture(scope="module")
def gnews_items():
    return collect.parse_feed((FEEDS / "google_news.xml").read_bytes())


@pytest.fixture(scope="module")
def list_items():
    return collect.parse_link_list(
        (PAGES / "bunka_list.html").read_bytes(), LIST_BASE, LIST_PATTERN
    )


# ---- フィード解析 ----


def test_rss_items_are_parsed(gnews_items):
    assert len(gnews_items) == 2


def test_trailing_publisher_is_split_from_title(gnews_items):
    """Google ニュースの「見出し - 媒体名」を分解できること。"""
    item = gnews_items[0]
    assert item.title == "文化財修理の補助拡充へ 文化庁が方針"
    assert item.publisher == "サンプル新聞"


def test_publisher_with_a_hyphen_is_split_from_title():
    """媒体名にハイフンが入ると「見出し - 媒体名」を正規表現では切れない。

    実データで「… 新館長に北山浩士氏 - mc-jpn.com」が見出しに残っていた。
    """
    feed = (
        '<?xml version="1.0"?><rss version="2.0"><channel><item>'
        "<title>東京国立近代美術館 新館長に北山浩士氏 - mc-jpn.com</title>"
        "<link>https://news.example.org/1</link>"
        "<source url='https://mc-jpn.com'>mc-jpn.com</source>"
        "</item></channel></rss>"
    )
    item = collect.parse_feed(feed.encode("utf-8"))[0]
    assert item.title == "東京国立近代美術館 新館長に北山浩士氏"
    assert item.publisher == "mc-jpn.com"


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


# ---- 一覧ページ解析（kind: html_list） ----


def test_only_links_matching_the_pattern_become_items(list_items):
    """一覧ページはナビ・PDF・関連ページだらけ。link_pattern で記事だけ残すこと。"""
    assert len(list_items) == 3
    assert all("/hodohappyo/" in item.url for item in list_items)
    titles = [item.title for item in list_items]
    assert "PDF" not in titles and "審議会のページへ" not in titles


def test_relative_links_are_resolved_against_the_list_page(list_items):
    assert list_items[1].url == (
        "https://www.example.go.jp/koho_hodo_oshirase/hodohappyo/93012201.html"
    )


def test_date_is_read_from_the_text_before_the_link(list_items):
    """<dt>2026年8月14日</dt><dd><a>…</a></dd> の形を拾えること。"""
    assert list_items[0].published == "2026-08-14"
    assert list_items[1].published == "2026-08-07"


def test_date_is_read_from_the_text_after_the_link(list_items):
    """「見出し（2026年7月31日）」の形も拾えること。"""
    assert list_items[2].published == "2026-07-31"


def test_same_url_twice_in_one_page_is_kept_once(list_items):
    assert len({item.url for item in list_items}) == len(list_items)
    assert "交付要綱の改正について（再掲）" not in [item.title for item in list_items]


def test_link_pattern_is_required():
    """パターン無しだと一覧ページの全リンクが記事になる。設定漏れは例外で止める。"""
    with pytest.raises(ValueError):
        collect.parse_link_list(b"<html></html>", LIST_BASE, "")


def test_shift_jis_page_is_decoded():
    """官公庁の古いページは Shift_JIS が残っている。utf-8 決め打ちだと文字化けする。"""
    html = (
        '<html><head><meta charset="Shift_JIS"></head><body>'
        '<a href="/hodohappyo/1.html">文化財の保存と活用について</a>'
        "</body></html>"
    )
    items = collect.parse_link_list(html.encode("shift_jis"), LIST_BASE, LIST_PATTERN)
    assert items[0].title == "文化財の保存と活用について"


def test_title_pattern_filters_a_too_wide_feed(tmp_path, monkeypatch):
    """省庁全体の新着情報のようなフィードを、見出しで問いに寄せられること。"""
    items = [
        collect.Item(title="文化審議会の答申について", url="https://e.go.jp/1"),
        collect.Item(title="H3ロケット9号機の打上げ成功について", url="https://e.go.jp/2"),
        collect.Item(title="博物館ワーキンググループの開催について", url="https://e.go.jp/3"),
    ]
    monkeypatch.setattr(collect, "fetch_items", lambda source: items)
    source = collect.Source(
        id="s", name="n", url="https://e.go.jp/rss", title_pattern="文化|博物館"
    )
    result = collect.collect_source(source, tmp_path, set(), set(), dry_run=True)
    assert result.written == 2
    assert result.filtered == 1


def test_broken_pattern_is_reported_before_fetching(tmp_path):
    source = collect.Source(id="s", name="n", url="https://e.go.jp/rss", title_pattern="文化(")
    result = collect.collect_source(source, tmp_path, set(), set(), dry_run=True)
    assert "title_pattern" in result.error


def test_unknown_kind_is_reported(tmp_path):
    source = collect.Source(id="s", name="n", url="https://e.com/", kind="scrape")
    result = collect.collect_source(source, tmp_path, set(), set(), dry_run=True)
    assert "kind=scrape" in result.error


# ---- 著作権ルールの強制 ----


def test_secondary_tier_never_fetches_body():
    """報道記事の本文取得は、設定に書いてあってもコード側で握り潰すこと。"""
    source = collect.Source(id="s", name="n", url="u", tier="secondary", fetch_body=True)
    assert collect._should_fetch_body(source) is False

    primary = collect.Source(id="s", name="n", url="u", tier="primary", fetch_body=True)
    assert collect._should_fetch_body(primary) is True


def test_primary_article_stores_the_body(tmp_path):
    """官公庁の公表資料は本文まで保存してよい。ここが通らないと §5-② が解けない。"""
    item = collect.Item(
        title="文化財保存活用地域計画を認定しました",
        url="https://www.example.go.jp/koho_hodo_oshirase/hodohappyo/1.html",
        published="2026-08-14",
        body="文化庁は14日、文化財保存活用地域計画12件を認定した。\n認定を受けた市町村は、国庫補助の対象となる。",
    )
    source = collect.Source(
        id="p", name="架空庁", url="u", tier="primary", fetch_body=True
    )
    text = collect.write_article(item, source, tmp_path).read_text(encoding="utf-8")
    assert "tier: primary" in text
    assert "国庫補助の対象となる" in text


def test_body_is_truncated(monkeypatch):
    """1記事が長すぎると共起単位を数百持って母集団を歪める。上限で切ること。"""
    monkeypatch.setattr(
        collect, "fetch", lambda url, retries=3: ("<p>" + "文化政策の推進に関する記述。" * 4000 + "</p>").encode()
    )
    assert len(collect.fetch_body("https://e.com/1")) == collect.BODY_MAX_CHARS


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
        assert source.kind in {"rss", "html_list"}
        # 報道ソースが本文取得を有効にしていないこと
        if source.tier == "secondary":
            assert not source.fetch_body
        # 一覧ページはパターン無しだとナビまで記事になる
        if source.kind == "html_list":
            assert source.link_pattern, f"{source.id}: link_pattern が無い"


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
