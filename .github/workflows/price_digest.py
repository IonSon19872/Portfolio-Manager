#!/usr/bin/env python3
"""
price_digest.py -- Two modes, controlled by DIGEST_MODE env var
===============================================================
  DIGEST_MODE=full      (07:45 CET via price_digest.yml)
    -> Fetch all holdings, send full portfolio digest email, check movements.

  DIGEST_MODE=movement  (08:30 * 12:00 * 15:15 * 17:00 CET via movement_check.yml)
    -> Fetch all holdings, check movements only -- NO digest email.
    -> Movement alert emails are still sent per-stock when threshold breached.

# Finnhub calls per run: 5 calls x 20 holdings = 100 calls (~100 s at 1 s/call).
"""

import os
import sys
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from shared import (
    load_config, save_config, save_json, load_json,
    SNAPSHOT_F, DATA_DIR,
    get_stock_data, to_eur,
    append_alert, send_email,
    digest_html, movement_html, log
)

WEEK_OPEN_F = DATA_DIR / "week_open.json"


def build_snapshot(cfg: dict) -> dict:
    api_key  = cfg["finnhub"]["api_key"]
    snapshot = {"stocks": [], "etfs": [], "total_eur": 0.0,
                "timestamp": datetime.utcnow().isoformat()}

    log.info("-- Stocks ------------------------------------------")
    for holding in cfg["portfolio"]["stocks"]:
        if not holding.get("ticker"):
            continue
        data = get_stock_data(holding, api_key)
        data["shares"]    = holding.get("shares", 0)
        data["value_eur"] = round((data.get("price_eur") or 0) * data["shares"], 2)
        snapshot["stocks"].append(data)
        if "error" not in data:
            snapshot["total_eur"] += data["value_eur"]
            log.info(f"    [OK] €{data['price_eur']}  ({data['change_pct']:+.2f}%)  value €{data['value_eur']}")
        else:
            log.warning(f"    [FAIL] {data['error']}")

    log.info("-- ETFs ---------------------------------------------")
    for holding in cfg["portfolio"]["etfs"]:
        if not holding.get("ticker"):
            continue
        data = get_stock_data(holding, api_key)
        data["shares"]    = holding.get("shares", 0)
        data["value_eur"] = round((data.get("price_eur") or 0) * data["shares"], 2)
        snapshot["etfs"].append(data)
        if "error" not in data:
            snapshot["total_eur"] += data["value_eur"]
            log.info(f"    [OK] €{data['price_eur']}  ({data['change_pct']:+.2f}%)  value €{data['value_eur']}")
        else:
            log.warning(f"    [FAIL] {data['error']}")

    snapshot["total_eur"] = round(snapshot["total_eur"], 2)
    return snapshot


def check_movements(snapshot: dict, cfg: dict):
    """Compare current prices against last known prices.
    Send an alert email for any holding that moved > threshold %."""
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
                direction = "UP (UP)" if move > 0 else "DOWN (DOWN)"
                msg = (f"{ticker} moved {direction} {abs(move):.1f}% "
                       f"(€{prev:.2f} -> €{price_now:.2f})")
                log.info(f"  [ALERT] ALERT: {msg}")
                append_alert("movement", ticker, msg)
                send_email(
                    f"[ALERT] {ticker} {direction} {abs(move):.1f}%",
                    movement_html(ticker, item.get("name", ticker),
                                  price_now, prev, move),
                    cfg
                )
                alerts_sent += 1

        last_prices[ticker] = price_now

    cfg["last_prices"] = last_prices
    save_config(cfg)
    return alerts_sent


def main():
    mode = os.environ.get("DIGEST_MODE", "full").strip().lower()
    log.info(f"======== Price Digest -- mode={mode} ========")

    cfg = load_config()
    if not cfg["finnhub"]["api_key"]:
        log.error("FINNHUB_API_KEY not set. Add it as a GitHub Secret.")
        sys.exit(1)

    log.info(f"Portfolio: {len(cfg['portfolio']['stocks'])} stocks, "
             f"{len(cfg['portfolio']['etfs'])} ETFs")

    snapshot = build_snapshot(cfg)
    log.info(f"Total portfolio value: €{snapshot['total_eur']:,.2f}")

    save_json(SNAPSHOT_F, snapshot)
    log.info(f"Snapshot saved -> {SNAPSHOT_F}")

    # Save Monday's first snapshot for Saturday week-over-week comparison
    if datetime.utcnow().weekday() == 0:
        existing = load_json(WEEK_OPEN_F, {})
        if existing.get("timestamp", "")[:10] != datetime.utcnow().strftime("%Y-%m-%d"):
            save_json(WEEK_OPEN_F, snapshot)
            log.info(f"Monday open snapshot saved -> {WEEK_OPEN_F}")

    log.info("-- Movement check -----------------------------------")
    alerts = check_movements(snapshot, cfg)
    log.info(f"  {alerts} movement alert(s) sent")

    if mode == "full":
        log.info("-- Sending full digest email ------------------------")
        label = datetime.utcnow().strftime("%H:%M UTC")
        append_alert("digest", "", f"Digest sent at {label}")
        send_email(
            f"[DIGEST] Portfolio Digest -- {label}",
            digest_html(snapshot, f"Digest * {label}"),
            cfg
        )
        log.info("  Digest email sent")
    else:
        log.info("-- Movement-only mode -- no digest email -------------")

    log.info("======== Done ========")


if __name__ == "__main__":
    main()

