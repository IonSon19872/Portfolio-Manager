"""
shared.py -- All data via yfinance (free, no API key needed)
Supports all exchanges: US, XETRA (.DE), Euronext (.PA), LSE (.L), AMS (.AS), STO (.ST)
"""

import os, json, time, smtplib, logging
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import yfinance as yf

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s %(message)s",
    datefmt="%H:%M:%S"
)
log = logging.getLogger("portfolio")

# -- PATHS --------------------------------------------------------------------
ROOT       = Path(__file__).parent.parent
CONFIG_F   = ROOT / "portfolio_config.json"
DATA_DIR   = ROOT / "docs" / "data"
ALERTS_F   = DATA_DIR / "alerts.json"
SNAPSHOT_F = DATA_DIR / "snapshot.json"
INTEL_F    = DATA_DIR / "intelligence.json"
RATINGS_F  = DATA_DIR / "ratings_history.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)

# -- CONFIG -------------------------------------------------------------------
DEFAULT_CONFIG = {
    "portfolio": {"stocks": [], "etfs": []},
    "alerts": {
        "movement_threshold_pct": 3.0,
        "email_enabled": True,
        "email_from": "",
        "email_password": "",
        "email_to": "",
    },
    "last_prices": {}
}


def load_config() -> dict:
    if CONFIG_F.exists():
        cfg = json.loads(CONFIG_F.read_text())
        for k, v in DEFAULT_CONFIG.items():
            cfg.setdefault(k, v)
            if isinstance(v, dict):
                for kk, vv in v.items():
                    cfg[k].setdefault(kk, vv)
        env_map = {
            "EMAIL_FROM":     ("alerts", "email_from"),
            "EMAIL_PASSWORD": ("alerts", "email_password"),
            "EMAIL_TO":       ("alerts", "email_to"),
        }
        for env_var, (section, key) in env_map.items():
            if os.environ.get(env_var):
                cfg[section][key] = os.environ[env_var]
        return cfg
    cfg = DEFAULT_CONFIG.copy()
    save_config(cfg)
    return cfg


def save_config(cfg: dict):
    safe = json.loads(json.dumps(cfg))
    if os.environ.get("GITHUB_ACTIONS"):
        safe["alerts"]["email_password"] = ""
    # preserve morning_prices and last_prices — never wipe them
    CONFIG_F.write_text(json.dumps(safe, indent=2))


def load_json(path: Path, default):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return default


def save_json(path: Path, data):
    path.write_text(json.dumps(data, indent=2, default=str))


# -- FX to EUR ----------------------------------------------------------------
_fx_cache: dict = {}


def _get_fx_rate(currency: str) -> float:
    ccy = (currency or "USD").upper().strip()
    if ccy == "EUR":
        return 1.0
    if ccy == "GBX":
        rate = _get_fx_rate("GBP") / 100.0
        _fx_cache["GBX"] = rate
        return rate
    if ccy in _fx_cache:
        return _fx_cache[ccy]
    try:
        pair = "GBPEUR=X" if ccy == "GBP" else ccy + "EUR=X"
        t    = yf.Ticker(pair)
        fi   = t.fast_info
        rate = float(fi.last_price or fi.previous_close or 1.0)
        _fx_cache[ccy] = rate
        return rate
    except Exception:
        fallbacks = {
            "USD": 0.925, "GBP": 1.17, "CHF": 1.06, "SEK": 0.087,
            "NOK": 0.086, "DKK": 0.134, "JPY": 0.006, "CAD": 0.683,
        }
        return fallbacks.get(ccy, 1.0)


def to_eur(price, currency: str) -> float:
    if price is None:
        return 0.0
    return float(price) * _get_fx_rate(currency)


# -- STOCK DATA ---------------------------------------------------------------
def get_stock_data(holding: dict, _ignored: str = "") -> dict:
    ticker = holding["ticker"]
    log.info("  " + ticker)

    out = {
        "ticker": ticker,
        "name":   holding.get("name", ticker),
    }

    try:
        t     = yf.Ticker(ticker)
        fi    = t.fast_info
        price = float(fi.last_price or fi.previous_close or 0)
        prev  = float(fi.previous_close or fi.last_price or 0)

        if not price and not prev:
            out["error"] = "No price data from yfinance for " + ticker
            return out

        currency = str(fi.currency or "USD").upper()
        if currency == "GBP" and price > 500:
            currency = "GBX"

        chg_pct   = ((price - prev) / prev * 100) if prev else 0.0
        price_eur = to_eur(price, currency)
        prev_eur  = to_eur(prev,  currency)

        out.update({
            "price_native": round(price, 4),
            "prev_close":   round(prev,  4),
            "currency":     currency,
            "change_pct":   round(chg_pct, 2),
            "price_eur":    round(price_eur, 2),
            "prev_eur":     round(prev_eur,  2),
        })

        try:
            info = t.info
            out["name"]           = info.get("shortName") or info.get("longName") or out["name"]
            out["sector"]         = info.get("sector", "")
            out["country"]        = info.get("country", "")
            out["pe_ratio"]       = info.get("trailingPE") or info.get("forwardPE")
            out["beta"]           = info.get("beta")
            out["eps_ttm"]        = info.get("trailingEps")
            out["dividend_yield"] = info.get("dividendYield")
            out["market_cap"]     = info.get("marketCap")

            rec_key = info.get("recommendationKey", "")
            if rec_key:
                rec_map = {
                    "strong_buy": "buy", "buy": "buy",
                    "hold": "hold", "underperform": "sell",
                    "sell": "sell", "strong_sell": "sell"
                }
                out["recommendation"] = rec_map.get(rec_key.lower(), "hold")

            out["analyst_total"] = info.get("numberOfAnalystOpinions", 0) or 0

            target_mean = info.get("targetMeanPrice")
            target_high = info.get("targetHighPrice")
            target_low  = info.get("targetLowPrice")
            if target_mean:
                out["analyst_target_mean"] = round(to_eur(target_mean, currency), 2)
                out["analyst_target_high"] = round(to_eur(target_high, currency), 2) if target_high else None
                out["analyst_target_low"]  = round(to_eur(target_low,  currency), 2) if target_low  else None

            w52_high = info.get("fiftyTwoWeekHigh")
            w52_low  = info.get("fiftyTwoWeekLow")
            if w52_high:
                out["52w_high"] = round(to_eur(w52_high, currency), 2)
                out["52w_low"]  = round(to_eur(w52_low,  currency), 2)

        except Exception as e:
            log.warning("  info fetch failed for " + ticker + ": " + str(e))

        out["timestamp"] = datetime.utcnow().isoformat()
       # Attach Morningstar data if pre-fetched (passed in via holding dict)
        if holding.get("star_rating"):
            out["star_rating"]    = holding["star_rating"]
        if holding.get("analyst_rating"):
            out["analyst_rating"] = holding["analyst_rating"]

    except Exception as e:
        out["error"] = "yfinance error for " + ticker + ": " + str(e)

    return out


