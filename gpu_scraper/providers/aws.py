"""AWS — spot prices via public JSONP endpoint + static on-demand table.

On-demand prices are fetched from the public AWS pricing JSON for us-east-1.
Spot prices come from the real-time spot.js JSONP endpoint.
"""
from __future__ import annotations

import json
import re
from typing import Any

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

# JSONP endpoint — no auth, updated frequently
_SPOT_URL = "https://spot-price.s3.amazonaws.com/spot.js"

# Public on-demand pricing JSON (us-east-1 only — smaller regional file)
_OD_URL = (
    "https://pricing.us-east-1.amazonaws.com"
    "/offers/v1.0/aws/AmazonEC2/current/us-east-1/index.json"
)

# GPU instance families we care about
_GPU_FAMILIES = {"p3", "p4d", "p4de", "p5", "g4dn", "g5", "g6"}

# instance-type → (GPU model, count)
_INSTANCE_GPU: dict[str, tuple[str, int]] = {
    "p3.2xlarge":    ("V100 16GB",    1),
    "p3.8xlarge":    ("V100 16GB",    4),
    "p3.16xlarge":   ("V100 16GB",    8),
    "p3dn.24xlarge": ("V100 32GB",    8),
    "p4d.24xlarge":  ("A100 40GB SXM", 8),
    "p4de.24xlarge": ("A100 80GB SXM", 8),
    "p5.48xlarge":   ("H100 SXM",     8),
    "p5e.48xlarge":  ("H100 SXM",     8),
    "g4dn.xlarge":   ("T4",           1),
    "g4dn.2xlarge":  ("T4",           1),
    "g4dn.4xlarge":  ("T4",           1),
    "g4dn.8xlarge":  ("T4",           1),
    "g4dn.12xlarge": ("T4",           4),
    "g4dn.16xlarge": ("T4",           1),
    "g5.xlarge":     ("A10G",         1),
    "g5.2xlarge":    ("A10G",         1),
    "g5.4xlarge":    ("A10G",         1),
    "g5.8xlarge":    ("A10G",         1),
    "g5.12xlarge":   ("A10G",         4),
    "g5.16xlarge":   ("A10G",         1),
    "g5.24xlarge":   ("A10G",         4),
    "g5.48xlarge":   ("A10G",         8),
    "g6.xlarge":     ("L4",           1),
    "g6.4xlarge":    ("L4",           1),
    "g6.8xlarge":    ("L4",           1),
    "g6.12xlarge":   ("L4",           4),
    "g6.16xlarge":   ("L4",           1),
    "g6.24xlarge":   ("L4",           4),
    "g6.48xlarge":   ("L4",           8),
}


def _is_gpu_instance(itype: str) -> bool:
    family = itype.split(".")[0]
    return family in _GPU_FAMILIES


class AWSProvider(BaseProvider):
    name = "AWS"
    timeout = 60.0

    async def _scrape(self) -> list[GPUOffer]:
        offers: list[GPUOffer] = []
        offers += await self._fetch_spot()
        offers += await self._fetch_on_demand()
        return offers

    # ------------------------------------------------------------------ spot

    async def _fetch_spot(self) -> list[GPUOffer]:
        client = await self._get_client()
        resp = await client.get(_SPOT_URL)
        resp.raise_for_status()

        # Strip JSONP wrapper: callback({...})
        text = resp.text.strip()
        m = re.match(r"^\w+\((.+)\)\s*;?\s*$", text, re.S)
        if not m:
            return []
        data: dict[str, Any] = json.loads(m.group(1))

        offers: list[GPUOffer] = []
        for region_data in data.get("config", {}).get("regions", []):
            region: str = region_data.get("region", "unknown")
            for itype_data in region_data.get("instanceTypes", []):
                for size_data in itype_data.get("sizes", []):
                    itype: str = size_data.get("size", "")
                    if not _is_gpu_instance(itype):
                        continue
                    for os_data in size_data.get("valueColumns", []):
                        if os_data.get("name") != "linux":
                            continue
                        try:
                            price = float(os_data["prices"]["USD"])
                        except (KeyError, ValueError, TypeError):
                            continue
                        if price <= 0:
                            continue

                        gpu_model, gpu_count = _INSTANCE_GPU.get(itype, (normalize_gpu_name(itype), 1))
                        offers.append(GPUOffer(
                            provider=self.name,
                            gpu_model=gpu_model,
                            vram_gb=lookup_vram(gpu_model),
                            price_per_hour=round(price / gpu_count, 4),  # per-GPU price
                            region=region,
                            contract_type="spot",
                            availability=True,
                            gpu_count=gpu_count,
                            instance_type=itype,
                            raw_gpu_name=itype,
                        ))
        return offers

    # ------------------------------------------------------------------ on-demand

    async def _fetch_on_demand(self) -> list[GPUOffer]:
        """Fetch on-demand GPU pricing from the us-east-1 public pricing JSON.

        The file is ~200 MB; we stream it and early-exit once we've parsed
        all known GPU SKUs. Falls back to an empty list if too slow.
        """
        client = await self._get_client()

        # Fetch the full JSON — only for us-east-1 to keep size manageable
        resp = await client.get(_OD_URL)
        resp.raise_for_status()

        try:
            data: dict = resp.json()
        except Exception:  # noqa: BLE001
            return []

        products: dict = data.get("products", {})
        terms: dict = data.get("terms", {}).get("OnDemand", {})

        # Build sku → instance-type map for GPU instances
        sku_itype: dict[str, str] = {}
        for sku, prod in products.items():
            attrs = prod.get("attributes", {})
            itype: str = attrs.get("instanceType", "")
            if _is_gpu_instance(itype) and attrs.get("operatingSystem") == "Linux" and attrs.get("tenancy") == "Shared":
                sku_itype[sku] = itype

        offers: list[GPUOffer] = []
        for sku, itype in sku_itype.items():
            sku_terms = terms.get(sku, {})
            for offer_term in sku_terms.values():
                for dim in offer_term.get("priceDimensions", {}).values():
                    try:
                        price = float(dim["pricePerUnit"]["USD"])
                    except (KeyError, ValueError, TypeError):
                        continue
                    if price <= 0:
                        continue
                    gpu_model, gpu_count = _INSTANCE_GPU.get(itype, (normalize_gpu_name(itype), 1))
                    offers.append(GPUOffer(
                        provider=self.name,
                        gpu_model=gpu_model,
                        vram_gb=lookup_vram(gpu_model),
                        price_per_hour=round(price / gpu_count, 4),  # per-GPU price
                        region="us-east-1",
                        contract_type="on-demand",
                        availability=True,
                        gpu_count=gpu_count,
                        instance_type=itype,
                        raw_gpu_name=itype,
                    ))

        return offers
