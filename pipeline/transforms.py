from transforms.api import transform, Input, Output
from transforms.verbs.dataframes import sanitize_schema_for_parquet
from pyspark.sql import SparkSession
import pandas as pd
import numpy as np
import re

# -- Dataset RIDs -------------------------------------------------------------
IS_RID = "ri.foundry.main.dataset.e9855b4f-5c65-4bce-92e7-161d212e77c7"
BS_RID = "ri.foundry.main.dataset.09fc00c7-0f7c-4c94-9b52-b5ae32ca1777"
CF_RID = "ri.foundry.main.dataset.81308bc8-59c4-4df8-8c09-e17e704c8825"


def _sanitize_col(name):
    """
    Foundry/Parquet column name rules:
    - Must start with a letter or underscore
    - Only letters, digits, underscores allowed (no spaces, hyphens,
      commas, parentheses, slashes, dots, ampersands, %, etc.)
    - Column names must be unique after sanitization
    """
    s = re.sub(r"[^A-Za-z0-9_]", "_", str(name))
    # If it starts with a digit, prefix with underscore
    if s and s[0].isdigit():
        s = "_" + s
    # If empty after sanitization, fallback
    if not s or s == "_":
        s = "col"
    return s


def _dedup_columns(cols):
    """Deduplicate column names after sanitization (Foundry rejects duplicate names)."""
    seen = {}
    result = []
    for c in cols:
        if c in seen:
            seen[c] += 1
            result.append(f"{c}_{seen[c]}")
        else:
            seen[c] = 0
            result.append(c)
    return result


def _to_spark(df):
    """
    Convert pandas DataFrame to Spark, applying all Foundry schema safety steps:
    1. Sanitize column names (no special chars)
    2. Deduplicate column names
    3. Convert all values to string (avoids type inference issues)
    4. Replace NaN/None with None (Spark-compatible nulls)
    5. Use sanitize_schema_for_parquet as final Spark-level safety net
    """
    spark = SparkSession.builder.getOrCreate()

    # Sanitize and deduplicate columns
    sanitized = _dedup_columns([_sanitize_col(c) for c in df.columns])
    df.columns = sanitized

    # Convert to string, replace NaN with None
    df_str = df.astype(str).where(pd.notnull(df), None)

    # Create Spark DataFrame
    sdf = spark.createDataFrame(df_str)

    # Foundry's own Parquet sanitizer as an additional safety net
    sdf = sanitize_schema_for_parquet(sdf)

    return sdf


def _parse_raw(source):
    """
    Read raw Foundry dataset, transpose from wide (metrics as rows) to
    tall (dates as rows), parse dates robustly.
    Raw format: first column = metric names, remaining columns = dates (MMDDYYYY).
    """
    df = source.dataframe().toPandas()

    # Use first column as index regardless of its name
    df = df.set_index(df.columns[0]).T.reset_index()
    df.rename(columns={"index": "Date"}, inplace=True)

    # Dates come in as MMDDYYYY (e.g. 09302025)
    # Try explicit format first, fall back to infer with errors='coerce'
    try:
        df["Date"] = pd.to_datetime(df["Date"], format="%m%d%Y")
    except Exception:
        df["Date"] = pd.to_datetime(df["Date"], infer_datetime_format=True, errors="coerce")

    df["Year"] = df["Date"].dt.year
    return df


