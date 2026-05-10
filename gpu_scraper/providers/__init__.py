"""Provider registry — add new providers here."""
from __future__ import annotations

from .aws import AWSProvider
from .azure import AzureProvider
from .coreweave import CoreWeaveProvider
from .gcp import GCPProvider
from .lambda_labs import LambdaLabsProvider
from .oci import OCIProvider
from .paperspace import PaperspaceProvider
from .runpod import RunPodProvider
from .tensordock import TensorDockProvider
from .vast_ai import VastAIProvider

ALL_PROVIDERS = [
    RunPodProvider,
    LambdaLabsProvider,
    VastAIProvider,
    CoreWeaveProvider,
    PaperspaceProvider,
    TensorDockProvider,
    AWSProvider,
    GCPProvider,
    AzureProvider,
    OCIProvider,
]

__all__ = [
    "ALL_PROVIDERS",
    "AWSProvider",
    "AzureProvider",
    "CoreWeaveProvider",
    "GCPProvider",
    "LambdaLabsProvider",
    "OCIProvider",
    "PaperspaceProvider",
    "RunPodProvider",
    "TensorDockProvider",
    "VastAIProvider",
]
