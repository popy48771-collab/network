"""sources/*.yaml を読んでフィードを取得し、articles/ に記事ファイルを書き出す。

    uv run python -m pipeline.collect              # 全ソースを取得
    uv run python -m pipeline.collect --dry-run    # 書き込まずに件数だけ見る
    uv run python -m pipeline.collect --only ga_bunkacho

冪等: 同じ記事（正規化URLが同じ）は何度走らせても1ファイルにしかならない。

取得の種類は2つ。
  kind: rss        RSS 2.0 / RDF / Atom を parse_feed で読む
  kind: html_list  RSS が無いサイト向け。一覧ページのリンクを parse_link_list で拾う

著作権の扱いはコードで強制する。人間の注意力に頼ると必ず破られるため。
  tier: primary   … 官公庁・独法の公表資料。本文まで取得・保存してよい
  tier: secondary … 報道記事。見出し + フィードの要約 + URL のみ。本文は取りに行かない
`fetch_body: true` を書いても tier が primary でなければ無視される（_should_fetch_body）。
"""

from __future__ import annotations

import argparse
import hashlib
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from pathlib import Path

import yaml

from .ingest import _DATE_PATTERNS, _TextExtractor, normalize_date, normalize_text

USER_AGENT = (
    "culture-policy-network/0.1 (+https://github.com/popy48771-collab/network; "
    "research use; contact via GitHub issues)"
)
TIMEOUT = 30
NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}

# 本文の保存上限。官公庁ページは添付の説明や関連リンクで長くなることがあり、
# 全部入れると1記事が共起単位を数百持って母集団を歪める。
BODY_MAX_CHARS = 20000
# 一覧ページのリンクを記事とみなす最短の見出し長。「PDF」「詳細」「一覧」を落とす
MIN_LIST_TITLE_LEN = 5
# 日付を探す文脈の幅（リンクの前後それぞれ何文字まで見るか）
DATE_CONTEXT_CHARS = 80

# 追跡用クエリは URL 正規化のときに落とす（同じ記事が別URLで二重登録されるのを防ぐ）
_TRACKING = re.compile(r"^(utm_|fbclid|gclid|igshid|mc_[ce]id|ref|ref_src|spm|yclid|_ga)")
# Google ニュースの見出し末尾に付く「 - 媒体名」
_TRAILING_SOURCE = re.compile(r"\s+[-–—]\s+([^-–—]{2,30})$")


@dataclass
class Source:
    id: str
    name: str
    url: str
    kind: str = "rss"
    tier: str = "secondary"
    country: str = "JP"
    lang: str = "ja"
    enabled: bool = True
    fetch_body: bool = False
    max_items: int = 60
    rate_limit_sec: float = 2.0
    # kind: html_list のとき、記事リンクとみなす href の正規表現。必須
    link_pattern: str = ""
    # 見出しがこの正規表現に一致するアイテムだけ採る。省庁全体の新着情報のように
    # フィードが広すぎるときの絞り込み。この場合 **これが検索語＝問いにあたる**
    title_pattern: str = ""
    note: str = ""

    @classmethod
    def from_dict(cls, data: dict, path: Path) -> "Source":
        known = {f for f in cls.__dataclass_fields__}
        missing = {"id", "name", "url"} - set(data)
        if missing:
            raise ValueError(f"{path}: 必須項目がありません: {sorted(missing)}")
        return cls(**{k: v for k, v in data.items() if k in known})


@dataclass
class Item:
    title: str
    url: str
    summary: str = ""
    published: str = ""
    publisher: str = ""
    body: str = ""

    @property
    def doc_id(self) -> str:
        return hashlib.sha256(self.url.encode("utf-8")).hexdigest()[:12]


@dataclass
class Result:
    source: Source
    fetched: int = 0
    written: int = 0
    skipped: int = 0
    filtered: int = 0  # title_pattern に一致しなかった件数
    error: str = ""
    samples: list[str] = field(default_factory=list)
    dates: list[str] = field(default_factory=list)  # 新規アイテムの日付（空文字＝日付なし）


