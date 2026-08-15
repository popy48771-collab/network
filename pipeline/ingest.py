"""articles/ に置かれたファイルを読み込んで記事レコードに正規化する。

置き方は articles/README.md を参照。対応形式:
  .txt .md   YAML フロントマター（任意）+ 本文
  .html      <title> と本文テキストを抽出
  .json      オブジェクト、またはオブジェクトの配列
  .jsonl     1行1オブジェクト
  .csv .tsv  ヘッダ行から title/body/url/date/source を拾う
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import date as Date
from html.parser import HTMLParser
from pathlib import Path

import yaml

TEXT_EXT = {".txt", ".md", ".markdown"}
HTML_EXT = {".html", ".htm"}
JSON_EXT = {".json"}
JSONL_EXT = {".jsonl", ".ndjson"}
CSV_EXT = {".csv", ".tsv"}
SUPPORTED = TEXT_EXT | HTML_EXT | JSON_EXT | JSONL_EXT | CSV_EXT

# 列名・キー名のゆれ吸収
ALIASES = {
    "title": ("title", "見出し", "タイトル", "headline", "subject"),
    "body": ("body", "text", "content", "本文", "記事", "article"),
    "url": ("url", "link", "リンク", "出典url", "source_url"),
    "date": ("date", "published_at", "published", "日付", "発表日", "掲載日"),
    "source": ("source", "媒体", "出典", "発表元", "publisher", "site"),
    "tags": ("tags", "tag", "タグ", "分類"),
}

_DATE_PATTERNS = (
    (re.compile(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})"), (1, 2, 3)),
    (re.compile(r"(\d{4})年\s*(\d{1,2})月\s*(\d{1,2})日"), (1, 2, 3)),
    (re.compile(r"(\d{4})(\d{2})(\d{2})"), (1, 2, 3)),
)


@dataclass
class Doc:
    doc_id: str
    title: str
    body: str
    url: str = ""
    date: str = ""  # ISO 8601 (YYYY-MM-DD) / 不明なら空
    source: str = ""
    path: str = ""
    tags: list[str] = field(default_factory=list)
    # primary=官公庁の公表資料（本文あり） / secondary=報道（見出しのみ） / 空=人が置いた
    tier: str = ""

    @property
    def text(self) -> str:
        """解析対象テキスト。見出しは本文より情報密度が高いので必ず含める。"""
        if self.title and not self.body.startswith(self.title):
            return f"{self.title}。\n{self.body}"
        return self.body


class _TextExtractor(HTMLParser):
    """依存を増やさないための最小 HTML → テキスト変換。"""

    _SKIP = {"script", "style", "noscript", "svg", "head"}
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title = ""
        self._skip_depth = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip_depth += 1
        if tag == "title":
            self._in_title = True
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP and self._skip_depth:
            self._skip_depth -= 1
        if tag == "title":
            self._in_title = False
        if tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self._in_title:
            self.title += data.strip()
            return
        if self._skip_depth:
            return
        self.parts.append(data)

    def text(self) -> str:
        return re.sub(r"\n{3,}", "\n\n", "".join(self.parts)).strip()


def _pick(mapping: dict, field_name: str) -> str:
    """辞書から別名込みでキーを引く。"""
    lowered = {str(k).strip().lower(): v for k, v in mapping.items()}
    for alias in ALIASES[field_name]:
        if alias in lowered and lowered[alias] not in (None, ""):
            return str(lowered[alias]).strip()
    return ""


def normalize_date(value: str) -> str:
    """いろいろな日付表記を ISO 8601 に寄せる。読めなければ空文字。"""
    if not value:
        return ""
    for pattern, (y, m, d) in _DATE_PATTERNS:
        hit = pattern.search(value)
        if hit:
            try:
                return Date(int(hit.group(y)), int(hit.group(m)), int(hit.group(d))).isoformat()
            except ValueError:
                continue
    return ""


def normalize_text(text: str) -> str:
    """全角英数の統一と空白の整理。NFKC は半角カナも直してくれる。"""
    text = unicodedata.normalize("NFKC", text)
    text = text.replace("　", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _split_front_matter(raw: str) -> tuple[dict, str]:
    if not raw.startswith("---"):
        return {}, raw
    parts = raw.split("\n---", 2)
    if len(parts) < 2:
        return {}, raw
    head = parts[0][3:]
    body = parts[1].lstrip("\n") if len(parts) == 2 else parts[1].lstrip("\n")
    try:
        meta = yaml.safe_load(head) or {}
    except yaml.YAMLError:
        return {}, raw
    if not isinstance(meta, dict):
        return {}, raw
    return meta, body


def _make_id(*parts: str) -> str:
    return hashlib.sha256(" ".join(parts).encode("utf-8")).hexdigest()[:16]


def _from_mapping(mapping: dict, path: Path, index: int = 0) -> Doc | None:
    body = normalize_text(_pick(mapping, "body"))
    title = normalize_text(_pick(mapping, "title"))
    if not body and not title:
        return None
    url = _pick(mapping, "url")
    tags_raw = _pick(mapping, "tags")
    tags = [t.strip() for t in re.split(r"[,、;]", tags_raw) if t.strip()] if tags_raw else []
    return Doc(
        doc_id=_make_id(url or f"{path}#{index}", title),
        title=title or body[:40],
        body=body,
        url=url,
        date=normalize_date(_pick(mapping, "date")) or normalize_date(path.stem),
        source=_pick(mapping, "source"),
        path=str(path),
        tags=tags,
        tier=str(mapping.get("tier") or "").strip(),
    )


def _read_text_file(path: Path) -> Doc | None:
    raw = path.read_text(encoding="utf-8", errors="replace")
    meta, body = _split_front_matter(raw)
    body = normalize_text(body)
    if not body:
        return None
    title = normalize_text(str(meta.get("title", "")))
    if not title:
        # 先頭の非空行を見出しとみなす（Markdown の # は落とす）
        for line in body.splitlines():
            if line.strip():
                title = line.lstrip("#").strip()
                break
    merged = {**meta, "title": title, "body": body}
    return _from_mapping(merged, path)


def _read_html_file(path: Path) -> Doc | None:
    parser = _TextExtractor()
    parser.feed(path.read_text(encoding="utf-8", errors="replace"))
    body = normalize_text(parser.text())
    if not body:
        return None
    return _from_mapping({"title": parser.title, "body": body}, path)


def _read_json_file(path: Path) -> list[Doc]:
    data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    records = data if isinstance(data, list) else [data]
    docs = []
    for i, rec in enumerate(records):
        if isinstance(rec, dict):
            doc = _from_mapping(rec, path, i)
            if doc:
                docs.append(doc)
    return docs


def _read_jsonl_file(path: Path) -> list[Doc]:
    docs = []
    for i, line in enumerate(path.read_text(encoding="utf-8", errors="replace").splitlines()):
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            doc = _from_mapping(rec, path, i)
            if doc:
                docs.append(doc)
    return docs


def _read_csv_file(path: Path) -> list[Doc]:
    delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    docs = []
    for i, row in enumerate(reader):
        doc = _from_mapping({k: v for k, v in row.items() if k}, path, i)
        if doc:
            docs.append(doc)
    return docs


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED:
            continue
        # README や _ 始まりのファイル・ディレクトリは対象外
        if path.name.lower().startswith("readme"):
            continue
        if any(part.startswith((".", "_")) for part in path.relative_to(root).parts):
            continue
        yield path


def load_articles(root: Path | str) -> list[Doc]:
    """articles/ 以下を再帰的に読み、本文が同一の記事は1件にまとめて返す。"""
    root = Path(root)
    if not root.exists():
        return []

    docs: list[Doc] = []
    for path in _iter_files(root):
        suffix = path.suffix.lower()
        try:
            if suffix in TEXT_EXT:
                doc = _read_text_file(path)
                if doc:
                    docs.append(doc)
            elif suffix in HTML_EXT:
                doc = _read_html_file(path)
                if doc:
                    docs.append(doc)
            elif suffix in JSON_EXT:
                docs.extend(_read_json_file(path))
            elif suffix in JSONL_EXT:
                docs.extend(_read_jsonl_file(path))
            elif suffix in CSV_EXT:
                docs.extend(_read_csv_file(path))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            print(f"  ! 読み込み失敗: {path} ({exc})")

    # 本文が完全一致するものは重複とみなす（同じ記事を別形式で置いた場合など）
    seen: dict[str, Doc] = {}
    for doc in docs:
        key = hashlib.sha256(doc.body.encode("utf-8")).hexdigest()
        if key not in seen:
            seen[key] = doc
    return list(seen.values())
