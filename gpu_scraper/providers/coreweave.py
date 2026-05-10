"""CoreWeave — HTML scraper for public pricing page."""
from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_PRICING_URL = "https://www.coreweave.com/gpu-cloud-compute"

# Known CoreWeave prices (updated 2025-Q2) as a reliable fallback when
# the live page structure changes. Keys are raw GPU names from their page.
_STATIC_PRICES: list[dict] = [
    {"gpu": "H100 SXM5 80GB",    "price": 2.06,  "vram": 80,  "region": "US"},
    {"gpu": "H100 NVL 94GB",     "price": 2.49,  "vram": 94,  "region": "US"},
    {"gpu": "A100 80GB NVLINK",  "price": 2.21,  "vram": 80,  "region": "US"},
    {"gpu": "A100 80GB PCIe",    "price": 2.06,  "vram": 80,  "region": "US"},
    {"gpu": "A100 40GB NVLINK",  "price": 1.65,  "vram": 40,  "region": "US"},
    {"gpu": "A100 40GB PCIe",    "price": 1.40,  "vram": 40,  "region": "US"},
    {"gpu": "A40",               "price": 0.74,  "vram": 48,  "region": "US"},
    {"gpu": "RTX A6000",         "price": 0.80,  "vram": 48,  "region": "US"},
    {"gpu": "RTX A5000",         "price": 0.65,  "vram": 24,  "region": "US"},
    {"gpu": "V100 FHHL 16GB",    "price": 0.80,  "vram": 16,  "region": "US"},
    {"gpu": "V100 SXM2 16GB",    "price": 0.80,  "vram": 16,  "region": "US"},
]

_PRICE_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)\s*/\s*hr", re.I)


class CoreWeaveProvider(BaseProvider):
    name = "CoreWeave"

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

        # CoreWeave pricing page has pricing cards — locate tables or price rows
        for row in tree.css("tr, [class*='pricing-row'], [class*='price-row']"):
            cells = row.css("td, th")
            if len(cells) < 2:
                continue
            row_text = " ".join(c.text(strip=True) for c in cells)
            m = _PRICE_RE.search(row_text)
            if not m:
                continue
            price = float(m.group(1))

            # Extract GPU name from first cell
            raw_name = cells[0].text(strip=True)
            if not raw_name:
                continue
            canonical = normalize_gpu_name(raw_name)
            vram = lookup_vram(canonical)

            offers.append(GPUOffer(
                provider=self.name,
                gpu_model=canonical,
                vram_gb=vram,
                price_per_hour=price,
                region="US",
                contract_type="on-demand",
                availability=True,
                raw_gpu_name=raw_name,
            ))

        if not offers:
            raise ValueError("no pricing rows found in live page")
        return offers

    def _static_fallback(self) -> list[GPUOffer]:
        return [
            GPUOffer(
                provider=self.name,
                gpu_model=normalize_gpu_name(row["gpu"]),
                vram_gb=row["vram"],
                price_per_hour=row["price"],
                region=row["region"],
                contract_type="on-demand",
                availability=True,
                raw_gpu_name=row["gpu"],
            )
            for row in _STATIC_PRICES
        ]
