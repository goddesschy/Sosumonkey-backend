"""
소수몽키 백엔드 — Day 3 (v3, Render 실측 반영 최종)
Render IP 실측 결론:
  ✅ 작동: income_stmt / balance_sheet / quarterly_* / history / dividends / fast_info
  ⚠️ history_metadata: 단독 접근 시 간헐 차단 → .history()를 먼저 부르면 그 응답에서 안정적으로 채워짐
  ❌ 차단: .info / get_info / funds_data (quoteSummary 계열, YFRateLimitError)
설계:
  - 종목명/타입 = .history() 선호출 후 history_metadata 에서
  - 가격/시총/52주/MA = fast_info (+history 폴백)
  - 개별주 펀더멘털 = 재무제표 직접 계산 (.info 원값과 대조 검증 완료)
  - ETF 운용보수/카테고리 = funds_data 시도 → 막히면 ETF_OVERRIDES(정적 맵)에서 보충

단위: operatingMargin/roe/revenueGrowth = 소수 / dividendYield/debtToEquity/expenseRatio = 퍼센트값
"""

import re
import numpy as np
import pandas as pd
from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf

app = Flask(__name__)
CORS(app)

# funds_data가 Render에서 차단되므로, 보유 ETF의 정적 정보를 직접 보관(운용보수는 거의 안 변함).
# 새 ETF 보유 시 여기에 한 줄 추가. AUM은 변동성이 커서 생략(필요 시 별도 소스).
ETF_OVERRIDES = {
    "SOXL": {"expenseRatio": 0.75, "category": "Trading--Leveraged Equity", "isLeveraged": True},
    "SOXX": {"expenseRatio": 0.35, "category": "Technology", "isLeveraged": False},
    "SMH":  {"expenseRatio": 0.35, "category": "Technology", "isLeveraged": False},
}


def num(v):
    if v is None:
        return None
    try:
        f = float(v)
        return None if np.isnan(f) else f
    except Exception:
        return None


def L(df, *labels, col=0):
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