# -- ANALYST UPGRADES ---------------------------------------------------------
def get_analyst_upgrades(ticker: str, _ignored: str = "", days_back: int = 7) -> list:
    is_european = any(ticker.endswith(x) for x in
                      [".DE", ".PA", ".L", ".AS", ".ST", ".MI", ".BR",
                       ".CO", ".HE", ".OL", ".VI", ".SW", ".MC"])
    if is_european:
        return []

    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    try:
        t  = yf.Ticker(ticker)
        df = t.upgrades_downgrades
        if df is None or df.empty:
            return []
        results = []
        for idx, row in df.iterrows():
            d = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
            if d < cutoff:
                continue
            fg = str(row.get("FromGrade", "") or "")
            tg = str(row.get("ToGrade",   "") or "")
            action = (
                "up"   if tg.lower() in ["buy", "outperform", "overweight", "strong buy"]
                else "down" if tg.lower() in ["sell", "underperform", "underweight"]
                else "reit"
            )
            results.append({
                "date":       d,
                "firm":       str(row.get("Firm", "") or ""),
                "from_grade": fg,
                "to_grade":   tg,
                "action":     action,
            })
        return results[:20]
    except Exception as e:
        log.warning("  upgrades fetch failed for " + ticker + ": " + str(e))
        return []

def get_morningstar_data(ticker: str, isin: str) -> dict:
    """
    Fetch Morningstar star rating and analyst rating via mstarpy.
    Uses ISIN for reliable lookup across all exchanges.
    Returns dict with star_rating (1-5) and analyst_rating (Gold/Silver/etc)
    """
    if not isin:
        return {}

    try:
        import mstarpy

        # Determine country code from ISIN prefix
        isin_upper  = isin.upper()
        country_map = {
            "US": "us", "DE": "de", "FR": "fr", "GB": "gb",
            "NL": "nl", "SE": "se", "IE": "ie", "CH": "ch",
            "IT": "it", "ES": "es", "BE": "be", "DK": "dk",
            "NO": "no", "FI": "fi", "AT": "at",
        }
        country_code = country_map.get(isin_upper[:2], "us")

        # Try as Fund first (ETFs), then as Stock
        ms_obj  = None
        is_fund = any(ticker.endswith(x) for x in
                      [".DE", ".PA", ".L", ".AS", ".ST", ".MI",
                       ".CO", ".HE", ".OL", ".VI", ".SW", ".MC", ".BR"])

        if is_fund:
            try:
                ms_obj = mstarpy.Funds(term=isin, country=country_code)
            except Exception:
                try:
                    ms_obj = mstarpy.Stock(term=isin, exchange=country_code)
                except Exception:
                    pass
        else:
            try:
                ms_obj = mstarpy.Stock(term=isin, exchange=country_code)
            except Exception:
                try:
                    ms_obj = mstarpy.Funds(term=isin, country=country_code)
                except Exception:
                    pass

        if ms_obj is None:
            return {}

        result = {}

        try:
            sr = ms_obj.starRating()
            if sr:
                result["star_rating"] = int(sr)
        except Exception:
            pass

        try:
            ar = ms_obj.analystRating()
            if ar:
                result["analyst_rating"] = str(ar)
        except Exception:
            pass

        if result:
            log.info("    Morningstar: stars=" + str(result.get("star_rating", "--")) +
                     " rating=" + str(result.get("analyst_rating", "--")))

        return result

    except ImportError:
        log.warning("  mstarpy not installed")
        return {}
    except Exception as e:
        log.warning("  Morningstar fetch failed for " + ticker + ": " + str(e))
        return {}
        
# -- COMPANY NEWS -------------------------------------------------------------
def get_company_news(ticker: str, _ignored: str = "",
                     days_back: int = 1, max_articles: int = 3,
                     holding_name: str = "") -> list:
    cutoff = (date.today() - timedelta(days=days_back)).isoformat()
    holding_name = holding_name or ticker.split(".")[0]

    import urllib.request
    import xml.etree.ElementTree as ET

    def _parse_rss(raw: bytes, default_source: str) -> list:
        try:
            root  = ET.fromstring(raw)
            items = root.findall(".//item")
        except Exception:
            return []
        results = []
        seen    = set()
        for item in items:
            title = (item.findtext("title") or "").strip()
            if not title or title in seen:
                continue
            seen.add(title)
            pub = item.findtext("pubDate") or ""
            try:
                dt = datetime.strptime(pub[:16].strip(), "%a, %d %b %Y")
                d  = dt.strftime("%Y-%m-%d")
            except Exception:
                d = ""
            if d and d < cutoff:
                continue
            results.append({
                "title":   title,
                "source":  item.findtext("source") or default_source,
                "url":     item.findtext("link") or "",
                "date":    d,
                "summary": "",
            })
        return results

    def _fetch(url: str, default_source: str) -> list:
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": "Mozilla/5.0"}
            )
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read()
            return _parse_rss(raw, default_source)
        except Exception as e:
            log.warning("  " + default_source + " failed for " + ticker + ": " + str(e))
            return []

    # Fetch from BOTH sources always, in parallel via threads
    import threading

    yahoo_results  = []
    google_results = []

    def _fetch_yahoo():
        url = (
            "https://feeds.finance.yahoo.com/rss/2.0/headline?s="
            + ticker + "&region=US&lang=en-US"
        )
        yahoo_results.extend(_fetch(url, "Yahoo Finance"))

    def _fetch_google():
        # Use full company name for much better results
        # e.g. "Siemens Energy stock" instead of "ENR stock"
        name  = holding_name.split(" ")[:3]  # first 3 words of company name
        query = urllib.request.quote(" ".join(name) + " stock")
        url   = (
            "https://news.google.com/rss/search?q="
            + query + "&hl=en-US&gl=US&ceid=US:en"
        )
        google_results.extend(_fetch(url, "Google News"))

    t1 = threading.Thread(target=_fetch_yahoo)
    t2 = threading.Thread(target=_fetch_google)
    t1.start()
    t2.start()
    t1.join(timeout=12)
    t2.join(timeout=12)

    # Merge both — deduplicate by title, interleave so both sources represented
    seen    = set()
    merged  = []

    # Interleave: take 1 from yahoo, 1 from google, 1 from yahoo etc
    # so final result always has mix of both sources when both return results
    yi = 0
    gi = 0
    while len(merged) < max_articles * 2:
        added = False
        if yi < len(yahoo_results):
            r = yahoo_results[yi]
            yi += 1
            if r["title"] not in seen:
                seen.add(r["title"])
                merged.append(r)
                added = True
        if gi < len(google_results):
            r = google_results[gi]
            gi += 1
            if r["title"] not in seen:
                seen.add(r["title"])
                merged.append(r)
                added = True
        if not added:
            break

    # Sort by date descending, cap at max_articles
    merged.sort(key=lambda x: x.get("date", ""), reverse=True)
    return merged[:max_articles]


