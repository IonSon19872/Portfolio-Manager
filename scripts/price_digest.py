#!/usr/bin/env python3
"""
price_digest.py - Two modes, controlled by DIGEST_MODE env var

  DIGEST_MODE=full      - fetch all holdings, send digest email, check movements
  DIGEST_MODE=movement  - fetch all holdings, check movements only, no digest email
"""

import os
import sys
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from shared import (
    load_config, save_config, save_json, load_json,
    SNAPSHOT_F, INTEL_F, DATA_DIR,
    get_stock_data, get_company_news, get_earnings_calendar,
    append_alert, send_email,
    digest_html, news_digest_html, _BASE, log
)

WEEK_OPEN_F = DATA_DIR / "week_open.json"


def build_snapshot(cfg: dict) -> dict:
    snapshot = {
        "stocks":    [],
        "etfs":      [],
        "total_eur": 0.0,
        "timestamp": datetime.utcnow().isoformat()
    }

    log.info("--- Stocks ---")
    for holding in cfg["portfolio"]["stocks"]:
        if not holding.get("ticker"):
            continue
        data = get_stock_data(holding)
        data["shares"]    = holding.get("shares", 0)
        data["value_eur"] = round((data.get("price_eur") or 0) * data["shares"], 2)
        snapshot["stocks"].append(data)
        if "error" not in data:
            snapshot["total_eur"] += data["value_eur"]
            log.info("    OK  EUR " + str(data["price_eur"]) +
                     "  (" + "{:+.2f}".format(data["change_pct"]) + "%)" +
                     "  value EUR " + str(data["value_eur"]))
        else:
            log.warning("    FAIL " + data["error"])

    log.info("--- ETFs ---")
    for holding in cfg["portfolio"]["etfs"]:
        if not holding.get("ticker"):
            continue
        data = get_stock_data(holding)
        data["shares"]    = holding.get("shares", 0)
        data["value_eur"] = round((data.get("price_eur") or 0) * data["shares"], 2)
        snapshot["etfs"].append(data)
        if "error" not in data:
            snapshot["total_eur"] += data["value_eur"]
            log.info("    OK  EUR " + str(data["price_eur"]) +
                     "  (" + "{:+.2f}".format(data["change_pct"]) + "%)" +
                     "  value EUR " + str(data["value_eur"]))
        else:
            log.warning("    FAIL " + data["error"])

    snapshot["total_eur"] = round(snapshot["total_eur"], 2)
    return snapshot



