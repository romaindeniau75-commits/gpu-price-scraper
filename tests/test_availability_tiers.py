"""Tests for availability tier semantics, new providers, and analytics tier separation.

Run with:  python3 -m pytest tests/ -v
"""
from __future__ import annotations

import json
import tempfile
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gpu_scraper.models import GPUOffer
from gpu_scraper.analytics import PriceAnalytics
from gpu_scraper.storage import PriceDatabase
from gpu_scraper.normalizer import canonicalize_region


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_offer(**kwargs) -> GPUOffer:
    defaults = dict(
        provider="TestProvider",
        gpu_model="H100 SXM",
        vram_gb=80,
        price_per_hour=2.00,
        price_unit="per_gpu",
        region="us-east-1",
        availability="on_demand",
        available=True,
    )
    defaults.update(kwargs)
    return GPUOffer(**defaults)


def _db_with_offers(offers: list[GPUOffer]) -> PriceDatabase:
    tmp = tempfile.mktemp(suffix=".db")
    db = PriceDatabase(tmp)
    db.init()
    run_id = db.start_run()
    db.save_offers(offers, run_id)
    db.finish_run(run_id, len(offers), ["TestProvider"], [])
    return db


# ---------------------------------------------------------------------------
# 1. GPUOffer availability tier derivation
# ---------------------------------------------------------------------------

class TestAvailabilityTierDerivation:
    """GPUOffer derives interruptible + contract_type from availability tier."""

    def test_on_demand_not_interruptible(self):
        offer = make_offer(availability="on_demand")
        assert offer.interruptible is False
        assert offer.contract_type == "on-demand"

    def test_spot_is_interruptible(self):
        offer = make_offer(availability="spot")
        assert offer.interruptible is True
        assert offer.contract_type == "spot"

    def test_interruptible_tier(self):
        offer = make_offer(availability="interruptible")
        assert offer.interruptible is True
        assert offer.contract_type == "spot"

    def test_community_is_interruptible(self):
        offer = make_offer(availability="community")
        assert offer.interruptible is True
        assert offer.contract_type == "on-demand"  # community maps to on-demand contract

    def test_reserved_not_interruptible(self):
        offer = make_offer(availability="reserved", commitment_term="1 year")
        assert offer.interruptible is False
        assert offer.contract_type == "reserved"

    def test_commitment_term_stored(self):
        offer = make_offer(availability="reserved", commitment_term="3 years")
        assert offer.commitment_term == "3 years"

    def test_default_availability_is_on_demand(self):
        offer = make_offer()
        assert offer.availability == "on_demand"


# ---------------------------------------------------------------------------
# 2. Region canonicalization
# ---------------------------------------------------------------------------

class TestRegionCanonicalization:
    """canonicalize_region maps provider strings to canonical buckets."""

    def test_us_east_variants(self):
        for region in ["us-east-1", "eastus", "us-ashburn-1", "US East", "Virginia"]:
            assert canonicalize_region(region) == "us-east", f"Failed: {region!r}"

    def test_us_west_variants(self):
        for region in ["us-west-2", "westus", "Oregon", "us-west"]:
            assert canonicalize_region(region) == "us-west", f"Failed: {region!r}"

    def test_eu_west_variants(self):
        for region in ["eu-west-1", "westeurope", "Ireland", "amsterdam"]:
            assert canonicalize_region(region) == "eu-west", f"Failed: {region!r}"

    def test_global_maps_to_global(self):
        assert canonicalize_region("Global") == "global"

    def test_region_canonical_auto_computed(self):
        offer = make_offer(region="us-east-1")
        assert offer.region_canonical == "us-east"

    def test_eu_region_canonical(self):
        offer = make_offer(region="eu-west-1")
        assert offer.region_canonical == "eu-west"


# ---------------------------------------------------------------------------
# 3. Provider mocked scrape tests
# ---------------------------------------------------------------------------

class TestRunPodProvider:
    """RunPod emits three tiers: on_demand, spot, community."""

    @pytest.mark.asyncio
    async def test_runpod_tiers(self):
        from gpu_scraper.providers.runpod import RunPodProvider

        mock_data = {
            "data": {
                "gpuTypes": [{
                    "id": "NVIDIA_H100_SXM5",
                    "displayName": "NVIDIA H100 SXM5 80GB",
                    "memoryInGb": 80,
                    "securePrice": 3.50,
                    "secureSpotPrice": 1.20,
                    "communityPrice": 2.80,
                    "communitySpotPrice": 0.0,
                    "lowestPrice": {"minimumBidPrice": None, "uninterruptablePrice": None},
                }]
            }
        }

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = mock_data

        mock_client = AsyncMock()
        mock_client.post.return_value = mock_resp

        provider = RunPodProvider()
        with patch.object(provider, "_get_client", return_value=mock_client):
            offers = await provider._scrape()

        tiers = {o.availability for o in offers}
        assert "on_demand" in tiers
        assert "spot" in tiers
        assert "community" in tiers
        assert all(o.vram_gb == 80 for o in offers)


