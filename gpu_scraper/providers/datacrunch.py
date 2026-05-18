"""DataCrunch — public instance-types REST API, no auth required.

Price semantics
---------------
DataCrunch returns ``price_per_hour`` (on-demand) and ``spot_price``
(interruptible) per GPU per hour already — no division needed.
``price_unit = "per_gpu"``

API endpoint: https://api.datacrunch.io/v1/instance-types
"""
from __future__ import annotations

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_API_URL = "https://api.datacrunch.io/v1/instance-types"


class DataCrunchProvider(BaseProvider):
    name = "DataCrunch"

    async def _scrape(self) -> list[GPUOffer]:
        client = await self._get_client()
        resp = await client.get(_API_URL)
        resp.raise_for_status()

        # API returns either a list directly or {"data": [...]}
        body = resp.json()
        instances: list[dict] = body if isinstance(body, list) else body.get("data", [])
        offers: list[GPUOffer] = []

        for inst in instances:
            gpu_info = inst.get("gpu", {}) or {}
            raw_name: str = (
                gpu_info.get("description")
                or gpu_info.get("name")
                or inst.get("gpu_model", "")
                or inst.get("name", "")
            )
            if not raw_name:
                continue

            canonical = normalize_gpu_name(raw_name)
            gpu_count: int = int(gpu_info.get("count") or inst.get("gpu_count") or 1)
            vram_mb: int = int(gpu_info.get("memory_in_gigabytes", 0) or 0)
            vram_gb: int = vram_mb if vram_mb > 0 else lookup_vram(canonical)

            # On-demand price
            od_price = inst.get("price_per_hour") or inst.get("on_demand_price")
            if od_price:
                try:
                    price = float(od_price)
                except (TypeError, ValueError):
                    price = 0.0
                if price > 0:
                    offers.append(GPUOffer(
                        provider=self.name,
                        gpu_model=canonical,
                        vram_gb=vram_gb,
                        price_per_hour=price,
                        price_unit="per_gpu",  # DataCrunch API is per-GPU-hour
                        region="EU",           # DataCrunch is Finland-based
                        availability="on_demand",
                        available=True,
                        gpu_count=gpu_count,
                        instance_type=inst.get("instance_type") or inst.get("id", ""),
                        raw_gpu_name=raw_name,
                    ))

            # Spot / interruptible price
            spot_price = inst.get("spot_price") or inst.get("interruptible_price")
            if spot_price:
                try:
                    sprice = float(spot_price)
                except (TypeError, ValueError):
                    sprice = 0.0
                if sprice > 0:
                    offers.append(GPUOffer(
                        provider=self.name,
                        gpu_model=canonical,
                        vram_gb=vram_gb,
                        price_per_hour=sprice,
                        price_unit="per_gpu",
                        region="EU",
                        availability="spot",
                        available=True,
                        gpu_count=gpu_count,
                        instance_type=inst.get("instance_type") or inst.get("id", ""),
                        raw_gpu_name=raw_name,
                    ))

        return offers
