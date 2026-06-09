"""
소수몽키 백엔드 — Day 3 (v2, 재무제표 기반)
배경: Render IP에서 yfinance .info(quoteSummary)가 YFRateLimitError로 차단됨.
      그러나 income_stmt / balance_sheet / quarterly_* / history / dividends / fast_info /
      history_metadata 는 정상 작동(chart·fundamentals-timeseries 엔드포인트).
설계: .info를 아예 호출하지 않고, 재무제표 + 메타데이터로 체크리스트 지표를 직접 계산.
      (로컬에서 .info 원값과 대조 검증 완료: D/E·EPS·PER·매출성장·영업이익률 일치)

엔드포인트:
  GET /                  -> {"status":"ok"}
  GET /quote?ticker=AAPL -> 공통 + (개별주 펀더멘털 | ETF 정보)
  GET /diag?ticker=AAPL  -> 어떤 yfinance 경로가 사는지 진단

단위(소수몽키 체크리스트용):
  operatingMargin / roe / revenueGrowth = 소수(0.12 = 12%)
  dividendYield / debtToEquity / expenseRatio = 퍼센트값(2.28 = 2.28%)
"""

import re
import numpy as np
import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf

app = Flask(__name__)
CORS(app)


def num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except Exception:
        return None


def L(df, *labels, col=0):
    """재무제표 df에서 label(여러 후보) 행의 col번째(최신=0) 값."""
    if df is None or getattr(df, "empty", True):
        return None
    for label in labels:
        if label in df.index:
            try:
                return num(df.loc[label].iloc[col])
            except Exception:
                continue
    return None


def ttm(qdf, *labels, n=4):
    """분기 df에서 label 행의 최근 n개 합(TTM). 값 있는 것만 합산."""
    if qdf is None or getattr(qdf, "empty", True):
        return None
    for label in labels:
        if label in qdf.index:
            try:
                vals = qdf.loc[label].dropna().iloc[:n]
                return float(vals.sum()) if len(vals) > 0 else None
            except Exception:
                continue
    return None


def safe_attr(obj, name):
    try:
        return num(getattr(obj, name, None))
    except Exception:
        return None


def compute_beta(symbol, period="1y"):
    """1년 일간 수익률, SPY 대비 베타(근사)."""
    try:
        s = yf.Ticker(symbol).history(period=period)["Close"].pct_change().dropna()
        m = yf.Ticker("SPY").history(period=period)["Close"].pct_change().dropna()
        j = s.to_frame("s").join(m.to_frame("m"), how="inner").dropna()
        if len(j) < 30:
            return None
        var = np.var(j["m"])
        return float(np.cov(j["s"], j["m"])[0, 1] / var) if var else None
    except Exception:
        return None


def dividend_yield_ttm(t, price):
    try:
        dv = t.dividends
        if dv is None or len(dv) == 0 or not price:
            return 0.0 if (dv is not None) else None
        cutoff = dv.index.max() - pd.Timedelta(days=365)
        ttm_div = float(dv[dv.index >= cutoff].sum())
        return round(ttm_div / price * 100, 4)
    except Exception:
        return None


@app.route("/")
def health():
    return jsonify({"status": "ok"})


