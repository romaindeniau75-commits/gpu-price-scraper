"""CLI entry point — Click + Rich.

Commands
--------
  fetch          Fetch current prices from providers (optionally save to DB)
  watch          Continuous re-scrape loop with price-change logging
  init-db        Initialise (or upgrade) the SQLite database
  opportunities  Show ranked arbitrage opportunities from stored data
  list-providers List all supported providers
"""
from __future__ import annotations

import asyncio
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
from rich.console import Console
from rich.live import Live
from rich.table import Table as RTable
from rich.text import Text
from rich import box as rbox

from .display import build_table, console, print_price_changes
from .exporter import export_csv, export_json
from .models import GPUOffer
from .providers import ALL_PROVIDERS
from .providers.base import BaseProvider

# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_DB = "./data/gpu_prices.db"

# ──────────────────────────────────────────────────────────────────────────────
# Core async runner (shared by all commands)
# ──────────────────────────────────────────────────────────────────────────────

async def run_all_providers(
    selected: list[type[BaseProvider]],
    verbose: bool = False,
) -> tuple[list[GPUOffer], dict[str, str]]:
    """Run selected providers concurrently.

    Returns
    -------
    (offers, errors)
        offers — all successful GPUOffer results
        errors — {provider_name: error_message} for failed providers
    """
    instances = [cls() for cls in selected]

    if verbose:
        logging.basicConfig(
            level=logging.INFO,
            format="%(levelname)s [%(name)s] %(message)s",
            stream=sys.stderr,
        )

    tasks = [p.fetch() for p in instances]
    results = await asyncio.gather(*tasks, return_exceptions=True)

    offers: list[GPUOffer] = []
    errors: dict[str, str] = {}

    for instance, result in zip(instances, results):
        if isinstance(result, Exception):
            msg = f"{type(result).__name__}: {result}"
            errors[instance.name] = msg
            console.print(f"[red]✗ {instance.name}:[/red] {msg}", highlight=False)
            print(f"PROVIDER_ERROR [{instance.name}]: {msg}", file=sys.stderr)
        else:
            if not result:
                # Providers swallow exceptions internally; an empty list here
                # means the provider's _scrape() failed silently. Log to stderr
                # so CI logs capture it without --verbose.
                print(f"PROVIDER_EMPTY [{instance.name}]: returned 0 offers", file=sys.stderr)
            offers.extend(result)

    if not offers:
        print(
            f"FETCH_SUMMARY: 0 offers total from {len(instances)} providers "
            f"(errors: {list(errors.keys()) or 'none visible — check PROVIDER_EMPTY lines above'})",
            file=sys.stderr,
        )

    for p in instances:
        await p.close()

    return offers, errors


def _offers_to_price_map(offers: list[GPUOffer]) -> dict[str, float]:
    return {o.price_key(): o.price_per_hour for o in offers}


def _filter_offers(
    offers: list[GPUOffer],
    gpu: Optional[str],
    contract: str,
    no_filter: bool,
) -> list[GPUOffer]:
    if not no_filter and not gpu:
        offers = [o for o in offers if any(f in o.gpu_model for f in ("H100", "A100"))]
    if gpu:
        offers = [o for o in offers if gpu.upper() in o.gpu_model.upper()]
    if contract != "all":
        offers = [o for o in offers if o.contract_type == contract]
    return offers


def _resolve_providers(names: tuple[str, ...]) -> list[type[BaseProvider]]:
    if not names:
        return ALL_PROVIDERS
    selected = []
    for n in names:
        match = [cls for cls in ALL_PROVIDERS if n.lower() in cls.name.lower()]
        if not match:
            console.print(f"[yellow]Warning:[/yellow] unknown provider '{n}' — skipping.")
        selected.extend(match)
    return list(dict.fromkeys(selected))


# ──────────────────────────────────────────────────────────────────────────────
# CLI group
# ──────────────────────────────────────────────────────────────────────────────

@click.group(invoke_without_command=True, context_settings={"help_option_names": ["-h", "--help"]})
@click.option(
    "--db-path",
    default=_DEFAULT_DB,
    show_default=True,
    envvar="GPU_SCRAPER_DB_PATH",
    help="SQLite database path.",
)
@click.pass_context
def main(ctx: click.Context, db_path: str) -> None:
    """🎯 Atlas GPU price scraper — H100 & A100 across 10+ cloud providers."""
    ctx.ensure_object(dict)
    ctx.obj["db_path"] = db_path
    if ctx.invoked_subcommand is None:
        ctx.invoke(fetch)


