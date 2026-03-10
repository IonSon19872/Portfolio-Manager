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
    SNAPSHOT_F, DATA_DIR,
    get_stock_data,
    append_alert, send_email,
    digest_html, movement_html, log
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


def check_movements(snapshot: dict, cfg: dict) -> int:
    threshold   = cfg["alerts"].get("movement_threshold_pct", 3.0)
    last_prices = cfg.get("last_prices", {})
    alerts_sent = 0

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
                    ticker + " moved " + direction + " " +
                    "{:.1f}".format(abs(move)) + "% " +
                    "(EUR " + "{:.2f}".format(prev) +
                    " -> EUR " + "{:.2f}".format(price_now) + ")"
                )
                log.info("  ALERT: " + msg)
                append_alert("movement", ticker, msg)
                send_email(
                    "[ALERT] " + ticker + " " + direction + " " + "{:.1f}".format(abs(move)) + "%",
                    movement_html(ticker, item.get("name", ticker), price_now, prev, move),
                    cfg
                )
                alerts_sent += 1

        last_prices[ticker] = price_now

    cfg["last_prices"] = last_prices
    save_config(cfg)
    return alerts_sent


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

    log.info("--- Movement check ---")
    alerts = check_movements(snapshot, cfg)
    log.info(str(alerts) + " movement alert(s) sent")

    if mode == "full":
        log.info("--- Sending full digest email ---")
        label = datetime.utcnow().strftime("%H:%M UTC")
        append_alert("digest", "", "Digest sent at " + label)
        send_email(
            "Portfolio Digest - " + label,
            digest_html(snapshot, label),
            cfg
        )
        log.info("  Digest email sent")
    else:
        log.info("--- Movement-only mode - no digest email ---")

    log.info("=== Done ===")


if __name__ == "__main__":
    main()
