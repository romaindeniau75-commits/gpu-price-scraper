"""Tests for price_per_gpu_hour normalization and analytics correctness.

Run with:  python3 -m pytest tests/ -v
"""
from __future__ import annotations

import sqlite3
import tempfile
from pathlib import Path

import pytest

from gpu_scraper.models import GPUOffer
from gpu_scraper.analytics import PriceAnalytics, compute_opportunity_score
from gpu_scraper.storage import PriceDatabase


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_offer(**kwargs) -> GPUOffer:
    defaults = dict(
        provider="TestProvider",
        gpu_model="H100 SXM",
        vram_gb=80,
        price_per_hour=1.0,
        price_unit="per_gpu",
        region="us-east-1",
        availability="on_demand",
        available=True,
    )
    defaults.update(kwargs)
    return GPUOffer(**defaults)


def _db_with_offers(offers: list[GPUOffer]) -> PriceDatabase:
    """Create a fresh in-memory-backed temp DB, seed with offers, return DB."""
    tmp = tempfile.mktemp(suffix=".db")
    db = PriceDatabase(tmp)
    db.init()
    run_id = db.start_run()
    db.save_offers(offers, run_id)
    db.finish_run(run_id, len(offers), ["TestProvider"], [])
    return db


# ---------------------------------------------------------------------------
# Model-level normalization tests
# ---------------------------------------------------------------------------

class TestGPUOfferPriceNormalization:
    def test_aws_node_price_divided_by_gpu_count(self):
        """p5.48xlarge: $55.04/node ÷ 8 GPUs = $6.88/GPU."""
        offer = make_offer(
            provider="AWS",
            price_per_hour=55.04,
            price_unit="per_node",
            gpu_count=8,
        )
        assert offer.price_per_gpu_hour == pytest.approx(55.04 / 8, rel=1e-4)

    def test_tensordock_per_gpu_passthrough(self):
        """TensorDock $1.99/GPU stays $1.99 — must NOT be divided."""
        offer = make_offer(
            provider="TensorDock",
            price_per_hour=1.99,
            price_unit="per_gpu",
            gpu_count=8,
        )
        assert offer.price_per_gpu_hour == pytest.approx(1.99)

    def test_per_gpu_single_gpu(self):
        """Single-GPU offer: price_per_gpu_hour == price_per_hour."""
        offer = make_offer(price_per_hour=2.49, price_unit="per_gpu", gpu_count=1)
        assert offer.price_per_gpu_hour == pytest.approx(2.49)

    def test_per_node_single_gpu(self):
        """Per-node offer with gpu_count=1: price_per_gpu_hour == price_per_hour."""
        offer = make_offer(price_per_hour=3.00, price_unit="per_node", gpu_count=1)
        assert offer.price_per_gpu_hour == pytest.approx(3.00)

    def test_raw_price_preserved(self):
        """price_per_hour (raw API price) must not be mutated."""
        offer = make_offer(price_per_hour=55.04, price_unit="per_node", gpu_count=8)
        assert offer.price_per_hour == pytest.approx(55.04)

    def test_azure_nd96_8_gpus(self):
        """Azure ND96isr_H100_v5: $32/VM ÷ 8 = $4/GPU."""
        offer = make_offer(
            provider="Azure",
            price_per_hour=32.00,
            price_unit="per_node",
            gpu_count=8,
        )
        assert offer.price_per_gpu_hour == pytest.approx(4.00)

    def test_azure_nc24_1_gpu(self):
        """Azure NC24ads_A100_v4: 1 GPU, price_per_gpu_hour == price_per_hour."""
        offer = make_offer(
            provider="Azure",
            price_per_hour=3.67,
            price_unit="per_node",
            gpu_count=1,
        )
        assert offer.price_per_gpu_hour == pytest.approx(3.67)


# ---------------------------------------------------------------------------
# Opportunity score uses price_per_gpu_hour
# ---------------------------------------------------------------------------

class TestOpportunityScore:
    def test_equal_per_gpu_prices_give_zero_discount(self):
        """Two offers at the same price_per_gpu_hour → discount_pct = 0 → no opportunity."""
        # If AWS stores $55.04 per_node x8 → $6.88/GPU
        # and CoreWeave stores $6.88 per_gpu x1 → $6.88/GPU
        # they should be treated as equal; neither is an opportunity vs the other.
        aws = make_offer(provider="AWS", price_per_hour=55.04,
                         price_unit="per_node", gpu_count=8)
        cw = make_offer(provider="CoreWeave", price_per_hour=6.88,
                        price_unit="per_gpu", gpu_count=1)
        assert aws.price_per_gpu_hour == pytest.approx(cw.price_per_gpu_hour, rel=1e-3)

    def test_score_increases_with_discount(self):
        score_low  = compute_opportunity_score(5,  20, 5, 0.9, "on-demand", 0.5)
        score_high = compute_opportunity_score(40, 20, 5, 0.9, "on-demand", 0.5)
        assert score_high > score_low

    def test_score_capped_at_one(self):
        score = compute_opportunity_score(100, 200, 100, 1.0, "on-demand", 0.0)
        assert score <= 1.0

    def test_score_non_negative(self):
        score = compute_opportunity_score(0, 0, 1, 0.0, "spot", 999)
        assert score >= 0.0


# ---------------------------------------------------------------------------
# Analytics integration: price_per_gpu_hour used in market stats
# ---------------------------------------------------------------------------

