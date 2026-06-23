#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
ProjectX 6 PM Order Resubmit — Cronmaster edition.
Reads the snapshot written by projectx_snapshot_cronmaster.py at 4:18 PM and
re-places each captured open order via the ProjectX API.

Logs every step to stdout so Cronmaster's log viewer shows live status.
MUST use the same persist dir as the snapshot job (same $PROJECTX_STATE_DIR /
-v mount) so it can find the snapshot file.
"""

import os
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

ORDER_TYPE_NAMES = {1: "Limit", 2: "Market", 4: "Stop", 5: "TrailingStop", 6: "JoinBid", 7: "JoinAsk"}
SIDE_NAMES       = {0: "Buy", 1: "Sell"}

# ── Skylit Design System palette + logo ───────────────────────────────────────
SKY = {"ink": "#0A0A0A", "ice": "#90BFF9", "bull": "#34D399",
       "bear": "#F87171", "amber": "#FBBF24", "mist": "#E8E8E8"}
SKY_ICE, SKY_BULL, SKY_BEAR, SKY_AMBER = 0x90BFF9, 0x34D399, 0xF87171, 0xFBBF24


def log(msg: str):
    ts = datetime.now(timezone.utc).strftime("%H:%M:%S")
    print(f"[{ts}] {msg}", flush=True)


def _ensure_mpl():
    try:
        import matplotlib  # noqa
        return True
    except ImportError:
        target = next((p for p in os.environ.get("PYTHONPATH", "").split(":") if p), "/work/.pydeps")
        try:
            subprocess.check_call([sys.executable, "-m", "pip", "install", "--target", target,
                                   "matplotlib", "-q", "--root-user-action=ignore"])
            import importlib; importlib.invalidate_caches()
            import matplotlib  # noqa
            return True
        except Exception:
            return False


def _draw_skylit_mark(ax):
    import matplotlib.patches as mp
    ax.set_xlim(0, 40); ax.set_ylim(0, 40); ax.axis("off"); ax.set_aspect("equal")
    ax.add_patch(mp.FancyBboxPatch((6, 6), 28, 28,
                 boxstyle=mp.BoxStyle("Round", pad=2, rounding_size=8),
                 fc=SKY["ink"], ec=(0.565, 0.749, 0.976, 0.45), lw=1.1, joinstyle="round"))
    ax.add_patch(mp.Polygon([(20, 31), (29, 13), (11, 13)], closed=True, fc=SKY["ice"], ec="none", alpha=0.92))
    ax.add_patch(mp.Polygon([(20, 24), (24.5, 13), (15.5, 13)], closed=True, fc=SKY["ink"], ec="none"))
    ax.add_patch(mp.Circle((20, 28.5), 2.1, fc="#ffffff", ec="none"))


def make_skylit_logo_png():
    cache = _DIR / "skylit_mark.png"
    try:
        if cache.exists() and cache.stat().st_size > 0:
            return cache.read_bytes()
    except Exception:
        pass
    if not _ensure_mpl():
        return None
    os.environ.setdefault("MPLCONFIGDIR", "/tmp/mpl")
    try:
        import matplotlib; matplotlib.use("Agg")
        import matplotlib.pyplot as plt, io as _io
        fig = plt.figure(figsize=(0.8, 0.8), dpi=96); fig.patch.set_alpha(0)
        ax = fig.add_axes([0, 0, 1, 1]); _draw_skylit_mark(ax)
        buf = _io.BytesIO(); fig.savefig(buf, dpi=96, transparent=True); plt.close(fig)
        data = buf.getvalue()
        try:
            cache.write_bytes(data)
        except Exception:
            pass
        return data
    except Exception:
        return None


def _persist_dir() -> Path:
    for d in [os.environ.get("PROJECTX_STATE_DIR"), "/app/data", str(Path.home())]:
        if not d:
            continue
        try:
            p = Path(d)
            p.mkdir(parents=True, exist_ok=True)
            t = p / ".px_write_test"
            t.write_text("ok"); t.unlink()
            return p
        except Exception:
            continue
    return Path("/tmp")


_DIR          = _persist_dir()
SNAPSHOT_FILE = _DIR / "projectx_order_snapshot.json"
STATE_FILE    = _DIR / "projectx_monitor_state.json"


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


def send_discord(embeds: list, logo_bytes=None):
    """Brand every embed (Skylit author + logo icon + footer) and send."""
    if logo_bytes is None:
        logo_bytes = make_skylit_logo_png()
    for e in embeds:
        a = dict(e.get("author") or {})
        a.setdefault("name", "Alantiix · ProjectX")
        if logo_bytes:
            a["icon_url"] = "attachment://skylit.png"
        e["author"] = a
        e.setdefault("footer", {"text": "Alantiix · ProjectX Monitor"})
    if logo_bytes:
        requests.post(DISCORD_WEBHOOK,
                      data={"payload_json": json.dumps({"embeds": embeds[:10]})},
                      files={"file_logo": ("skylit.png", logo_bytes, "image/png")}, timeout=20)
    else:
        for i in range(0, len(embeds), 10):
            requests.post(DISCORD_WEBHOOK, json={"embeds": embeds[i:i+10]}, timeout=10)


def place_order(token: str, account_id: int, order: dict):
    payload = {"accountId": account_id, "contractId": order["contractId"],
               "type": order["type"], "side": order["side"], "size": order["size"],
               "limitPrice": order.get("limitPrice"), "stopPrice": order.get("stopPrice"),
               "trailPrice": order.get("trailPrice"), "customTag": None}
    try:
        data = api_post(token, "Order/place", payload)
        if data.get("success"):
            return True, data.get("orderId"), None
        return False, None, data.get("errorMessage", "Unknown error")
    except Exception as e:
        return False, None, str(e)


def main():
    _require_config()
    log(f"6 PM resubmit starting | snapshot file: {SNAPSHOT_FILE}")
    if not SNAPSHOT_FILE.exists():
        log("NO SNAPSHOT FOUND — did the 4:18 PM snapshot job run with the same -v mount?")
        send_discord([{"title": "⚠️ Resubmit — No Snapshot Found",
                       "description": f"Expected `{SNAPSHOT_FILE}` but it doesn't exist.\nDid the 4:18 PM snapshot task run?",
                       "color": SKY_AMBER, "timestamp": datetime.now(timezone.utc).isoformat(),
                       "footer": {"text": "Alantiix · ProjectX Monitor"}}])
        sys.exit(1)

    try:
        snapshot = json.loads(SNAPSHOT_FILE.read_text())
    except Exception as e:
        log(f"SNAPSHOT PARSE ERROR: {e}")
        send_discord([{"title": "❌ Resubmit — Snapshot Parse Error", "description": f"```{e}```",
                       "color": SKY_BEAR, "timestamp": datetime.now(timezone.utc).isoformat()}])
        sys.exit(1)

    captured_at     = snapshot.get("captured_at", "unknown time")
    account_entries = snapshot.get("accounts", [])
    log(f"Snapshot captured at {captured_at} | {len(account_entries)} account(s) with orders")

    if not account_entries:
        log("Snapshot had no orders — nothing to resubmit.")
        send_discord([{"title": "📭 Resubmit — Snapshot Was Empty",
                       "description": f"Snapshot from {captured_at} had no open orders. Nothing to resubmit.",
                       "color": SKY_AMBER, "timestamp": datetime.now(timezone.utc).isoformat(),
                       "footer": {"text": "Alantiix · ProjectX Monitor"}}])
        return

    state = load_state()
    try:
        token = get_token(state)
        save_state(state)
        log("Auth OK")
    except Exception as e:
        log(f"AUTH ERROR: {e}")
        send_discord([{"title": "❌ Resubmit Auth Error", "description": f"```{e}```",
                       "color": SKY_BEAR, "timestamp": datetime.now(timezone.utc).isoformat()}])
        sys.exit(1)

    embed_fields = []
    total_placed = 0
    total_failed = 0

    for entry in account_entries:
        account_id   = entry["accountId"]
        account_name = entry.get("accountName", str(account_id))
        orders       = entry.get("orders", [])
        result_lines = []
        log(f"  [{account_name}] resubmitting {len(orders)} order(s)")
        for order in orders:
            orig_id   = order["id"]
            type_name = ORDER_TYPE_NAMES.get(order.get("type"), f"Type{order.get('type')}")
            side_name = SIDE_NAMES.get(order.get("side"), "?")
            symbol    = order.get("symbolId") or order.get("contractId", "?")
            price_str = ""
            if order.get("limitPrice"):
                price_str = f" @ {order['limitPrice']}"
            elif order.get("stopPrice"):
                price_str = f" @ {order['stopPrice']}"
            label = f"{side_name} {order.get('size', 1)}x {symbol} [{type_name}{price_str}]"
            success, new_id, err = place_order(token, account_id, order)
            if success:
                total_placed += 1
                log(f"      OK   #{new_id} — {label} (was #{orig_id})")
                result_lines.append(f"✅ `#{new_id}` — {label}  *(was #{orig_id})*")
            else:
                total_failed += 1
                log(f"      FAIL — {label} | {err}")
                result_lines.append(f"❌ FAILED — {label}\n  ↳ `{err}`")
        embed_fields.append({"name": f"📋 {account_name}  ({len(orders)} orders)",
                             "value": "\n".join(result_lines) or "—", "inline": False})

    if total_failed == 0:
        color, title = SKY_BULL, f"✅ Resubmit Complete — {total_placed} Order(s) Placed"
    elif total_placed == 0:
        color, title = SKY_BEAR, f"❌ Resubmit Failed — All {total_failed} Order(s) Failed"
    else:
        color, title = SKY_AMBER, f"⚠️ Resubmit Partial — {total_placed} Placed, {total_failed} Failed"

    send_discord([{"title": title, "description": f"Replayed snapshot from **{captured_at}**",
                   "color": color, "fields": embed_fields,
                   "timestamp": datetime.now(timezone.utc).isoformat(),
                   "footer": {"text": "Alantiix · ProjectX Monitor"}}])

    # Rename so it can't replay tomorrow by mistake.
    SNAPSHOT_FILE.rename(SNAPSHOT_FILE.with_suffix(".json.used"))
    log(f"Done — {total_placed} placed, {total_failed} failed. Snapshot archived. Exit 0.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        log("UNHANDLED ERROR:\n" + tb)
        try:
            send_discord([{"title": "❌ Resubmit Unhandled Error", "description": f"```{tb[:1800]}```",
                           "color": SKY_BEAR, "timestamp": datetime.now(timezone.utc).isoformat()}])
        except Exception:
            pass
        sys.exit(1)
