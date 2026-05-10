"""Azure — public Retail Prices REST API, no auth required."""
from __future__ import annotations

import re

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

_BASE_URL = "https://prices.azure.com/api/retail/prices"
_API_VERSION = "2023-01-01-preview"

# ARM SKU name regex for GPU VM families
_GPU_SKU_RE = re.compile(r"Standard_(NC|ND|NV)\d", re.I)

# Map ARM SKU fragments → canonical GPU name
_SKU_GPU: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"ND96isr_H100",            re.I), "H100 SXM"),
    (re.compile(r"ND96amsr_A100",           re.I), "A100 80GB SXM"),
    (re.compile(r"ND96asr_A100",            re.I), "A100 80GB SXM"),
    (re.compile(r"NC\d+ads_A100",           re.I), "A100 80GB PCIe"),
    (re.compile(r"NC96ads_A100",            re.I), "A100 80GB PCIe"),
    (re.compile(r"NC24s_v3|NC48s_v3|NC96s_v3", re.I), "V100 16GB"),
    (re.compile(r"NV\d+adms_A10",           re.I), "A10"),
    (re.compile(r"NV\d+ads_A10",            re.I), "A10"),
]

# Fetch GPU VM prices for one filter clause, following pagination
_GPU_FILTERS = [
    "serviceName eq 'Virtual Machines' and contains(skuName, 'NC') and priceType eq 'Consumption'",
    "serviceName eq 'Virtual Machines' and contains(skuName, 'ND') and priceType eq 'Consumption'",
]


def _infer_gpu(sku_name: str) -> str:
    for pattern, gpu in _SKU_GPU:
        if pattern.search(sku_name):
            return gpu
    return normalize_gpu_name(sku_name)


def _infer_gpu_count(sku_name: str) -> int:
    m = re.search(r"_x(\d+)$", sku_name, re.I)
    return int(m.group(1)) if m else 1


def _contract_type(sku_name: str) -> str:
    low = sku_name.lower()
    if "spot" in low:
        return "spot"
    if "1 year" in low or "3 year" in low or "reserved" in low:
        return "reserved"
    return "on-demand"


class AzureProvider(BaseProvider):
    name = "Azure"
    timeout = 60.0

    async def _scrape(self) -> list[GPUOffer]:
        client = await self._get_client()
        offers: list[GPUOffer] = []
        seen: set[str] = set()

        for filt in _GPU_FILTERS:
            url: str | None = _BASE_URL
            params: dict = {"api-version": _API_VERSION, "$filter": filt}

            while url:
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                body = resp.json()
                params = {}
                url = body.get("NextPageLink")

                for item in body.get("Items", []):
                    sku: str = item.get("armSkuName", "")
                    if not _GPU_SKU_RE.match(sku):
                        continue

                    retail_price: float = item.get("retailPrice", 0.0)
                    if not retail_price:
                        continue

                    gpu_model = _infer_gpu(sku)
                    region: str = item.get("armRegionName", "unknown")
                    ctype = _contract_type(item.get("skuName", ""))
                    key = f"{sku}|{region}|{ctype}"
                    if key in seen:
                        continue
                    seen.add(key)

                    offers.append(GPUOffer(
                        provider=self.name,
                        gpu_model=gpu_model,
                        vram_gb=lookup_vram(gpu_model),
                        price_per_hour=retail_price,
                        region=region,
                        contract_type=ctype,
                        availability=True,
                        gpu_count=_infer_gpu_count(sku),
                        instance_type=sku,
                        raw_gpu_name=sku,
                    ))

        return offers
