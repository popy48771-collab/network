"""形態素解析・辞書マッチ・共起の単位づくり。

方針:
  1. 先に辞書エンティティ（政策名・施設名など）を表層一致で拾い、その範囲を
     マスクしてから形態素解析する。SudachiPy は Mode C でも「文化芸術基本法」を
     文化/芸術/基本法 に割ってしまうため、この順序でないと政策名が壊れる。
  2. 残りの名詞の連続は、コーパス全体での出現回数を見て複合語に結合する
     （2回以上出る連続だけを結合。1回きりの偶発的な連続は結合しない）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .config import Config
from .ingest import Doc, normalize_text

# 名詞の連続を複合語にまとめるときに巻き込む品詞
_NOUN_MAIN = {"名詞"}
_NOUN_SUB_OK = {"普通名詞", "固有名詞"}
_SUFFIX_OK = ("接尾辞", "名詞的")
_PREFIX = "接頭辞"

_SENT_SPLIT = re.compile(r"(?<=[。！？!?])\s*|\n+")
_HAS_CONTENT = re.compile(r"[ぁ-んァ-ヶ一-龥a-zA-Z]")
_ONLY_KANA_OR_ASCII_SHORT = re.compile(r"^[ぁ-んァ-ヶa-zA-Z]{1,2}$")


@dataclass
class Entity:
    id: str
    label: str
    etype: str
    aliases: list[str] = field(default_factory=list)
    meta: dict = field(default_factory=dict)


@dataclass
class NodeMeta:
    node_id: str
    label: str
    kind: str  # term | policy | law | program | facility | org | region | person


def load_entities(dict_dir: Path | str) -> list[Entity]:
    """dict/*.yaml から政策・施設などのエンティティ定義を読む。"""
    dict_dir = Path(dict_dir)
    entities: list[Entity] = []
    if not dict_dir.exists():
        return entities
    for path in sorted(dict_dir.glob("*.yaml")):
        data = yaml.safe_load(path.read_text(encoding="utf-8")) or []
        if not isinstance(data, list):
            continue
        for item in data:
            if not isinstance(item, dict) or "id" not in item:
                continue
            label = str(item.get("label") or item["id"])
            aliases = [str(a) for a in (item.get("aliases") or [])]
            entities.append(
                Entity(
                    id=str(item["id"]),
                    label=label,
                    etype=str(item.get("etype") or item["id"].split(":", 1)[0]),
                    aliases=sorted({label, *aliases}, key=len, reverse=True),
                    meta={k: v for k, v in item.items() if k not in {"id", "label", "etype", "aliases"}},
                )
            )
    return entities


def load_stopwords(path: Path | str) -> set[str]:
    path = Path(path)
    if not path.exists():
        return set()
    words = set()
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            words.add(line)
    return words


def split_sentences(text: str) -> list[str]:
    """句点・改行で文に割る。極端に短い断片は捨てる。"""
    out = []
    for chunk in _SENT_SPLIT.split(text):
        if chunk is None:
            continue
        chunk = chunk.strip()
        if len(chunk) >= 4 and _HAS_CONTENT.search(chunk):
            out.append(chunk)
    return out


class Analyzer:
    """文 → ノード集合（語 + エンティティ）への変換器。"""

    def __init__(self, entities: list[Entity], stopwords: set[str], cfg: Config) -> None:
        from sudachipy import dictionary, tokenizer

        self._tok = dictionary.Dictionary(dict="core").create()
        self._mode = tokenizer.Tokenizer.SplitMode.C
        self.cfg = cfg
        self.stopwords = stopwords
        self.entities = entities
        # 長いエイリアスから順に当てる（「文化芸術基本法」を「基本法」より先に）
        self._alias_index: list[tuple[str, Entity]] = sorted(
            ((normalize_text(alias), ent) for ent in entities for alias in ent.aliases),
            key=lambda pair: len(pair[0]),
            reverse=True,
        )
        self.node_meta: dict[str, NodeMeta] = {}

    # ---- エンティティ ----------------------------------------------------

    def find_entities(self, sentence: str) -> tuple[list[str], str]:
        """文中のエンティティを拾い、その範囲を空白に置換した文を返す。"""
        found: list[str] = []
        masked = sentence
        for alias, ent in self._alias_index:
            if not alias or alias not in masked:
                continue
            masked = masked.replace(alias, " " * len(alias))
            if ent.id not in found:
                found.append(ent.id)
                self.node_meta.setdefault(ent.id, NodeMeta(ent.id, ent.label, ent.etype))
        return found, masked

    # ---- 形態素 ----------------------------------------------------------

    def _tokenize(self, text: str) -> list[tuple[str, tuple, str]]:
        # SudachiPy は入力長に上限があるので念のため分割して渡す
        out = []
        for i in range(0, len(text), 2000):
            for m in self._tok.tokenize(text[i : i + 2000], self._mode):
                out.append((m.surface(), m.part_of_speech(), m.normalized_form()))
        return out

    def _is_noun_run_member(self, pos: tuple) -> bool:
        if pos[0] in _NOUN_MAIN and pos[1] in _NOUN_SUB_OK:
            return True
        return pos[0] == _SUFFIX_OK[0] and pos[1] == _SUFFIX_OK[1]

    def _keep_single(self, surface: str, pos: tuple, lemma: str) -> bool:
        if pos[0] not in self.cfg.keep_pos:
            return False
        if pos[0] == "名詞" and pos[1] not in _NOUN_SUB_OK:
            return False  # 数詞・代名詞・非自立は落とす
        if pos[0] == "動詞" and pos[1] == "非自立可能":
            return False
        if len(lemma) < self.cfg.min_term_len:
            return False
        if _ONLY_KANA_OR_ASCII_SHORT.match(lemma):
            return False
        if lemma in self.stopwords or surface in self.stopwords:
            return False
        return bool(_HAS_CONTENT.search(lemma))

    def analyze_sentence(self, sentence: str) -> tuple[list[str], list[list[tuple[str, str]]]]:
        """文を (エンティティID列, 名詞連続のリスト) に分解する。

        名詞連続は [(表層, 正規形), ...] のリスト。複合語にするかは
        コーパス全体の頻度を見てから決めるため、ここでは結合しない。
        """
        ents, masked = self.find_entities(sentence)
        runs: list[list[tuple[str, str]]] = []
        current: list[tuple[str, str]] = []
        for surface, pos, lemma in self._tokenize(masked):
            if self._is_noun_run_member(pos):
                current.append((surface, lemma if pos[0] == "名詞" else surface))
                continue
            if current:
                runs.append(current)
                current = []
            if pos[0] == _PREFIX:
                continue
            if self._keep_single(surface, pos, lemma):
                # 用言は単独の語として扱う（連続結合の対象外）
                runs.append([(surface, lemma)])
        if current:
            runs.append(current)
        return ents, runs

    # ---- 複合語 ----------------------------------------------------------

    def compound_candidates(self, runs: list[list[tuple[str, str]]]):
        """名詞連続から、結合候補となる部分列を列挙する。"""
        max_len = self.cfg.compound_max_len
        for run in runs:
            if len(run) < 2:
                continue
            for size in range(2, min(max_len, len(run)) + 1):
                for start in range(len(run) - size + 1):
                    yield "".join(tok[1] for tok in run[start : start + size])

    def nodes_from_runs(
        self, runs: list[list[tuple[str, str]]], compound_freq: dict[str, int]
    ) -> list[str]:
        """名詞連続を、頻度の高い複合語を優先して語ノードに変換する。"""
        nodes: list[str] = []
        threshold = self.cfg.compound_min_freq
        max_len = self.cfg.compound_max_len
        for run in runs:
            i = 0
            while i < len(run):
                taken = 0
                for size in range(min(max_len, len(run) - i), 1, -1):
                    joined = "".join(tok[1] for tok in run[i : i + size])
                    if compound_freq.get(joined, 0) >= threshold and joined not in self.stopwords:
                        nodes.append(self._term_node(joined))
                        taken = size
                        break
                if taken:
                    i += taken
                    continue
                surface, lemma = run[i]
                if self._keep_single(surface, ("名詞", "普通名詞", "一般"), lemma):
                    nodes.append(self._term_node(lemma))
                i += 1
        return [n for n in nodes if n]

    def _term_node(self, lemma: str) -> str:
        node_id = f"t:{lemma}"
        self.node_meta.setdefault(node_id, NodeMeta(node_id, lemma, "term"))
        return node_id


@dataclass
class Basket:
    """共起を数える単位（既定は1文）。"""

    doc_id: str
    date: str
    nodes: set[str]
    text: str


def build_baskets(docs: list[Doc], analyzer: Analyzer, cfg: Config) -> list[Basket]:
    """記事群を共起単位に分解する。複合語の頻度を見るため全体を2度なめる。"""
    # 1周目: 文に割り、名詞連続と複合語候補の頻度を集める
    staged: list[tuple[Doc, str, list[str], list[list[tuple[str, str]]]]] = []
    compound_freq: dict[str, int] = {}
    for doc in docs:
        units = _split_units(doc, cfg.unit)
        for unit_text in units:
            ents, runs = analyzer.analyze_sentence(unit_text)
            staged.append((doc, unit_text, ents, runs))
            for cand in analyzer.compound_candidates(runs):
                compound_freq[cand] = compound_freq.get(cand, 0) + 1

    # 2周目: 頻度を踏まえて語ノードを確定させる
    baskets: list[Basket] = []
    for doc, unit_text, ents, runs in staged:
        nodes = set(analyzer.nodes_from_runs(runs, compound_freq)) | set(ents)
        if len(nodes) >= 2:
            baskets.append(Basket(doc.doc_id, doc.date, nodes, unit_text))
    return baskets


def _split_units(doc: Doc, unit: str) -> list[str]:
    text = doc.text
    if unit == "doc":
        return [text]
    if unit == "paragraph":
        return [p.strip() for p in text.split("\n\n") if len(p.strip()) >= 4]
    return split_sentences(text)
