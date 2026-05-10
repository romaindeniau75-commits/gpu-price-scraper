"""RunPod — public GraphQL API, no auth required."""
from __future__ import annotations

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_GQL_URL = "https://api.runpod.io/graphql"

_QUERY = """
query GpuTypes {
  gpuTypes {
    id
    displayName
    memoryInGb
    securePrice
    communityPrice
    secureSpotPrice
    communitySpotPrice
    lowestPrice {
      minimumBidPrice
      uninterruptablePrice
    }
  }
}
"""


class RunPodProvider(BaseProvider):
    name = "RunPod"

    async def _scrape(self) -> list[GPUOffer]:
        client = await self._get_client()
        resp = await client.post(_GQL_URL, json={"query": _QUERY})
        resp.raise_for_status()

        gpu_types = resp.json()["data"]["gpuTypes"]
        offers: list[GPUOffer] = []

        for gt in gpu_types:
            raw_name: str = gt.get("displayName") or gt.get("id", "")
            canonical = normalize_gpu_name(raw_name)
            vram = gt.get("memoryInGb") or lookup_vram(canonical)

            # Secure cloud (data-centre grade) — on-demand
            if (price := gt.get("securePrice")) and price > 0:
                offers.append(GPUOffer(
                    provider=self.name,
                    gpu_model=canonical,
                    vram_gb=vram,
                    price_per_hour=price,
                    region="Global",
                    contract_type="on-demand",
                    availability=True,
                    raw_gpu_name=raw_name,
                ))

            # Secure cloud — spot
            if (spot := gt.get("secureSpotPrice")) and spot > 0:
                offers.append(GPUOffer(
                    provider=self.name,
                    gpu_model=canonical,
                    vram_gb=vram,
                    price_per_hour=spot,
                    region="Global",
                    contract_type="spot",
                    availability=True,
                    raw_gpu_name=raw_name,
                ))

            # Community cloud — on-demand
            if (cprice := gt.get("communityPrice")) and cprice > 0:
                offers.append(GPUOffer(
                    provider=f"{self.name} Community",
                    gpu_model=canonical,
                    vram_gb=vram,
                    price_per_hour=cprice,
                    region="Global",
                    contract_type="on-demand",
                    availability=True,
                    raw_gpu_name=raw_name,
                ))

        return offers