# ---- ソース定義 -----------------------------------------------------------


def load_sources(dir_path: Path | str) -> list[Source]:
    dir_path = Path(dir_path)
    sources: list[Source] = []
    if not dir_path.exists():
        return sources
    for path in sorted(dir_path.glob("*.yaml")):
        if path.name.startswith("_"):  # _ 始まりはテンプレート扱い
            continue
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        for entry in data if isinstance(data, list) else [data]:
            if isinstance(entry, dict):
                sources.append(Source.from_dict(entry, path))
    return sources


# ---- 取得 -----------------------------------------------------------------


def fetch(url: str, retries: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(retries):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
                return response.read()
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            last = exc
            if attempt < retries - 1:
                time.sleep(2 ** attempt)
    raise RuntimeError(f"取得失敗: {url} ({last})")


def _decode(payload: bytes) -> str:
    """meta charset を見てからデコードする。官公庁の古いページは Shift_JIS が残っている。"""
    head = payload[:4096].decode("ascii", errors="ignore").lower()
    hit = re.search(r'charset=["\']?\s*([\w\-]+)', head)
    if hit:
        try:
            return payload.decode(hit.group(1), errors="replace")
        except LookupError:
            pass
    return payload.decode("utf-8", errors="replace")


def canonical_url(url: str) -> str:
    """追跡パラメータとフラグメントを落として正規化する。"""
    try:
        parts = urllib.parse.urlsplit(url.strip())
    except ValueError:
        return url.strip()
    query = [
        (k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
        if not _TRACKING.match(k)
    ]
    return urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), "")
    )


# ---- フィード解析 ---------------------------------------------------------


def _local(tag: str) -> str:
    """{名前空間}タグ名 → タグ名。RSS2.0 / RDF / Atom を同じコードで扱うため。"""
    return tag.rsplit("}", 1)[-1] if isinstance(tag, str) else ""


def _find(node, *names):
    """子要素を名前空間なしのタグ名で引く。

    ElementTree の Element は子を持たないと偽になるので、`a or b` で繋ぐと
    「中身のある要素」が握り潰される。必ず None 判定で拾うこと。
    """
    for name in names:
        for child in node:
            if _local(child.tag) == name:
                return child
    return None


def _text(element) -> str:
    if element is None:
        return ""
    return normalize_text("".join(element.itertext()))


def _text_of(node, *names) -> str:
    return _text(_find(node, *names))


def _strip_html(text: str) -> str:
    if "<" not in text:
        return normalize_text(text)
    parser = _TextExtractor()
    parser.feed(text)
    return normalize_text(parser.text())


def _parse_date(raw: str) -> str:
    raw = raw.strip()
    if not raw:
        return ""
    try:  # RFC 822（RSS 2.0）
        return parsedate_to_datetime(raw).date().isoformat()
    except (TypeError, ValueError):
        pass
    try:  # ISO 8601（Atom）
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return normalize_date(raw)


