"""Atlas GPU Price Dashboard — Streamlit app.

Launch:
    streamlit run dashboard/app.py
    streamlit run dashboard/app.py -- --db-path ./data/gpu_prices.db
"""
from __future__ import annotations

import sys
from pathlib import Path

# Allow running from repo root without installing the package
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import os
import sqlite3
from datetime import datetime, timezone

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from gpu_scraper.analytics import PriceAnalytics, compute_opportunity_score
from gpu_scraper.storage import PriceDatabase

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

st.set_page_config(
    page_title="Atlas GPU Pricing",
    page_icon="🎯",
    layout="wide",
    initial_sidebar_state="expanded",
)

_DEFAULT_DB = str(Path(__file__).parent.parent / "data" / "gpu_prices.db")

# ─────────────────────────────────────────────────────────────────────────────
# Sidebar — navigation + global filters
# ─────────────────────────────────────────────────────────────────────────────

with st.sidebar:
    st.title("🎯 Atlas GPU Pricing")
    st.caption("Real-time arbitrage intelligence")
    st.divider()

    page = st.radio(
        "Navigation",
        [
            "📊 Market Overview",
            "⚡ Arbitrage Opportunities",
            "🏢 Provider Comparison",
            "📈 Historical Trends",
            "🚀 Atlas Routing",
        ],
        label_visibility="collapsed",
    )

    st.divider()
    st.subheader("Filters")

    db_path = st.text_input("Database path", value=os.getenv("GPU_SCRAPER_DB_PATH", _DEFAULT_DB))
    gpu_filter = st.selectbox("GPU family", ["All", "H100", "A100", "L40S", "A10", "V100"])
    contract_filter = st.selectbox("Contract type", ["all", "on-demand", "spot", "reserved"])
    time_window = st.select_slider(
        "Time window (hours)",
        options=[1, 3, 6, 12, 24, 48, 72, 168],
        value=24,
    )
    region_filter = st.text_input("Region filter (free text)", placeholder="e.g. Europe, US, APAC")

    st.divider()
    if st.button("🔄 Refresh data", use_container_width=True):
        st.cache_data.clear()

    st.caption(f"Last UI refresh: {datetime.now().strftime('%H:%M:%S')}")

# ─────────────────────────────────────────────────────────────────────────────
# Data helpers (cached)
# ─────────────────────────────────────────────────────────────────────────────

@st.cache_resource
def get_db(path: str) -> PriceDatabase:
    db = PriceDatabase(path)
    return db


@st.cache_data(ttl=300)
def load_latest(db_path: str, gpu: str, region: str, contract: str, hours: int) -> pd.DataFrame:
    db = get_db(db_path)
    try:
        rows = db.get_latest_prices(
            gpu_filter=None if gpu == "All" else gpu,
            region_filter=region or None,
            contract_filter=None if contract == "all" else contract,
            hours=hours,
        )
        if not rows:
            return pd.DataFrame()
        return pd.DataFrame([dict(r) for r in rows])
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_history(db_path: str, gpu: str, hours: int) -> pd.DataFrame:
    db = get_db(db_path)
    try:
        analytics = PriceAnalytics(db)
        return analytics.get_price_history(
            gpu_filter=None if gpu == "All" else gpu,
            hours=hours,
        )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_opportunities(db_path: str, gpu: str, region: str, hours: int) -> pd.DataFrame:
    db = get_db(db_path)
    try:
        analytics = PriceAnalytics(db)
        return analytics.find_opportunities(
            gpu_filter=None if gpu == "All" else gpu,
            region_filter=region or None,
            hours=hours,
            top_n=40,
        )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_market_stats(db_path: str, gpu: str, region: str, hours: int) -> pd.DataFrame:
    db = get_db(db_path)
    try:
        analytics = PriceAnalytics(db)
        return analytics.get_market_stats(
            gpu_filter=None if gpu == "All" else gpu,
            region_filter=region or None,
            hours=hours,
        )
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=300)
def load_provider_summary(db_path: str, gpu: str, hours: int) -> pd.DataFrame:
    db = get_db(db_path)
    try:
        analytics = PriceAnalytics(db)
        return analytics.get_provider_summary(
            gpu_filter=None if gpu == "All" else gpu,
            hours=hours,
        )
    except Exception:
        return pd.DataFrame()


def _db_exists(path: str) -> bool:
    return Path(path).exists() and Path(path).stat().st_size > 0