# ========================================================
# 1. Fetches live EUR prices + fundamentals for every holding via Finnhub.
# 2. Checks for price movements > threshold % -> sends individual alert email.
# 3. Sends the full portfolio digest email.
# 4. Writes docs/data/snapshot.json  (dashboard reads this file).
# 5. Appends to docs/data/alerts.json.

# Finnhub calls per stock: 5  (quote, profile, metrics, rec, price-target)
# With 1 s between each call, 20 stocks x 5 calls = ~100 s total runtime.
# GitHub Actions free tier allows jobs up to 6 hours -- plenty of headroom.
# ---

import sys
import logging
from pathlib import Path
from datetime import datetime

sys.path.insert(0, str(Path(__file__).parent))
from shared import (
    load_config, save_config, save_json, load_json,
    SNAPSHOT_F, DATA_DIR,
    get_stock_data, to_eur,
    append_alert, send_email,
    digest_html, movement_html, log
)

WEEK_OPEN_F = DATA_DIR / "week_open.json"


def build_snapshot(cfg: dict) -> dict:
    api_key  = cfg["finnhub"]["api_key"]
    snapshot = {"stocks": [], "etfs": [], "total_eur": 0.0,
                "timestamp": datetime.utcnow().isoformat()}

    log.info("-- Stocks ------------------------------------------")
    for holding in cfg["portfolio"]["stocks"]:
        if not holding.get("ticker"):
            continue
        data = get_stock_data(holding, api_key)
        data["shares"]    = holding.get("shares", 0)
        data["value_eur"] = round((data.get("price_eur") or 0) * data["shares"], 2)
        snapshot["stocks"].append(data)
        if "error" not in data:
            snapshot["total_eur"] += data["value_eur"]
            log.info(f"    [OK] €{data['price_eur']}  ({data['change_pct']:+.2f}%)  value €{data['value_eur']}")
        else:
            log.warning(f"    [FAIL] {data['error']}")

    log.info("-- ETFs ---------------------------------------------")
    for holding in cfg["portfolio"]["etfs"]:
        if not holding.get("ticker"):
            continue
        data = get_stock_data(holding, api_key)
        data["shares"]    = holding.get("shares", 0)
        data["value_eur"] = round((data.get("price_eur") or 0) * data["shares"], 2)
        snapshot["etfs"].append(data)
        if "error" not in data:
            snapshot["total_eur"] += data["value_eur"]
            log.info(f"    [OK] €{data['price_eur']}  ({data['change_pct']:+.2f}%)  value €{data['value_eur']}")
        else:
            log.warning(f"    [FAIL] {data['error']}")

    snapshot["total_eur"] = round(snapshot["total_eur"], 2)
    return snapshot


def check_movements(snapshot: dict, cfg: dict):
    """
    Compare current prices against last known prices.
    Send an individual alert email for any holding that moved > threshold %.
    """
    threshold   = cfg["alerts"].get("movement_threshold_pct", 3.0)
    last_prices = cfg.get("last_prices", {})

    for item in snapshot["stocks"] + snapshot["etfs"]:
        if "error" in item or not item.get("price_eur"):
            continue
        ticker    = item["ticker"]
        price_now = item["price_eur"]
        prev      = last_prices.get(ticker)

        if prev and prev > 0:
            move = ((price_now - prev) / prev) * 100
            if abs(move) >= threshold:
                direction = "UP (UP)" if move > 0 else "DOWN (DOWN)"
                msg = (f"{ticker} moved {direction} {abs(move):.1f}% "
                       f"(€{prev:.2f} -> €{price_now:.2f})")
                log.info(f"  [ALERT] ALERT: {msg}")
                append_alert("movement", ticker, msg)
                send_email(
                    f"[ALERT] {ticker} {direction} {abs(move):.1f}%",
                    movement_html(ticker, item.get("name", ticker),
                                  price_now, prev, move),
                    cfg
                )

        # Update stored price
        last_prices[ticker] = price_now

    cfg["last_prices"] = last_prices
    save_config(cfg)


def main():
    log.info("======== Price Digest (Finnhub) ========")
    cfg = load_config()

    if not cfg["finnhub"]["api_key"]:
        log.error("FINNHUB_API_KEY not set. Add it as a GitHub Secret.")
        sys.exit(1)

    log.info(f"Portfolio: {len(cfg['portfolio']['stocks'])} stocks, "
             f"{len(cfg['portfolio']['etfs'])} ETFs")

    snapshot = build_snapshot(cfg)
    log.info(f"Total portfolio value: €{snapshot['total_eur']:,.2f}")

    save_json(SNAPSHOT_F, snapshot)
    log.info(f"Snapshot saved -> {SNAPSHOT_F}")

    # Save Monday's opening snapshot for Saturday week-over-week comparison
    if datetime.utcnow().weekday() == 0:   # 0 = Monday
        if not WEEK_OPEN_F.exists() or \
           load_json(WEEK_OPEN_F, {}).get("timestamp","")[:10] != datetime.utcnow().strftime("%Y-%m-%d"):
            save_json(WEEK_OPEN_F, snapshot)
            log.info(f"Monday open snapshot saved -> {WEEK_OPEN_F}")

    log.info("-- Movement check -----------------------------------")
    check_movements(snapshot, cfg)

    log.info("-- Sending digest email -----------------------------")
    label = datetime.utcnow().strftime("%H:%M UTC")
    append_alert("digest", "", f"Digest sent at {label}")
    send_email(
        f"[DIGEST] Portfolio Digest -- {label}",
        digest_html(snapshot, f"Digest * {label}"),
        cfg
    )

    log.info("======== Done ========")


if __name__ == "__main__":
    main()
