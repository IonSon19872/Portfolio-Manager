#!/usr/bin/env python3
"""
saturday_summary.py -- Triggered every Saturday at 09:00 CET
=============================================================
Sends one weekly roundup email covering:

  PAST WEEK
  ---------
  1. Portfolio total value + week-over-week change
  2. Top movers of the week (Mon open vs Fri close)
  3. Analyst rating changes from the past 5 trading days
  4. News from the past 5 days across all holdings

  NEXT WEEK AHEAD
  ---------------
  5. [EARNINGS] Earnings reports (date, time BMO/AMC, EPS estimate, revenue estimate)
  6. [DIV] Ex-dividend dates (amount, pay date, frequency)
  7. [SPLIT]  Stock splits (ratio)

Calendar data is fetched from Finnhub for EACH holding.
Calls per holding: 2 (earnings + dividends) + 1 split check = 3
With 1 s throttle + 1 calendar/ipo call: ~65 s for 20 holdings.
"""

import sys
from pathlib import Path
from datetime import datetime, timedelta, date

sys.path.insert(0, str(Path(__file__).parent))
from shared import (
    load_config, load_json, save_json,
    SNAPSHOT_F, INTEL_F, DATA_DIR,
    send_email, append_alert,
    saturday_summary_html, next_week_calendar_html,
    get_earnings_calendar, get_dividends, get_stock_splits,
    to_finnhub_symbol,
    log
)

WEEK_OPEN_F = DATA_DIR / "week_open.json"


# -- Date helpers --------------------------------------------------------------
def next_weekday_range():
    """Return (next_monday_str, next_friday_str) as YYYY-MM-DD."""
    today    = date.today()
    days_to_mon = (7 - today.weekday()) % 7 or 7   # always next Monday
    next_mon = today + timedelta(days=days_to_mon)
    next_fri = next_mon + timedelta(days=4)
    return next_mon.isoformat(), next_fri.isoformat()


def fmt_date(d: str) -> str:
    """YYYY-MM-DD -> 'Mon 10 Mar'"""
    try:
        return datetime.strptime(d, "%Y-%m-%d").strftime("%a %d %b")
    except Exception:
        return d


# -- Week movers ---------------------------------------------------------------
def build_week_movements(snapshot: dict, week_open: dict) -> list:
    now_map  = {s["ticker"]: s for s in
                snapshot.get("stocks", []) + snapshot.get("etfs", [])
                if "error" not in s and s.get("price_eur")}
    open_map = {s["ticker"]: s for s in
                week_open.get("stocks", []) + week_open.get("etfs", [])
                if "error" not in s and s.get("price_eur")}
    moves = []
    for ticker, now in now_map.items():
        opened = open_map.get(ticker)
        if not opened:
            continue
        p_now  = now["price_eur"]
        p_open = opened["price_eur"]
        if p_open == 0:
            continue
        moves.append({
            "ticker":   ticker,
            "name":     now.get("name", ticker),
            "from_eur": round(p_open, 2),
            "to_eur":   round(p_now,  2),
            "move_pct": round((p_now - p_open) / p_open * 100, 2),
        })
    return sorted(moves, key=lambda x: abs(x["move_pct"]), reverse=True)