def parse_feed(payload: bytes) -> list[Item]:
    """RSS 2.0 / RDF / Atom を同じ形に均す。"""
    root = ET.fromstring(payload)
    items: list[Item] = []

    for node in (e for e in root.iter() if _local(e.tag) in {"item", "entry"}):
        title = _strip_html(_text_of(node, "title"))
        url = _text_of(node, "link")
        if not url:  # Atom は link/@href
            for child in node:
                if _local(child.tag) == "link" and child.get("rel", "alternate") == "alternate":
                    url = child.get("href", "")
                    break
        if not url or not title:
            continue

        summary = _strip_html(
            _text_of(node, "description", "summary", "content", "encoded")
        )
        published = _parse_date(
            _text_of(node, "pubDate", "date", "published", "updated")
        )
        publisher = _text_of(node, "source")

        # Google ニュースは見出し末尾に「 - 媒体名」を付ける。媒体名は source に寄せる。
        # 媒体名自体にハイフンが入ること（mc-jpn.com など）があり正規表現では切れないので、
        # <source> で媒体名が分かっているときはその文字列を直接剥がす
        if publisher and re.search(r"\s[-–—]\s" + re.escape(publisher) + r"\s*$", title):
            title = re.sub(r"\s[-–—]\s" + re.escape(publisher) + r"\s*$", "", title)
        else:
            hit = _TRAILING_SOURCE.search(title)
            if hit:
                if not publisher:
                    publisher = hit.group(1).strip()
                title = title[: hit.start()].strip()

        # 要約が見出しの繰り返しでしかないことがある（Google ニュース）。その場合は捨てる
        if summary and (summary == title or summary.startswith(title[:20])):
            summary = ""

        items.append(
            Item(title=title, url=canonical_url(url), summary=summary,
                 published=published, publisher=publisher)
        )
    return items


# ---- 一覧ページ解析（kind: html_list） ------------------------------------


