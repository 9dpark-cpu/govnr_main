"""Manual Execution Streamlit view."""
from __future__ import annotations

import io
import os
from contextlib import redirect_stdout

import streamlit as st

import auth

from app_pages.paths import NETSUITE_DIR
from app_pages.manual_helpers import (
    _latest_manual_run_metrics_from_db,
    _persist_manual_stdout,
    _read_manual_stdout,
    _upsert_manual_run_buffers,
)


def render_manual_execution_page() -> None:
    # Onboarding mapping uploaders hold file buffers in session_state; drop when opening Manual Execution.
    for _k in ("erp_fu_inv", "erp_fu_as", "erp_fu_po"):
        st.session_state.pop(_k, None)

    st.title("Manual Execution")
    tier = st.session_state.get("account_tier", "diagnostic")

    st.info(
        "Runs the Govnr engine on CSVs already placed in the secure inbound folder: "
        "**inventory_snapshot.csv**, **annual_sold.csv**, and optionally **po_activity.csv**. "
        "Use **Onboarding Settings** to map columns and SFTP patterns; set **weekly procurement ceiling** and "
        "**minimum transfer threshold** on the Executive Dashboard."
    )
    with st.expander("Intraday CSV Upload (Manual Override)", expanded=False):
        with st.form("intraday_upload_form", clear_on_submit=True):
            inv_fu = st.file_uploader("inventory_snapshot.csv (required)", type=["csv"], key="man_inv")
            as_fu = st.file_uploader("annual_sold.csv (required)", type=["csv"], key="man_as")
            po_fu = st.file_uploader("po_activity.csv (optional)", type=["csv"], key="man_po")
            submitted = st.form_submit_button("Stage Uploaded Files")
        if submitted:
            if inv_fu is None or as_fu is None:
                st.error("Both inventory_snapshot.csv and annual_sold.csv are required to stage files.")
            else:
                inbound_dir = os.path.join(NETSUITE_DIR, "data", "inbound")
                os.makedirs(inbound_dir, exist_ok=True)
                with open(os.path.join(inbound_dir, "inventory_snapshot.csv"), "wb") as f:
                    f.write(inv_fu.getbuffer())
                with open(os.path.join(inbound_dir, "annual_sold.csv"), "wb") as f:
                    f.write(as_fu.getbuffer())
                po_path = os.path.join(inbound_dir, "po_activity.csv")
                if po_fu is not None:
                    with open(po_path, "wb") as f:
                        f.write(po_fu.getbuffer())
                elif os.path.exists(po_path):
                    os.remove(po_path)
                st.success("Files securely staged. Browser memory flushed.")
    if tier == "diagnostic":
        st.caption(
            "Diagnostic tier: place files in inbound (or save from Onboarding Settings) and run from here."
        )
    else:
        st.markdown(
            "Enterprise SFTP Pipeline: Automated 6:00 AM ingestion relies on the Port 443 Drop Zone. "
            "Ensure filename patterns match configuration."
        )

    if st.button("Run Manual Execution", type="primary", key="btn_run_health_scan"):
        from main import run_daily_governor

        st.session_state.pop("manual_exec_metrics_saved", None)
        st.session_state.pop("manual_exec_save_error", None)

        with st.spinner("Executing Govnr logic…"):
            report_buffer = io.StringIO()
            with redirect_stdout(report_buffer):
                result = run_daily_governor(
                    client_uuid=st.session_state["client_uuid"],
                    run_tier=tier,
                    company_name=st.session_state["company_name"],
                )

        _persist_manual_stdout(report_buffer.getvalue())
        cu = st.session_state["client_uuid"]

        if result is None:
            st.session_state["manual_exec_success"] = False
        else:
            _upsert_manual_run_buffers(cu, result.get("buffers"))
            st.session_state["manual_exec_success"] = True
            err = result.get("metrics_save_error")
            if err:
                st.session_state["manual_exec_save_error"] = err
                st.session_state["manual_exec_metrics_saved"] = False
            else:
                st.session_state.pop("manual_exec_save_error", None)
                st.session_state["manual_exec_metrics_saved"] = True
        st.rerun()

    stdout_text = _read_manual_stdout()
    if stdout_text:
        st.subheader("Govnr terminal output")
        st.code(stdout_text, language="markdown")

    if st.session_state.get("manual_exec_success"):
        buffers = auth.get_latest_report_artifacts(st.session_state["client_uuid"])
        metrics = _latest_manual_run_metrics_from_db(st.session_state["client_uuid"])

        if metrics:
            st.subheader("Run summary (OTB)")
            intercepted = float(metrics.get("intercepted_cash_total", 0.0) or 0.0)
            suppressed = float(metrics.get("suppressed_cash_total", 0.0) or 0.0)
            col_otb1, col_otb2, col_otb3, col_otb4 = st.columns(4)
            col_otb1.metric(
                "Weekly OTB limit (CFO)",
                f"${float(metrics.get('weekly_otb_limit', 0)):,.2f}",
            )
            col_otb2.metric(
                "Rolling 7-day PO exposure",
                f"${float(metrics.get('rolling_po_total', 0)):,.2f}",
                help="Same total as the compliance brief and CFO PDF (anchor = max PO Date_Created).",
            )
            col_otb3.metric(
                "Capital Intercepted",
                f"${intercepted:,.2f}",
            )
            col_otb4.metric(
                "Opportunity Cost (Suppressed)",
                f"${suppressed:,.2f}",
            )

            is_exception = bool(metrics.get("is_exception_state", False))
            if is_exception:
                st.warning(
                    "⚠️ Acute Exceptions Detected: In an automated SFTP run, this state triggers immediate Email and SMS dispatch."
                )
            else:
                st.success(
                    "✅ Status Normal: In an automated SFTP run, outward communications would be suppressed to prevent alert fatigue."
                )

        if st.session_state.get("manual_exec_metrics_saved"):
            st.success("Financial metrics securely logged to Govnr Vault.")
        elif st.session_state.get("manual_exec_save_error"):
            st.error(f"Internal Metrics Logging Error: {st.session_state['manual_exec_save_error']}")

        st.subheader("Generated Manual Execution deliverables")
        if st.button("🚀 Dispatch CFO Alerts (Email/SMS)", type="primary", key="btn_manual_dispatch_alerts"):
            from engine.mailer import send_outbound_email, send_outbound_sms

            client_email = auth.fetch_client_contact_email(st.session_state["client_uuid"])
            acute_attachments = {}
            db_artifacts = auth.get_latest_report_artifacts(st.session_state["client_uuid"])
            for key in (
                "Action_Intercept_Audit_Log.csv",
                "Report_4_warehouse_transfers.csv",
            ):
                payload = db_artifacts.get(key)
                if isinstance(payload, (bytes, bytearray)):
                    acute_attachments[key] = bytes(payload)

            send_outbound_email(
                recipient_email=client_email,
                subject="Govnr Manual Dispatch: Acute CSV Hit-Lists",
                body_text="Manual execution complete. Review the attached intercept and transfer CSV reports.",
                attachments=acute_attachments,
            )
            send_outbound_sms("+15555555555", "Govnr Manual Run complete. Check email for acute PO intercepts.")
            st.success("Alerts successfully dispatched to CFO and Operations.")

        st.markdown("### 🔴 Immediate Operational Directives")
        acute_found = False
        for filename, payload in buffers.items():
            if "Action_Intercept" in filename or "Govnr_Executive_Brief" in filename:
                acute_found = True
                st.download_button(
                    label=f"Download {filename}",
                    data=payload,
                    file_name=filename,
                    mime="text/csv" if filename.endswith(".csv") else "application/pdf",
                    key=f"manual_acute_{filename}",
                )
        if not acute_found:
            st.caption("No acute directive reports were triggered in this run.")

        st.markdown("### 🛠️ Systemic Cleanup Projects")
        systemic_found = False
        for filename, payload in buffers.items():
            if filename.startswith("Report_"):
                systemic_found = True
                st.download_button(
                    label=f"Download {filename}",
                    data=payload,
                    file_name=filename,
                    mime="text/csv",
                    key=f"manual_systemic_{filename}",
                )
        if not systemic_found:
            st.caption("No systemic cleanup project reports were triggered in this run.")

    elif st.session_state.get("manual_exec_success") is False and stdout_text:
        st.error(
            "Manual Execution halted. Ensure mandatory inbound files exist, mappings are saved, "
            "and required columns are present (see terminal output for MissingFileError or SchemaDriftError)."
        )
