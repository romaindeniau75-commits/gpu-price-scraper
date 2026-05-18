# Atlas GPU Price Scraper

Real-time H100 / A100 cloud GPU pricing across 14 providers, with persistent storage, arbitrage analytics, and a Streamlit dashboard.

---

## Features

| Layer | What it does |
|-------|-------------|
| **Scraper** | Fetches live prices from 14 providers (RunPod, Lambda Labs, Vast.ai, CoreWeave, Paperspace, TensorDock, AWS, GCP, Azure, OCI, DataCrunch, Crusoe, Hyperstack, Nebius) |
| **Tiers** | Tracks `on_demand`, `spot`, `interruptible`, `community`, and `reserved` per offer; computes `interruptible` flag automatically |
| **Storage** | Persists every observation to SQLite with full audit trail including availability tier and region canonical bucket |
| **Analytics** | Computes min/median/max, market spread (on-demand only), spot discount view, and ranked arbitrage opportunities |
| **Dashboard** | 5-page Streamlit app — market overview, arbitrage, provider comparison, historical trends, routing simulator |
| **Automation** | `watch` mode re-scrapes on a configurable interval and logs price changes |

---

## Installation

```bash
cd gpu-price-scraper
pip install httpx pydantic click rich selectolax python-dotenv anyio \
            pandas streamlit plotly
```

Optional — Lambda Labs live pricing (other providers work without any API key):

```bash
export LAMBDA_API_KEY=your_key_here
```

---

## Quick start

```bash
# 1. Initialise the database
python3 -m gpu_scraper.cli init-db

# 2. Fetch current prices and save to DB
python3 -m gpu_scraper.cli fetch --gpu H100 --save-db

# 3. View arbitrage opportunities
python3 -m gpu_scraper.cli opportunities --gpu H100

# 4. Launch the dashboard
streamlit run dashboard/app.py
```

---

## CLI reference

All commands accept `--db-path` (default `./data/gpu_prices.db`) and honour the `GPU_SCRAPER_DB_PATH` environment variable.

### `init-db`

Create or upgrade the SQLite schema.

```bash
python3 -m gpu_scraper.cli init-db
python3 -m gpu_scraper.cli --db-path /data/prod.db init-db
```

### `fetch`

Scrape current prices from all (or selected) providers.

```bash
# H100 only, all providers, save to DB
python3 -m gpu_scraper.cli fetch --gpu H100 --save-db

# Specific providers, spot only, export CSV+JSON
python3 -m gpu_scraper.cli fetch \
    --provider RunPod --provider Vast.ai \
    --contract spot \
    --export --output-dir ./exports

# All GPU types, verbose HTTP logging
python3 -m gpu_scraper.cli fetch --no-filter --verbose
```

### `opportunities`

Show ranked arbitrage opportunities from stored data.

```bash
# H100 globally, last 24 h
python3 -m gpu_scraper.cli opportunities --gpu H100

# A100 in Europe, last 48 h, top 10, min score 0.4
python3 -m gpu_scraper.cli opportunities \
    --gpu A100 --region Europe \
    --hours 48 --top-n 10 --min-score 0.4
```

**Output columns:**

| Column | Description |
|--------|-------------|
| Score | Opportunity score 0–1 (higher = better) |
| GPU | Normalised GPU model |
| Provider | Where to buy |
| Buy $/hr | Per-GPU price at this provider |
| Median | Market median across all providers |
| Discount | % below market median |
| Spread | Max − min across entire market |
| Save/mo | USD saved per GPU per month vs median |

### `watch`

Continuous re-scrape loop with price-change notifications.

```bash
# Re-scrape every 15 min, save every snapshot to DB
python3 -m gpu_scraper.cli watch --interval 15 --gpu H100 --save-db

# Export CSV+JSON on every tick too
python3 -m gpu_scraper.cli watch --interval 30 --save-db --export

# Specific providers, all GPU types
python3 -m gpu_scraper.cli watch \
    --provider RunPod --provider Azure \
    --interval 10 --no-filter --save-db
```

