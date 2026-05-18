"""Rich table rendering for GPU offers."""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence

from rich.console import Console
from rich.table import Table
from rich import box

from .models import GPUOffer

console = Console()

_CONTRACT_STYLE = {
    "on-demand": "green",
    "spot":      "yellow",
    "reserved":  "cyan",
}

_AVAIL_STYLE = {True: "[green]●[/green]", False: "[red]○[/red]"}


def build_table(
    offers: Sequence[GPUOffer],
    title: str = "GPU Cloud Prices",
    filter_gpu: str | None = None,
) -> Table:
    if filter_gpu:
        offers = [o for o in offers if filter_gpu.upper() in o.gpu_model.upper()]

    # Sort: GPU model → price
    sorted_offers = sorted(offers, key=lambda o: (o.gpu_model, o.price_per_hour))

    table = Table(
        title=title,
        box=box.ROUNDED,
        show_lines=False,
        highlight=True,
        caption=f"[dim]{len(sorted_offers)} offers · {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}[/dim]",
    )

    table.add_column("Provider",      style="bold",          no_wrap=True)
    table.add_column("GPU",           style="cyan bold",     no_wrap=True)
    table.add_column("VRAM",          justify="right",       style="blue")
    table.add_column("$/hr",          justify="right",       style="yellow bold")
    table.add_column("GPUs",          justify="center")
    table.add_column("Contract",      justify="center")
    table.add_column("Region",        style="dim",           no_wrap=True)
    table.add_column("Avail",         justify="center")

    prev_gpu = ""
    for o in sorted_offers:
        if o.gpu_model != prev_gpu and prev_gpu:
            table.add_section()
        prev_gpu = o.gpu_model

        contract_style = _CONTRACT_STYLE.get(o.contract_type, "white")
        avail_marker = _AVAIL_STYLE.get(o.available, "[dim]?[/dim]")
        price_str = f"${o.price_per_hour:.4f}"

        table.add_row(
            o.provider,
            o.gpu_model,
            f"{o.vram_gb} GB" if o.vram_gb else "?",
            price_str,
            str(o.gpu_count),
            f"[{contract_style}]{o.contract_type}[/{contract_style}]",
            o.region[:30],
            avail_marker,
        )

    return table


def print_table(
    offers: Sequence[GPUOffer],
    title: str = "GPU Cloud Prices",
    filter_gpu: str | None = None,
) -> None:
    console.print(build_table(offers, title=title, filter_gpu=filter_gpu))


def print_price_changes(
    prev: dict[str, float],
    curr: dict[str, float],
    timestamp: str,
) -> None:
    changes: list[tuple[str, float, float]] = []
    for key, new_price in curr.items():
        old_price = prev.get(key)
        if old_price is not None and abs(new_price - old_price) > 1e-6:
            changes.append((key, old_price, new_price))

    if not changes:
        console.print(f"[dim]{timestamp}[/dim] No price changes detected.")
        return

    console.print(f"\n[bold yellow]{timestamp}[/bold yellow] — {len(changes)} price change(s):")
    for key, old, new in sorted(changes, key=lambda x: abs(x[2] - x[1]), reverse=True):
        direction = "[green]▼[/green]" if new < old else "[red]▲[/red]"
        pct = (new - old) / old * 100
        console.print(
            f"  {direction} [cyan]{key}[/cyan] "
            f"${old:.4f} → ${new:.4f} ([bold]{pct:+.1f}%[/bold])"
        )
