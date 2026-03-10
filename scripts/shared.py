"""
shared.py -- All data via Finnhub (free tier: 60 calls/min)
"""

import os, json, time, smtplib, logging
from datetime import datetime, date, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from pathlib import Path

import requests

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
    "finnhub": {
        "api_key": "",
        "news_days_back": 1,
        "max_news_per_stock": 3,
        "ratings_days_back": 7
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
            "EMAIL_FROM":      ("alerts",  "email_from"),
            "EMAIL_PASSWORD":  ("alerts",  "email_password"),
            "EMAIL_TO":        ("alerts",  "email_to"),
            "FINNHUB_API_KEY": ("finnhub", "api_key"),
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
        safe["finnhub"]["api_key"]       = ""
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


# -- FINNHUB CLIENT -----------------------------------------------------------
FINNHUB_BASE  = "https://finnhub.io/api/v1"
_last_call_ts = 0.0


def _get(endpoint: str, params: dict, api_key: str):
    global _last_call_ts
    gap = time.time() - _last_call_ts
    if gap < 1.0:
        time.sleep(1.0 - gap)
    params = dict(params)
    params["token"] = api_key
    try:
        r = requests.get(f"{FINNHUB_BASE}/{endpoint}", params=params, timeout=15)
        _last_call_ts = time.time()
        if r.status_code == 200:
            return r.json()
        if r.status_code == 429:
            log.warning("  Rate limited -- sleeping 5 s")
            time.sleep(5)
        else:
            log.warning(f"  {endpoint} HTTP {r.status_code}")
        return None
    except Exception as e:
        _last_call_ts = time.time()
        log.warning(f"  {endpoint} error: {e}")
        return None


# -- FX -----------------------------------------------------------------------
_fx_cache: dict = {}


def _load_fx_rates(api_key: str):
    if _fx_cache:
        return
    data = _get("forex/rates", {"base": "EUR"}, api_key)
    if data and "quote" in data:
        for ccy, eur_to_x in data["quote"].items():
            if eur_to_x and eur_to_x != 0:
                _fx_cache[ccy.upper()] = 1.0 / float(eur_to_x)
    _fx_cache.setdefault("EUR", 1.0)
    fallbacks = {"USD":0.925,"GBP":1.17,"CHF":1.06,"SEK":0.087,
                 "NOK":0.086,"DKK":0.134,"JPY":0.006,"CAD":0.683,"AUD":0.60}
    for ccy, rate in fallbacks.items():
        _fx_cache.setdefault(ccy, rate)


def to_eur(price, currency: str, api_key: str) -> float:
    if price is None:
        return 0.0
    _load_fx_rates(api_key)
    ccy = (currency or "USD").upper().strip()
    if ccy == "GBX":
        price /= 100
        ccy = "GBP"
    rate = _fx_cache.get(ccy, 1.0)
    return price * rate


# -- TICKER CONVERSION --------------------------------------------------------
_SUFFIX_TO_PREFIX = {
    ".AS": "AMS:", ".DE": "XETRA:", ".PA": "EPA:",  ".MI": "MIL:",
    ".L":  "LON:", ".SW": "SWX:",   ".BR": "EBR:",  ".CO": "CPH:",
    ".MC": "BME:", ".HE": "HEL:",   ".ST": "STO:",  ".OL": "OSL:",
    ".VI": "VIE:", ".LS": "LIS:",   ".AT": "ATH:",
}


def to_finnhub_symbol(yahoo_ticker: str) -> str:
    t = yahoo_ticker.strip()
    if ":" in t:
        return t.upper()
    if "." not in t:
        return t.upper()
    t_upper = t.upper()
    for suffix, prefix in _SUFFIX_TO_PREFIX.items():
        if t_upper.endswith(suffix.upper()):
            base = t[:len(t)-len(suffix)].upper()
            return f"{prefix}{base}"
    return t.split(".")[0].upper()


# -- STOCK DATA ---------------------------------------------------------------
def get_stock_data(holding: dict, api_key: str) -> dict:
    yahoo_ticker = holding["ticker"]
    finnhub_sym  = holding.get("finnhub_symbol") or to_finnhub_symbol(yahoo_ticker)
    log.info(f"  {yahoo_ticker}  ->  Finnhub: {finnhub_sym}")

    out = {
        "ticker":         yahoo_ticker,
        "finnhub_symbol": finnhub_sym,
        "name":           holding.get("name", yahoo_ticker),
    }

    # Call 1: Quote
    q = _get("quote", {"symbol": finnhub_sym}, api_key)
    # q["c"] is 0 when market is closed (common for European stocks on free tier)
    # Fall back to previous close so we still show the stock rather than hiding it
    raw_c  = q.get("c")  if q else None
    raw_pc = q.get("pc") if q else None
    price  = float(raw_c  or raw_pc or 0)
    prev   = float(raw_pc or raw_c  or 0)
    if not q or price == 0:
        out["error"] = (
            "No price data from Finnhub (symbol tried: " + finnhub_sym + "). "
            "Add 'finnhub_symbol' override in portfolio_config.json if needed."
        )
        return out

    chg_pct = float(q.get("dp", 0))

    out.update({
        "price_native": round(price, 2),
        "prev_close":   round(prev, 2),
        "change_pct":   round(chg_pct, 2),
        "high_today":   round(float(q.get("h", 0)), 2),
        "low_today":    round(float(q.get("l", 0)), 2),
        "open_today":   round(float(q.get("o", 0)), 2),
    })

    # Call 2: Company profile
    profile  = _get("stock/profile2", {"symbol": finnhub_sym}, api_key)
    currency = "USD"
    if profile and profile.get("name"):
        currency = profile.get("currency", "USD") or "USD"
        out.update({
            "name":       profile.get("name") or out["name"],
            "currency":   currency,
            "sector":     profile.get("finnhubIndustry", ""),
            "country":    profile.get("country", ""),
            "exchange":   profile.get("exchange", ""),
            "market_cap": profile.get("marketCapitalization"),
            "logo":       profile.get("logo", ""),
        })
    else:
        out["currency"] = currency

    price_eur = to_eur(price, currency, api_key)
    prev_eur  = to_eur(prev,  currency, api_key)
    out["price_eur"] = round(price_eur, 2)
    out["prev_eur"]  = round(prev_eur,  2)

    # Call 3: Metrics
    met = _get("stock/metric", {"symbol": finnhub_sym, "metric": "all"}, api_key)
    if met and "metric" in met:
        m = met["metric"]
        out.update({
            "pe_ratio":       m.get("peNormalizedAnnual") or m.get("peTTM"),
            "forward_pe":     m.get("forwardPE"),
            "beta":           m.get("beta"),
            "52w_high":       round(to_eur(m.get("52WeekHigh"), currency, api_key), 2) if m.get("52WeekHigh") else None,
            "52w_low":        round(to_eur(m.get("52WeekLow"),  currency, api_key), 2) if m.get("52WeekLow")  else None,
            "dividend_yield": m.get("dividendYieldIndicatedAnnual"),
            "eps_ttm":        m.get("epsTTM"),
            "revenue_growth": m.get("revenueGrowthTTMYoy"),
            "roe":            m.get("roeTTM"),
        })

    # Call 4: Analyst recommendation
    rec = _get("stock/recommendation", {"symbol": finnhub_sym}, api_key)
    if rec and isinstance(rec, list) and rec:
        latest     = rec[0]
        sb         = latest.get("strongBuy",  0)
        b          = latest.get("buy",        0)
        h          = latest.get("hold",       0)
        s          = latest.get("sell",       0)
        ss         = latest.get("strongSell", 0)
        tot        = sb + b + h + s + ss
        bull_ratio = (sb + b) / tot if tot else 0
        label      = "buy" if bull_ratio >= 0.6 else "sell" if bull_ratio <= 0.3 else "hold"
        out.update({
            "recommendation": label,
            "analyst_buy":    sb + b,
            "analyst_hold":   h,
            "analyst_sell":   s + ss,
            "analyst_total":  tot,
            "rec_period":     latest.get("period", ""),
        })

    # Call 5: Price target
    pt = _get("stock/price-target", {"symbol": finnhub_sym}, api_key)
    if pt and pt.get("targetMean"):
        out.update({
            "analyst_target_mean": round(to_eur(pt.get("targetMean"), currency, api_key), 2),
            "analyst_target_high": round(to_eur(pt.get("targetHigh"), currency, api_key), 2) if pt.get("targetHigh") else None,
            "analyst_target_low":  round(to_eur(pt.get("targetLow"),  currency, api_key), 2) if pt.get("targetLow")  else None,
            "num_analysts":        pt.get("numberOfAnalysts"),
        })

    out["timestamp"] = datetime.utcnow().isoformat()
    return out


# -- ANALYST UPGRADES ---------------------------------------------------------
def get_analyst_upgrades(finnhub_sym: str, api_key: str, days_back: int = 7) -> list:
    from_date = (date.today() - timedelta(days=days_back)).isoformat()
    to_date   = date.today().isoformat()
    data = _get("stock/upgrade-downgrade",
                {"symbol": finnhub_sym, "from": from_date, "to": to_date},
                api_key)
    if not data or not isinstance(data, list):
        return []
    return [
        {
            "date":       item.get("gradeDate", ""),
            "firm":       item.get("company", ""),
            "from_grade": item.get("fromGrade", ""),
            "to_grade":   item.get("toGrade", ""),
            "action":     item.get("action", ""),
        }
        for item in data
    ]


# -- COMPANY NEWS -------------------------------------------------------------
def get_company_news(finnhub_sym: str, api_key: str,
                     days_back: int = 1, max_articles: int = 3) -> list:
    from_date = (date.today() - timedelta(days=days_back)).isoformat()
    to_date   = date.today().isoformat()
    data = _get("company-news",
                {"symbol": finnhub_sym, "from": from_date, "to": to_date},
                api_key)
    if not data or not isinstance(data, list):
        return []
    results, seen = [], set()
    for item in data:
        title = item.get("headline", "")
        if not title or title in seen:
            continue
        seen.add(title)
        ts = item.get("datetime", 0)
        results.append({
            "title":   title,
            "source":  item.get("source", ""),
            "url":     item.get("url", ""),
            "date":    datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d") if ts else "",
            "summary": (item.get("summary") or "")[:200],
        })
        if len(results) >= max_articles:
            break
    return results


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
        log.info(f"  Sent: {subject}")
        return True
    except Exception as e:
        log.error(f"  Email failed: {e}")
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
        "<th style='padding:8px 12px;text-align:left;background:#1c2330;"
        "color:#7d8fa8;font-size:10px;text-transform:uppercase;"
        "letter-spacing:1px'>" + s + "</th>"
    )

