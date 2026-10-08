# -*- coding: utf-8 -*-
"""Daily US market snapshot (runs on GitHub Actions).

Writes data/latest.json and data/YYYY-MM-DD.json with public end-of-day market data:
major indices, FX, commodities, Treasury yields (U.S. Treasury daily par yield curve),
sector ETFs, and every S&P 500 constituent's daily change / market cap / sector.
"""
import io, json, os, sys, datetime as dt, time, traceback
from concurrent.futures import ThreadPoolExecutor

import pandas as pd
import requests
import yfinance as yf

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "data")
os.makedirs(OUT, exist_ok=True)
UA = {"User-Agent": "Mozilla/5.0 (market snapshot bot)"}

INDICES = {
    "^GSPC": "S&P500", "^IXIC": "ナスダック総合", "^DJI": "ダウ平均", "^RUT": "ラッセル2000",
    "^SOX": "フィラデルフィア半導体指数", "^VIX": "VIX（恐怖指数）", "^N225": "日経平均",
    "JPY=X": "ドル円", "CL=F": "WTI原油", "BZ=F": "ブレント原油", "GC=F": "金", "BTC-USD": "ビットコイン",
    "ES=F": "S&P500先物", "NQ=F": "ナスダック100先物",
}
SECTORS = {
    "XLK": "情報技術", "XLC": "通信サービス", "XLY": "一般消費財", "XLP": "生活必需品", "XLE": "エネルギー",
    "XLF": "金融", "XLV": "ヘルスケア", "XLI": "資本財", "XLB": "素材", "XLRE": "不動産", "XLU": "公益",
}
GICS_JA = {
    "Information Technology": "情報技術", "Communication Services": "通信サービス",
    "Consumer Discretionary": "一般消費財", "Consumer Staples": "生活必需品", "Energy": "エネルギー",
    "Financials": "金融", "Health Care": "ヘルスケア", "Industrials": "資本財", "Materials": "素材",
    "Real Estate": "不動産", "Utilities": "公益",
}
WATCH = ["NVDA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "MU", "AMD", "INTC", "QCOM", "TSM", "ASML",
         "ARM", "SMCI", "PLTR", "SNOW", "CRM", "ADBE", "NOW", "ORCL", "IBM", "NFLX", "DIS", "UBER", "ABNB", "SHOP",
         "COIN", "HOOD", "SOFI", "PYPL", "SQ", "MSTR", "IONQ", "RGTI", "IREN", "NBIS", "CRWV", "RKLB", "JOBY", "ACHR",
         "BRK-B", "JPM", "GS", "BAC", "V", "MA", "LLY", "NVO", "UNH", "WMT", "COST", "KO", "PG", "XOM", "CVX",
         "SPY", "VOO", "QQQ", "VTI", "VT", "SCHD", "VYM", "SOXX", "GLD", "TLT", "IBIT"]


def us_session_open_today():
    """米国の取引時間中（＝今日の日足がまだ確定していない）なら、その日付を返す"""
    from zoneinfo import ZoneInfo
    now = dt.datetime.now(ZoneInfo("America/New_York"))
    if now.weekday() < 5 and dt.time(9, 25) <= now.time() < dt.time(16, 20):
        return now.date()
    return None


PARTIAL = us_session_open_today()


def finalize(c):
    """取引時間中に取得した場合、未確定の当日分を除く"""
    c = c.dropna()
    if PARTIAL is not None and len(c) and c.index[-1].date() == PARTIAL:
        c = c.iloc[:-1]
    return c


def series_of(df_close, n=None):
    s = df_close.dropna()
    if n:
        s = s.iloc[-n:]
    return [[d.strftime("%Y-%m-%d"), round(float(v), 4)] for d, v in s.items()]


def quote_block(sym, name, hist):
    c = finalize(hist["Close"]) if not sym.endswith("=X") and sym not in ("BTC-USD",) and not sym.endswith("=F") else hist["Close"].dropna()
    if sym == "^N225":
        c = hist["Close"].dropna()
    if len(c) < 2:
        return None
    last, prev = float(c.iloc[-1]), float(c.iloc[-2])
    return {
        "symbol": sym, "name": name, "date": c.index[-1].strftime("%Y-%m-%d"),
        "close": round(last, 4), "prev_close": round(prev, 4),
        "chg": round(last - prev, 4), "chg_pct": round((last / prev - 1) * 100, 3),
        "high_52w": round(float(c.iloc[-252:].max()), 4), "low_52w": round(float(c.iloc[-252:].min()), 4),
        "ytd_pct": ytd_pct(c),
        "series": series_of(c, 400),
    }


def ytd_pct(c):
    y = c.index[-1].year
    prev_year = c[c.index.year < y]
    if prev_year.empty:
        return None
    return round((float(c.iloc[-1]) / float(prev_year.iloc[-1]) - 1) * 100, 3)


def fetch_many(symbols, period="2y"):
    df = yf.download(list(symbols), period=period, interval="1d", auto_adjust=False,
                     group_by="ticker", threads=True, progress=False)
    out = {}
    for s in symbols:
        try:
            h = df[s] if isinstance(df.columns, pd.MultiIndex) else df
            out[s] = h.dropna(how="all")
        except Exception:
            pass
    return out