# -- TRANSFORM 1: Clean Income Statement --------------------------------------
@transform(
    output=Output("/Japnoor Singh-cc67d5/Apple Financial Intelligence/income_statement_clean"),
    source=Input(IS_RID),
)
def clean_income_statement(source, output):
    df = _parse_raw(source)

    # Filter to fiscal years 2016-2025
    df = df[(df["Year"] >= 2016) & (df["Year"] <= 2025)].copy()

    # Rename known columns (only if they exist in the data)
    rename_map = {
        "Revenue": "Revenue",
        "Cost Of Goods Sold": "COGS",
        "Gross Profit": "GrossProfit",
        "Research And Development Expenses": "RD",
        "SG&A Expenses": "SGA",
        "Operating Income": "EBIT",
        "Income Taxes": "Tax",
        "Net Income": "NetIncome",
        "EBITDA": "EBITDA",
    }
    df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}, inplace=True)
    df = df.loc[:, ~df.columns.duplicated()].copy()

    # Convert numeric columns (divide by 1000 to go from millions to billions)
    num_cols = ["Revenue", "COGS", "GrossProfit", "RD", "SGA", "EBIT", "Tax", "NetIncome", "EBITDA"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce") / 1000

    # Derived metrics
    if "GrossProfit" in df.columns and "Revenue" in df.columns:
        df["GrossMargin_pct"] = (df["GrossProfit"] / df["Revenue"] * 100).round(2)
    if "EBIT" in df.columns and "Revenue" in df.columns:
        df["EBITMargin_pct"] = (df["EBIT"] / df["Revenue"] * 100).round(2)
    if "NetIncome" in df.columns and "Revenue" in df.columns:
        df["NetMargin_pct"] = (df["NetIncome"] / df["Revenue"] * 100).round(2)

    df["Scenario"] = "Actual"
    df = df.sort_values("Year").reset_index(drop=True)
    df["Date"] = df["Date"].astype(str)

    output.write_dataframe(_to_spark(df))


# -- TRANSFORM 2: Clean Balance Sheet -----------------------------------------
@transform(
    output=Output("/Japnoor Singh-cc67d5/Apple Financial Intelligence/balance_sheet_clean"),
    source=Input(BS_RID),
)
def clean_balance_sheet(source, output):
    df = _parse_raw(source)

    df = df[(df["Year"] >= 2016) & (df["Year"] <= 2025)].copy()

    rename_map = {
        "Cash On Hand": "Cash",
        "Receivables": "AR",
        "Inventory": "Inventory",
        "Net PPE": "PPE",
        "Long Term Debt": "LTDebt",
        "Short Term Debt": "STDebt",
        "Total Assets": "TotalAssets",
        "Total Liabilities": "TotalLiab",
        "Share Holder Equity": "Equity",
    }
    df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}, inplace=True)

    num_cols = ["Cash", "AR", "Inventory", "PPE", "LTDebt", "STDebt", "TotalAssets", "TotalLiab", "Equity"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce") / 1000

    if "LTDebt" in df.columns and "STDebt" in df.columns:
        df["TotalDebt"] = df["LTDebt"].fillna(0) + df["STDebt"].fillna(0)
    if "TotalDebt" in df.columns and "Cash" in df.columns:
        df["NetDebt"] = df["TotalDebt"] - df["Cash"].fillna(0)

    df["Scenario"] = "Actual"
    df = df.sort_values("Year").reset_index(drop=True)
    df["Date"] = df["Date"].astype(str)

    output.write_dataframe(_to_spark(df))


# -- TRANSFORM 3: Clean Cash Flow ---------------------------------------------
@transform(
    output=Output("/Japnoor Singh-cc67d5/Apple Financial Intelligence/cashflow_clean"),
    source=Input(CF_RID),
)
def clean_cashflow(source, output):
    df = _parse_raw(source)

    df = df[(df["Year"] >= 2016) & (df["Year"] <= 2025)].copy()

    rename_map = {
        "Net Income/Loss": "NetIncome",
        "Total Depreciation And Amortization - Cash Flow": "DA",
        "Capital Expenditures": "Capex_raw",
        "Cash Dividends Paid": "Dividends_raw",
        "Cash Flow From Operating Activities": "CFO",
    }
    df.rename(columns={k: v for k, v in rename_map.items() if k in df.columns}, inplace=True)

    num_cols = ["NetIncome", "DA", "Capex_raw", "Dividends_raw", "CFO"]
    for c in num_cols:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce") / 1000

    if "Capex_raw" in df.columns:
        df["Capex"] = df["Capex_raw"].abs()
    if "Dividends_raw" in df.columns:
        df["Dividends"] = df["Dividends_raw"].abs()
    if "CFO" in df.columns and "Capex" in df.columns:
        df["FCF"] = df["CFO"] - df["Capex"]

    df["Scenario"] = "Actual"
    df = df.sort_values("Year").reset_index(drop=True)
    df["Date"] = df["Date"].astype(str)

    output.write_dataframe(_to_spark(df))


# -- TRANSFORM 4: Master Dashboard + Projections ------------------------------
@transform(
    output=Output("/Japnoor Singh-cc67d5/Apple Financial Intelligence/master_dashboard"),
    is_data=Input("/Japnoor Singh-cc67d5/Apple Financial Intelligence/income_statement_clean"),
    cf_data=Input("/Japnoor Singh-cc67d5/Apple Financial Intelligence/cashflow_clean"),
)
def build_master_dashboard(is_data, cf_data, output):
    is_df = is_data.dataframe().toPandas()
    cf_df = cf_data.dataframe().toPandas()

    # Convert numeric columns (they were stored as strings)
    for col in is_df.columns:
        try:
            is_df[col] = pd.to_numeric(is_df[col])
        except (ValueError, TypeError):
            pass

    for col in cf_df.columns:
        try:
            cf_df[col] = pd.to_numeric(cf_df[col])
        except (ValueError, TypeError):
            pass

    # Pull actual columns that exist (sanitized names from upstream)
    actual_cols = [
        "Year",
        "Revenue",
        "GrossProfit",
        "EBIT",
        "NetIncome",
        "GrossMargin_pct",
        "EBITMargin_pct",
        "NetMargin_pct",
        "Scenario",
    ]
    existing_actual_cols = [c for c in actual_cols if c in is_df.columns]
    actuals = is_df[existing_actual_cols].copy()

    cf_merge_cols = [c for c in ["Year", "CFO", "FCF", "Capex"] if c in cf_df.columns]
    if "Year" in cf_merge_cols and len(cf_merge_cols) > 1:
        actuals = actuals.merge(cf_df[cf_merge_cols], on="Year", how="left")

    # Build projections from last actual year
    is_df_numeric = is_df[pd.to_numeric(is_df["Year"], errors="coerce").notna()].copy()
    is_df_numeric["Year"] = pd.to_numeric(is_df_numeric["Year"])
    base_year = int(is_df_numeric["Year"].max())
    base_rev_series = pd.to_numeric(is_df_numeric[is_df_numeric["Year"] == base_year]["Revenue"], errors="coerce")

    if base_rev_series.empty or base_rev_series.isna().all():
        # Fallback: use most recent non-null Revenue
        base_rev = float(pd.to_numeric(is_df_numeric["Revenue"], errors="coerce").dropna().iloc[-1])
    else:
        base_rev = float(base_rev_series.values[0])

    scenarios = {
        "Bear": {"rev_g": 0.04, "cogs_pct": 0.65, "opex_pct": 0.13, "da_pct": 0.05, "tax": 0.23, "capex_pct": 0.055},
        "Base": {"rev_g": 0.07, "cogs_pct": 0.62, "opex_pct": 0.12, "da_pct": 0.045, "tax": 0.21, "capex_pct": 0.05},
        "Bull": {"rev_g": 0.09, "cogs_pct": 0.60, "opex_pct": 0.11, "da_pct": 0.04, "tax": 0.20, "capex_pct": 0.045},
    }

    proj_rows = []
    for scen, d in scenarios.items():
        rev = base_rev
        for yr in range(base_year + 1, base_year + 7):
            rev = rev * (1 + d["rev_g"])
            gp = rev * (1 - d["cogs_pct"])
            ebit = gp - rev * d["opex_pct"]
            da = rev * d["da_pct"]
            ni = max(ebit - rev * 0.01, 0) * (1 - d["tax"])
            cfo = ni + da
            capex = rev * d["capex_pct"]
            fcf = cfo - capex
            proj_rows.append(
                {
                    "Year": yr,
                    "Scenario": scen,
                    "Revenue": round(rev, 2),
                    "GrossProfit": round(gp, 2),
                    "EBIT": round(ebit, 2),
                    "NetIncome": round(ni, 2),
                    "GrossMargin_pct": round((1 - d["cogs_pct"]) * 100, 1),
                    "EBITMargin_pct": round(ebit / rev * 100, 1),
                    "NetMargin_pct": round(ni / rev * 100, 1),
                    "CFO": round(cfo, 2),
                    "FCF": round(fcf, 2),
                    "Capex": round(capex, 2),
                }
            )

    proj_df = pd.DataFrame(proj_rows)
    master = pd.concat([actuals, proj_df], ignore_index=True)
    master = master.sort_values(["Year", "Scenario"]).reset_index(drop=True)

    output.write_dataframe(_to_spark(master))


# -- TRANSFORM 5: DCF Sensitivity Table ---------------------------------------
@transform(
    output=Output("/Japnoor Singh-cc67d5/Apple Financial Intelligence/dcf_sensitivity"),
    is_data=Input("/Japnoor Singh-cc67d5/Apple Financial Intelligence/income_statement_clean"),
)
def build_dcf_sensitivity(is_data, output):
    is_df = is_data.dataframe().toPandas()

    for col in is_df.columns:
        try:
            is_df[col] = pd.to_numeric(is_df[col])
        except (ValueError, TypeError):
            pass

    is_df_numeric = is_df[pd.to_numeric(is_df["Year"], errors="coerce").notna()].copy()
    is_df_numeric["Year"] = pd.to_numeric(is_df_numeric["Year"])
    base_year = int(is_df_numeric["Year"].max())
    base_rev_series = pd.to_numeric(is_df_numeric[is_df_numeric["Year"] == base_year]["Revenue"], errors="coerce")

    if base_rev_series.empty or base_rev_series.isna().all():
        base_rev = float(pd.to_numeric(is_df_numeric["Revenue"], errors="coerce").dropna().iloc[-1])
    else:
        base_rev = float(base_rev_series.values[0])

    d = {"rev_g": 0.07, "cogs_pct": 0.62, "opex_pct": 0.12, "da_pct": 0.045, "tax": 0.21, "capex_pct": 0.05}

    fcfs = []
    rev = base_rev
    for _ in range(6):
        rev = rev * (1 + d["rev_g"])
        gp = rev * (1 - d["cogs_pct"])
        ebit = gp - rev * d["opex_pct"]
        da = rev * d["da_pct"]
        ni = max(ebit - rev * 0.01, 0) * (1 - d["tax"])
        cfo = ni + da
        capex = rev * d["capex_pct"]
        fcfs.append(cfo - capex)

    # Net debt approximation (in billions)
    net_debt = 53.756

    rows = []
    for wacc in [0.07, 0.08, 0.09, 0.10, 0.11]:
        for tgr in [0.01, 0.02, 0.025, 0.03, 0.035]:
            if wacc <= tgr:
                continue  # Terminal growth rate must be less than WACC
            pv_fcf = sum(f / (1 + wacc) ** i for i, f in enumerate(fcfs, 1))
            tv = fcfs[-1] * (1 + tgr) / (wacc - tgr)
            pv_tv = tv / (1 + wacc) ** 6
            ev = pv_fcf + pv_tv
            eq_val = ev - net_debt
            rows.append(
                {
                    "WACC_pct": round(wacc * 100, 1),
                    "TerminalGrowth_pct": round(tgr * 100, 1),
                    "PV_FCFs_Bn": round(pv_fcf, 1),
                    "PV_TerminalValue_Bn": round(pv_tv, 1),
                    "EnterpriseValue_Bn": round(ev, 1),
                    "ImpliedSharePrice_USD": round(eq_val / 17, 2),
                }
            )

    output.write_dataframe(_to_spark(pd.DataFrame(rows)))