def beta_from(stock_close, spy_close):
    try:
        s = stock_close.pct_change().dropna()
        m = spy_close.pct_change().dropna()
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
        if dv is None or len(dv) == 0:
            return 0.0
        if not price:
            return None
        cutoff = dv.index.max() - pd.Timedelta(days=365)
        return round(float(dv[dv.index >= cutoff].sum()) / price * 100, 4)
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

        # 1) history 먼저 (chart 엔드포인트=안정) → history_metadata 채워짐 + beta에 재사용
        hist = None
        try:
            hist = t.history(period="1y", auto_adjust=True)
        except Exception:
            hist = None

        meta = {}
        try:
            meta = t.history_metadata or {}
        except Exception:
            meta = {}
        name = meta.get("longName") or meta.get("shortName") or symbol
        qtype = (meta.get("instrumentType") or "").upper()

        # 2) fast_info → 가격/시총/52주/MA (+history 폴백)
        fi = t.fast_info
        price = safe_attr(fi, "last_price") or num(meta.get("regularMarketPrice"))
        market_cap = safe_attr(fi, "market_cap")
        currency = safe_attr(fi, "currency") or meta.get("currency")
        week52_high = safe_attr(fi, "year_high") or num(meta.get("fiftyTwoWeekHigh"))
        week52_low = safe_attr(fi, "year_low") or num(meta.get("fiftyTwoWeekLow"))
        ma50 = safe_attr(fi, "fifty_day_average")
        ma200 = safe_attr(fi, "two_hundred_day_average")
        if hist is not None and not hist.empty:
            c = hist["Close"]
            if week52_high is None: week52_high = float(c.max())
            if week52_low is None: week52_low = float(c.min())
            if ma50 is None and len(c) >= 50: ma50 = float(c.tail(50).mean())
            if ma200 is None and len(c) >= 200: ma200 = float(c.tail(200).mean())
            if price is None: price = float(c.iloc[-1])

        out = {
            "ok": True, "ticker": symbol, "type": qtype or "UNKNOWN", "name": name,
            "price": price, "currency": currency, "marketCap": market_cap,
            "week52High": week52_high, "week52Low": week52_low, "ma50": ma50, "ma200": ma200,
            "dividendYield": dividend_yield_ttm(t, price),
            "trailingPE": None, "trailingEps": None, "roe": None, "debtToEquity": None,
            "operatingMargin": None, "revenueGrowth": None, "beta": None,
            "expenseRatio": None, "aum": None, "category": None, "isLeveraged": None,
            "source": "yfinance(statements)",
        }
        diag = {}

        if qtype == "ETF":
            cat = None
            er = None
            try:
                fd = t.funds_data
                ov = getattr(fd, "fund_overview", None) or {}
                cat = ov.get("categoryName")
                fo = getattr(fd, "fund_operations", None)
                if fo is not None and not fo.empty and "Annual Report Expense Ratio" in fo.index:
                    v = num(fo.loc["Annual Report Expense Ratio"].iloc[0])
                    er = round(v * 100, 4) if v is not None else None
                diag["fundsData"] = "ok"
            except Exception as e:
                diag["fundsData"] = f"ERR {type(e).__name__}"

            ov_map = ETF_OVERRIDES.get(symbol, {})
            out["expenseRatio"] = er if er is not None else ov_map.get("expenseRatio")
            out["category"] = cat or ov_map.get("category")
            lname = (name or "").lower()
            inferred = ("leverage" in (out["category"] or "").lower()) or bool(re.search(r"\b[23]x\b", lname)) or ("bull" in lname or "bear" in lname)
            out["isLeveraged"] = ov_map.get("isLeveraged", inferred)
            if symbol in ETF_OVERRIDES and diag.get("fundsData", "").startswith("ERR"):
                diag["etfSource"] = "override"

        else:
            inc = t.income_stmt
            qi = t.quarterly_income_stmt
            qbs = t.quarterly_balance_sheet
            bs = t.balance_sheet
            diag = {"incOk": not getattr(inc, "empty", True),
                    "qiOk": not getattr(qi, "empty", True),
                    "qbsOk": not getattr(qbs, "empty", True)}

            rev_a = L(inc, "Total Revenue", "Operating Revenue")
            opinc_a = L(inc, "Operating Income", "Total Operating Income As Reported")
            out["operatingMargin"] = round(opinc_a / rev_a, 5) if (opinc_a and rev_a) else None

            qrev0 = L(qi, "Total Revenue", "Operating Revenue", col=0)
            qrev4 = L(qi, "Total Revenue", "Operating Revenue", col=4)
            out["revenueGrowth"] = round((qrev0 - qrev4) / qrev4, 5) if (qrev0 and qrev4) else None

            eps_ttm = ttm(qi, "Diluted EPS", "Basic EPS") or L(inc, "Diluted EPS", "Basic EPS")
            out["trailingEps"] = round(eps_ttm, 2) if eps_ttm else None
            out["trailingPE"] = round(price / eps_ttm, 2) if (price and eps_ttm and eps_ttm > 0) else None

            ni_ttm = ttm(qi, "Net Income", "Net Income Common Stockholders")
            equity = L(qbs, "Stockholders Equity", "Common Stock Equity") or L(bs, "Stockholders Equity", "Common Stock Equity")
            out["roe"] = round(ni_ttm / equity, 4) if (ni_ttm and equity) else None

            debt = L(qbs, "Total Debt") or L(bs, "Total Debt")
            out["debtToEquity"] = round(debt / equity * 100, 2) if (debt and equity) else None

            if hist is not None and not hist.empty:
                try:
                    spy = yf.Ticker("SPY").history(period="1y", auto_adjust=True)["Close"]
                    b = beta_from(hist["Close"], spy)
                    out["beta"] = round(b, 3) if b else None
                except Exception:
                    out["beta"] = None

        if price is None and market_cap is None and out["operatingMargin"] is None and out["expenseRatio"] is None:
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
