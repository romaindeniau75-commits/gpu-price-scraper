"""Arbitrage analytics and opportunity scoring over stored price observations."""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

import pandas as pd

from .storage import PriceDatabase

# ---------------------------------------------------------------------------
# Weights for opportunity score (must sum to 1.0)
# ---------------------------------------------------------------------------
_W_DISCOUNT   = 0.30  # how far below market median
_W_SPREAD     = 0.25  # total market spread → more room to arbitrage
_W_DEPTH      = 0.15  # number of live offers → market is real
_W_CONFIDENCE = 0.15  # provider data reliability
_W_CONTRACT   = 0.10  # contract-type reliability
_W_FRESHNESS  = 0.05  # data age

_CONTRACT_SCORE = {"on-demand": 1.0, "reserved": 0.80, "spot": 0.45}


def _clamp(x: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return max(lo, min(hi, x))


def _freshness(timestamp_iso: str) -> float:
    try:
        ts = datetime.fromisoformat(timestamp_iso.replace("Z", "+00:00"))
        age_h = (datetime.now(timezone.utc) - ts).total_seconds() / 3600
    except Exception:
        return 0.1
    if age_h < 1:   return 1.0
    if age_h < 6:   return 0.80
    if age_h < 24:  return 0.50
    if age_h < 72:  return 0.20
    return 0.05


def compute_opportunity_score(
    discount_pct: float,
    spread_pct: float,
    offer_count: int,
    provider_confidence: float,
    contract_type: str,
    data_age_hours: float,
) -> float:
    discount_score   = _clamp(discount_pct / 50)          # 50 % discount → max
    spread_score     = _clamp(spread_pct / 100)           # 100 % spread → max
    depth_score      = _clamp((offer_count - 1) / 9)      # 10 offers → max
    confidence_score = _clamp(provider_confidence)
    contract_score   = _CONTRACT_SCORE.get(contract_type, 0.50)
    fresh_score: float
    if data_age_hours < 1:   fresh_score = 1.0
    elif data_age_hours < 6: fresh_score = 0.80
    elif data_age_hours < 24:fresh_score = 0.50
    else:                    fresh_score = 0.10

    raw = (
        _W_DISCOUNT   * discount_score
        + _W_SPREAD     * spread_score
        + _W_DEPTH      * depth_score
        + _W_CONFIDENCE * confidence_score
        + _W_CONTRACT   * contract_score
        + _W_FRESHNESS  * fresh_score
    )
    return round(_clamp(raw), 4)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------

@dataclass
class MarketStats:
    gpu_model: str
    region_group: str        # "Global", "US", "EU", …
    contract_type: str
    min_price: float
    max_price: float
    avg_price: float
    median_price: float
    spread_abs: float
    spread_pct: float
    cheapest_provider: str
    most_expensive_provider: str
    offer_count: int


@dataclass
class ArbitrageOpportunity:
    gpu_model: str
    vram_gb: int
    region: str
    country: str
    buy_provider: str
    buy_price: float
    market_median: float
    market_min: float
    market_max: float
    spread_pct: float
    discount_abs: float
    discount_pct: float
    contract_type: str
    gpu_count: int
    availability: bool
    provider_confidence: float
    data_age_hours: float
    opportunity_score: float
    monthly_saving_vs_median: float   # USD saved per GPU per month vs median


# ---------------------------------------------------------------------------
# Main analytics class
# ---------------------------------------------------------------------------

class PriceAnalytics:
    def __init__(self, db: PriceDatabase) -> None:
        self.db = db

    # ---------------------------------------------------------------- helpers

    def _load_df(
        self,
        gpu_filter: Optional[str] = None,
        region_filter: Optional[str] = None,
        contract_filter: Optional[str] = None,
        hours: int = 24,
    ) -> pd.DataFrame:
        rows = self.db.get_latest_prices(
            gpu_filter=gpu_filter,
            region_filter=region_filter,
            contract_filter=contract_filter,
            hours=hours,
        )
        if not rows:
            return pd.DataFrame()
        df = pd.DataFrame([dict(r) for r in rows])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df["age_hours"] = (
            pd.Timestamp.now(tz="UTC") - df["timestamp"]
        ).dt.total_seconds() / 3600
        return df

    def _region_group(self, df: pd.DataFrame) -> pd.DataFrame:
        """Collapse fine-grained regions into coarse groups for comparison."""
        def _group(r: str) -> str:
            r = str(r).strip().lower()
            if r in ("us", "usa") or any(x in r for x in ("us-", "eastus", "westus", "united states", "us,", ", us", "us-ashburn", "us-phoenix")):
                return "US"
            if any(x in r for x in ("eu-", "europe", "westeurope", "czech", "german", "france", "uk", "nether")):
                return "EU"
            if any(x in r for x in ("ap-", "asia", "japan", "korea", "australia", "india", "singapore")):
                return "APAC"
            if "global" in r:
                return "Global"
            return r.split("/")[0].strip().title() if "/" in r else r.title()

        df = df.copy()
        df["region_group"] = df["region"].apply(_group)
        return df

    # ----------------------------------------------------------- market stats

    def get_market_stats(
        self,
        gpu_filter: Optional[str] = None,
        region_filter: Optional[str] = None,
        hours: int = 24,
    ) -> pd.DataFrame:
        """Aggregate price stats per (gpu_model, region_group, contract_type)."""
        df = self._load_df(gpu_filter=gpu_filter, region_filter=region_filter, hours=hours)
        if df.empty:
            return pd.DataFrame()

        df = self._region_group(df)

        # Deduplicate: keep cheapest per (provider, gpu, region, contract)
        df = (
            df.sort_values("price_per_hour_usd")
            .drop_duplicates(subset=["provider", "gpu_model_normalized", "region_group", "contract_type"])
        )

        records = []
        group_cols = ["gpu_model_normalized", "region_group", "contract_type"]
        for keys, g in df.groupby(group_cols):
            prices = g["price_per_hour_usd"].tolist()
            cheapest_row = g.loc[g["price_per_hour_usd"].idxmin()]
            priciest_row = g.loc[g["price_per_hour_usd"].idxmax()]
            lo, hi = min(prices), max(prices)
            med = statistics.median(prices)
            records.append({
                "gpu_model":              keys[0],
                "region_group":           keys[1],
                "contract_type":          keys[2],
                "min_price":              round(lo, 4),
                "max_price":              round(hi, 4),
                "avg_price":              round(statistics.mean(prices), 4),
                "median_price":           round(med, 4),
                "spread_abs":             round(hi - lo, 4),
                "spread_pct":             round((hi - lo) / lo * 100 if lo else 0, 2),
                "cheapest_provider":      cheapest_row["provider"],
                "most_expensive_provider":priciest_row["provider"],
                "offer_count":            len(prices),
            })

        out = pd.DataFrame(records)
        if not out.empty:
            out = out.sort_values(["gpu_model", "spread_pct"], ascending=[True, False])
        return out

    # ------------------------------------------------ arbitrage opportunities

    def find_opportunities(
        self,
        gpu_filter: Optional[str] = None,
        region_filter: Optional[str] = None,
        hours: int = 24,
        top_n: int = 30,
    ) -> pd.DataFrame:
        """Ranked arbitrage opportunities — cheapest offers vs market median."""
        df = self._load_df(gpu_filter=gpu_filter, region_filter=region_filter, hours=hours)
        if df.empty:
            return pd.DataFrame()

        df = self._region_group(df)

        opps = []
        group_cols = ["gpu_model_normalized", "region_group", "contract_type"]
        for keys, g in df.groupby(group_cols):
            prices = g["price_per_hour_usd"].tolist()
            if len(prices) < 2:
                continue

            lo, hi = min(prices), max(prices)
            med = statistics.median(prices)
            spread_pct = (hi - lo) / lo * 100 if lo else 0

            for _, row in g.iterrows():
                p = row["price_per_hour_usd"]
                if p >= med:
                    continue  # not cheaper than median → not an opportunity

                discount_abs = med - p
                discount_pct = discount_abs / med * 100 if med else 0
                age_h = float(row.get("age_hours", 24))

                score = compute_opportunity_score(
                    discount_pct=discount_pct,
                    spread_pct=spread_pct,
                    offer_count=len(prices),
                    provider_confidence=float(row.get("confidence_score", 0.7)),
                    contract_type=str(row.get("contract_type", "on-demand")),
                    data_age_hours=age_h,
                )

                opps.append({
                    "gpu_model":              row["gpu_model_normalized"],
                    "vram_gb":                int(row.get("vram_gb") or 0),
                    "region":                 row["region"],
                    "region_group":           keys[1],
                    "country":                row.get("country", ""),
                    "buy_provider":           row["provider"],
                    "buy_price":              round(p, 4),
                    "market_median":          round(med, 4),
                    "market_min":             round(lo, 4),
                    "market_max":             round(hi, 4),
                    "spread_pct":             round(spread_pct, 2),
                    "discount_abs":           round(discount_abs, 4),
                    "discount_pct":           round(discount_pct, 2),
                    "contract_type":          row.get("contract_type", "on-demand"),
                    "gpu_count":              int(row.get("gpu_count", 1)),
                    "availability":           bool(row.get("availability_status", 1)),
                    "provider_confidence":    float(row.get("confidence_score", 0.7)),
                    "data_age_hours":         round(age_h, 1),
                    "opportunity_score":      score,
                    "monthly_saving_vs_median": round(discount_abs * 720, 2),
                })

        if not opps:
            return pd.DataFrame()

        out = pd.DataFrame(opps).sort_values("opportunity_score", ascending=False)
        return out.head(top_n).reset_index(drop=True)

    # ------------------------------------------------- provider summary

    def get_provider_summary(
        self,
        gpu_filter: Optional[str] = None,
        hours: int = 24,
    ) -> pd.DataFrame:
        """Per-provider aggregate stats."""
        df = self._load_df(gpu_filter=gpu_filter, hours=hours)
        if df.empty:
            return pd.DataFrame()

        records = []
        for provider, g in df.groupby("provider"):
            prices = g["price_per_hour_usd"].tolist()
            records.append({
                "provider":          provider,
                "offer_count":       len(prices),
                "gpu_types":         g["gpu_model_normalized"].nunique(),
                "min_price":         round(min(prices), 4),
                "avg_price":         round(statistics.mean(prices), 4),
                "max_price":         round(max(prices), 4),
                "confidence_score":  round(float(g["confidence_score"].mean()), 3),
                "on_demand_count":   int((g["contract_type"] == "on-demand").sum()),
                "spot_count":        int((g["contract_type"] == "spot").sum()),
            })

        return pd.DataFrame(records).sort_values("min_price")

    # ------------------------------------------------- price history

    def get_price_history(
        self,
        gpu_filter: Optional[str] = None,
        hours: int = 168,
        bucket: str = "1h",
    ) -> pd.DataFrame:
        """Historical price aggregates bucketed by time."""
        rows = self.db.get_historical_prices(gpu_filter=gpu_filter, hours=hours)
        if not rows:
            return pd.DataFrame()

        df = pd.DataFrame([dict(r) for r in rows])
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp")

        records = []
        for (gpu, contract), g in df.groupby(["gpu_model_normalized", "contract_type"]):
            resampled = g["price_per_hour_usd"].resample(bucket).agg(["min", "median", "max", "count"])
            resampled = resampled.dropna(subset=["min"])
            for ts, row in resampled.iterrows():
                records.append({
                    "timestamp":    ts,
                    "gpu_model":    gpu,
                    "contract_type":contract,
                    "min_price":    round(row["min"], 4),
                    "median_price": round(row["median"], 4),
                    "max_price":    round(row["max"], 4),
                    "sample_count": int(row["count"]),
                })

        if not records:
            return pd.DataFrame()
        return pd.DataFrame(records).sort_values("timestamp")

    # ------------------------------------------------- atlas routing

    def find_best_routes(
        self,
        gpu_model: str,
        gpu_count: int = 1,
        contract_type: str = "on-demand",
        region_filter: Optional[str] = None,
        max_price_per_gpu: Optional[float] = None,
        hours: int = 24,
        top_n: int = 10,
    ) -> pd.DataFrame:
        """Ranked provider options for a specific workload requirement."""
        df = self._load_df(
            gpu_filter=gpu_model,
            region_filter=region_filter,
            contract_filter=contract_type if contract_type != "all" else None,
            hours=hours,
        )
        if df.empty:
            return pd.DataFrame()

        df = self._region_group(df)

        # Filter by GPU count — only nodes with enough GPUs
        df = df[df["gpu_count"] >= gpu_count].copy()

        if max_price_per_gpu:
            df = df[df["price_per_hour_usd"] <= max_price_per_gpu]

        if df.empty:
            return pd.DataFrame()

        # Deduplicate: cheapest per provider
        df = (
            df.sort_values("price_per_hour_usd")
            .drop_duplicates(subset=["provider", "gpu_model_normalized", "region_group"])
        )

        df["total_price_per_hour"] = df["price_per_hour_usd"] * gpu_count
        df["monthly_estimate"]     = df["total_price_per_hour"] * 720
        df["rank_score"] = (
            df["price_per_hour_usd"].rank(pct=True, ascending=True) * 0.6
            + df["confidence_score"].rank(pct=True, ascending=False) * 0.25
            + df["availability_status"].rank(pct=True, ascending=False) * 0.15
        )

        out_cols = [
            "provider", "gpu_model_normalized", "vram_gb", "region", "region_group",
            "contract_type", "gpu_count", "price_per_hour_usd",
            "total_price_per_hour", "monthly_estimate",
            "confidence_score", "availability_status", "age_hours",
        ]
        out = (
            df[[c for c in out_cols if c in df.columns]]
            .sort_values("price_per_hour_usd")
            .head(top_n)
            .reset_index(drop=True)
        )
        out.index = out.index + 1  # 1-based rank
        return out
