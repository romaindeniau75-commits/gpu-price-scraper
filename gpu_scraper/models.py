from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator


ContractType = Literal["on-demand", "spot", "reserved"]
PriceUnit = Literal["per_gpu", "per_node"]


class GPUOffer(BaseModel):
    """A single GPU pricing observation from one provider.

    Pricing semantics
    -----------------
    ``price_per_hour``     — Raw price exactly as returned by the provider API.
                             Semantics vary by provider (see ``price_unit``).
    ``price_unit``         — "per_gpu"  → price_per_hour is already per individual GPU.
                             "per_node" → price_per_hour covers the whole multi-GPU node.
    ``price_per_gpu_hour`` — Analytics source of truth: always $/GPU/hr.
                             Auto-computed from the two fields above; do not set manually.
    ``gpu_count``          — Number of GPUs on the node/instance (informational).
    """

    provider: str
    gpu_model: str          # normalised: "H100 SXM", "A100 80GB PCIe", …
    vram_gb: int
    price_per_hour: float   # Raw API price — audit trail, do NOT use for analytics
    price_unit: PriceUnit = "per_gpu"   # Semantics of price_per_hour
    region: str
    contract_type: ContractType
    availability: bool
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    gpu_count: int = 1
    price_per_gpu_hour: float = 0.0     # Analytics source of truth; auto-computed below
    instance_type: Optional[str] = None  # "p5.48xlarge", "Standard_NC24ads_A100_v4", …
    raw_gpu_name: Optional[str] = None   # Original name from provider

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}

    @model_validator(mode="after")
    def _compute_price_per_gpu_hour(self) -> "GPUOffer":
        """Derive price_per_gpu_hour from price_per_hour ÷ gpu_count when per_node."""
        if self.price_per_gpu_hour == 0.0:
            if self.price_unit == "per_node":
                self.price_per_gpu_hour = round(
                    self.price_per_hour / max(self.gpu_count, 1), 4
                )
            else:
                self.price_per_gpu_hour = self.price_per_hour
        return self

    def price_key(self) -> str:
        """Stable key for price-change tracking in watch mode."""
        return f"{self.provider}|{self.gpu_model}|{self.region}|{self.contract_type}|{self.gpu_count}"