def _empty_state(msg: str = "No data yet.") -> None:
    st.info(
        f"**{msg}**\n\n"
        "Run `python3 -m gpu_scraper.cli fetch --save-db` to populate the database,\n"
        "or start `python3 -m gpu_scraper.cli watch --interval 15 --save-db` for continuous tracking.",
        icon="📭",
    )


# ─────────────────────────────────────────────────────────────────────────────
# Colour helpers
# ─────────────────────────────────────────────────────────────────────────────

_PROVIDER_PALETTE = px.colors.qualitative.Plotly
_GPU_COLOURS = {
    "H100 SXM":    "#FF4B4B",
    "H100 NVL":    "#FF8C00",
    "H100 PCIe":   "#FFA07A",
    "A100 80GB":   "#4169E1",
    "A100 80GB SXM":"#1E90FF",
    "A100 80GB PCIe":"#87CEEB",
    "A100 40GB":   "#6495ED",
    "L40S":        "#32CD32",
    "A10G":        "#90EE90",
}


def _score_colour(score: float) -> str:
    if score >= 0.7: return "🟢"
    if score >= 0.4: return "🟡"
    return "🔴"


# ─────────────────────────────────────────────────────────────────────────────
# Page 1 — Market Overview
# ─────────────────────────────────────────────────────────────────────────────

def page_market_overview() -> None:
    st.header("📊 Market Overview")

    if not _db_exists(db_path):
        _empty_state("Database not found.")
        return

    df = load_latest(db_path, gpu_filter, region_filter, contract_filter, time_window)
    if df.empty:
        _empty_state("No observations match your filters.")
        return

    # KPI row
    col1, col2, col3, col4, col5 = st.columns(5)
    col1.metric("Total offers",    len(df))
    col2.metric("Providers",       df["provider"].nunique())
    col3.metric("GPU models",      df["gpu_model_normalized"].nunique())
    col4.metric("Lowest $/hr",     f"${df['price_per_hour_usd'].min():.4f}")
    col5.metric("Highest $/hr",    f"${df['price_per_hour_usd'].max():.4f}")

    st.divider()

    # Price range by GPU model
    col_a, col_b = st.columns([3, 2])

    with col_a:
        st.subheader("Price range by GPU model")
        stats = (
            df.groupby("gpu_model_normalized")["price_per_hour_usd"]
            .agg(["min", "median", "max", "count"])
            .reset_index()
            .sort_values("min")
        )
        fig = go.Figure()
        for _, row in stats.iterrows():
            gpu = row["gpu_model_normalized"]
            colour = _GPU_COLOURS.get(gpu, "#888888")
            fig.add_trace(go.Bar(
                name=gpu, x=[gpu],
                y=[row["max"] - row["min"]],
                base=[row["min"]],
                marker_color=colour,
                hovertemplate=(
                    f"<b>{gpu}</b><br>"
                    f"Min: ${row['min']:.4f}<br>"
                    f"Median: ${row['median']:.4f}<br>"
                    f"Max: ${row['max']:.4f}<br>"
                    f"Offers: {int(row['count'])}<extra></extra>"
                ),
            ))
        fig.update_layout(
            barmode="overlay",
            showlegend=False,
            yaxis_title="$/hr",
            height=360,
            margin=dict(l=0, r=0, t=10, b=0),
        )
        st.plotly_chart(fig, use_container_width=True)

    with col_b:
        st.subheader("Offer distribution by provider")
        prov_counts = df["provider"].value_counts().reset_index()
        prov_counts.columns = ["provider", "count"]
        fig2 = px.pie(
            prov_counts, values="count", names="provider",
            hole=0.45, height=360,
            color_discrete_sequence=_PROVIDER_PALETTE,
        )
        fig2.update_traces(textinfo="label+percent")
        fig2.update_layout(margin=dict(l=0, r=0, t=10, b=0), showlegend=False)
        st.plotly_chart(fig2, use_container_width=True)

    # Detailed table
    st.subheader("Live price table")
    display = df[[
        "provider", "gpu_model_normalized", "vram_gb", "price_per_hour_usd",
        "gpu_count", "contract_type", "region", "availability_status",
        "confidence_score", "timestamp",
    ]].copy()
    display.columns = [
        "Provider", "GPU", "VRAM GB", "$/hr", "GPUs",
        "Contract", "Region", "Available", "Confidence", "Timestamp",
    ]
    display["$/hr"] = display["$/hr"].map("${:.4f}".format)
    display["Available"] = display["Available"].map({1: "✅", 0: "❌"})
    display["Confidence"] = display["Confidence"].map("{:.0%}".format)
    st.dataframe(display, use_container_width=True, hide_index=True, height=400)


