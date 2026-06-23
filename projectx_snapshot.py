#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""
ProjectX Pre-Flatten Snapshot — Cronmaster edition.
Run ~5 minutes before 4:30 PM ET flatten.
Captures all open orders across all accounts and saves them to a shared file
so the 6 PM resubmit job can replay them.

Logs every step to stdout so Cronmaster's log viewer shows live status.
State/snapshot dir is auto-selected: first writable of
  $PROJECTX_STATE_DIR, /app/data, ~/.
Use the SAME dir for the resubmit job so the handoff works.
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


def main():
    _require_config()
    log(f"Pre-flatten snapshot starting | snapshot file: {SNAPSHOT_FILE}")
    state = load_state()
    try:
        token = get_token(state)
        save_state(state)
        log("Auth OK")
    except Exception as e:
        log(f"AUTH ERROR: {e}")
        send_discord([{"title": "❌ Snapshot Auth Error", "description": f"```{e}```",
                       "color": SKY_BEAR, "timestamp": datetime.now(timezone.utc).isoformat()}])
        sys.exit(1)

    accounts = api_post(token, "Account/search", {"onlyActiveAccounts": True}).get("accounts", [])
    log(f"Active accounts: {len(accounts)}")

    snapshot = {"captured_at": datetime.now(timezone.utc).isoformat(), "accounts": []}
    total_orders = 0
    embed_fields = []

    for account in accounts:
        account_id   = account["id"]
        account_name = account.get("name", str(account_id))
        open_orders  = api_post(token, "Order/searchOpen", {"accountId": account_id}).get("orders", [])
        if not open_orders:
            log(f"  [{account_name}] no open orders")
            continue
        log(f"  [{account_name}] captured {len(open_orders)} open order(s)")
        snapshot["accounts"].append({"accountId": account_id, "accountName": account_name,
                                     "orders": open_orders})
        total_orders += len(open_orders)
        order_lines = []
        for o in open_orders:
            type_name = ORDER_TYPE_NAMES.get(o.get("type"), f"Type{o.get('type')}")
            side_name = SIDE_NAMES.get(o.get("side"), "?")
            symbol    = o.get("symbolId") or o.get("contractId", "?")
            price_str = ""
            if o.get("limitPrice"):
                price_str = f" @ Limit {o['limitPrice']}"
            elif o.get("stopPrice"):
                price_str = f" @ Stop {o['stopPrice']}"
            line = f"#{o['id']} {side_name} {o.get('size',1)}x {symbol} [{type_name}{price_str}]"
            log(f"      {line}")
            order_lines.append("`" + line + "`")
        embed_fields.append({"name": f"📋 {account_name}  ({len(open_orders)} orders)",
                             "value": "\n".join(order_lines), "inline": False})

    SNAPSHOT_FILE.write_text(json.dumps(snapshot, indent=2))
    log(f"Snapshot written: {total_orders} order(s) total -> {SNAPSHOT_FILE}")

    if total_orders == 0:
        send_discord([{"title": "📸 Snapshot — No Open Orders Found",
                       "description": "Nothing to resubmit at 6 PM.", "color": SKY_AMBER,
                       "timestamp": datetime.now(timezone.utc).isoformat(),
                       "footer": {"text": "Alantiix · ProjectX Monitor"}}])
    else:
        send_discord([{"title": f"📸 Snapshot Captured — {total_orders} Open Order(s)",
                       "description": "Will resubmit at **6:00 PM ET** when market reopens.",
                       "color": SKY_ICE, "fields": embed_fields,
                       "timestamp": datetime.now(timezone.utc).isoformat(),
                       "footer": {"text": "Alantiix · ProjectX Monitor"}}])
    log(f"Done — {total_orders} order(s) snapshotted. Exit 0.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        log("UNHANDLED ERROR:\n" + tb)
        try:
            send_discord([{"title": "❌ Snapshot Error", "description": f"```{tb[:1800]}```",
                           "color": SKY_BEAR, "timestamp": datetime.now(timezone.utc).isoformat()}])
        except Exception:
            pass
        sys.exit(1)
