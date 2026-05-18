"""Provider registry — add new providers here."""
from __future__ import annotations

from .aws import AWSProvider
from .azure import AzureProvider
from .coreweave import CoreWeaveProvider
from .crusoe import CrusoeProvider
from .datacrunch import DataCrunchProvider
from .gcp import GCPProvider
from .hyperstack import HyperstackProvider
from .lambda_labs import LambdaLabsProvider
from .nebius import NebiusProvider
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
    DataCrunchProvider,
    CrusoeProvider,
    HyperstackProvider,
    NebiusProvider,
]

__all__ = [
    "ALL_PROVIDERS",
    "AWSProvider",
    "AzureProvider",
    "CoreWeaveProvider",
    "CrusoeProvider",
    "DataCrunchProvider",
    "GCPProvider",
    "HyperstackProvider",
    "LambdaLabsProvider",
    "NebiusProvider",
    "OCIProvider",
    "PaperspaceProvider",
    "RunPodProvider",
    "TensorDockProvider",
    "VastAIProvider",
]
