"""Cost ledger: append-only JSONL log of every AI API call."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.utils.paths import DEFAULT_DIRS, safe_filename


@dataclass
class CostRecord:
    sku: str
    platform: str
    task: str
    provider: str
    model: str
    request_id: str | None
    timestamp: str
    attempt: int
    input_images: list[str]
    requested_size: str | None
    aspect_ratio: str | None
    estimated_cost_usd: float
    actual_cost_usd: float | None
    status: str
    accepted: bool
    error: str | None
    duration_seconds: float


class CostLedger:
    def __init__(self, logs_dir: Path | None = None) -> None:
        self.logs_dir = logs_dir or DEFAULT_DIRS["logs"]
        self.ledger_path = self.logs_dir / "cost-ledger.jsonl"
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    def append(self, record: CostRecord) -> Path:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.ledger_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")
        return self.ledger_path

    def record_call(
        self,
        *,
        sku: str,
        platform: str,
        task: str,
        provider: str,
        model: str,
        request_id: str | None,
        attempt: int,
        input_images: list[str],
        requested_size: str | None,
        aspect_ratio: str | None,
        estimated_cost_usd: float,
        actual_cost_usd: float | None,
        status: str,
        accepted: bool,
        error: str | None,
        duration_seconds: float,
    ) -> CostRecord:
        record = CostRecord(
            sku=safe_filename(sku),
            platform=safe_filename(platform),
            task=safe_filename(task),
            provider=provider,
            model=model,
            request_id=request_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            attempt=attempt,
            input_images=[str(p) for p in input_images],
            requested_size=requested_size,
            aspect_ratio=aspect_ratio,
            estimated_cost_usd=estimated_cost_usd,
            actual_cost_usd=actual_cost_usd,
            status=status,
            accepted=accepted,
            error=error,
            duration_seconds=duration_seconds,
        )
        self.append(record)
        return record

    def read_all(self) -> list[CostRecord]:
        if not self.ledger_path.exists():
            return []
        records = []
        with open(self.ledger_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                records.append(CostRecord(**json.loads(line)))
        return records
