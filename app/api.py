from datetime import datetime, timedelta

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse, Response

from app.fetcher import fetch_market_data
from app.filters import clean_and_filter_data
from app.scorer import score_coins
from app.risk import add_risk_labels
from app.ai_explainer import add_explanations
from app.prediction_service import add_prediction

from app.reporter import (
    get_top_overall,
    get_top_momentum,
    get_top_safer,
    get_alert_candidates,
    get_scan_mix,
)
from app.export_utils import dataframe_to_csv_text


app = FastAPI(
    title="Crypto AI Agent API",
    version="1.3.0",
    description="Scan, score, classify, explain, and export crypto market candidates."
)

CACHE_TTL_SECONDS = 180
_cache = {
    "data": None,
    "updated_at": None,
}


def run_pipeline():
    df_raw = fetch_market_data()
    if df_raw is None or df_raw.empty:
        return None

    df_filtered = clean_and_filter_data(df_raw)
    if df_filtered is None or df_filtered.empty:
        return None

    df_scored = score_coins(df_filtered)
    if df_scored is None or df_scored.empty:
        return None

    df_labeled = add_risk_labels(df_scored)
    if df_labeled is None or df_labeled.empty:
        return None

    # اول explanation
    df_final = add_explanations(df_labeled)
    if df_final is None or df_final.empty:
        return None

    # بعد prediction
    df_final = add_prediction(df_final)
    if df_final is None or df_final.empty:
        return None

    return df_final


def get_cached_pipeline():
    now = datetime.utcnow()

    if (
        _cache["data"] is not None
        and _cache["updated_at"] is not None
        and now - _cache["updated_at"] < timedelta(seconds=CACHE_TTL_SECONDS)
    ):
        return _cache["data"]

    df = run_pipeline()
    if df is not None:
        _cache["data"] = df
        _cache["updated_at"] = now

    return df


def make_csv_response(df, filename: str) -> Response:
    csv_text = dataframe_to_csv_text(df)
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8-sig",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"'
        },
    )


@app.get("/")
def root():
    return {
        "message": "Crypto AI Agent API is running",
        "endpoints": [
            "/scan",
            "/alerts",
            "/top-overall",
            "/top-momentum",
            "/top-safer",
            "/refresh",
            "/export/top-overall",
            "/export/top-momentum",
            "/export/top-safer",
            "/export/full-scan",
        ],
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
    }


@app.get("/refresh")
def refresh():
    df = run_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "Refresh failed. No data could be fetched or processed."}
        )

    _cache["data"] = df
    _cache["updated_at"] = datetime.utcnow()

    return {
        "message": "Cache refreshed successfully.",
        "rows": len(df),
        "updated_at": _cache["updated_at"].isoformat(),
    }


@app.get("/scan")
def scan(limit: int = Query(6, ge=1, le=50)):
    df = get_cached_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No data could be fetched or processed."}
        )

    result = get_scan_mix(df, limit).to_dict(orient="records")
    return {
        "count": len(result),
        "cached": True,
        "results": result,
    }


@app.get("/alerts")
def alerts(limit: int = Query(5, ge=1, le=20)):
    df = get_cached_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No data could be fetched or processed."}
        )

    result = get_alert_candidates(df, limit).to_dict(orient="records")
    return {
        "count": len(result),
        "cached": True,
        "results": result,
    }


@app.get("/top-overall")
def top_overall(limit: int = Query(10, ge=1, le=50)):
    df = get_cached_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No data could be fetched or processed."}
        )

    result = get_top_overall(df, limit).to_dict(orient="records")
    return {
        "count": len(result),
        "cached": True,
        "results": result,
    }


@app.get("/top-momentum")
def top_momentum(limit: int = Query(10, ge=1, le=50)):
    df = get_cached_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No data could be fetched or processed."}
        )

    result = get_top_momentum(df, limit).to_dict(orient="records")
    return {
        "count": len(result),
        "cached": True,
        "results": result,
    }


@app.get("/top-safer")
def top_safer(limit: int = Query(10, ge=1, le=50)):
    df = get_cached_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No data could be fetched or processed."}
        )

    result = get_top_safer(df, limit).to_dict(orient="records")
    return {
        "count": len(result),
        "cached": True,
        "results": result,
    }


@app.get("/export/top-overall")
def export_top_overall(limit: int = Query(10, ge=1, le=200)):
    df = get_cached_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No data could be fetched or processed."}
        )

    export_df = get_top_overall(df, limit)
    return make_csv_response(export_df, "top_overall.csv")


@app.get("/export/top-momentum")
def export_top_momentum(limit: int = Query(10, ge=1, le=200)):
    df = get_cached_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No data could be fetched or processed."}
        )

    export_df = get_top_momentum(df, limit)
    return make_csv_response(export_df, "top_momentum.csv")


@app.get("/export/top-safer")
def export_top_safer(limit: int = Query(10, ge=1, le=200)):
    df = get_cached_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No data could be fetched or processed."}
        )

    export_df = get_top_safer(df, limit)
    return make_csv_response(export_df, "top_safer.csv")


@app.get("/export/full-scan")
def export_full_scan(limit: int = Query(100, ge=1, le=1000)):
    df = get_cached_pipeline()
    if df is None:
        return JSONResponse(
            status_code=503,
            content={"error": "No data could be fetched or processed."}
        )

    export_df = get_scan_mix(df, limit)
    return make_csv_response(export_df, "full_scan.csv")