# -- Next-week calendar fetch --------------------------------------------------
def fetch_next_week_calendar(cfg: dict) -> dict:
    """
    Fetch earnings, dividends, and splits for every holding for next week.
    Returns:
      {
        "earnings":  [...],   # sorted by date
        "dividends": [...],   # sorted by ex_date
        "splits":    [...],   # sorted by date
      }
    Each item includes ticker + name for display.
    """
    api_key  = cfg["finnhub"]["api_key"]
    next_mon, next_fri = next_weekday_range()

    all_holdings = cfg["portfolio"]["stocks"] + cfg["portfolio"]["etfs"]
    earnings_all  = []
    dividends_all = []
    splits_all    = []

    log.info(f"  Fetching calendar data for next week: {next_mon} -> {next_fri}")

    for h in all_holdings:
        ticker      = (h.get("ticker") or "").strip()
        name        = h.get("name", ticker)
        finnhub_sym = h.get("finnhub_symbol") or to_finnhub_symbol(ticker)
        if not ticker:
            continue

        log.info(f"    {ticker} ({finnhub_sym})")

        # -- Earnings (1 call) ----------------------------------------------
        earnings = get_earnings_calendar(finnhub_sym, api_key, next_mon, next_fri)
        for e in earnings:
            earnings_all.append({**e, "ticker": ticker, "name": name})

        # -- Dividends (1 call) --------------------------------------------
        # Dividends: look for ex-dates in next week's range
        divs = get_dividends(finnhub_sym, api_key, next_mon, next_fri)
        for d in divs:
            dividends_all.append({**d, "ticker": ticker, "name": name})

        # -- Splits (1 call) -----------------------------------------------
        splits = get_stock_splits(finnhub_sym, api_key, next_mon, next_fri)
        for s in splits:
            splits_all.append({**s, "ticker": ticker, "name": name})

    # Sort each list by date
    earnings_all.sort(key=lambda x: x.get("date",""))
    dividends_all.sort(key=lambda x: x.get("ex_date",""))
    splits_all.sort(key=lambda x: x.get("date",""))

    log.info(f"  Calendar: {len(earnings_all)} earnings, "
             f"{len(dividends_all)} dividends, {len(splits_all)} splits")
    return {
        "earnings":  earnings_all,
        "dividends": dividends_all,
        "splits":    splits_all,
        "next_mon":  next_mon,
        "next_fri":  next_fri,
    }


# -- Main ----------------------------------------------------------------------
def main():
    log.info("======== Saturday Weekly Summary ========")
    cfg = load_config()

    if not cfg["finnhub"]["api_key"]:
        log.error("FINNHUB_API_KEY not set.")
        sys.exit(1)

    # -- Load this week's data ---------------------------------------------
    snapshot   = load_json(SNAPSHOT_F, {"stocks": [], "etfs": [], "total_eur": 0})
    week_open  = load_json(WEEK_OPEN_F, {})
    intel_data = load_json(INTEL_F, {"holdings": []})

    if week_open:
        snapshot["week_start_eur"] = week_open.get("total_eur")
        log.info(f"  Week: €{snapshot['week_start_eur']:,.2f}  ->  €{snapshot['total_eur']:,.2f}")
    else:
        log.info("  No week_open.json -- week change omitted")

    week_movements = build_week_movements(snapshot, week_open) if week_open else []
    log.info(f"  {len(week_movements)} movers computed")

    # -- Fetch next week's calendar ----------------------------------------
    log.info("-- Fetching next-week calendar (earnings, dividends, splits) --")
    calendar = fetch_next_week_calendar(cfg)
    next_mon, next_fri = calendar["next_mon"], calendar["next_fri"]

    # -- Build email -------------------------------------------------------
    log.info("-- Building and sending Saturday email --")

    # Past-week body
    past_html = saturday_summary_html(snapshot, intel_data, week_movements)

    # Next-week calendar section
    cal_html = next_week_calendar_html(
        calendar,
        fmt_date(next_mon),
        fmt_date(next_fri)
    )

    # Inject calendar section just before the footer line
    footer_marker = "<p style='color:#323749;font-size:10px;margin-top:24px'>"
    combined_html = past_html.replace(
        footer_marker,
        cal_html + footer_marker,
        1
    )

    now_label = datetime.utcnow().strftime("%d %b %Y")
    sent = send_email(
        f"[WEEKLY] Weekly Summary -- {now_label}",
        combined_html,
        cfg
    )

    if sent:
        append_alert("weekly_summary", "",
                     f"Weekly summary sent -- {len(calendar['earnings'])} earnings, "
                     f"{len(calendar['dividends'])} dividends, "
                     f"{len(calendar['splits'])} splits next week")

    log.info("======== Done ========")


if __name__ == "__main__":
    main()