class TestVastAIProvider:
    """Vast.ai interruptible vs on-demand detection."""

    @pytest.mark.asyncio
    async def test_vastai_interruptible_detected(self):
        from gpu_scraper.providers.vast_ai import VastAIProvider

        mock_data = {
            "offers": [
                {
                    "gpu_name": "RTX 3090",
                    "num_gpus": 1,
                    "dph_total": 0.50,
                    "gpu_ram": 24576,
                    "min_bid": 0.10,   # interruptible
                    "rentable": True,
                    "geolocation": "US",
                },
                {
                    "gpu_name": "RTX 3090",
                    "num_gpus": 1,
                    "dph_total": 0.70,
                    "gpu_ram": 24576,
                    "min_bid": None,   # on-demand
                    "rentable": True,
                    "geolocation": "US",
                },
            ]
        }

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = mock_data

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        provider = VastAIProvider()
        with patch.object(provider, "_get_client", return_value=mock_client):
            offers = await provider._scrape()

        assert len(offers) == 2
        tiers = {o.availability for o in offers}
        assert "interruptible" in tiers
        assert "on_demand" in tiers
        interruptible = next(o for o in offers if o.availability == "interruptible")
        assert interruptible.interruptible is True


class TestDataCrunchProvider:
    """DataCrunch both on-demand and spot from a single instance."""

    @pytest.mark.asyncio
    async def test_datacrunch_both_tiers(self):
        from gpu_scraper.providers.datacrunch import DataCrunchProvider

        mock_data = [
            {
                "instance_type": "8V100.48V",
                "gpu": {
                    "description": "V100 SXM2 16GB",
                    "name": "V100",
                    "count": 8,
                    "memory_in_gigabytes": 16,
                },
                "price_per_hour": 6.40,
                "spot_price": 2.00,
            }
        ]

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = mock_data

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        provider = DataCrunchProvider()
        with patch.object(provider, "_get_client", return_value=mock_client):
            offers = await provider._scrape()

        assert len(offers) == 2
        od = next(o for o in offers if o.availability == "on_demand")
        sp = next(o for o in offers if o.availability == "spot")
        assert od.price_per_hour == pytest.approx(6.40)
        assert sp.price_per_hour == pytest.approx(2.00)
        assert sp.interruptible is True


class TestCrusoeProvider:
    """Crusoe falls back to static prices gracefully."""

    @pytest.mark.asyncio
    async def test_crusoe_static_fallback(self):
        from gpu_scraper.providers.crusoe import CrusoeProvider

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "<html><body>No pricing rows here</body></html>"

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        provider = CrusoeProvider()
        with patch.object(provider, "_get_client", return_value=mock_client):
            offers = await provider._scrape()

        # Should return static fallback, not raise
        assert len(offers) > 0
        assert all(o.availability == "on_demand" for o in offers)
        assert all(o.provider == "Crusoe" for o in offers)


class TestHyperstackProvider:
    """Hyperstack returns static prices directly."""

    @pytest.mark.asyncio
    async def test_hyperstack_static(self):
        from gpu_scraper.providers.hyperstack import HyperstackProvider

        provider = HyperstackProvider()
        offers = await provider._scrape()
        assert len(offers) > 0
        assert all(o.availability == "on_demand" for o in offers)
        assert all(o.price_per_hour > 0 for o in offers)


class TestNebiusProvider:
    """Nebius returns static prices in multiple regions."""

    @pytest.mark.asyncio
    async def test_nebius_static(self):
        from gpu_scraper.providers.nebius import NebiusProvider

        provider = NebiusProvider()
        offers = await provider._scrape()
        assert len(offers) > 0
        regions = {o.region for o in offers}
        assert len(regions) > 1  # multiple regions
        assert all(o.availability == "on_demand" for o in offers)