# ──────────────────────────────────────────────────────────────────────────────
# init-db
# ──────────────────────────────────────────────────────────────────────────────

@main.command("init-db")
@click.pass_context
def init_db(ctx: click.Context) -> None:
    """Initialise (or upgrade) the SQLite database schema."""
    from .storage import PriceDatabase

    db_path: str = ctx.obj["db_path"]
    db = PriceDatabase(db_path)
    db.init()
    console.print(f"[green]✓ Database ready:[/green] {Path(db_path).resolve()}")
    console.print(f"  Existing observations: {db.row_count():,}")


# ──────────────────────────────────────────────────────────────────────────────
# fetch
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--gpu", "-g",      default=None,   help="Filter by GPU family, e.g. H100 or A100.")
@click.option("--provider", "-p", multiple=True,  help="Limit to specific provider(s). Repeatable.")
@click.option("--contract", "-c",
              type=click.Choice(["on-demand", "spot", "reserved", "all"]),
              default="all", show_default=True, help="Contract type filter.")
@click.option("--export", "-e",   is_flag=True,   help="Export results to CSV + JSON.")
@click.option("--output-dir",     default="./exports", show_default=True,
              help="Directory for exported files.")
@click.option("--save-db",        is_flag=True,   help="Persist observations to the SQLite database.")
@click.option("--verbose", "-v",  is_flag=True,   help="Show per-provider HTTP logs.")
@click.option("--no-filter",      is_flag=True,   help="Show all GPU types, not just H100/A100.")
@click.pass_context
def fetch(
    ctx: click.Context,
    gpu: Optional[str],
    provider: tuple[str, ...],
    contract: str,
    export: bool,
    output_dir: str,
    save_db: bool,
    verbose: bool,
    no_filter: bool,
) -> None:
    """Fetch GPU prices from providers and display a Rich table."""
    db_path: str = (ctx.obj or {}).get("db_path", _DEFAULT_DB)
    selected = _resolve_providers(provider)
    if not selected:
        raise click.UsageError("No matching providers found.")

    offers, errors = asyncio.run(run_all_providers(selected, verbose))
    filtered = _filter_offers(offers, gpu, contract, no_filter)

    if not filtered:
        console.print("[yellow]No offers found matching your filters.[/yellow]")
    else:
        console.print()
        console.print(build_table(filtered, filter_gpu=gpu))
        console.print(
            f"\n[dim]Total: {len(filtered)} offers from "
            f"{len(set(o.provider for o in filtered))} providers[/dim]"
        )

    if export and filtered:
        out = Path(output_dir)
        console.print(f"[green]✓ Exported:[/green] {export_json(filtered, out)}")
        console.print(f"[green]✓ Exported:[/green] {export_csv(filtered, out)}")

    if save_db:
        _save_to_db(db_path, offers, errors)


