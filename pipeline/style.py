"""ノード種別の見せ方。色は2色 + 中立グレーに絞り、残りは形で区別する。

配色の根拠:
  - 語ノードは全体の大半を占める「地」なので中立グレー（#898781）に置く。
    ここに色を使うと、本当に見たい施設・政策が埋もれる。
  - 色を持つのは施設（青）と政策・制度（橙）の2種類だけ。この3色の組み合わせは
    全ペアで CVD 分離 ΔE 9.8 以上・通常視 17.6 以上・対サーフェス3:1 以上を
    light/dark 両モードで満たす（dataviz の validate_palette.js で検証済み）。
  - グレーは「彩度が低い」検査に引っかかるが、これは意図した中立色であって
    識別を担う色ではない。識別は形（●/■/◆/▲）が担う = 色だけに頼らない。
"""

from __future__ import annotations

# etype → 表示グループ
KIND_GROUP = {
    "term": "term",
    "facility": "facility",
    "policy": "policy",
    "law": "policy",
    "program": "policy",
    "org": "other",
    "region": "other",
    "person": "other",
}

GROUP_STYLE = {
    "term": {
        "label": "語",
        "shape": "circle",
        "light": "#898781",
        "dark": "#898781",
        "rgb": (137, 135, 129),
    },
    "facility": {
        "label": "文化施設",
        "shape": "square",
        "light": "#2a78d6",
        "dark": "#3987e5",
        "rgb": (42, 120, 214),
    },
    "policy": {
        "label": "政策・制度・法令",
        "shape": "diamond",
        "light": "#eb6834",
        "dark": "#d95926",
        "rgb": (235, 104, 52),
    },
    "other": {
        "label": "組織・地域・人物",
        "shape": "triangle",
        "light": "#898781",
        "dark": "#898781",
        "rgb": (137, 135, 129),
    },
}


def group_of(kind: str) -> str:
    return KIND_GROUP.get(kind, "term")