def _td(v, x=""):
    style = "padding:8px 12px;border-bottom:1px solid #21293a;" + x
    return "<td style='" + style + "'>" + str(v) + "</td>"


def _holding_row(s: dict) -> str:
    chg = s.get("change_pct") or 0

    # Detect market-closed state: price returned is prev close, change is 0
    price_native  = s.get("price_native") or 0
    prev_close    = s.get("prev_close")   or 0
    market_closed = (chg == 0 and price_native > 0 and abs(price_native - prev_close) < 0.001)
    color = "#7d8fa8" if market_closed else ("#52d68a" if chg >= 0 else "#f56565")
    arrow = "+" if chg >= 0 else "-"

    rec    = (s.get("recommendation") or "").replace("_", " ")
    rc     = "#52d68a" if "buy" in rec else "#f56565" if "sell" in rec else "#f6ad55"
    counts = ""
    if s.get("analyst_total"):
        counts = (
            "B:" + str(s.get("analyst_buy",  0)) +
            " H:" + str(s.get("analyst_hold", 0)) +
            " S:" + str(s.get("analyst_sell", 0))
        )

    # Safely format price and value
    p_raw  = s.get("price_eur")
    v_raw  = s.get("value_eur")
    shares = s.get("shares") or 0
    p_str  = "{:.2f}".format(float(p_raw)) if p_raw not in (None, "", "--") else "--"
    v_str  = "{:.2f}".format(float(v_raw)) if v_raw not in (None, "", "--") else "--"
    # Recompute value if missing (backward compat with old snapshots)
    if v_str == "--" and p_str != "--" and shares:
        v_str = "{:.2f}".format(float(p_str) * float(shares))

    label        = rec or counts or "--"
    closed_badge = "<span style='color:#4a5568;font-size:9px'> mkt closed</span>" if market_closed else ""
    chg_cell     = (
        "<span style='color:" + color + "'>"
        + arrow + " " + "{:.2f}".format(abs(chg)) + "%"
        + "</span>" + closed_badge
    )
    rec_cell = (
        "<span style='color:" + rc + ";font-size:10px;text-transform:uppercase'>"
        + label + "</span>"
    )

    return (
        "<tr>"
        + _td(s.get("ticker", ""),         "color:#4f9ef8;font-weight:600")
        + _td((s.get("name") or "")[:26],  "color:#7d8fa8")
        + _td(("EUR " + p_str) if p_str != "--" else "--")
        + _td(chg_cell)
        + _td(str(shares) if shares else "--")
        + _td(("EUR " + v_str) if v_str != "--" else "--", "font-weight:600")
        + _td(rec_cell)
        + "</tr>"
    )