### `list-providers`

```bash
python3 -m gpu_scraper.cli list-providers
```

---

## Dashboard

```bash
streamlit run dashboard/app.py
```

Open `http://localhost:8501` in your browser.

### Pages

| Page | Description |
|------|-------------|
| 📊 Market Overview | Live price table, GPU price range chart, provider distribution |
| ⚡ Arbitrage Opportunities | Ranked table + scatter plot of discount vs score |
| 🏢 Provider Comparison | Heatmap (provider × GPU), average price bars |
| 📈 Historical Trends | Min / Median / Max ribbon charts over time |
| 🚀 Atlas Routing | Input workload spec → ranked provider recommendations |

**Global filters** (sidebar): GPU family, contract type, time window, region free-text.

Use the **🔄 Refresh data** button or set `GPU_SCRAPER_DB_PATH` to point at your database.

---

## Database schema

```sql
-- Main observations table
CREATE TABLE gpu_price_observations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL,        -- ISO-8601 UTC
    scrape_run_id         TEXT    NOT NULL,        -- groups observations from one fetch
    provider              TEXT    NOT NULL,
    gpu_model_raw         TEXT,                   -- original string from provider
    gpu_model_normalized  TEXT    NOT NULL,        -- canonical: "H100 SXM", "A100 80GB", …
    region                TEXT,
    region_canonical      TEXT    NOT NULL DEFAULT 'unknown', -- canonical bucket: us-east, eu-west, …
    country               TEXT,                   -- derived: "US", "EU", "APAC", …
    price_per_hour_usd    REAL    NOT NULL,        -- raw API price (may be per-node)
    price_unit            TEXT    NOT NULL DEFAULT 'per_gpu', -- per_gpu | per_node
    price_per_gpu_hour    REAL    NOT NULL DEFAULT 0.0,       -- normalised $/GPU/hr
    currency              TEXT    NOT NULL DEFAULT 'USD',
    contract_type         TEXT,                   -- on-demand | spot | reserved (legacy compat)
    availability_tier     TEXT    NOT NULL DEFAULT 'on_demand', -- on_demand|spot|interruptible|community|reserved
    commitment_term       TEXT,                   -- e.g. "1 year", "3 years"
    availability_status   INTEGER NOT NULL DEFAULT 1,
    vram_gb               INTEGER,
    gpu_count             INTEGER NOT NULL DEFAULT 1,
    source_url            TEXT,
    confidence_score      REAL    NOT NULL DEFAULT 1.0,  -- 0–1 provider reliability
    scrape_success        INTEGER NOT NULL DEFAULT 1,
    error_message         TEXT
);

-- One row per scrape run
CREATE TABLE scrape_runs (
    id               TEXT PRIMARY KEY,  -- UUID hex
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    total_offers     INTEGER DEFAULT 0,
    providers_ok     TEXT,              -- comma-separated
    providers_failed TEXT
);
```

---

## Opportunity score formula

```
score = 0.30 × discount_score       # how far below market median (cap 50%)
      + 0.25 × spread_score         # total market spread (cap 100%)
      + 0.15 × depth_score          # number of live offers (cap 10)
      + 0.15 × confidence_score     # provider data reliability (0–1)
      + 0.10 × contract_score       # on-demand=1.0, reserved=0.8, spot=0.45
      + 0.05 × freshness_score      # <1h=1.0, <6h=0.8, <24h=0.5, older=0.1
```

Score ≥ 0.7 → strong opportunity (green).
Score 0.4–0.7 → moderate (yellow).
Score < 0.4 → weak or stale (red).

---

## Availability tiers

Every offer stores an `availability` tier:

| Tier | Meaning | `interruptible` | `contract_type` |
|------|---------|-----------------|-----------------|
| `on_demand` | Pay-as-you-go, no interruption risk | `False` | `on-demand` |
| `spot` | Provider may reclaim the instance (AWS/Azure/GCP) | `True` | `spot` |
| `interruptible` | Marketplace auction (Vast.ai min_bid listings) | `True` | `spot` |
| `community` | Community-hosted hardware (RunPod Community Cloud) | `True` | `on-demand` |
| `reserved` | Committed-use / reserved instances (1y or 3y) | `False` | `reserved` |

