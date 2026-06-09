"""
소수몽키 백엔드 — Day 2
목표: yfinance가 Render 서버에서 실제로 도는지 증명.
엔드포인트:
  GET /                  → {"status": "ok"}            (Day 1 헬스체크, 유지)
  GET /quote?ticker=AAPL → 현재가 / 시총 / PER JSON     (Day 2 신규)

Day 2의 핵심: 데이터가 나오면 성공(아키텍처 전체 생존),
실패하면 error 메시지를 그대로 노출해서 "차단인지/다른 문제인지" 눈으로 확인.
"""

from flask import Flask, jsonify, request
from flask_cors import CORS
import yfinance as yf

app = Flask(__name__)
CORS(app)  # 개인용: 모든 출처 허용 (나중에 PWA에서 fetch 대비)


@app.route("/")
def health():
    return jsonify({"status": "ok"})


@app.route("/quote")
def quote():
    ticker_symbol = (request.args.get("ticker") or "").strip().upper()
    if not ticker_symbol:
        return jsonify({
            "ok": False,
            "error": "ticker 파라미터가 필요합니다. 예: /quote?ticker=AAPL",
        }), 400

    try:
        t = yf.Ticker(ticker_symbol)

        # 1) 빠르고 비교적 안정적인 fast_info 먼저 (현재가/시총/통화)
        price = market_cap = currency = None
        try:
            fi = t.fast_info
            price = getattr(fi, "last_price", None)
            market_cap = getattr(fi, "market_cap", None)
            currency = getattr(fi, "currency", None)
        except Exception:
            pass  # fast_info 실패 시 아래 info로 폴백

        # 2) info 에서 PER / 종목명 / 보조값 (조금 무겁고 가끔 흔들림)
        try:
            info = t.info or {}
        except Exception:
            info = {}

        if price is None:
            price = info.get("currentPrice") or info.get("regularMarketPrice")
        if market_cap is None:
            market_cap = info.get("marketCap")
        if currency is None:
            currency = info.get("currency")

        per = info.get("trailingPE")
        name = info.get("longName") or info.get("shortName") or ticker_symbol

        # 핵심값이 전부 비면 = 차단/실패로 판단 (Day 2 진단 포인트)
        if price is None and market_cap is None and per is None:
            return jsonify({
                "ok": False,
                "ticker": ticker_symbol,
                "error": ("yfinance가 데이터를 반환하지 않음. "
                          "Yahoo가 Render IP를 차단했을 가능성 → Render 로그 확인 후 대안(FMP) 검토."),
            }), 502

        return jsonify({
            "ok": True,
            "ticker": ticker_symbol,
            "name": name,
            "price": price,
            "marketCap": market_cap,
            "trailingPE": per,
            "currency": currency,
            "source": "yfinance",
        })

    except Exception as e:
        # Day 2 진단용: 예외 타입+메시지를 그대로 노출
        return jsonify({
            "ok": False,
            "ticker": ticker_symbol,
            "error": f"{type(e).__name__}: {e}",
        }), 502


if __name__ == "__main__":
    # 로컬 실행용 (Render에서는 gunicorn app:app 으로 구동)
    app.run(host="0.0.0.0", port=5000)