# -- CALENDAR -----------------------------------------------------------------
def get_earnings_calendar(ticker: str, _ignored: str = "",
                           from_date: str = "", to_date: str = "") -> list:
    try:
        t   = yf.Ticker(ticker)
        cal = t.calendar
        if cal is None:
            return []
        ed = cal.get("Earnings Date")
        if not ed:
            return []
        d = str(ed[0].date()) if hasattr(ed[0], "date") else str(ed[0])[:10]
        if from_date <= d <= to_date:
            return [{
                "date":         d,
                "hour":         "",
                "eps_estimate": cal.get("EPS Estimate"),
                "eps_actual":   None,
                "revenue_est":  cal.get("Revenue Estimate"),
                "revenue_act":  None,
                "quarter":      None,
                "year":         None,
            }]
    except Exception:
        pass
    return []


def get_dividends(ticker: str, _ignored: str = "",
                  from_date: str = "", to_date: str = "") -> list:
    try:
        t  = yf.Ticker(ticker)
        df = t.dividends
        if df is None or df.empty:
            return []
        results = []
        for idx, val in df.items():
            d = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
            if from_date <= d <= to_date:
                results.append({
                    "ex_date":  d,
                    "pay_date": "",
                    "amount":   float(val),
                    "currency": "USD",
                    "freq":     "",
                })
        return results
    except Exception:
        return []


def get_stock_splits(ticker: str, _ignored: str = "",
                     from_date: str = "", to_date: str = "") -> list:
    try:
        t  = yf.Ticker(ticker)
        df = t.splits
        if df is None or df.empty:
            return []
        results = []
        for idx, val in df.items():
            d = str(idx.date()) if hasattr(idx, "date") else str(idx)[:10]
            if from_date <= d <= to_date:
                results.append({"date": d, "ratio": str(val) + ":1"})
        return results
    except Exception:
        return []


# -- kept for any legacy import -----------------------------------------------
def to_finnhub_symbol(ticker: str) -> str:
    return ticker


# -- EMAIL --------------------------------------------------------------------
def send_email(subject: str, html: str, cfg: dict) -> bool:
    a = cfg["alerts"]
    if not a.get("email_enabled") or not a.get("email_from") or not a.get("email_password"):
        log.info("  Email skipped (not configured)")
        return False
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = a["email_from"]
        msg["To"]      = a["email_to"]
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=20) as s:
            s.login(a["email_from"], a["email_password"])
            s.send_message(msg)
        log.info("  Sent: " + subject)
        return True
    except Exception as e:
        log.error("  Email failed: " + str(e))
        return False


# -- ALERT LOG ----------------------------------------------------------------
def append_alert(alert_type: str, ticker: str, message: str):
    alerts = load_json(ALERTS_F, [])
    alerts.insert(0, {
        "ts":      datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "type":    alert_type,
        "ticker":  ticker,
        "message": message,
    })
    save_json(ALERTS_F, alerts[:500])


# -- EMAIL HTML TEMPLATES -----------------------------------------------------
_BASE = (
    "font-family:'Arial',sans-serif;background:#161b22;"
    "color:#f0f2f5;padding:32px;max-width:680px;margin:auto;border-radius:12px"
)


def _TH(s):
    return (
        "<th style='padding:8px 12px;text-align:left;background:#87CEFB;"
        "color:#0a0a0a;font-size:10px;text-transform:uppercase;"
        "letter-spacing:1px'>" + s + "</th>"
    )


def _td(v, x=""):
    return (
        "<td style='padding:8px 12px;border-bottom:1px solid #21293a;"
        "background:#87CEFB;color:#0a0a0a;" + x + "'>"
        + str(v) + "</td>"
    )