def _table(rows: str) -> str:
    heads = "".join(_TH(h) for h in ["Ticker", "Name", "Price EUR", "Day Chg", "Shares", "Value EUR", "Analyst"])
    return (
        "<table style='width:100%;border-collapse:collapse;"
        "background:#1c2330;border-radius:8px;overflow:hidden'>"
        "<thead><tr>" + heads + "</tr></thead><tbody>" + rows + "</tbody></table>"
    )


def digest_html(snapshot: dict, label: str) -> str:
    total    = snapshot.get("total_eur", 0)
    stk_rows = "".join(_holding_row(s) for s in snapshot.get("stocks", []) if "error" not in s)
    etf_rows = "".join(_holding_row(e) for e in snapshot.get("etfs",   []) if "error" not in e)
    now      = datetime.utcnow().strftime("%A, %d %B %Y - %H:%M UTC")
    return (
        "<div style='" + _BASE + "'>"
        "<div style='font-size:10px;color:#7d8fa8;text-transform:uppercase;letter-spacing:2px;margin-bottom:6px'>Portfolio Digest - Finnhub</div>"
        "<h1 style='font-size:20px;color:#4f9ef8;margin:0 0 4px'>Portfolio Digest - " + label + "</h1>"
        "<p style='color:#7d8fa8;margin:0 0 20px'>" + now + "</p>"
        "<p style='font-size:24px;color:#52d68a;margin:0 0 24px'>Total: <strong>EUR " + "{:,.2f}".format(total) + "</strong></p>"
        "<h2 style='font-size:14px;color:#f0f2f5;margin:0 0 10px'>Stocks</h2>" + _table(stk_rows)
        + "<h2 style='font-size:14px;color:#f0f2f5;margin:24px 0 10px'>ETFs</h2>" + _table(etf_rows)
        + "<p style='color:#4a5568;font-size:10px;margin-top:24px'>Portfolio Intelligence - GitHub Actions - Finnhub.io</p>"
        "</div>"
    )