def check_movements_and_ratings(snapshot: dict, cfg: dict) -> int:
    threshold    = cfg["alerts"].get("movement_threshold_pct", 3.0)
    last_prices  = cfg.get("last_prices", {})
    morning_prices = cfg.get("morning_prices", {})  # saved by full digest run
    intel_data   = load_json(INTEL_F, {"holdings": []})

    movement_alerts = []
    rating_alerts   = []

    for item in snapshot["stocks"] + snapshot["etfs"]:
        if "error" in item or not item.get("price_eur"):
            continue
        ticker    = item["ticker"]
        price_now = item["price_eur"]
        prev_call = last_prices.get(ticker)    # price from last movement check
        morning   = morning_prices.get(ticker) # price from morning digest

        triggered = False

        # Check 1: moved more than 3% from morning price
        if morning and morning > 0:
            move_from_morning = ((price_now - morning) / morning) * 100
            if abs(move_from_morning) >= threshold:
                direction = "UP" if move_from_morning > 0 else "DOWN"
                msg = (
                    ticker + " " + direction + " " +
                    "{:.1f}".format(abs(move_from_morning)) + "% from morning" +
                    " (EUR " + "{:.2f}".format(morning) +
                    " -> EUR " + "{:.2f}".format(price_now) + ")"
                )
                log.info("  MOVE FROM MORNING: " + msg)
                append_alert("movement_morning", ticker, msg)
                movement_alerts.append({
                    "ticker":     ticker,
                    "name":       item.get("name", ticker),
                    "price_now":  price_now,
                    "price_prev": morning,
                    "move_pct":   move_from_morning,
                    "direction":  direction,
                    "label":      "vs Morning",
                })
                triggered = True

        # Check 2: moved more than 1% since last call (only if not already alerted)
        if not triggered and prev_call and prev_call > 0:
            move_from_last = ((price_now - prev_call) / prev_call) * 100
            # UP more than 1% or DOWN more than 1%
            if move_from_last >= 1.0 or move_from_last <= -1.0:
                direction = "UP" if move_from_last > 0 else "DOWN"
                msg = (
                    ticker + " " + direction + " " +
                    "{:.1f}".format(abs(move_from_last)) + "% since last check" +
                    " (EUR " + "{:.2f}".format(prev_call) +
                    " -> EUR " + "{:.2f}".format(price_now) + ")"
                )
                log.info("  MOVE SINCE LAST: " + msg)
                append_alert("movement_last", ticker, msg)
                movement_alerts.append({
                    "ticker":     ticker,
                    "name":       item.get("name", ticker),
                    "price_now":  price_now,
                    "price_prev": prev_call,
                    "move_pct":   move_from_last,
                    "direction":  direction,
                    "label":      "vs Last Check",
                })

        last_prices[ticker] = price_now

    # Analyst rating changes from today
    today = datetime.utcnow().strftime("%Y-%m-%d")
    for h in intel_data.get("holdings", []):
        for r in h.get("new_ratings", []):
            if r.get("date", "") == today:
                rating_alerts.append({
                    "ticker":     h["ticker"],
                    "name":       h.get("name", h["ticker"]),
                    "firm":       r.get("firm", ""),
                    "from_grade": r.get("from_grade", ""),
                    "to_grade":   r.get("to_grade", ""),
                    "action":     r.get("action", ""),
                })
                log.info(
                    "  RATING: " + h["ticker"] + " " +
                    r.get("firm", "") + " -> " + r.get("to_grade", "")
                )

    cfg["last_prices"] = last_prices
    save_config(cfg)

    if not movement_alerts and not rating_alerts:
        log.info("  Nothing to alert")
        return 0

    # Build combined alert email
    now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html = "<div style='" + _BASE + "'>"
    html += (
        "<h1 style='font-size:20px;color:#FFBF00;margin:0 0 4px'>Portfolio Alerts</h1>"
        "<p style='color:#7d8fa8;margin:0 0 24px'>" + now + "</p>"
    )

    if movement_alerts:
        html += "<h2 style='font-size:14px;color:#f0f2f5;margin:0 0 10px'>Price Movements</h2>"
        html += (
            "<table style='width:100%;border-collapse:collapse;"
            "background:#87CEFB;border-radius:8px;overflow:hidden;margin-bottom:24px'>"
            "<thead><tr>"
        )
        for h_txt in ["Ticker", "Name", "From EUR", "Now EUR", "Change", "Reference"]:
            html += (
                "<th style='padding:8px 12px;text-align:left;background:#87CEFB;"
                "color:#0a0a0a;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
                + h_txt + "</th>"
            )
        html += "</tr></thead><tbody>"
        for m in movement_alerts:
            col   = "#1a7a3a" if m["move_pct"] > 0 else "#c0392b"
            arrow = "+" if m["move_pct"] > 0 else "-"
            bd    = "border-bottom:1px solid #21293a;background:#87CEFB;color:#0a0a0a"
            html += (
                "<tr>"
                "<td style='padding:9px 12px;" + bd + ";color:#06402B;font-weight:700'>" + m["ticker"] + "</td>"
                "<td style='padding:9px 12px;" + bd + ";color:#06402B'>" + m["name"][:22] + "</td>"
                "<td style='padding:9px 12px;" + bd + "'>EUR " + "{:.2f}".format(m["price_prev"]) + "</td>"
                "<td style='padding:9px 12px;" + bd + "'>EUR " + "{:.2f}".format(m["price_now"]) + "</td>"
                "<td style='padding:9px 12px;" + bd + ";color:" + col + ";font-weight:700'>"
                + arrow + " " + "{:.2f}".format(abs(m["move_pct"])) + "%</td>"
                "<td style='padding:9px 12px;" + bd + ";color:#7d8fa8;font-size:10px'>" + m.get("label", "") + "</td>"
                "</tr>"
            )
        html += "</tbody></table>"

    if rating_alerts:
        html += "<h2 style='font-size:14px;color:#f0f2f5;margin:0 0 10px'>Analyst Rating Changes</h2>"
        html += (
            "<table style='width:100%;border-collapse:collapse;"
            "background:#87CEFB;border-radius:8px;overflow:hidden;margin-bottom:24px'>"
            "<thead><tr>"
        )
        for h_txt in ["Ticker", "Name", "Firm", "From", "", "To", "Action"]:
            html += (
                "<th style='padding:8px 12px;text-align:left;background:#87CEFB;"
                "color:#0a0a0a;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
                + h_txt + "</th>"
            )
        html += "</tr></thead><tbody>"
        for r in rating_alerts:
            tg    = r.get("to_grade", "")
            tl    = tg.lower()
            col   = (
                "#1a7a3a" if any(w in tl for w in ["buy", "outperform", "overweight"])
                else "#c0392b" if any(w in tl for w in ["sell", "underperform", "underweight"])
                else "#b8860b"
            )
            act   = r.get("action", "").lower()
            a_lbl = {"up": "UPGRADE", "down": "DOWNGRADE", "init": "INIT", "reit": "--"}.get(act, act)
            a_col = {"up": "#1a7a3a", "down": "#c0392b", "init": "#4f9ef8"}.get(act, "#555555")
            bd    = "border-bottom:1px solid #21293a;background:#87CEFB;color:#0a0a0a"
            html += (
                "<tr>"
                "<td style='padding:9px 12px;" + bd + ";color:#06402B;font-weight:700'>" + r["ticker"] + "</td>"
                "<td style='padding:9px 12px;" + bd + ";color:#06402B'>" + r["name"][:22] + "</td>"
                "<td style='padding:9px 12px;" + bd + "'>" + r.get("firm", "") + "</td>"
                "<td style='padding:9px 12px;" + bd + ";text-decoration:line-through'>" + (r.get("from_grade") or "--") + "</td>"
                "<td style='padding:9px 12px;" + bd + "'>-></td>"
                "<td style='padding:9px 12px;" + bd + ";color:" + col + ";font-weight:700'>" + tg + "</td>"
                "<td style='padding:9px 12px;" + bd + ";color:" + a_col + ";font-size:10px'>" + a_lbl + "</td>"
                "</tr>"
            )
        html += "</tbody></table>"

    html += (
        "<p style='color:#4a5568;font-size:10px;margin-top:24px'>"
        "Portfolio Intelligence - GitHub Actions</p></div>"
    )

    subject_parts = []
    if movement_alerts:
        subject_parts.append(str(len(movement_alerts)) + " movement(s)")
    if rating_alerts:
        subject_parts.append(str(len(rating_alerts)) + " rating change(s)")

    send_email(
        "[ALERT] " + " + ".join(subject_parts) + " - " +
        datetime.utcnow().strftime("%H:%M UTC"),
        html,
        cfg
    )
    return len(movement_alerts) + len(rating_alerts)