def _holding_row(s: dict) -> str:
    chg          = s.get("change_pct") or 0
    price_native = s.get("price_native") or 0
    prev_close   = s.get("prev_close")   or 0
    market_closed = (abs(chg) < 0.001 and price_native > 0
                     and abs(price_native - prev_close) < 0.001)
    color = "#7d8fa8" if market_closed else ("#1a7a3a" if chg >= 0 else "#c0392b")
    arrow = "+" if chg >= 0 else "-"

    # Analyst cell
    rec      = (s.get("recommendation") or "").replace("_", " ")
    ms_stars = s.get("star_rating")
    ms_ar    = s.get("analyst_rating") or ""
    ar_color = (
        "#b8860b" if ms_ar == "Gold"
        else "#707070" if ms_ar == "Silver"
        else "#8B4513" if ms_ar == "Bronze"
        else "#c0392b" if ms_ar == "Negative"
        else "#555555"
    )

    if ms_stars:
        stars_str = ("★" * ms_stars) + ("☆" * (5 - ms_stars))
        if ms_ar:
            analyst_html = (
                "<span style='color:#b8860b'>" + stars_str + "</span>"
                " <span style='color:" + ar_color + ";font-size:10px'>" + ms_ar + "</span>"
            )
        else:
            analyst_html = "<span style='color:#b8860b'>" + stars_str + "</span>"
    elif rec:
        rc           = "#1a7a3a" if "buy" in rec else "#c0392b" if "sell" in rec else "#b8860b"
        analyst_html = (
            "<span style='color:" + rc + ";font-size:10px;text-transform:uppercase'>"
            + rec + "</span>"
        )
    else:
        analyst_html = "<span style='color:#555555'>--</span>"

    p_raw  = s.get("price_eur")
    v_raw  = s.get("value_eur")
    shares = s.get("shares") or 0
    p_str  = "{:.2f}".format(float(p_raw)) if p_raw not in (None, "", "--") else "--"
    v_str  = "{:.2f}".format(float(v_raw)) if v_raw not in (None, "", "--") else "--"
    if v_str == "--" and p_str != "--" and shares:
        v_str = "{:.2f}".format(float(p_str) * float(shares))

    closed_badge = (
        "<span style='color:#555555;font-size:9px'> mkt closed</span>"
        if market_closed else ""
    )
    chg_cell = (
        "<span style='color:" + color + ";font-weight:600'>"
        + arrow + " " + "{:.2f}".format(abs(chg)) + "%"
        + "</span>" + closed_badge
    )

    return (
        "<tr>"
        + _td(s.get("ticker", ""),        "color:#06402B;font-weight:700")
        + _td((s.get("name") or "")[:26], "color:#06402B;font-weight:600")
        + _td(("EUR " + p_str) if p_str != "--" else "--", "color:#0a0a0a")
        + _td(chg_cell)
        + _td(str(shares) if shares else "--", "color:#0a0a0a")
        + _td(("EUR " + v_str) if v_str != "--" else "--", "color:#0a0a0a;font-weight:700")
        + _td(analyst_html)
        + "</tr>"
    )

def _table(rows: str) -> str:
    heads = "".join(_TH(h) for h in
                    ["Ticker", "Name", "Price EUR", "Day Chg", "Shares", "Value EUR", "Analyst"])
    return (
        "<table style='width:100%;border-collapse:collapse;"
        "background:#87CEFB;border-radius:8px;overflow:hidden'>"
        "<thead><tr>" + heads + "</tr></thead><tbody>" + rows + "</tbody></table>"
    )


def digest_html(snapshot: dict, label: str) -> str:
    total    = snapshot.get("total_eur", 0)
    stk_rows = "".join(_holding_row(s) for s in snapshot.get("stocks", []) if "error" not in s)
    etf_rows = "".join(_holding_row(e) for e in snapshot.get("etfs",   []) if "error" not in e)
    now      = datetime.utcnow().strftime("%A, %d %B %Y - %H:%M UTC")
    return (
        "<div style='" + _BASE + "'>"
        "<div style='font-size:10px;color:#7d8fa8;text-transform:uppercase;"
        "letter-spacing:2px;margin-bottom:6px'>Portfolio Digest</div>"
        "<h1 style='font-size:20px;color:#4f9ef8;margin:0 0 4px'>Portfolio Digest - " + label + "</h1>"
        "<p style='color:#7d8fa8;margin:0 0 20px'>" + now + "</p>"
        "<p style='font-size:24px;color:#52d68a;margin:0 0 24px'>Total: <strong>EUR "
        + "{:,.2f}".format(total) + "</strong></p>"
        "<h2 style='font-size:14px;color:#f0f2f5;margin:0 0 10px'>Stocks</h2>"
        + _table(stk_rows)
        + "<h2 style='font-size:14px;color:#f0f2f5;margin:24px 0 10px'>ETFs</h2>"
        + _table(etf_rows)
        + "<p style='color:#4a5568;font-size:10px;margin-top:24px'>"
        "Portfolio Intelligence - GitHub Actions - Yahoo Finance</p>"
        "</div>"
    )


def movement_html(ticker: str, name: str, price_now: float,
                  price_prev: float, move_pct: float) -> str:
    color = "#52d68a" if move_pct > 0 else "#f56565"
    arrow = "UP" if move_pct > 0 else "DOWN"
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<div style='" + _BASE + "'>"
        "<h1 style='font-size:26px;color:" + color + ";margin:0 0 8px'>"
        + arrow + " " + ticker + "</h1>"
        "<p style='color:#7d8fa8;margin:0 0 20px'>" + name + "</p>"
        "<table style='width:100%;border-collapse:collapse;"
        "background:#1c2330;border-radius:8px;overflow:hidden'>"
        "<tr><td style='padding:12px 16px;color:#7d8fa8'>Previous close</td>"
        "<td style='padding:12px 16px;font-weight:600'>EUR "
        + "{:.2f}".format(price_prev) + "</td></tr>"
        "<tr><td style='padding:12px 16px;color:#7d8fa8'>Current price</td>"
        "<td style='padding:12px 16px;font-weight:600;color:" + color + "'>EUR "
        + "{:.2f}".format(price_now) + "</td></tr>"
        "<tr><td style='padding:12px 16px;color:#7d8fa8'>Change</td>"
        "<td style='padding:12px 16px;font-size:22px;font-weight:700;color:" + color + "'>"
        + arrow + " " + "{:.2f}".format(abs(move_pct)) + "%</td></tr>"
        "</table>"
        "<p style='color:#4a5568;font-size:10px;margin-top:20px'>" + now + "</p>"
        "</div>"
    )


