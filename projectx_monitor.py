#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
ProjectX Order Fill Monitor — Cronmaster edition.

Runs every minute from Cronmaster. Unlike the Cowork sandbox, the Cronmaster
run environment has real network access and a persistent filesystem, so this
version uses a normal state file for dedupe (no duplicate Discord alerts).

State file location is auto-selected: first writable of
  $PROJECTX_STATE_DIR, /app/data, ~/.  -> projectx_monitor_state.json
"""

import os
import io
import json
import sys
import subprocess
import traceback
from datetime import datetime, timezone, timedelta
from pathlib import Path

try:
    import requests
except ImportError:
    import subprocess
    subprocess.check_call([sys.executable, "-m", "pip", "install", "requests",
                           "-q", "--break-system-packages"])
    import requests

# ── Config ────────────────────────────────────────────────────────────────────
# Secrets come from environment variables (see README / .env.example).
USERNAME        = os.environ.get("PROJECTX_USERNAME", "")
API_KEY         = os.environ.get("PROJECTX_API_KEY", "")
BASE_URL        = os.environ.get("PROJECTX_API_BASE", "https://api.topstepx.com/api")
DISCORD_WEBHOOK = os.environ.get("DISCORD_WEBHOOK", "")


def _require_config():
    missing = [k for k, v in {"PROJECTX_USERNAME": USERNAME,
                              "PROJECTX_API_KEY": API_KEY,
                              "DISCORD_WEBHOOK": DISCORD_WEBHOOK}.items() if not v]
    if missing:
        print("CONFIG ERROR: missing environment variable(s): " + ", ".join(missing) +
              ". Set them in your wrapper / .env (see README).", flush=True)
        sys.exit(1)

LOOKBACK_MINUTES = 5
STATUS_FILLED = 2
SIDE_BUY  = 0
SIDE_SELL = 1

# ── Skylit Design System palette ──────────────────────────────────────────────
# The terminal aesthetic of skylit.ai: near-black canvas lit by ice-blue, with
# bull/bear/amber/iris carrying trading + status meaning.
SKY = {
    "ink": "#0A0A0A", "graphite": "#111111", "graphite2": "#151515",
    "mist": "#E8E8E8", "fog": "#9CA3AF", "slate": "#6B7280",
    "ice": "#90BFF9", "ice_soft": "#B9D6FC", "ice_deep": "#5B9DF0",
    "bull": "#34D399", "bear": "#F87171", "amber": "#FBBF24", "iris": "#A78BFA",
    "border": "#1F1F1F", "grid": "#171717", "primary_fg": "#031322",
}
def _hex_int(h):
    return int(h.lstrip("#"), 16)
SKY_ICE  = _hex_int(SKY["ice"])
SKY_BULL = _hex_int(SKY["bull"])
SKY_BEAR = _hex_int(SKY["bear"])


def log(msg: str):
    """Timestamped line to stdout so Cronmaster's log viewer shows live status."""
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _pick_state_file() -> Path:
    candidates = [os.environ.get("PROJECTX_STATE_DIR"), "/app/data", str(Path.home())]
    for d in candidates:
        if not d:
            continue
        try:
            p = Path(d)
            p.mkdir(parents=True, exist_ok=True)
            test = p / ".px_write_test"
            test.write_text("ok")
            test.unlink()
            return p / "projectx_monitor_state.json"
        except Exception:
            continue
    return Path("/tmp/projectx_monitor_state.json")


STATE_FILE = _pick_state_file()

POINT_VALUES = {
    "F.US.EP": 50, "F.US.ENQ": 20, "F.US.MEP": 5, "F.US.MENQ": 2,
    "F.US.RY": 50, "F.US.ERY": 10, "F.US.YM": 5, "F.US.MYM": 0.5,
    "F.US.CL": 1000, "F.US.MCL": 100, "F.US.GC": 100, "F.US.MGC": 10,
    "F.US.SI": 5000, "F.US.HG": 25000, "F.US.NG": 10000, "F.US.ZN": 1000,
    "F.US.ZB": 1000, "F.US.6E": 125000, "F.US.6J": 12500000,
    "F.US.BTC": 5, "F.US.ETH": 50,
    # Micro Silver (SIL) — VERIFY: 1,000 oz, $1/pt = $1,000. Adjust if your
    # broker reports a different multiplier.
    "F.US.SIL": 1000,
}

# Minimum tick size per contract (price increment). Used for ticks-to-SL/PT.
# VERIFY any you trade actively — wrong tick size only affects the tick counts,
# not the dollar math (that uses POINT_VALUES).
TICK_SIZES = {
    "F.US.EP": 0.25, "F.US.ENQ": 0.25, "F.US.MEP": 0.25, "F.US.MENQ": 0.25,
    "F.US.RY": 0.10, "F.US.ERY": 0.10, "F.US.YM": 1.0, "F.US.MYM": 1.0,
    "F.US.CL": 0.01, "F.US.MCL": 0.01, "F.US.GC": 0.10, "F.US.MGC": 0.10,
    "F.US.SI": 0.005, "F.US.SIL": 0.005, "F.US.HG": 0.0005, "F.US.NG": 0.001,
    "F.US.ZN": 0.015625, "F.US.ZB": 0.03125, "F.US.6E": 0.00005,
    "F.US.6J": 0.0000005, "F.US.BTC": 5.0, "F.US.ETH": 0.5,
}


def tick_size(contract_id, symbol_id=None):
    if symbol_id and symbol_id in TICK_SIZES:
        return TICK_SIZES[symbol_id]
    if contract_id:
        for key, ts in TICK_SIZES.items():
            if key in contract_id:
                return ts
    return None


def short_symbol(contract_id, symbol_id=None):
    if symbol_id:
        return symbol_id
    if contract_id:
        for key in POINT_VALUES:
            if key in contract_id:
                return key
    return contract_id or "?"


# ── Topstep account risk limits ───────────────────────────────────────────────
# NOT exposed by the TopstepX API, so set them here. Keyed by a substring of the
# account name. Standard Topstep 50K combine: trailing max drawdown $2,000,
# daily loss limit $1,000. Accounts matching nothing get no guardrail block.
# >>> VERIFY these against your plan. <<<
TOPSTEP_LIMITS = {
    # Uncomment / edit for your plan. Key = a substring of your account name.
    # Standard Topstep 50K combine shown as an example:
    # "50K": {"start": 50000.0, "daily_loss": 1000.0, "trailing_dd": 2000.0, "lock_at": 50000.0},
}
# Daily-loss window resets at this UTC hour (~6 PM ET during EDT futures open).
DAY_RESET_UTC_HOUR = 22


def account_limits(account):
    name = account.get("name", "")
    for key, cfg in TOPSTEP_LIMITS.items():
        if key in name:
            return cfg
    return None


def day_window_start(now):
    d = now.replace(hour=DAY_RESET_UTC_HOUR, minute=0, second=0, microsecond=0)
    return d if now >= d else d - timedelta(days=1)


def _ts(s):
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except Exception:
        return None


def fetch_trades(token, account_id, since):
    try:
        r = api_post(token, "Trade/search", {
            "accountId": account_id, "startTimestamp": since.isoformat(),
            "endTimestamp": datetime.now(timezone.utc).isoformat()})
        return r.get("trades") or []
    except Exception as e:
        log(f"      Trade/search failed: {e}")
        return []


def day_realized(trades):
    """Realized P&L + fees for the day window. profitAndLoss is populated on the
    closing (realizing) trade and null on the opening trade."""
    realized = sum(t["profitAndLoss"] for t in trades
                   if t.get("profitAndLoss") is not None and not t.get("voided"))
    fees = sum((t.get("fees") or 0) + (t.get("commissions") or 0)
               for t in trades if not t.get("voided"))
    return round(realized, 2), round(fees, 2)


