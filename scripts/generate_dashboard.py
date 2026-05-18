#!/usr/bin/env python3
"""Generate docs/index.html and docs/data.json from data/pricing_history.db.

Usage:
    python3 scripts/generate_dashboard.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
ROOT       = Path(__file__).parent.parent
HISTORY_DB = ROOT / "data" / "pricing_history.db"
DOCS_DIR   = ROOT / "docs"

# GPU display sort priority (first pattern match wins)
_GPU_PRIORITY = [
    "H100", "H200", "B200", "B300", "A100",
    "L40S", "L40", "A40", "A10G", "A10",
    "L4", "V100", "T4", "RTX", "P100",
]

# Tiers treated as "spot / interruptible" in the spot display slot
_SPOT_TIERS = frozenset({"spot", "interruptible"})

# Chart.js sparkline accent colours (cycled per GPU card)
_CARD_COLORS = [
    "#3B82F6", "#10B981", "#F59E0B", "#EF4444", "#8B5CF6",
    "#EC4899", "#06B6D4", "#F97316", "#84CC16", "#6366F1",
    "#14B8A6", "#E879F9",
]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    if not HISTORY_DB.exists():
        sys.exit(
            f"ERROR: {HISTORY_DB} not found.\n"
            "Run: python3 -m gpu_scraper.cli fetch --save-db"
        )
    con = sqlite3.connect(str(HISTORY_DB))
    con.row_factory = sqlite3.Row
    return con


def _latest_date(con: sqlite3.Connection) -> str:
    row = con.execute("SELECT MAX(snapshot_date) FROM price_history").fetchone()
    return row[0] or ""


def _gpu_sort_key(name: str) -> tuple[int, str]:
    u = name.upper()
    for i, pat in enumerate(_GPU_PRIORITY):
        if pat in u:
            return (i, name)
    return (len(_GPU_PRIORITY), name)


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------

def _build_hero_stats(con: sqlite3.Connection) -> dict:
    row = con.execute(
        """
        SELECT
            COUNT(DISTINCT gpu_model)     AS gpu_count,
            COUNT(DISTINCT provider)      AS provider_count,
            COUNT(DISTINCT snapshot_date) AS day_count,
            COUNT(*)                      AS total_rows
        FROM price_history
        """
    ).fetchone()

    ld = _latest_date(con)

    # Median spot discount across all GPUs (on-demand min vs spot min, latest day)
    disc_rows = con.execute(
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
        SELECT ROUND((od.price - sp.price) / od.price * 100, 1) AS disc
        FROM   od JOIN sp ON od.gpu_model = sp.gpu_model
        WHERE  od.price > sp.price
        ORDER  BY disc
        """,
        (ld, ld),
    ).fetchall()

    median_disc: Optional[float] = None
    if disc_rows:
        vals = [r["disc"] for r in disc_rows]
        n = len(vals)
        median_disc = round(
            vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2,
            1,
        )

    return {
        "gpu_count":            row["gpu_count"],
        "provider_count":       row["provider_count"],
        "day_count":            row["day_count"],
        "total_rows":           row["total_rows"],
        "median_spot_discount": median_disc,
        "latest_date":          ld,
    }