def movement_html(ticker: str, name: str, price_now: float,
                  price_prev: float, move_pct: float) -> str:
    up    = move_pct > 0
    color = "#52d68a" if up else "#f56565"
    arrow = "UP" if up else "DOWN"
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<div style='" + _BASE + "'>"
        "<div style='font-size:10px;color:#7d8fa8;text-transform:uppercase;"
        "letter-spacing:2px;margin-bottom:6px'>Movement Alert - Finnhub</div>"
        "<h1 style='font-size:26px;color:" + color + ";margin:0 0 8px'>" + arrow + " " + ticker + "</h1>"
        "<p style='color:#7d8fa8;margin:0 0 20px'>" + name + "</p>"
        "<table style='width:100%;border-collapse:collapse;background:#1c2330;border-radius:8px;overflow:hidden'>"
        "<tr><td style='padding:12px 16px;color:#7d8fa8;width:140px'>Previous close</td>"
        "    <td style='padding:12px 16px;font-weight:600'>EUR " + "{:.2f}".format(price_prev) + "</td></tr>"
        "<tr style='background:#1c2330'>"
        "    <td style='padding:12px 16px;color:#7d8fa8'>Current price</td>"
        "    <td style='padding:12px 16px;font-weight:600;color:" + color + "'>EUR " + "{:.2f}".format(price_now) + "</td></tr>"
        "<tr><td style='padding:12px 16px;color:#7d8fa8'>Change</td>"
        "    <td style='padding:12px 16px;font-size:22px;font-weight:700;color:" + color + "'>" + arrow + " " + "{:.2f}".format(abs(move_pct)) + "%</td></tr>"
        "</table>"
        "<p style='color:#4a5568;font-size:10px;margin-top:20px'>" + now + "</p>"
        "</div>"
    )


def rating_change_html(ticker: str, name: str, changes: list) -> str:
    def _row(c):
        to_g = c.get("to_grade", "")
        tl   = to_g.lower()
        col  = (
            "#52d68a" if any(w in tl for w in ["buy", "outperform", "overweight", "positive"])
            else "#f56565" if any(w in tl for w in ["sell", "underperform", "underweight", "negative"])
            else "#f6ad55"
        )
        action    = c.get("action", "").lower()
        badge_map = {
            "up":   "<span style='color:#52d68a;font-size:10px'>UPGRADE</span>",
            "down": "<span style='color:#f56565;font-size:10px'>DOWNGRADE</span>",
            "init": "<span style='color:#4f9ef8;font-size:10px'>INITIATION</span>",
            "reit": "<span style='color:#7d8fa8;font-size:10px'>REITERATE</span>",
        }
        badge = badge_map.get(action, "")
        bd    = "1px solid #21293a"
        return (
            "<tr>"
            "<td style='padding:9px 12px;border-bottom:" + bd + ";color:#7d8fa8'>" + c.get("date", "") + "</td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + ";font-weight:600'>" + c.get("firm", "") + "</td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + ";color:#7d8fa8;text-decoration:line-through'>" + (c.get("from_grade") or "--") + "</td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + "'>-></td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + ";color:" + col + ";font-weight:700;font-size:15px'>" + to_g + "</td>"
            "<td style='padding:9px 12px;border-bottom:" + bd + "'>" + badge + "</td>"
            "</tr>"
        )

    rows  = "".join(_row(c) for c in changes)
    heads = "".join(_TH(h) for h in ["Date", "Firm", "From", "", "To", "Action"])
    now   = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    return (
        "<div style='" + _BASE + "'>"
        "<div style='font-size:10px;color:#b794f4;text-transform:uppercase;"
        "letter-spacing:2px;margin-bottom:6px'>Analyst Rating Change - Finnhub</div>"
        "<h1 style='font-size:24px;color:#b794f4;margin:0 0 6px'>" + ticker + "</h1>"
        "<p style='color:#7d8fa8;margin:0 0 24px'>" + name + "</p>"
        "<table style='width:100%;border-collapse:collapse;background:#1c2330;border-radius:8px;overflow:hidden'>"
        "<thead><tr>" + heads + "</tr></thead><tbody>" + rows + "</tbody></table>"
        "<p style='color:#4a5568;font-size:10px;margin-top:20px'>" + now + "</p>"
        "</div>"
    )


