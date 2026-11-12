"""Runcrate (an AI cloud): public pricing feed, no auth required.

Price semantics
---------------
The feed returns one entry per GPU type with ``min_price_hr``, the live
on-demand price per GPU per hour. ``price_unit = "per_gpu"``.

The feed carries one price per GPU type (not per region), so offers use the
``Global`` region bucket.

Skipped on purpose
------------------
* Entries with ``available_options < 1``: the price is stale for types that
  are out of stock.
* Types the feed does not describe unambiguously (H100 and A100 do not say
  SXM vs PCIe, V100 comes in two memory sizes). They are not in ``_GPU_MODELS``.

API endpoint: https://www.runcrate.ai/api/gpu-pricing
"""
from __future__ import annotations

from ..models import GPUOffer
from ..normalizer import lookup_vram
from .base import BaseProvider

_API_URL = "https://www.runcrate.ai/api/gpu-pricing"

# Feed ``gpu_type`` -> canonical model name. Only unambiguous types are mapped.
_GPU_MODELS: dict[str, str] = {
    "B300": "B300",
    "B200": "B200",
    "H200": "H200 SXM",
    "H200_nvl": "H200 NVL",
    "L40S": "L40S",
    "L40": "L40",
    "RTX5090": "RTX 5090",
    "RTX4090": "RTX 4090",
    "RTX3090": "RTX 3090",
    "RTX6000Ada": "RTX 6000 Ada",
    "RTXPro6000": "RTX PRO 6000",
    "A6000": "A6000",
    "A40": "A40",
    "L4": "L4",
    "A10": "A10",
}


def _parse_vram_gb(raw: object) -> int:
    """Parse a feed VRAM string such as ``"141GB"``; 0 when absent."""
    digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return int(digits) if digits else 0


class RuncrateProvider(BaseProvider):
    name = "Runcrate"

    async def _scrape(self) -> list[GPUOffer]:
        client = await self._get_client()
        resp = await client.get(_API_URL)
        resp.raise_for_status()

        offers: list[GPUOffer] = []
        for gpu in resp.json().get("gpus", []):
            canonical = _GPU_MODELS.get(gpu.get("gpu_type", ""))
            if canonical is None:
                continue
            if int(gpu.get("available_options") or 0) < 1:
                continue

            try:
                price = float(gpu.get("min_price_hr") or 0.0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue

            offers.append(GPUOffer(
                provider=self.name,
                gpu_model=canonical,
                vram_gb=_parse_vram_gb(gpu.get("vram")) or lookup_vram(canonical),
                price_per_hour=price,
                price_unit="per_gpu",  # min_price_hr is $/GPU/hr
                region="Global",
                availability="on_demand",
                available=True,
                gpu_count=1,
                instance_type=gpu.get("gpu_type"),
                raw_gpu_name=gpu.get("name"),
            ))

        return offers
