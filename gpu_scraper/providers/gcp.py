"""GCP — static price table + HTML scrape of the public GPU pricing page.

The public pricelist JSON endpoint was retired in 2025. We now fall back to
a curated static table (updated 2025-Q2) which is the most reliable approach
for a read-only scraper without a GCP API key.
"""
from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_PRICING_URL = "https://cloud.google.com/compute/gpus-pricing"

# Static prices (per GPU, on-demand, us-central1) — updated 2025-Q2
# Source: https://cloud.google.com/compute/gpus-pricing
_STATIC: list[dict] = [
    # H100 (a3-highgpu family)
    {"gpu": "H100 80GB",        "price": 4.0612, "vram": 80,  "region": "us-central1", "contract": "on-demand"},
    {"gpu": "H100 Mega 80GB",   "price": 5.0765, "vram": 80,  "region": "us-central1", "contract": "on-demand"},
    # A100 (a2 family)
    {"gpu": "A100 80GB",        "price": 3.9482, "vram": 80,  "region": "us-central1", "contract": "on-demand"},
    {"gpu": "A100 40GB",        "price": 2.9331, "vram": 40,  "region": "us-central1", "contract": "on-demand"},
    # Other
    {"gpu": "L4",               "price": 0.7063, "vram": 24,  "region": "us-central1", "contract": "on-demand"},
    {"gpu": "T4",               "price": 0.3533, "vram": 16,  "region": "us-central1", "contract": "on-demand"},
    {"gpu": "V100 16GB",        "price": 2.4800, "vram": 16,  "region": "us-central1", "contract": "on-demand"},
    {"gpu": "P100 16GB",        "price": 1.4600, "vram": 16,  "region": "us-central1", "contract": "on-demand"},
    # 1-year committed use (~37% off on-demand)
    {"gpu": "H100 80GB",        "price": 2.5585, "vram": 80,  "region": "us-central1", "contract": "reserved"},
    {"gpu": "A100 80GB",        "price": 2.4874, "vram": 80,  "region": "us-central1", "contract": "reserved"},
    {"gpu": "A100 40GB",        "price": 1.8478, "vram": 40,  "region": "us-central1", "contract": "reserved"},
]

_PRICE_RE = re.compile(r"\$\s*(\d+\.\d+)")


class GCPProvider(BaseProvider):
    name = "GCP"
    timeout = 30.0

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

        # GCP pricing page has <table> elements with GPU model and price columns
        for table in tree.css("table"):
            rows = table.css("tr")
            header_cells = [c.text(strip=True).lower() for c in rows[0].css("th, td")] if rows else []
            # Look for tables with a price column
            price_col = next((i for i, h in enumerate(header_cells) if "price" in h or "cost" in h), None)
            name_col  = next((i for i, h in enumerate(header_cells) if "gpu" in h or "model" in h or "type" in h), 0)
            if price_col is None:
                continue

            for row in rows[1:]:
                cells = row.css("td")
                if len(cells) <= max(price_col, name_col):
                    continue
                raw_name = cells[name_col].text(strip=True)
                price_text = cells[price_col].text(strip=True)
                m = _PRICE_RE.search(price_text)
                if not m or not raw_name:
                    continue
                canonical = normalize_gpu_name(raw_name)
                offers.append(GPUOffer(
                    provider=self.name,
                    gpu_model=canonical,
                    vram_gb=lookup_vram(canonical),
                    price_per_hour=float(m.group(1)),
                    region="us-central1",
                    contract_type="on-demand",
                    availability=True,
                    raw_gpu_name=raw_name,
                ))

        if not offers:
            raise ValueError("no rows parsed from live page")
        return offers

    def _static_fallback(self) -> list[GPUOffer]:
        return [
            GPUOffer(
                provider=self.name,
                gpu_model=normalize_gpu_name(r["gpu"]),
                vram_gb=r["vram"],
                price_per_hour=r["price"],
                region=r["region"],
                contract_type=r["contract"],
                availability=True,
                raw_gpu_name=r["gpu"],
            )
            for r in _STATIC
        ]