@app.route("/quote")
def quote():
    symbol = (request.args.get("ticker") or "").strip().upper()
    if not symbol:
        return jsonify({"ok": False, "error": "ticker 파라미터가 필요합니다. 예: /quote?ticker=AAPL"}), 400

    try:
        t = yf.Ticker(symbol)

        # --- 메타데이터 (chart 엔드포인트, Render OK) → 이름/타입 ---
        meta = {}
        try:
            meta = t.history_metadata or {}
        except Exception:
            meta = {}
        name = meta.get("longName") or meta.get("shortName") or symbol
        qtype = (meta.get("instrumentType") or "").upper()  # EQUITY / ETF / ...

        # --- fast_info (chart, Render OK) → 가격/시총/52주/MA ---
        fi = t.fast_info
        price = safe_attr(fi, "last_price") or num(meta.get("regularMarketPrice"))
        market_cap = safe_attr(fi, "market_cap")
        currency = safe_attr(fi, "currency") or meta.get("currency")
        week52_high = safe_attr(fi, "year_high") or num(meta.get("fiftyTwoWeekHigh"))
        week52_low = safe_attr(fi, "year_low") or num(meta.get("fiftyTwoWeekLow"))
        ma50 = safe_attr(fi, "fifty_day_average")
        ma200 = safe_attr(fi, "two_hundred_day_average")

        # 공통 결과 틀
        out = {
            "ok": True, "ticker": symbol, "type": qtype or "UNKNOWN", "name": name,
            "price": price, "currency": currency, "marketCap": market_cap,
            "week52High": week52_high, "week52Low": week52_low,
            "ma50": ma50, "ma200": ma200,
            "dividendYield": dividend_yield_ttm(t, price),
            # 개별주
            "trailingPE": None, "trailingEps": None, "roe": None, "debtToEquity": None,
            "operatingMargin": None, "revenueGrowth": None, "beta": None,
            # ETF
            "expenseRatio": None, "aum": None, "category": None, "isLeveraged": None,
            "source": "yfinance(statements)",
        }
        diag = {}

        if qtype == "ETF":
            # ETF: 운용보수/카테고리 — funds_data 시도(quoteSummary라 Render에서 막힐 수 있음)
            cat = None
            try:
                fd = t.funds_data
                ov = getattr(fd, "fund_overview", None) or {}
                cat = ov.get("categoryName")
                fo = getattr(fd, "fund_operations", None)
                if fo is not None and not fo.empty and "Annual Report Expense Ratio" in fo.index:
                    er = num(fo.loc["Annual Report Expense Ratio"].iloc[0])  # 소수(0.0075)
                    out["expenseRatio"] = round(er * 100, 4) if er is not None else None  # → 퍼센트(0.75)
                diag["fundsData"] = "ok"
            except Exception as e:
                diag["fundsData"] = f"ERR {type(e).__name__}"
            out["category"] = cat
            lname = (name or "").lower()
            out["isLeveraged"] = ("leverage" in (cat or "").lower()) or bool(re.search(r"\b[23]x\b", lname)) or ("bull" in lname or "bear" in lname)
            # AUM: funds_data Total Net Assets는 단위 불일치로 보류. (Render에서 .info 차단)

        else:
            # 개별주(EQUITY 등): 재무제표에서 직접 계산
            inc = t.income_stmt
            qi = t.quarterly_income_stmt
            qbs = t.quarterly_balance_sheet
            bs = t.balance_sheet
            diag = {"incOk": not getattr(inc, "empty", True),
                    "qiOk": not getattr(qi, "empty", True),
                    "qbsOk": not getattr(qbs, "empty", True)}

            # 영업이익률 (연간, 안정적)
            rev_a = L(inc, "Total Revenue", "Operating Revenue")
            opinc_a = L(inc, "Operating Income", "Total Operating Income As Reported")
            out["operatingMargin"] = round(opinc_a / rev_a, 5) if (opinc_a and rev_a) else None

            # 매출성장률 (최근 분기 YoY = .info 방식)
            qrev0 = L(qi, "Total Revenue", "Operating Revenue", col=0)
            qrev4 = L(qi, "Total Revenue", "Operating Revenue", col=4)
            out["revenueGrowth"] = round((qrev0 - qrev4) / qrev4, 5) if (qrev0 and qrev4) else None

            # EPS(TTM) & PER
            eps_ttm = ttm(qi, "Diluted EPS", "Basic EPS") or L(inc, "Diluted EPS", "Basic EPS")
            out["trailingEps"] = round(eps_ttm, 2) if eps_ttm else None
            out["trailingPE"] = round(price / eps_ttm, 2) if (price and eps_ttm and eps_ttm > 0) else None

            # ROE (TTM 순이익 / 최신 분기 자기자본)
            ni_ttm = ttm(qi, "Net Income", "Net Income Common Stockholders")
            equity = L(qbs, "Stockholders Equity", "Common Stock Equity") or L(bs, "Stockholders Equity", "Common Stock Equity")
            out["roe"] = round(ni_ttm / equity, 4) if (ni_ttm and equity) else None

            # 부채비율 (분기 MRQ = .info와 일치)
            debt = L(qbs, "Total Debt") or L(bs, "Total Debt")
            out["debtToEquity"] = round(debt / equity * 100, 2) if (debt and equity) else None

            # 베타 (1년 일간 vs SPY, 근사)
            out["beta"] = round(compute_beta(symbol), 3) if compute_beta(symbol) else None

        # 데이터가 거의 다 비면 차단으로 판단
        if price is None and market_cap is None and out["operatingMargin"] is None:
            return jsonify({"ok": False, "ticker": symbol,
                            "error": "yfinance 데이터 없음(차단 의심). Render 로그 확인.",
                            "_diag": diag}), 502

        out["_diag"] = diag
        return jsonify(out)

    except Exception as e:
        return jsonify({"ok": False, "ticker": symbol, "error": f"{type(e).__name__}: {e}"}), 502


@app.route("/diag")
def diag():
    symbol = (request.args.get("ticker") or "AAPL").strip().upper()
    t = yf.Ticker(symbol)
    out = {}

    def probe(label, fn):
        try:
            v = fn()
            if v is None:
                out[label] = "none"
            elif hasattr(v, "empty"):
                out[label] = "EMPTY" if v.empty else f"shape {tuple(v.shape)}"
            elif isinstance(v, dict):
                out[label] = f"dict {len(v)}"
            else:
                out[label] = f"{type(v).__name__}={v}"
        except Exception as e:
            out[label] = f"ERR {type(e).__name__}"

    probe("fast_info.last_price", lambda: t.fast_info.last_price)
    probe("history_metadata", lambda: t.history_metadata)
    probe("info", lambda: t.info)
    probe("income_stmt(annual)", lambda: t.income_stmt)
    probe("quarterly_income_stmt", lambda: t.quarterly_income_stmt)
    probe("quarterly_balance_sheet", lambda: t.quarterly_balance_sheet)
    probe("dividends", lambda: t.dividends)
    return jsonify({"ticker": symbol, "probe": out})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
