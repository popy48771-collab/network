"""辞書に追加する候補を洗い出す。

    uv run python -m pipeline.review

頻出しているのに dict/*.yaml に登録されていない固有名詞っぽい語を頻度順に出す。
辞書は放っておくと必ず腐るので、記事が溜まったらこれを回して人手で足していく。
出力をそのまま dict/ に貼らないこと（表記ゆれの統合は人間が判断する）。
"""

from __future__ import annotations

import argparse
import re
from collections import Counter
from pathlib import Path

from . import ingest, textproc
from .config import Config

# 施設・組織・制度の語尾。これで終わる語は辞書候補になりやすい
_SUFFIX = re.compile(
    r"(博物館|美術館|劇場|音楽堂|文化会館|図書館|資料館|記念館|会館|ホール|"
    r"法|条例|計画|戦略|制度|事業|基金|助成金|補助金|センター|機構|財団|協会|"
    r"振興会|審議会|委員会|庁|省|部会|遺産|芸術祭|ビエンナーレ|トリエンナーレ)$"
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="辞書の追加候補を出す")
    parser.add_argument("--articles", default="articles")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--dict", dest="dict_dir", default="dict")
    parser.add_argument("--stopwords", default="dict/stopwords.txt")
    parser.add_argument("--top", type=int, default=60)
    parser.add_argument("--min-count", type=int, default=2)
    args = parser.parse_args(argv)

    cfg = Config.load(args.config)
    docs = ingest.load_articles(args.articles)
    if not docs:
        print(f"{args.articles} に記事がありません。")
        return 0

    entities = textproc.load_entities(args.dict_dir)
    known = {alias for ent in entities for alias in ent.aliases}
    analyzer = textproc.Analyzer(entities, textproc.load_stopwords(args.stopwords), cfg)
    baskets = textproc.build_baskets(docs, analyzer, cfg)

    counts: Counter = Counter()
    for basket in baskets:
        for node in basket.nodes:
            if node.startswith("t:"):
                counts[node[2:]] += 1

    strong = [(w, c) for w, c in counts.items() if _SUFFIX.search(w) and w not in known]
    other = [(w, c) for w, c in counts.items() if not _SUFFIX.search(w) and w not in known and len(w) >= 4]

    print(f"記事 {len(docs)} 件 / 語 {len(counts)} 種類 / 既登録 {len(entities)} 件\n")
    _table("辞書候補（施設・制度の語尾を持つ語）", strong, args)
    _table("複合語で頻出している語（4文字以上）", other, args)
    print("追加するときは dict/facilities.yaml か dict/policies.yaml に手で書く。")
    print("id は一度決めたら変えないこと（過去の分析と繋がらなくなる）。")
    return 0


def _table(title: str, rows: list[tuple[str, int]], args) -> None:
    rows = [r for r in rows if r[1] >= args.min_count]
    rows.sort(key=lambda kv: (-kv[1], kv[0]))
    print(f"## {title}  ({len(rows)} 件)")
    if not rows:
        print("  （なし）\n")
        return
    for word, count in rows[: args.top]:
        print(f"  {count:4d}  {word}")
    print()


if __name__ == "__main__":
    raise SystemExit(main())
