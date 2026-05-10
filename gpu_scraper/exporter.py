"""CSV and JSON export with timestamped filenames."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Sequence

from .models import GPUOffer


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def export_json(offers: Sequence[GPUOffer], output_dir: Path = Path(".")) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"gpu_prices_{_timestamp()}.json"
    payload = [o.model_dump(mode="json") for o in offers]
    path.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    return path


def export_csv(offers: Sequence[GPUOffer], output_dir: Path = Path(".")) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"gpu_prices_{_timestamp()}.csv"

    if not offers:
        path.write_text("", encoding="utf-8")
        return path

    fieldnames = list(GPUOffer.model_fields.keys())
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fieldnames)
        writer.writeheader()
        for offer in offers:
            row = offer.model_dump(mode="json")
            writer.writerow(row)

    return path
