import streamlit as st

st.set_page_config(
    page_title="Govnr",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Bootstrap netsuite_core on sys.path before engine imports from app_pages.*
import app_pages.paths  # noqa: F401

import auth

from app_pages.executive import render_executive_dashboard
from app_pages.manual import render_manual_execution_page
from app_pages.onboarding import onboarding_settings_page

if "authenticated" not in st.session_state:
    st.session_state["authenticated"] = False
if "client_uuid" not in st.session_state:
    st.session_state["client_uuid"] = None
if "company_name" not in st.session_state:
    st.session_state["company_name"] = None


def login_page():
    st.title("Govnr Platform Login")
    st.markdown("Secure SaaS CFO Automotive Wholesaler Portal")

    tab1, tab2 = st.tabs(["Log In", "Register New Account"])

    with tab1:
        st.subheader("Client Login")
        with st.form("login_form", clear_on_submit=False):
            email = st.text_input("Work Email", key="login_email")
            password = st.text_input("Password", type="password", key="login_password")
            submitted = st.form_submit_button("Access Dashboard")

        if submitted:
            if email and password:
                with st.spinner("Signing in…"):
                    success, result = auth.login_client(email, password)
                if success:
                    st.session_state["authenticated"] = True
                    st.session_state["client_uuid"] = str(result["client_uuid"])
                    st.session_state["company_name"] = result["company_name"]
                    st.session_state["account_tier"] = result.get("account_tier", "diagnostic")
                    st.session_state["inbound_email_prefix"] = result.get("inbound_email_prefix", "client")
                    st.session_state["govnr_nav_module"] = "Executive Dashboard"
                    st.rerun()
                else:
                    st.error(result)
            else:
                st.warning("Please enter both email and password.")

    with tab2:
        st.subheader("Onboarding Registration")
        reg_company = st.text_input("Company Name", key="reg_company")
        reg_email = st.text_input("CFO / Admin Email", key="reg_email")
        reg_password = st.text_input("Create Password", type="password", key="reg_password")

        if st.button("Register & Generate Secure ID", key="btn_register"):
            if reg_company and reg_email and reg_password:
                with st.spinner("Creating your account…"):
                    success, result = auth.register_new_client(reg_company, reg_email, reg_password)
                if success:
                    st.success(
                        "Account created successfully! Please switch to the Log In tab to access your dashboard."
                    )
                else:
                    st.error(result)
            else:
                st.warning("Please fill out all registration fields.")


def main_dashboard():
    for _legacy in (
        "manual_exec_stdout",
        "manual_exec_metrics",
        "manual_exec_buffers",
        "erp_mapping_files_ready",
        "inventory_column_mapping",
    ):
        st.session_state.pop(_legacy, None)

    st.markdown(
        """
<style>
[data-testid="stAppViewContainer"] .main {
    background-color: #F8FAFC !important;
}
[data-testid="stSidebar"] {
    background: linear-gradient(180deg, #0f172a 0%, #1e293b 100%) !important;
    border-right: none !important;
}
[data-testid="stSidebar"] p,
[data-testid="stSidebar"] span:not([data-baseweb="tag"]),
[data-testid="stSidebar"] label {
    color: #e2e8f0 !important;
}
[data-testid="stSidebar"] [data-baseweb="select"] span {
    color: #f8fafc !important;
}
section.main h1, section.main h2, section.main h3 {
    color: #1E293B !important;
}
section.main [data-testid="stCaption"] {
    color: #64748B !important;
}
</style>
        """,
        unsafe_allow_html=True,
    )
    st.sidebar.title(f"{st.session_state['company_name']}")
    if st.sidebar.button("Log Out"):
        st.session_state["authenticated"] = False
        st.session_state["client_uuid"] = None
        st.session_state["company_name"] = None
        for _k in (
            "manual_exec_success",
            "manual_exec_save_error",
            "manual_exec_metrics_saved",
            "_govnr_nav_previous",
        ):
            st.session_state.pop(_k, None)
        st.rerun()

    st.sidebar.markdown("---")
    st.sidebar.title("Govnr Navigation")

    def _on_nav_module_change() -> None:
        """Clear file-uploader widget state when switching modules — UploadedFile lives only in
        st.session_state (not PostgreSQL) and serializing multi‑MB buffers causes white-screen crashes."""
        old = st.session_state.get("_govnr_nav_previous")
        new = st.session_state.get("govnr_nav_module")
        if old == "Manual Execution" and new != "Manual Execution":
            for _k in ("man_inv", "man_as", "man_po"):
                st.session_state.pop(_k, None)
        if old == "Onboarding Settings" and new != "Onboarding Settings":
            for _k in ("erp_fu_inv", "erp_fu_as", "erp_fu_po"):
                st.session_state.pop(_k, None)
        st.session_state["_govnr_nav_previous"] = new

    if "_govnr_nav_previous" not in st.session_state:
        st.session_state["_govnr_nav_previous"] = st.session_state.get(
            "govnr_nav_module", "Executive Dashboard"
        )

    _nav_modules = ("Executive Dashboard", "Onboarding Settings", "Manual Execution")
    if st.session_state.get("govnr_nav_module") not in _nav_modules:
        st.session_state["govnr_nav_module"] = "Executive Dashboard"

    app_mode = st.sidebar.selectbox(
        "Select Module",
        list(_nav_modules),
        key="govnr_nav_module",
        on_change=_on_nav_module_change,
    )

    if app_mode == "Executive Dashboard":
        render_executive_dashboard()
    elif app_mode == "Onboarding Settings":
        onboarding_settings_page()
    elif app_mode == "Manual Execution":
        render_manual_execution_page()

    st.session_state["_govnr_nav_previous"] = app_mode


if __name__ == "__main__":
    if st.session_state.get("authenticated", False):
        main_dashboard()
    else:
        login_page()
