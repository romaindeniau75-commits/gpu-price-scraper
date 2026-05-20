#!/usr/bin/env python3
"""Generate docs/index.html and docs/data.json from data/pricing_history.db.

Tier layout
-----------
  Flagship  (B200 B300 H200 H100)  – 2-col wide cards, background sparkline
  Workhorse (A100 L40S L40  A40)   – 3-col cards, background sparkline
  Inference (L4 A10G A10 T4 RTX*)  – compact single-line table, no sparkline
  Legacy    (V100 P100 AMD RTX-ws)  – hidden, toggle button reveals

Form-factor merge
-----------------
  H100 SXM / PCIe / NVL   → single "H100" card  (variant chip next to provider)
  H200 SXM / NVL           → "H200"
  A100 80GB / 40GB / SXM  → "A100"

Outlier filtering
-----------------
  Any offer > 3× median for same (gpu_model, tier) is excluded from display
  and logged as a WARNING. Stays in DB untouched.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import median as _stats_median
from typing import Optional

# ---------------------------------------------------------------------------
ROOT       = Path(__file__).parent.parent
HISTORY_DB = ROOT / "data" / "pricing_history.db"
DOCS_DIR   = ROOT / "docs"

# ---------------------------------------------------------------------------
# GPU classification tables
# ---------------------------------------------------------------------------

# Ordered (pattern, base_name) — first match wins; order is critical!
_MERGE_PATTERNS: list[tuple[str, str]] = [
    # Integrated systems must be matched BEFORE their GPU-only counterparts
    ("GB300", "GB300"),  # Grace Blackwell 300 — CPU+GPU integrated system
    ("GH200", "GH200"),  # Grace Hopper 200 — CPU+GPU integrated system (future)
    ("MI300A","MI300A"), # AMD APU integrated system (future)
    ("B300",  "B300"),   ("B200",  "B200"),
    ("H200",  "H200"),   ("H100",  "H100"),
    ("A100",  "A100"),
    ("L40S",  "L40S"),   ("L40",   "L40"),    ("A40",   "A40"),
    ("A10G",  "A10G"),   ("A10",   "A10"),
    ("L4",    "L4"),     ("T4",    "T4"),
    ("MI355", "MI355X"), ("MI300", "MI300X"),
    ("V100",  "V100"),   ("P100",  "P100"),
    ("K80",   "K80"),    ("M60",   "M60"),
    # Workstation RTX (matched before generic RTX catch-all)
    ("A6000", "RTX A6000"), ("A5000", "RTX A5000"),
    ("A4500", "RTX A4500"), ("A4000", "RTX A4000"),
    ("A2000", "RTX A2000"),
    ("RTX",   "RTX"),    # gaming / prosumer catch-all
]

_FLAGSHIP_BASES  = frozenset({"H100", "H200", "B200", "B300"})
_SYSTEMS_BASES   = frozenset({"GB300", "GH200", "MI300A"})   # CPU+GPU integrated systems
_WORKHORSE_BASES = frozenset({"A100", "L40S", "L40", "A40"})
_INFERENCE_BASES = frozenset({"L4", "A10G", "A10", "T4"})
_LEGACY_BASES    = frozenset({
    "V100", "MI300X", "MI355X", "P100", "K80", "M60",
    "RTX A6000", "RTX A5000", "RTX A4500", "RTX A4000", "RTX A2000",
})
_RTX_CATCH_ALL   = "RTX"

# Models that are commercially available but pre-GA.
# Pricing may be volatile and provider coverage limited.
EARLY_ACCESS_MODELS: dict[str, str] = {
    "B300":  "Early access · pre-GA pricing",
    "GB300": "Early access · Grace Blackwell system",
}

# AWS EC2 exact instance SKU → (canonical GPU model, number of GPUs in that instance).
# AWS stores these SKUs as gpu_model in the DB; this table lets us translate them to
# real GPU names AND divide the node price by gpu_count to get $/GPU/hr.
AWS_SKU_TO_GPU: dict[str, tuple[str, int]] = {
    "g4dn.xlarge":   ("T4", 1),
    "g4dn.2xlarge":  ("T4", 1),
    "g4dn.4xlarge":  ("T4", 1),
    "g4dn.8xlarge":  ("T4", 1),
    "g4dn.16xlarge": ("T4", 1),
    "g4dn.12xlarge": ("T4", 4),
    "g4dn.metal":    ("T4", 8),
    "g5.xlarge":     ("A10G", 1),
    "g5.2xlarge":    ("A10G", 1),
    "g5.4xlarge":    ("A10G", 1),
    "g5.8xlarge":    ("A10G", 1),
    "g5.16xlarge":   ("A10G", 1),
    "g5.12xlarge":   ("A10G", 4),
    "g5.24xlarge":   ("A10G", 4),
    "g5.48xlarge":   ("A10G", 8),
    "g6.xlarge":     ("L4", 1),
    "g6.2xlarge":    ("L4", 1),
    "g6.4xlarge":    ("L4", 1),
    "g6.8xlarge":    ("L4", 1),
    "g6.16xlarge":   ("L4", 1),
    "g6.12xlarge":   ("L4", 4),
    "g6.24xlarge":   ("L4", 4),
    "g6.48xlarge":   ("L4", 8),
    "g6e.xlarge":    ("L40S", 1),
    "g6e.2xlarge":   ("L40S", 1),
    "g6e.4xlarge":   ("L40S", 1),
    "g6e.8xlarge":   ("L40S", 1),
    "g6e.16xlarge":  ("L40S", 1),
    "g6e.12xlarge":  ("L40S", 4),
    "g6e.24xlarge":  ("L40S", 4),
    "g6e.48xlarge":  ("L40S", 8),
    "p3.2xlarge":    ("V100", 1),
    "p3.8xlarge":    ("V100", 4),
    "p3.16xlarge":   ("V100", 8),
    "p3dn.24xlarge": ("V100", 8),
    "p4d.24xlarge":  ("A100 40GB", 8),
    "p4de.24xlarge": ("A100 80GB", 8),
    "p5.48xlarge":   ("H100", 8),
    "p5e.48xlarge":  ("H200", 8),
    "p6.48xlarge":   ("B200", 8),
}

# A GPU base must appear at ≥ this many distinct providers in the last 7 days
# to be shown in flagship / workhorse / inference (guards against phantom models).
# Systems section is curated and exempt from this filter.
MIN_PROVIDERS_LAST_7D: int = 2

# Providers whose RTX gaming cards count as datacenter inference
_DC_PROVIDERS = frozenset({
    "RunPod", "RunPod Community", "Vast.ai", "TensorDock",
})

_SPOT_TIERS  = frozenset({"spot", "interruptible", "community"})
_SKIP_REGION = frozenset({"unknown", "global", ""})

# Hard floor/ceiling per GPU base family — catches single-point outliers the
# median filter misses. Prices in $/GPU/hr.
_PLAUSIBILITY_BOUNDS: dict[str, tuple[float, float]] = {
    "B300":    (5.00, 30.00),
    "B200":    (5.00, 30.00),
    "H200":    (2.50, 20.00),
    "H100":    (1.00, 12.00),
    "A100":    (0.80,  8.00),
    "L40S":    (0.50,  6.00),
    "L40":     (0.40,  5.00),
    "A40":     (0.30,  4.00),
    "A10G":    (0.20,  3.00),
    "A10":     (0.20,  3.00),
    "L4":      (0.10,  2.50),
    "T4":      (0.10,  2.00),
    "MI355X":  (2.00, 15.00),
    "MI300X":  (1.50, 12.00),
    "V100":    (0.30,  4.00),
    "P100":    (0.10,  2.50),
}

# Flagship GPU families included in the trend chart (after form-factor merge).
# B300 is included: it has confirmed GA availability across 3+ providers.
FLAGSHIP_TREND_MODELS = {"H100", "H200", "B200", "B300"}

_CARD_COLORS = [
    "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6",
    "#EC4899", "#06B6D4", "#F97316", "#84CC16", "#6366F1",
    "#14B8A6", "#E879F9",
]

# Sort orders within each section
_FLAG_ORDER = ["B300", "B200", "H200", "H100"]
_WORK_ORDER = ["A100", "L40S", "L40", "A40"]
_INF_ORDER  = ["L4", "A10G", "A10", "T4", "RTX"]
_SYS_ORDER  = ["GB300", "GH200", "MI300A"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    if not HISTORY_DB.exists():
        sys.exit(f"ERROR: {HISTORY_DB} not found. Run: python3 -m gpu_scraper.cli fetch --save-db")
    con = sqlite3.connect(str(HISTORY_DB))
    con.row_factory = sqlite3.Row
    return con


def _latest_date(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT MAX(snapshot_date) FROM price_history").fetchone()
    return row[0] or ""


def _gpu_base(gpu_model: str) -> Optional[str]:
    """Map original gpu_model name to a canonical base family name, or None."""
    upper = gpu_model.upper()
    for pat, base in _MERGE_PATTERNS:
        if pat.upper() in upper:
            return base
    return None  # unrecognised → completely excluded


def _gpu_variant(gpu_model: str) -> str:
    """Extract the most meaningful variant tag from the original name."""
    upper = gpu_model.upper()
    for pat, tag in [
        ("SXM5", "SXM5"), ("SXM4", "SXM4"), ("SXM", "SXM"),
        ("NVL",  "NVL"),  ("PCIE", "PCIe"),
        ("80GB", "80GB"), ("40GB", "40GB"),
        ("16GB", "16GB"), ("8GB",  "8GB"),
    ]:
        if pat in upper:
            return tag
    return ""


def _gpu_tier(base: Optional[str], providers: frozenset[str]) -> Optional[str]:
    if base is None:                     return None
    if base in _SYSTEMS_BASES:           return "systems"
    if base in _FLAGSHIP_BASES:          return "flagship"
    if base in _WORKHORSE_BASES:         return "workhorse"
    if base in _INFERENCE_BASES:         return "inference"
    if base in _LEGACY_BASES:            return "legacy"
    if base == _RTX_CATCH_ALL:
        return "inference" if (providers & _DC_PROVIDERS) else "legacy"
    return None  # e.g. AWS instance type names → excluded


def _tier_order(base: str, order: list[str]) -> int:
    try:
        return order.index(base)
    except ValueError:
        return len(order)


def _fmt(p: Optional[float]) -> str:
    return f"${p:.4f}" if p is not None else "—"


def _meta(provider: str, region: str = "", commitment_term: str = "",
          is_reserved: bool = False) -> str:
    """Format provider + secondary info, skipping uninformative regions."""
    parts = [provider]
    if is_reserved and commitment_term:
        parts.append(commitment_term)
    elif region and region.lower() not in _SKIP_REGION:
        parts.append(region)
    return " · ".join(parts)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _is_plausible(price: float, base: Optional[str], tier: str) -> bool:
    """Return True if price is within plausibility bounds for this GPU + tier."""
    if base is None or base not in _PLAUSIBILITY_BOUNDS:
        return True
    lo, hi = _PLAUSIBILITY_BOUNDS[base]
    effective_lo = lo if tier in ("on_demand", "reserved") else lo * 0.30
    return effective_lo <= price <= hi


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------

def _build_raw_offers(con: sqlite3.Connection, ld: str) -> list[dict]:
    """Query all offers for latest date, remove statistical outliers."""
    rows = con.execute(
        """
        SELECT gpu_model, provider, region_canonical,
               availability_tier, price_per_gpu_hour, commitment_term
        FROM   price_history
        WHERE  snapshot_date = ?
        ORDER  BY gpu_model, availability_tier, price_per_gpu_hour
        """,
        (ld,),
    ).fetchall()
    offers = [dict(r) for r in rows]

    # Compute median per (gpu_model, tier)
    price_groups: dict[tuple, list[float]] = defaultdict(list)
    for o in offers:
        price_groups[(o["gpu_model"], o["availability_tier"])].append(o["price_per_gpu_hour"])

    clean: list[dict] = []
    for o in offers:
        price = o["price_per_gpu_hour"]
        base  = _gpu_base(o["gpu_model"])

        # 1 — Plausibility bounds (catches single-point outliers).
        if not _is_plausible(price, base, o["availability_tier"]):
            if base and base in _PLAUSIBILITY_BOUNDS:
                lo, hi = _PLAUSIBILITY_BOUNDS[base]
                eff = lo if o["availability_tier"] in ("on_demand", "reserved") else lo * 0.30
                print(
                    f"WARNING plausibility excluded: {o['gpu_model']} @ {o['provider']} "
                    f"${price:.4f}/hr (bounds=[${eff:.2f}, ${hi:.2f}])"
                )
            continue

        # 2 — Statistical outlier filter: >3× median within same (model, tier)
        key   = (o["gpu_model"], o["availability_tier"])
        group = price_groups[key]
        if len(group) >= 2:
            med       = _stats_median(group)
            threshold = 3.0 * med
            if price > threshold:
                print(
                    f"WARNING outlier excluded: {o['gpu_model']} @ {o['provider']} "
                    f"${price:.4f}/hr "
                    f"(median=${med:.4f}, 3× threshold=${threshold:.4f})"
                )
                continue

        # 3 — Informational note: OCI B300/GB300 runs ~2× peers (OCI premium pricing).
        #     The spread is within plausibility bounds; no exclusion, just visibility.
        if o["provider"] == "OCI" and base in {"B300", "GB300"} and price >= 10.0:
            print(
                f"INFO OCI {base} ${price:.2f}/hr — OCI premium pricing "
                f"(~2× peers ~$7/hr); within plausibility bounds [{base}], keeping."
            )

        clean.append(o)

    return clean


def _build_gpu_groups(
    offers: list[dict],
    qualifying_bases: Optional[frozenset] = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict], list[dict]]:
    """
    Aggregate offers by base GPU family, returning five sorted lists:
    (flagship, workhorse, inference, legacy, systems)

    qualifying_bases: frozenset of base GPU names that pass the
    MIN_PROVIDERS_LAST_7D filter.  Applied to flagship/workhorse/inference
    only; legacy and systems are exempt.  Pass None to skip filtering.
    """
    # Step 1 — inventory which providers offer each base GPU
    base_providers: dict[str, set[str]] = defaultdict(set)
    for o in offers:
        base = _gpu_base(o["gpu_model"])
        if base:
            base_providers[base].add(o["provider"])

    # Step 2 — group offers by (base, slot) keeping best (lowest) price
    #           also record the variant tag from the winning offer
    Slot = dict  # type alias for clarity
    base_slots: dict[str, dict[str, Slot]] = defaultdict(dict)

    for o in offers:
        base = _gpu_base(o["gpu_model"])
        if base is None:
            continue
        tier = o["availability_tier"]
        if tier == "on_demand":       slot = "on_demand"
        elif tier in _SPOT_TIERS:     slot = "spot"
        elif tier == "reserved":      slot = "reserved"
        else:                         continue

        price = o["price_per_gpu_hour"]
        existing = base_slots[base].get(slot)
        if existing is None or price < existing["price"]:
            base_slots[base][slot] = {
                "price":           price,
                "provider":        o["provider"],
                "region":          o["region_canonical"],
                "variant":         _gpu_variant(o["gpu_model"]),
                "commitment_term": o["commitment_term"] or "",
            }

    # Step 3 — classify into tiers and sort
    buckets: dict[str, list[dict]] = {
        "flagship": [], "workhorse": [], "inference": [], "legacy": [], "systems": [],
    }

    for base, slots in base_slots.items():
        providers = frozenset(base_providers[base])
        tier_cls  = _gpu_tier(base, providers)
        if tier_cls is None:
            continue

        # Min-providers guard: flagship/workhorse/inference only (systems + legacy exempt)
        if (
            qualifying_bases is not None
            and tier_cls not in ("systems", "legacy")
            and base not in qualifying_bases
        ):
            print(
                f"INFO min-providers excluded: {base} "
                f"(< {MIN_PROVIDERS_LAST_7D} providers in last 7 days)"
            )
            continue

        od = slots.get("on_demand")
        sp = slots.get("spot")
        rv = slots.get("reserved")

        spot_disc: Optional[float] = None
        if od and sp and od["price"] > 0 and od["price"] > sp["price"]:
            spot_disc = round((od["price"] - sp["price"]) / od["price"] * 100, 1)

        buckets[tier_cls].append({
            "base_gpu":          base,
            "on_demand":         od,
            "spot":              sp,
            "reserved":          rv,
            "spot_discount_pct": spot_disc,
            "sparkline":         {"dates": [], "prices": []},  # filled by _attach_sparklines
        })

    # Sort each bucket by predefined order
    for order_list, bucket_key in [
        (_FLAG_ORDER, "flagship"),
        (_WORK_ORDER, "workhorse"),
        (_INF_ORDER,  "inference"),
        (_SYS_ORDER,  "systems"),
    ]:
        buckets[bucket_key].sort(key=lambda g: _tier_order(g["base_gpu"], order_list))
    # Legacy: alphabetical
    buckets["legacy"].sort(key=lambda g: g["base_gpu"])

    return (
        buckets["flagship"],
        buckets["workhorse"],
        buckets["inference"],
        buckets["legacy"],
        buckets["systems"],
    )


def _build_sparklines(con: sqlite3.Connection) -> dict[str, dict]:
    """30-day on-demand sparklines aggregated by base GPU family."""
    rows = con.execute(
        """
        SELECT gpu_model, snapshot_date, MIN(price_per_gpu_hour) AS min_price
        FROM   price_history
        WHERE  availability_tier = 'on_demand'
          AND  snapshot_date >= date('now', '-30 days')
        GROUP  BY gpu_model, snapshot_date
        ORDER  BY snapshot_date
        """
    ).fetchall()

    # Aggregate by (base, date) taking minimum across variants
    by_base_date: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(lambda: float("inf")))
    for r in rows:
        base = _gpu_base(r["gpu_model"])
        if base:
            d = r["snapshot_date"]
            by_base_date[base][d] = min(by_base_date[base][d], r["min_price"])

    result: dict[str, dict] = {}
    for base, date_map in by_base_date.items():
        dates  = sorted(date_map)
        prices = [round(date_map[d], 4) for d in dates]
        result[base] = {"dates": dates, "prices": prices}
    return result


def _attach_sparklines(groups: list[dict], sl: dict[str, dict]) -> None:
    for g in groups:
        g["sparkline"] = sl.get(g["base_gpu"], {"dates": [], "prices": []})


def _build_hero_stats(
    con: sqlite3.Connection,
    flagship: list[dict],
    workhorse: list[dict],
    inference: list[dict],
    legacy: list[dict],
    systems: list[dict] = [],
) -> dict:
    row = con.execute(
        """
        SELECT COUNT(DISTINCT snapshot_date) AS day_count,
               COUNT(*)                      AS total_rows
        FROM   price_history
        """
    ).fetchone()

    ld = _latest_date(con)
    prov_count, _ = _build_active_providers(con)  # normalised: RunPod Community → RunPod

    # Median spot discount from flagship + workhorse + systems
    discounts = [
        g["spot_discount_pct"]
        for g in (flagship + workhorse + systems)
        if g["spot_discount_pct"] is not None
    ]
    median_disc: Optional[float] = None
    if discounts:
        ds = sorted(discounts)
        n  = len(ds)
        median_disc = round(
            ds[n // 2] if n % 2 else (ds[n // 2 - 1] + ds[n // 2]) / 2,
            1,
        )

    total_bases = sum(len(b) for b in [flagship, workhorse, inference, legacy, systems])

    return {
        "gpu_count":            total_bases,
        "provider_count":       prov_count,
        "day_count":            row["day_count"],
        "total_rows":           row["total_rows"],
        "median_spot_discount": median_disc,
        "latest_date":          ld,
    }


def _build_movers(con: sqlite3.Connection, day_count: int) -> dict:
    if day_count < 7:
        return {"insufficient": True, "have_days": day_count}
    ld = _latest_date(con)
    rows = con.execute(
        """
        WITH now_p AS (
            SELECT gpu_model, AVG(price_per_gpu_hour) AS price
            FROM   price_history
            WHERE  snapshot_date = ? AND availability_tier = 'on_demand'
            GROUP  BY gpu_model
        ),
        old_p AS (
            SELECT gpu_model, AVG(price_per_gpu_hour) AS price
            FROM   price_history
            WHERE  snapshot_date <= date(?, '-7 days')
              AND  availability_tier = 'on_demand'
            GROUP  BY gpu_model
        )
        SELECT
            n.gpu_model,
            ROUND(n.price, 4)                                  AS now_price,
            ROUND(o.price, 4)                                  AS old_price,
            ROUND((n.price - o.price) / o.price * 100, 1)     AS pct_change
        FROM now_p n JOIN old_p o ON n.gpu_model = o.gpu_model
        ORDER BY ABS(pct_change) DESC
        LIMIT 5
        """,
        (ld, ld),
    ).fetchall()
    return {"insufficient": False, "movers": [dict(r) for r in rows]}


def _build_spread_leaders(con: sqlite3.Connection) -> list[dict]:
    ld = _latest_date(con)
    if not ld:
        return []
    rows = con.execute(
        """
        WITH od AS (
            SELECT gpu_model, MIN(price_per_gpu_hour) AS price
            FROM   price_history
            WHERE  snapshot_date = ? AND availability_tier = 'on_demand'
            GROUP  BY gpu_model
        ),
        sp AS (
            SELECT gpu_model, MIN(price_per_gpu_hour) AS price
            FROM   price_history
            WHERE  snapshot_date = ?
              AND  availability_tier IN ('spot', 'interruptible')
            GROUP  BY gpu_model
        )
        SELECT
            od.gpu_model,
            ROUND(od.price, 4) AS od_price,
            ROUND(sp.price, 4) AS sp_price,
            ROUND((od.price - sp.price) / od.price * 100, 1) AS spread_pct
        FROM od JOIN sp ON od.gpu_model = sp.gpu_model
        WHERE od.price > sp.price
        ORDER BY spread_pct DESC
        LIMIT 5
        """,
        (ld, ld),
    ).fetchall()
    return [dict(r) for r in rows]


def _aws_sku_to_gpu(sku: str) -> Optional[tuple[str, int]]:
    """Translate an AWS EC2 instance SKU to (gpu_model, gpu_count), or None.

    Exact match first (g6.2xlarge → ("L4", 1)).
    Falls back to longest-prefix match for any unknown size variants.
    Returns None for unrecognised SKUs.
    """
    key = sku.lower()
    # 1 — exact match
    if key in AWS_SKU_TO_GPU:
        return AWS_SKU_TO_GPU[key]
    # 2 — longest-prefix fallback (handles any size not in the explicit table)
    family = key.split(".")[0]  # e.g. "g6"
    best: Optional[tuple[str, int]] = None
    best_len = 0
    for exact_sku, val in AWS_SKU_TO_GPU.items():
        prefix = exact_sku.split(".")[0]  # e.g. "g6" from "g6.2xlarge"
        if family == prefix and len(prefix) > best_len:
            best = val
            best_len = len(prefix)
    # Return with gpu_count=1 for unknown sizes (conservative)
    if best is not None:
        return (best[0], 1)
    return None


def _build_active_providers(con: sqlite3.Connection) -> tuple[int, list[str]]:
    """Return (count, sorted_list) of active providers over the last 7 days.

    'RunPod Community' is normalised to 'RunPod' so it is not double-counted.
    """
    rows = con.execute(
        """
        SELECT DISTINCT
            CASE WHEN provider = 'RunPod Community' THEN 'RunPod' ELSE provider END AS prov
        FROM price_history
        WHERE snapshot_date >= date('now', '-7 days')
        ORDER BY prov
        """
    ).fetchall()
    providers = [r[0] for r in rows]
    return len(providers), providers


def _build_min_provider_bases(
    con: sqlite3.Connection,
    min_providers: int = MIN_PROVIDERS_LAST_7D,
) -> frozenset:
    """Return base GPU names with ≥ min_providers distinct providers in last 7 days."""
    rows = con.execute(
        """
        SELECT gpu_model, COUNT(DISTINCT provider) AS n
        FROM   price_history
        WHERE  snapshot_date >= date('now', '-7 days')
        GROUP  BY gpu_model
        HAVING n >= ?
        """,
        (min_providers,),
    ).fetchall()
    result: set[str] = set()
    for r in rows:
        base = _gpu_base(r["gpu_model"])
        if base:
            result.add(base)
    return frozenset(result)


def compute_flagship_trend(con: sqlite3.Connection) -> list[dict]:
    """Compute per-date average on-demand and spot prices for FLAGSHIP_TREND_MODELS.

    Returns a list sorted by date:
      [{"date": "2026-05-10", "avg_on_demand": 3.12, "avg_spot": 1.05}, ...]
    Prices are plausibility-filtered (same rules as the main display).
    """
    rows = con.execute(
        """
        SELECT snapshot_date, gpu_model, availability_tier, price_per_gpu_hour
        FROM   price_history
        ORDER  BY snapshot_date
        """
    ).fetchall()

    date_od: dict[str, list[float]] = defaultdict(list)
    date_sp: dict[str, list[float]] = defaultdict(list)

    for r in rows:
        base  = _gpu_base(r["gpu_model"])
        if base not in FLAGSHIP_TREND_MODELS:
            continue
        price = r["price_per_gpu_hour"]
        tier  = r["availability_tier"]
        if not _is_plausible(price, base, tier):
            continue
        date  = r["snapshot_date"]
        if tier == "on_demand":
            date_od[date].append(price)
        elif tier in _SPOT_TIERS:
            date_sp[date].append(price)

    all_dates = sorted(set(list(date_od) + list(date_sp)))
    result: list[dict] = []
    for date in all_dates:
        od_prices = date_od.get(date, [])
        sp_prices = date_sp.get(date, [])
        avg_od = round(sum(od_prices) / len(od_prices), 4) if od_prices else None
        avg_sp = round(sum(sp_prices) / len(sp_prices), 4) if sp_prices else None
        result.append({"date": date, "avg_on_demand": avg_od, "avg_spot": avg_sp})

    return result


# ---------------------------------------------------------------------------
# HTML fragment builders
# ---------------------------------------------------------------------------

def _tier_block_html(label: str, cls: str, info: Optional[dict],
                     is_reserved: bool = False) -> str:
    if info is None:
        return (
            f'<div class="tb tb-empty">'
            f'<span class="chip {cls}">{label}</span>'
            f'<div class="tb-price tb-na">—</div>'
            f'<div class="tb-meta">Not available</div>'
            f"</div>"
        )
    price   = _fmt(info["price"])
    variant = info.get("variant") or ""
    prov    = info.get("provider") or ""
    region  = info.get("region") or ""
    ct      = info.get("commitment_term") or ""
    meta    = _meta(prov, region, ct, is_reserved)
    var_chip = f'<span class="var-chip">{variant}</span>' if variant else ""
    return (
        f'<div class="tb">'
        f'<span class="chip {cls}">{label}</span>'
        f'<div class="tb-price">{price}<span class="per-hr">/hr</span></div>'
        f'<div class="tb-meta">{meta}{var_chip}</div>'
        f"</div>"
    )


def _card_html(g: dict, idx: int, card_cls: str) -> str:
    """Shared HTML for flagship and workhorse cards (background sparkline)."""
    gpu      = g["base_gpu"]
    disc     = g["spot_discount_pct"]
    sl       = g["sparkline"]
    color    = _CARD_COLORS[idx % len(_CARD_COLORS)]
    cid      = "bgc_" + gpu.replace(" ", "_").replace("/", "_")

    od_html = _tier_block_html("On-Demand",   "chip-od",       g["on_demand"])
    sp_html = _tier_block_html("Spot / Int.", "chip-spot",     g["spot"])
    # Reserved: only rendered when data actually exists — keeps cards clean in the 99% case
    rv_html = (
        _tier_block_html("Reserved", "chip-reserved", g["reserved"], is_reserved=True)
        if g["reserved"] is not None else ""
    )

    banner = (
        f'<div class="spot-banner">Spot discount: &minus;{disc:.1f}%</div>'
        if disc is not None else ""
    )

    dates  = sl.get("dates", [])
    prices = sl.get("prices", [])
    if len(prices) >= 2:
        series = json.dumps({"labels": dates, "data": prices})
        bg_layer = (
            f"<canvas id='{cid}' class='sparkline-bg' height='0' "
            f"data-series='{series}' data-color='{color}'></canvas>"
        )
        hist_hint = ""
    else:
        bg_layer  = ""
        hist_hint = (
            f'<div class="hist-hint">Building history&hellip; {len(prices)}/30 days</div>'
        )

    # Early-access badge (B300, GB300 …)
    ea_badge = ""
    if gpu in EARLY_ACCESS_MODELS:
        desc = EARLY_ACCESS_MODELS[gpu]
        ea_badge = f'<span class="ea-badge" title="{desc}">EARLY ACCESS</span>'

    return (
        f'<div class="{card_cls}">'
        f"{bg_layer}"
        f'<div class="card-content">'
        f'<div class="card-header"><span class="gpu-name">{gpu}</span>{ea_badge}</div>'
        f'<div class="tier-blocks">{od_html}{sp_html}{rv_html}</div>'
        f"{banner}"
        f"{hist_hint}"
        f"</div>"
        f"</div>"
    )


def _inference_row_html(g: dict, show_reserved: bool = True) -> str:
    gpu = g["base_gpu"]
    od  = g["on_demand"]
    sp  = g["spot"]
    rv  = g["reserved"]

    def _cell(info: Optional[dict], is_reserved: bool = False) -> str:
        if info is None:
            return '<span class="inf-na">—</span>'
        price   = _fmt(info["price"])
        variant = info.get("variant") or ""
        prov    = info.get("provider") or ""
        region  = info.get("region") or ""
        ct      = info.get("commitment_term") or ""
        meta    = _meta(prov, region, ct, is_reserved)
        var_s   = f' <span class="var-chip">{variant}</span>' if variant else ""
        return (
            f'<span class="inf-price">{price}</span>'
            f'<span class="inf-meta">{meta}{var_s}</span>'
        )

    rv_cell = f'<div class="inf-cell">{_cell(rv, is_reserved=True)}</div>' if show_reserved else ""
    return (
        f'<div class="inf-row">'
        f'<span class="inf-gpu">{gpu}</span>'
        f'<div class="inf-cell">{_cell(od)}</div>'
        f'<div class="inf-cell">{_cell(sp)}</div>'
        f'{rv_cell}'
        f"</div>"
    )


def _legacy_item_html(g: dict) -> str:
    gpu = g["base_gpu"]
    od  = g["on_demand"]
    sp  = g["spot"]
    od_s = _fmt(od["price"]) if od else "—"
    sp_s = _fmt(sp["price"]) if sp else "—"
    return (
        f'<div class="leg-row">'
        f'<span class="leg-gpu">{gpu}</span>'
        f'<span class="leg-price">OD {od_s}</span>'
        f'<span class="leg-price">Spot {sp_s}</span>'
        f"</div>"
    )


def _render_movers(movers_data: dict) -> str:
    if movers_data.get("insufficient"):
        have = movers_data.get("have_days", 0)
        return (
            f'<p class="notice-msg">Insufficient history '
            f"(need 7 days, have {have})</p>"
        )
    movers = movers_data.get("movers", [])
    if not movers:
        return '<p class="notice-msg">No mover data available yet.</p>'
    parts = []
    for m in movers:
        pct  = float(m["pct_change"])
        cls  = "val-up" if pct > 0 else ("val-down" if pct < 0 else "val-flat")
        sign = "+" if pct > 0 else ""
        # Map to base GPU for display
        base = _gpu_base(m["gpu_model"]) or m["gpu_model"]
        parts.append(
            f'<div class="mover-row">'
            f'<span class="mover-gpu">{base}</span>'
            f'<span class="mover-trail">'
            f'<span class="mover-prices">'
            f'{_fmt(m["old_price"])} &rarr; {_fmt(m["now_price"])}</span>'
            f'<span class="mover-pct {cls}">{sign}{pct:.1f}%</span>'
            f"</span></div>"
        )
    return "\n".join(parts)


def _render_spread_leaders(leaders: list[dict]) -> str:
    """Render Market Spread Leaders widget.

    For AWS multi-GPU instances the node price is divided by gpu_count so
    each row reflects the per-GPU price.  Rows that resolve to the same GPU
    model are deduplicated (highest spread wins).  Top-5 distinct GPU models.
    """
    if not leaders:
        return '<p class="notice-msg">No spot offers available for comparison.</p>'

    # 1 — Resolve GPU name + normalise price to per-GPU/hr
    resolved: list[dict] = []
    for lead in leaders:
        raw_model = lead["gpu_model"]
        # Canonical base name first (non-AWS rows)
        base = _gpu_base(raw_model)
        gpu_count = 1
        if base is None:
            # Try AWS SKU → (model, count)
            aws = _aws_sku_to_gpu(raw_model)
            if aws is None:
                continue  # unrecognised — skip
            base, gpu_count = aws

        od_per_gpu = lead["od_price"] / gpu_count
        sp_per_gpu = lead["sp_price"] / gpu_count
        if od_per_gpu <= sp_per_gpu or od_per_gpu <= 0:
            continue  # spread inverted after division — skip
        spread_pct = round((od_per_gpu - sp_per_gpu) / od_per_gpu * 100, 1)

        resolved.append({
            "base":       base,
            "od_price":   od_per_gpu,
            "sp_price":   sp_per_gpu,
            "spread_pct": spread_pct,
            "gpu_count":  gpu_count,
        })

    # 2 — Deduplicate: keep highest spread per GPU model
    best: dict[str, dict] = {}
    for r in resolved:
        key = r["base"]
        if key not in best or r["spread_pct"] > best[key]["spread_pct"]:
            best[key] = r

    # 3 — Sort by spread descending, take top 5
    top5 = sorted(best.values(), key=lambda x: -x["spread_pct"])[:5]

    if not top5:
        return '<p class="notice-msg">No spot offers available for comparison.</p>'

    parts = []
    for r in top5:
        parts.append(
            f'<div class="spread-row">'
            f'<div class="spread-pct">{r["spread_pct"]:.0f}%</div>'
            f'<div class="spread-body">'
            f'<div class="spread-gpu">{r["base"]}</div>'
            f'<div class="spread-prices">'
            f'<span class="spread-od">{_fmt(r["od_price"])} on-demand</span>'
            f'<span class="spread-arr"> &rarr; </span>'
            f'<span class="spread-sp">{_fmt(r["sp_price"])} spot</span>'
            f"</div></div></div>"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CSS  (plain string — NOT an f-string so curly braces need no escaping)
# ---------------------------------------------------------------------------

CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg0:#0A1628;--bg1:#0D1B3D;
  --card:#0F1F3D;
  --bd:rgba(59,130,246,.20);--bd-hi:rgba(59,130,246,.50);
  --accent:#3B82F6;--cyan:#06B6D4;--indigo:#6366F1;
  --green:#10B981;--red:#EF4444;--amber:#F59E0B;
  --t1:#FFFFFF;--t2:#94A3B8;--t3:#4B5E7A;
  --glow:0 0 24px rgba(59,130,246,.35);--r:16px;
}
html{
  background:linear-gradient(150deg,var(--bg0) 0%,var(--bg1) 100%);
  background-attachment:fixed;color:var(--t1);
  font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;
  font-size:15px;line-height:1.5;min-height:100vh;
}
.page{max-width:1600px;margin:0 auto;padding:0 28px 80px}

/* ── Header ─────────────────────────────────────────────────────────── */
.site-hdr{
  display:flex;align-items:center;justify-content:space-between;
  padding:24px 28px 20px;max-width:1600px;margin:0 auto 36px;
  border-bottom:1px solid var(--bd);
}
.hdr-left{display:flex;align-items:center;gap:14px}
.logo-dot{
  width:10px;height:10px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 14px rgba(59,130,246,.8);flex-shrink:0;
}
.logo-brand{font-size:1.25rem;font-weight:800;letter-spacing:-.4px}
.logo-sub{font-size:.78rem;color:var(--t2);margin-top:2px}
.atlas-wm{font-size:1rem;font-weight:900;letter-spacing:.25em;color:var(--accent);text-transform:uppercase}
.snap-date{font-size:.72rem;color:var(--t3);text-align:right;margin-top:3px}

/* ── Hero stats ─────────────────────────────────────────────────────── */
.hero{display:flex;flex-wrap:wrap;gap:14px;margin-bottom:44px}
.stat-pill{
  background:var(--card);border:1px solid var(--bd);border-radius:12px;
  padding:14px 22px;display:flex;flex-direction:column;gap:5px;min-width:148px;
}
.sv{
  font-size:1.65rem;font-weight:800;
  background:linear-gradient(90deg,var(--accent),var(--cyan));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.sv.kpi{background:linear-gradient(90deg,var(--cyan),var(--green));-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.sl{font-size:.68rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;color:var(--t2)}

/* ── Section headings ───────────────────────────────────────────────── */
.sec{margin-bottom:52px}
.sec-hdr{display:flex;align-items:center;gap:12px;margin-bottom:22px}
.sec-label{font-size:.65rem;font-weight:800;letter-spacing:.14em;text-transform:uppercase;color:var(--accent);white-space:nowrap}
.sec-rule{flex:1;height:1px;background:var(--bd)}
.sec-sub{font-size:.7rem;color:var(--t3);white-space:nowrap}

/* ── Card grids ─────────────────────────────────────────────────────── */
.flag-grid{display:grid;grid-template-columns:repeat(2,1fr);gap:20px}
.work-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:16px}
@media(max-width:1100px){.work-grid{grid-template-columns:repeat(2,1fr)}}
@media(max-width:800px){.flag-grid,.work-grid{grid-template-columns:1fr}}

/* ── GPU cards (flagship + workhorse) ───────────────────────────────── */
.flag-card,.work-card{
  background:var(--card);border:1px solid var(--bd);border-radius:var(--r);
  position:relative;overflow:hidden;
  transition:border-color .2s,box-shadow .2s;
}
.flag-card:hover,.work-card:hover{border-color:var(--bd-hi);box-shadow:var(--glow)}
.flag-card{min-height:200px}
.card-content{position:relative;z-index:1;padding:20px}
.flag-card .card-content{padding:22px}

/* ── Background sparkline canvas ────────────────────────────────────── */
.sparkline-bg{
  position:absolute;inset:0;width:100%!important;height:100%!important;
  opacity:.13;pointer-events:none;z-index:0;
}

.card-header{
  display:flex;align-items:flex-start;justify-content:space-between;
  gap:10px;margin-bottom:14px;
}
.gpu-name{font-size:1.1rem;font-weight:700;color:var(--t1);line-height:1.2}
.flag-card .gpu-name{font-size:1.25rem}
.ea-badge{
  background:rgba(245,158,11,.12);border:1px solid rgba(245,158,11,.4);
  color:#F59E0B;border-radius:6px;padding:2px 8px;
  font-size:10px;letter-spacing:1px;text-transform:uppercase;font-weight:600;
  white-space:nowrap;flex-shrink:0;cursor:default;line-height:1.6;
}

/* ── Tier blocks ────────────────────────────────────────────────────── */
.tier-blocks{display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:8px;margin-bottom:12px}
.tb{
  background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);
  border-radius:10px;padding:10px 10px 9px;display:flex;flex-direction:column;gap:5px;min-width:0;
}
.tb-empty{opacity:.4}

/* Chips */
.chip{display:inline-block;font-size:.6rem;font-weight:800;letter-spacing:.08em;
  text-transform:uppercase;border-radius:5px;padding:2px 6px;line-height:1.5;white-space:nowrap}
.chip-od      {background:rgba(59,130,246,.15);color:#3B82F6;border:1px solid rgba(59,130,246,.3)}
.chip-spot    {background:rgba(6,182,212,.15); color:#06B6D4;border:1px solid rgba(6,182,212,.3)}
.chip-reserved{background:rgba(99,102,241,.15);color:#6366F1;border:1px solid rgba(99,102,241,.3)}

.tb-price{font-size:1.05rem;font-weight:700;color:var(--t1);line-height:1}
.tb-na{color:var(--t3)}
.per-hr{font-size:.7rem;font-weight:400;color:var(--t2);margin-left:1px}
.tb-meta{font-size:.7rem;color:var(--t2);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.var-chip{
  display:inline-block;margin-left:5px;
  font-size:.58rem;font-weight:700;padding:1px 5px;border-radius:4px;
  background:rgba(255,255,255,.08);color:var(--t2);vertical-align:middle;
}

/* Spot banner */
.spot-banner{
  background:rgba(6,182,212,.08);border:1px solid rgba(6,182,212,.22);
  border-radius:8px;padding:5px 12px;font-size:.78rem;font-weight:700;
  color:var(--cyan);text-align:center;box-shadow:0 0 10px rgba(6,182,212,.12);
  margin-bottom:8px;
}

/* History hint */
.hist-hint{font-size:.7rem;color:var(--t3);font-style:italic;padding-top:4px}

/* ── Inference compact table ────────────────────────────────────────── */
.inf-table{border:1px solid var(--bd);border-radius:12px;overflow:hidden}
.inf-header,.inf-row{
  display:grid;
  grid-template-columns:var(--inf-cols,130px 1fr 1fr 1fr);
  gap:0;padding:10px 18px;align-items:center;
}
.inf-header{
  font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;
  color:var(--t3);background:rgba(255,255,255,.02);border-bottom:1px solid var(--bd);
}
.inf-row{border-bottom:1px solid rgba(255,255,255,.04)}
.inf-row:last-child{border-bottom:none}
.inf-row:hover{background:rgba(59,130,246,.04)}
.inf-gpu{font-weight:700;font-size:.9rem}
.inf-cell{display:flex;flex-direction:column;gap:2px}
.inf-price{font-size:.88rem;font-weight:600;color:var(--t1)}
.inf-meta{font-size:.7rem;color:var(--t2)}
.inf-na{font-size:.88rem;color:var(--t3)}

/* ── Legacy section ─────────────────────────────────────────────────── */
#legacy-section{display:none}
.legacy-grid{
  display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:8px;
}
.leg-row{
  background:var(--card);border:1px solid var(--bd);border-radius:10px;
  padding:10px 14px;display:flex;flex-direction:column;gap:3px;
}
.leg-gpu{font-size:.85rem;font-weight:700;color:var(--t1)}
.leg-price{font-size:.75rem;color:var(--t2)}
.show-legacy-btn{
  background:transparent;border:1px solid var(--bd);border-radius:8px;
  color:var(--t2);font-size:.78rem;padding:8px 18px;cursor:pointer;
  transition:border-color .2s,color .2s;margin-bottom:24px;
}
.show-legacy-btn:hover{border-color:var(--accent);color:var(--accent)}

/* ── Bottom two-col ─────────────────────────────────────────────────── */
.two-col{display:grid;grid-template-columns:1fr 1fr;gap:32px}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* ── Movers ─────────────────────────────────────────────────────────── */
.mover-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 0;border-bottom:1px solid var(--bd);
}
.mover-row:last-child{border-bottom:none}
.mover-gpu{font-weight:600;font-size:.9rem}
.mover-trail{display:flex;align-items:center;gap:16px}
.mover-prices{font-size:.78rem;color:var(--t2)}
.mover-pct{font-weight:700;font-size:.95rem;min-width:64px;text-align:right}
.val-up{color:var(--red)}.val-down{color:var(--green)}.val-flat{color:var(--t3)}

/* ── Spread leaders ─────────────────────────────────────────────────── */
.spread-row{display:flex;align-items:center;gap:18px;padding:12px 0;border-bottom:1px solid var(--bd)}
.spread-row:last-child{border-bottom:none}
.spread-pct{
  font-size:1.6rem;font-weight:800;min-width:64px;text-align:right;
  background:linear-gradient(90deg,var(--cyan),var(--accent));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.spread-body{display:flex;flex-direction:column;gap:3px;min-width:0}
.spread-gpu{font-weight:700;font-size:.9rem}
.spread-prices{font-size:.78rem}
.spread-od{color:var(--t2)}.spread-arr{color:var(--t3)}.spread-sp{color:var(--cyan);font-weight:600}

/* ── Flagship Trend Chart ────────────────────────────────────────────── */
#flagship-trend-chart{margin-bottom:52px}
.trend-card{
  background:var(--card);border:1px solid rgba(59,130,246,.25);
  border-radius:var(--r);padding:24px;
}
.trend-chart-wrap{position:relative;height:320px}
.trend-placeholder{
  height:320px;display:flex;flex-direction:column;
  align-items:center;justify-content:center;gap:18px;
}
.trend-placeholder-icon{
  width:52px;height:52px;
  clip-path:polygon(50% 0%,100% 25%,100% 75%,50% 100%,0% 75%,0% 25%);
  background:rgba(59,130,246,.18);border:1px solid rgba(59,130,246,.4);
  box-shadow:0 0 28px rgba(59,130,246,.45);
  display:flex;align-items:center;justify-content:center;
}
.trend-placeholder-inner{
  width:18px;height:18px;border-radius:50%;
  background:var(--accent);box-shadow:0 0 12px rgba(59,130,246,.8);
}
.trend-placeholder-msg{
  color:#94A3B8;font-size:.9rem;text-align:center;
  max-width:400px;line-height:1.7;
}
.trend-placeholder-msg strong{color:var(--t1)}
.trend-stats{display:flex;flex-wrap:wrap;gap:10px;margin-top:18px}
.trend-stat{
  background:rgba(255,255,255,.04);border:1px solid rgba(255,255,255,.08);
  border-radius:8px;padding:7px 14px;font-size:.78rem;color:var(--t2);
}
.trend-stat strong{color:var(--t1);font-weight:700}
.ts-up{color:var(--red)}.ts-down{color:var(--green)}

/* ── Misc ────────────────────────────────────────────────────────────── */
.notice-msg{color:var(--t3);font-style:italic;font-size:.88rem;padding:8px 0}

/* ── Atlas watermark sub-label ───────────────────────────────────────── */
.atlas-idx{font-size:.55rem;font-weight:700;letter-spacing:.18em;color:var(--t3);text-transform:uppercase;text-align:right;margin-top:2px}

/* ── Waitlist CTA ────────────────────────────────────────────────────── */
.cta-block{
  background:linear-gradient(135deg,rgba(59,130,246,.10) 0%,rgba(6,182,212,.07) 100%);
  border:1px solid rgba(59,130,246,.30);border-radius:var(--r);
  padding:52px 48px;margin:0 0 64px;
  display:flex;align-items:center;justify-content:space-between;gap:48px;
  box-shadow:0 0 60px rgba(59,130,246,.10),inset 0 1px 0 rgba(255,255,255,.04);
  position:relative;overflow:hidden;
}
.cta-block::before{
  content:'';position:absolute;inset:0;
  background:radial-gradient(ellipse 60% 80% at 80% 50%,rgba(6,182,212,.06) 0%,transparent 70%);
  pointer-events:none;
}
@media(max-width:700px){.cta-block{flex-direction:column;padding:36px 28px;gap:28px}}
.cta-text-col{flex:1;min-width:0;position:relative;z-index:1}
.cta-headline{
  font-size:1.55rem;font-weight:800;color:var(--t1);line-height:1.25;margin-bottom:12px;
  letter-spacing:-.3px;
}
.cta-sub{font-size:.9rem;color:var(--t2);line-height:1.7;max-width:520px}
.cta-btn-col{flex-shrink:0;position:relative;z-index:1}
.cta-btn{
  display:inline-block;
  background:linear-gradient(135deg,var(--accent) 0%,var(--cyan) 100%);
  color:#fff;font-weight:700;font-size:1rem;letter-spacing:-.2px;
  padding:16px 36px;border-radius:10px;text-decoration:none;
  box-shadow:0 0 28px rgba(59,130,246,.40);
  transition:box-shadow .2s,transform .15s;white-space:nowrap;
}
.cta-btn:hover{box-shadow:0 0 48px rgba(59,130,246,.70);transform:translateY(-2px)}

.site-footer{
  border-top:1px solid var(--bd);padding:28px 28px 0;
  max-width:1600px;margin:0 auto;font-size:.75rem;color:var(--t3);
  display:flex;justify-content:space-between;align-items:center;
}
.site-footer a{color:var(--accent);text-decoration:none}
"""

