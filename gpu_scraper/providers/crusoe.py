"""Crusoe Cloud — public pricing page HTML scraper.

Price semantics
---------------
Crusoe lists on-demand per-GPU hourly rates.
``price_unit = "per_gpu"``

Crusoe is a clean-energy GPU cloud headquartered in the US.
"""
from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_PRICING_URL = "https://crusoe.ai/cloud/pricing/"

_PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*(?:/\s*hr|/\s*hour|per\s*hour)", re.I)

# Static fallback — updated 2025-Q2
# Source: https://crusoe.ai/cloud/pricing/
_STATIC: list[dict] = [
    {"gpu": "H100 SXM5 80GB",  "price": 2.42,  "vram": 80, "region": "US"},
    {"gpu": "H100 PCIe 80GB",  "price": 2.14,  "vram": 80, "region": "US"},
    {"gpu": "A100 SXM4 80GB",  "price": 1.79,  "vram": 80, "region": "US"},
    {"gpu": "A100 PCIe 80GB",  "price": 1.59,  "vram": 80, "region": "US"},
    {"gpu": "A100 SXM4 40GB",  "price": 1.39,  "vram": 40, "region": "US"},
    {"gpu": "L40S",            "price": 1.25,  "vram": 48, "region": "US"},
]


class CrusoeProvider(BaseProvider):
    name = "Crusoe"

    async def _scrape(self) -> list[GPUOffer]:
        try:
            return await self._scrape_live()
        except Exception:  # noqa: BLE001
            return self._static_fallback()

    async def _scrape_live(self) -> list[GPUOffer]:
        client = await self._get_client()
        resp = await client.get(_PRICING_URL)
        resp.raise_for_status()

        tree = HTMLParser(resp.text)
        offers: list[GPUOffer] = []

        # Crusoe pricing page uses table rows and/or card-style layouts
        for row in tree.css("tr, [class*='pricing'], [class*='gpu-row']"):
            text = row.text(strip=True)
            m = _PRICE_RE.search(text)
            if not m:
                continue
            price = float(m.group(1))
            if price <= 0:
                continue

            # Best-effort name extraction from first cell / heading
            name_node = row.css_first("td, th, [class*='name'], [class*='gpu']")
            raw_name = name_node.text(strip=True) if name_node else ""
            if not raw_name:
                continue

            canonical = normalize_gpu_name(raw_name)
            offers.append(GPUOffer(
                provider=self.name,
                gpu_model=canonical,
                vram_gb=lookup_vram(canonical),
                price_per_hour=price,
                price_unit="per_gpu",
                region="US",
                availability="on_demand",
                available=True,
                raw_gpu_name=raw_name,
            ))

        if not offers:
            raise ValueError("no pricing rows found on live Crusoe page")
        return offers

    def _static_fallback(self) -> list[GPUOffer]:
        return [
            GPUOffer(
                provider=self.name,
                gpu_model=normalize_gpu_name(r["gpu"]),
                vram_gb=r["vram"],
                price_per_hour=r["price"],
                price_unit="per_gpu",
                region=r["region"],
                availability="on_demand",
                available=True,
                raw_gpu_name=r["gpu"],
            )
            for r in _STATIC
        ]