def rating_change_html(ticker: str, name: str, changes: list) -> str:
    def _row(c):
        to_g   = c.get("to_grade", "")
        tl     = to_g.lower()
        col    = (
            "#52d68a" if any(w in tl for w in ["buy", "outperform", "overweight", "positive"])
            else "#f56565" if any(w in tl for w in ["sell", "underperform", "underweight", "negative"])
            else "#f6ad55"
        )
        action = c.get("action", "").lower()
        badge_map = {
            "up":   "<span style='color:#52d68a;font-size:10px'>UPGRADE</span>",
            "down": "<span style='color:#f56565;font-size:10px'>DOWNGRADE</span>",
            "init": "<span style='color:#4f9ef8;font-size:10px'>INITIATION</span>",
            "reit": "<span style='color:#7d8fa8;font-size:10px'>REITERATE</span>",
        }
        bd = "1px solid #21293a"
        return (
            "<tr>"
            "<td style='padding:9px 12px;border-bottom:" + bd + ";color:#7d8fa8'>"
            + c.get("date", "") + "</td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + ";font-weight:600'>"
            + c.get("firm", "") + "</td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + ";"
            "color:#7d8fa8;text-decoration:line-through'>"
            + (c.get("from_grade") or "--") + "</td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + "'>-></td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + ";color:" + col + ";"
            "font-weight:700'>" + to_g + "</td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + "'>"
            + badge_map.get(action, "") + "</td>"
            "</tr>"
        )

    rows  = "".join(_row(c) for c in changes)
    heads = "".join(_TH(h) for h in ["Date", "Firm", "From", "", "To", "Action"])
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<div style='" + _BASE + "'>"
        "<h1 style='font-size:24px;color:#b794f4;margin:0 0 6px'>" + ticker + "</h1>"
        "<p style='color:#7d8fa8;margin:0 0 24px'>" + name + "</p>"
        "<table style='width:100%;border-collapse:collapse;"
        "background:#1c2330;border-radius:8px;overflow:hidden'>"
        "<thead><tr>" + heads + "</tr></thead><tbody>" + rows + "</tbody></table>"
        "<p style='color:#4a5568;font-size:10px;margin-top:20px'>" + now + "</p>"
        "</div>"
    )


def news_digest_html(holdings_with_news: list, run_label: str) -> str:
    now = datetime.utcnow().strftime("%A, %d %B %Y - %H:%M UTC")

    def _section(h):
        articles = h.get("news", [])
        if not articles:
            return ""
        rows = ""
        for a in articles:
            src   = a.get("source", "")
            d     = a.get("date", "")
            summ  = (a.get("summary") or "")[:160]
            if summ and not summ.endswith((".", "...")):
                summ += "..."
            url   = a.get("url", "#")
            title = a.get("title", "")
            rows += (
                "<tr><td style='padding:12px 14px;border-bottom:1px solid #21293a;"
                "vertical-align:top'>"
                "<a href='" + url + "' style='color:#4f9ef8;text-decoration:none;"
                "font-weight:600;font-size:12.5px;display:block;margin-bottom:5px'>"
                + title + "</a>"
                "<span style='color:#52d68a;font-size:11px'>" + src + "</span>"
                "<span style='color:#7d8fa8;font-size:11px'>"
                + (" - " + d if d else "") + "</span>"
                + ("<div style='color:#7d8fa8;font-size:11px;margin-top:4px'>"
                   + summ + "</div>" if summ else "")
                + "</td></tr>"
            )
        return (
            "<div style='margin-bottom:20px'>"
            "<span style='color:#4f9ef8;font-weight:600'>" + h["ticker"] + "</span>"
            " <span style='color:#7d8fa8;font-size:11px'>" + h.get("name", "") + "</span>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden;margin-top:8px'>"
            "<tbody>" + rows + "</tbody></table></div>"
        )

    sections = "".join(_section(h) for h in holdings_with_news)
    total    = sum(len(h.get("news", [])) for h in holdings_with_news)
    return (
        "<div style='" + _BASE + "'>"
        "<h1 style='font-size:20px;color:#52d68a;margin:0 0 4px'>"
        "News Digest - " + run_label + "</h1>"
        "<p style='color:#7d8fa8;margin:0 0 6px'>" + now + "</p>"
        "<p style='color:#7d8fa8;font-size:11px;margin:0 0 24px'>"
        + str(total) + " article(s) across "
        + str(len(holdings_with_news)) + " holding(s)</p>"
        + sections
        + "<p style='color:#4a5568;font-size:10px;margin-top:16px'>"
        "Portfolio Intelligence - GitHub Actions</p>"
        "</div>"
    )


