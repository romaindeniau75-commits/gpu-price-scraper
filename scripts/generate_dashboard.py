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
_WORKHORSE_BASES = frozenset({"A100", "L40S", "L40", "A40"})
_INFERENCE_BASES = frozenset({"L4", "A10G", "A10", "T4"})
_LEGACY_BASES    = frozenset({
    "V100", "MI300X", "MI355X", "P100", "K80", "M60",
    "RTX A6000", "RTX A5000", "RTX A4500", "RTX A4000", "RTX A2000",
})
_RTX_CATCH_ALL   = "RTX"

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

_CARD_COLORS = [
    "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6",
    "#EC4899", "#06B6D4", "#F97316", "#84CC16", "#6366F1",
    "#14B8A6", "#E879F9",
]

# Flagship sort: within flagship, prefer bigger/newer GPUs
_FLAG_ORDER = ["B300", "B200", "H200", "H100"]
_WORK_ORDER = ["A100", "L40S", "L40", "A40"]
_INF_ORDER  = ["L4", "A10G", "A10", "T4", "RTX"]


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
        #     On-demand/reserved: full [lo, hi] window.
        #     Spot/community/interruptible: relaxed floor at 30 % of lo (prices
        #     can be deeply discounted, but can't be implausibly close to zero).
        #     Ceiling applies to all tiers.
        if base and base in _PLAUSIBILITY_BOUNDS:
            lo, hi = _PLAUSIBILITY_BOUNDS[base]
            tier_str = o["availability_tier"]
            effective_lo = lo if tier_str in ("on_demand", "reserved") else lo * 0.30
            if price < effective_lo or price > hi:
                print(
                    f"WARNING plausibility excluded: {o['gpu_model']} @ {o['provider']} "
                    f"${price:.4f}/hr (bounds=[${effective_lo:.2f}, ${hi:.2f}])"
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
        clean.append(o)

    return clean


def _build_gpu_groups(
    offers: list[dict],
) -> tuple[list[dict], list[dict], list[dict], list[dict]]:
    """
    Aggregate offers by base GPU family, returning four sorted lists:
    (flagship, workhorse, inference, legacy)
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
    buckets: dict[str, list[dict]] = {"flagship": [], "workhorse": [], "inference": [], "legacy": []}

    for base, slots in base_slots.items():
        providers = frozenset(base_providers[base])
        tier_cls  = _gpu_tier(base, providers)
        if tier_cls is None:
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
    ]:
        buckets[bucket_key].sort(key=lambda g: _tier_order(g["base_gpu"], order_list))
    # Legacy: alphabetical
    buckets["legacy"].sort(key=lambda g: g["base_gpu"])

    return (
        buckets["flagship"],
        buckets["workhorse"],
        buckets["inference"],
        buckets["legacy"],
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
) -> dict:
    row = con.execute(
        """
        SELECT COUNT(DISTINCT snapshot_date) AS day_count,
               COUNT(*)                      AS total_rows
        FROM   price_history
        """
    ).fetchone()

    ld = _latest_date(con)
    prov_count: int = con.execute(
        "SELECT COUNT(DISTINCT provider) FROM price_history WHERE snapshot_date = ?",
        (ld,),
    ).fetchone()[0]

    # Median spot discount from flagship + workhorse (the cards that matter for the pitch)
    discounts = [
        g["spot_discount_pct"]
        for g in (flagship + workhorse)
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

    total_bases = sum(len(b) for b in [flagship, workhorse, inference, legacy])

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

    od_html = _tier_block_html("On-Demand", "chip-od",       g["on_demand"])
    sp_html = _tier_block_html("Spot / Int.", "chip-spot",   g["spot"])
    rv_html = _tier_block_html("Reserved",  "chip-reserved", g["reserved"], is_reserved=True)

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

    return (
        f'<div class="{card_cls}">'
        f"{bg_layer}"
        f'<div class="card-content">'
        f'<div class="card-header"><span class="gpu-name">{gpu}</span></div>'
        f'<div class="tier-blocks">{od_html}{sp_html}{rv_html}</div>'
        f"{banner}"
        f"{hist_hint}"
        f"</div>"
        f"</div>"
    )


def _inference_row_html(g: dict) -> str:
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

    return (
        f'<div class="inf-row">'
        f'<span class="inf-gpu">{gpu}</span>'
        f'<div class="inf-cell">{_cell(od)}</div>'
        f'<div class="inf-cell">{_cell(sp)}</div>'
        f'<div class="inf-cell">{_cell(rv, is_reserved=True)}</div>'
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
    if not leaders:
        return '<p class="notice-msg">No spot offers available for comparison.</p>'
    parts = []
    for lead in leaders:
        base = _gpu_base(lead["gpu_model"]) or lead["gpu_model"]
        parts.append(
            f'<div class="spread-row">'
            f'<div class="spread-pct">{lead["spread_pct"]:.0f}%</div>'
            f'<div class="spread-body">'
            f'<div class="spread-gpu">{base}</div>'
            f'<div class="spread-prices">'
            f'<span class="spread-od">{_fmt(lead["od_price"])} on-demand</span>'
            f'<span class="spread-arr"> &rarr; </span>'
            f'<span class="spread-sp">{_fmt(lead["sp_price"])} spot</span>'
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

.card-header{margin-bottom:14px}
.gpu-name{font-size:1.1rem;font-weight:700;color:var(--t1);display:block;line-height:1.2}
.flag-card .gpu-name{font-size:1.25rem}

/* ── Tier blocks ────────────────────────────────────────────────────── */
.tier-blocks{display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;margin-bottom:12px}
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
  grid-template-columns:130px 1fr 1fr 1fr;
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

/* ── Misc ────────────────────────────────────────────────────────────── */
.notice-msg{color:var(--t3);font-style:italic;font-size:.88rem;padding:8px 0}
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
        inf_rows = "\n".join(_inference_row_html(g) for g in inference)
        inf_html = (
            f'<div class="inf-table">'
            f'<div class="inf-header">'
            f'<span>GPU</span><span>On-Demand</span>'
            f'<span>Spot / Int.</span><span>Reserved</span>'
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

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1.0"/>
  <title>Atlas GPU Pricing Dashboard</title>
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
      <div class="logo-brand">Atlas GPU Pricing</div>
      <div class="logo-sub">H100 &middot; H200 &middot; B200 &middot; A100 across 14 providers</div>
    </div>
  </div>
  <div>
    <div class="atlas-wm">Atlas</div>
    <div class="snap-date">
      {snap_date} &middot; <span id="rel-time" data-ts="{gen_ts}">...</span>
    </div>
  </div>
</header>

<div class="page">
  <div class="hero">{hero_html}</div>

  <section class="sec">
    <div class="sec-hdr">
      <span class="sec-label">Flagship Training</span>
      <div class="sec-rule"></div>
      <span class="sec-sub">H100 &middot; H200 &middot; B200 &middot; B300 &mdash; all form factors merged</span>
    </div>
    {flag_html}
  </section>

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
</div>

<footer class="site-footer">
  <span>Automated daily scrape &middot; 06:00 UTC &middot; on-demand prices per GPU, USD
  &middot; <a href="https://github.com/romaindeniau75-commits/gpu-price-scraper/actions">GitHub Actions</a></span>
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

    offers   = _build_raw_offers(con, ld) if ld else []
    flagship, workhorse, inference, legacy = _build_gpu_groups(offers)

    sl = _build_sparklines(con)
    for group_list in (flagship, workhorse, inference, legacy):
        _attach_sparklines(group_list, sl)

    hero    = _build_hero_stats(con, flagship, workhorse, inference, legacy)
    movers  = _build_movers(con, hero["day_count"])
    leaders = _build_spread_leaders(con)
    con.close()

    html = generate_html(hero, flagship, workhorse, inference, legacy, movers, leaders, gen_ts)

    (DOCS_DIR / "index.html").write_text(html)
    (DOCS_DIR / "data.json").write_text(json.dumps(
        {"generated_at": gen_ts, "hero": hero, "movers": movers, "spread_leaders": leaders},
        indent=2,
    ))

    print(f"docs/index.html  {len(html):,} chars")
    print(f"  Flagship  : {[g['base_gpu'] for g in flagship]}")
    print(f"  Workhorse : {[g['base_gpu'] for g in workhorse]}")
    print(f"  Inference : {[g['base_gpu'] for g in inference]}")
    print(f"  Legacy    : {[g['base_gpu'] for g in legacy]}")
    print(f"  Day count : {hero['day_count']}")
    print(f"  Outliers  : see WARNING lines above")


if __name__ == "__main__":
    main()
