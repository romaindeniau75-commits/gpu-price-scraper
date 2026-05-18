from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


ContractType = Literal["on-demand", "spot", "reserved"]
PriceUnit    = Literal["per_gpu", "per_node"]

# Availability tier — distinguishes interruptible workloads from stable ones.
# on_demand    → always-on, billed per-second, no interruption
# spot         → cloud-native spot (AWS/GCP/Azure), can be interrupted
# interruptible→ provider-specific interruptible (Vast.ai, RunPod spot)
# community    → community-contributed hardware (RunPod community cloud)
# reserved     → committed-use discount (1y / 3y)
AvailabilityTier = Literal["on_demand", "spot", "interruptible", "community", "reserved"]

_INTERRUPTIBLE_TIERS: frozenset[str] = frozenset({"spot", "interruptible", "community"})

# Map AvailabilityTier → ContractType (for backward compat in DB/display)
_TIER_TO_CONTRACT: dict[str, ContractType] = {
    "on_demand":     "on-demand",
    "spot":          "spot",
    "interruptible": "spot",
    "community":     "on-demand",
    "reserved":      "reserved",
}


class GPUOffer(BaseModel):
    """A single GPU pricing observation from one provider.

    Field semantics
    ---------------
    ``price_per_hour``     — Raw price exactly as returned by the provider API.
    ``price_unit``         — "per_gpu" or "per_node" (whole instance).
    ``price_per_gpu_hour`` — Analytics source of truth: always $/GPU/hr. Auto-computed.
    ``availability``       — Offer tier: on_demand / spot / interruptible / community / reserved.
    ``interruptible``      — True when availability ∈ {spot, interruptible, community}. Auto-computed.
    ``contract_type``      — Legacy field kept for DB/display compat; derived from availability.
    ``available``          — Whether the GPU is currently rentable (live availability check).
    ``commitment_term``    — For reserved offers: "1y", "3y", None otherwise.
    ``region_canonical``   — Normalised region bucket (us-east, eu-west, …). Auto-computed.
    ``gpu_count``          — Number of GPUs on the node/instance (informational).
    """

    provider: str
    gpu_model: str          # normalised: "H100 SXM", "A100 80GB PCIe", …
    vram_gb: int
    price_per_hour: float   # Raw API price — audit trail, do NOT use for analytics
    price_unit: PriceUnit = "per_gpu"
    region: str
    availability: AvailabilityTier = "on_demand"
    available: bool = True  # Is this offer currently rentable?
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    gpu_count: int = 1
    commitment_term: Optional[str] = None       # "1y", "3y", or None
    price_per_gpu_hour: float = 0.0             # auto-computed
    interruptible: bool = False                 # auto-computed from availability
    contract_type: ContractType = "on-demand"   # auto-derived from availability (compat)
    region_canonical: str = ""                  # auto-computed from region
    instance_type: Optional[str] = None
    raw_gpu_name: Optional[str] = None

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}

    @model_validator(mode="after")
    def _compute_derived_fields(self) -> "GPUOffer":
        """Compute price_per_gpu_hour, interruptible, contract_type, region_canonical."""
        # 1. price_per_gpu_hour
        if self.price_per_gpu_hour == 0.0:
            if self.price_unit == "per_node":
                self.price_per_gpu_hour = round(
                    self.price_per_hour / max(self.gpu_count, 1), 4
                )
            else:
                self.price_per_gpu_hour = self.price_per_hour

        # 2. interruptible
        self.interruptible = self.availability in _INTERRUPTIBLE_TIERS

        # 3. contract_type (kept for backward compat)
        self.contract_type = _TIER_TO_CONTRACT.get(self.availability, "on-demand")

        # 4. region_canonical
        if not self.region_canonical:
            from .normalizer import canonicalize_region
            self.region_canonical = canonicalize_region(self.region)

        return self

    def price_key(self) -> str:
        """Stable key for price-change tracking in watch mode."""
        return (
            f"{self.provider}|{self.gpu_model}|{self.region}"
            f"|{self.availability}|{self.gpu_count}"
        )
