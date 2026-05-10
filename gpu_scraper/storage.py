"""SQLite persistence layer for GPU price observations."""
from __future__ import annotations

import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional, Sequence

from .models import GPUOffer

# ---------------------------------------------------------------------------
# Provider confidence — how reliable is each data source?
# ---------------------------------------------------------------------------
PROVIDER_CONFIDENCE: dict[str, float] = {
    "RunPod":           0.95,
    "RunPod Community": 0.80,
    "Lambda Labs":      0.95,
    "Vast.ai":          0.78,
    "TensorDock":       0.85,
    "CoreWeave":        0.90,
    "Paperspace":       0.82,
    "AWS":              0.95,
    "GCP":              0.95,
    "Azure":            0.95,
    "OCI":              0.88,
}
_DEFAULT_CONFIDENCE = 0.70

# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------
_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS gpu_price_observations (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp             TEXT    NOT NULL,
    scrape_run_id         TEXT    NOT NULL,
    provider              TEXT    NOT NULL,
    gpu_model_raw         TEXT,
    gpu_model_normalized  TEXT    NOT NULL,
    region                TEXT,
    country               TEXT,
    price_per_hour_usd    REAL    NOT NULL,
    currency              TEXT    NOT NULL DEFAULT 'USD',
    contract_type         TEXT,
    availability_status   INTEGER NOT NULL DEFAULT 1,
    vram_gb               INTEGER,
    gpu_count             INTEGER NOT NULL DEFAULT 1,
    source_url            TEXT,
    confidence_score      REAL    NOT NULL DEFAULT 1.0,
    scrape_success        INTEGER NOT NULL DEFAULT 1,
    error_message         TEXT
);

CREATE INDEX IF NOT EXISTS idx_obs_timestamp ON gpu_price_observations(timestamp);
CREATE INDEX IF NOT EXISTS idx_obs_gpu       ON gpu_price_observations(gpu_model_normalized);
CREATE INDEX IF NOT EXISTS idx_obs_provider  ON gpu_price_observations(provider);
CREATE INDEX IF NOT EXISTS idx_obs_run       ON gpu_price_observations(scrape_run_id);
CREATE INDEX IF NOT EXISTS idx_obs_contract  ON gpu_price_observations(contract_type);

CREATE TABLE IF NOT EXISTS scrape_runs (
    id               TEXT PRIMARY KEY,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    total_offers     INTEGER DEFAULT 0,
    providers_ok     TEXT,
    providers_failed TEXT
);
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_country(region: str) -> str:
    """Best-effort country extraction from a region string."""
    r = region.lower()
    # Cloud provider prefixes
    for prefix, code in [
        ("us-", "US"), ("eastus", "US"), ("westus", "US"), ("centralus", "US"),
        ("eu-", "EU"), ("europe", "EU"), ("westeurope", "EU"), ("northeurope", "EU"),
        ("ap-", "APAC"), ("asia", "APAC"), ("australia", "AU"),
        ("ca-", "CA"), ("canada", "CA"),
        ("sa-", "BR"), ("brazil", "BR"),
        ("me-", "ME"),
    ]:
        if r.startswith(prefix) or prefix in r:
            return code

    # Vast.ai / TensorDock: "Czech Republic / Prague", "United States, US"
    if "/" in region:
        return region.split("/")[0].strip()
    if "," in region:
        parts = region.split(",")
        last = parts[-1].strip()
        return last if len(last) <= 3 else parts[0].strip()

    return region or "Unknown"


# ---------------------------------------------------------------------------
# Database class
# ---------------------------------------------------------------------------

