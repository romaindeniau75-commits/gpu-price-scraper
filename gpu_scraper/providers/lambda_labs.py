"""Lambda Labs — REST API (requires LAMBDA_API_KEY env var)."""
from __future__ import annotations

import os
from typing import Any

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_BASE = "https://cloud.lambdalabs.com/api/v1"


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

            regions = info.get("regions_with_capacity_available", [])
            available = bool(regions)
            region_name = regions[0]["name"] if regions else "us-east-1"

            offers.append(GPUOffer(
                provider=self.name,
                gpu_model=canonical,
                vram_gb=lookup_vram(canonical),
                price_per_hour=price,
                region=region_name,
                contract_type="on-demand",
                availability=available,
                instance_type=name,
                raw_gpu_name=gpu_desc,
            ))

        return offers
