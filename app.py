"""
소수몽키 백엔드 — Day 3
목표: /quote 확장 — 베이직 체크리스트가 필요로 하는 핵심 지표를 한 번에.
  - 공통: 현재가, 시총, 통화, 52주 고저, MA50, MA200
  - 개별주(EQUITY): PER, EPS, ROE, 부채비율, 영업이익률, 매출성장률, 베타, 배당수익률
  - ETF: 운용보수(expenseRatio), AUM, category, 레버리지 여부  (베이직 Ch.4 별도 체크리스트)

Day 2 발견 대응: Render IP에서 .info가 빈 응답으로 오는 일시적 실패가 있었음.
  -> .info를 '새 Ticker로 재시도'(캐시 우회)하고, 안정적인 fast_info로 52주 고저 등을 교차 보강.
  -> 응답 끝의 _diag 로 .info가 실제로 됐는지/몇 번 만에 됐는지 눈으로 확인 가능.

엔드포인트:
  GET /                  -> {"status": "ok"}
  GET /quote?ticker=AAPL -> 위 필드 전부 JSON

단위 메모(yfinance 원값 그대로 반환):
  - roe / operatingMargin / revenueGrowth = 소수(0.12 = 12%)
  - dividendYield / debtToEquity / expenseRatio = 퍼센트값(2.28 = 2.28%)
"""

import re
import time
from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf

app = Flask(__name__)
CORS(app)


def fetch_info(symbol, attempts=3, delay=0.8):
    """.info를 새 Ticker로 재시도(캐시 우회). (info_dict, 성공한_시도횟수) 반환."""
    last = {}
    for i in range(1, attempts + 1):
        try:
            info = yf.Ticker(symbol).info or {}
            if len(info) > 10:
                return info, i
            last = info
        except Exception:
            pass
        if i < attempts:
            time.sleep(delay)
    return last, attempts


def safe(obj, name):
    try:
        return getattr(obj, name, None)
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

        fi = t.fast_info
        price = safe(fi, "last_price")
        market_cap = safe(fi, "market_cap")
        currency = safe(fi, "currency")
        week52_high = safe(fi, "year_high")
        week52_low = safe(fi, "year_low")
        ma50 = safe(fi, "fifty_day_average")
        ma200 = safe(fi, "two_hundred_day_average")

        info, info_attempts = fetch_info(symbol)
        info_ok = len(info) > 10

        if price is None:
            price = info.get("currentPrice") or info.get("regularMarketPrice")
        if market_cap is None:
            market_cap = info.get("marketCap")
        if currency is None:
            currency = info.get("currency")
        if week52_high is None:
            week52_high = info.get("fiftyTwoWeekHigh")
        if week52_low is None:
            week52_low = info.get("fiftyTwoWeekLow")

        quote_type = (info.get("quoteType") or "").upper()
        name = info.get("longName") or info.get("shortName") or symbol

        category = info.get("category")
        is_leveraged = None
        if quote_type == "ETF":
            cat = (category or "").lower()
            is_leveraged = ("leverage" in cat) or bool(re.search(r"\b[23]x\b", name.lower()))

        if price is None and market_cap is None and not info_ok:
            return jsonify({
                "ok": False, "ticker": symbol,
                "error": "yfinance가 데이터를 반환하지 않음(차단 의심). Render 로그 확인 -> 대안(FMP) 검토.",
                "_diag": {"infoKeys": len(info), "infoAttempts": info_attempts, "infoOk": info_ok},
            }), 502

        return jsonify({
            "ok": True,
            "ticker": symbol,
            "type": quote_type or "UNKNOWN",
            "name": name,
            "price": price,
            "currency": currency,
            "marketCap": market_cap,
            "week52High": week52_high,
            "week52Low": week52_low,
            "ma50": ma50,
            "ma200": ma200,
            "dividendYield": info.get("dividendYield"),
            "trailingPE": info.get("trailingPE"),
            "trailingEps": info.get("trailingEps"),
            "roe": info.get("returnOnEquity"),
            "debtToEquity": info.get("debtToEquity"),
            "operatingMargin": info.get("operatingMargins"),
            "revenueGrowth": info.get("revenueGrowth"),
            "beta": info.get("beta"),
            "expenseRatio": info.get("netExpenseRatio") or info.get("annualReportExpenseRatio"),
            "aum": info.get("totalAssets"),
            "category": category,
            "isLeveraged": is_leveraged,
            "source": "yfinance",
            "_diag": {"infoKeys": len(info), "infoAttempts": info_attempts, "infoOk": info_ok},
        })

    except Exception as e:
        return jsonify({"ok": False, "ticker": symbol, "error": f"{type(e).__name__}: {e}"}), 502


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
