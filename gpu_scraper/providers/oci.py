"""Oracle Cloud Infrastructure — public pricing REST API."""
from __future__ import annotations

import re
from typing import Any

from ..models import GPUOffer
from ..normalizer import normalize_gpu_name, lookup_vram
from .base import BaseProvider

# No query params — returns all ~620 products
_API_URL = "https://apexapps.oracle.com/pls/apex/cetools/api/v1/products/"

# Match on displayName: must contain "GPU" (case-insensitive)
_GPU_RE = re.compile(r"\bgpu\b", re.I)

# Skip reseller / commit / infrastructure-as-a-whole entries
_SKIP_RE = re.compile(
    r"(vmware|roving edge|cloud@customer|infrastructure.*gpu compute|resource credit"
    r"|monthly commit|year commit|nvidia ai enterprise)",
    re.I,
)

# Map display name fragments → canonical GPU model
_NAME_GPU: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\bH200\b",          re.I), "H200"),
    (re.compile(r"\bH100T\b",         re.I), "H100 SXM"),
    (re.compile(r"\bH100\b",          re.I), "H100 SXM"),
    (re.compile(r"\bGB300\b",         re.I), "B300"),
    (re.compile(r"\bGB200\b",         re.I), "H100 SXM"),   # Grace Blackwell node
    (re.compile(r"\bB300\b",          re.I), "B300"),
    (re.compile(r"\bB200\b",          re.I), "B200"),
    (re.compile(r"\bMI355X\b",        re.I), "MI355X"),
    (re.compile(r"\bMI300X\b",        re.I), "MI300X"),
    (re.compile(r"\bA100.*80\b",      re.I), "A100 80GB"),
    (re.compile(r"\bA100.*v2\b",      re.I), "A100 80GB"),
    (re.compile(r"\bA100.*40\b",      re.I), "A100 40GB"),
    (re.compile(r"\bA100\b",          re.I), "A100 80GB"),
    (re.compile(r"\bL40S\b",          re.I), "L40S"),
    (re.compile(r"\bA10\b",           re.I), "A10"),
    (re.compile(r"\bV100\b",          re.I), "V100 16GB"),
    # Legacy shapes by GPU generation
    (re.compile(r"GPU Standard.*X7",  re.I), "P100"),
    (re.compile(r"GPU Standard.*V2",  re.I), "V100 16GB"),
    (re.compile(r"GPU.*E[34]\b",      re.I), "A10"),
]


def _display_to_gpu(display_name: str) -> str | None:
    for pattern, gpu in _NAME_GPU:
        if pattern.search(display_name):
            return gpu
    return None


def _get_usd_price(item: dict[str, Any]) -> float:
    """Extract USD pay-as-you-go hourly price from currencyCodeLocalizations."""
    for loc in item.get("currencyCodeLocalizations", []):
        if loc.get("currencyCode") == "USD":
            for price_entry in loc.get("prices", []):
                if price_entry.get("model") == "PAY_AS_YOU_GO":
                    try:
                        return float(price_entry.get("value", 0) or 0)
                    except (TypeError, ValueError):
                        pass
    return 0.0


class OCIProvider(BaseProvider):
    name = "OCI"
    timeout = 30.0

    async def _scrape(self) -> list[GPUOffer]:
        client = await self._get_client()
        resp = await client.get(_API_URL)
        resp.raise_for_status()

        data = resp.json()
        items: list[dict[str, Any]] = (
            data.get("items", data) if isinstance(data, dict) else data
        )
        offers: list[GPUOffer] = []

        for item in items:
            display: str = item.get("displayName", "")

            if not _GPU_RE.search(display):
                continue
            if _SKIP_RE.search(display):
                continue

            gpu_model = _display_to_gpu(display)
            if not gpu_model:
                continue

            price = _get_usd_price(item)
            if price <= 0:
                continue

            offers.append(GPUOffer(
                provider=self.name,
                gpu_model=gpu_model,
                vram_gb=lookup_vram(gpu_model),
                price_per_hour=price,
                region="us-ashburn-1",
                contract_type="on-demand",
                availability=True,
                gpu_count=1,
                instance_type=item.get("partNumber", ""),
                raw_gpu_name=display,
            ))

        return offers