def news_digest_html(holdings_with_news: list, run_label: str) -> str:
    now = datetime.utcnow().strftime("%A, %d %B %Y - %H:%M UTC")

    def _news_section(h: dict) -> str:
        articles = h.get("news", [])
        if not articles:
            return ""
        rows = ""
        for a in articles:
            src      = a.get("source", "")
            date_str = a.get("date", "")
            summ     = (a.get("summary") or "")[:160]
            if summ and not summ.endswith((".", "...")):
                summ += "..."
            url       = a.get("url", "#")
            title     = a.get("title", "")
            date_part = (" * " + date_str) if date_str else ""
            summ_part = (
                "<div style='color:#7d8fa8;font-size:11px;line-height:1.6'>" + summ + "</div>"
                if summ else ""
            )
            rows += (
                "<tr>"
                "<td style='padding:12px 14px;border-bottom:1px solid #21293a;vertical-align:top'>"
                "<a href='" + url + "' style='color:#4f9ef8;text-decoration:none;font-weight:600;"
                "font-size:12.5px;line-height:1.5;display:block;margin-bottom:5px'>" + title + "</a>"
                "<div style='color:#7d8fa8;font-size:11px;margin-bottom:4px'>"
                "<span style='color:#52d68a'>" + src + "</span>"
                " " + date_part +
                "</div>"
                + summ_part +
                "</td>"
                "</tr>"
            )
        return (
            "<div style='margin-bottom:20px'>"
            "<div style='display:flex;align-items:baseline;gap:10px;margin-bottom:8px'>"
            "<span style='color:#4f9ef8;font-weight:600;font-size:13px'>" + h["ticker"] + "</span>"
            "<span style='color:#7d8fa8;font-size:11px'>" + h.get("name", "") + "</span>"
            "</div>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden'><tbody>" + rows + "</tbody></table>"
            "</div>"
        )

    sections       = "".join(_news_section(h) for h in holdings_with_news)
    total_articles = sum(len(h.get("news", [])) for h in holdings_with_news)
    return (
        "<div style='" + _BASE + "'>"
        "<div style='font-size:10px;color:#7d8fa8;text-transform:uppercase;"
        "letter-spacing:2px;margin-bottom:6px'>News Digest - Finnhub</div>"
        "<h1 style='font-size:20px;color:#52d68a;margin:0 0 4px'>News Digest - " + run_label + "</h1>"
        "<p style='color:#7d8fa8;margin:0 0 6px'>" + now + "</p>"
        "<p style='color:#7d8fa8;font-size:11px;margin:0 0 24px'>"
        + str(total_articles) + " article(s) across " + str(len(holdings_with_news)) + " holding(s)</p>"
        + sections
        + "<p style='color:#4a5568;font-size:10px;margin-top:16px'>"
        "Portfolio Intelligence - GitHub Actions - Finnhub.io</p>"
        "</div>"
    )


