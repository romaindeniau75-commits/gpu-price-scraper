"""Nebius AI Cloud — static price table (API requires auth).

Price semantics
---------------
Nebius lists on-demand per-GPU hourly rates.
``price_unit = "per_gpu"``

Nebius (formerly Yandex Cloud's AI division) operates GPU clusters in
the EU (Amsterdam) and US (Seattle). API requires authentication so we
use a curated static table updated 2025-Q2.

Source: https://nebius.com/prices
"""
from __future__ import annotations

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_STATIC: list[dict] = [
    # H100 SXM — flagship offering
    {"gpu": "H100 SXM 80GB",  "price": 3.04,  "vram": 80, "region": "eu-north1"},
    {"gpu": "H100 SXM 80GB",  "price": 3.04,  "vram": 80, "region": "us-west1"},
    # A100
    {"gpu": "A100 80GB",      "price": 2.21,  "vram": 80, "region": "eu-north1"},
    {"gpu": "A100 40GB",      "price": 1.74,  "vram": 40, "region": "eu-north1"},
    # L40S
    {"gpu": "L40S",           "price": 1.41,  "vram": 48, "region": "eu-north1"},
    # T4
    {"gpu": "T4",             "price": 0.35,  "vram": 16, "region": "eu-north1"},
]


class NebiusProvider(BaseProvider):
    name = "Nebius"

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
