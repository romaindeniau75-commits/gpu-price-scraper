"""Paperspace (DigitalOcean) — HTML scraper for public pricing page."""
from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_PRICING_URL = "https://www.paperspace.com/gpu-cloud-computing"

_PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*/\s*hr", re.I)

# Static fallback (updated 2025-Q2)
_STATIC: list[dict] = [
    {"gpu": "A100-80G",  "price": 3.18, "vram": 80, "region": "US East"},
    {"gpu": "A100",      "price": 2.30, "vram": 40, "region": "US East"},
    {"gpu": "A6000",     "price": 1.89, "vram": 48, "region": "US East"},
    {"gpu": "V100-32G",  "price": 2.30, "vram": 32, "region": "US East"},
    {"gpu": "V100",      "price": 1.50, "vram": 16, "region": "US East"},
    {"gpu": "RTX4000",   "price": 0.51, "vram": 8,  "region": "US East"},
    {"gpu": "P5000",     "price": 0.78, "vram": 16, "region": "US East"},
    {"gpu": "P4000",     "price": 0.51, "vram": 8,  "region": "US East"},
]


class PaperspaceProvider(BaseProvider):
    name = "Paperspace"

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

        for card in tree.css("[class*='machine'], [class*='gpu'], tr"):
            text = card.text(strip=True)
            m = _PRICE_RE.search(text)
            if not m:
                continue
            price = float(m.group(1))

            # Look for a GPU label in nearby elements
            heading = card.css_first("h2, h3, h4, [class*='name'], [class*='title'], td")
            raw_name = heading.text(strip=True) if heading else ""
            if not raw_name:
                continue

            canonical = normalize_gpu_name(raw_name)
            offers.append(GPUOffer(
                provider=self.name,
                gpu_model=canonical,
                vram_gb=lookup_vram(canonical),
                price_per_hour=price,
                price_unit="per_gpu",  # Paperspace lists /GPU/hr
                region="US East",
                contract_type="on-demand",
                availability=True,
                raw_gpu_name=raw_name,
            ))

        if not offers:
            raise ValueError("no offers parsed")
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
                contract_type="on-demand",
                availability=True,
                raw_gpu_name=r["gpu"],
            )
            for r in _STATIC
        ]