def saturday_summary_html(snapshot: dict, intel_data: dict,
                           week_movements: list) -> str:
    now        = datetime.utcnow().strftime("%A, %d %B %Y - %H:%M UTC")
    total_eur  = snapshot.get("total_eur", 0)
    week_start = snapshot.get("week_start_eur")
    week_chg   = ((total_eur - week_start) / week_start * 100) if week_start else None
    chg_color  = "#52d68a" if (week_chg or 0) >= 0 else "#f56565"
    chg_arrow  = "+" if (week_chg or 0) >= 0 else "-"

    if week_chg is not None:
        week_chg_html = (
            "<div>"
            "<div style='font-size:10px;color:#7d8fa8;text-transform:uppercase;"
            "letter-spacing:1px;margin-bottom:6px'>Week Change</div>"
            "<div style='font-size:22px;color:" + chg_color + ";font-weight:700'>"
            + chg_arrow + " " + "{:.2f}".format(abs(week_chg)) + "%</div>"
            "</div>"
        )
    else:
        week_chg_html = ""

    week_block = (
        "<div style='background:#1c2330;border-radius:10px;padding:20px 24px;"
        "margin-bottom:24px;display:flex;justify-content:space-between;"
        "align-items:center;flex-wrap:wrap;gap:12px'>"
        "<div>"
        "<div style='font-size:10px;color:#7d8fa8;text-transform:uppercase;"
        "letter-spacing:1px;margin-bottom:6px'>Portfolio Value</div>"
        "<div style='font-size:28px;color:#52d68a;font-weight:700'>EUR " + "{:,.2f}".format(total_eur) + "</div>"
        "</div>"
        + week_chg_html +
        "</div>"
    )

    movers_block = ""
    if week_movements:
        top  = sorted(week_movements, key=lambda x: abs(x.get("move_pct", 0)), reverse=True)[:8]
        rows = ""
        for m in top:
            mp    = m.get("move_pct", 0)
            col   = "#52d68a" if mp >= 0 else "#f56565"
            arrow = "+" if mp >= 0 else "-"
            rows += (
                "<tr>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a;"
                "color:#4f9ef8;font-weight:600'>" + m.get("ticker", "") + "</td>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a;"
                "color:#7d8fa8'>" + (m.get("name", ""))[:24] + "</td>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a'>"
                "EUR " + "{:.2f}".format(m.get("from_eur", 0)) + "</td>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a'>"
                "EUR " + "{:.2f}".format(m.get("to_eur", 0)) + "</td>"
                "<td style='padding:9px 14px;border-bottom:1px solid #21293a;"
                "color:" + col + ";font-weight:700'>" + arrow + " " + "{:.2f}".format(abs(mp)) + "%</td>"
                "</tr>"
            )
        heads = "".join(
            "<th style='padding:8px 14px;text-align:left;background:#1c2330;"
            "color:#7d8fa8;font-size:10px;text-transform:uppercase;letter-spacing:1px'>" + h + "</th>"
            for h in ["Ticker", "Name", "Mon Open", "Fri Close", "Week Chg"]
        )
        movers_block = (
            "<h2 style='font-size:14px;color:#f0f2f5;margin:0 0 10px'>Top Movers This Week</h2>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden;margin-bottom:24px'>"
            "<thead><tr>" + heads + "</tr></thead><tbody>" + rows + "</tbody></table>"
        )

    ratings_block = ""
    cutoff        = (datetime.utcnow() - timedelta(days=6)).strftime("%Y-%m-%d")
    all_changes   = []
    for h in (intel_data.get("holdings") or []):
        for r in (h.get("ratings") or []):
            if (r.get("date", "") >= cutoff
                    and r.get("to_grade")
                    and (r.get("from_grade", "") or "").lower() != r.get("to_grade", "").lower()):
                all_changes.append({**r, "ticker": h["ticker"], "name": h.get("name", "")})
    all_changes.sort(key=lambda x: x.get("date", ""), reverse=True)

    if all_changes:
        def _rc_row(c):
            to_g = c.get("to_grade", "")
            tl   = to_g.lower()
            col  = (
                "#52d68a" if any(w in tl for w in ["buy", "outperform", "overweight"])
                else "#f56565" if any(w in tl for w in ["sell", "underperform", "underweight"])
                else "#f6ad55"
            )
            act   = c.get("action", "").lower()
            a_lbl = {"up": "UPGRADE", "down": "DOWNGRADE", "init": "INIT", "reit": "--"}.get(act, act)
            a_col = {"up": "#52d68a", "down": "#f56565", "init": "#4f9ef8"}.get(act, "#7d8fa8")
            bd    = "1px solid #21293a"
            return (
                "<tr>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";color:#4f9ef8;font-weight:600'>" + c.get("ticker", "") + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";color:#7d8fa8'>" + c.get("date", "") + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + "'>" + c.get("firm", "") + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";color:#7d8fa8;text-decoration:line-through'>" + (c.get("from_grade") or "--") + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + "'>-></td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";color:" + col + ";font-weight:700'>" + to_g + "</td>"
                "<td style='padding:8px 12px;border-bottom:" + bd + ";color:" + a_col + ";font-size:10px'>" + a_lbl + "</td>"
                "</tr>"
            )
        rc_heads = "".join(
            "<th style='padding:8px 12px;text-align:left;background:#1c2330;"
            "color:#7d8fa8;font-size:10px;text-transform:uppercase;letter-spacing:1px'>" + h + "</th>"
            for h in ["Ticker", "Date", "Firm", "From", "", "To", "Action"]
        )
        ratings_block = (
            "<h2 style='font-size:14px;color:#f0f2f5;margin:0 0 10px'>Rating Changes This Week</h2>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden;margin-bottom:24px'>"
            "<thead><tr>" + rc_heads + "</tr></thead>"
            "<tbody>" + "".join(_rc_row(c) for c in all_changes) + "</tbody></table>"
        )

    news_sections = ""
    for h in (intel_data.get("holdings") or []):
        articles = [a for a in (h.get("news") or []) if a.get("date", "") >= cutoff]
        if not articles:
            continue
        rows = ""
        for a in articles[:4]:
            src       = a.get("source", "")
            dt        = a.get("date", "")
            title     = a.get("title", "")
            url       = a.get("url", "#")
            summ      = (a.get("summary") or "")[:140]
            dt_part   = (" * " + dt) if dt else ""
            summ_part = (
                "<div style='color:#7d8fa8;font-size:11px;margin-top:3px'>" + summ + "...</div>"
                if summ else ""
            )
            rows += (
                "<tr><td style='padding:11px 14px;border-bottom:1px solid #21293a;vertical-align:top'>"
                "<a href='" + url + "' style='color:#4f9ef8;text-decoration:none;font-weight:600;"
                "font-size:12px;display:block;margin-bottom:4px'>" + title + "</a>"
                "<span style='color:#52d68a;font-size:10px'>" + src + "</span>"
                "<span style='color:#7d8fa8;font-size:10px'>" + dt_part + "</span>"
                + summ_part +
                "</td></tr>"
            )
        news_sections += (
            "<div style='margin-bottom:18px'>"
            "<div style='margin-bottom:8px'>"
            "<span style='color:#4f9ef8;font-weight:600'>" + h.get("ticker", "") + "</span>"
            "<span style='color:#7d8fa8;font-size:11px;margin-left:8px'>" + h.get("name", "") + "</span>"
            "</div>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden'><tbody>" + rows + "</tbody></table>"
            "</div>"
        )
    if news_sections:
        news_sections = (
            "<h2 style='font-size:14px;color:#f0f2f5;margin:0 0 12px'>News This Week</h2>"
            + news_sections
        )

    return (
        "<div style='" + _BASE + "'>"
        "<div style='font-size:10px;color:#7d8fa8;text-transform:uppercase;"
        "letter-spacing:2px;margin-bottom:6px'>Weekly Summary - Finnhub</div>"
        "<h1 style='font-size:20px;color:#f6ad55;margin:0 0 4px'>Weekly Summary</h1>"
        "<p style='color:#7d8fa8;margin:0 0 24px'>" + now + "</p>"
        + week_block
        + movers_block
        + ratings_block
        + news_sections
        + "<p style='color:#4a5568;font-size:10px;margin-top:24px'>"
        "Portfolio Intelligence - GitHub Actions - Finnhub.io</p>"
        "</div>"
    )


