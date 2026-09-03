"""Streamlit dashboard for the AI Finance Controller.

Talks to the FastAPI service over HTTP -- no direct DB/business-logic
imports here, so the dashboard is a pure consumer of the same API a judge
could curl directly. Every network call is wrapped so an unreachable API or
an empty database degrades to a clear message, never a crash.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd
import plotly.express as px
import requests
import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import config
from app import constants as C

API = config.API_BASE_URL
st.set_page_config(page_title="AI Finance Controller", layout="wide", page_icon="\U0001F9FE")

# Minimal, purposeful styling only: a single accent color for primary actions
# and monospace for identifiers (payment/bank/run ids), so they're easy to
# scan and copy during a live demo. No decoration that competes with the data.
st.markdown(
    """
    <style>
    div[data-testid="stMetricValue"] { font-variant-numeric: tabular-nums; }
    code, .stCode, div[data-testid="stMarkdownContainer"] code { font-size: 0.85em; }
    button[kind="primary"] { background-color: #0a3d91; border-color: #0a3d91; }
    button[kind="primary"]:hover { background-color: #0c4bb3; border-color: #0c4bb3; }
    </style>
    """,
    unsafe_allow_html=True,
)


def fmt_amount(value) -> str:
    """Rupee-format an amount, tolerating None/corrupt values (e.g. a payment
    whose amount field failed validation) instead of crashing the page."""
    if value is None:
        return "N/A (missing/corrupt amount)"
    return f"\u20b9{value:,.2f}"


def api_get(path: str, **params):
    try:
        r = requests.get(f"{API}{path}", params=params, timeout=30)
        if r.status_code >= 400:
            return None, r.json().get("detail", r.text)
        return r.json(), None
    except requests.exceptions.ConnectionError:
        return None, f"Cannot reach the API at {API}. Start it with: uvicorn app.api:app --reload"
    except requests.exceptions.Timeout:
        return None, "API request timed out."


def api_post(path: str, **params):
    try:
        r = requests.post(f"{API}{path}", params=params, timeout=180)
        if r.status_code >= 400:
            return None, r.json().get("detail", r.text)
        return r.json(), None
    except requests.exceptions.ConnectionError:
        return None, f"Cannot reach the API at {API}. Start it with: uvicorn app.api:app --reload"
    except requests.exceptions.Timeout:
        return None, "API request timed out (AI calls can be slow -- try again or check LLM_TIMEOUT_SECONDS)."


# ---------------------------------------------------------------------------
# Sidebar: dataset + reconciliation controls
# ---------------------------------------------------------------------------
st.sidebar.title("\U0001F9FE AI Finance Controller")
health, health_err = api_get("/health")
if health_err:
    st.sidebar.error(health_err)
else:
    ai_badge = f"AI enabled ({health['llm_model']})" if health["ai_enabled"] else "AI disabled (fallback mode)"
    if health["ai_enabled"]:
        st.sidebar.success(ai_badge)
    else:
        st.sidebar.warning(ai_badge)

st.sidebar.subheader("1. Synthetic dataset")
seed = st.sidebar.number_input("Seed", value=config.RANDOM_SEED, step=1)
size = st.sidebar.number_input("Payment records", value=config.DATASET_SIZE, step=50, min_value=10, max_value=20000)
if st.sidebar.button("Generate dataset", width="stretch"):
    with st.spinner("Generating synthetic multi-source dataset..."):
        result, err = api_post("/dataset/generate", seed=int(seed), size=int(size))
    if err:
        st.sidebar.error(err)
    else:
        st.session_state["dataset_summary"] = result
        st.sidebar.success(f"Generated {result['payments']} payments, {result['bank_settlements']} bank rows, "
                            f"{result['invoices']} invoices.")

st.sidebar.subheader("2. Reconciliation")
if st.sidebar.button("Run reconciliation", type="primary", width="stretch"):
    with st.spinner("Ingesting, matching, and reasoning over the batch..."):
        result, err = api_post("/reconcile/run")
    if err:
        st.sidebar.error(err)
    else:
        st.session_state["last_run_id"] = result["run_id"]
        st.sidebar.success(f"Run {result['run_id']} complete: {result['total_payments']} records in "
                            f"{result['total_processing_seconds']}s")

runs, runs_err = api_get("/runs", limit=25)
run_id = None
if runs:
    options = [r["run_id"] for r in runs]
    default_idx = 0
    if st.session_state.get("last_run_id") in options:
        default_idx = options.index(st.session_state["last_run_id"])
    run_id = st.sidebar.selectbox(
        "Inspect run", options,
        format_func=lambda rid: f"{rid} ({next(r['total_payments'] for r in runs if r['run_id']==rid)} records)",
        index=default_idx,
    )
elif runs_err and "No runs" not in str(runs_err):
    st.sidebar.error(runs_err)

if not run_id:
    st.title("AI Finance Controller")
    st.info("No reconciliation run yet. Generate a dataset and click **Run reconciliation** in the sidebar.")
    st.stop()

run_detail, err = api_get(f"/runs/{run_id}")
if err:
    st.error(err)
    st.stop()
metrics = run_detail["metrics"]
evaluation, eval_err = api_get(f"/runs/{run_id}/evaluation")

st.title("AI Finance Controller")
st.caption(f'"We automate the financial decisions we can support with evidence, explain those decisions, '
           f'and safely surface the ones we cannot." — Run `{run_id}` · started {run_detail["started_at"]}')

tab_overview, tab_decisions, tab_exceptions, tab_eval, tab_audit = st.tabs(
    ["\U0001F4CA Overview", "\U0001F50D Decisions", "\u26A0\uFE0F Exceptions", "\u2705 Evaluation", "\U0001F4DC Audit Trail"]
)

# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------
with tab_overview:
    sc = metrics["status_counts"]
    total = metrics["total_payments"]
    auto = sc.get(C.STATUS_AUTO_MATCH, 0)
    ai_matched = sc.get(C.STATUS_AI_ASSISTED_MATCH, 0)
    excepted = sc.get(C.STATUS_EXCEPTION, 0)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Total processed", total)
    c2.metric("Auto-reconciled", auto, help="Deterministic rule match -- no AI involved.")
    c3.metric("AI-assisted matches", ai_matched, help="AI proposed a match AND it passed every policy guardrail.")
    c4.metric("Exceptions", excepted, help="Unresolved -- routed to human review with structured evidence.")
    c5.metric("Reconciliation rate", f"{(auto + ai_matched) / total:.1%}" if total else "-")

    c6, c7, c8, c9 = st.columns(4)
    c6.metric("Throughput", f"{metrics['throughput_per_second']:.1f} rec/s" if metrics["throughput_per_second"] else "-")
    c7.metric("Total processing time", f"{metrics['total_processing_seconds']:.2f}s")
    c8.metric("AI invocations", metrics["ai_invocations"], help="Cases genuinely escalated for semantic reasoning.")
    c9.metric("Avg time/record", f"{metrics['avg_processing_ms_per_record']:.2f} ms" if metrics["avg_processing_ms_per_record"] else "-")

    if evaluation:
        st.subheader("Measured against known ground truth")
        e1, e2, e3, e4 = st.columns(4)
        e1.metric("Automation precision", f"{evaluation['automation_precision']:.1%}" if evaluation["automation_precision"] is not None else "-",
                   help="Of everything auto/AI-matched, the fraction that was actually correct. A false match is the worst outcome.")
        e2.metric("Coverage (recall)", f"{evaluation['coverage_recall']:.1%}" if evaluation["coverage_recall"] is not None else "-",
                   help="Of cases that COULD safely be resolved, the fraction the system actually automated.")
        e3.metric("Safety rate", f"{evaluation['safety_rate']:.1%}" if evaluation["safety_rate"] is not None else "-",
                   help="Of cases that should NEVER be auto-resolved, the fraction correctly escalated instead.")
        e4.metric("False-match rate", f"{evaluation['false_match_rate']:.2%}",
                   help="Of automated decisions, the fraction that were wrong or unsafe. Target: as close to 0% as possible.")

        if evaluation["outcomes"]["INCORRECT_AUTO"] or evaluation["outcomes"]["UNSAFE_AUTO"]:
            st.error(f"{evaluation['outcomes']['INCORRECT_AUTO']} incorrect auto-matches and "
                     f"{evaluation['outcomes']['UNSAFE_AUTO']} unsafe auto-matches detected -- see Evaluation tab.")
        else:
            st.success("Zero false matches and zero unsafe auto-resolutions in this run.")

    col_a, col_b = st.columns(2)
    with col_a:
        st.subheader("Decision mix")
        mix_df = pd.DataFrame({"status": list(sc.keys()), "count": list(sc.values())})
        fig = px.pie(mix_df, names="status", values="count", hole=0.5,
                     color="status", color_discrete_map={
                         C.STATUS_AUTO_MATCH: "#2ca02c", C.STATUS_AI_ASSISTED_MATCH: "#1f77b4", C.STATUS_EXCEPTION: "#d62728"})
        st.plotly_chart(fig, width="stretch")
    with col_b:
        st.subheader("Exception categories")
        cc = metrics["category_counts"]
        if cc:
            cc_df = pd.DataFrame({"category": [C.CATEGORY_LABELS.get(k, k) for k in cc], "count": list(cc.values())}).sort_values("count", ascending=True)
            fig2 = px.bar(cc_df, x="count", y="category", orientation="h")
            st.plotly_chart(fig2, width="stretch")
        else:
            st.info("No exceptions in this run.")

    with st.expander("Ingestion report"):
        st.json(metrics["ingestion_reports"])

# ---------------------------------------------------------------------------
# Decisions explorer
# ---------------------------------------------------------------------------
with tab_decisions:
    st.subheader("Inspect individual decisions")
    f1, f2 = st.columns(2)
    status_filter = f1.selectbox("Status", ["All", C.STATUS_AUTO_MATCH, C.STATUS_AI_ASSISTED_MATCH, C.STATUS_EXCEPTION])
    cat_filter = f2.selectbox("Category", ["All"] + list(C.CATEGORY_LABELS.keys()))

    params = {"run_id": run_id, "limit": 300}
    if status_filter != "All":
        params["status"] = status_filter
    if cat_filter != "All":
        params["category"] = cat_filter
    listing, list_err = api_get("/decisions", **params)

    if list_err:
        st.error(list_err)
    elif not listing["results"]:
        st.info("No decisions match this filter.")
    else:
        df = pd.DataFrame(listing["results"])
        display_cols = ["payment_id", "customer_name", "amount", "order_id", "status", "category",
                         "matched_bank_ref", "confidence", "method", "ai_used", "reason"]
        st.dataframe(df[display_cols], width="stretch", height=350)

        st.markdown(f"Showing {len(df)} of {listing['total']} matching decisions.")
        chosen = st.selectbox("Inspect a payment", df["payment_id"].tolist())
        if chosen:
            detail, derr = api_get(f"/decisions/{chosen}", run_id=run_id)
            if derr:
                st.error(derr)
            else:
                d = detail["decision"]
                p = detail["payment"]
                st.markdown(f"#### `{chosen}` -- {p['customer_name']} -- {fmt_amount(p['amount'])}")
                badge = {"AUTO_MATCH": "\U0001F7E2 Auto-matched (rule)", "AI_ASSISTED_MATCH": "\U0001F535 AI-assisted match",
                         "EXCEPTION": "\U0001F534 Exception"}[d["status"]]
                st.markdown(f"**{badge}** &nbsp;|&nbsp; confidence: {d.get('confidence')} &nbsp;|&nbsp; "
                            f"AI used: {'yes' if d['ai_used'] else 'no'} &nbsp;|&nbsp; invoice: {d.get('invoice_status')}")
                st.write(f"**Reason:** {d['reason']}")
                st.write("**Evidence:**")
                st.json(d.get("evidence", {}))

# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------
with tab_exceptions:
    st.subheader("Exception queue -- what a human reviewer sees")
    ex_cat = st.selectbox("Filter by category", ["All"] + list(C.CATEGORY_LABELS.keys()), key="ex_cat")
    params = {"run_id": run_id, "limit": 300}
    if ex_cat != "All":
        params["category"] = ex_cat
    exs, ex_err = api_get("/exceptions", **params)
    if ex_err:
        st.error(ex_err)
    elif not exs["results"]:
        st.success("No exceptions to review.")
    else:
        for row in exs["results"][:100]:
            label = C.CATEGORY_LABELS.get(row["category"], row["category"])
            with st.expander(f"{row['payment_id']} -- {row['customer_name']} -- {fmt_amount(row['amount'])} -- {label}"):
                ev = row.get("evidence", {})
                st.write(f"**Why unresolved:** {ev.get('why_unresolved', row['reason'])}")
                st.write(f"**Suggested next action:** {row['suggested_action']}")
                if ev.get("attempted"):
                    st.write(f"**Attempted:** {', '.join(ev['attempted'])}")
                if ev.get("evidence_found"):
                    st.write("**Evidence found:**")
                    st.json(ev["evidence_found"])
                if ev.get("ai_evidence"):
                    st.write("**AI reasoning:**")
                    st.json(ev["ai_evidence"])

# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
with tab_eval:
    st.subheader("Evaluation against known ground truth")
    if not evaluation:
        st.error(eval_err or "Evaluation unavailable.")
    else:
        st.markdown(
            "Every ground-truth payment is classified as either **safely resolvable** (a correct answer exists "
            "in the evidence) or **not safely resolvable** (no system, human or AI, could determine a unique "
            "correct answer). The system is scored on both correctness AND safety."
        )
        outcomes = evaluation["outcomes"]
        odf = pd.DataFrame({"outcome": list(outcomes.keys()), "count": list(outcomes.values())})
        color_map = {"CORRECT_AUTO": "#2ca02c", "CORRECTLY_ESCALATED": "#1f77b4",
                     "MISSED_OPPORTUNITY": "#ff7f0e", "INCORRECT_AUTO": "#d62728", "UNSAFE_AUTO": "#8b0000"}
        fig = px.bar(odf, x="outcome", y="count", color="outcome", color_discrete_map=color_map)
        st.plotly_chart(fig, width="stretch")

        st.subheader("Accuracy by difficulty case type")
        rows = []
        for ct, v in evaluation["per_case_type"].items():
            resolvable_n = v["CORRECT_AUTO"] + v["INCORRECT_AUTO"] + v["MISSED_OPPORTUNITY"]
            unresolvable_n = v["UNSAFE_AUTO"] + v["CORRECTLY_ESCALATED"]
            if resolvable_n:
                correct_rate = v["CORRECT_AUTO"] / resolvable_n
            else:
                correct_rate = v["CORRECTLY_ESCALATED"] / unresolvable_n if unresolvable_n else None
            rows.append({"case_type": ct, "total": v["total"], "correct_rate": correct_rate, **v})
        cdf = pd.DataFrame(rows).sort_values("total", ascending=False)
        st.dataframe(cdf, width="stretch")

        if evaluation["incorrect_auto_examples"] or evaluation["unsafe_auto_examples"]:
            st.error("Cases where automation was WRONG or UNSAFE:")
            st.json({"incorrect_auto": evaluation["incorrect_auto_examples"], "unsafe_auto": evaluation["unsafe_auto_examples"]})

        st.subheader("Why this beats \"just fuzzy-match everything\"")
        st.markdown(
            "The naive baseline below always commits to the closest-amount bank record in the settlement "
            "window -- no reference check, no name evidence, no duplicate detection, no ambiguity detection, "
            "no AI, no policy caps. Same dataset, same ground truth, same scoring function -- the only "
            "variable is evidence-gating."
        )
        baseline, base_err = api_get("/baseline")
        if base_err:
            st.warning(base_err)
        else:
            comp_rows = [
                {"metric": "Automation precision", "This system": evaluation["automation_precision"], "Naive baseline": baseline["automation_precision"]},
                {"metric": "Coverage (recall)", "This system": evaluation["coverage_recall"], "Naive baseline": baseline["coverage_recall"]},
                {"metric": "Safety rate", "This system": evaluation["safety_rate"], "Naive baseline": baseline["safety_rate"]},
                {"metric": "False-match rate", "This system": evaluation["false_match_rate"], "Naive baseline": baseline["false_match_rate"]},
            ]
            comp_df = pd.DataFrame(comp_rows)
            fig3 = px.bar(comp_df, x="metric", y=["This system", "Naive baseline"], barmode="group",
                          color_discrete_map={"This system": "#2ca02c", "Naive baseline": "#d62728"})
            fig3.update_layout(yaxis_tickformat=".0%", yaxis_title=None, xaxis_title=None, legend_title=None)
            st.plotly_chart(fig3, width="stretch")
            b1, b2 = st.columns(2)
            b1.metric("Naive baseline false-match rate", f"{baseline['false_match_rate']:.1%}",
                       help="How often the naive matcher confidently reconciles the WRONG record or one that shouldn't be auto-resolved at all.")
            b2.metric("This system's false-match rate", f"{evaluation['false_match_rate']:.1%}",
                       delta=f"{(evaluation['false_match_rate'] - baseline['false_match_rate']):.1%}", delta_color="inverse")

# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------
with tab_audit:
    st.subheader("Audit trail")
    audit_payment = st.text_input("Filter by payment_id (optional)")
    params = {"run_id": run_id, "limit": 300}
    if audit_payment:
        params["payment_id"] = audit_payment
    audit, aerr = api_get("/audit", **params)
    if aerr:
        st.error(aerr)
    elif not audit["results"]:
        st.info("No audit entries.")
    else:
        adf = pd.DataFrame(audit["results"])
        cols = ["created_at", "payment_id", "actor", "status", "category", "ai_used", "confidence", "reason"]
        st.dataframe(adf[cols], width="stretch", height=450)