def risk_guardrails(account, limits, trades_day, balance, trade_risk, state):
    realized, fees = day_realized(trades_day)
    net = round(realized - fees, 2)
    daily = limits["daily_loss"]
    g = {"realized": realized, "fees": fees, "net": net,
         "daily_loss": daily, "daily_room": round(daily + net, 2),
         "trade_risk": trade_risk}
    peaks = state.setdefault("peak_equity", {})
    key = str(account["id"])
    eq = balance if balance is not None else limits["start"]
    peak = max(peaks.get(key, limits["start"]), eq, limits["start"])
    peaks[key] = peak
    floor = min(peak - limits["trailing_dd"], limits.get("lock_at", limits["start"]))
    g["trailing_floor"] = round(floor, 2)
    g["trailing_room"] = round(balance - floor, 2) if balance is not None else None
    if trade_risk is not None:
        g["daily_breach"] = (net - trade_risk) <= -daily
        if balance is not None:
            g["trail_breach"] = (balance - trade_risk) <= floor
    return g


POS_TYPE_LONG  = 1
POS_TYPE_SHORT = 2

ORDER_TYPE_NAMES = {1: "Limit", 2: "Market", 4: "Stop", 5: "TrailingStop", 6: "JoinBid", 7: "JoinAsk"}
SIDE_NAMES       = {0: "Buy", 1: "Sell"}


def report_open_orders(token, accounts) -> int:
    """Print a snapshot of working/resting orders (status Open) to the console."""
    total = 0
    log("--- Working orders ---")
    for account in accounts:
        aid   = account["id"]
        aname = account.get("name", str(aid))
        try:
            orders = api_post(token, "Order/searchOpen", {"accountId": aid}).get("orders", [])
        except Exception as e:
            log(f"  [{aname}] Order/searchOpen failed: {e}")
            continue
        if not orders:
            log(f"  [{aname}] no working orders")
            continue
        total += len(orders)
        for o in orders:
            type_name = ORDER_TYPE_NAMES.get(o.get("type"), f"Type{o.get('type')}")
            side_name = SIDE_NAMES.get(o.get("side"), "?")
            sym = o.get("symbolId") or o.get("contractId", "?")
            px = ""
            if o.get("limitPrice"):
                px = f" @ Limit {o['limitPrice']}"
            elif o.get("stopPrice"):
                px = f" @ Stop {o['stopPrice']}"
            log(f"  [{aname}] #{o.get('id')} {side_name} {o.get('size', 1)}x {sym} "
                f"[{type_name}{px}]")
    if total == 0:
        log("  No working orders on any account.")
    else:
        log(f"--- {total} working order(s) total ---")
    return total


def point_value(contract_id, symbol_id=None):
    """Resolve $/point for a contract. Handles both 'F.US.ENQ' and
    longer ids like 'CON.F.US.ENQ.M25' by substring match."""
    if symbol_id and symbol_id in POINT_VALUES:
        return POINT_VALUES[symbol_id]
    if contract_id:
        for key, pv in POINT_VALUES.items():
            if key in contract_id:
                return pv
    return None


def report_positions(token, accounts) -> int:
    """Print a live snapshot of open positions + unrealized P&L to the console.
    Returns total number of open positions found."""
    total_positions = 0
    grand_usd = 0.0
    have_usd = False
    log("--- Open positions ---")
    for account in accounts:
        aid   = account["id"]
        aname = account.get("name", str(aid))
        try:
            positions = api_post(token, "Position/searchOpen",
                                 {"accountId": aid}).get("positions", [])
        except Exception as e:
            log(f"  [{aname}] Position/searchOpen failed: {e}")
            continue

        if not positions:
            log(f"  [{aname}] flat (no open positions)")
            continue

        acct_usd = 0.0
        for p in positions:
            total_positions += 1
            cid   = p.get("contractId", "")
            size  = p.get("size") or p.get("netPos") or p.get("quantity")
            avg   = p.get("averagePrice", p.get("avgPrice"))
            ptype = p.get("type", p.get("positionType"))
            if avg is None or size is None or ptype is None:
                # Field names differ from assumption — dump raw so we can fix it.
                log(f"  [{aname}] raw position (unrecognized fields): {json.dumps(p)}")
                continue
            is_long = ptype == POS_TYPE_LONG
            direction = 1 if is_long else -1
            cur = get_last_price(token, cid)
            pv = point_value(cid, p.get("symbolId"))
            pts = usd = None
            if cur is not None:
                pts = round((cur - avg) * direction, 4)
                if pv:
                    usd = round(pts * abs(size) * pv, 2)
                    acct_usd += usd
            sym = p.get("symbolId") or cid
            side = "Long" if is_long else "Short"
            cur_s = f"{cur:.4f}" if cur is not None else "—"
            pts_s = (f"{'+' if pts >= 0 else ''}{pts} pts" if pts is not None else "—")
            usd_s = (f" (${'+' if usd >= 0 else ''}{usd:,.2f})" if usd is not None else "")
            log(f"  [{aname}] {sym} {side} x{size} @ {avg} | now {cur_s} | {pts_s}{usd_s}")
        if positions:
            have_usd = True
            grand_usd += acct_usd
            log(f"  [{aname}] account unrealized: ${'+' if acct_usd >= 0 else ''}{acct_usd:,.2f}")
    if total_positions == 0:
        log("  All accounts flat.")
    elif have_usd:
        log(f"--- Total unrealized P&L: ${'+' if grand_usd >= 0 else ''}{grand_usd:,.2f} "
            f"across {total_positions} position(s) ---")
    return total_positions


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"token": None, "token_expiry": None, "seen_order_ids": []}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


