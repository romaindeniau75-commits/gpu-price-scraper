"""Lambda Labs — REST API (requires LAMBDA_API_KEY env var).

Price semantics
---------------
Lambda's ``price_cents_per_hour`` covers the entire instance (all GPUs).
``price_unit = "per_node"``; ``price_per_gpu_hour`` is auto-computed by
GPUOffer as ``price_per_hour / gpu_count``.

GPU count is derived from the instance name pattern ``gpu_Nx_*``
(e.g. ``gpu_8x_h100_sxm5`` → 8 GPUs) or from the ``gpus`` spec field.
"""
from __future__ import annotations

import os
import re
from typing import Any

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_BASE = "https://cloud.lambdalabs.com/api/v1"

_GPU_COUNT_RE = re.compile(r"gpu_(\d+)x_")


def _parse_gpu_count(instance_name: str, specs: dict[str, Any]) -> int:
    """Infer GPU count from instance name or spec field."""
    # Prefer explicit spec: specs.get("gpus") is a list like [{"count": 8, "name": "H100"}]
    gpus_spec = specs.get("gpus")
    if gpus_spec and isinstance(gpus_spec, list) and gpus_spec:
        try:
            return int(gpus_spec[0].get("count", 1))
        except (TypeError, ValueError):
            pass
    # Fall back to parsing the name: gpu_8x_h100_sxm5 → 8
    m = _GPU_COUNT_RE.match(instance_name)
    return int(m.group(1)) if m else 1


class LambdaLabsProvider(BaseProvider):
    name = "Lambda Labs"

    def __init__(self, api_key: str | None = None) -> None:
        super().__init__(api_key or os.getenv("LAMBDA_API_KEY"))

    async def _scrape(self) -> list[GPUOffer]:
        if not self.api_key:
            raise RuntimeError("LAMBDA_API_KEY not set — skipping Lambda Labs")

        client = await self._get_client()
        resp = await client.get(
            f"{_BASE}/instance-types",
            auth=(self.api_key, ""),
        )
        resp.raise_for_status()

        data: dict[str, Any] = resp.json().get("data", {})
        offers: list[GPUOffer] = []

        for name, info in data.items():
            specs = info.get("instance_type", {})
            gpu_desc = specs.get("gpu_description", name)
            canonical = normalize_gpu_name(gpu_desc)
            price_cents: int = specs.get("price_cents_per_hour", 0)
            price = price_cents / 100
            gpu_count = _parse_gpu_count(name, specs)

            regions = info.get("regions_with_capacity_available", [])
            available = bool(regions)
            region_name = regions[0]["name"] if regions else "us-east-1"

            offers.append(GPUOffer(
                provider=self.name,
                gpu_model=canonical,
                vram_gb=lookup_vram(canonical),
                price_per_hour=price,      # raw per-instance price
                price_unit="per_node",     # price_per_gpu_hour auto-computed
                region=region_name,
                availability="on_demand",
                availability=available,
                gpu_count=gpu_count,
                instance_type=name,
                raw_gpu_name=gpu_desc,
            ))

        return offers