# -- CALENDAR -----------------------------------------------------------------
def get_earnings_calendar(finnhub_sym: str, api_key: str,
                          from_date: str, to_date: str) -> list:
    data = _get("calendar/earnings",
                {"symbol": finnhub_sym, "from": from_date, "to": to_date},
                api_key)
    if not data or not isinstance(data, dict):
        return []
    return [
        {
            "date":         e.get("date", ""),
            "hour":         e.get("hour", ""),
            "eps_estimate": e.get("epsEstimate"),
            "eps_actual":   e.get("epsActual"),
            "revenue_est":  e.get("revenueEstimate"),
            "revenue_act":  e.get("revenueActual"),
            "quarter":      e.get("quarter"),
            "year":         e.get("year"),
        }
        for e in data.get("earningsCalendar", [])
    ]


def get_dividends(finnhub_sym: str, api_key: str,
                  from_date: str, to_date: str) -> list:
    data = _get("stock/dividend2",
                {"symbol": finnhub_sym, "from": from_date, "to": to_date},
                api_key)
    if not data or not isinstance(data, list):
        return []
    return [
        {
            "ex_date":  d.get("exDate", ""),
            "pay_date": d.get("payDate", ""),
            "amount":   d.get("amount"),
            "currency": d.get("currency", "USD"),
            "freq":     d.get("freq", ""),
        }
        for d in data
    ]


