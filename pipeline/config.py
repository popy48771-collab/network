"""config.yaml の読み込み。値が無ければ既定値を使う。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class Config:
    unit: str = "sentence"
    min_df: int = 3
    min_cooc: int = 2
    min_npmi: float = 0.20
    max_edges: int = 1200
    max_nodes: int = 600
    compound_min_freq: int = 2
    compound_max_len: int = 4
    min_term_len: int = 2
    keep_pos: list[str] = field(default_factory=lambda: ["名詞", "動詞", "形容詞"])
    layout_iterations: int = 200
    betweenness_max_nodes: int = 1200
    trend_enabled: bool = True
    trend_recent_days: int = 7

    @classmethod
    def load(cls, path: Path | str | None) -> "Config":
        if path is None:
            return cls()
        p = Path(path)
        if not p.exists():
            return cls()
        raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
        trend = raw.pop("trend", None) or {}
        cfg = cls()
        for key, value in raw.items():
            if hasattr(cfg, key):
                setattr(cfg, key, value)
        if "enabled" in trend:
            cfg.trend_enabled = bool(trend["enabled"])
        if "recent_days" in trend:
            cfg.trend_recent_days = int(trend["recent_days"])
        return cfg