# ---------------------------------------------------------------------------
# JavaScript  (plain string)
# ---------------------------------------------------------------------------

JS = """
/* ── Background sparklines ─────────────────────────────────────────── */
document.querySelectorAll('.sparkline-bg').forEach(function(canvas) {
  var raw = canvas.dataset.series, color = canvas.dataset.color || '#3B82F6';
  if (!raw) return;
  var s; try { s = JSON.parse(raw); } catch(e) { return; }
  if (!s.data || s.data.length < 2) return;
  new Chart(canvas, {
    type: 'line',
    data: {
      labels: s.labels,
      datasets: [{
        data: s.data, borderColor: color, backgroundColor: color + '30',
        borderWidth: 1.5, pointRadius: 0, fill: true, tension: 0.4,
      }]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 0 },
      plugins: { legend: {display:false}, tooltip: {enabled:false} },
      scales: { x: {display:false}, y: {display:false} }
    }
  });
});

/* ── Legacy toggle ──────────────────────────────────────────────────── */
var legBtn = document.getElementById('legacy-btn');
if (legBtn) {
  legBtn.addEventListener('click', function() {
    document.getElementById('legacy-section').style.display = 'block';
    this.style.display = 'none';
  });
}

/* ── Flagship trend chart ───────────────────────────────────────────── */
(function() {
  var canvas = document.getElementById('flagship-trend-canvas');
  if (!canvas) return;
  var raw = canvas.dataset.series;
  if (!raw) return;
  var s; try { s = JSON.parse(raw); } catch(e) { return; }
  if (!s || !s.labels || s.labels.length < 2) return;
  var MONTHS_SHORT = ['Jan','Feb','Mar','Apr','May','Jun','Jul','Aug','Sep','Oct','Nov','Dec'];
  var MONTHS_LONG  = ['January','February','March','April','May','June',
                      'July','August','September','October','November','December'];
  var labels = s.labels.map(function(d) {
    var p = d.split('-');
    return MONTHS_SHORT[parseInt(p[1],10)-1] + ' ' + parseInt(p[2],10);
  });
  new Chart(canvas, {
    type: 'line',
    data: {
      labels: labels,
      datasets: [
        {
          label: 'On-Demand',
          data: s.on_demand,
          borderColor: '#3B82F6',
          backgroundColor: 'rgba(59,130,246,0.08)',
          borderWidth: 2, pointRadius: 3, pointHoverRadius: 5,
          fill: true, tension: 0.35, spanGaps: true,
        },
        {
          label: 'Spot / Interruptible',
          data: s.spot,
          borderColor: '#06B6D4',
          backgroundColor: 'rgba(6,182,212,0.08)',
          borderWidth: 2, pointRadius: 3, pointHoverRadius: 5,
          fill: true, tension: 0.35, spanGaps: true,
        }
      ]
    },
    options: {
      responsive: true, maintainAspectRatio: false,
      animation: { duration: 600 },
      plugins: {
        legend: {
          display: true, position: 'top', align: 'end',
          labels: {
            color: '#94A3B8', usePointStyle: true,
            pointStyleWidth: 10, boxHeight: 6,
            font: { size: 12, weight: '600' }
          }
        },
        tooltip: {
          backgroundColor: '#0D1B3D',
          borderColor: 'rgba(59,130,246,0.4)', borderWidth: 1,
          titleColor: '#FFFFFF', bodyColor: '#94A3B8', padding: 12,
          callbacks: {
            title: function(items) {
              var iso = s.labels[items[0].dataIndex];
              var p = iso.split('-');
              return p[2] + ' ' + MONTHS_LONG[parseInt(p[1],10)-1] + ' ' + p[0];
            },
            label: function(item) {
              var v = item.raw;
              if (v === null || v === undefined) return item.dataset.label + ': N/A';
              return item.dataset.label + ': $' + v.toFixed(4) + '/hr avg';
            }
          }
        }
      },
      scales: {
        x: {
          display: true,
          grid: { display: false },
          ticks: { color: '#94A3B8', font: { size: 11 } }
        },
        y: {
          display: true,
          grid: { color: 'rgba(148,163,184,0.08)' },
          ticks: {
            color: '#94A3B8', font: { size: 11 },
            callback: function(v) { return '$' + v.toFixed(2); }
          }
        }
      }
    }
  });
})();

/* ── Relative timestamp ─────────────────────────────────────────────── */
(function() {
  var el = document.getElementById('rel-time');
  if (!el) return;
  var ts = new Date(el.dataset.ts);
  if (isNaN(ts)) return;
  var mins = Math.round((Date.now() - ts) / 60000);
  el.textContent = mins < 2 ? 'just now'
    : mins < 60 ? mins + ' min ago'
    : mins < 1440 ? Math.round(mins/60) + ' hr ago'
    : Math.round(mins/1440) + ' days ago';
})();
"""