def saturday_summary_html(snapshot: dict, intel_data: dict,
                           week_movements: list, sentiments: list = []) -> str:
    now        = datetime.utcnow().strftime("%A, %d %B %Y - %H:%M UTC")
    total_eur  = snapshot.get("total_eur", 0)
    week_start = snapshot.get("week_start_eur")
    week_chg   = ((total_eur - week_start) / week_start * 100) if week_start else None
    chg_color  = "#52d68a" if (week_chg or 0) >= 0 else "#f56565"

    week_chg_html = ""
    if week_chg is not None:
        week_chg_html = (
            "<div><div style='font-size:10px;color:#7d8fa8;text-transform:uppercase;"
            "letter-spacing:1px;margin-bottom:6px'>Week Change</div>"
            "<div style='font-size:22px;color:" + chg_color + ";font-weight:700'>"
            + ("+" if week_chg >= 0 else "") + "{:.2f}".format(week_chg) + "%</div></div>"
        )

    week_block = (
        "<div style='background:#1c2330;border-radius:10px;padding:20px 24px;"
        "margin-bottom:24px;display:flex;justify-content:space-between;"
        "align-items:center;flex-wrap:wrap;gap:12px'>"
        "<div><div style='font-size:10px;color:#7d8fa8;text-transform:uppercase;"
        "letter-spacing:1px;margin-bottom:6px'>Portfolio Value</div>"
        "<div style='font-size:28px;color:#52d68a;font-weight:700'>EUR "
        + "{:,.2f}".format(total_eur) + "</div></div>"
        + week_chg_html + "</div>"
    )

    # -- Holdings table with week change --
    def _week_chg_for(ticker: str) -> float:
        for m in week_movements:
            if m.get("ticker") == ticker:
                return m.get("move_pct", 0)
        return 0.0

    def _holding_row_weekly(s: dict) -> str:
        ticker   = s.get("ticker", "")
        name     = (s.get("name") or "")[:26]
        p_raw    = s.get("price_eur")
        v_raw    = s.get("value_eur")
        shares   = s.get("shares") or 0
        p_str    = "{:.2f}".format(float(p_raw)) if p_raw not in (None, "", "--") else "--"
        v_str    = "{:.2f}".format(float(v_raw)) if v_raw not in (None, "", "--") else "--"
        if v_str == "--" and p_str != "--" and shares:
            v_str = "{:.2f}".format(float(p_str) * float(shares))

        wchg     = _week_chg_for(ticker)
        wcolor   = "#1a7a3a" if wchg >= 0 else "#c0392b"
        warrow   = "+" if wchg >= 0 else "-"
        wchg_cell = (
            "<span style='color:" + wcolor + ";font-weight:600'>"
            + warrow + " " + "{:.2f}".format(abs(wchg)) + "%"
            + "</span>"
        )

        rec      = (s.get("recommendation") or "").replace("_", " ")
        ms_stars = s.get("star_rating")
        ms_ar    = s.get("analyst_rating") or ""
        ar_color = (
            "#b8860b" if ms_ar == "Gold"
            else "#707070" if ms_ar == "Silver"
            else "#8B4513" if ms_ar == "Bronze"
            else "#c0392b" if ms_ar == "Negative"
            else "#555555"
        )
        if ms_stars:
            stars_str    = ("★" * ms_stars) + ("☆" * (5 - ms_stars))
            analyst_html = (
                "<span style='color:#b8860b'>" + stars_str + "</span>"
                + (" <span style='color:" + ar_color + ";font-size:10px'>" + ms_ar + "</span>" if ms_ar else "")
            )
        elif rec:
            rc           = "#1a7a3a" if "buy" in rec else "#c0392b" if "sell" in rec else "#b8860b"
            analyst_html = "<span style='color:" + rc + ";font-size:10px;text-transform:uppercase'>" + rec + "</span>"
        else:
            analyst_html = "<span style='color:#555555'>--</span>"

        bd = "border-bottom:1px solid #21293a;background:#87CEFB;color:#0a0a0a"
        return (
            "<tr>"
            "<td style='padding:8px 12px;" + bd + ";color:#06402B;font-weight:700'>" + ticker + "</td>"
            "<td style='padding:8px 12px;" + bd + ";color:#06402B;font-weight:600'>" + name + "</td>"
            "<td style='padding:8px 12px;" + bd + "'>" + (("EUR " + p_str) if p_str != "--" else "--") + "</td>"
            "<td style='padding:8px 12px;" + bd + "'>" + wchg_cell + "</td>"
            "<td style='padding:8px 12px;" + bd + "'>" + (str(shares) if shares else "--") + "</td>"
            "<td style='padding:8px 12px;" + bd + ";font-weight:700'>" + (("EUR " + v_str) if v_str != "--" else "--") + "</td>"
            "<td style='padding:8px 12px;" + bd + "'>" + analyst_html + "</td>"
            "</tr>"
        )

    def _holdings_table(items: list) -> str:
        heads = "".join(
            "<th style='padding:8px 12px;text-align:left;background:#87CEFB;"
            "color:#0a0a0a;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
            + h + "</th>"
            for h in ["Ticker", "Name", "Price EUR", "Week Chg", "Shares", "Value EUR", "Analyst"]
        )
        rows = "".join(
            _holding_row_weekly(s)
            for s in items
            if "error" not in s
        )
        return (
            "<table style='width:100%;border-collapse:collapse;"
            "background:#87CEFB;border-radius:8px;overflow:hidden'>"
            "<thead><tr>" + heads + "</tr></thead><tbody>" + rows + "</tbody></table>"
        )

    stocks_table = (
        "<h2 style='font-size:14px;color:#f0f2f5;margin:0 0 10px'>Stocks</h2>"
        + _holdings_table(snapshot.get("stocks", []))
        + "<h2 style='font-size:14px;color:#f0f2f5;margin:24px 0 10px'>ETFs</h2>"
        + _holdings_table(snapshot.get("etfs", []))
    )

    # -- Top movers block --
    movers_block = ""
    if week_movements:
        top  = sorted(week_movements, key=lambda x: abs(x.get("move_pct", 0)), reverse=True)[:8]
        rows = ""
        for m in top:
            mp  = m.get("move_pct", 0)
            col = "#52d68a" if mp >= 0 else "#f56565"
            rows += (
                "<tr>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a;"
                "color:#4f9ef8;font-weight:600'>" + m.get("ticker", "") + "</td>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a;"
                "color:#7d8fa8'>" + m.get("name", "")[:24] + "</td>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a'>EUR "
                + "{:.2f}".format(m.get("from_eur", 0)) + "</td>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a'>EUR "
                + "{:.2f}".format(m.get("to_eur", 0)) + "</td>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a;"
                "color:" + col + ";font-weight:700'>"
                + ("+" if mp >= 0 else "") + "{:.2f}".format(mp) + "%</td>"
                "</tr>"
            )
        heads = "".join(
            "<th style='padding:8px 14px;text-align:left;background:#1c2330;"
            "color:#7d8fa8;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
            + h + "</th>"
            for h in ["Ticker", "Name", "Mon Open", "Fri Close", "Week Chg"]
        )
        movers_block = (
            "<h2 style='font-size:14px;color:#f0f2f5;margin:24px 0 10px'>"
            "Top Movers This Week</h2>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden;margin-bottom:24px'>"
            "<thead><tr>" + heads + "</tr></thead><tbody>" + rows + "</tbody></table>"
        )

    # -- Rating changes block --
    cutoff      = (datetime.utcnow() - timedelta(days=6)).strftime("%Y-%m-%d")
    all_changes = []
    for h in (intel_data.get("holdings") or []):
        for r in (h.get("ratings") or []):
            if (r.get("date", "") >= cutoff
                    and r.get("to_grade")
                    and (r.get("from_grade", "") or "").lower() != r.get("to_grade", "").lower()):
                all_changes.append({**r, "ticker": h["ticker"], "name": h.get("name", "")})
    all_changes.sort(key=lambda x: x.get("date", ""), reverse=True)

    ratings_block = ""
    if all_changes:
        def _rcrow(c):
            to_g  = c.get("to_grade", "")
            col   = (
                "#52d68a" if any(w in to_g.lower() for w in ["buy", "outperform", "overweight"])
                else "#f56565" if any(w in to_g.lower() for w in ["sell", "underperform", "underweight"])
                else "#f6ad55"
            )
            act   = c.get("action", "").lower()
            a_lbl = {"up": "UPGRADE", "down": "DOWNGRADE", "init": "INIT", "reit": "--"}.get(act, act)
            a_col = {"up": "#52d68a", "down": "#f56565", "init": "#4f9ef8"}.get(act, "#7d8fa8")
            bd    = "1px solid #21293a"
            return (
                "<tr>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";"
                "color:#4f9ef8;font-weight:600'>" + c.get("ticker", "") + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";"
                "color:#7d8fa8'>" + c.get("date", "") + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + "'>"
                + c.get("firm", "") + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";"
                "color:#7d8fa8;text-decoration:line-through'>"
                + (c.get("from_grade") or "--") + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + "'>-></td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";color:" + col + ";"
                "font-weight:700'>" + to_g + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";color:" + a_col + ";"
                "font-size:10px'>" + a_lbl + "</td>"
                "</tr>"
            )
        rc_heads = "".join(
            "<th style='padding:8px 12px;text-align:left;background:#1c2330;"
            "color:#7d8fa8;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
            + h + "</th>"
            for h in ["Ticker", "Date", "Firm", "From", "", "To", "Action"]
        )
        ratings_block = (
            "<h2 style='font-size:14px;color:#f0f2f5;margin:24px 0 10px'>"
            "Rating Changes This Week</h2>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden;margin-bottom:24px'>"
            "<thead><tr>" + rc_heads + "</tr></thead>"
            "<tbody>" + "".join(_rcrow(c) for c in all_changes) + "</tbody></table>"
        )

    # -- News block --
    news_sections = ""
    for h in (intel_data.get("holdings") or []):
        articles = [a for a in (h.get("news") or []) if a.get("date", "") >= cutoff]
        if not articles:
            continue
        rows = ""
        for a in articles[:4]:
            url   = a.get("url", "#")
            title = a.get("title", "")
            src   = a.get("source", "")
            dt    = a.get("date", "")
            summ  = (a.get("summary") or "")[:140]
            rows += (
                "<tr><td style='padding:11px 14px;border-bottom:1px solid #21293a;"
                "vertical-align:top'>"
                "<a href='" + url + "' style='color:#4f9ef8;text-decoration:none;"
                "font-weight:600;font-size:12px;display:block;margin-bottom:4px'>"
                + title + "</a>"
                "<span style='color:#52d68a;font-size:10px'>" + src + "</span>"
                "<span style='color:#7d8fa8;font-size:10px'>"
                + (" - " + dt if dt else "") + "</span>"
                + ("<div style='color:#7d8fa8;font-size:11px;margin-top:3px'>"
                   + summ + "...</div>" if summ else "")
                + "</td></tr>"
            )
        news_sections += (
            "<div style='margin-bottom:18px'>"
            "<span style='color:#4f9ef8;font-weight:600'>" + h.get("ticker", "") + "</span>"
            "<span style='color:#7d8fa8;font-size:11px;margin-left:8px'>"
            + h.get("name", "") + "</span>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden;margin-top:8px'>"
            "<tbody>" + rows + "</tbody></table></div>"
        )
    if news_sections:
        news_sections = (
            "<h2 style='font-size:14px;color:#f0f2f5;margin:24px 0 12px'>"
            "News This Week</h2>" + news_sections
        )

    return (
        "<div style='" + _BASE + "'>"
        "<h1 style='font-size:20px;color:#f6ad55;margin:0 0 4px'>Weekly Summary</h1>"
        "<p style='color:#7d8fa8;margin:0 0 24px'>" + now + "</p>"
        + week_block
        + stocks_table
        + sentiment_html(sentiments)
        + movers_block
        + ratings_block
        + news_sections
        + "<p style='color:#4a5568;font-size:10px;margin-top:24px'>"
        "Portfolio Intelligence - GitHub Actions</p>"
        "</div>"
    )


