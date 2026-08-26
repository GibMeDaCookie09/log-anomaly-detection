"""Log template extraction using Drain3.

Raw log lines are unbounded in variety, but most differ only in their variable
parts (IDs, timestamps, addresses). Drain3 collapses lines into templates so
downstream stages work on a few hundred distinct event types instead of
millions of near-identical strings.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from drain3 import TemplateMiner
from drain3.template_miner_config import TemplateMinerConfig


@dataclass
class ParsedLine:
    line_id: int
    raw: str
    template: str
    cluster_id: int
    label: str | None = None

    @property
    def is_anomaly(self) -> bool:
        """BGL convention: '-' means normal, any other label is a fault type."""
        return self.label is not None and self.label != "-"


class LogParser:
    """Wraps Drain3 with the config we actually want and a typed interface."""

    def __init__(self, similarity_threshold: float = 0.4, depth: int = 4):
        config = TemplateMinerConfig()
        config.drain_sim_th = similarity_threshold
        config.drain_depth = depth
        config.profiling_enabled = False
        self._miner = TemplateMiner(config=config)

    def parse_line(self, line_id: int, raw: str, label: str | None = None) -> ParsedLine:
        result = self._miner.add_log_message(raw.strip())
        return ParsedLine(
            line_id=line_id,
            raw=raw.strip(),
            template=result["template_mined"],
            cluster_id=result["cluster_id"],
            label=label,
        )

    def parse(
        self,
        lines: Iterable[str],
        labels: Iterable[str | None] | None = None,
    ) -> list[ParsedLine]:
        label_list = list(labels) if labels is not None else []
        out: list[ParsedLine] = []
        for i, raw in enumerate(lines):
            label = label_list[i] if i < len(label_list) else None
            out.append(self.parse_line(i, raw, label))
        return out

    @property
    def n_templates(self) -> int:
        return len(self._miner.drain.clusters)

    def templates(self) -> dict[int, str]:
        return {c.cluster_id: c.get_template() for c in self._miner.drain.clusters}
