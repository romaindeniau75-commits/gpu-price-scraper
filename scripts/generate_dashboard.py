#!/usr/bin/env python3
"""Generate docs/index.html and docs/data.json from data/pricing_history.db.

Run manually:
    python3 scripts/generate_dashboard.py

Run in CI:
    Called automatically by .github/workflows/scrape-daily.yml after each scrape.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
ROOT = Path(__file__).parent.parent
HISTORY_DB = ROOT / "data" / "pricing_history.db"
DOCS_DIR = ROOT / "docs"
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    if not HISTORY_DB.exists():
        sys.exit(f"ERROR: {HISTORY_DB} not found. Run fetch --save-db first.")
    con = sqlite3.connect(str(HISTORY_DB))
    con.row_factory = sqlite3.Row
    return con


def _build_data(con: sqlite3.Connection) -> dict:
    # ── 1. 30-day sparklines (min on_demand price per GPU per day) ──────────
    sparkline_rows = con.execute(
        """
        SELECT snapshot_date,
               gpu_model,
               MIN(price_per_gpu_hour) AS min_price
        FROM   price_history
        WHERE  availability_tier = 'on_demand'
          AND  snapshot_date >= date('now', '-30 days')
        GROUP  BY snapshot_date, gpu_model
        ORDER  BY gpu_model, snapshot_date
        """
    ).fetchall()

    sparklines: dict[str, dict] = defaultdict(lambda: {"dates": [], "prices": []})
    for r in sparkline_rows:
        sparklines[r["gpu_model"]]["dates"].append(r["snapshot_date"])
        sparklines[r["gpu_model"]]["prices"].append(round(r["min_price"], 4))

    # ── 2. Latest prices table ───────────────────────────────────────────────
    latest_date_row = con.execute(
        "SELECT MAX(snapshot_date) AS d FROM price_history"
    ).fetchone()
    latest_date: str = latest_date_row["d"] or ""

    latest_rows = con.execute(
        """
        SELECT gpu_model,
               provider,
               region_canonical,
               availability_tier,
               price_per_gpu_hour
        FROM   price_history
        WHERE  snapshot_date     = ?
          AND  availability_tier = 'on_demand'
        ORDER  BY gpu_model, price_per_gpu_hour
        """,
        (latest_date,),
    ).fetchall()

    # Group by GPU: keep cheapest provider
    latest_by_gpu: dict[str, dict] = {}
    for r in latest_rows:
        gpu = r["gpu_model"]
        if gpu not in latest_by_gpu:
            latest_by_gpu[gpu] = {
                "gpu_model": gpu,
                "best_provider": r["provider"],
                "best_region": r["region_canonical"],
                "best_price": round(r["price_per_gpu_hour"], 4),
                "offer_count": 1,
            }
        else:
            latest_by_gpu[gpu]["offer_count"] += 1

    latest = sorted(latest_by_gpu.values(), key=lambda x: x["gpu_model"])

    # ── 3. 7-day biggest movers ──────────────────────────────────────────────
    movers_rows = con.execute(
        """
        WITH now_prices AS (
            SELECT gpu_model,
                   AVG(price_per_gpu_hour) AS price
            FROM   price_history
            WHERE  snapshot_date     = ?
              AND  availability_tier = 'on_demand'
            GROUP  BY gpu_model
        ),
        old_prices AS (
            SELECT gpu_model,
                   AVG(price_per_gpu_hour) AS price
            FROM   price_history
            WHERE  snapshot_date <= date(?, '-7 days')
              AND  availability_tier = 'on_demand'
            GROUP  BY gpu_model
        )
        SELECT n.gpu_model,
               ROUND(n.price, 4)                          AS now_price,
               ROUND(o.price, 4)                          AS old_price,
               ROUND((n.price - o.price) / o.price * 100, 1) AS pct_change
        FROM   now_prices n
        JOIN   old_prices o ON n.gpu_model = o.gpu_model
        ORDER  BY ABS(pct_change) DESC
        LIMIT  8
        """,
        (latest_date, latest_date),
    ).fetchall()

    movers = [dict(r) for r in movers_rows]

    # ── 4. Stats ─────────────────────────────────────────────────────────────
    stats_row = con.execute(
        """
        SELECT COUNT(DISTINCT gpu_model)   AS gpu_count,
               COUNT(DISTINCT provider)    AS provider_count,
               COUNT(DISTINCT snapshot_date) AS day_count,
               COUNT(*)                    AS total_rows
        FROM   price_history
        """
    ).fetchone()

    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "latest_snapshot_date": latest_date,
        "stats": dict(stats_row),
        "sparklines": dict(sparklines),
        "latest": latest,
        "movers": movers,
    }


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

_CHART_COLORS = [
    "#3B82F6",  # blue
    "#10B981",  # green
    "#F59E0B",  # amber
    "#EF4444",  # red
    "#8B5CF6",  # violet
    "#EC4899",  # pink
    "#06B6D4",  # cyan
    "#F97316",  # orange
    "#84CC16",  # lime
    "#6366F1",  # indigo
    "#14B8A6",  # teal
    "#E879F9",  # fuchsia
]

_TIER_BADGES = {
    "on_demand":     ("OD",   "#3B82F6"),
    "spot":          ("SPOT", "#F59E0B"),
    "interruptible": ("INT",  "#F97316"),
    "community":     ("COMM", "#10B981"),
    "reserved":      ("RES",  "#8B5CF6"),
}


def _mover_arrow(pct: float) -> str:
    if pct > 0:
        return f'<span class="mover-up">▲ {pct:+.1f}%</span>'
    elif pct < 0:
        return f'<span class="mover-down">▼ {pct:.1f}%</span>'
    else:
        return f'<span class="mover-flat">— 0.0%</span>'


def _generate_sparkline_cards(sparklines: dict, colors: list) -> str:
    cards = []
    for i, (gpu, series) in enumerate(
        sorted(sparklines.items(), key=lambda kv: kv[0])
    ):
        color = colors[i % len(colors)]
        dates = series["dates"]
        prices = series["prices"]
        if not prices:
            continue
        latest_price = prices[-1] if prices else 0.0
        first_price = prices[0] if prices else 0.0
        trend = "up" if latest_price > first_price else "down" if latest_price < first_price else "flat"
        trend_icon = "▲" if trend == "up" else "▼" if trend == "down" else "—"
        trend_class = "trend-up" if trend == "up" else "trend-down" if trend == "down" else "trend-flat"
        canvas_id = f"chart_{gpu.replace(' ', '_').replace('/', '_')}"

        series_json = json.dumps({"labels": dates, "data": prices})

        cards.append(
            f"""
    <div class="gpu-card" data-series='{series_json}' data-canvas="{canvas_id}" data-color="{color}">
      <div class="card-header">
        <span class="gpu-name">{gpu}</span>
        <span class="gpu-price">${latest_price:.4f}/hr</span>
      </div>
      <div class="card-sub">
        <span class="{trend_class}">{trend_icon} {len(dates)}-day range:
          ${min(prices):.4f} – ${max(prices):.4f}</span>
      </div>
      <canvas id="{canvas_id}" class="sparkline-canvas" height="60"></canvas>
    </div>"""
        )
    return "\n".join(cards)


def _generate_latest_table(latest: list) -> str:
    if not latest:
        return '<p class="empty-msg">No data yet — run a scrape first.</p>'
    rows = []
    for r in latest:
        rows.append(
            f"""    <tr>
      <td class="gpu-cell">{r['gpu_model']}</td>
      <td>${r['best_price']:.4f}</td>
      <td>{r['best_provider']}</td>
      <td>{r['best_region']}</td>
      <td class="count-cell">{r['offer_count']}</td>
    </tr>"""
        )
    return "\n".join(rows)


def _generate_movers_section(movers: list) -> str:
    if not movers:
        return '<p class="empty-msg">Need ≥ 8 days of history for mover analysis.</p>'
    items = []
    for m in movers:
        pct = float(m["pct_change"])
        items.append(
            f"""    <div class="mover-item">
      <span class="mover-gpu">{m['gpu_model']}</span>
      <span class="mover-prices">${m['old_price']:.4f} → ${m['now_price']:.4f}</span>
      {_mover_arrow(pct)}
    </div>"""
        )
    return "\n".join(items)


def generate_html(data: dict) -> str:
    gen_ts = data.get("generated_at", "")
    snap_date = data.get("latest_snapshot_date", "N/A")
    stats = data.get("stats", {})
    gpu_count = stats.get("gpu_count", 0)
    provider_count = stats.get("provider_count", 0)
    day_count = stats.get("day_count", 0)
    total_rows = stats.get("total_rows", 0)

    sparklines = data.get("sparklines", {})
    latest = data.get("latest", [])
    movers = data.get("movers", [])

    sparkline_cards = _generate_sparkline_cards(sparklines, _CHART_COLORS)
    latest_table = _generate_latest_table(latest)
    movers_html = _generate_movers_section(movers)

    # Inline the full data JSON so Charts can be initialised in <script>
    data_json = json.dumps(data)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Atlas GPU Pricing Dashboard</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    /* ── Reset & base ─────────────────────────────────────────────────── */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    :root {{
      --bg:        #0A1628;
      --card-bg:   #0F1E35;
      --border:    #1E2D45;
      --accent:    #3B82F6;
      --accent-lo: rgba(59,130,246,.12);
      --text:      #E2E8F0;
      --muted:     #64748B;
      --green:     #10B981;
      --red:       #EF4444;
      --amber:     #F59E0B;
      --glow:      0 0 20px rgba(59,130,246,.25);
    }}
    html {{ background: var(--bg); color: var(--text); font-family: 'Inter', system-ui, sans-serif; font-size: 15px; }}
    a {{ color: var(--accent); text-decoration: none; }}

    /* ── Layout ────────────────────────────────────────────────────────── */
    .page {{ max-width: 1400px; margin: 0 auto; padding: 32px 20px 64px; }}

    /* ── Header ────────────────────────────────────────────────────────── */
    .site-header {{
      display: flex; align-items: center; justify-content: space-between;
      border-bottom: 1px solid var(--border); padding-bottom: 20px; margin-bottom: 32px;
    }}
    .logo {{ display: flex; align-items: center; gap: 12px; }}
    .logo-dot {{ width: 10px; height: 10px; border-radius: 50%; background: var(--accent); box-shadow: var(--glow); }}
    .logo-text {{ font-size: 1.3rem; font-weight: 700; letter-spacing: -.5px; }}
    .logo-sub {{ font-size: .75rem; color: var(--muted); margin-top: 2px; }}
    .header-meta {{ text-align: right; font-size: .8rem; color: var(--muted); line-height: 1.6; }}
    .header-meta strong {{ color: var(--text); }}

    /* ── Stat pills ────────────────────────────────────────────────────── */
    .stat-row {{ display: flex; gap: 16px; flex-wrap: wrap; margin-bottom: 32px; }}
    .stat-pill {{
      background: var(--card-bg); border: 1px solid var(--border);
      border-radius: 10px; padding: 12px 20px;
      display: flex; flex-direction: column; gap: 4px;
    }}
    .stat-value {{ font-size: 1.5rem; font-weight: 700; color: var(--accent); }}
    .stat-label {{ font-size: .75rem; color: var(--muted); text-transform: uppercase; letter-spacing: .05em; }}

    /* ── Section titles ────────────────────────────────────────────────── */
    .section-title {{
      font-size: .7rem; font-weight: 700; letter-spacing: .12em; text-transform: uppercase;
      color: var(--accent); margin-bottom: 16px; display: flex; align-items: center; gap: 8px;
    }}
    .section-title::after {{ content: ''; flex: 1; height: 1px; background: var(--border); }}

    /* ── GPU sparkline cards ───────────────────────────────────────────── */
    .gpu-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(280px, 1fr));
      gap: 16px;
      margin-bottom: 40px;
    }}
    .gpu-card {{
      background: var(--card-bg); border: 1px solid var(--border);
      border-radius: 12px; padding: 16px 18px;
      transition: border-color .2s, box-shadow .2s;
    }}
    .gpu-card:hover {{ border-color: var(--accent); box-shadow: var(--glow); }}
    .card-header {{ display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 4px; }}
    .gpu-name {{ font-weight: 700; font-size: .95rem; }}
    .gpu-price {{ font-size: 1.05rem; font-weight: 700; color: var(--accent); }}
    .card-sub {{ font-size: .75rem; color: var(--muted); margin-bottom: 10px; }}
    .trend-up   {{ color: var(--red); }}
    .trend-down {{ color: var(--green); }}
    .trend-flat {{ color: var(--muted); }}
    .sparkline-canvas {{ width: 100% !important; display: block; }}

    /* ── Latest prices table ───────────────────────────────────────────── */
    .table-wrap {{ overflow-x: auto; margin-bottom: 40px; }}
    table {{ width: 100%; border-collapse: collapse; font-size: .875rem; }}
    thead tr {{ border-bottom: 2px solid var(--border); }}
    th {{ text-align: left; padding: 10px 14px; color: var(--muted); font-weight: 600;
          font-size: .7rem; text-transform: uppercase; letter-spacing: .06em; }}
    tbody tr {{ border-bottom: 1px solid var(--border); transition: background .15s; }}
    tbody tr:hover {{ background: var(--accent-lo); }}
    td {{ padding: 10px 14px; }}
    .gpu-cell {{ font-weight: 600; color: var(--text); }}
    .count-cell {{ color: var(--muted); text-align: right; }}

    /* ── Biggest movers ────────────────────────────────────────────────── */
    .movers-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
      gap: 12px;
      margin-bottom: 40px;
    }}
    .mover-item {{
      background: var(--card-bg); border: 1px solid var(--border);
      border-radius: 10px; padding: 14px 16px;
      display: flex; flex-direction: column; gap: 4px;
    }}
    .mover-gpu {{ font-weight: 700; font-size: .9rem; }}
    .mover-prices {{ font-size: .78rem; color: var(--muted); }}
    .mover-up   {{ color: var(--red);   font-weight: 700; font-size: .95rem; }}
    .mover-down {{ color: var(--green); font-weight: 700; font-size: .95rem; }}
    .mover-flat {{ color: var(--muted); font-weight: 700; font-size: .95rem; }}

    .empty-msg {{ color: var(--muted); font-style: italic; margin-bottom: 24px; }}

    /* ── Footer ────────────────────────────────────────────────────────── */
    .site-footer {{
      border-top: 1px solid var(--border); padding-top: 24px;
      font-size: .78rem; color: var(--muted); text-align: center;
    }}
  </style>
</head>
<body>
<div class="page">

  <!-- Header -->
  <header class="site-header">
    <div class="logo">
      <div class="logo-dot"></div>
      <div>
        <div class="logo-text">Atlas GPU Pricing</div>
        <div class="logo-sub">H100 · A100 · L4 · A10G across 14 providers</div>
      </div>
    </div>
    <div class="header-meta">
      <strong>Latest snapshot:</strong> {snap_date}<br>
      Updated <span id="rel-time">{gen_ts}</span>
    </div>
  </header>

  <!-- Stat pills -->
  <div class="stat-row">
    <div class="stat-pill"><span class="stat-value">{gpu_count}</span><span class="stat-label">GPU Models</span></div>
    <div class="stat-pill"><span class="stat-value">{provider_count}</span><span class="stat-label">Providers</span></div>
    <div class="stat-pill"><span class="stat-value">{day_count}</span><span class="stat-label">Days of History</span></div>
    <div class="stat-pill"><span class="stat-value">{total_rows:,}</span><span class="stat-label">Price Rows</span></div>
  </div>

  <!-- Sparklines -->
  <h2 class="section-title">30-Day Price Evolution (on-demand min $/hr)</h2>
  <div class="gpu-grid">
{sparkline_cards}
  </div>

  <!-- Latest prices -->
  <h2 class="section-title">Latest On-Demand Prices</h2>
  <div class="table-wrap">
    <table>
      <thead>
        <tr>
          <th>GPU Model</th>
          <th>Best Price ($/hr)</th>
          <th>Best Provider</th>
          <th>Region</th>
          <th style="text-align:right">Offers</th>
        </tr>
      </thead>
      <tbody>
{latest_table}
      </tbody>
    </table>
  </div>

  <!-- Biggest movers -->
  <h2 class="section-title">Biggest Movers — Last 7 Days</h2>
  <div class="movers-grid">
{movers_html}
  </div>

  <footer class="site-footer">
    Data collected automatically every day at 06:00 UTC via
    <a href="https://github.com/actions">GitHub Actions</a>.
    Prices are on-demand, per-GPU, in USD.
    Source: <a href="https://github.com/">github.com/...</a>
  </footer>

</div>

<script>
/* ── Inline data ─────────────────────────────────────────────────────────── */
const DASHBOARD_DATA = {data_json};

/* ── Render Chart.js sparklines ─────────────────────────────────────────── */
document.querySelectorAll('.gpu-card').forEach(card => {{
  const canvasId = card.dataset.canvas;
  const color    = card.dataset.color;
  const series   = JSON.parse(card.dataset.series);

  const ctx = document.getElementById(canvasId);
  if (!ctx || !series.data.length) return;

  new Chart(ctx, {{
    type: 'line',
    data: {{
      labels: series.labels,
      datasets: [{{
        data:            series.data,
        borderColor:     color,
        backgroundColor: color + '22',
        borderWidth:     2,
        pointRadius:     series.data.length <= 7 ? 3 : 0,
        pointHoverRadius: 4,
        fill:            true,
        tension:         0.3,
      }}],
    }},
    options: {{
      responsive:         true,
      maintainAspectRatio: false,
      animation:          {{ duration: 400 }},
      plugins: {{
        legend: {{ display: false }},
        tooltip: {{
          callbacks: {{
            label: ctx => '$' + ctx.parsed.y.toFixed(4) + '/hr',
          }},
        }},
      }},
      scales: {{
        x: {{
          display: series.data.length > 3,
          ticks: {{
            color:    '#64748B',
            font:     {{ size: 9 }},
            maxTicksLimit: 5,
            maxRotation: 0,
          }},
          grid: {{ color: '#1E2D45' }},
        }},
        y: {{
          display: true,
          ticks: {{
            color:    '#64748B',
            font:     {{ size: 9 }},
            maxTicksLimit: 4,
            callback: v => '$' + v.toFixed(2),
          }},
          grid: {{ color: '#1E2D45' }},
        }},
      }},
    }},
  }});
}});

/* ── Relative timestamp ─────────────────────────────────────────────────── */
(function() {{
  const el = document.getElementById('rel-time');
  if (!el) return;
  const ts = new Date(el.textContent);
  if (isNaN(ts)) return;
  const mins = Math.round((Date.now() - ts) / 60000);
  if (mins < 60)       el.textContent = mins + ' min ago';
  else if (mins < 1440) el.textContent = Math.round(mins/60) + ' hr ago';
  else                  el.textContent = Math.round(mins/1440) + ' days ago';
}})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    con = _connect()
    data = _build_data(con)
    con.close()

    # Write data.json
    data_path = DOCS_DIR / "data.json"
    data_path.write_text(json.dumps(data, indent=2))

    # Write index.html
    html = generate_html(data)
    (DOCS_DIR / "index.html").write_text(html)

    # Ensure .nojekyll exists
    (DOCS_DIR / ".nojekyll").touch()

    stats = data.get("stats", {})
    print(f"✓ docs/index.html generated")
    print(f"  GPU models   : {stats.get('gpu_count', 0)}")
    print(f"  Providers    : {stats.get('provider_count', 0)}")
    print(f"  Days tracked : {stats.get('day_count', 0)}")
    print(f"  Total rows   : {stats.get('total_rows', 0):,}")
    print(f"  Snapshot date: {data.get('latest_snapshot_date', 'N/A')}")


if __name__ == "__main__":
    main()