class TestAnalyticsPricePerGpu:
    def test_market_stats_uses_per_gpu_price(self):
        """AWS (per_node) and CoreWeave (per_gpu) at same effective price → spread=0."""
        offers = [
            make_offer(provider="AWS", price_per_hour=55.04,
                       price_unit="per_node", gpu_count=8,
                       region="us-east-1", availability="on_demand"),
            make_offer(provider="CoreWeave", price_per_hour=6.88,
                       price_unit="per_gpu", gpu_count=1,
                       region="US", availability="on_demand"),
        ]
        db = _db_with_offers(offers)
        analytics = PriceAnalytics(db)
        stats = analytics.get_market_stats(gpu_filter="H100", hours=24 * 365)
        assert not stats.empty
        row = stats[stats["contract_type"] == "on-demand"].iloc[0]
        assert row["spread_pct"] == pytest.approx(0.0, abs=0.5)

    def test_opportunity_not_created_by_price_unit_mismatch(self):
        """If AWS per_node and RunPod per_gpu are effectively equal, no opportunity."""
        offers = [
            make_offer(provider="AWS", price_per_hour=16.00,
                       price_unit="per_node", gpu_count=8,
                       region="us-east-1", availability="on_demand"),
            # 16/8 = 2.00/GPU — same as RunPod below
            make_offer(provider="RunPod", price_per_hour=2.00,
                       price_unit="per_gpu", gpu_count=1,
                       region="us-east-1", availability="on_demand"),
        ]
        db = _db_with_offers(offers)
        analytics = PriceAnalytics(db)
        opps = analytics.find_opportunities(gpu_filter="H100", hours=24 * 365)
        # Neither offer should be below the median of the other
        assert opps.empty or opps["discount_pct"].max() < 1.0


# ---------------------------------------------------------------------------
# Storage migration backfill test
# ---------------------------------------------------------------------------

class TestMigrationBackfill:
    def test_backfill_per_node_provider(self):
        """Legacy AWS row (no price_per_gpu_hour) → backfill = raw / gpu_count."""
        tmp = tempfile.mktemp(suffix=".db")

        # Simulate a legacy DB: create table without new columns, insert raw data
        con = sqlite3.connect(tmp)
        con.executescript("""
            CREATE TABLE gpu_price_observations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL DEFAULT '2025-01-01T00:00:00+00:00',
                scrape_run_id TEXT NOT NULL DEFAULT 'test',
                provider TEXT NOT NULL,
                gpu_model_raw TEXT,
                gpu_model_normalized TEXT NOT NULL,
                region TEXT,
                country TEXT,
                price_per_hour_usd REAL NOT NULL,
                currency TEXT NOT NULL DEFAULT 'USD',
                contract_type TEXT,
                availability_status INTEGER NOT NULL DEFAULT 1,
                vram_gb INTEGER,
                gpu_count INTEGER NOT NULL DEFAULT 1,
                source_url TEXT,
                confidence_score REAL NOT NULL DEFAULT 1.0,
                scrape_success INTEGER NOT NULL DEFAULT 1,
                error_message TEXT
            );
            CREATE TABLE scrape_runs (
                id TEXT PRIMARY KEY,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                total_offers INTEGER DEFAULT 0,
                providers_ok TEXT,
                providers_failed TEXT
            );
        """)
        # Insert legacy AWS row: $55.04/node, 8 GPUs
        con.execute("""
            INSERT INTO gpu_price_observations
            (provider, gpu_model_normalized, region, price_per_hour_usd,
             gpu_count, contract_type, scrape_success)
            VALUES ('AWS', 'H100 SXM', 'us-east-1', 55.04, 8, 'on-demand', 1)
        """)
        # Insert legacy RunPod row: $2.99/GPU, 1 GPU
        con.execute("""
            INSERT INTO gpu_price_observations
            (provider, gpu_model_normalized, region, price_per_hour_usd,
             gpu_count, contract_type, scrape_success)
            VALUES ('RunPod', 'H100 SXM', 'Global', 2.99, 1, 'on-demand', 1)
        """)
        con.commit()
        con.close()

        # Run migration
        db = PriceDatabase(tmp)
        db.init()

        con = sqlite3.connect(tmp)
        con.row_factory = sqlite3.Row
        aws = con.execute(
            "SELECT price_per_gpu_hour, price_unit FROM gpu_price_observations WHERE provider='AWS'"
        ).fetchone()
        runpod = con.execute(
            "SELECT price_per_gpu_hour, price_unit FROM gpu_price_observations WHERE provider='RunPod'"
        ).fetchone()
        con.close()

        assert aws["price_unit"] == "per_node"
        assert aws["price_per_gpu_hour"] == pytest.approx(55.04 / 8, rel=1e-4)
        assert runpod["price_unit"] == "per_gpu"
        assert runpod["price_per_gpu_hour"] == pytest.approx(2.99)

    def test_migration_idempotent(self):
        """Running init() twice must not corrupt data."""
        tmp = tempfile.mktemp(suffix=".db")
        db = PriceDatabase(tmp)
        db.init()
        offer = make_offer(provider="AWS", price_per_hour=55.04,
                           price_unit="per_node", gpu_count=8)
        run_id = db.start_run()
        db.save_offers([offer], run_id)
        db.finish_run(run_id, 1, ["AWS"], [])

        # Second init — must be a no-op
        db.init()

        con = sqlite3.connect(tmp)
        count = con.execute("SELECT COUNT(*) FROM gpu_price_observations").fetchone()[0]
        con.close()
        assert count == 1
