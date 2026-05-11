"""Manual execution: disk-backed stdout and DB metrics (no large session_state)."""
import os
from typing import Optional

import auth

from app_pages.paths import NETSUITE_DIR

def _manual_stdout_path() -> str:
    """Disk-backed manual execution log (never store large stdout in st.session_state)."""
    return os.path.join(NETSUITE_DIR, "data", "outbound", "_last_manual_stdout.txt")


def _persist_manual_stdout(text: str) -> None:
    path = _manual_stdout_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(text or "")
    except OSError:
        pass


def _read_manual_stdout() -> str:
    path = _manual_stdout_path()
    if not os.path.isfile(path):
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def _latest_manual_run_metrics_from_db(client_uuid: str) -> dict:
    """Fresh OTB row for Manual Execution UI — no metrics dict in session_state."""
    rows = auth.get_client_metrics_history(client_uuid)
    if not rows:
        return {}
    r = rows[0]
    if not isinstance(r, dict):
        return {}
    wlim = float(r.get("weekly_otb_limit") or 0)
    roll = float(r.get("rolling_po_exposure") or 0)
    inter = float(r.get("intercepted_cash_total") or 0)
    supp = float(r.get("suppressed_cash_total") or 0)
    otb_breach = int(r.get("otb_breach_count") or 0)
    xfer = int(r.get("transfer_imbalance_items") or 0)
    return {
        "weekly_otb_limit": wlim,
        "rolling_po_total": roll,
        "rolling_po_exposure": roll,
        "intercepted_cash_total": inter,
        "suppressed_cash_total": supp,
        "is_exception_state": bool(otb_breach > 0 or inter > 0.0 or xfer > 0),
    }


def _upsert_manual_run_buffers(client_uuid: str, buffers: Optional[dict]) -> None:
    """Persist pipeline artifacts to PostgreSQL (same path as engine); never stash bytes in session_state."""
    for fname, payload in (buffers or {}).items():
        if not fname or not isinstance(payload, (bytes, bytearray)):
            continue
        try:
            auth.upsert_report_artifact(client_uuid, fname, bytes(payload))
        except Exception:
            pass
