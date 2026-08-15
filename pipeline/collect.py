"""sources/*.yaml を読んでフィードを取得し、articles/ に記事ファイルを書き出す。

    uv run python -m pipeline.collect              # 全ソースを取得
    uv run python -m pipeline.collect --dry-run    # 書き込まずに件数だけ見る
    uv run python -m pipeline.collect --only ga_bunkacho

冪等: 同じ記事（正規化URLが同じ）は何度走らせても1ファイルにしかならない。

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
from pathlib import Path

import yaml

from .ingest import _TextExtractor, normalize_date, normalize_text

USER_AGENT = (
    "culture-policy-network/0.1 (+https://github.com/popy48771-collab/network; "
    "research use; contact via GitHub issues)"
)
TIMEOUT = 30
NS = {"atom": "http://www.w3.org/2005/Atom", "dc": "http://purl.org/dc/elements/1.1/"}

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
    error: str = ""
    samples: list[str] = field(default_factory=list)


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

        # Google ニュースは見出し末尾に「 - 媒体名」を付ける。媒体名は source に寄せる
        hit = _TRAILING_SOURCE.search(title)
        if hit and not publisher:
            publisher = hit.group(1).strip()
        if hit:
            title = title[: hit.start()].strip()

        # 要約が見出しの繰り返しでしかないことがある（Google ニュース）。その場合は捨てる
        if summary and (summary == title or summary.startswith(title[:20])):
            summary = ""

        items.append(
            Item(title=title, url=canonical_url(url), summary=summary,
                 published=published, publisher=publisher)
        )
    return items


# ---- 本文取得（tier: primary のみ） ---------------------------------------


def _should_fetch_body(source: Source) -> bool:
    """報道記事の本文は取りに行かない。設定ミスをここで握り潰す。"""
    return source.fetch_body and source.tier == "primary"


def fetch_body(url: str) -> str:
    parser = _TextExtractor()
    parser.feed(fetch(url).decode("utf-8", errors="replace"))
    text = normalize_text(parser.text())
    # ナビゲーションの短い行を落として、本文らしい塊だけ残す
    lines = [line for line in text.splitlines() if len(line.strip()) >= 15]
    return "\n".join(lines)


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


def collect_source(source: Source, articles_dir: Path, seen: set[str], dry_run: bool) -> Result:
    result = Result(source=source)
    if source.kind != "rss":
        result.error = f"kind={source.kind} は未実装（今は rss のみ）"
        return result
    try:
        items = parse_feed(fetch(source.url))[: source.max_items]
    except (RuntimeError, ET.ParseError) as exc:
        result.error = str(exc)[:200]
        return result

    result.fetched = len(items)
    for item in items:
        if item.doc_id in seen:
            result.skipped += 1
            continue
        seen.add(item.doc_id)
        if _should_fetch_body(source):
            try:
                item.body = fetch_body(item.url)
                time.sleep(source.rate_limit_sec)
            except RuntimeError:
                pass  # 本文が取れなくても見出しだけで登録する
        if not dry_run:
            write_article(item, source, articles_dir)
        result.written += 1
        if len(result.samples) < 3:
            result.samples.append(item.title)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="フィードから記事を集めて articles/ に書き出す")
    parser.add_argument("--sources", default="sources")
    parser.add_argument("--articles", default="articles")
    parser.add_argument("--only", default="", help="このIDのソースだけ取得する")
    parser.add_argument("--dry-run", action="store_true", help="書き込まずに件数だけ見る")
    args = parser.parse_args(argv)

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

    results = [collect_source(s, articles_dir, seen, args.dry_run) for s in sources]

    print()
    print("| ソース | 取得 | 新規 | 既出 | 状態 |")
    print("|---|---:|---:|---:|---|")
    for r in results:
        status = f"⚠️ {r.error}" if r.error else "ok"
        print(f"| {r.source.name} | {r.fetched} | {r.written} | {r.skipped} | {status} |")
    print()
    for r in results:
        for title in r.samples:
            print(f"  + [{r.source.id}] {title}")

    failed = [r for r in results if r.error]
    total_new = sum(r.written for r in results)
    print(f"\n新規 {total_new} 件 / 失敗 {len(failed)} ソース")
    # 全ソースが失敗したときだけ異常終了する。1本の不調で毎朝赤くしても読まなくなる
    return 1 if failed and len(failed) == len(results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
