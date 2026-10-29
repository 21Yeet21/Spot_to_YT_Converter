"""Authenticated Flask routes exposing only the sanitized host-monitor snapshot."""
from __future__ import annotations

import json
import os
import time
from pathlib import Path

from flask import jsonify

STATUS_PATH = Path(os.environ.get("PRIVACY_MONITOR_STATUS_PATH", "/monitoring/status.json"))


def register_privacy_monitor(app):
    @app.get("/api/privacy/status")
    def api_privacy_status():
        try:
            payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            collected_at = float(payload.get("collected_at", 0))
        except FileNotFoundError:
            return jsonify({"status": "unavailable", "error": "Host monitor has not written its first snapshot yet."}), 503
        except (OSError, ValueError, json.JSONDecodeError):
            return jsonify({"status": "unavailable", "error": "Host monitor snapshot is unreadable."}), 503

        age = max(0, round(time.time() - collected_at, 1))
        payload["snapshot_age_seconds"] = age
        payload["snapshot_stale"] = age > 75
        payload["status"] = "stale" if age > 75 else "ok"
        return jsonify(payload)
