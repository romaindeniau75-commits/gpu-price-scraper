from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


ContractType = Literal["on-demand", "spot", "reserved"]


class GPUOffer(BaseModel):
    provider: str
    gpu_model: str          # normalised: "H100 SXM", "A100 80GB PCIe", …
    vram_gb: int
    price_per_hour: float
    region: str
    contract_type: ContractType
    availability: bool
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    gpu_count: int = 1
    instance_type: Optional[str] = None   # "p5.48xlarge", "Standard_NC24ads_A100_v4", …
    raw_gpu_name: Optional[str] = None    # original name from provider

    model_config = {"json_encoders": {datetime: lambda v: v.isoformat()}}

    def price_key(self) -> str:
        """Stable key for price-change tracking."""
        return f"{self.provider}|{self.gpu_model}|{self.region}|{self.contract_type}|{self.gpu_count}"
