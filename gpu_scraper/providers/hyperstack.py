"""Hyperstack — static price table (API requires auth).

Price semantics
---------------
Hyperstack lists on-demand per-GPU hourly rates.
``price_unit = "per_gpu"``

Hyperstack (formerly Acorn Labs) is a Canadian GPU cloud with locations
in North America and Europe. Their API requires authentication; we use a
curated static table updated 2025-Q2.

Source: https://www.hyperstack.cloud/gpu-pricing
"""
from __future__ import annotations

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_STATIC: list[dict] = [
    # H100 SXM
    {"gpu": "H100 SXM5 80GB", "price": 2.79,  "vram": 80, "region": "CANADA-1"},
    {"gpu": "H100 SXM5 80GB", "price": 2.79,  "vram": 80, "region": "NORWAY-1"},
    # A100
    {"gpu": "A100 SXM4 80GB", "price": 1.99,  "vram": 80, "region": "CANADA-1"},
    {"gpu": "A100 PCIe 80GB", "price": 1.79,  "vram": 80, "region": "CANADA-1"},
    # RTX A6000
    {"gpu": "RTX A6000",      "price": 0.85,  "vram": 48, "region": "CANADA-1"},
    # L40
    {"gpu": "L40",            "price": 1.15,  "vram": 48, "region": "CANADA-1"},
    # V100
    {"gpu": "V100 16GB",      "price": 0.55,  "vram": 16, "region": "CANADA-1"},
]


class HyperstackProvider(BaseProvider):
    name = "Hyperstack"

    async def _scrape(self) -> list[GPUOffer]:
        return [
            GPUOffer(
                provider=self.name,
                gpu_model=normalize_gpu_name(r["gpu"]),
                vram_gb=r.get("vram") or lookup_vram(normalize_gpu_name(r["gpu"])),
                price_per_hour=r["price"],
                price_unit="per_gpu",
                region=r["region"],
                availability="on_demand",
                available=True,
                raw_gpu_name=r["gpu"],
            )
            for r in _STATIC
        ]