def treasury_yields():
    """米財務省 公式 日次イールドカーブ（今年＋昨年）"""
    rows = []
    year = dt.date.today().year
    for y in (year - 1, year):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{y}/all?type=daily_treasury_yield_curve&field_tdr_date_value={y}&page&_format=csv")
        r = requests.get(url, headers=UA, timeout=30)
        r.raise_for_status()
        rows.append(pd.read_csv(io.StringIO(r.text)))
    df = pd.concat(rows)
    df["Date"] = pd.to_datetime(df["Date"])
    df = df.sort_values("Date").set_index("Date")
    res = {}
    for col, key in (("2 Yr", "US2Y"), ("10 Yr", "US10Y"), ("30 Yr", "US30Y"), ("3 Mo", "US3M")):
        s = df[col].dropna()
        res[key] = {
            "name": {"US2Y": "米2年債利回り", "US10Y": "米10年債利回り", "US30Y": "米30年債利回り", "US3M": "米3か月債利回り"}[key],
            "date": s.index[-1].strftime("%Y-%m-%d"), "close": float(s.iloc[-1]), "prev_close": float(s.iloc[-2]),
            "chg_bp": round((float(s.iloc[-1]) - float(s.iloc[-2])) * 100, 1),
            "series": series_of(s, 500), "source": "U.S. Treasury Daily Treasury Par Yield Curve Rates",
        }
    return res


def sp500_constituents():
    r = requests.get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies", headers=UA, timeout=30)
    r.raise_for_status()
    t = pd.read_html(io.StringIO(r.text))[0]
    t["Symbol"] = t["Symbol"].str.replace(".", "-", regex=False)
    return [(s, n, GICS_JA.get(g, g)) for s, n, g in zip(t["Symbol"], t["Security"], t["GICS Sector"])]


def sp500_board():
    cons = sp500_constituents()
    syms = [c[0] for c in cons]
    hist = yf.download(syms, period="10d", interval="1d", auto_adjust=False, group_by="ticker",
                       threads=True, progress=False)

    def shares(s):
        for _ in range(2):
            try:
                return s, float(yf.Ticker(s).fast_info["shares"] or 0)
            except Exception:
                time.sleep(1)
        return s, 0.0

    with ThreadPoolExecutor(16) as ex:
        sh = dict(ex.map(shares, syms))
    board = []
    for sym, name, sector in cons:
        try:
            c = finalize(hist[sym]["Close"])
            if len(c) < 2:
                continue
            last, prev = float(c.iloc[-1]), float(c.iloc[-2])
            board.append({"symbol": sym, "name": name, "sector": sector, "close": round(last, 3),
                          "chg_pct": round((last / prev - 1) * 100, 3),
                          "mcap": round(last * sh.get(sym, 0.0)), "date": c.index[-1].strftime("%Y-%m-%d")})
        except Exception:
            continue
    return board


def main():
    data = {"generated_at_utc": dt.datetime.utcnow().replace(microsecond=0).isoformat() + "Z", "errors": []}

    try:
        h = fetch_many(list(INDICES) + list(SECTORS) + WATCH, period="2y")
        data["indices"] = {s: quote_block(s, n, h[s]) for s, n in INDICES.items() if s in h}
        data["sectors"] = {s: dict(quote_block(s, n, h[s]) or {}, series=None) for s, n in SECTORS.items() if s in h}
        data["watchlist"] = {s: quote_block(s, s, h[s]) for s in WATCH if s in h}
        # 円建てS&P500
        sp = finalize(h["^GSPC"]["Close"]); fx = h["JPY=X"]["Close"].dropna()
        j = (sp * fx.reindex(sp.index, method="ffill")).dropna()
        data["sp500_yen"] = {"name": "円建てS&P500（S&P500×ドル円）", "close": round(float(j.iloc[-1]), 1),
                             "chg_pct": round((float(j.iloc[-1]) / float(j.iloc[-2]) - 1) * 100, 3),
                             "ytd_pct": ytd_pct(j), "series": series_of(j, 400)}
    except Exception as e:
        data["errors"].append("prices: " + repr(e)); traceback.print_exc()

    try:
        data["treasury"] = treasury_yields()
    except Exception as e:
        data["errors"].append("treasury: " + repr(e)); traceback.print_exc()

    try:
        board = sp500_board()
        data["sp500_board"] = board
        up = sum(1 for b in board if b["chg_pct"] > 0); dn = sum(1 for b in board if b["chg_pct"] < 0)
        srt = sorted(board, key=lambda b: b["chg_pct"])
        data["breadth"] = {"advancers": up, "decliners": dn, "unchanged": len(board) - up - dn, "count": len(board)}
        data["top_gainers"] = srt[::-1][:15]
        data["top_losers"] = srt[:15]
        big = sorted(board, key=lambda b: -b["mcap"])[:30]
        data["megacaps"] = big
        # 時価総額加重でない「平均的な銘柄」の動き
        data["median_chg_pct"] = round(float(pd.Series([b["chg_pct"] for b in board]).median()), 3)
    except Exception as e:
        data["errors"].append("sp500_board: " + repr(e)); traceback.print_exc()

    try:
        mdate = data["indices"]["^GSPC"]["date"]
    except Exception:
        mdate = dt.date.today().isoformat()
    data["market_date"] = mdate
    for name in ("latest.json", f"{mdate}.json"):
        with open(os.path.join(OUT, name), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    print("market_date", mdate, "errors", data["errors"])
    if "indices" not in data:
        sys.exit(1)


if __name__ == "__main__":
    main()
