"""Executive Dashboard Streamlit view."""
from __future__ import annotations

import hashlib
import io
import os

import auth
import pandas as pd
import plotly.graph_objects as go
import polars as pl
import streamlit as st

from app_pages.paths import NETSUITE_DIR

def render_executive_dashboard() -> None:
    """Enterprise SaaS layout: single column, spacing, typography tokens; native Streamlit only."""
    st.markdown(
        """
<style>
    /* 1. Main block — wide layout, institutional padding */
    .block-container {
        padding-top: 2rem !important;
        padding-bottom: 2rem !important;
        max-width: 95% !important;
    }

    /* 2. S&P / Capital IQ style metric cards */
    [data-testid="stMetric"] {
        background-color: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 6px;
        padding: 1rem;
        box-shadow: 0 1px 2px 0 rgba(0, 0, 0, 0.05);
    }

    /* Metric labels (e.g. "WEEKLY PROCUREMENT") */
    [data-testid="stMetricLabel"] > div {
        color: #64748B !important;
        font-size: 0.75rem !important;
        font-weight: 700 !important;
        text-transform: uppercase !important;
        letter-spacing: 0.05em !important;
    }

    /* Metric values (e.g. "$206k") */
    [data-testid="stMetricValue"] > div {
        color: #0F172A !important;
        font-size: 1.8rem !important;
        font-weight: 600 !important;
        font-family: "Helvetica Neue", Helvetica, Arial, sans-serif !important;
    }

    /* Metric delta (e.g. "Ceiling: $120k") */
    [data-testid="stMetricDelta"] > div {
        font-size: 0.85rem !important;
        font-weight: 500 !important;
    }

    /* 3. Alert contrast (status tier) — :contains() is not valid CSS; use structure + text color */
    [data-testid="stAlert"] {
        border-radius: 4px;
        font-weight: 600;
        border-left: 4px solid;
    }
    [data-testid="stAlert"] [data-testid="stMarkdownContainer"],
    [data-testid="stAlert"] [data-testid="stMarkdownContainer"] p,
    [data-testid="stAlert"] [data-testid="stMarkdownContainer"] span {
        color: #0F172A !important;
    }
    /* st.error — cash collision / breach (red shell, legible body) */
    div[data-testid="stAlert"][kind="error"],
    div[data-testid="stAlert"][data-baseweb="notification"][kind="error"] {
        background-color: #FEF2F2 !important;
        border-left-color: #DC2626 !important;
    }
    div[data-testid="stAlert"][kind="error"] [data-testid="stMarkdownContainer"] p,
    div[data-testid="stAlert"][kind="error"] [data-testid="stMarkdownContainer"] span {
        color: #991B1B !important;
    }
    /* st.warning */
    div[data-testid="stAlert"][kind="warning"] {
        background-color: #FFFBEB !important;
        border-left-color: #D97706 !important;
    }
    div[data-testid="stAlert"][kind="warning"] [data-testid="stMarkdownContainer"] p,
    div[data-testid="stAlert"][kind="warning"] [data-testid="stMarkdownContainer"] span {
        color: #92400E !important;
    }
    /* st.success */
    div[data-testid="stAlert"][kind="success"] {
        background-color: #ECFDF5 !important;
        border-left-color: #16A34A !important;
    }
    div[data-testid="stAlert"][kind="success"] [data-testid="stMarkdownContainer"] p,
    div[data-testid="stAlert"][kind="success"] [data-testid="stMarkdownContainer"] span {
        color: #166534 !important;
    }

    /* 4. Expander / governance */
    [data-testid="stExpander"] {
        background-color: #FFFFFF !important;
        border: 1px solid #E2E8F0 !important;
        border-radius: 6px !important;
    }
    [data-testid="stExpander"] summary p {
        font-weight: 600 !important;
        color: #0F172A !important;
    }

    /* 5. Section titles — beats main_dashboard section.main h3 (same specificity, later cascade) */
    section.main h3 {
        color: #004385 !important;
        font-weight: 600 !important;
        font-size: 1.1rem !important;
        margin-bottom: -0.5rem !important;
        border-bottom: 1px solid #E2E8F0;
        padding-bottom: 0.5rem;
    }

    /* Custom callouts (raw HTML in this view) */
    .govnr-exec-soft-info {
        background: #f8fafc;
        border: 1px solid #e2e8f0;
        border-radius: 12px;
        padding: 24px 28px;
        color: #334155;
        font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
        font-size: 14px;
        line-height: 1.55;
        margin-bottom: 1rem;
    }
    .govnr-exec-soft-ok {
        background: #ecfdf5;
        border: 1px solid #bbf7d0;
        border-radius: 12px;
        padding: 20px 24px;
        color: #166534;
        font-family: "Helvetica Neue", Helvetica, Arial, sans-serif;
        font-size: 14px;
        margin-bottom: 1rem;
    }
</style>
        """,
        unsafe_allow_html=True,
    )

    def _gap():
        st.markdown(
            '<div style="height:2.5rem;min-height:32px" aria-hidden="true"></div>',
            unsafe_allow_html=True,
        )

    def _exec_transfer_df(artifacts: dict) -> pd.DataFrame:
        for name, payload in (artifacts or {}).items():
            if "Report_4" in name and str(name).lower().endswith(".csv"):
                try:
                    return pd.read_csv(io.BytesIO(payload))
                except Exception:
                    return pd.DataFrame()
        return pd.DataFrame()

    def _exec_tile_label(filename: str) -> str:
        base = filename.rsplit("/", 1)[-1]
        stem = base.replace("_", " ")
        low = stem.lower()
        if low.endswith(".pdf"):
            return f"{stem[:-4]} (PDF)"
        if low.endswith(".csv"):
            return f"{stem[:-4]} (CSV)"
        return base

    def _usd_pulse(n: float) -> str:
        n = float(n or 0.0)
        ax = abs(n)
        if ax >= 1_000_000:
            return f"${n / 1_000_000:.2f}M"
        if ax >= 1_000:
            return f"${n / 1_000:,.0f}k".replace(",", "")
        return f"${n:,.0f}"

    def _partition_audit_artifacts(
        artifacts: dict,
    ) -> tuple[list[tuple[str, bytes]], list[tuple[str, bytes]], list[tuple[str, bytes]]]:
        """Route vault files into 3-pillar locker: acute intercepts, Report_* ledger CSVs, executive PDFs."""
        acute: list[tuple[str, bytes]] = []
        systemic: list[tuple[str, bytes]] = []
        brief: list[tuple[str, bytes]] = []
        for fname, payload in sorted((artifacts or {}).items()):
            if not isinstance(payload, (bytes, bytearray)):
                continue
            raw = bytes(payload)
            low = fname.lower()
            base = fname.rsplit("/", 1)[-1]
            if low.endswith(".pdf") or "executive_brief" in low:
                brief.append((fname, raw))
            elif "action_intercept" in low:
                acute.append((fname, raw))
            elif low.endswith(".csv") and base.lower().startswith("report_"):
                systemic.append((fname, raw))
            elif low.endswith(".csv"):
                systemic.append((fname, raw))
        return acute, systemic, brief

    EXEC_CARD_BG = "#FFFFFF"

    st.markdown("# Executive Dashboard")
    if st.session_state.pop("exec_governance_applied", False):
        st.markdown(
            '<div class="govnr-exec-soft-ok">Governance rules saved to <strong>client_ingestion_config</strong>. '
            "Pulse and the action queue use the updated thresholds.</div>",
            unsafe_allow_html=True,
        )
    st.caption("CFO view — procurement integrity, inactive capital, and inter-branch optimization.")

    timeframe = st.selectbox(
        "History window",
        ["Last 30 Days", "Last Quarter", "Year-to-Date", "All Time"],
        key="exec_timeframe",
    )

    client_uuid_dash = st.session_state["client_uuid"]
    from engine.erp_config_loader import (
        fetch_ingestion_config,
        resolve_enforce_supersession,
        resolve_toxic_po_material_threshold,
        resolve_min_transfer_value_threshold,
        resolve_weekly_otb_limit,
    )
    from engine.netsuite_parser import ingest_po_activity
    from engine.logic_cash import compute_rolling_po_exposure_total, rolling_po_orders_in_window

    _cfg_dash = fetch_ingestion_config(client_uuid_dash)
    _w_show, _ = resolve_weekly_otb_limit(_cfg_dash.get("weekly_otb_limit"))
    _m_show, _ = resolve_min_transfer_value_threshold(_cfg_dash.get("min_transfer_value_threshold"))
    toxic_raw, _ = resolve_toxic_po_material_threshold(_cfg_dash.get("toxic_po_material_threshold"))
    super_raw = resolve_enforce_supersession(_cfg_dash.get("enforce_supersession"))
    weekly_ceiling_resolved, _ = resolve_weekly_otb_limit(_cfg_dash.get("weekly_otb_limit"))

    records = auth.get_client_metrics_history(client_uuid_dash)
    df_history = pd.DataFrame(records) if records else pd.DataFrame()
    db_artifacts = auth.get_latest_report_artifacts(client_uuid_dash)

    df_xfer = _exec_transfer_df(db_artifacts)
    xfer_n = (
        int(df_xfer["Item_Identifier"].nunique())
        if not df_xfer.empty and "Item_Identifier" in df_xfer.columns
        else 0
    )

    df_po_live = ingest_po_activity(client_uuid_dash, erp_config=_cfg_dash)
    draft_live = None if df_po_live.is_empty() else compute_rolling_po_exposure_total(df_po_live)
    po_window_pl = rolling_po_orders_in_window(df_po_live) if not df_po_live.is_empty() else None

    if not records:
        latest = None
        df_filtered = pd.DataFrame()
        draft_po = float(draft_live) if draft_live is not None else 0.0
        weekly_ceiling = float(weekly_ceiling_resolved)
        xfer_opportunity_count = xfer_n
        t_imb = 0
        c_int = 0
        inactive_cap = 0.0
        turnover_x = 0.0
        intercepted = 0.0
    else:
        numeric_cols = (
            "total_capital_deployed",
            "dead_stock_val",
            "inventory_turnover",
            "gross_margin_pct",
            "rolling_po_exposure",
            "weekly_otb_limit",
            "intercepted_cash_total",
            "suppressed_cash_total",
            "otb_breach_amount",
        )
        for col in numeric_cols:
            if col in df_history.columns:
                df_history[col] = pd.to_numeric(df_history[col], errors="coerce")
        for col in ("transfer_imbalance_items", "cost_integrity_items", "otb_breach_count"):
            if col in df_history.columns:
                df_history[col] = pd.to_numeric(df_history[col], errors="coerce").fillna(0).astype(int)

        df_history["date_recorded"] = pd.to_datetime(df_history["date_recorded"])
        today = pd.Timestamp.today().normalize()
        df_filtered = df_history.copy()
        if timeframe == "Last 30 Days":
            df_filtered = df_filtered[df_filtered["date_recorded"] >= (today - pd.Timedelta(days=30))]
        elif timeframe == "Last Quarter":
            df_filtered = df_filtered[df_filtered["date_recorded"] >= (today - pd.Timedelta(days=90))]
        elif timeframe == "Year-to-Date":
            df_filtered = df_filtered[
                df_filtered["date_recorded"] >= pd.Timestamp(year=today.year, month=1, day=1)
            ]
        if df_filtered.empty:
            df_filtered = df_history.copy()

        latest = df_filtered.iloc[0]
        draft_po = float(draft_live) if draft_live is not None else float(
            pd.to_numeric(latest.get("rolling_po_exposure"), errors="coerce") or 0.0
        )
        weekly_ceiling = float(weekly_ceiling_resolved)

        t_imb = int(latest.get("transfer_imbalance_items") or 0)
        c_int = int(latest.get("cost_integrity_items") or 0)
        xfer_opportunity_count = max(xfer_n, t_imb)

        inactive_cap = float(pd.to_numeric(latest.get("dead_stock_val"), errors="coerce") or 0.0)
        turnover_x = float(pd.to_numeric(latest.get("inventory_turnover"), errors="coerce") or 0.0)
        intercepted = float(pd.to_numeric(latest.get("intercepted_cash_total"), errors="coerce") or 0.0)

    cash_collision_risk = weekly_ceiling > 0 and draft_po > weekly_ceiling
    over_otb = cash_collision_risk

    # --- Level 1: Immediate alerts (status) ---
    if cash_collision_risk:
        st.error(
            "🚨 CASH COLLISION DETECTED: Draft PO exposure exceeds the Weekly Procurement Ceiling."
        )
    if not records:
        st.warning(
            "**Optimization required** — Run Manual Execution (or your scheduled sync) to generate the first "
            "snapshot. Pulse metrics populate after the first successful engine run."
        )
    elif not cash_collision_risk:
        if xfer_opportunity_count > 0 or c_int > 0:
            st.warning(
                "**Optimization required** — Non-urgent flags (inter-warehouse transfers or integrity items). "
                "Review the action queue when convenient."
            )
        else:
            st.success(
                "**All systems nominal** — No procurement limit breach. Latest snapshot is within the active ceiling."
            )

    _gap()

    # --- Level 2: Executive metrics (pulse) ---
    with st.container(border=True):
        st.caption("Executive pulse — rolling 7-day draft exposure, capital at risk, and intercept posture.")
        c_m1, c_m2, c_m3, c_m4 = st.columns(4)
        ceiling_delta = f"Ceiling: {_usd_pulse(weekly_ceiling)}" if weekly_ceiling > 0 else None
        c_m1.metric("Weekly procurement", _usd_pulse(draft_po), delta=ceiling_delta)
        c_m2.metric("Dead stock risk", _usd_pulse(inactive_cap))
        c_m3.metric("Inv. turnover", f"{turnover_x:.2f}x")
        c_m4.metric("Capital intercepted", _usd_pulse(intercepted))

    _gap()

    # --- Level 3: Governance & limits (control knobs) ---
    with st.expander("⚙️ Procurement Governance & Alert Thresholds", expanded=False):
        st.caption(
            "Values persist in PostgreSQL **client_ingestion_config** and apply on the next engine run. "
            "Saving refreshes procurement math on this screen."
        )
        with st.form("cfo_limits_form"):
            gov_ceiling = st.number_input(
                "Weekly Procurement Ceiling ($) (formerly Weekly OTB limit)",
                min_value=0.0,
                step=1000.0,
                value=float(_w_show),
                help="Rolling 7-day draft PO ceiling.",
                key="cfo_weekly_procurement_ceiling",
            )
            gov_min_xfer = st.number_input(
                "Inter-Warehouse Liquidity Threshold ($) (formerly Min transfer value)",
                min_value=0.0,
                step=100.0,
                value=float(_m_show),
                help="Minimum economic size for inter-warehouse liquidity moves.",
                key="cfo_inter_wh_liquidity_threshold",
            )
            gov_stagnant = st.number_input(
                "Stagnant Procurement Threshold ($) (formerly Toxic PO materiality)",
                min_value=0.0,
                step=50.0,
                value=float(toxic_raw),
                help="Materiality floor for stagnant / dead-stock procurement intercepts.",
                key="cfo_stagnant_procurement_threshold",
            )
            gov_super = st.checkbox(
                "Enforce supersession blocks",
                value=bool(super_raw),
                help="Block new part POs when superseded legacy stock is available.",
                key="cfo_enforce_supersession",
            )
            submitted_cfo = st.form_submit_button("Update Governance Rules", type="primary")
    if submitted_cfo:
        auth.upsert_client_ingestion_erp_settings(
            client_uuid_dash,
            float(gov_ceiling),
            float(gov_min_xfer),
            float(gov_stagnant),
            bool(gov_super),
        )
        st.session_state["exec_governance_applied"] = True
        st.rerun()

    _gap()

    # --- 3-Pillar Audit Locker (native downloads; below governance) ---
    st.subheader("Audit locker")
    st.caption("Latest engine artifacts from PostgreSQL — acute intercepts, ledger audits, and executive briefing.")
    if not db_artifacts:
        st.info("No artifacts in PostgreSQL yet. Run **Manual Execution** (or scheduled ingestion) to populate the locker.")

    _acute_art, _systemic_art, _brief_art = _partition_audit_artifacts(db_artifacts)
    c_aud1, c_aud2, c_aud3 = st.columns(3)
    with c_aud1:
        st.markdown("### 🔴 Alert Intercepts (Acute Action required).")
        if not _acute_art:
            st.caption("No acute intercept exports in vault.")
        for i, (fname, payload) in enumerate(_acute_art):
            st.download_button(
                label=f"Download {_exec_tile_label(fname)}",
                data=payload,
                file_name=fname,
                mime="text/csv" if fname.lower().endswith(".csv") else "application/octet-stream",
                key=f"audit_p3_acute_{i}_{hashlib.sha256(fname.encode()).hexdigest()[:10]}",
                use_container_width=True,
                type="primary",
            )
    with c_aud2:
        st.markdown("### 🛠️ Systemic Cleanup (Ledger Audits).")
        if not _systemic_art:
            st.caption("No systemic ledger audit CSVs in vault.")
        for i, (fname, payload) in enumerate(_systemic_art):
            st.download_button(
                label=f"Download {_exec_tile_label(fname)}",
                data=payload,
                file_name=fname,
                mime="text/csv",
                key=f"audit_p3_sys_{i}_{hashlib.sha256(fname.encode()).hexdigest()[:10]}",
                use_container_width=True,
                type="secondary",
            )
    with c_aud3:
        st.markdown("### 📅 Executive Brief (Monthly PDF).")
        if not _brief_art:
            st.caption("No executive briefing PDF in vault.")
        for i, (fname, payload) in enumerate(_brief_art):
            st.download_button(
                label=f"Download {_exec_tile_label(fname)}",
                data=payload,
                file_name=fname,
                mime="application/pdf" if fname.lower().endswith(".pdf") else "application/octet-stream",
                key=f"audit_p3_brf_{i}_{hashlib.sha256(fname.encode()).hexdigest()[:10]}",
                use_container_width=True,
                type="primary",
            )

    _gap()

    if records:
        with st.container(border=True):
            st.markdown(
                '<div style="font-size:18px;font-weight:700;color:#0F172A;margin-bottom:20px;'
                'font-family:Helvetica,Arial,sans-serif;">Trends</div>',
                unsafe_allow_html=True,
            )
            cap_hi = (
                max(weekly_ceiling * 1.15, draft_po * 1.1, 1.0)
                if weekly_ceiling > 0
                else max(draft_po * 1.1, 1.0)
            )
            ref_line = weekly_ceiling if weekly_ceiling > 0 else 0.0
            _ind_kw = dict(
                mode="number+gauge+delta" if ref_line > 0 else "number+gauge",
                value=draft_po,
                number={
                    "prefix": "$",
                    "valueformat": ",.0f",
                    "font": {"size": 18, "color": "#0F172A"},
                },
                gauge={
                    "shape": "bullet",
                    "axis": {
                        "range": [0, cap_hi],
                        "tickcolor": "#94a3b8",
                        "tickwidth": 1,
                        "tickfont": {"size": 11, "color": "#64748b"},
                        "showticklabels": True,
                    },
                    "threshold": {
                        "line": {"color": "#2563eb", "width": 1},
                        "thickness": 0.85,
                        "value": ref_line if ref_line > 0 else cap_hi,
                    },
                    "bgcolor": "#e2e8f0",
                    "bar": {"color": "#1e40af"},
                },
                title={"text": ""},
            )
            if ref_line > 0:
                _ind_kw["delta"] = {
                    "reference": ref_line,
                    "valueformat": ",.0f",
                    "increasing": {"color": "#64748B"},
                    "decreasing": {"color": "#64748B"},
                }
            fig_bullet = go.Figure(go.Indicator(**_ind_kw))
            fig_bullet.update_layout(
                height=160,
                margin=dict(l=16, r=16, t=24, b=16),
                paper_bgcolor=EXEC_CARD_BG,
                plot_bgcolor=EXEC_CARD_BG,
                font=dict(color="#64748B", family="Helvetica, Arial, sans-serif"),
                xaxis=dict(showgrid=False, zeroline=False, visible=False),
                yaxis=dict(showgrid=False, zeroline=False, visible=False),
            )
            st.plotly_chart(fig_bullet, use_container_width=True, config={"displayModeBar": False})
            _gap()

            dh = df_filtered.sort_values("date_recorded")[["date_recorded", "dead_stock_val"]].copy()
            dh["month"] = dh["date_recorded"].dt.strftime("%Y-%m")
            agg_dead = dh.groupby("month", as_index=False).last()
            fig_dead = go.Figure(
                data=[
                    go.Bar(
                        x=agg_dead["month"],
                        y=agg_dead["dead_stock_val"],
                        name="Inactive capital",
                        marker_color="#64748b",
                        width=0.45,
                    )
                ]
            )
            fig_dead.update_layout(
                height=160,
                margin=dict(l=12, r=12, t=16, b=12),
                showlegend=False,
                paper_bgcolor=EXEC_CARD_BG,
                plot_bgcolor=EXEC_CARD_BG,
                font=dict(color="#64748B", family="Helvetica, Arial, sans-serif"),
            )
            fig_dead.update_xaxes(showgrid=False, zeroline=False, tickfont=dict(size=11, color="#64748b"))
            fig_dead.update_yaxes(
                showgrid=False,
                zeroline=False,
                showticklabels=True,
                tickfont=dict(size=11, color="#64748b"),
                title_text=None,
            )
            st.plotly_chart(fig_dead, use_container_width=True, config={"displayModeBar": False})
            _gap()

            turn_hist = df_filtered.sort_values("date_recorded")
            fig_spark = go.Figure(
                go.Scatter(
                    x=turn_hist["date_recorded"],
                    y=turn_hist["inventory_turnover"],
                    mode="lines",
                    line=dict(width=2, color="#2563eb"),
                )
            )
            fig_spark.update_xaxes(visible=False, showgrid=False, zeroline=False)
            fig_spark.update_yaxes(visible=False, showgrid=False, zeroline=False)
            fig_spark.update_layout(
                height=88,
                margin=dict(l=8, r=8, t=8, b=8),
                plot_bgcolor=EXEC_CARD_BG,
                paper_bgcolor=EXEC_CARD_BG,
                font=dict(color="#64748B", family="Helvetica, Arial, sans-serif"),
            )
            st.plotly_chart(fig_spark, use_container_width=True, config={"displayModeBar": False})
    else:
        st.markdown(
            '<div class="govnr-exec-soft-info">No historical ledger rows yet. Run <strong>Manual Execution</strong> '
            "(or your scheduled sync) to populate trend charts.</div>",
            unsafe_allow_html=True,
        )

    _gap()
    po_breakdown_open = bool(st.session_state.get("exec_open_po_breakdown")) or (
        bool(latest is not None) and over_otb
    )

    collision_rows = []
    if po_window_pl is not None and not po_window_pl.is_empty():
        pdf = po_window_pl.to_pandas()
        for _, r in pdf.iterrows():
            amt = float(r.get("Order_Total_Amount") or 0.0)
            if weekly_ceiling > 0 and amt > weekly_ceiling:
                collision_rows.append(r)
        if over_otb and not collision_rows:
            pdf2 = pdf.sort_values("Order_Total_Amount", ascending=False).head(12)
            collision_rows = [pdf2.iloc[i] for i in range(len(pdf2))]

    show_po_cards = bool(collision_rows) and (
        over_otb
        or any(
            float(r.get("Order_Total_Amount") or 0) > weekly_ceiling
            for r in collision_rows
            if weekly_ceiling > 0
        )
    )

    xfer_cards_df = pd.DataFrame()
    if not df_xfer.empty and "Item_Identifier" in df_xfer.columns:
        loc_col = "Item_Location" if "Item_Location" in df_xfer.columns else None
        val_col = "Transfer_Capital_Value" if "Transfer_Capital_Value" in df_xfer.columns else None
        if loc_col and val_col:
            xfer_cards_df = (
                df_xfer.groupby("Item_Identifier", as_index=False)
                .agg(
                    locations=(loc_col, lambda s: " · ".join(sorted({str(x) for x in s.dropna().unique()}))),
                    transfer_value=(val_col, "sum"),
                )
                .sort_values("transfer_value", ascending=False)
            )

    queue_empty = not show_po_cards and xfer_cards_df.empty

    st.subheader("Immediate action queue")
    st.caption("Decision-ready items only; threshold-filtered noise reduction.")
    _gap()

    if over_otb and queue_empty:
        st.error(
            "**Limited PO detail** — Procurement limit breach is active on rolling exposure, but there are no "
            "PO rows or transfer lines to show. Stage **po_activity.csv** and refresh, or run the engine to "
            "repopulate artifacts."
        )
    elif queue_empty:
        st.success("**Procurement strategy optimized** — No action required.")
    else:
        if show_po_cards:
            st.markdown(
                '<div style="font-size:18px;font-weight:700;color:#0F172A;margin:24px 0 12px 0;">'
                "Procurement limit breach — orders</div>",
                unsafe_allow_html=True,
            )
            exp_po = st.expander(
                "PO exposure breakdown (filtered table)",
                expanded=po_breakdown_open,
            )
            with exp_po:
                if po_window_pl is not None and not po_window_pl.is_empty():
                    ptab = po_window_pl.to_pandas()
                    _sty = (
                        ptab.style.map(
                            lambda v: "color: #b91c1c; font-weight: 600"
                            if isinstance(v, (int, float)) and weekly_ceiling > 0 and float(v) > weekly_ceiling
                            else "",
                            subset=["Order_Total_Amount"],
                        )
                        if "Order_Total_Amount" in ptab.columns
                        else ptab.style
                    )
                    st.dataframe(_sty, use_container_width=True, hide_index=True)
                else:
                    st.caption("Stage **po_activity.csv** to enumerate PO-level exposure.")

            st.caption(
                "Govnr surfaces alerts only — execute decisions in your ERP or workflow tools."
            )
            _gap()
            for _idx, row in enumerate(collision_rows):
                po_num = str(row.get("PO_Number", ""))
                val = float(row.get("Order_Total_Amount") or 0.0)
                vendor = str(row.get("Purchase_Order_Vendor", "") or "—")
                po_label = po_num if po_num else f"PO row {_idx + 1}"
                st.metric(label=po_label, value=f"${val:,.2f}", help=vendor)
                _gap()

        if not xfer_cards_df.empty:
            st.markdown(
                '<div style="font-size:18px;font-weight:700;color:#0F172A;margin:24px 0 12px 0;">'
                "Inter-branch opportunities</div>",
                unsafe_allow_html=True,
            )
            for j, xr in xfer_cards_df.head(20).iterrows():
                item = str(xr.get("Item_Identifier", ""))
                locs = str(xr.get("locations", ""))
                tv = float(xr.get("transfer_value") or 0.0)
                xfer_label = f"Part {item}" if item else f"Transfer line {j}"
                st.metric(label=xfer_label, value=f"${tv:,.2f}", help=f"Locations: {locs}")
                _gap()

    _gap()
    with st.expander("Diagnostics & historical ledger", expanded=False):
        st.caption("Integrity audit exports and raw metric history for engineering review.")
        if records:
            st.dataframe(df_history.head(12), use_container_width=True, hide_index=True)
        base_path = os.path.dirname(os.path.abspath(__file__))
        outbound_dir = os.path.join(base_path, "netsuite_core", "data", "outbound")
        st.markdown(
            f'<p style="color:#334155;font-size:14px;">Outbound path: <code>{outbound_dir}</code> '
            f"(exists: {os.path.exists(outbound_dir)})</p>",
            unsafe_allow_html=True,
        )