def get_stock_splits(finnhub_sym: str, api_key: str,
                     from_date: str, to_date: str) -> list:
    data = _get("stock/split",
                {"symbol": finnhub_sym, "from": from_date, "to": to_date},
                api_key)
    if not data or not isinstance(data, list):
        return []
    return [
        {
            "date":  s.get("date", ""),
            "ratio": str(s.get("fromFactor", "?")) + ":" + str(s.get("toFactor", "?")),
        }
        for s in data
    ]


def next_week_calendar_html(calendar: dict, next_mon: str, next_fri: str) -> str:
    def _TH2(s):
        return (
            "<th style='padding:8px 12px;text-align:left;background:#1c2330;"
            "color:#7d8fa8;font-size:10px;text-transform:uppercase;"
            "letter-spacing:1px'>" + s + "</th>"
        )

    def _section(title, color, rows_html, col_headers):
        heads = "".join(_TH2(h) for h in col_headers)
        return (
            "<h2 style='font-size:14px;color:" + color + ";margin:0 0 10px'>" + title + "</h2>"
            "<table style='width:100%;border-collapse:collapse;background:#1c2330;"
            "border-radius:8px;overflow:hidden;margin-bottom:24px'>"
            "<thead><tr>" + heads + "</tr></thead>"
            "<tbody>" + rows_html + "</tbody></table>"
        )

    BD = "border-bottom:1px solid #21293a"

    def _td2(v, x=""):
        return "<td style='padding:9px 12px;" + BD + ";" + x + "'>" + str(v) + "</td>"

    earnings_block = ""
    earnings = calendar.get("earnings", [])
    if earnings:
        hour_label = {"bmo": "Before Open", "amc": "After Close", "dmh": "During Market"}
        rows = ""
        for e in earnings:
            hl  = hour_label.get((e.get("hour") or "").lower(), e.get("hour", "") or "--")
            eps = ("$" + "{:.2f}".format(e["eps_estimate"])) if e.get("eps_estimate") is not None else "--"
            rev = ("$" + "{:.1f}".format(e["revenue_est"] / 1e9) + "B") if (e.get("revenue_est") and e["revenue_est"] > 1e6) else "--"
            qtr = ("Q" + str(e.get("quarter")) + " " + str(e.get("year", ""))) if e.get("quarter") else "--"
            rows += (
                "<tr>"
                + _td2(e.get("date", ""),      "color:#7d8fa8")
                + _td2(e.get("ticker", ""),     "color:#4f9ef8;font-weight:600")
                + _td2(e.get("name", "")[:22],  "color:#7d8fa8")
                + _td2(qtr)
                + _td2(hl,                       "color:#7d8fa8;font-size:11px")
                + _td2(eps,                      "color:#f6ad55")
                + _td2(rev,                      "color:#7d8fa8")
                + "</tr>"
            )
        earnings_block = _section(
            "Earnings Reports Next Week", "#f6ad55", rows,
            ["Date", "Ticker", "Company", "Quarter", "Time", "EPS Est.", "Rev Est."]
        )

    dividends_block = ""
    dividends = calendar.get("dividends", [])
    if dividends:
        rows = ""
        for d in dividends:
            amt = ((d.get("currency", "") + " " + "{:.4f}".format(d["amount"])) if d.get("amount") is not None else "--")
            rows += (
                "<tr>"
                + _td2(d.get("ex_date", ""),   "color:#7d8fa8")
                + _td2(d.get("ticker", ""),    "color:#4f9ef8;font-weight:600")
                + _td2(d.get("name", "")[:22], "color:#7d8fa8")
                + _td2(amt,                     "color:#52d68a")
                + _td2(d.get("pay_date", "") or "--", "color:#7d8fa8")
                + _td2((d.get("freq") or "--").title(), "color:#7d8fa8;font-size:11px")
                + "</tr>"
            )
        dividends_block = _section(
            "Ex-Dividend Dates Next Week", "#52d68a", rows,
            ["Ex-Date", "Ticker", "Company", "Amount", "Pay Date", "Frequency"]
        )

    splits_block = ""
    splits = calendar.get("splits", [])
    if splits:
        rows = ""
        for s in splits:
            rows += (
                "<tr>"
                + _td2(s.get("date", ""),      "color:#7d8fa8")
                + _td2(s.get("ticker", ""),    "color:#4f9ef8;font-weight:600")
                + _td2(s.get("name", "")[:22], "color:#7d8fa8")
                + _td2(s.get("ratio", "--"),    "color:#b794f4;font-weight:600")
                + "</tr>"
            )
        splits_block = _section(
            "Stock Splits Next Week", "#b794f4", rows,
            ["Date", "Ticker", "Company", "Ratio"]
        )

    if not earnings and not dividends and not splits:
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