The `interruptible` field is auto-computed from the tier and lets analytics filter without string comparisons.

### Tier-aware analytics

`get_market_stats()` and `find_opportunities()` default to **on-demand only** so spot prices never pollute the market spread metric:

```python
analytics = PriceAnalytics(db)

# On-demand market (default)
stats = analytics.get_market_stats(gpu_filter="H100")

# All tiers combined
stats_all = analytics.get_market_stats(gpu_filter="H100", availability_filter="all")

# Spot discount view — compare best on-demand vs best spot per GPU
discounts = analytics.get_spot_discount(gpu_filter="H100")
# → DataFrame with: gpu_model, on_demand_min, spot_min, discount_abs, discount_pct
```

Example spot discount output:

```
gpu_model      on_demand_min  spot_min  discount_abs  discount_pct
H100 SXM              4.06      1.30          2.76         67.98
A100 80GB             3.95      1.19          2.76         69.87
L4                    0.71      0.21          0.50         70.42
```

---

## Example arbitrage workflow

```bash
# Step 1 — seed the database with a fresh snapshot
python3 -m gpu_scraper.cli fetch --save-db

# Step 2 — find H100 deals below market median in Europe
python3 -m gpu_scraper.cli opportunities \
    --gpu H100 --region Europe --min-score 0.5

# Step 3 — start continuous monitoring
python3 -m gpu_scraper.cli watch \
    --interval 15 --gpu H100 --save-db &

# Step 4 — open dashboard for full analysis
streamlit run dashboard/app.py
```

---

## Provider data sources

| Provider | Auth | Method | Tiers | Confidence |
|----------|------|--------|-------|-----------|
| RunPod | None | GraphQL API | on_demand · spot · community | 95% |
| Lambda Labs | `LAMBDA_API_KEY` | REST API | on_demand | 95% |
| Vast.ai | None | Public bundles API | on_demand · interruptible | 78% |
| CoreWeave | None | HTML scrape + static fallback | on_demand | 90% |
| Paperspace | None | HTML scrape + static fallback | on_demand | 82% |
| TensorDock | None | REST API | on_demand | 85% |
| AWS | None | Spot JSONP + OD pricing JSON | on_demand · spot | 95% |
| GCP | None | HTML scrape + static fallback | on_demand · spot · reserved | 95% |
| Azure | None | Retail Prices REST API | on_demand · spot · reserved | 95% |
| OCI | None | Products REST API | on_demand | 88% |
| DataCrunch | None | Public REST API | on_demand · spot | 88% |
| Crusoe | None | HTML scrape + static fallback | on_demand | 85% |
| Hyperstack | None | Static table (API requires auth) | on_demand | 80% |
| Nebius | None | Static table (API requires auth) | on_demand | 82% |

---

## Limitations & next steps

**Current limitations**
- AWS on-demand pricing fetches the full `us-east-1/index.json` (~200 MB); for multi-region coverage use `boto3` with the Pricing API.
- GCP GPU pricing is parsed from the public calculator JSON, which may lag official announcements by hours.
- CoreWeave and Paperspace fall back to static price tables when page structure changes; prices should be verified manually if the live scrape fails.
- Vast.ai public endpoint returns ~64 top-scored offers; authenticated API gives full marketplace.

**Planned next steps**
- [ ] Slack / email alerts when opportunity score crosses threshold
- [ ] Multi-region AWS / GCP coverage with async streaming JSON parsing
- [ ] Authenticated Vast.ai API integration for full marketplace depth
- [ ] Confidence score auto-calibration based on historical accuracy
- [ ] Atlas cost-optimisation API endpoint (REST) for programmatic routing
- [ ] Kubernetes CronJob manifest for automated scraping