def _save_to_db(
    db_path: str,
    offers: list[GPUOffer],
    errors: dict[str, str],
    history_db_path: Optional[str] = None,
) -> None:
    from .storage import HistoryDatabase, PriceDatabase

    db = PriceDatabase(db_path)
    db.init()
    run_id = db.start_run()
    count = db.save_offers(offers, run_id, provider_errors=errors)
    providers_ok = list({o.provider for o in offers} - set(errors))
    db.finish_run(run_id, count, providers_ok, list(errors.keys()))
    console.print(
        f"[green]✓ Saved {count:,} observations[/green] → {Path(db_path).resolve()}"
        f"  [dim](run {run_id[:8]})[/dim]"
    )

    # Persist daily snapshot for time-series / GitHub Pages dashboard
    hist_path = history_db_path or str(Path(db_path).parent / "pricing_history.db")
    hdb = HistoryDatabase(hist_path)
    hdb.init()
    snap_count = hdb.save_snapshot(offers)
    console.print(
        f"[green]✓ Snapshot:[/green] {snap_count:,} rows → {Path(hist_path).resolve()}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# opportunities
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--gpu", "-g",     default=None,  help="Filter by GPU family, e.g. H100.")
@click.option("--region", "-r",  default=None,  help="Region filter, e.g. Europe, US.")
@click.option("--hours",         default=24, show_default=True,
              help="Look-back window in hours.")
@click.option("--top-n",         default=20, show_default=True,
              help="Maximum opportunities to show.")
@click.option("--min-score",     default=0.0, show_default=True,
              help="Minimum opportunity score (0–1).")
@click.pass_context
def opportunities(
    ctx: click.Context,
    gpu: Optional[str],
    region: Optional[str],
    hours: int,
    top_n: int,
    min_score: float,
) -> None:
    """Show ranked arbitrage opportunities from stored price data.

    Run 'fetch --save-db' first to populate the database.
    """
    from .analytics import PriceAnalytics
    from .storage import PriceDatabase

    db_path: str = ctx.obj["db_path"]

    if not Path(db_path).exists():
        console.print(
            f"[red]Database not found:[/red] {db_path}\n"
            "Run [bold]python3 -m gpu_scraper.cli fetch --save-db[/bold] first."
        )
        raise SystemExit(1)

    analytics = PriceAnalytics(PriceDatabase(db_path))
    opps = analytics.find_opportunities(
        gpu_filter=gpu,
        region_filter=region,
        hours=hours,
        top_n=top_n,
    )

    if opps.empty:
        console.print(
            "[yellow]No opportunities found.[/yellow]\n"
            "Need ≥ 2 providers with offers for the same GPU model. "
            f"Check the last {hours}h of data."
        )
        return

    if min_score > 0:
        opps = opps[opps["opportunity_score"] >= min_score]
        if opps.empty:
            console.print(f"[yellow]No opportunities with score ≥ {min_score}[/yellow]")
            return

    # Build Rich table
    t = RTable(
        title=f"⚡ Arbitrage Opportunities — last {hours}h",
        box=rbox.ROUNDED,
        show_lines=False,
        highlight=True,
        caption=f"[dim]{len(opps)} opportunities · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}[/dim]",
    )
    t.add_column("Score",    justify="center", style="bold")
    t.add_column("GPU",      style="cyan bold")
    t.add_column("Provider", style="bold")
    t.add_column("Buy $/hr", justify="right", style="green bold")
    t.add_column("Median",   justify="right", style="dim")
    t.add_column("Discount", justify="right", style="yellow")
    t.add_column("Spread",   justify="right")
    t.add_column("Contract", justify="center")
    t.add_column("Region",   style="dim")
    t.add_column("Save/mo",  justify="right", style="magenta")

    for _, row in opps.iterrows():
        score = float(row["opportunity_score"])
        if score >= 0.7:
            score_str = f"[green]{score:.3f}[/green]"
        elif score >= 0.4:
            score_str = f"[yellow]{score:.3f}[/yellow]"
        else:
            score_str = f"[red]{score:.3f}[/red]"

        t.add_row(
            score_str,
            str(row["gpu_model"]),
            str(row["buy_provider"]),
            f"${row['buy_price']:.4f}",
            f"${row['market_median']:.4f}",
            f"{row['discount_pct']:.1f}%",
            f"{row['spread_pct']:.1f}%",
            str(row["contract_type"]),
            str(row["region"])[:28],
            f"${row['monthly_saving_vs_median']:.0f}",
        )

    console.print()
    console.print(t)

    # Market stats summary
    console.print("\n[bold]Market stats:[/bold]")
    db = PriceDatabase(db_path)
    stats = PriceAnalytics(db).get_market_stats(gpu_filter=gpu, region_filter=region, hours=hours)
    if not stats.empty:
        s = RTable(box=rbox.SIMPLE, show_header=True)
        s.add_column("GPU")
        s.add_column("Region")
        s.add_column("Contract")
        s.add_column("Min",    justify="right")
        s.add_column("Median", justify="right")
        s.add_column("Max",    justify="right")
        s.add_column("Spread", justify="right")
        s.add_column("Offers", justify="right")
        for _, row in stats.iterrows():
            s.add_row(
                str(row["gpu_model"]),
                str(row["region_group"]),
                str(row["contract_type"]),
                f"${row['min_price']:.4f}",
                f"${row['median_price']:.4f}",
                f"${row['max_price']:.4f}",
                f"{row['spread_pct']:.1f}%",
                str(row["offer_count"]),
            )
        console.print(s)


# ──────────────────────────────────────────────────────────────────────────────
# watch
# ──────────────────────────────────────────────────────────────────────────────

@main.command()
@click.option("--interval", "-i", default=15, show_default=True,
              help="Refresh interval in minutes.")
@click.option("--gpu", "-g",      default=None,   help="Filter by GPU family.")
@click.option("--provider", "-p", multiple=True,  help="Limit to specific provider(s).")
@click.option("--export", "-e",   is_flag=True,   help="Export each snapshot to disk.")
@click.option("--output-dir",     default="./exports", show_default=True)
@click.option("--save-db",        is_flag=True,   help="Persist every scrape to the SQLite database.")
@click.option("--verbose", "-v",  is_flag=True)
@click.option("--no-filter",      is_flag=True,   help="Track all GPU types.")
@click.pass_context
def watch(
    ctx: click.Context,
    interval: int,
    gpu: Optional[str],
    provider: tuple[str, ...],
    export: bool,
    output_dir: str,
    save_db: bool,
    verbose: bool,
    no_filter: bool,
) -> None:
    """Re-scrape every N minutes, log price changes, and optionally save to DB."""
    db_path: str = ctx.obj["db_path"]
    selected = _resolve_providers(provider)
    if not selected:
        raise click.UsageError("No matching providers found.")

    asyncio.run(
        _watch_loop(
            selected=selected,
            interval_minutes=interval,
            gpu=gpu,
            do_export=export,
            output_dir=output_dir,
            save_db=save_db,
            db_path=db_path,
            verbose=verbose,
            no_filter=no_filter,
        )
    )


async def _watch_loop(
    selected: list[type[BaseProvider]],
    interval_minutes: int,
    gpu: Optional[str],
    do_export: bool,
    output_dir: str,
    save_db: bool,
    db_path: str,
    verbose: bool,
    no_filter: bool,
) -> None:
    save_str = f" · [green]saving to DB[/green]" if save_db else ""
    console.print(
        f"[bold green]Watch mode[/bold green] — every [cyan]{interval_minutes}m[/cyan]"
        f"{save_str}. Ctrl+C to stop.\n"
    )

    if save_db:
        from .storage import PriceDatabase
        db = PriceDatabase(db_path)
        db.init()
    else:
        db = None

    prev_prices: dict[str, float] = {}
    iteration = 0

    try:
        while True:
            iteration += 1
            ts = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")

            with Live(
                Text(f"⟳ Scraping … ({ts})", style="dim"),
                console=console,
                transient=True,
            ):
                all_offers, errors = await run_all_providers(selected, verbose)

            filtered = _filter_offers(all_offers, gpu, "all", no_filter)
            curr_prices = _offers_to_price_map(filtered)

            if iteration == 1:
                console.print(build_table(filtered, title=f"GPU Prices — {ts}", filter_gpu=gpu))
            else:
                print_price_changes(prev_prices, curr_prices, ts)

            if do_export and filtered:
                out = Path(output_dir)
                export_json(filtered, out)
                export_csv(filtered, out)

            if save_db and db is not None:
                run_id = db.start_run()
                count = db.save_offers(all_offers, run_id, provider_errors=errors)
                providers_ok = list({o.provider for o in all_offers} - set(errors))
                db.finish_run(run_id, count, providers_ok, list(errors.keys()))
                console.print(
                    f"[dim]  DB: +{count} observations · run {run_id[:8]}[/dim]"
                )

            prev_prices = curr_prices
            console.print(
                f"[dim]Next refresh in {interval_minutes}m "
                f"({len(filtered)} offers tracked · {len(errors)} provider errors). "
                "Ctrl+C to stop.[/dim]"
            )
            await asyncio.sleep(interval_minutes * 60)

    except (KeyboardInterrupt, asyncio.CancelledError):
        console.print("\n[yellow]Watch mode stopped.[/yellow]")


# ──────────────────────────────────────────────────────────────────────────────
# list-providers
# ──────────────────────────────────────────────────────────────────────────────

@main.command("list-providers")
def list_providers() -> None:
    """List all available providers."""
    t = RTable(box=rbox.SIMPLE, show_header=True)
    t.add_column("Provider",       style="cyan bold")
    t.add_column("Auth required?")
    t.add_column("Method")
    t.add_column("Confidence")
    rows = [
        ("RunPod",       "No",                  "GraphQL API",                  "95%"),
        ("Lambda Labs",  "Yes (LAMBDA_API_KEY)", "REST API",                     "95%"),
        ("Vast.ai",      "No",                  "REST API",                     "78%"),
        ("CoreWeave",    "No",                  "HTML scrape + static fallback", "90%"),
        ("Paperspace",   "No",                  "HTML scrape + static fallback", "82%"),
        ("TensorDock",   "No",                  "REST API",                     "85%"),
        ("AWS",          "No",                  "Spot JSONP + OD pricing JSON",  "95%"),
        ("GCP",          "No",                  "Pricing calculator JSON",       "95%"),
        ("Azure",        "No",                  "Retail Prices REST API",        "95%"),
        ("OCI",          "No",                  "REST API",                     "88%"),
    ]
    for row in rows:
        t.add_row(*row)
    console.print(t)