class PriceDatabase:
    def __init__(self, db_path: str | Path = "./data/gpu_prices.db") -> None:
        self.path = Path(db_path)

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(str(self.path), timeout=30)
        con.row_factory = sqlite3.Row
        try:
            yield con
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    # ------------------------------------------------------------------ init

    def init(self) -> None:
        """Create tables and indexes if they do not exist."""
        with self._conn() as con:
            con.executescript(_SCHEMA)

    # --------------------------------------------------------------- scrape run

    def start_run(self) -> str:
        """Register a new scrape run; return its ID."""
        run_id = uuid.uuid4().hex
        ts = datetime.now(timezone.utc).isoformat()
        with self._conn() as con:
            con.execute(
                "INSERT INTO scrape_runs (id, started_at) VALUES (?, ?)",
                (run_id, ts),
            )
        return run_id

    def finish_run(
        self,
        run_id: str,
        total_offers: int,
        providers_ok: Sequence[str],
        providers_failed: Sequence[str],
    ) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        with self._conn() as con:
            con.execute(
                """UPDATE scrape_runs
                   SET finished_at=?, total_offers=?, providers_ok=?, providers_failed=?
                   WHERE id=?""",
                (
                    ts,
                    total_offers,
                    ",".join(providers_ok),
                    ",".join(providers_failed),
                    run_id,
                ),
            )

    # --------------------------------------------------------------- save offers

    def save_offers(
        self,
        offers: Sequence[GPUOffer],
        run_id: str,
        provider_errors: Optional[dict[str, str]] = None,
    ) -> int:
        """Insert observations; return the count inserted."""
        ts_now = datetime.now(timezone.utc).isoformat()
        rows = []
        for o in offers:
            confidence = PROVIDER_CONFIDENCE.get(o.provider, _DEFAULT_CONFIDENCE)
            country = _extract_country(o.region or "")
            rows.append((
                (o.timestamp or datetime.now(timezone.utc)).isoformat(),
                run_id,
                o.provider,
                o.raw_gpu_name,
                o.gpu_model,
                o.region or "",
                country,
                o.price_per_hour,
                "USD",
                o.contract_type,
                1 if o.availability else 0,
                o.vram_gb,
                o.gpu_count,
                None,          # source_url — not yet captured per-offer
                confidence,
                1,             # scrape_success
                None,          # error_message
            ))

        # Also insert failure rows for providers that errored
        for provider, err_msg in (provider_errors or {}).items():
            rows.append((
                ts_now,
                run_id,
                provider,
                None, "UNKNOWN", "", "", 0.0, "USD",
                None, 0, None, 1, None,
                PROVIDER_CONFIDENCE.get(provider, _DEFAULT_CONFIDENCE),
                0,             # scrape_success = False
                err_msg[:500],
            ))

        with self._conn() as con:
            con.executemany(
                """INSERT INTO gpu_price_observations
                   (timestamp, scrape_run_id, provider, gpu_model_raw, gpu_model_normalized,
                    region, country, price_per_hour_usd, currency, contract_type,
                    availability_status, vram_gb, gpu_count, source_url,
                    confidence_score, scrape_success, error_message)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )
        return len([r for r in rows if r[15] == 1])  # count successes

    # --------------------------------------------------------------- queries

    def get_latest_prices(
        self,
        gpu_filter: Optional[str] = None,
        region_filter: Optional[str] = None,
        contract_filter: Optional[str] = None,
        hours: int = 24,
    ) -> list[sqlite3.Row]:
        """Most recent successful observation per (provider, gpu, region, contract)."""
        wheres = [
            "scrape_success = 1",
            "price_per_hour_usd > 0",
            f"timestamp >= datetime('now', '-{hours} hours')",
        ]
        params: list = []
        if gpu_filter:
            wheres.append("gpu_model_normalized LIKE ?")
            params.append(f"%{gpu_filter}%")
        if region_filter:
            wheres.append("(region LIKE ? OR country LIKE ?)")
            params += [f"%{region_filter}%", f"%{region_filter}%"]
        if contract_filter and contract_filter != "all":
            wheres.append("contract_type = ?")
            params.append(contract_filter)

        where_clause = " AND ".join(wheres)
        sql = f"""
            SELECT *
            FROM gpu_price_observations
            WHERE {where_clause}
            ORDER BY timestamp DESC
        """
        with self._conn() as con:
            return con.execute(sql, params).fetchall()

    def get_historical_prices(
        self,
        gpu_filter: Optional[str] = None,
        hours: int = 168,
    ) -> list[sqlite3.Row]:
        """All successful observations over the requested window."""
        wheres = [
            "scrape_success = 1",
            "price_per_hour_usd > 0",
            f"timestamp >= datetime('now', '-{hours} hours')",
        ]
        params: list = []
        if gpu_filter:
            wheres.append("gpu_model_normalized LIKE ?")
            params.append(f"%{gpu_filter}%")
        where_clause = " AND ".join(wheres)
        sql = f"""
            SELECT *
            FROM gpu_price_observations
            WHERE {where_clause}
            ORDER BY timestamp ASC
        """
        with self._conn() as con:
            return con.execute(sql, params).fetchall()

    def get_scrape_runs(self, limit: int = 20) -> list[sqlite3.Row]:
        with self._conn() as con:
            return con.execute(
                "SELECT * FROM scrape_runs ORDER BY started_at DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def row_count(self) -> int:
        with self._conn() as con:
            return con.execute(
                "SELECT COUNT(*) FROM gpu_price_observations WHERE scrape_success=1"
            ).fetchone()[0]