def check_earnings_alerts(cfg: dict):
    """Send alert if any holding has earnings in the next 2 days."""
    from datetime import timedelta
    all_holdings = cfg["portfolio"]["stocks"] + cfg["portfolio"]["etfs"]
    today        = datetime.utcnow().date()
    alerts       = []

    for h in all_holdings:
        ticker = (h.get("ticker") or "").strip()
        name   = h.get("name", ticker)
        if not ticker:
            continue
        try:
            from_date = today.isoformat()
            to_date   = (today + timedelta(days=2)).isoformat()
            events    = get_earnings_calendar(ticker,
                                              from_date=from_date,
                                              to_date=to_date)
            for e in events:
                alerts.append({
                    "ticker":       ticker,
                    "name":         name,
                    "date":         e.get("date", ""),
                    "eps_estimate": e.get("eps_estimate"),
                    "revenue_est":  e.get("revenue_est"),
                })
                log.info("  EARNINGS SOON: " + ticker + " on " + e.get("date", ""))
        except Exception as e:
            log.warning("  earnings check failed for " + ticker + ": " + str(e))

    if not alerts:
        return

    now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html = "<div style='" + _BASE + "'>"
    html += (
        "<h1 style='font-size:20px;color:#f6ad55;margin:0 0 4px'>Earnings Alert</h1>"
        "<p style='color:#7d8fa8;margin:0 0 24px'>" + now + "</p>"
        "<table style='width:100%;border-collapse:collapse;"
        "background:#87CEFB;border-radius:8px;overflow:hidden;margin-bottom:24px'>"
        "<thead><tr>"
    )
    for h_txt in ["Ticker", "Name", "Date", "EPS Est.", "Rev Est."]:
        html += (
            "<th style='padding:8px 12px;text-align:left;background:#87CEFB;"
            "color:#0a0a0a;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
            + h_txt + "</th>"
        )
    html += "</tr></thead><tbody>"
    for a in alerts:
        eps = ("$" + "{:.2f}".format(a["eps_estimate"])) if a.get("eps_estimate") is not None else "--"
        rev = ("$" + "{:.1f}".format(a["revenue_est"] / 1e9) + "B") if (a.get("revenue_est") and a["revenue_est"] > 1e6) else "--"
        bd  = "border-bottom:1px solid #21293a;background:#87CEFB;color:#0a0a0a"
        html += (
            "<tr>"
            "<td style='padding:9px 12px;" + bd + ";color:#06402B;font-weight:700'>" + a["ticker"] + "</td>"
            "<td style='padding:9px 12px;" + bd + ";color:#06402B'>" + a["name"][:24] + "</td>"
            "<td style='padding:9px 12px;" + bd + ";color:#f6ad55;font-weight:600'>" + a["date"] + "</td>"
            "<td style='padding:9px 12px;" + bd + "'>" + eps + "</td>"
            "<td style='padding:9px 12px;" + bd + "'>" + rev + "</td>"
            "</tr>"
        )
    html += (
        "</tbody></table>"
        "<p style='color:#4a5568;font-size:10px;margin-top:24px'>"
        "Portfolio Intelligence - GitHub Actions</p></div>"
    )

    send_email(
        "[EARNINGS] " + ", ".join(a["ticker"] for a in alerts) +
        " report within 2 days",
        html,
        cfg
    )
    for a in alerts:
        append_alert("earnings", a["ticker"],
                     a["ticker"] + " earnings on " + a["date"])

