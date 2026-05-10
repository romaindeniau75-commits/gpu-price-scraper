"""TensorDock — public marketplace API."""
from __future__ import annotations

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_API_URL = "https://marketplace.tensordock.com/api/session/deploy/hostnodes"


class TensorDockProvider(BaseProvider):
    name = "TensorDock"

    async def _scrape(self) -> list[GPUOffer]:
        client = await self._get_client()
        resp = await client.get(_API_URL)
        resp.raise_for_status()

        hostnodes: dict = resp.json().get("hostnodes", {})
        offers: list[GPUOffer] = []

        for _node_id, node in hostnodes.items():
            location = node.get("location", {})
            region = f"{location.get('country', 'Unknown')} / {location.get('city', '')}"

            # gpu spec is a dict keyed by GPU slug
            gpu_specs: dict = node.get("specs", {}).get("gpu", {})
            if not gpu_specs:
                continue

            available: bool = node.get("status", {}).get("online", True)

            for gpu_slug, gpu_info in gpu_specs.items():
                raw_name: str = gpu_slug.replace("-", " ")
                canonical = normalize_gpu_name(raw_name)
                vram: int = gpu_info.get("vram", 0) or lookup_vram(canonical)
                gpu_count: int = gpu_info.get("amount", 1)
                price: float = gpu_info.get("price", 0.0)

                if not price:
                    continue

                offers.append(GPUOffer(
                    provider=self.name,
                    gpu_model=canonical,
                    vram_gb=vram,
                    price_per_hour=round(price, 4),
                    price_unit="per_gpu",  # TensorDock API price field is /GPU/hr
                    region=region,
                    contract_type="on-demand",
                    availability=available,
                    gpu_count=gpu_count,
                    raw_gpu_name=raw_name,
                ))

        return offers
