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
    get_stock_data, get_company_news,
    append_alert, send_email,
    digest_html, news_digest_html, movement_html, _BASE, log
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
    threshold   = cfg["alerts"].get("movement_threshold_pct", 3.0)
    last_prices = cfg.get("last_prices", {})
    intel_data  = load_json(INTEL_F, {"holdings": []})

    movement_alerts = []
    rating_alerts   = []

    # --- Price movements ---
    for item in snapshot["stocks"] + snapshot["etfs"]:
        if "error" in item or not item.get("price_eur"):
            continue
        ticker    = item["ticker"]
        price_now = item["price_eur"]
        prev      = last_prices.get(ticker)

        if prev and prev > 0:
            move = ((price_now - prev) / prev) * 100
            if abs(move) >= threshold:
                direction = "UP" if move > 0 else "DOWN"
                msg = (
                    ticker + " " + direction + " " +
                    "{:.1f}".format(abs(move)) + "% " +
                    "(EUR " + "{:.2f}".format(prev) +
                    " -> EUR " + "{:.2f}".format(price_now) + ")"
                )
                log.info("  MOVE: " + msg)
                append_alert("movement", ticker, msg)
                movement_alerts.append({
                    "ticker":    ticker,
                    "name":      item.get("name", ticker),
                    "price_now": price_now,
                    "price_prev": prev,
                    "move_pct":  move,
                    "direction": direction,
                })

        last_prices[ticker] = price_now

    # --- Analyst rating changes (from latest intelligence run) ---
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

    # --- Build combined alert email ---
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
        for h_txt in ["Ticker", "Name", "Prev EUR", "Now EUR", "Change"]:
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
                "<td style='padding:9px 12px;" + bd + ";color:#FFBF00;font-weight:700'>" + m["ticker"] + "</td>"
                "<td style='padding:9px 12px;" + bd + ";color:#FFBF00'>" + m["name"][:24] + "</td>"
                "<td style='padding:9px 12px;" + bd + "'>EUR " + "{:.2f}".format(m["price_prev"]) + "</td>"
                "<td style='padding:9px 12px;" + bd + "'>EUR " + "{:.2f}".format(m["price_now"]) + "</td>"
                "<td style='padding:9px 12px;" + bd + ";color:" + col + ";font-weight:700'>"
                + arrow + " " + "{:.2f}".format(abs(m["move_pct"])) + "%</td>"
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
                "<td style='padding:9px 12px;" + bd + ";color:#FFBF00;font-weight:700'>" + r["ticker"] + "</td>"
                "<td style='padding:9px 12px;" + bd + ";color:#FFBF00'>" + r["name"][:22] + "</td>"
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
            news = get_company_news(ticker, days_back=news_days_back, max_articles=max_news)
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

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