def _build_gpu_cards(con: sqlite3.Connection) -> list[dict]:
    ld = _latest_date(con)
    if not ld:
        return []

    # Best price per (gpu_model, availability_tier) for the latest snapshot
    rows = con.execute(
        """
        WITH ranked AS (
            SELECT
                gpu_model, provider, region_canonical,
                availability_tier, price_per_gpu_hour, commitment_term,
                ROW_NUMBER() OVER (
                    PARTITION BY gpu_model, availability_tier
                    ORDER BY price_per_gpu_hour ASC
                ) AS rn
            FROM price_history
            WHERE snapshot_date = ?
        )
        SELECT gpu_model, provider, region_canonical,
               availability_tier, price_per_gpu_hour, commitment_term
        FROM ranked
        WHERE rn = 1
        """,
        (ld,),
    ).fetchall()

    # Aggregate into logical display tiers per GPU
    by_gpu: dict[str, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        gpu  = r["gpu_model"]
        tier = r["availability_tier"]

        if tier == "on_demand":
            slot = "on_demand"
        elif tier in _SPOT_TIERS:
            slot = "spot"
        elif tier == "reserved":
            slot = "reserved"
        else:
            continue  # skip community / unknown in card display

        price = r["price_per_gpu_hour"]
        if slot not in by_gpu[gpu] or price < by_gpu[gpu][slot]["price"]:
            by_gpu[gpu][slot] = {
                "price":           price,
                "provider":        r["provider"],
                "region":          r["region_canonical"],
                "commitment_term": r["commitment_term"],
            }

    # 30-day on-demand sparklines (min per day per GPU)
    sl_rows = con.execute(
        """
        SELECT gpu_model, snapshot_date, MIN(price_per_gpu_hour) AS min_price
        FROM   price_history
        WHERE  availability_tier = 'on_demand'
          AND  snapshot_date >= date('now', '-30 days')
        GROUP  BY gpu_model, snapshot_date
        ORDER  BY gpu_model, snapshot_date
        """
    ).fetchall()

    sparklines: dict[str, dict] = defaultdict(lambda: {"dates": [], "prices": []})
    for r in sl_rows:
        sparklines[r["gpu_model"]]["dates"].append(r["snapshot_date"])
        sparklines[r["gpu_model"]]["prices"].append(round(r["min_price"], 4))

    cards = []
    for gpu, tiers in by_gpu.items():
        od = tiers.get("on_demand")
        sp = tiers.get("spot")
        rv = tiers.get("reserved")

        spot_disc: Optional[float] = None
        if od and sp and od["price"] > 0 and od["price"] > sp["price"]:
            spot_disc = round((od["price"] - sp["price"]) / od["price"] * 100, 1)

        sl = sparklines.get(gpu, {"dates": [], "prices": []})

        cards.append(
            {
                "gpu_model":         gpu,
                "on_demand":         od,
                "spot":              sp,
                "reserved":          rv,
                "spot_discount_pct": spot_disc,
                "sparkline":         sl,
            }
        )

    cards.sort(key=lambda c: _gpu_sort_key(c["gpu_model"]))
    return cards


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
            ROUND(n.price, 4) AS now_price,
            ROUND(o.price, 4) AS old_price,
            ROUND((n.price - o.price) / o.price * 100, 1) AS pct_change
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

def _fmt(p: Optional[float]) -> str:
    return f"${p:.4f}" if p is not None else "—"


def _tier_block(
    label: str,
    chip_cls: str,
    info: Optional[dict],
    is_reserved: bool = False,
) -> str:
    if info is None:
        return (
            f'<div class="tier-block tier-empty">'
            f'<span class="chip {chip_cls}">{label}</span>'
            f'<div class="tier-price tier-na">—</div>'
            f'<div class="tier-meta">Not available</div>'
            f"</div>"
        )

    price   = _fmt(info["price"])
    prov    = info.get("provider") or ""
    region  = info.get("region") or ""
    ct      = info.get("commitment_term") or ""
    meta    = f"{prov} · {ct}" if (is_reserved and ct) else (f"{prov} · {region}" if region else prov)

    return (
        f'<div class="tier-block">'
        f'<span class="chip {chip_cls}">{label}</span>'
        f'<div class="tier-price">{price}<span class="per-hr">/hr</span></div>'
        f'<div class="tier-meta">{meta}</div>'
        f"</div>"
    )


def _gpu_card(card: dict, idx: int) -> str:
    gpu       = card["gpu_model"]
    od        = card["on_demand"]
    sp        = card["spot"]
    rv        = card["reserved"]
    disc      = card["spot_discount_pct"]
    sl        = card["sparkline"]
    color     = _CARD_COLORS[idx % len(_CARD_COLORS)]
    canvas_id = "c_" + gpu.replace(" ", "_").replace("/", "_").replace(".", "_")

    od_html = _tier_block("On-Demand", "chip-od",       od)
    sp_html = _tier_block("Spot / Int.", "chip-spot",   sp)
    rv_html = _tier_block("Reserved",  "chip-reserved", rv, is_reserved=True)

    banner = (
        f'<div class="spot-banner">Spot discount: &minus;{disc:.1f}%</div>'
        if disc is not None else ""
    )

    dates  = sl.get("dates", [])
    prices = sl.get("prices", [])
    if len(prices) >= 2:
        series = json.dumps({"labels": dates, "data": prices})
        # Use single-quoted attribute — JSON never contains unescaped single quotes
        sparkline_html = (
            f'<div class="sparkline-area">'
            f"<canvas id='{canvas_id}' class='sparkline-canvas' height='60' "
            f"data-series='{series}' data-color='{color}'></canvas>"
            f"</div>"
        )
    else:
        have = len(prices)
        sparkline_html = (
            f'<div class="sparkline-area building">'
            f"Building history&hellip; {have}/30 days"
            f"</div>"
        )

    return f"""<div class="gpu-card">
  <div class="card-header">
    <span class="gpu-name">{gpu}</span>
  </div>
  <div class="tier-blocks">
    {od_html}
    {sp_html}
    {rv_html}
  </div>
  {banner}
  {sparkline_html}
</div>"""


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
        pct = float(m["pct_change"])
        if pct > 0:
            cls, sign = "val-up", "+"
        elif pct < 0:
            cls, sign = "val-down", ""
        else:
            cls, sign = "val-flat", ""

        parts.append(
            f'<div class="mover-row">'
            f'<span class="mover-gpu">{m["gpu_model"]}</span>'
            f'<span class="mover-trail">'
            f'<span class="mover-prices">{_fmt(m["old_price"])} &rarr; {_fmt(m["now_price"])}</span>'
            f'<span class="mover-pct {cls}">{sign}{pct:.1f}%</span>'
            f"</span>"
            f"</div>"
        )
    return "\n".join(parts)


def _render_spread_leaders(leaders: list[dict]) -> str:
    if not leaders:
        return '<p class="notice-msg">No spot offers available for comparison.</p>'

    parts = []
    for lead in leaders:
        parts.append(
            f'<div class="spread-row">'
            f'<div class="spread-pct">{lead["spread_pct"]:.0f}%</div>'
            f'<div class="spread-body">'
            f'<div class="spread-gpu">{lead["gpu_model"]}</div>'
            f'<div class="spread-prices">'
            f'<span class="spread-od">{_fmt(lead["od_price"])} on-demand</span>'
            f'<span class="spread-arrow"> &rarr; </span>'
            f'<span class="spread-sp">{_fmt(lead["sp_price"])} spot</span>'
            f"</div>"
            f"</div>"
            f"</div>"
        )
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# CSS  (plain string — no f-string escaping needed)
# ---------------------------------------------------------------------------

CSS = """
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg0:#0A1628;
  --bg1:#0D1B3D;
  --card:#0F1F3D;
  --border:rgba(59,130,246,.20);
  --border-hi:rgba(59,130,246,.50);
  --accent:#3B82F6;
  --cyan:#06B6D4;
  --indigo:#6366F1;
  --green:#10B981;
  --red:#EF4444;
  --amber:#F59E0B;
  --text1:#FFFFFF;
  --text2:#94A3B8;
  --text3:#4B5E7A;
  --glow:0 0 24px rgba(59,130,246,.35);
  --r:16px;
}

html{
  background:linear-gradient(150deg,var(--bg0) 0%,var(--bg1) 100%);
  background-attachment:fixed;
  color:var(--text1);
  font-family:'Inter',system-ui,-apple-system,'Segoe UI',sans-serif;
  font-size:15px;
  line-height:1.5;
  min-height:100vh;
}

/* ── Page wrapper ─────────────────────────────────────────────── */
.page{max-width:1600px;margin:0 auto;padding:0 28px 72px}

/* ── Header ───────────────────────────────────────────────────── */
.site-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:24px 28px 20px;max-width:1600px;margin:0 auto;
  border-bottom:1px solid var(--border);margin-bottom:36px;
}
.hdr-left{display:flex;align-items:center;gap:14px}
.logo-mark{
  width:10px;height:10px;border-radius:50%;background:var(--accent);
  box-shadow:0 0 14px rgba(59,130,246,.7);flex-shrink:0;
}
.logo-brand{font-size:1.25rem;font-weight:800;letter-spacing:-.4px}
.logo-sub{font-size:.78rem;color:var(--text2);margin-top:2px}
.hdr-right .atlas-wordmark{
  font-size:1rem;font-weight:900;letter-spacing:.25em;
  color:var(--accent);text-transform:uppercase;
}
.hdr-right .snap-date{
  font-size:.72rem;color:var(--text3);text-align:right;margin-top:3px;
}

/* ── Hero stats ───────────────────────────────────────────────── */
.hero-stats{
  display:flex;flex-wrap:wrap;gap:14px;margin-bottom:40px;
}
.stat-pill{
  background:var(--card);border:1px solid var(--border);
  border-radius:12px;padding:14px 22px;display:flex;
  flex-direction:column;gap:5px;min-width:140px;
}
.stat-value{
  font-size:1.65rem;font-weight:800;
  background:linear-gradient(90deg,var(--accent),var(--cyan));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.stat-value.disc{
  background:linear-gradient(90deg,var(--cyan),var(--green));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.stat-label{
  font-size:.68rem;font-weight:700;letter-spacing:.1em;
  text-transform:uppercase;color:var(--text2);
}

/* ── Section header ───────────────────────────────────────────── */
.sec-hdr{
  display:flex;align-items:center;gap:12px;
  margin-bottom:20px;
}
.sec-label{
  font-size:.68rem;font-weight:800;letter-spacing:.12em;
  text-transform:uppercase;color:var(--accent);white-space:nowrap;
}
.sec-rule{flex:1;height:1px;background:var(--border)}
.section{margin-bottom:52px}

/* ── GPU card grid ────────────────────────────────────────────── */
.gpu-grid{
  display:grid;
  grid-template-columns:repeat(auto-fill,minmax(340px,1fr));
  gap:18px;
}
.gpu-card{
  background:var(--card);
  border:1px solid var(--border);
  border-radius:var(--r);
  padding:20px;
  display:flex;flex-direction:column;gap:0;
  transition:border-color .2s,box-shadow .2s;
}
.gpu-card:hover{border-color:var(--border-hi);box-shadow:var(--glow)}

.card-header{margin-bottom:14px}
.gpu-name{
  font-size:1.05rem;font-weight:700;color:var(--text1);
  line-height:1.2;display:block;
}

/* ── Tier blocks ──────────────────────────────────────────────── */
.tier-blocks{
  display:grid;grid-template-columns:1fr 1fr 1fr;gap:8px;
  margin-bottom:12px;
}
.tier-block{
  background:rgba(255,255,255,.03);
  border:1px solid rgba(255,255,255,.06);
  border-radius:10px;padding:10px 10px 9px;
  display:flex;flex-direction:column;gap:5px;
  min-width:0;
}
.tier-empty{opacity:.45}

/* Chips */
.chip{
  display:inline-block;
  font-size:.6rem;font-weight:800;letter-spacing:.08em;
  text-transform:uppercase;border-radius:5px;
  padding:2px 6px;line-height:1.5;white-space:nowrap;
}
.chip-od      {background:rgba(59,130,246,.15);color:#3B82F6;border:1px solid rgba(59,130,246,.3)}
.chip-spot    {background:rgba(6,182,212,.15); color:#06B6D4;border:1px solid rgba(6,182,212,.3)}
.chip-reserved{background:rgba(99,102,241,.15);color:#6366F1;border:1px solid rgba(99,102,241,.3)}

.tier-price{
  font-size:1.05rem;font-weight:700;color:var(--text1);line-height:1;
}
.tier-na{color:var(--text3)}
.per-hr{font-size:.7rem;font-weight:400;color:var(--text2);margin-left:1px}
.tier-meta{
  font-size:.7rem;color:var(--text2);white-space:nowrap;
  overflow:hidden;text-overflow:ellipsis;
}

/* ── Spot discount banner ─────────────────────────────────────── */
.spot-banner{
  background:rgba(6,182,212,.08);
  border:1px solid rgba(6,182,212,.22);
  border-radius:8px;padding:5px 12px;
  font-size:.78rem;font-weight:700;
  color:var(--cyan);text-align:center;
  box-shadow:0 0 10px rgba(6,182,212,.12);
  margin-bottom:12px;
}

/* ── Sparkline ────────────────────────────────────────────────── */
.sparkline-area{
  height:60px;margin-top:auto;padding-top:4px;
}
.sparkline-canvas{display:block;width:100%!important}
.sparkline-area.building{
  display:flex;align-items:center;justify-content:center;
  font-size:.72rem;color:var(--text3);font-style:italic;
}

/* ── Bottom two-column layout ─────────────────────────────────── */
.two-col{
  display:grid;
  grid-template-columns:1fr 1fr;
  gap:32px;
}
@media(max-width:900px){.two-col{grid-template-columns:1fr}}

/* ── Biggest movers ───────────────────────────────────────────── */
.mover-row{
  display:flex;align-items:center;justify-content:space-between;
  padding:10px 0;border-bottom:1px solid var(--border);
}
.mover-row:last-child{border-bottom:none}
.mover-gpu{font-weight:600;font-size:.9rem;color:var(--text1)}
.mover-trail{display:flex;align-items:center;gap:16px}
.mover-prices{font-size:.78rem;color:var(--text2)}
.mover-pct{font-weight:700;font-size:.95rem;min-width:64px;text-align:right}
.val-up  {color:var(--red)}
.val-down{color:var(--green)}
.val-flat{color:var(--text3)}

/* ── Spread leaders ───────────────────────────────────────────── */
.spread-row{
  display:flex;align-items:center;gap:18px;
  padding:12px 0;border-bottom:1px solid var(--border);
}
.spread-row:last-child{border-bottom:none}
.spread-pct{
  font-size:1.6rem;font-weight:800;min-width:68px;text-align:right;
  background:linear-gradient(90deg,var(--cyan),var(--accent));
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;
}
.spread-body{display:flex;flex-direction:column;gap:3px;min-width:0}
.spread-gpu{font-weight:700;font-size:.9rem;color:var(--text1)}
.spread-prices{font-size:.78rem}
.spread-od{color:var(--text2)}
.spread-arrow{color:var(--text3);margin:0 3px}
.spread-sp{color:var(--cyan);font-weight:600}

/* ── Misc ─────────────────────────────────────────────────────── */
.notice-msg{color:var(--text3);font-style:italic;font-size:.88rem;padding:8px 0}

/* ── Footer ───────────────────────────────────────────────────── */
.site-footer{
  border-top:1px solid var(--border);
  padding:28px 28px 0;max-width:1600px;margin:0 auto;
  font-size:.75rem;color:var(--text3);
  display:flex;justify-content:space-between;align-items:center;
}
.site-footer a{color:var(--accent);text-decoration:none}
"""

# ---------------------------------------------------------------------------
# JavaScript  (plain string — chart init + relative timestamp)
# ---------------------------------------------------------------------------

JS = """
/* Chart.js sparklines --------------------------------------------------- */
document.querySelectorAll('.sparkline-canvas').forEach(function(canvas) {
  var raw   = canvas.dataset.series;
  var color = canvas.dataset.color || '#3B82F6';
  if (!raw) return;
  var series;
  try { series = JSON.parse(raw); } catch(e) { return; }
  if (!series.data || series.data.length < 2) return;

  new Chart(canvas, {
    type: 'line',
    data: {
      labels: series.labels,
      datasets: [{
        data:             series.data,
        borderColor:      color,
        backgroundColor:  color + '18',
        borderWidth:      2,
        pointRadius:      series.data.length <= 8 ? 2 : 0,
        pointHoverRadius: 4,
        fill:             true,
        tension:          0.35,
      }]
    },
    options: {
      responsive:          true,
      maintainAspectRatio: false,
      animation:           { duration: 500 },
      plugins: {
        legend:  { display: false },
        tooltip: {
          callbacks: {
            label: function(ctx) {
              return '$' + ctx.parsed.y.toFixed(4) + '/hr';
            }
          }
        }
      },
      scales: {
        x: {
          display:    series.data.length > 3,
          grid:       { color: 'rgba(30,45,69,.8)' },
          ticks: {
            color:          '#4B5E7A',
            font:           { size: 9 },
            maxTicksLimit:  5,
            maxRotation:    0,
          }
        },
        y: {
          display: true,
          grid:    { color: 'rgba(30,45,69,.8)' },
          ticks: {
            color:         '#4B5E7A',
            font:          { size: 9 },
            maxTicksLimit: 4,
            callback: function(v) { return '$' + v.toFixed(2); }
          }
        }
      }
    }
  });
});

/* Relative timestamp ---------------------------------------------------- */
(function() {
  var el = document.getElementById('rel-time');
  if (!el) return;
  var ts = new Date(el.dataset.ts);
  if (isNaN(ts)) return;
  var mins = Math.round((Date.now() - ts) / 60000);
  var txt;
  if (mins < 2)        txt = 'just now';
  else if (mins < 60)  txt = mins + ' min ago';
  else if (mins < 1440) txt = Math.round(mins/60) + ' hr ago';
  else                  txt = Math.round(mins/1440) + ' days ago';
  el.textContent = txt;
})();
"""


# ---------------------------------------------------------------------------
# HTML assembler
# ---------------------------------------------------------------------------

def generate_html(
    hero:    dict,
    cards:   list[dict],
    movers:  dict,
    leaders: list[dict],
    gen_ts:  str,
) -> str:
    snap_date    = hero.get("latest_date") or "N/A"
    gpu_count    = hero.get("gpu_count", 0)
    prov_count   = hero.get("provider_count", 0)
    day_count    = hero.get("day_count", 0)
    total_rows   = hero.get("total_rows", 0)
    median_disc  = hero.get("median_spot_discount")

    disc_str = f"&minus;{median_disc:.1f}%" if median_disc is not None else "N/A"

    # --- Hero pills ---------------------------------------------------------
    hero_html = (
        f'<div class="stat-pill">'
        f'<span class="stat-value">{gpu_count}</span>'
        f'<span class="stat-label">GPU Models</span></div>\n'
        f'<div class="stat-pill">'
        f'<span class="stat-value">{prov_count}</span>'
        f'<span class="stat-label">Providers</span></div>\n'
        f'<div class="stat-pill">'
        f'<span class="stat-value">{day_count}</span>'
        f'<span class="stat-label">Days of History</span></div>\n'
        f'<div class="stat-pill">'
        f'<span class="stat-value">{total_rows:,}</span>'
        f'<span class="stat-label">Price Rows</span></div>\n'
        f'<div class="stat-pill">'
        f'<span class="stat-value disc">{disc_str}</span>'
        f'<span class="stat-label">Median Spot Discount</span></div>'
    )

    # --- GPU cards ----------------------------------------------------------
    if cards:
        cards_html = "\n".join(_gpu_card(c, i) for i, c in enumerate(cards))
    else:
        cards_html = '<p class="notice-msg">No price data yet — run a scrape first.</p>'

    # --- Movers + spread leaders --------------------------------------------
    movers_html  = _render_movers(movers)
    leaders_html = _render_spread_leaders(leaders)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Atlas GPU Pricing Dashboard</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet" />
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>{CSS}</style>
</head>
<body>

<header class="site-header">
  <div class="hdr-left">
    <div class="logo-mark"></div>
    <div>
      <div class="logo-brand">Atlas GPU Pricing</div>
      <div class="logo-sub">H100 &middot; H200 &middot; B200 &middot; A100 across 14 providers</div>
    </div>
  </div>
  <div class="hdr-right">
    <div class="atlas-wordmark">Atlas</div>
    <div class="snap-date">
      Snapshot {snap_date} &middot;
      <span id="rel-time" data-ts="{gen_ts}">...</span>
    </div>
  </div>
</header>

<div class="page">

  <!-- Hero stats -->
  <div class="hero-stats">
    {hero_html}
  </div>

  <!-- GPU pricing matrix -->
  <section class="section">
    <div class="sec-hdr">
      <span class="sec-label">GPU Pricing Matrix</span>
      <div class="sec-rule"></div>
      <span style="font-size:.72rem;color:var(--text3);white-space:nowrap">
        on-demand &middot; spot &middot; reserved &middot; min $/GPU/hr
      </span>
    </div>
    <div class="gpu-grid">
{cards_html}
    </div>
  </section>

  <!-- Movers + Spread side by side -->
  <div class="two-col">
    <section class="section">
      <div class="sec-hdr">
        <span class="sec-label">Biggest Movers (7 days)</span>
        <div class="sec-rule"></div>
      </div>
      {movers_html}
    </section>

    <section class="section">
      <div class="sec-hdr">
        <span class="sec-label">Market Spread Leaders</span>
        <div class="sec-rule"></div>
      </div>
      {leaders_html}
    </section>
  </div>

</div>

<footer class="site-footer">
  <span>Automated daily scrape via
    <a href="https://github.com/romaindeniau75-commits/gpu-price-scraper/actions">GitHub Actions</a>
    &middot; 06:00 UTC &middot; on-demand prices, per GPU, USD
  </span>
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

    con     = _connect()
    gen_ts  = datetime.now(timezone.utc).isoformat()

    hero    = _build_hero_stats(con)
    cards   = _build_gpu_cards(con)
    movers  = _build_movers(con, hero["day_count"])
    leaders = _build_spread_leaders(con)
    con.close()

    # Write data.json (lightweight summary for external consumers)
    data = {
        "generated_at":  gen_ts,
        "hero":          hero,
        "movers":        movers,
        "spread_leaders": leaders,
    }
    (DOCS_DIR / "data.json").write_text(json.dumps(data, indent=2))

    # Write index.html
    html = generate_html(hero, cards, movers, leaders, gen_ts)
    (DOCS_DIR / "index.html").write_text(html)

    print(f"docs/index.html  {len(html):,} chars")
    print(f"  GPU models   : {hero['gpu_count']}")
    print(f"  Providers    : {hero['provider_count']}")
    print(f"  Days tracked : {hero['day_count']}")
    print(f"  Total rows   : {hero['total_rows']:,}")
    print(f"  Median disc  : {hero['median_spot_discount']}")
    print(f"  Spread leaders: {len(leaders)}")
    print(f"  Cards rendered: {len(cards)}")


if __name__ == "__main__":
    main()