def check_52w_alerts(snapshot: dict, cfg: dict):
    """Send alert if any holding hits a new 52-week high or low."""
    alerts = []

    for item in snapshot["stocks"] + snapshot["etfs"]:
        if "error" in item:
            continue
        ticker   = item.get("ticker", "")
        price    = item.get("price_eur")
        high_52w = item.get("52w_high")
        low_52w  = item.get("52w_low")

        if not price or not high_52w or not low_52w:
            continue

        hit_high = price >= high_52w * 0.995  # within 0.5% of 52w high
        hit_low  = price <= low_52w  * 1.005  # within 0.5% of 52w low

        if hit_high or hit_low:
            label = "52W HIGH" if hit_high else "52W LOW"
            color = "#1a7a3a" if hit_high else "#c0392b"
            alerts.append({
                "ticker":  ticker,
                "name":    item.get("name", ticker),
                "price":   price,
                "high":    high_52w,
                "low":     low_52w,
                "label":   label,
                "color":   color,
                "hit_high": hit_high,
            })
            log.info("  52W ALERT: " + ticker + " " + label +
                     " EUR " + "{:.2f}".format(price))

    if not alerts:
        return

    now  = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    html = "<div style='" + _BASE + "'>"
    html += (
        "<h1 style='font-size:20px;color:#b794f4;margin:0 0 4px'>52-Week High/Low Alert</h1>"
        "<p style='color:#7d8fa8;margin:0 0 24px'>" + now + "</p>"
        "<table style='width:100%;border-collapse:collapse;"
        "background:#87CEFB;border-radius:8px;overflow:hidden;margin-bottom:24px'>"
        "<thead><tr>"
    )
    for h_txt in ["Ticker", "Name", "Price EUR", "52W High", "52W Low", "Signal"]:
        html += (
            "<th style='padding:8px 12px;text-align:left;background:#87CEFB;"
            "color:#0a0a0a;font-size:10px;text-transform:uppercase;letter-spacing:1px'>"
            + h_txt + "</th>"
        )
    html += "</tr></thead><tbody>"
    for a in alerts:
        bd = "border-bottom:1px solid #21293a;background:#87CEFB;color:#0a0a0a"
        html += (
            "<tr>"
            "<td style='padding:9px 12px;" + bd + ";color:#06402B;font-weight:700'>" + a["ticker"] + "</td>"
            "<td style='padding:9px 12px;" + bd + ";color:#06402B'>" + a["name"][:24] + "</td>"
            "<td style='padding:9px 12px;" + bd + ";font-weight:600'>EUR " + "{:.2f}".format(a["price"]) + "</td>"
            "<td style='padding:9px 12px;" + bd + ";color:#1a7a3a'>EUR " + "{:.2f}".format(a["high"]) + "</td>"
            "<td style='padding:9px 12px;" + bd + ";color:#c0392b'>EUR " + "{:.2f}".format(a["low"]) + "</td>"
            "<td style='padding:9px 12px;" + bd + ";color:" + a["color"] + ";font-weight:700;font-size:12px'>" + a["label"] + "</td>"
            "</tr>"
        )
    html += (
        "</tbody></table>"
        "<p style='color:#4a5568;font-size:10px;margin-top:24px'>"
        "Portfolio Intelligence - GitHub Actions</p></div>"
    )

    highs = [a["ticker"] for a in alerts if a["hit_high"]]
    lows  = [a["ticker"] for a in alerts if not a["hit_high"]]
    parts = []
    if highs:
        parts.append(", ".join(highs) + " near 52W HIGH")
    if lows:
        parts.append(", ".join(lows) + " near 52W LOW")

    send_email("[52W] " + " | ".join(parts), html, cfg)
    for a in alerts:
        append_alert("52w_" + ("high" if a["hit_high"] else "low"),
                     a["ticker"],
                     a["ticker"] + " " + a["label"] +
                     " EUR " + "{:.2f}".format(a["price"]))

