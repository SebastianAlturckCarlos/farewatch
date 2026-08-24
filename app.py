#!/usr/bin/env python3
"""
app.py - Flask server for the Farewatch PWA.

    pip install flask fast-flights
    python3 app.py

Then open http://<your-machine-ip>:5055 on your phone (same wifi) and use
Share -> Add to Home Screen. It installs as a standalone app and shows the
last known fare instantly, even with no signal.

Keep the daily cron running too -- the app reads history, cron builds it:
    0 9 * * *  cd ~/farewatch && /usr/bin/python3 farewatch.py >> farewatch.log 2>&1
"""

import socket
import sqlite3
import threading
from flask import Flask, jsonify, send_from_directory

import farewatch as fw

PORT = 5055


def lan_ip():
    """Find the address other devices on this network can reach us at.

    Opens a UDP socket toward a public address to see which local interface
    the OS would route through. No packets are actually sent.
    """
    for probe in ("8.8.8.8", "1.1.1.1"):
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.connect((probe, 80))
            ip = s.getsockname()[0]
            if not ip.startswith("127."):
                return ip
        except OSError:
            continue
        finally:
            s.close()
    return None


def banner():
    ip = lan_ip()
    url = f"http://{ip}:{PORT}" if ip else None
    line = "=" * 52
    print(f"\n{line}\n  Farewatch is running\n{line}")
    print(f"\n  On this computer:  http://localhost:{PORT}")
    if url:
        print(f"  On your phone:     {url}")
        print("\n  Open that on your phone (same wifi), then:")
        print("    iPhone  - Share -> Add to Home Screen")
        print("    Android - menu  -> Install app / Add to Home screen")
        try:
            import qrcode
            print()
            q = qrcode.QRCode(border=1)
            q.add_data(url)
            q.print_ascii(invert=True)
            print("  ...or just scan that with your phone camera.")
        except ImportError:
            print("\n  Tip: pip install qrcode  -> prints a scannable QR code here.")
    else:
        print("  Could not detect a LAN address. Are you connected to wifi?")
    print(f"\n{line}\n  Ctrl-C to stop.\n")

app = Flask(__name__, static_folder="static", static_url_path="")

# fast-flights fetches are slow and not thread-safe enough to run concurrently.
_refresh_lock = threading.Lock()


def _conn():
    c = sqlite3.connect(fw.DB_PATH)
    fw.init_db(c)
    return c


def _active():
    return [t for t in fw.TRIPS if t.get("enabled")]


@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/api/status")
def status():
    """Read-only. Fast. Never touches the network."""
    conn = _conn()
    try:
        return jsonify({"trips": [fw.stats(conn, t) for t in _active()]})
    finally:
        conn.close()


@app.route("/api/refresh", methods=["POST"])
def refresh():
    """Fetch live fares now. Takes 5-20s -- the UI shows a pending state."""
    if not _refresh_lock.acquire(blocking=False):
        return jsonify({"error": "A refresh is already running."}), 409
    try:
        conn = _conn()
        try:
            errors = []
            for trip in _active():
                try:
                    if fw.record(conn, trip) is None:
                        errors.append(f"{trip['name']}: no fares returned")
                except Exception as e:
                    errors.append(f"{trip['name']}: {type(e).__name__}")
            payload = {"trips": [fw.stats(conn, t) for t in _active()]}
            if errors:
                payload["warning"] = "; ".join(errors)
            return jsonify(payload)
        finally:
            conn.close()
    finally:
        _refresh_lock.release()


if __name__ == "__main__":
    banner()
    # 0.0.0.0 so your phone can reach it over the LAN.
    app.run(host="0.0.0.0", port=PORT, debug=False)