class _LinkList(HTMLParser):
    """一覧ページから「リンク先・見出し・その前後のテキスト」を拾う。

    RSS を出していないサイト（文化庁の報道発表など）はこれで拾うしかない。
    構造はサイトごとに違うので、セレクタを書かせるのではなく
    **href の正規表現だけを設定させて、日付は前後の文脈から探す**方式にした。
    セレクタはサイト改修のたびに壊れるが、URLの形はそう変わらない。
    """

    _SKIP = {"script", "style", "noscript", "svg", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        # (href, 見出し, 直前のテキスト, 直後のテキスト)
        self.links: list[list[str]] = []
        # <link rel="alternate" type="application/rss+xml"> で告知されたフィード
        self.feeds: list[str] = []
        self._skip_depth = 0
        self._depth = 0          # <a> のネスト深さ
        self._before = ""        # 直近に流れたテキスト（日付の文脈）
        self._open: list | None = None

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
            return
        if tag == "link":
            attr = dict(attrs)
            if "xml" in (attr.get("type") or "") and attr.get("href"):
                self.feeds.append(attr["href"])
            return
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        if self._open is not None:  # <a> の中の <a>（不正なHTML）は無視する
            self._depth += 1
            return
        self._open = [href, "", self._before[-DATE_CONTEXT_CHARS:], ""]
        self._depth = 1

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
            return
        if tag != "a" or self._open is None:
            return
        self._depth -= 1
        if self._depth > 0:
            return
        self._open[1] = normalize_text(self._open[1])
        self.links.append(self._open)
        self._open = None
        self._before = ""

    def handle_data(self, data):
        if self._skip_depth:
            return
        if self._open is not None:
            self._open[1] += data
            return
        self._before += data
        # 直前のリンクから見れば、これは「直後のテキスト」でもある
        if self.links and len(self.links[-1][3]) < DATE_CONTEXT_CHARS:
            self.links[-1][3] += data


def _last_date(text: str) -> str:
    """テキスト中で最後に現れる日付を ISO 8601 で返す。

    normalize_date は最初の一致を返すが、リンクの手前の文脈では
    **リンクに近い＝最後の**日付のほうが正しい。
    """
    best = ""
    for pattern, (y, m, d) in _DATE_PATTERNS:
        for hit in pattern.finditer(text):
            try:
                value = datetime(
                    int(hit.group(y)), int(hit.group(m)), int(hit.group(d))
                ).date().isoformat()
            except ValueError:
                continue
            best = value
    return best


def parse_link_list(payload: bytes, base_url: str, link_pattern: str) -> list[Item]:
    """一覧ページから記事アイテムを作る。href が link_pattern に一致するものだけ拾う。"""
    if not link_pattern:
        raise ValueError("kind: html_list には link_pattern（href の正規表現）が要ります")
    pattern = re.compile(link_pattern)

    parser = _LinkList()
    parser.feed(_decode(payload))

    items: list[Item] = []
    seen: set[str] = set()
    for href, title, before, after in parser.links:
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        url = canonical_url(urllib.parse.urljoin(base_url, href))
        if not pattern.search(url) or len(title) < MIN_LIST_TITLE_LEN:
            continue
        if url in seen:
            continue
        seen.add(url)
        # 日付は「リンクの直前 → 直後 → 見出しの中 → URL」の順に探す。
        # 一覧ページは <dt>2026年8月14日</dt><dd><a>…</a></dd> の形が多い
        published = (
            _last_date(before)
            or normalize_date(after[:DATE_CONTEXT_CHARS])
            or normalize_date(title)
            or normalize_date(urllib.parse.urlsplit(url).path)
        )
        items.append(Item(title=title, url=url, published=published))
    return items


# ---- 本文取得（tier: primary のみ） ---------------------------------------


def _should_fetch_body(source: Source) -> bool:
    """報道記事の本文は取りに行かない。設定ミスをここで握り潰す。"""
    return source.fetch_body and source.tier == "primary"


def fetch_body(url: str, retries: int = 1) -> str:
    """本文を取る。**再試行はしない**のが既定。

    本文は取れなくても見出しで記事は登録できる（`collect_source` が握り潰す）。
    一方で1ページあたり3回×30秒の再試行を積むと、相手が詰まっているときに
    30件で45分かかる（実測で1回の収集が15分を超えた）。任意の取得に時間を使わない。
    """
    parser = _TextExtractor()
    parser.feed(_decode(fetch(url, retries=retries)))
    text = normalize_text(parser.text())
    return "\n".join(_content_lines(text))[:BODY_MAX_CHARS]


# ページの「部品」。本文ではないので保存しない。
# 実データで踏んだ: 文化庁の報道発表を取り込んだら、頻出語の上位を
# 「傍聴登録」「御覧」「ダウンロード」「Adobe」「PDF形式」「rights」「reserved」が占めた。
# これは閾値では直らない（どのページにも必ず出るので df も NPMI も高い）。
# **本文でないものを保存しない**のが正しい直し方。
_BOILERPLATE = re.compile(
    r"メニュー開閉|メニューを開く|メニューを閉じる|ここから本文|本文へ移動"
    r"|Adobe|Acrobat|Reader|rights reserved|Copyright|PDF形式|ダウンロード"
    r"|傍聴|お問(い)?合(わ)?せ|問い合わせ先|電話番号|電話:|内線|FAX"
    r"|サイトマップ|プライバシーポリシー|ウェブアクセシビリティ|免責事項"
    r"|このページ|ページの先頭|前のページに戻る|関連リンク|新着情報一覧"
    r"|別添|担当:|\(担当\)|Copyright|https?://"
)


def _content_lines(text: str) -> list[str]:
    """本文らしい行だけ残す。短い行・ページの部品・繰り返しを落とす。

    見出しは HTML の <title> と <h1> で2〜3回繰り返されることが多い。
    同じ行を何度も数えると、その語の df がページ数ぶん水増しされる。
    """
    seen: set[str] = set()
    lines = []
    for line in text.splitlines():
        line = line.strip()
        if len(line) < 15 or _BOILERPLATE.search(line) or line in seen:
            continue
        seen.add(line)
        lines.append(line)
    return lines


# ---- 書き出し -------------------------------------------------------------


def existing_doc_ids(articles_dir: Path) -> set[str]:
    """ファイル名の末尾に doc_id を埋めてあるので、走査だけで既出判定ができる。"""
    return {
        path.stem.rsplit("-", 1)[-1]
        for path in articles_dir.rglob("*.md")
        if "-" in path.stem
    }


def _front_matter(value: str) -> str:
    """YAML のスカラーとして安全な形に包む。"""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_article(item: Item, source: Source, articles_dir: Path) -> Path:
    date = item.published or datetime.now(timezone.utc).astimezone().date().isoformat()
    out_dir = articles_dir / date[:4] / date[5:7]
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / f"{source.id}-{item.doc_id}.md"

    publisher = item.publisher or source.name
    lines = [
        "---",
        f"title: {_front_matter(item.title)}",
        f"date: {date}",
        f"source: {_front_matter(publisher)}",
        f"url: {_front_matter(item.url)}",
        f"collected_by: {source.id}",
        f"tier: {source.tier}",
        f"lang: {source.lang}",
        f"country: {source.country}",
        "---",
        "",
        item.title + "。",
    ]
    body = item.body if source.tier == "primary" else item.summary
    if body:
        lines += ["", body]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


# ---- 実行 -----------------------------------------------------------------


def title_key(title: str) -> str:
    """見出しの同一性を見るためのキー。記号と空白の違いは無視する。

    同じ記事が複数媒体に配信されると、URL が違うので doc_id では重複を落とせない。
    """
    return re.sub(r"[\s　【】「」『』（）()\[\]・:：\-–—|｜/／]+", "", title)


def fetch_items(source: Source) -> list[Item]:
    """ソース定義に従ってアイテムを取ってくる。取得と解析の分岐はここだけ。

    設定の検証は取得より先にやる。ネットワークに出てから落ちると、
    設定ミスが「取得失敗」に化けて原因が分からなくなる。
    """
    if source.kind not in {"rss", "html_list"}:
        raise ValueError(f"kind={source.kind} は未対応（rss / html_list のみ）")
    if source.kind == "html_list" and not source.link_pattern:
        raise ValueError(f"{source.id}: kind: html_list には link_pattern が要ります")
    for field_name in ("link_pattern", "title_pattern"):
        try:
            re.compile(getattr(source, field_name))
        except re.error as exc:
            raise ValueError(f"{source.id}: {field_name} が正規表現として不正です（{exc}）") from exc
    payload = fetch(source.url)
    if source.kind == "rss":
        return parse_feed(payload)
    return parse_link_list(payload, source.url, source.link_pattern)


def collect_source(
    source: Source, articles_dir: Path, seen: set[str], seen_titles: set[str], dry_run: bool,
    show_all: bool = False,
) -> Result:
    result = Result(source=source)
    try:
        items = fetch_items(source)[: source.max_items]
    except (RuntimeError, ValueError, ET.ParseError) as exc:
        result.error = str(exc)[:200]
        return result

    result.fetched = len(items)
    if source.title_pattern:
        # 省庁全体の新着情報のような広すぎるフィードを、見出しで問いに寄せる
        keep = re.compile(source.title_pattern)
        matched = [i for i in items if keep.search(i.title)]
        result.filtered = len(items) - len(matched)
        items = matched
    for item in items:
        key = title_key(item.title)
        if item.doc_id in seen or key in seen_titles:
            result.skipped += 1
            continue
        seen.add(item.doc_id)
        seen_titles.add(key)
        if _should_fetch_body(source):
            try:
                item.body = fetch_body(item.url)
                time.sleep(source.rate_limit_sec)
            except RuntimeError:
                pass  # 本文が取れなくても見出しだけで登録する
        if not dry_run:
            write_article(item, source, articles_dir)
        result.written += 1
        result.dates.append(item.published)
        if show_all or len(result.samples) < 3:
            # 本文の字数まで出す。dry-run の目的は「ちゃんと取れているか」の確認なので、
            # 件数だけ見えても本文が空だったことに気付けない
            size = f"（本文 {len(item.body)}字）" if item.body else ""
            date = item.published or "日付なし"
            result.samples.append(f"[{date}] {item.title}{size}")
    return result


def probe(url: str) -> int:
    """URLが使えるソースかどうかだけ確認する。新しい監視対象を足す前の下見に使う。

    開発環境から外部サイトに繋がらないので、これは Actions から実行する
    （collect.yml の probe_url）。フィードなら件数、HTMLならフィードのURL候補を出す。
    """
    print(f"確認: {url}")
    try:
        payload = fetch(url)
    except RuntimeError as exc:
        print(f"⚠️ {exc}")
        return 1
    print(f"取得 {len(payload)} バイト")

    try:
        items = parse_feed(payload)
    except ET.ParseError:
        items = []
    else:
        print(f"フィードとして解析できました: {len(items)} 件")
        for item in items[:5]:
            print(f"  + [{item.published or '日付なし'}] {item.title}")
            print(f"    {item.url}")
        return 0 if items else 1

    parser = _LinkList()
    parser.feed(_decode(payload))
    print("フィードとしては解析できませんでした。HTMLとして見ます。")
    feeds = [urllib.parse.urljoin(url, href) for href in parser.feeds]
    candidates = [
        urllib.parse.urljoin(url, href)
        for href, *_ in parser.links
        if re.search(r"\.(xml|rdf|rss)$", href or "", re.I)
    ]
    for label, urls in (("告知されたフィード", feeds), ("フィードらしいリンク", candidates)):
        print(f"{label}: {len(urls)} 件")
        for found in dict.fromkeys(urls):
            print(f"  - {found}")
    print(f"ページ内のリンク {len(parser.links)} 件。先頭10件（link_pattern を決める材料）:")
    for href, title, *_ in parser.links[:10]:
        print(f"  - {urllib.parse.urljoin(url, href)}  「{title[:40]}」")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="フィードから記事を集めて articles/ に書き出す")
    parser.add_argument("--sources", default="sources")
    parser.add_argument("--articles", default="articles")
    parser.add_argument("--only", default="", help="このIDのソースだけ取得する")
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに件数だけ見る")
    parser.add_argument("--show-all", action="store_true", help="取れたアイテムを全件表示する")
    parser.add_argument("--probe", default="", help="収集せず、このURLが使えるかだけ確認する")
    args = parser.parse_args(argv)

    if args.probe:
        return probe(args.probe)

    articles_dir = Path(args.articles)
    articles_dir.mkdir(parents=True, exist_ok=True)
    sources = [s for s in load_sources(args.sources) if s.enabled]
    if args.only:
        sources = [s for s in sources if s.id == args.only]
    if not sources:
        print(f"{args.sources} に有効なソースがありません。")
        return 0

    seen = existing_doc_ids(articles_dir)
    print(f"既存の記事 {len(seen)} 件 / ソース {len(sources)} 件"
          + ("（dry-run）" if args.dry_run else ""))

    seen_titles: set[str] = set()
    results = [
        collect_source(s, articles_dir, seen, seen_titles, args.dry_run, args.show_all)
        for s in sources
    ]

    print()
    print("| ソース | 取得 | 新規 | 既出 | 対象外 | 状態 |")
    print("|---|---:|---:|---:|---:|---|")
    for r in results:
        status = f"⚠️ {r.error}" if r.error else "ok"
        print(f"| {r.source.name} | {r.fetched} | {r.written} | {r.skipped} "
              f"| {r.filtered} | {status} |")
    print()
    for r in results:
        if r.dates:
            # 日付の範囲を出す。全件が同じ日付になっていたら日付の拾い方が壊れている合図
            known = sorted(d for d in r.dates if d)
            span = f"{known[0]} 〜 {known[-1]}" if known else "なし"
            missing = sum(1 for d in r.dates if not d)
            print(f"  [{r.source.id}] 日付 {span}"
                  + (f" / 日付なし {missing} 件" if missing else ""))
        for title in r.samples:
            print(f"  + [{r.source.id}] {title}")

    failed = [r for r in results if r.error]
    total_new = sum(r.written for r in results)
    print(f"\n新規 {total_new} 件 / 失敗 {len(failed)} ソース")
    # 全ソースが失敗したときだけ異常終了する。1本の不調で毎朝赤くしても読まなくなる
    return 1 if failed and len(failed) == len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