def main():
    mode = os.environ.get("DIGEST_MODE", "full").strip().lower()
    log.info("=== Price Digest  mode=" + mode + " ===")

    cfg = load_config()

    log.info(
        "Portfolio: " + str(len(cfg["portfolio"]["stocks"])) + " stocks, " +
        str(len(cfg["portfolio"]["etfs"])) + " ETFs"
    )

    snapshot = build_snapshot(cfg)
    log.info("Total portfolio value: EUR " + "{:,.2f}".format(snapshot["total_eur"]))

    save_json(SNAPSHOT_F, snapshot)

    # Save morning prices as baseline for intraday movement checks
    cfg["morning_prices"] = {
        item["ticker"]: item["price_eur"]
        for item in snapshot["stocks"] + snapshot["etfs"]
        if "error" not in item and item.get("price_eur")
    }
    save_config(cfg)
  
    log.info("Snapshot saved -> " + str(SNAPSHOT_F))

    if datetime.utcnow().weekday() == 0:
        existing  = load_json(WEEK_OPEN_F, {})
        today_str = datetime.utcnow().strftime("%Y-%m-%d")
        if existing.get("timestamp", "")[:10] != today_str:
            save_json(WEEK_OPEN_F, snapshot)
            log.info("Monday open snapshot saved -> " + str(WEEK_OPEN_F))

    if mode == "full":
        log.info("--- Fetching news for morning digest ---")
        news_days_back = cfg.get("finnhub", {}).get("news_days_back", 1)
        max_news       = cfg.get("finnhub", {}).get("max_news_per_stock", 3)
        all_holdings   = cfg["portfolio"]["stocks"] + cfg["portfolio"]["etfs"]
        holdings_with_news = []
        for h in all_holdings:
            ticker = (h.get("ticker") or "").strip()
            name   = h.get("name", ticker)
            if not ticker:
                continue
            news = get_company_news(ticker, days_back=news_days_back,
                        max_articles=max_news, holding_name=name)
            if news:
                holdings_with_news.append({"ticker": ticker, "name": name, "news": news})
                log.info("  " + ticker + ": " + str(len(news)) + " article(s)")

        log.info("--- Sending full morning digest ---")
        label    = datetime.utcnow().strftime("%H:%M UTC")
        html     = digest_html(snapshot, label)
        if holdings_with_news:
            html = html.replace(
                "<p style='color:#4a5568;font-size:10px;margin-top:24px'>",
                news_digest_html(holdings_with_news, label) +
                "<p style='color:#4a5568;font-size:10px;margin-top:24px'>"
            )
        send_email("Portfolio Digest - " + label, html, cfg)
      # Update last_prices silently so intraday checks have fresh baseline
        check_movements_and_ratings(snapshot, cfg)
        append_alert("digest", "", "Morning digest sent at " + label)

    elif mode == "movement":
        log.info("--- Movement + analyst check ---")
        alerts_triggered = check_movements_and_ratings(snapshot, cfg)
        log.info(str(alerts_triggered) + " alert(s) sent")
    
    log.info("--- Earnings alert check ---")
    check_earnings_alerts(cfg)

    log.info("--- 52-week high/low alert check ---")
    check_52w_alerts(snapshot, cfg)
  
    log.info("=== Done ===")


if __name__ == "__main__":
    main()