def _render_systems_section_html(systems: list[dict]) -> str:
    """HTML for the Systems (CPU+GPU integrated) section.

    Renders only when there is at least one system in the data.
    GH200 and MI300A cards will appear automatically once they enter the DB.
    """
    if not systems:
        return ""
    cards = "\n".join(_card_html(g, i, "flag-card") for i, g in enumerate(systems))
    return (
        f'<section class="sec">\n'
        f'<div class="sec-hdr">'
        f'<span class="sec-label">Systems (CPU+GPU Integrated)</span>'
        f'<div class="sec-rule"></div>'
        f'<span class="sec-sub">'
        f'GB300 &middot; GH200 &middot; MI300A &mdash; full server-class'
        f'</span>'
        f'</div>\n'
        f'<div class="flag-grid">{cards}</div>\n'
        f'</section>'
    )


def _render_flagship_trend_html(trend: list[dict]) -> str:
    """Render the flagship price trend section HTML."""
    have_chart = len(trend) >= 2

    if have_chart:
        series = {
            "labels":    [d["date"]         for d in trend],
            "on_demand": [d["avg_on_demand"] for d in trend],
            "spot":      [d["avg_spot"]      for d in trend],
        }
        chart_inner = (
            f'<div class="trend-chart-wrap">'
            f"<canvas id='flagship-trend-canvas' "
            f"data-series='{json.dumps(series)}'></canvas>"
            f"</div>"
        )
    else:
        n = len(trend)
        s = "s" if n != 1 else ""
        chart_inner = (
            f'<div class="trend-placeholder">'
            f'<div class="trend-placeholder-icon">'
            f'<div class="trend-placeholder-inner"></div>'
            f'</div>'
            f'<div class="trend-placeholder-msg">'
            f'Building trend data&hellip; '
            f'<strong>{n} day{s}</strong> of history.<br>'
            f'Curves will appear once 2+ daily snapshots are available.'
            f'</div>'
            f'</div>'
        )

    # Mini-stats chips
    stats_parts: list[str] = []
    if trend:
        latest = trend[-1]
        od = latest.get("avg_on_demand")
        sp = latest.get("avg_spot")
        if od is not None:
            stats_parts.append(
                f'<div class="trend-stat">Current on-demand avg: '
                f'<strong>${od:.2f}/hr</strong></div>'
            )
        if sp is not None:
            stats_parts.append(
                f'<div class="trend-stat">Current spot avg: '
                f'<strong>${sp:.2f}/hr</strong></div>'
            )
        if od and sp and od > 0 and od > sp:
            spread = (od - sp) / od * 100
            stats_parts.append(
                f'<div class="trend-stat">Current avg spread: '
                f'<strong>&minus;{spread:.1f}%</strong></div>'
            )
        # 7-day change chip (only when >= 7 data points)
        if len(trend) >= 7 and od is not None:
            old_od = trend[-7].get("avg_on_demand")
            if old_od and old_od > 0:
                change = (od - old_od) / old_od * 100
                sign   = "+" if change >= 0 else ""
                cls    = "ts-up" if change > 0.05 else ("ts-down" if change < -0.05 else "")
                arrow  = "▲" if change > 0.05 else ("▼" if change < -0.05 else "—")
                stats_parts.append(
                    f'<div class="trend-stat">7d change: '
                    f'<strong class="{cls}">{arrow} {sign}{change:.1f}%</strong></div>'
                )

    stats_html = (
        f'<div class="trend-stats">{"".join(stats_parts)}</div>'
        if stats_parts else ""
    )

    return (
        f'<section class="sec" id="flagship-trend-chart">\n'
        f'<div class="sec-hdr">'
        f'<span class="sec-label">Flagship GPU Price Trend</span>'
        f'<div class="sec-rule"></div>'
        f'<span class="sec-sub">'
        f'H100 &middot; H200 &middot; B200 &middot; B300 &mdash; daily average across all providers'
        f'</span>'
        f'</div>\n'
        f'<div class="trend-card">\n'
        f'{chart_inner}\n'
        f'{stats_html}\n'
        f'</div>\n'
        f'</section>'
    )