def next_week_calendar_html(calendar: dict, next_mon: str, next_fri: str) -> str:
    def _TH2(s):
        return (
            "<th style='padding:8px 12px;text-align:left;background:#87CEFB;"
            "color:#0a0a0a;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
            + s + "</th>"
        )

    def _td2(v, x=""):
        return (
            "<td style='padding:9px 12px;border-bottom:1px solid #21293a;"
            "background:#87CEFB;color:#0a0a0a;" + x + "'>"
            + str(v) + "</td>"
        )

    def _section(title, color, rows_html, cols):
        heads = "".join(_TH2(c) for c in cols)
        return (
            "<h2 style='font-size:14px;color:" + color + ";margin:0 0 10px'>"
            + title + "</h2>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden;margin-bottom:24px'>"
            "<thead><tr>" + heads + "</tr></thead><tbody>" + rows_html + "</tbody></table>"
        )

    earnings_block = ""
    if calendar.get("earnings"):
        rows = ""
        for e in calendar["earnings"]:
            eps = ("$" + "{:.2f}".format(e["eps_estimate"])) if e.get("eps_estimate") is not None else "--"
            rev = ("$" + "{:.1f}".format(e["revenue_est"] / 1e9) + "B") if (e.get("revenue_est") and e["revenue_est"] > 1e6) else "--"
            rows += (
                "<tr>"
                + _td2(e.get("date", ""),      "color:#7d8fa8")
                + _td2(e.get("ticker", ""),     "color:#4f9ef8;font-weight:600")
                + _td2(e.get("name", "")[:22],  "color:#7d8fa8")
                + _td2(eps,                      "color:#f6ad55")
                + _td2(rev,                      "color:#7d8fa8")
                + "</tr>"
            )
        earnings_block = _section(
            "Earnings Reports Next Week", "#f6ad55", rows,
            ["Date", "Ticker", "Company", "EPS Est.", "Rev Est."]
        )

    dividends_block = ""
    if calendar.get("dividends"):
        rows = ""
        for d in calendar["dividends"]:
            amt = ((d.get("currency", "") + " " + "{:.4f}".format(d["amount"]))
                   if d.get("amount") is not None else "--")
            rows += (
                "<tr>"
                + _td2(d.get("ex_date", ""),   "color:#7d8fa8")
                + _td2(d.get("ticker", ""),     "color:#4f9ef8;font-weight:600")
                + _td2(d.get("name", "")[:22],  "color:#7d8fa8")
                + _td2(amt,                      "color:#52d68a")
                + "</tr>"
            )
        dividends_block = _section(
            "Ex-Dividend Dates Next Week", "#52d68a", rows,
            ["Ex-Date", "Ticker", "Company", "Amount"]
        )

    splits_block = ""
    if calendar.get("splits"):
        rows = ""
        for s in calendar["splits"]:
            rows += (
                "<tr>"
                + _td2(s.get("date", ""),      "color:#7d8fa8")
                + _td2(s.get("ticker", ""),     "color:#4f9ef8;font-weight:600")
                + _td2(s.get("name", "")[:22],  "color:#7d8fa8")
                + _td2(s.get("ratio", "--"),     "color:#b794f4;font-weight:600")
                + "</tr>"
            )
        splits_block = _section(
            "Stock Splits Next Week", "#b794f4", rows,
            ["Date", "Ticker", "Company", "Ratio"]
        )

    if not earnings_block and not dividends_block and not splits_block:
        body = (
            "<div style='background:#1c2330;border-radius:8px;padding:18px 20px;"
            "color:#7d8fa8;font-size:12px;margin-bottom:24px'>"
            "No earnings, dividends, or splits scheduled for your holdings "
            "next week (" + next_mon + " - " + next_fri + ").</div>"
        )
    else:
        body = earnings_block + dividends_block + splits_block

    return (
        "<h2 style='font-size:15px;color:#f0f2f5;margin:0 0 4px'>"
        "Next Week's Important Dates</h2>"
        "<p style='color:#7d8fa8;font-size:11px;margin:0 0 16px'>"
        + next_mon + " - " + next_fri + "</p>"
        + body
    )