# ─────────────────────────────────────────────────────────────────────────────
# Page 2 — Arbitrage Opportunities
# ─────────────────────────────────────────────────────────────────────────────

def page_arbitrage() -> None:
    st.header("⚡ Arbitrage Opportunities")
    st.caption("Offers priced below market median, ranked by opportunity score.")

    if not _db_exists(db_path):
        _empty_state("Database not found.")
        return

    opps = load_opportunities(db_path, gpu_filter, region_filter, time_window)
    if opps.empty:
        _empty_state("No arbitrage opportunities found. Need ≥ 2 providers per GPU model.")
        return

    # Top-level metrics
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Opportunities found", len(opps))
    col2.metric("Best discount", f"{opps['discount_pct'].max():.1f}%")
    col3.metric("Max spread", f"{opps['spread_pct'].max():.1f}%")
    col4.metric("Top monthly saving/GPU", f"${opps['monthly_saving_vs_median'].max():.0f}")

    st.divider()

    col_l, col_r = st.columns([3, 2])

    with col_l:
        st.subheader("Top opportunities by score")
        top = opps.head(20).copy()
        top["Score"] = top["opportunity_score"].apply(
            lambda s: f"{_score_colour(s)} {s:.3f}"
        )
        top["Buy price"] = top["buy_price"].map("${:.4f}".format)
        top["Median"]    = top["market_median"].map("${:.4f}".format)
        top["Discount"]  = top["discount_pct"].map("{:.1f}%".format)
        top["Spread"]    = top["spread_pct"].map("{:.1f}%".format)
        top["Save/mo"]   = top["monthly_saving_vs_median"].map("${:.0f}".format)

        show_cols = {
            "gpu_model":    "GPU",
            "buy_provider": "Provider",
            "Buy price":    "Buy price",
            "Median":       "Mkt median",
            "Discount":     "Discount",
            "Spread":       "Spread",
            "contract_type":"Contract",
            "region":       "Region",
            "Save/mo":      "Save/mo",
            "Score":        "Score",
        }
        st.dataframe(
            top.rename(columns=show_cols)[[v for v in show_cols.values() if v in top.rename(columns=show_cols).columns]],
            use_container_width=True, hide_index=True, height=480,
        )

    with col_r:
        st.subheader("Discount % by provider")
        top20 = opps.head(20)
        fig = px.bar(
            top20.sort_values("discount_pct", ascending=True),
            x="discount_pct", y="buy_provider",
            color="gpu_model", orientation="h",
            labels={"discount_pct": "Discount vs median (%)", "buy_provider": ""},
            height=480,
            color_discrete_sequence=_PROVIDER_PALETTE,
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0), showlegend=True,
                          legend=dict(orientation="h", y=-0.15))
        st.plotly_chart(fig, use_container_width=True)

    # Scatter: price vs opportunity score
    st.subheader("Price vs opportunity score")
    fig2 = px.scatter(
        opps,
        x="buy_price", y="opportunity_score",
        color="gpu_model", size="spread_pct",
        hover_data=["buy_provider", "contract_type", "region", "discount_pct"],
        labels={"buy_price": "$/hr", "opportunity_score": "Opportunity score"},
        height=350,
        color_discrete_sequence=_PROVIDER_PALETTE,
    )
    fig2.update_layout(margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig2, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# Page 3 — Provider Comparison
# ─────────────────────────────────────────────────────────────────────────────

def page_provider_comparison() -> None:
    st.header("🏢 Provider Comparison")

    if not _db_exists(db_path):
        _empty_state("Database not found.")
        return

    df = load_latest(db_path, gpu_filter, region_filter, contract_filter, time_window)
    summary = load_provider_summary(db_path, gpu_filter, time_window)

    if df.empty or summary.empty:
        _empty_state("No data matches your filters.")
        return

    # Provider summary cards
    st.subheader("Provider summary")
    st.dataframe(
        summary.rename(columns={
            "provider": "Provider", "offer_count": "Offers",
            "gpu_types": "GPU types", "min_price": "Min $/hr",
            "avg_price": "Avg $/hr", "max_price": "Max $/hr",
            "confidence_score": "Confidence",
            "on_demand_count": "On-demand", "spot_count": "Spot",
        }).style.format({
            "Min $/hr": "${:.4f}", "Avg $/hr": "${:.4f}", "Max $/hr": "${:.4f}",
            "Confidence": "{:.0%}",
        }),
        use_container_width=True, hide_index=True,
    )

    st.divider()

    # Heatmap — provider × GPU model → median price
    st.subheader("Price heatmap  (provider × GPU model)")
    pivot = (
        df.groupby(["provider", "gpu_model_normalized"])["price_per_hour_usd"]
        .median()
        .unstack(fill_value=None)
    )
    if not pivot.empty:
        fig = px.imshow(
            pivot,
            text_auto=".2f",
            color_continuous_scale="RdYlGn_r",
            labels={"color": "Median $/hr"},
            aspect="auto",
            height=max(300, len(pivot) * 40),
        )
        fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig, use_container_width=True)

    # Average price by provider (bar)
    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Average price by provider")
        fig2 = px.bar(
            summary.sort_values("avg_price"),
            x="avg_price", y="provider", orientation="h",
            color="avg_price", color_continuous_scale="RdYlGn_r",
            labels={"avg_price": "Avg $/hr", "provider": ""},
            height=350,
        )
        fig2.update_layout(margin=dict(l=0, r=0, t=10, b=0), coloraxis_showscale=False)
        st.plotly_chart(fig2, use_container_width=True)

    with col_b:
        st.subheader("Offer count by provider")
        fig3 = px.bar(
            summary.sort_values("offer_count", ascending=False),
            x="provider", y="offer_count",
            color="confidence_score", color_continuous_scale="Blues",
            labels={"offer_count": "Offers", "provider": "", "confidence_score": "Confidence"},
            height=350,
        )
        fig3.update_layout(margin=dict(l=0, r=0, t=10, b=0))
        st.plotly_chart(fig3, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# Page 4 — Historical Trends
# ─────────────────────────────────────────────────────────────────────────────

def page_historical() -> None:
    st.header("📈 Historical Price Trends")

    if not _db_exists(db_path):
        _empty_state("Database not found.")
        return

    hist = load_history(db_path, gpu_filter, time_window)
    if hist.empty:
        _empty_state(
            "Not enough historical data yet. "
            "Run watch mode for a few hours to build history."
        )
        return

    gpu_options = ["All"] + sorted(hist["gpu_model"].unique().tolist())
    selected_gpus = st.multiselect("GPU models to plot", gpu_options, default=["All"])
    if "All" in selected_gpus:
        plot_df = hist
    else:
        plot_df = hist[hist["gpu_model"].isin(selected_gpus)]

    # Min / Median / Max ribbon
    st.subheader("Min / Median / Max price over time")
    for gpu, g in plot_df.groupby("gpu_model"):
        for contract, cg in g.groupby("contract_type"):
            colour = _GPU_COLOURS.get(gpu, "#888888")
            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=cg["timestamp"], y=cg["max_price"],
                name="Max", line=dict(color=colour, dash="dot"), opacity=0.5,
            ))
            fig.add_trace(go.Scatter(
                x=cg["timestamp"], y=cg["min_price"],
                name="Min", fill="tonexty",
                fillcolor=f"rgba(128,128,128,0.15)",
                line=dict(color=colour, dash="dash"), opacity=0.5,
            ))
            fig.add_trace(go.Scatter(
                x=cg["timestamp"], y=cg["median_price"],
                name="Median", line=dict(color=colour, width=2.5),
            ))
            fig.update_layout(
                title=f"{gpu} — {contract}",
                yaxis_title="$/hr",
                height=280,
                margin=dict(l=0, r=0, t=32, b=0),
                showlegend=True,
                legend=dict(orientation="h", y=-0.2),
            )
            st.plotly_chart(fig, use_container_width=True)

    # Raw data expander
    with st.expander("Raw historical data"):
        st.dataframe(plot_df, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────────────────────
# Page 5 — Atlas Routing Simulator
# ─────────────────────────────────────────────────────────────────────────────

def page_routing() -> None:
    st.header("🚀 Atlas Routing Simulator")
    st.caption("Find the optimal GPU provider for your workload.")

    if not _db_exists(db_path):
        _empty_state("Database not found.")
        return

    with st.form("routing_form"):
        col1, col2, col3 = st.columns(3)
        req_gpu      = col1.selectbox("GPU model", ["H100 SXM", "H100 NVL", "H100 PCIe",
                                                     "A100 80GB", "A100 40GB", "L40S", "A10G", "V100 16GB"])
        req_count    = col2.number_input("GPU count needed", min_value=1, max_value=64, value=1)
        req_contract = col3.selectbox("Contract type", ["on-demand", "spot", "reserved", "all"])

        col4, col5 = st.columns(2)
        req_region   = col4.text_input("Preferred region (optional)", placeholder="US, EU, APAC …")
        req_budget   = col5.number_input(
            "Max price per GPU/hr (USD, 0 = no limit)", min_value=0.0, value=0.0, step=0.1,
        )

        submitted = st.form_submit_button("🔍 Find best routes", use_container_width=True)

    if not submitted:
        st.info("Configure your workload requirements above and click **Find best routes**.")
        return

    db = get_db(db_path)
    analytics = PriceAnalytics(db)
    routes = analytics.find_best_routes(
        gpu_model=req_gpu,
        gpu_count=int(req_count),
        contract_type=req_contract,
        region_filter=req_region or None,
        max_price_per_gpu=req_budget if req_budget > 0 else None,
        hours=time_window,
        top_n=10,
    )

    if routes.empty:
        st.warning(
            f"No providers found with **{req_count}× {req_gpu}** "
            f"({req_contract}) matching your criteria in the last {time_window}h.\n\n"
            "Try widening the time window or relaxing region/budget filters."
        )
        return

    st.success(f"Found **{len(routes)}** matching options")

    # Recommendation cards (top 3)
    st.subheader("Top 3 recommendations")
    medals = ["🥇", "🥈", "🥉"]
    cols = st.columns(min(3, len(routes)))
    for i, (_, row) in enumerate(routes.head(3).iterrows()):
        with cols[i]:
            st.markdown(f"### {medals[i]} {row.get('provider', '?')}")
            st.metric("$/GPU/hr",     f"${row.get('price_per_hour_usd', 0):.4f}")
            st.metric("Total $/hr",   f"${row.get('total_price_per_hour', 0):.2f}")
            st.metric("Monthly est.", f"${row.get('monthly_estimate', 0):,.0f}")
            st.caption(f"Region: {row.get('region', '?')} · {row.get('contract_type', '?')}")
            conf = row.get("confidence_score", 0)
            st.progress(float(conf), text=f"Data confidence: {conf:.0%}")

    st.divider()

    # Full ranked table
    st.subheader("All options (ranked by price)")
    display = routes.copy()
    if "price_per_hour_usd" in display.columns:
        display["price_per_hour_usd"]    = display["price_per_hour_usd"].map("${:.4f}".format)
    if "total_price_per_hour" in display.columns:
        display["total_price_per_hour"]  = display["total_price_per_hour"].map("${:.2f}".format)
    if "monthly_estimate" in display.columns:
        display["monthly_estimate"]      = display["monthly_estimate"].map("${:,.0f}".format)
    if "confidence_score" in display.columns:
        display["confidence_score"]      = display["confidence_score"].map("{:.0%}".format)
    if "availability_status" in display.columns:
        display["availability_status"]   = display["availability_status"].map({1: "✅", 0: "❌"})
    if "age_hours" in display.columns:
        display["age_hours"]             = display["age_hours"].map("{:.1f}h ago".format)

    col_rename = {
        "provider": "Provider", "gpu_model_normalized": "GPU",
        "vram_gb": "VRAM GB", "region": "Region", "region_group": "Region group",
        "contract_type": "Contract", "gpu_count": "GPUs avail.",
        "price_per_hour_usd": "$/GPU/hr", "total_price_per_hour": f"Total $/hr ({req_count}×)",
        "monthly_estimate": "Monthly est.", "confidence_score": "Confidence",
        "availability_status": "Available", "age_hours": "Data age",
    }
    display = display.rename(columns=col_rename)
    st.dataframe(display, use_container_width=True, height=380)

    # Cost breakdown chart
    st.subheader("Cost comparison")
    chart_df = routes.copy()
    chart_df["label"] = chart_df.apply(
        lambda r: f"{r.get('provider','?')} ({r.get('region_group','?')})", axis=1
    )
    fig = px.bar(
        chart_df,
        x="label", y="monthly_estimate",
        color="contract_type",
        labels={"monthly_estimate": "Monthly estimate (USD)", "label": ""},
        color_discrete_map={"on-demand": "#4CAF50", "spot": "#FF9800", "reserved": "#2196F3"},
        height=320,
    )
    fig.update_layout(margin=dict(l=0, r=0, t=10, b=0))
    st.plotly_chart(fig, use_container_width=True)


# ─────────────────────────────────────────────────────────────────────────────
# Router
# ─────────────────────────────────────────────────────────────────────────────

if page == "📊 Market Overview":
    page_market_overview()
elif page == "⚡ Arbitrage Opportunities":
    page_arbitrage()
elif page == "🏢 Provider Comparison":
    page_provider_comparison()
elif page == "📈 Historical Trends":
    page_historical()
elif page == "🚀 Atlas Routing":
    page_routing()