# ---------------------------------------------------------------------------
# HTML assembler
# ---------------------------------------------------------------------------

def generate_html(
    hero:     dict,
    flagship: list[dict],
    workhorse: list[dict],
    inference: list[dict],
    legacy:   list[dict],
    movers:   dict,
    leaders:  list[dict],
    gen_ts:   str,
    flagship_trend: list[dict] = [],
    systems:  list[dict] = [],
) -> str:
    snap_date = hero.get("latest_date") or "N/A"
    day_count = hero.get("day_count", 0)
    prov_cnt  = hero.get("provider_count", 0)
    rows_cnt  = hero.get("total_rows", 0)
    gpu_cnt   = hero.get("gpu_count", 0)
    md        = hero.get("median_spot_discount")
    max_spread = leaders[0]["spread_pct"] if leaders else None

    md_str  = f"&minus;{md:.1f}%"    if md         is not None else "N/A"
    sp_str  = f"&minus;{max_spread:.0f}%" if max_spread is not None else "N/A"

    # ── Hero pills (KPIs first) ──
    hero_html = (
        f'<div class="stat-pill"><span class="sv kpi">{md_str}</span>'
        f'<span class="sl">Median Spot Discount</span></div>\n'
        f'<div class="stat-pill"><span class="sv kpi">{sp_str}</span>'
        f'<span class="sl">Max Spread (on-demand vs spot)</span></div>\n'
        f'<div class="stat-pill"><span class="sv">{prov_cnt}</span>'
        f'<span class="sl">Providers tracked</span></div>\n'
        f'<div class="stat-pill"><span class="sv">{gpu_cnt}</span>'
        f'<span class="sl">GPU families</span></div>\n'
        f'<div class="stat-pill"><span class="sv">{day_count}</span>'
        f'<span class="sl">Days of history</span></div>\n'
        f'<div class="stat-pill"><span class="sv">{rows_cnt:,}</span>'
        f'<span class="sl">Price rows</span></div>'
    )

    # ── Flagship section ──
    if flagship:
        flag_cards = "\n".join(_card_html(g, i, "flag-card") for i, g in enumerate(flagship))
        flag_html = f'<div class="flag-grid">{flag_cards}</div>'
    else:
        flag_html = '<p class="notice-msg">No flagship GPU data.</p>'

    # ── Workhorse section ──
    if workhorse:
        work_cards = "\n".join(_card_html(g, i, "work-card") for i, g in enumerate(workhorse))
        work_html = f'<div class="work-grid">{work_cards}</div>'
    else:
        work_html = '<p class="notice-msg">No workhorse GPU data.</p>'

    # ── Inference section ──
    if inference:
        # Only show Reserved column when at least one inference GPU has reserved pricing
        inf_has_reserved = any(g["reserved"] is not None for g in inference)
        cols = "130px 1fr 1fr 1fr" if inf_has_reserved else "130px 1fr 1fr"
        rv_header = "<span>Reserved</span>" if inf_has_reserved else ""
        inf_rows = "\n".join(
            _inference_row_html(g, show_reserved=inf_has_reserved) for g in inference
        )
        inf_html = (
            f'<div class="inf-table" style="--inf-cols:{cols}">'
            f'<div class="inf-header">'
            f'<span>GPU</span><span>On-Demand</span>'
            f'<span>Spot / Int.</span>{rv_header}'
            f"</div>"
            f"{inf_rows}"
            f"</div>"
        )
    else:
        inf_html = '<p class="notice-msg">No inference GPU data.</p>'

    # ── Legacy section ──
    if legacy:
        leg_items = "\n".join(_legacy_item_html(g) for g in legacy)
        legacy_html = (
            f'<button class="show-legacy-btn" id="legacy-btn">'
            f"Show {len(legacy)} legacy GPU{'s' if len(legacy) != 1 else ''}"
            f"</button>"
            f'<div id="legacy-section">'
            f'<div class="legacy-grid">{leg_items}</div>'
            f"</div>"
        )
    else:
        legacy_html = ""

    movers_html  = _render_movers(movers)
    leaders_html = _render_spread_leaders(leaders)
    trend_html   = _render_flagship_trend_html(flagship_trend)
    systems_html = _render_systems_section_html(systems)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Atlas · GPU Compute Index</title>
  <meta name="description" content="The live cross-provider GPU price index. Real-time on-demand, spot, and reserved pricing across {prov_cnt} cloud providers — H100, H200, B200, A100 and more."/>
  <link rel="preconnect" href="https://fonts.googleapis.com"/>
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet"/>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>{CSS}</style>
</head>
<body>
<header class="site-hdr">
  <div class="hdr-left">
    <div class="logo-dot"></div>
    <div>
      <div class="logo-brand">Atlas &middot; GPU Compute Index</div>
      <div class="logo-sub">The live cross-provider GPU price index &middot; {prov_cnt} clouds tracked &middot; updated daily</div>
    </div>
  </div>
  <div>
    <div class="atlas-wm">ATLAS</div>
    <div class="atlas-idx">GPU Compute Index</div>
    <div class="snap-date">
      {snap_date} &middot; <span id="rel-time" data-ts="{gen_ts}">...</span>
    </div>
  </div>