def get_token(state: dict) -> str:
    if state.get("token") and state.get("token_expiry"):
        expiry = datetime.fromisoformat(state["token_expiry"])
        if datetime.now(timezone.utc) < expiry - timedelta(hours=1):
            return state["token"]
    resp = requests.post(f"{BASE_URL}/Auth/loginKey",
                         json={"userName": USERNAME, "apiKey": API_KEY}, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if not data.get("success"):
        raise RuntimeError(f"Auth failed: {data.get('errorMessage')}")
    state["token"] = data["token"]
    state["token_expiry"] = (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat()
    return state["token"]


def api_post(token: str, endpoint: str, payload: dict) -> dict:
    resp = requests.post(f"{BASE_URL}/{endpoint}", json=payload,
                         headers={"Authorization": f"Bearer {token}",
                                  "Content-Type": "application/json",
                                  "accept": "text/plain"}, timeout=15)
    resp.raise_for_status()
    return resp.json()


def get_last_price(token: str, contract_id: str):
    now = datetime.now(timezone.utc)
    try:
        data = api_post(token, "History/retrieveBars", {
            "contractId": contract_id, "live": False,
            "startTime": (now - timedelta(minutes=10)).isoformat(),
            "endTime": now.isoformat(), "unit": 2, "unitNumber": 1,
            "limit": 3, "includePartialBar": True})
        bars = data.get("bars", [])
        if bars:
            return float(bars[0]["c"])
    except Exception:
        pass
    return None


def send_discord(embeds: list):
    for i in range(0, len(embeds), 10):
        requests.post(DISCORD_WEBHOOK, json={"embeds": embeds[i:i+10]}, timeout=10)


def send_discord_embed(embed: dict, image_bytes: bytes = None, filename: str = "chart.png",
                       logo_bytes: bytes = None):
    """Send a single embed, optionally with an attached chart PNG and/or the
    Skylit logo as the author icon (multipart)."""
    embed = dict(embed)
    files = {}
    if logo_bytes:
        files["file_logo"] = ("skylit.png", logo_bytes, "image/png")
        author = dict(embed.get("author") or {})
        author["icon_url"] = "attachment://skylit.png"
        embed["author"] = author
    if image_bytes:
        files["file_chart"] = (filename, image_bytes, "image/png")
        embed["image"] = {"url": f"attachment://{filename}"}
    if files:
        requests.post(DISCORD_WEBHOOK,
                      data={"payload_json": json.dumps({"embeds": [embed]})},
                      files=files, timeout=20)
    else:
        requests.post(DISCORD_WEBHOOK, json={"embeds": [embed]}, timeout=10)


def fetch_bars(token, contract_id, minutes=180, unit_number=5):
    """Fetch `minutes` of bars at `unit_number`-minute resolution for a chart.
    Default: 3 hours of 5-minute candles."""
    now = datetime.now(timezone.utc)
    try:
        data = api_post(token, "History/retrieveBars", {
            "contractId": contract_id, "live": False,
            "startTime": (now - timedelta(minutes=minutes)).isoformat(),
            "endTime": now.isoformat(), "unit": 2, "unitNumber": unit_number,
            "limit": minutes // max(unit_number, 1) + 5, "includePartialBar": True})
        return data.get("bars", [])
    except Exception as e:
        log(f"      chart: bar fetch failed: {e}")
        return []


def compute_metrics(entry, sl, pt, size, pv, ts, current, is_long):
    """Return a dict of risk/reward + live metrics. Any field may be None."""
    m = {}
    direction = 1 if is_long else -1
    if entry is None:
        return m
    risk_pts = round(abs(entry - sl), 6) if sl is not None else None
    rew_pts  = round(abs(pt - entry), 6) if pt is not None else None
    m["risk_pts"], m["reward_pts"] = risk_pts, rew_pts
    m["risk_usd"]   = round(risk_pts * size * pv, 2) if (risk_pts and pv) else None
    m["reward_usd"] = round(rew_pts * size * pv, 2) if (rew_pts and pv) else None
    m["rr"] = round(rew_pts / risk_pts, 2) if (risk_pts and rew_pts) else None
    if ts:
        m["risk_ticks"]   = round(risk_pts / ts) if risk_pts else None
        m["reward_ticks"] = round(rew_pts / ts) if rew_pts else None
    if current is not None:
        m["unreal_pts"] = round((current - entry) * direction, 6)
        m["unreal_usd"] = round(m["unreal_pts"] * size * pv, 2) if pv else None
        if risk_pts:
            m["r_mult"] = round(m["unreal_pts"] / risk_pts, 2)
        if rew_pts:
            m["pct_to_pt"] = round(max(0.0, m["unreal_pts"]) / rew_pts * 100, 1)
    m["breakeven"] = entry
    return m


def progress_bar(sl, pt, current, is_long, width=22):
    """Text bar showing where `current` sits between SL and PT."""
    if sl is None or pt is None or current is None:
        return None
    lo, hi = (sl, pt) if is_long else (pt, sl)   # left=stop side visually
    if hi == lo:
        return None
    frac = (current - lo) / (hi - lo)
    frac = max(0.0, min(1.0, frac))
    pos = int(round(frac * (width - 1)))
    bar = "".join("●" if i == pos else "─" for i in range(width))
    left_lbl  = f"SL {sl:g}" if is_long else f"PT {pt:g}"
    right_lbl = f"PT {pt:g}" if is_long else f"SL {sl:g}"
    return f"`{left_lbl}` {bar} `{right_lbl}`"


def ensure_chart_libs():
    """Lazy-install plotting libs into the PYTHONPATH target (cached after 1st)."""
    try:
        import mplfinance, pandas, matplotlib  # noqa
        return True
    except ImportError:
        target = next((p for p in os.environ.get("PYTHONPATH", "").split(":") if p), "/work/.pydeps")
        log(f"      chart: installing mplfinance/pandas/matplotlib into {target} (one-time)…")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--target", target,
                                   "mplfinance", "pandas", "matplotlib", "-q",
                                   "--root-user-action=ignore"])
            import importlib
            importlib.invalidate_caches()
            import mplfinance, pandas, matplotlib  # noqa
            return True
        except Exception as e:
            log(f"      chart: lib install failed: {e}")
            return False


def draw_skylit_mark(ax):
    """Draw the Skylit logo mark (ice 'skylight aperture') into a 0..40 axes."""
    import matplotlib.patches as mp
    ax.set_xlim(0, 40); ax.set_ylim(0, 40); ax.axis("off"); ax.set_aspect("equal")
    # rounded ink tile + ice border (y flipped vs the SVG)
    ax.add_patch(mp.FancyBboxPatch((6, 6), 28, 28,
                 boxstyle=mp.BoxStyle("Round", pad=2, rounding_size=8),
                 fc=SKY["ink"], ec=SKY["ice"], lw=1.1, alpha=1.0, joinstyle="round"))
    ax.patches[-1].set_edgecolor((0.565, 0.749, 0.976, 0.45))
    # beam triangle (ice) with ink cut-out, white light point
    ax.add_patch(mp.Polygon([(20, 31), (29, 13), (11, 13)], closed=True,
                            fc=SKY["ice"], ec="none", alpha=0.92))
    ax.add_patch(mp.Polygon([(20, 24), (24.5, 13), (15.5, 13)], closed=True,
                            fc=SKY["ink"], ec="none"))
    ax.add_patch(mp.Circle((20, 28.5), 2.1, fc="#ffffff", ec="none"))


def make_skylit_logo_png():
    """Render the Skylit mark to a small transparent PNG (cached in the state dir)."""
    cache = STATE_FILE.parent / "skylit_mark.png"
    try:
        if cache.exists() and cache.stat().st_size > 0:
            return cache.read_bytes()
    except Exception:
        pass
    if not ensure_chart_libs():
        return None
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig = plt.figure(figsize=(0.8, 0.8), dpi=96)
        fig.patch.set_alpha(0)
        ax = fig.add_axes([0, 0, 1, 1])
        draw_skylit_mark(ax)
        buf = io.BytesIO()
        fig.savefig(buf, dpi=96, transparent=True)
        plt.close(fig)
        data = buf.getvalue()
        try:
            cache.write_bytes(data)
        except Exception:
            pass
        return data
    except Exception as e:
        log(f"      logo: render failed: {e}")
        return None


def make_chart(token, contract_id, sym, is_long, entry, sl, pt, current,
               size=1, pv=None, ts=None, exit_price=None, order_price=None,
               marks=None, title_override=None):
    """Render an annotated trade chart: candles, shaded risk/reward zones,
    labeled level tags, entry marker, session HI/LO. When exit_price is given,
    renders a closed-trade chart (ENTRY + EXIT lines). Returns PNG bytes or None."""
    bars = fetch_bars(token, contract_id, minutes=180, unit_number=5)
    if len(bars) < 3:
        return None
    if not ensure_chart_libs():
        return None
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")
    try:
        import pandas as pd
        import matplotlib
        matplotlib.use("Agg")
        import mplfinance as mpf

        rows = []
        for b in bars:
            t = b.get("t") or b.get("timestamp")
            rows.append({"Date": pd.to_datetime(t, utc=True),
                         "Open": b["o"], "High": b["h"], "Low": b["l"],
                         "Close": b["c"], "Volume": b.get("v", 0) or 0})
        df = pd.DataFrame(rows).set_index("Date").sort_index()
        N = len(df)
        m = compute_metrics(entry, sl, pt, size, pv, ts, current, is_long)
        session_hi = float(df["High"].max())
        session_lo = float(df["Low"].min())

        # Skylit-tinted candles: ice (light blue) up, mist (off-white) down.
        mc = mpf.make_marketcolors(up=SKY["ice"], down=SKY["mist"],
                                   edge={"up": SKY["ice"], "down": SKY["mist"]},
                                   wick={"up": SKY["ice"], "down": SKY["mist"]},
                                   inherit=True)
        style = mpf.make_mpf_style(
            base_mpf_style="nightclouds", marketcolors=mc,
            facecolor=SKY["ink"], figcolor=SKY["ink"], gridcolor=SKY["grid"],
            gridstyle="-", edgecolor=SKY["border"],
            rc={"font.family": "monospace", "text.color": SKY["mist"],
                "axes.labelcolor": SKY["fog"], "xtick.color": SKY["fog"],
                "ytick.color": SKY["fog"], "axes.edgecolor": SKY["border"],
                "axes.linewidth": 0.8, "grid.alpha": 0.5})
        fig, axlist = mpf.plot(df, type="candle", style=style, volume=False,
                               returnfig=True, figsize=(9.6, 5.3),
                               datetime_format="%H:%M", xrotation=0, tight_layout=True,
                               update_width_config=dict(candle_linewidth=0.7, candle_width=0.62))
        ax = axlist[0]
        # TradingView-style: price scale on the right, clean spines.
        ax.yaxis.tick_right(); ax.yaxis.set_label_position("right")
        for s in ("top", "left"):
            ax.spines[s].set_visible(False)

        # Shaded reward (entry→PT) and risk (entry→SL) zones
        if entry is not None and pt is not None:
            ax.axhspan(min(entry, pt), max(entry, pt), color=SKY["bull"], alpha=0.07, zorder=0)
        if entry is not None and sl is not None:
            ax.axhspan(min(entry, sl), max(entry, sl), color=SKY["bear"], alpha=0.08, zorder=0)

        # Labeled level tags in a right-side gutter (dark ink text on accent chips)
        def level(price, label, col):
            if price is None:
                return
            ax.axhline(price, color=col, lw=1.1, ls="--", alpha=0.9, zorder=2)
            ax.text(N + 0.6, price, f" {label} {price:g} ", color=SKY["ink"], va="center",
                    ha="left", fontsize=8, fontweight="bold", zorder=6, family="monospace",
                    bbox=dict(boxstyle="round,pad=0.28", fc=col, ec="none", alpha=0.96))
        level(pt, "PT", SKY["bull"])
        level(entry, "ENTRY", SKY["ice"])
        level(sl, "SL", SKY["bear"])
        if exit_price is not None:
            level(exit_price, "EXIT", SKY["amber"])
        elif current is not None:
            level(current, "NOW", SKY["fog"])
        if order_price is not None:
            level(order_price, "ORDER", SKY["iris"])
        for mk in (marks or []):
            mprice, mlabel, mcol = mk
            if mprice is None:
                continue
            ax.axhline(mprice, color=mcol, lw=1.0, ls="--", alpha=0.85, zorder=2)
            ax.text(N + 0.6, mprice, f" {mlabel} ", color=SKY["ink"], va="center", ha="left",
                    fontsize=7.5, fontweight="bold", zorder=6, family="monospace",
                    bbox=dict(boxstyle="round,pad=0.24", fc=mcol, ec="none", alpha=0.96))

        # Session high/low reference lines (high/low of the charted 3h window)
        def sess_level(price, label):
            ax.axhline(price, color=SKY["slate"], lw=1.0, ls=":", alpha=0.7, zorder=1)
            ax.text(N + 0.6, price, f" {label} {price:g} ", color=SKY["mist"], va="center",
                    ha="left", fontsize=7.5, fontweight="bold", zorder=6, family="monospace",
                    bbox=dict(boxstyle="round,pad=0.24", fc=SKY["slate"], ec="none", alpha=0.95))
        sess_level(session_hi, "HI")
        sess_level(session_lo, "LO")

        # Entry marker on the most recent bar (live trades only)
        if exit_price is None and entry is not None:
            ax.scatter([N - 1], [entry], marker="^" if is_long else "v",
                       color=SKY["ice"], s=90, zorder=7, edgecolors="white", linewidths=0.6)

        # Y-limits padded to always include SL/PT/EXIT/ORDER/marks
        prices = [df["Low"].min(), df["High"].max()] + \
                 [p for p in (sl, pt, current, entry, exit_price, order_price) if p is not None] + \
                 [mk[0] for mk in (marks or []) if mk[0] is not None]
        lo, hi = min(prices), max(prices)
        pad = (hi - lo) * 0.08 or (hi * 0.001)
        ax.set_ylim(lo - pad, hi + pad)
        ax.set_xlim(-1, N - 1 + max(7, N * 0.20))   # room for the gutter

        # Rich title
        side = "LONG" if is_long else "SHORT"
        if title_override is not None:
            l1 = title_override
        elif order_price is not None and entry is None:
            l1 = f"WORKING ORDER   {sym}   {side}  x{size}  @ {order_price:g}"
        elif exit_price is not None:
            l1 = f"CLOSED   {sym}   {side}  x{size}  {entry:g} → {exit_price:g}"
        else:
            l1 = f"{sym}   {side}  x{size}  @ {entry:g}"
        bits = []
        if m.get("rr") is not None:
            bits.append(f"R:R {m['rr']}:1")
        if m.get("risk_usd") is not None:
            bits.append(f"risk ${m['risk_usd']:,.0f}")
        if m.get("reward_usd") is not None:
            bits.append(f"reward ${m['reward_usd']:,.0f}")
        if m.get("unreal_usd") is not None:
            bits.append(f"P/L {_money(m['unreal_usd'])}")
        elif m.get("unreal_pts") is not None:
            bits.append(f"P/L {m['unreal_pts']:+g} pts")
        l2 = "   •   ".join(bits)
        ax.set_title(l1, color=SKY["mist"], fontsize=12, loc="left", pad=22,
                     family="monospace", fontweight="bold")
        if l2:
            ax.text(0.0, 1.012, l2, transform=ax.transAxes, color=SKY["fog"],
                    fontsize=9, ha="left", va="bottom", family="monospace")
        # Skylit logo mark + ice wordmark, bottom-right
        fig.text(0.978, 0.022, "alantiix", ha="right", va="bottom", color=SKY["ice"],
                 alpha=0.7, fontsize=10, family="monospace", fontweight="bold")
        try:
            lax = fig.add_axes([0.978, 0.012, 0.032, 0.06])
            draw_skylit_mark(lax)
        except Exception:
            pass

        buf = io.BytesIO()
        fig.savefig(buf, dpi=120, bbox_inches="tight", facecolor=fig.get_facecolor())
        import matplotlib.pyplot as plt
        plt.close(fig)
        buf.seek(0)
        return buf.getvalue()
    except Exception as e:
        log(f"      chart: render failed: {e}")
        return None


def account_context(token, account):
    """Pull balance, day P&L (if exposed) and open-position count for an account."""
    ctx = {}
    bal = account.get("balance")
    if bal is not None:
        ctx["balance"] = bal
    for k in ("todaysPnL", "todaysPnl", "dayPnL", "dailyPnL", "realizedDayPnl"):
        if account.get(k) is not None:
            ctx["day_pnl"] = account[k]
            break
    try:
        pos = api_post(token, "Position/searchOpen", {"accountId": account["id"]}).get("positions", [])
        ctx["open_positions"] = len(pos)
    except Exception:
        pass
    return ctx


def error_embed(title: str, detail: str) -> dict:
    return {"author": {"name": "Alantiix · ProjectX"},
            "title": f"❌ {title}", "description": f"```{detail[:1800]}```",
            "color": SKY_BEAR, "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": "Alantiix · ProjectX Monitor"}}


def _money(v):
    if v is None:
        return "—"
    return f"${'+' if v >= 0 else '-'}{abs(v):,.2f}"


def fill_embed(account_name, order, pt_price, sl_price, current_price, ctx=None, risk_ctx=None) -> dict:
    symbol  = order.get("symbolId") or order.get("contractId", "Unknown")
    cid     = order.get("contractId", "")
    entry   = order.get("filledPrice")
    size    = order.get("fillVolume") or order.get("size", 1)
    is_long = order.get("side") == SIDE_BUY
    pv      = point_value(cid, order.get("symbolId"))
    ts      = tick_size(cid, order.get("symbolId"))
    m = compute_metrics(entry, sl_price, pt_price, size, pv, ts, current_price, is_long)

    unreal_usd = m.get("unreal_usd")
    unreal_pts = m.get("unreal_pts")
    pnl_val = unreal_usd if unreal_usd is not None else (unreal_pts or 0)
    color = SKY_BULL if pnl_val >= 0 else SKY_BEAR

    def fmt(v): return f"{v:g}" if v is not None else "—"

    fields = [
        {"name": "Account", "value": account_name, "inline": True},
        {"name": "Symbol", "value": symbol, "inline": True},
        {"name": "Side", "value": "\U0001F7E2 Long" if is_long else "\U0001F534 Short", "inline": True},
        {"name": "Entry", "value": fmt(entry), "inline": True},
        {"name": "Size", "value": f"{size} (${pv:g}/pt)" if pv else str(size), "inline": True},
        {"name": "Current", "value": fmt(current_price), "inline": True},
    ]

    # Targets with distance
    if pt_price is not None:
        d = f"  ({m['reward_ticks']}t / {m['reward_pts']:g}pt)" if m.get("reward_ticks") is not None else ""
        fields.append({"name": "🎯 Take Profit", "value": f"{fmt(pt_price)}{d}", "inline": True})
    if sl_price is not None:
        d = f"  ({m['risk_ticks']}t / {m['risk_pts']:g}pt)" if m.get("risk_ticks") is not None else ""
        fields.append({"name": "🛑 Stop Loss", "value": f"{fmt(sl_price)}{d}", "inline": True})
    if m.get("rr") is not None:
        fields.append({"name": "R:R", "value": f"{m['rr']} : 1", "inline": True})

    # Risk / reward dollars
    if m.get("risk_usd") is not None or m.get("reward_usd") is not None:
        fields.append({"name": "Risk → Reward",
                       "value": f"{_money(-(m['risk_usd']) if m.get('risk_usd') else None)} → "
                                f"{_money(m.get('reward_usd'))}", "inline": True})

    # Live P&L + R-multiple + progress to target
    if unreal_pts is not None:
        sign = "+" if unreal_pts >= 0 else ""
        pl = f"{sign}{unreal_pts:g} pts"
        if unreal_usd is not None:
            pl += f"  •  **{_money(unreal_usd)}**"
        if m.get("r_mult") is not None:
            pl += f"  •  {m['r_mult']:+}R"
        fields.append({"name": "Unrealized P/L", "value": pl, "inline": False})

    # Account context
    if ctx:
        bits = []
        if ctx.get("balance") is not None:
            bits.append(f"Balance ${ctx['balance']:,.2f}")
        if ctx.get("day_pnl") is not None:
            bits.append(f"Day P/L {_money(ctx['day_pnl'])}")
        if ctx.get("open_positions") is not None:
            bits.append(f"Open positions: {ctx['open_positions']}")
        if bits:
            fields.append({"name": "Account", "value": "  •  ".join(bits), "inline": False})

    # Risk guardrails (Topstep daily-loss + trailing drawdown)
    if risk_ctx:
        g = risk_ctx
        rl = [f"Day P/L: **{_money(g['net'])}**  (gross {_money(g['realized'])}, fees {_money(-g['fees'])})"]
        dwarn = " ⚠️ stop-out would breach" if g.get("daily_breach") else ""
        rl.append(f"Daily-loss room: {_money(g['daily_room'])} / ${g['daily_loss']:,.0f}{dwarn}")
        if g.get("trailing_room") is not None:
            twarn = " ⚠️ stop-out would breach" if g.get("trail_breach") else ""
            rl.append(f"Trailing room: {_money(g['trailing_room'])}  (floor ${g['trailing_floor']:,.0f}){twarn}")
        if g.get("trade_risk") is not None:
            rl.append(f"This trade risks {_money(-g['trade_risk'])}")
        fields.append({"name": "🛡️ Risk Guardrails", "value": "\n".join(rl), "inline": False})

    # Description: progress bar SL→price→PT
    desc_parts = []
    bar = progress_bar(sl_price, pt_price, current_price, is_long)
    if bar:
        pct = f"  —  {m['pct_to_pt']:g}% to target" if m.get("pct_to_pt") is not None else ""
        desc_parts.append(bar + pct)

    embed = {"author": {"name": "Alantiix · ProjectX"},
             "title": f"\U0001F514 Order Filled — {symbol} {'Long' if is_long else 'Short'}",
             "color": color, "fields": fields,
             "timestamp": order.get("updateTimestamp") or datetime.now(timezone.utc).isoformat(),
             "footer": {"text": f"Alantiix · ProjectX Monitor  •  Order #{order.get('id')}"}}
    if desc_parts:
        embed["description"] = "\n".join(desc_parts)
    return embed


def run_test(token, accounts):
    """Fire ONE synthetic fill alert (real chart + real account context) so the
    formatting can be verified without waiting for a live fill."""
    log("=== TEST MODE: building a synthetic fill alert ===")

    # Borrow a real contract from a working order (best — gives live chart bars),
    # else from an open position. Fall back to a chart-less synthetic.
    borrowed = None
    for account in accounts:
        try:
            orders = api_post(token, "Order/searchOpen", {"accountId": account["id"]}).get("orders", [])
        except Exception:
            orders = []
        if orders:
            o = orders[0]
            borrowed = (account, o.get("contractId", ""), o.get("symbolId"),
                        o.get("side", SIDE_BUY), o.get("size", 1))
            break
    if not borrowed:
        for account in accounts:
            try:
                pos = api_post(token, "Position/searchOpen", {"accountId": account["id"]}).get("positions", [])
            except Exception:
                pos = []
            if pos:
                p = pos[0]
                side = SIDE_BUY if p.get("type") == POS_TYPE_LONG else SIDE_SELL
                borrowed = (account, p.get("contractId", ""), p.get("symbolId"),
                            side, p.get("size", 1))
                break
    if not borrowed:
        account = accounts[0]
        borrowed = (account, "", "F.US.MGC", SIDE_BUY, 1)
        log("  no working orders/positions to borrow — using a chart-less synthetic")

    account, cid, sym, side, size = borrowed
    sym = sym or cid or "F.US.MGC"
    is_long = side == SIDE_BUY
    log(f"  borrowed contract: {sym} ({cid or 'n/a'}) on {account.get('name')}")

    cur = get_last_price(token, cid) if cid else None
    entry = cur if cur is not None else (POINT_VALUES.get(sym) and 4200.0) or 4200.0
    ts = tick_size(cid, sym) or (entry * 0.0005)
    off_sl, off_pt = 12 * ts, 30 * ts          # 12-tick stop, 30-tick target
    if is_long:
        sl, pt = round(entry - off_sl, 6), round(entry + off_pt, 6)
    else:
        sl, pt = round(entry + off_sl, 6), round(entry - off_pt, 6)
    # Nudge "current" a few ticks in profit so live P&L / R-multiple populate.
    cur_show = (entry + 6 * ts) if is_long else (entry - 6 * ts)

    order = {"symbolId": sym, "contractId": cid, "filledPrice": entry,
             "fillVolume": size, "side": side, "id": "TEST",
             "updateTimestamp": datetime.now(timezone.utc).isoformat()}
    ctx = account_context(token, account)

    embed = fill_embed(account.get("name", "TEST"), order, pt, sl, cur_show, ctx)
    embed["title"] = "🧪 [TEST] " + embed["title"]
    embed["footer"] = {"text": "ProjectX Monitor  •  TEST ALERT (synthetic fill)"}
    chart = make_chart(token, cid, sym, is_long, entry, sl, pt, cur_show,
                       size=size, pv=point_value(cid, sym), ts=tick_size(cid, sym)) if cid else None
    log(f"  chart: {'rendered ' + str(len(chart)//1024) + ' KB' if chart else 'skipped (no contract bars)'}")
    send_discord_embed(embed, image_bytes=chart, logo_bytes=make_skylit_logo_png())
    log("=== TEST MODE: alert sent to Discord. Check the channel. ===")


def run_order_test(token, accounts):
    """Replay one of your current working orders as a [TEST] Order Working alert."""
    log("=== ORDER TEST MODE: replaying a current working order ===")
    for account in accounts:
        try:
            orders = api_post(token, "Order/searchOpen", {"accountId": account["id"]}).get("orders", [])
        except Exception:
            orders = []
        if orders:
            o = orders[0]
            aname = account.get("name", str(account["id"]))
            cid = o.get("contractId", "")
            cur = get_last_price(token, cid) if cid else None
            op = o.get("limitPrice") or o.get("stopPrice")
            is_long = o.get("side") == SIDE_BUY
            emb = order_placed_embed(aname, o, cur)
            emb["title"] = "🧪 [TEST] " + emb["title"]
            emb["footer"] = {"text": "Alantiix · ProjectX Monitor  •  TEST (working order)"}
            chart = make_chart(token, cid, o.get("symbolId") or short_symbol(cid), is_long,
                               None, None, None, cur, size=o.get("size", 1),
                               pv=point_value(cid, o.get("symbolId")),
                               ts=tick_size(cid, o.get("symbolId")), order_price=op) if cid else None
            log(f"  replaying working order {o.get('symbolId') or short_symbol(cid)} #{o.get('id')} | chart {'rendered' if chart else 'skipped'}")
            send_discord_embed(emb, image_bytes=chart, logo_bytes=make_skylit_logo_png())
            try:
                send_active_orders_summary(token, account, logo=make_skylit_logo_png())
            except Exception as e:
                log(f"  active-orders summary failed: {e}")
            log("=== ORDER TEST: alert sent to Discord. ===")
            return
    log("  no working orders found to replay")


def run_exit_test(token, accounts):
    """Replay the most recent real closed trade as a [TEST] Position Closed alert."""
    log("=== EXIT TEST MODE: replaying your most recent closed trade ===")
    now = datetime.now(timezone.utc)
    chosen = None
    for account in accounts:
        trades = fetch_trades(token, account["id"], now - timedelta(days=5))
        closes = [t for t in trades if t.get("profitAndLoss") is not None and not t.get("voided")]
        if closes:
            closes.sort(key=lambda t: t.get("creationTimestamp") or "")
            ct = closes[-1]
            opens = [t for t in trades if t.get("profitAndLoss") is None and not t.get("voided")]
            chosen = (account, ct, match_open(ct, opens, set()))
            break
    if not chosen:
        log("  no closed trades in the last 5 days to replay — make a sim trade and close it, then retry")
        return
    account, ct, entry_t = chosen
    aname = account.get("name", str(account["id"]))
    cid = ct.get("contractId", "")
    is_long = ct.get("side") == SIDE_SELL
    emb = exit_embed(aname, ct, entry_t)
    emb["title"] = "🧪 [TEST] " + emb["title"]
    emb["footer"] = {"text": "Alantiix · ProjectX Monitor  •  TEST (replayed close)"}
    chart = make_chart(token, cid, short_symbol(cid), is_long, (entry_t or ct).get("price"),
                       None, None, ct.get("price"), size=ct.get("size", 1),
                       pv=point_value(cid), ts=tick_size(cid), exit_price=ct.get("price")) if cid else None
    log(f"  replaying {short_symbol(cid)} close (trade #{ct.get('id')}) | chart {'rendered' if chart else 'skipped'}")
    send_discord_embed(emb, image_bytes=chart, logo_bytes=make_skylit_logo_png())
    log("=== EXIT TEST: alert sent to Discord. ===")


def match_open(ct, opens, used):
    """Find the opening trade for a close: same contract, opposite side, earlier,
    not already matched. Returns the nearest one or None."""
    ctt = _ts(ct.get("creationTimestamp"))
    cand = [o for o in opens
            if o.get("contractId") == ct.get("contractId")
            and o.get("side") != ct.get("side")
            and o.get("id") not in used
            and (_ts(o.get("creationTimestamp")) is None or ctt is None
                 or _ts(o.get("creationTimestamp")) <= ctt)]
    if not cand:
        return None
    o = max(cand, key=lambda x: x.get("creationTimestamp") or "")
    used.add(o.get("id"))
    return o


def exit_embed(account_name, ct, entry_t):
    cid = ct.get("contractId", "")
    sym = short_symbol(cid)
    pv = point_value(cid)
    pnl = ct.get("profitAndLoss") or 0.0
    fees = round((ct.get("fees") or 0) + (ct.get("commissions") or 0), 2)
    net = round(pnl - fees, 2)
    size = ct.get("size", 1)
    pts = round(pnl / (size * pv), 4) if (pv and size) else None
    exit_px = ct.get("price")
    entry_px = entry_t.get("price") if entry_t else None
    is_long = ct.get("side") == SIDE_SELL        # closing a long = SELL
    win = net >= 0
    color = SKY_BULL if win else SKY_BEAR
    icon = "✅" if win else "🛑"

    def fmt(v): return f"{v:g}" if v is not None else "—"
    fields = [
        {"name": "Account", "value": account_name, "inline": True},
        {"name": "Symbol", "value": sym, "inline": True},
        {"name": "Side", "value": "\U0001F7E2 Long" if is_long else "\U0001F534 Short", "inline": True},
        {"name": "Entry", "value": fmt(entry_px), "inline": True},
        {"name": "Exit", "value": fmt(exit_px), "inline": True},
        {"name": "Size", "value": str(size), "inline": True},
    ]
    pl = f"**{_money(net)}** net"
    if pts is not None:
        pl += f"  •  {pts:+g} pts"
    pl += f"  •  gross {_money(round(pnl, 2))}, fees {_money(-fees)}"
    fields.append({"name": "Realized P/L", "value": pl, "inline": False})

    dur = None
    if entry_t:
        a, b = _ts(entry_t.get("creationTimestamp")), _ts(ct.get("creationTimestamp"))
        if a and b:
            mins = int((b - a).total_seconds() // 60)
            dur = f"{mins}m" if mins < 60 else f"{mins // 60}h {mins % 60}m"
    if dur:
        fields.append({"name": "Time in trade", "value": dur, "inline": True})

    return {"author": {"name": "Alantiix · ProjectX"},
            "title": f"{icon} Trade Closed — {sym} {'Long' if is_long else 'Short'}  {_money(net)}",
            "color": color, "fields": fields,
            "timestamp": ct.get("creationTimestamp") or datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Alantiix · ProjectX Monitor  •  Trade #{ct.get('id')}"}}


def process_closed_trades(token, accounts, state, now):
    """Detect newly-closed trades (profitAndLoss populated) and alert each one.
    Seeds silently on first run so it doesn't backfill 24h of history."""
    seen = set(state.get("seen_trade_ids", []))
    first = not state.get("trades_initialized")
    new_ids, sent, logo = [], 0, None
    for account in accounts:
        aid = account["id"]
        aname = account.get("name", str(aid))
        trades = fetch_trades(token, aid, now - timedelta(hours=24))
        if not trades:
            continue
        for t in trades:
            tid = t.get("id")
            if tid is not None and tid not in seen:
                new_ids.append(tid)
        if first:
            continue
        opens = [t for t in trades if t.get("profitAndLoss") is None and not t.get("voided")]
        used = set()
        closes = [t for t in trades
                  if t.get("profitAndLoss") is not None and not t.get("voided")
                  and t.get("id") not in seen]
        closes.sort(key=lambda t: t.get("creationTimestamp") or "")
        for ct in closes:
            entry_t = match_open(ct, opens, used)
            cid = ct.get("contractId", "")
            is_long = ct.get("side") == SIDE_SELL
            emb = exit_embed(aname, ct, entry_t)
            chart = make_chart(token, cid, short_symbol(cid), is_long,
                               (entry_t or ct).get("price"), None, None, ct.get("price"),
                               size=ct.get("size", 1), pv=point_value(cid),
                               ts=tick_size(cid), exit_price=ct.get("price")) if cid else None
            if logo is None:
                logo = make_skylit_logo_png()
            send_discord_embed(emb, image_bytes=chart, logo_bytes=logo)
            sent += 1
            log(f"  exit alert: {short_symbol(cid)} net {emb['title'].split('  ')[-1]}")
    seen.update(new_ids)
    state["seen_trade_ids"] = list(seen)[-5000:]
    state["trades_initialized"] = True
    if first:
        log(f"  trade history seeded ({len(seen)} ids) — exits alert from next close")
    elif sent:
        log(f"  {sent} closed-trade alert(s) sent")


def order_placed_embed(account_name, order, current=None):
    cid = order.get("contractId", "")
    sym = order.get("symbolId") or short_symbol(cid)
    is_buy = order.get("side") == SIDE_BUY
    otype = ORDER_TYPE_NAMES.get(order.get("type"), f"Type{order.get('type')}")
    size = order.get("size", 1)
    trig = order.get("limitPrice") or order.get("stopPrice")
    trig_kind = "Limit" if order.get("limitPrice") else ("Stop" if order.get("stopPrice") else "Trigger")

    def fmt(v): return f"{v:g}" if v is not None else "—"
    fields = [
        {"name": "Account", "value": account_name, "inline": True},
        {"name": "Symbol", "value": sym, "inline": True},
        {"name": "Action", "value": "\U0001F7E2 Buy" if is_buy else "\U0001F534 Sell", "inline": True},
        {"name": "Type", "value": otype, "inline": True},
        {"name": "Size", "value": str(size), "inline": True},
    ]
    if trig is not None:
        fields.append({"name": f"{trig_kind} Price", "value": fmt(trig), "inline": True})
    if current is not None:
        fields.append({"name": "Current", "value": fmt(current), "inline": True})
        if trig is not None:
            ts = tick_size(cid, order.get("symbolId"))
            dist = abs(current - trig)
            dl = f"{dist:g} pts" + (f"  ({round(dist / ts)} ticks)" if ts else "")
            fields.append({"name": "Distance", "value": dl, "inline": True})
    return {"author": {"name": "Alantiix · ProjectX"},
            "title": f"\U0001F4E5 Order Working — {sym} {'Buy' if is_buy else 'Sell'}",
            "color": SKY_ICE, "fields": fields,
            "timestamp": order.get("creationTimestamp") or datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Alantiix · ProjectX Monitor  •  Order #{order.get('id')}"}}


def process_new_orders(token, accounts, state, now):
    """Alert when a new working/resting order appears (status Open, not filled).
    Seeds silently on first run so it won't backfill existing working orders."""
    seen = set(state.get("seen_open_order_ids", []))
    first = not state.get("open_orders_initialized")
    new_ids, sent, logo = [], 0, None
    for account in accounts:
        aid = account["id"]
        aname = account.get("name", str(aid))
        try:
            orders = api_post(token, "Order/searchOpen", {"accountId": aid}).get("orders", [])
        except Exception as e:
            log(f"  [{aname}] open-order check failed: {e}")
            continue
        acct_new = 0
        for o in orders:
            oid = o.get("id")
            if oid is None or oid in seen:
                continue
            new_ids.append(oid)
            if first:
                continue
            cid = o.get("contractId", "")
            cur = get_last_price(token, cid) if cid else None
            op = o.get("limitPrice") or o.get("stopPrice")
            is_long = o.get("side") == SIDE_BUY
            emb = order_placed_embed(aname, o, cur)
            chart = make_chart(token, cid, o.get("symbolId") or short_symbol(cid), is_long,
                               None, None, None, cur, size=o.get("size", 1),
                               pv=point_value(cid, o.get("symbolId")),
                               ts=tick_size(cid, o.get("symbolId")), order_price=op) if cid else None
            if logo is None:
                logo = make_skylit_logo_png()
            send_discord_embed(emb, image_bytes=chart, logo_bytes=logo)
            sent += 1
            acct_new += 1
            log(f"  new working order: {o.get('symbolId') or short_symbol(cid)} #{oid}")
        # After new order(s): post the full active-orders board + per-symbol charts.
        if acct_new and not first:
            try:
                send_active_orders_summary(token, account, logo=logo)
            except Exception as e:
                log(f"  active-orders summary failed: {e}")
    seen.update(new_ids)
    state["seen_open_order_ids"] = list(seen)[-5000:]
    state["open_orders_initialized"] = True
    if first:
        log(f"  working-order tracking seeded ({len(seen)} ids) — new orders alert from next placement")
    elif sent:
        log(f"  {sent} new working-order alert(s) sent")


def active_orders_embed(account_name, orders):
    by = {}
    for o in orders:
        sym = o.get("symbolId") or short_symbol(o.get("contractId", ""))
        by.setdefault(sym, []).append(o)
    fields, total = [], 0
    for sym in sorted(by):
        lines = []
        for o in sorted(by[sym], key=lambda x: (x.get("limitPrice") or x.get("stopPrice") or 0)):
            t = ORDER_TYPE_NAMES.get(o.get("type"), f"Type{o.get('type')}")
            sd = SIDE_NAMES.get(o.get("side"), "?")
            px = o.get("limitPrice") or o.get("stopPrice")
            tail = f" @ {px:g}" if px is not None else ""
            lines.append(f"`#{o.get('id')}` {sd} {o.get('size', 1)}x · {t}{tail}")
            total += 1
        fields.append({"name": f"{sym}  ({len(by[sym])})", "value": "\n".join(lines), "inline": False})
    return {"author": {"name": "Alantiix · ProjectX"},
            "title": f"\U0001F4CB Active Orders — {total} working",
            "color": SKY_ICE, "fields": fields,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "footer": {"text": f"Alantiix · ProjectX Monitor  •  {account_name}"}}


def send_active_orders_summary(token, account, fill_cid=None, fill_is_long=True, logo=None):
    """After a fill: post a text board of all working orders for the account, then
    one chart per symbol plotting that symbol's resting orders."""
    aname = account.get("name", str(account["id"]))
    try:
        orders = api_post(token, "Order/searchOpen", {"accountId": account["id"]}).get("orders", [])
    except Exception as e:
        log(f"  active-orders fetch failed: {e}")
        return
    if not orders:
        log("  active-orders summary: none working")
        return
    if logo is None:
        logo = make_skylit_logo_png()

    # 1) Text board (all symbols)
    send_discord_embed(active_orders_embed(aname, orders), logo_bytes=logo)

    # 2) One chart per distinct contract/symbol
    by_contract = {}
    for o in orders:
        by_contract.setdefault(o.get("contractId", ""), []).append(o)
    charts = 0
    for cid, group in by_contract.items():
        if not cid:
            continue
        symid = group[0].get("symbolId")
        sym = symid or short_symbol(cid)
        marks = []
        for o in group:
            px = o.get("limitPrice") or o.get("stopPrice")
            if px is None:
                continue
            kind = "L" if o.get("limitPrice") else "S"
            col = SKY["bull"] if o.get("limitPrice") else SKY["bear"]
            marks.append((px, f"{kind} {px:g}", col))
        if not marks:
            continue
        cur = get_last_price(token, cid)
        is_long = group[0].get("side") == SIDE_BUY
        chart = make_chart(token, cid, sym, is_long, None, None, None, cur,
                           pv=point_value(cid, symid), ts=tick_size(cid, symid),
                           marks=marks, title_override=f"ACTIVE ORDERS — {sym}  ({len(marks)})")
        if not chart:
            continue
        cembed = {"author": {"name": "Alantiix · ProjectX"},
                  "title": f"\U0001F4C8 {sym} — {len(marks)} working order(s)",
                  "color": SKY_ICE,
                  "timestamp": datetime.now(timezone.utc).isoformat(),
                  "footer": {"text": f"Alantiix · ProjectX Monitor  •  {aname}"}}
        send_discord_embed(cembed, image_bytes=chart, logo_bytes=logo)
        charts += 1
    log(f"  active-orders summary sent ({len(orders)} order(s) across {charts} chart(s))")


def main():
    _require_config()
    log(f"ProjectX monitor starting | state file: {STATE_FILE}")
    state = load_state()
    try:
        token = get_token(state)
        cached = bool(state.get("token")) and token == state.get("token")
        log(f"Auth OK ({'reused cached token' if cached else 'new login'})")
    except Exception as e:
        log(f"AUTH ERROR: {e}")
        send_discord([error_embed("Auth Error", str(e))])
        save_state(state)
        sys.exit(1)

    seen_ids = set(state.get("seen_order_ids", []))
    new_fill_ids = []
    sent_count = 0
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(minutes=LOOKBACK_MINUTES)
    log(f"Scanning fills from {window_start.strftime('%H:%M:%S')} to {now.strftime('%H:%M:%S')} UTC "
        f"| {len(seen_ids)} order id(s) already seen")

    try:
        accounts_data = api_post(token, "Account/search", {"onlyActiveAccounts": True})
    except Exception as e:
        log(f"ACCOUNT FETCH ERROR: {e}")
        send_discord([error_embed("Account Fetch Error", str(e))])
        save_state(state)
        sys.exit(1)

    accounts = accounts_data.get("accounts", [])
    log(f"Active accounts: {len(accounts)}")
    if not accounts:
        save_state(state)
        log("Done — no active accounts.")
        return

    # ── Test mode: set PROJECTX_TEST=1 to fire one synthetic fill alert (with a
    #    real chart) so you can verify the formatting without waiting for a fill.
    if os.environ.get("PROJECTX_TEST") == "1":
        run_test(token, accounts)
        save_state(state)
        return

    # ── Exit test: set PROJECTX_TEST_EXIT=1 to replay your most recent closed
    #    trade as a [TEST] Position Closed alert (real numbers + chart).
    if os.environ.get("PROJECTX_TEST_EXIT") == "1":
        run_exit_test(token, accounts)
        save_state(state)
        return

    # ── Order test: set PROJECTX_TEST_ORDER=1 to replay a current working order
    #    as a [TEST] Order Working alert.
    if os.environ.get("PROJECTX_TEST_ORDER") == "1":
        run_order_test(token, accounts)
        save_state(state)
        return

    # ── Debug dump: set PROJECTX_DEBUG=1 to print everything the API currently
    #    returns (working orders + open positions), regardless of fill window.
    if os.environ.get("PROJECTX_DEBUG") == "1":
        for account in accounts:
            aid = account["id"]
            aname = account.get("name", str(aid))
            log(f"=== DEBUG dump for {aname} (id {aid}) ===")
            try:
                openo = api_post(token, "Order/searchOpen", {"accountId": aid}).get("orders", [])
                log(f"  Open/working orders: {len(openo)}")
                for o in openo:
                    log(f"    order #{o.get('id')} status={o.get('status')} "
                        f"contract={o.get('contractId')} type={o.get('type')} "
                        f"side={o.get('side')} size={o.get('size')} "
                        f"limit={o.get('limitPrice')} stop={o.get('stopPrice')}")
            except Exception as e:
                log(f"  Order/searchOpen failed: {e}")
            try:
                pos = api_post(token, "Position/searchOpen", {"accountId": aid}).get("positions", [])
                log(f"  Open positions: {len(pos)}")
                for p in pos:
                    log(f"    pos contract={p.get('contractId')} size={p.get('size')} "
                        f"avgPrice={p.get('averagePrice')} type={p.get('type')}")
            except Exception as e:
                log(f"  Position/searchOpen failed: {e}")
            try:
                recent = api_post(token, "Order/search", {
                    "accountId": aid,
                    "startTimestamp": (now - timedelta(hours=24)).isoformat(),
                    "endTimestamp": now.isoformat()}).get("orders", [])
                from collections import Counter
                counts = Counter(o.get("status") for o in recent)
                log(f"  Orders in last 24h: {len(recent)} | status counts: {dict(counts)}")
            except Exception as e:
                log(f"  Order/search (24h) failed: {e}")

            # Full raw ACCOUNT object — looking for balance / day-P&L / limit fields
            log(f"  ACCOUNT RAW: {json.dumps(account, default=str)}")

            # Full raw POSITION objects — confirm field names for size/avg/type
            try:
                pos2 = api_post(token, "Position/searchOpen", {"accountId": aid}).get("positions", [])
                for p in pos2[:3]:
                    log(f"  POSITION RAW: {json.dumps(p, default=str)}")
            except Exception as e:
                log(f"  Position raw dump failed: {e}")

            # Trade/search sample — the source for realized P&L on a close
            for ep in ("Trade/search", "Trade/searchHalfTurn"):
                try:
                    tr = api_post(token, ep, {
                        "accountId": aid,
                        "startTimestamp": (now - timedelta(hours=24)).isoformat(),
                        "endTimestamp": now.isoformat()})
                    trades = tr.get("trades") or tr.get("halfTurns") or tr.get("data") or []
                    log(f"  {ep}: {len(trades)} trade(s)")
                    for t in trades[:3]:
                        log(f"    TRADE RAW: {json.dumps(t, default=str)}")
                    break
                except Exception as e:
                    log(f"  {ep} failed: {e}")
        log("=== DEBUG dump complete — exiting without alerting ===")
        save_state(state)
        return

    for account in accounts:
        account_id   = account["id"]
        account_name = account.get("name", str(account_id))
        try:
            orders_resp = api_post(token, "Order/search", {
                "accountId": account_id,
                "startTimestamp": window_start.isoformat(),
                "endTimestamp": now.isoformat()})
        except Exception as e:
            log(f"  [{account_name}] order search failed: {e}")
            continue

        new_fills = [o for o in orders_resp.get("orders", [])
                     if o.get("status") == STATUS_FILLED and o["id"] not in seen_ids]
        if not new_fills:
            log(f"  [{account_name}] no new fills")
            continue
        log(f"  [{account_name}] {len(new_fills)} new fill(s)")
        try:
            open_orders = api_post(token, "Order/searchOpen",
                                   {"accountId": account_id}).get("orders", [])
        except Exception:
            open_orders = []
        ctx = account_context(token, account)
        for order in new_fills:
            cid = order.get("contractId", "")
            brackets = [o for o in open_orders if o.get("contractId") == cid]
            pt = next((o["limitPrice"] for o in brackets if o.get("limitPrice")), None)
            sl = next((o["stopPrice"] for o in brackets if o.get("stopPrice")), None)
            cur = get_last_price(token, cid)
            sym  = order.get("symbolId") or cid
            is_long = order.get("side") == SIDE_BUY
            side = "Long" if is_long else "Short"
            log(f"      -> {sym} {side} @ {order.get('filledPrice')} "
                f"x{order.get('fillVolume') or order.get('size', 1)} (order #{order.get('id')})")
            fill_size = order.get("fillVolume") or order.get("size", 1)
            risk_ctx = None
            limits = account_limits(account)
            if limits:
                trades_day = fetch_trades(token, account_id, day_window_start(now))
                pvv = point_value(cid, order.get("symbolId"))
                trisk = None
                if sl is not None and order.get("filledPrice") is not None and pvv:
                    trisk = round(abs(order["filledPrice"] - sl) * fill_size * pvv, 2)
                risk_ctx = risk_guardrails(account, limits, trades_day,
                                           account.get("balance"), trisk, state)
            embed = fill_embed(account_name, order, pt, sl, cur, ctx, risk_ctx)
            chart = make_chart(token, cid, sym, is_long, order.get("filledPrice"), sl, pt, cur,
                               size=fill_size, pv=point_value(cid, order.get("symbolId")),
                               ts=tick_size(cid, order.get("symbolId")))
            if chart:
                log(f"      chart rendered ({len(chart)//1024} KB)")
            send_discord_embed(embed, image_bytes=chart, logo_bytes=make_skylit_logo_png())
            sent_count += 1
            new_fill_ids.append(order["id"])

    if sent_count:
        log(f"Sent {sent_count} Discord alert(s)")

    # New working-order alerts (resting orders that just appeared).
    try:
        process_new_orders(token, accounts, state, now)
    except Exception as e:
        log(f"new-order detection error: {e}")

    # Exit/lifecycle alerts from the trade stream (closed positions).
    try:
        process_closed_trades(token, accounts, state, now)
    except Exception as e:
        log(f"exit detection error: {e}")

    # Live snapshot of current open positions + unrealized P&L (console status).
    report_positions(token, accounts)
    # Live snapshot of working/resting orders (status Open) — e.g. resting stops.
    report_open_orders(token, accounts)

    seen_ids.update(new_fill_ids)
    state["seen_order_ids"] = list(seen_ids)[-2000:]
    save_state(state)
    log(f"Done — announced {sent_count} fill(s). Exit 0.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        log("UNHANDLED ERROR:\n" + tb)
        try:
            send_discord([error_embed("Unhandled Monitor Error", tb)])
        except Exception:
            pass
        sys.exit(1)
