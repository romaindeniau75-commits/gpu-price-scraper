"""Vast.ai — public marketplace bundles API, no auth required.

Without an API key the public endpoint returns ~64 top-scored offers.
All GPU types and contract types are mixed in a single response.
"""
from __future__ import annotations

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_BUNDLES_URL = "https://console.vast.ai/api/v0/bundles/"


class VastAIProvider(BaseProvider):
    name = "Vast.ai"

    async def _scrape(self) -> list[GPUOffer]:
        client = await self._get_client()
        resp = await client.get(_BUNDLES_URL)
        resp.raise_for_status()

        raw_offers: list[dict] = resp.json().get("offers", [])
        offers: list[GPUOffer] = []

        for offer in raw_offers:
            raw_name: str = offer.get("gpu_name", "")
            canonical = normalize_gpu_name(raw_name)
            num_gpus: int = offer.get("num_gpus", 1) or 1
            dph_total: float = offer.get("dph_total", 0.0) or 0.0
            if not dph_total:
                continue

            price_per_gpu = round(dph_total / num_gpus, 4)
            vram_mb: int = offer.get("gpu_ram", 0) or 0
            # Vast.ai reports gpu_ram in MB
            vram_gb = vram_mb // 1024 if vram_mb > 1024 else (vram_mb or lookup_vram(canonical))

            # Vast.ai bundles endpoint mixes on-demand and bid offers
            # min_bid > 0 indicates a bid/spot listing
            is_spot = bool(offer.get("min_bid"))
            ctype = "spot" if is_spot else "on-demand"

            offers.append(GPUOffer(
                provider=self.name,
                gpu_model=canonical,
                vram_gb=vram_gb,
                price_per_hour=price_per_gpu,
                region=offer.get("geolocation", "Unknown"),
                contract_type=ctype,
                availability=bool(offer.get("rentable")),
                gpu_count=num_gpus,
                raw_gpu_name=raw_name,
            ))

        return offers