</header>

<div class="page">
  <div class="hero">{hero_html}</div>

  {trend_html}

  <section class="sec">
    <div class="sec-hdr">
      <span class="sec-label">Flagship Training</span>
      <div class="sec-rule"></div>
      <span class="sec-sub">H100 &middot; H200 &middot; B200 &middot; B300 &mdash; all form factors merged</span>
    </div>
    {flag_html}
  </section>

  {systems_html}

  <section class="sec">
    <div class="sec-hdr">
      <span class="sec-label">Workhorse</span>
      <div class="sec-rule"></div>
      <span class="sec-sub">A100 &middot; L40S &middot; L40 &middot; A40</span>
    </div>
    {work_html}
  </section>

  <section class="sec">
    <div class="sec-hdr">
      <span class="sec-label">Inference</span>
      <div class="sec-rule"></div>
      <span class="sec-sub">L4 &middot; A10G &middot; A10 &middot; T4 &middot; RTX</span>
    </div>
    {inf_html}
  </section>

  {legacy_html}

  <div class="two-col">
    <section class="sec">
      <div class="sec-hdr">
        <span class="sec-label">Biggest Movers (7 days)</span>
        <div class="sec-rule"></div>
      </div>
      {movers_html}
    </section>
    <section class="sec">
      <div class="sec-hdr">
        <span class="sec-label">Market Spread Leaders</span>
        <div class="sec-rule"></div>
      </div>
      {leaders_html}
    </section>
  </div>

  <div class="cta-block">
    <div class="cta-text-col">
      <div class="cta-headline">Stop arbitraging GPU prices manually.</div>
      <div class="cta-sub">Atlas monitors {prov_cnt} clouds in real time, alerts you to price drops, and routes your workloads to the cheapest available GPU — automatically.</div>
    </div>
    <div class="cta-btn-col">
      <a class="cta-btn" href="mailto:hello@atlas-compute.io?subject=Atlas%20Waitlist">Join the waitlist &rarr;</a>
    </div>
  </div>