def get_perplexity_sentiment(ticker: str, name: str) -> dict:
    """
    Fetch trading sentiment for a stock via Perplexity API.
    Returns dict with sentiment (Bullish/Neutral/Bearish) and summary (2-3 sentences).
    """
    import urllib.request
    import json

    api_key = os.environ.get("PERPLEXITY_API_KEY", "")
    if not api_key:
        log.warning("  PERPLEXITY_API_KEY not set")
        return {}

    prompt = (
        "Analyze the current trading sentiment for " + name + " (" + ticker + "). "
        "Search for the latest news, analyst opinions, Reddit discussions, and social media. "
        "Reply in exactly this format:\n"
        "SENTIMENT: [Bullish/Neutral/Bearish]\n"
        "SUMMARY: [2-3 sentences explaining the key reasons for this sentiment, "
        "mentioning specific recent events or data points if available]"
    )

    payload = json.dumps({
        "model": "sonar",
        "messages": [
            {
                "role": "system",
                "content": (
                    "You are a financial analyst. Be concise and factual. "
                    "Always base your answer on the most recent available information."
                )
            },
            {
                "role": "user",
                "content": prompt
            }
        ],
        "max_tokens": 200,
        "temperature": 0.2,
        "search_recency_filter": "week",
        "return_citations": False,
    }).encode("utf-8")

    try:
        req = urllib.request.Request(
            "https://api.perplexity.ai/chat/completions",
            data=payload,
            headers={
                "Authorization": "Bearer " + api_key,
                "Content-Type":  "application/json",
                "Accept":        "application/json",
            },
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        text = data["choices"][0]["message"]["content"].strip()
        log.info("  Perplexity raw: " + text[:80])

        # Parse SENTIMENT and SUMMARY from response
        sentiment = "Neutral"
        summary   = text

        for line in text.splitlines():
            line = line.strip()
            if line.upper().startswith("SENTIMENT:"):
                val = line.split(":", 1)[1].strip().capitalize()
                if val in ("Bullish", "Bearish", "Neutral"):
                    sentiment = val
            elif line.upper().startswith("SUMMARY:"):
                summary = line.split(":", 1)[1].strip()

        log.info("  " + ticker + " sentiment: " + sentiment)
        return {"sentiment": sentiment, "summary": summary}

    except Exception as e:
        log.warning("  Perplexity failed for " + ticker + ": " + str(e))
        return {}

def sentiment_html(sentiments: list) -> str:
    """Render sentiment analysis table for weekly summary."""
    if not sentiments:
        return ""

    def _color(s):
        return (
            "#1a7a3a" if s == "Bullish"
            else "#c0392b" if s == "Bearish"
            else "#b8860b"
        )

    def _badge(s):
        col = _color(s)
        return (
            "<span style='background:" + col + ";color:#fff;"
            "padding:2px 8px;border-radius:4px;font-size:10px;"
            "font-weight:700;text-transform:uppercase'>" + s + "</span>"
        )

    rows = ""
    for item in sentiments:
        bd = "border-bottom:1px solid #21293a;background:#1c2330;color:#f0f2f5"
        rows += (
            "<tr>"
            "<td style='padding:10px 12px;" + bd + ";color:#4f9ef8;"
            "font-weight:700;white-space:nowrap'>" + item["ticker"] + "</td>"
            "<td style='padding:10px 12px;" + bd + ";color:#7d8fa8'>"
            + item["name"][:22] + "</td>"
            "<td style='padding:10px 12px;" + bd + ";text-align:center'>"
            + _badge(item["sentiment"]) + "</td>"
            "<td style='padding:10px 12px;" + bd + ";font-size:12px;color:#c0cad8'>"
            + item["summary"] + "</td>"
            "</tr>"
        )

    heads = "".join(
        "<th style='padding:8px 12px;text-align:left;background:#1c2330;"
        "color:#7d8fa8;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
        + h + "</th>"
        for h in ["Ticker", "Name", "Sentiment", "Analysis"]
    )

    return (
        "<h2 style='font-size:14px;color:#f0f2f5;margin:24px 0 10px'>"
        "AI Sentiment Analysis</h2>"
        "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
        "border-radius:8px;overflow:hidden;margin-bottom:24px'>"
        "<thead><tr>" + heads + "</tr></thead>"
        "<tbody>" + rows + "</tbody></table>"
    )