class TestGCPProvider:
    """GCP static fallback includes spot and reserved tiers."""

    @pytest.mark.asyncio
    async def test_gcp_has_spot_tier(self):
        from gpu_scraper.providers.gcp import GCPProvider

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "<html><body>No useful pricing</body></html>"

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        provider = GCPProvider()
        with patch.object(provider, "_get_client", return_value=mock_client):
            offers = await provider._scrape()

        tiers = {o.availability for o in offers}
        assert "on_demand" in tiers
        assert "spot" in tiers
        assert "reserved" in tiers

    @pytest.mark.asyncio
    async def test_gcp_reserved_has_commitment_term(self):
        from gpu_scraper.providers.gcp import GCPProvider

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.text = "<html><body></body></html>"

        mock_client = AsyncMock()
        mock_client.get.return_value = mock_resp

        provider = GCPProvider()
        with patch.object(provider, "_get_client", return_value=mock_client):
            offers = await provider._scrape()

        reserved = [o for o in offers if o.availability == "reserved"]
        assert len(reserved) > 0
        assert all(o.commitment_term is not None for o in reserved)


# ---------------------------------------------------------------------------
# 4. Analytics tier separation
# ---------------------------------------------------------------------------

class TestAnalyticsTierSeparation:
    """get_market_stats defaults to on_demand only; spot doesn't pollute spread."""

    def test_market_stats_excludes_spot_by_default(self):
        offers = [
            make_offer(provider="A", price_per_hour=3.00, availability="on_demand"),
            make_offer(provider="B", price_per_hour=3.50, availability="on_demand"),
            make_offer(provider="C", price_per_hour=0.50, availability="spot"),  # should be excluded
        ]
        db = _db_with_offers(offers)
        analytics = PriceAnalytics(db)
        stats = analytics.get_market_stats(gpu_filter="H100", hours=24 * 365)

        # With spot excluded, min price should be 3.00, not 0.50
        assert not stats.empty
        row = stats.iloc[0]
        assert row["min_price"] >= 2.9  # on-demand only

    def test_market_stats_all_includes_spot(self):
        offers = [
            make_offer(provider="A", price_per_hour=3.00, availability="on_demand"),
            make_offer(provider="C", price_per_hour=0.50, availability="spot"),
        ]
        db = _db_with_offers(offers)
        analytics = PriceAnalytics(db)
        stats = analytics.get_market_stats(
            gpu_filter="H100", hours=24 * 365, availability_filter="all"
        )
        assert not stats.empty
        # Should have both on-demand and spot rows or a combined row with min 0.50
        min_price = stats["min_price"].min()
        assert min_price < 1.0  # spot is included

    def test_find_opportunities_on_demand_only(self):
        """find_opportunities must not rank spot as an opportunity vs on-demand."""
        offers = [
            make_offer(provider="Expensive", price_per_hour=4.00, availability="on_demand"),
            make_offer(provider="Mid", price_per_hour=3.00, availability="on_demand"),
            make_offer(provider="Spot", price_per_hour=0.50, availability="spot"),
        ]
        db = _db_with_offers(offers)
        analytics = PriceAnalytics(db)
        opps = analytics.find_opportunities(gpu_filter="H100", hours=24 * 365)

        if not opps.empty:
            # All opportunities should be on-demand providers only
            assert "Spot" not in opps["buy_provider"].values


class TestGetSpotDiscount:
    """get_spot_discount shows on-demand vs spot savings per GPU model."""

    def test_spot_discount_computed(self):
        offers = [
            make_offer(provider="ProvA", price_per_hour=4.00, availability="on_demand",
                       gpu_model="A100 80GB", vram_gb=80),
            make_offer(provider="ProvB", price_per_hour=1.20, availability="spot",
                       gpu_model="A100 80GB", vram_gb=80),
        ]
        db = _db_with_offers(offers)
        analytics = PriceAnalytics(db)
        disc = analytics.get_spot_discount(gpu_filter="A100", hours=24 * 365)

        assert not disc.empty
        row = disc.iloc[0]
        assert row["on_demand_min"] == pytest.approx(4.00)
        assert row["spot_min"] == pytest.approx(1.20)
        assert row["discount_pct"] == pytest.approx(70.0, abs=0.1)

    def test_spot_discount_empty_when_no_spot(self):
        offers = [
            make_offer(provider="ProvA", price_per_hour=4.00, availability="on_demand"),
        ]
        db = _db_with_offers(offers)
        analytics = PriceAnalytics(db)
        disc = analytics.get_spot_discount(gpu_filter="H100", hours=24 * 365)
        assert disc.empty

    def test_interruptible_counted_as_spot(self):
        offers = [
            make_offer(provider="ProvA", price_per_hour=3.00, availability="on_demand",
                       gpu_model="T4", vram_gb=16),
            make_offer(provider="ProvB", price_per_hour=0.90, availability="interruptible",
                       gpu_model="T4", vram_gb=16),
        ]
        db = _db_with_offers(offers)
        analytics = PriceAnalytics(db)
        disc = analytics.get_spot_discount(gpu_filter="T4", hours=24 * 365)

        assert not disc.empty
        assert disc.iloc[0]["spot_min"] == pytest.approx(0.90)