</div>

<footer class="site-footer">
  <span>Automated daily scrape &middot; 05:17 UTC &middot; on-demand prices per GPU, USD
  &middot; <a href="https://github.com/romaindeniau/gpu-price-scraper/actions">GitHub Actions</a></span>
  <span>Generated {gen_ts[:19]} UTC</span>
</footer>

<script>{JS}</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    (DOCS_DIR / ".nojekyll").touch()

    con    = _connect()
    gen_ts = datetime.now(timezone.utc).isoformat()
    ld     = _latest_date(con)

    if not ld:
        print("WARNING: price_history is empty — generating empty dashboard.")

    qualifying_bases = _build_min_provider_bases(con)
    offers   = _build_raw_offers(con, ld) if ld else []
    flagship, workhorse, inference, legacy, systems = _build_gpu_groups(
        offers, qualifying_bases=qualifying_bases
    )

    sl = _build_sparklines(con)
    for group_list in (flagship, workhorse, inference, legacy, systems):
        _attach_sparklines(group_list, sl)

    hero            = _build_hero_stats(con, flagship, workhorse, inference, legacy, systems)
    movers          = _build_movers(con, hero["day_count"])
    leaders         = _build_spread_leaders(con)
    flagship_trend  = compute_flagship_trend(con)
    con.close()

    html = generate_html(
        hero, flagship, workhorse, inference, legacy,
        movers, leaders, gen_ts,
        flagship_trend=flagship_trend,
        systems=systems,
    )

    (DOCS_DIR / "index.html").write_text(html)
    (DOCS_DIR / "data.json").write_text(json.dumps(
        {
            "generated_at":   gen_ts,
            "hero":           hero,
            "movers":         movers,
            "spread_leaders": leaders,
            "flagship_trend": flagship_trend,
        },
        indent=2,
    ))

    print(f"docs/index.html  {len(html):,} chars")
    print(f"  Flagship  : {[g['base_gpu'] for g in flagship]}")
    print(f"  Systems   : {[g['base_gpu'] for g in systems]}")
    print(f"  Workhorse : {[g['base_gpu'] for g in workhorse]}")
    print(f"  Inference : {[g['base_gpu'] for g in inference]}")
    print(f"  Legacy    : {[g['base_gpu'] for g in legacy]}")
    print(f"  Day count : {hero['day_count']}")
    print(f"  Outliers  : see WARNING/INFO lines above")


if __name__ == "__main__":
    main()
