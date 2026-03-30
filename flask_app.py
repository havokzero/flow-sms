import base64
import json
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

from main import (
    ATTACHMENTS_DIR,
    DETAIL_DUMPS_DIR,
    PROBE_DIR,
    SETTINGS,
    NUMBER_LABELS,
    callback_url,
    clear_all_data,
    flowroute_request,
    get_message_detail,
    get_messages,
    load_log_records,
    save_settings,
    send_mms_v21,
    setting_int,
    setting_str,
)
from receive_sms import process_sms_message
from receive_mms import process_mms_message


def create_app() -> Flask:
    app = Flask(__name__)

    def render_notice(kind: str, message: str) -> str:
        if not message:
            return ""

        styles = {
            "ok": "background:#18361f;color:#7df29b;border:1px solid #285235;",
            "fail": "background:#3a1c1c;color:#ffb4b4;border:1px solid #6a2d2d;",
            "info": "background:#1a2638;color:#a8d1ff;border:1px solid #294567;",
        }
        style = styles.get(kind, styles["info"])
        return (
            f'<div style="margin-bottom:14px;padding:10px 12px;'
            f'border-radius:10px;{style}">{message}</div>'
        )

    def message_stats(records: list[dict]) -> dict[str, int]:
        total = len(records)
        sms = 0
        mms = 0
        with_media = 0

        for record in records:
            msg = record.get("message", {})
            attrs = msg.get("attributes", {}) if isinstance(msg, dict) else {}
            media = record.get("media", []) if isinstance(record.get("media"), list) else []
            is_mms = bool(attrs.get("is_mms", False))

            if is_mms:
                mms += 1
            else:
                sms += 1

            if media:
                with_media += 1

        return {
            "total": total,
            "sms": sms,
            "mms": mms,
            "with_media": with_media,
        }

    def bulkvs_base_url() -> str:
        value = setting_str("bulkvs_api_url")
        return value.rstrip("/") if value else "https://portal.bulkvs.com/api/v1.0"

    def bulkvs_auth_header() -> str:
        username = setting_str("bulkvs_username")
        token = setting_str("bulkvs_token")
        raw = f"{username}:{token}".encode("utf-8")
        return "Basic " + base64.b64encode(raw).decode("ascii")

    def bulkvs_is_configured() -> bool:
        return bool(setting_str("bulkvs_username") and setting_str("bulkvs_token"))

    def bulkvs_request(method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        if not bulkvs_is_configured():
            return 0, {"error": "BulkVS credentials missing"}

        url = f"{bulkvs_base_url()}{path}"
        headers = {
            "Authorization": bulkvs_auth_header(),
            "Accept": "application/json",
        }

        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode("utf-8")

        req = Request(url, headers=headers, data=data, method=method)
        try:
            with urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
                try:
                    parsed = json.loads(raw) if raw else {}
                except Exception:
                    parsed = {"raw": raw}
                return resp.status, parsed
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            try:
                parsed = json.loads(raw) if raw else {}
            except Exception:
                parsed = {"raw": raw}
            return exc.code, parsed
        except URLError as exc:
            return 0, {"error": str(exc)}

    def save_bulkvs_payload(prefix: str, payload: object) -> str:
        PROBE_DIR.mkdir(exist_ok=True)
        stamp = datetime.utcnow().strftime("%Y%m%dT%H%M%S%fZ")
        path = PROBE_DIR / f"{prefix}_{stamp}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False)
        return str(path)

    def extract_bulkvs_message(payload: dict) -> dict:
        direction = str(payload.get("direction", "inbound")).strip().lower() or "inbound"
        from_num = (
            payload.get("from")
            or payload.get("fromNumber")
            or payload.get("src")
            or payload.get("source")
            or ""
        )
        to_num = (
            payload.get("to")
            or payload.get("toNumber")
            or payload.get("dst")
            or payload.get("destination")
            or ""
        )
        body = payload.get("body") or payload.get("message") or payload.get("text") or ""
        msg_id = (
            payload.get("id")
            or payload.get("messageId")
            or payload.get("mdr")
            or payload.get("record_id")
            or f"bulkvs-{datetime.utcnow().timestamp()}"
        )
        ts = (
            payload.get("timestamp")
            or payload.get("time")
            or payload.get("created_at")
            or datetime.utcnow().isoformat() + "Z"
        )

        media_urls = []
        for key in ("media", "media_urls", "attachments", "files", "mms", "mediaUrls"):
            value = payload.get(key)
            if isinstance(value, list):
                for item in value:
                    if isinstance(item, str):
                        media_urls.append(item)
                    elif isinstance(item, dict):
                        maybe_url = (
                            item.get("url")
                            or item.get("media_url")
                            or item.get("download_url")
                            or item.get("href")
                        )
                        if maybe_url:
                            media_urls.append(maybe_url)

        is_mms = bool(media_urls) or bool(payload.get("is_mms")) or bool(payload.get("mms"))

        message = {
            "id": str(msg_id),
            "attributes": {
                "body": body,
                "status": payload.get("status", "received"),
                "direction": direction,
                "to": str(to_num),
                "from": str(from_num),
                "is_mms": is_mms,
                "timestamp": ts,
                "message_type": "bulkvs",
            },
        }

        if media_urls:
            message["included"] = []
            message["relationships"] = {"media": {"data": []}}
            for idx, url in enumerate(media_urls, start=1):
                media_id = f"{msg_id}-{idx}"
                message["relationships"]["media"]["data"].append({"id": media_id, "type": "media"})
                message["included"].append(
                    {
                        "id": media_id,
                        "type": "media",
                        "attributes": {
                            "file_name": f"bulkvs_media_{idx}",
                            "file_size": None,
                            "mime_type": "",
                            "url": url,
                        },
                        "links": {
                            "self": url,
                        },
                    }
                )

        return message

    @app.get("/")
    def index():
        records = list(reversed(load_log_records(limit=300)))
        stats = message_stats(records)
        live_refresh_seconds = max(2, setting_int("live_refresh_seconds"))

        cards = []
        for record in records:
            msg = record.get("message", {})
            attrs = msg.get("attributes", {}) if isinstance(msg, dict) else {}
            media = record.get("media", []) if isinstance(record.get("media"), list) else []

            msg_id = str(msg.get("id", ""))
            body = attrs.get("body")
            body_text = "(empty)" if body in [None, ""] else str(body)
            from_num = attrs.get("from", "")
            to_num = attrs.get("to", "")
            status = attrs.get("status", "") or "(none)"
            ts = attrs.get("timestamp", "") or ""
            is_mms = bool(attrs.get("is_mms", False))
            provider = attrs.get("message_type", "") or record.get("source", "")
            badge = "MMS" if is_mms else "SMS"
            badge_class = "mms" if is_mms else "sms"

            media_html = ""
            if media:
                parts = []
                for item in media:
                    name = item.get("file_name") or "(unnamed)"
                    mime = item.get("mime_type") or "(unknown mime)"
                    local_path = item.get("local_path") or ""
                    url = item.get("url") or ""
                    self_link = item.get("self_link") or ""

                    if local_path:
                        file_name = local_path.split("\\")[-1].split("/")[-1]
                        href = f"/attachments/{file_name}"
                        if str(mime).startswith("image/"):
                            parts.append(
                                f"""
                                <div class="media-item">
                                  <div class="media-name">{name}</div>
                                  <a href="{href}" target="_blank">
                                    <img src="{href}" alt="{name}" class="media-preview">
                                  </a>
                                </div>
                                """
                            )
                        else:
                            parts.append(
                                f"""
                                <div class="media-item">
                                  <div class="media-name">{name}</div>
                                  <a href="{href}" target="_blank">Open attachment</a>
                                </div>
                                """
                            )
                    elif url:
                        parts.append(
                            f"""
                            <div class="media-item">
                              <div class="media-name">{name}</div>
                              <a href="{url}" target="_blank">Open media URL</a>
                            </div>
                            """
                        )
                    else:
                        extra = (
                            f'<div style="margin-top:6px;"><a href="{self_link}" target="_blank">'
                            f'Provider self link</a></div>'
                            if self_link else ""
                        )
                        parts.append(
                            f"""
                            <div class="media-item">
                              <div class="media-name">{name}</div>
                              <div class="media-missing">No signed media URL provided.</div>
                              {extra}
                            </div>
                            """
                        )
                media_html = f'<div class="media-wrap">{"".join(parts)}</div>'

            cards.append(
                f"""
                <div class="msg-card">
                  <div class="msg-head">
                    <div class="msg-left">
                      <span class="badge {badge_class}">{badge}</span>
                      <span class="msg-id">{msg_id}</span>
                    </div>
                    <div class="msg-tools">
                      <span class="msg-time">{ts}</span>
                      <span style="color:#91a0b4;font-size:12px;">{provider}</span>
                      <a href="/api/detail/{msg_id}" target="_blank">detail</a>
                      <a href="/api/probe/{msg_id}" target="_blank">probe</a>
                    </div>
                  </div>
                  <div class="msg-meta"><strong>From:</strong> {from_num}</div>
                  <div class="msg-meta"><strong>To:</strong> {to_num}</div>
                  <div class="msg-meta"><strong>Status:</strong> {status}</div>
                  <div class="msg-body">{body_text}</div>
                  {media_html}
                </div>
                """
            )

        html = f"""
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8">
          <title>FlowSMS Viewer</title>
          <style>
            body {{
              font-family: Arial, sans-serif;
              background: #0f1115;
              color: #e8ecf1;
              margin: 0;
              padding: 24px;
            }}
            .wrap {{
              max-width: 1250px;
              margin: 0 auto;
            }}
            .topbar {{
              display: flex;
              justify-content: space-between;
              align-items: flex-start;
              gap: 16px;
              margin-bottom: 18px;
              flex-wrap: wrap;
            }}
            .subtitle {{
              color: #9aa4b2;
              margin-top: 6px;
            }}
            .actions {{
              display: flex;
              flex-wrap: wrap;
              gap: 8px;
            }}
            .actions a, .actions button {{
              display: inline-block;
              padding: 10px 14px;
              border-radius: 8px;
              border: none;
              text-decoration: none;
              background: #243042;
              color: #fff;
              cursor: pointer;
            }}
            .actions button:hover, .actions a:hover {{
              background: #314157;
            }}
            .stats {{
              display: grid;
              grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
              gap: 12px;
              margin-bottom: 18px;
            }}
            .stat {{
              background: #171a21;
              border: 1px solid #2a3040;
              border-radius: 12px;
              padding: 14px;
            }}
            .stat-label {{
              color: #9aa4b2;
              font-size: 12px;
              margin-bottom: 6px;
              text-transform: uppercase;
              letter-spacing: 0.04em;
            }}
            .stat-value {{
              font-size: 24px;
              font-weight: bold;
            }}
            .msg-card {{
              background: #171a21;
              border: 1px solid #2a3040;
              border-radius: 14px;
              padding: 16px;
              margin-bottom: 16px;
            }}
            .msg-head {{
              display: flex;
              justify-content: space-between;
              align-items: center;
              gap: 12px;
              margin-bottom: 12px;
              flex-wrap: wrap;
            }}
            .msg-left {{
              display: flex;
              gap: 10px;
              align-items: center;
              flex-wrap: wrap;
            }}
            .msg-id {{
              font-family: Consolas, monospace;
              color: #aab4c5;
              font-size: 12px;
            }}
            .msg-tools {{
              display: flex;
              gap: 10px;
              align-items: center;
              flex-wrap: wrap;
            }}
            .msg-tools a {{
              color: #8bc5ff;
            }}
            .msg-time {{
              color: #97a3b6;
              font-size: 12px;
            }}
            .badge {{
              display: inline-block;
              font-size: 12px;
              font-weight: bold;
              padding: 4px 8px;
              border-radius: 999px;
            }}
            .badge.sms {{
              background: #18361f;
              color: #7df29b;
            }}
            .badge.mms {{
              background: #351a38;
              color: #f19bff;
            }}
            .msg-meta {{
              color: #c8d0db;
              margin-bottom: 6px;
            }}
            .msg-body {{
              margin-top: 10px;
              background: #11141a;
              border: 1px solid #262d39;
              border-radius: 10px;
              padding: 12px;
              white-space: pre-wrap;
              word-break: break-word;
            }}
            .media-wrap {{
              display: flex;
              flex-wrap: wrap;
              gap: 16px;
              margin-top: 14px;
            }}
            .media-item {{
              background: #121720;
              border: 1px solid #262d39;
              border-radius: 10px;
              padding: 10px;
              max-width: 340px;
            }}
            .media-name {{
              margin-bottom: 8px;
              color: #d9e0ea;
              font-size: 13px;
              word-break: break-word;
            }}
            .media-preview {{
              max-width: 300px;
              max-height: 300px;
              border-radius: 8px;
              display: block;
            }}
            .media-missing {{
              color: #ffb4b4;
              font-size: 13px;
            }}
            .footer-note {{
              color: #748094;
              font-size: 12px;
              margin-top: 16px;
            }}
            a {{
              color: #8bc5ff;
              text-decoration: none;
            }}
            a:hover {{
              text-decoration: underline;
            }}
          </style>
          <script>
            let autoRefreshEnabled = true;
            const refreshMs = {live_refresh_seconds * 1000};

            async function postAndReload(url) {{
              await fetch(url, {{ method: "POST" }});
              window.location.reload();
            }}

            async function pollNow() {{
              const btn = document.getElementById("poll-now-btn");
              btn.disabled = true;
              btn.textContent = "Polling...";
              try {{
                const res = await fetch("/api/poll-now", {{ method: "POST" }});
                const data = await res.json();
                console.log(data);
              }} catch (e) {{
                console.error(e);
              }}
              window.location.reload();
            }}

            function toggleAutoRefresh() {{
              autoRefreshEnabled = !autoRefreshEnabled;
              const btn = document.getElementById("toggle-refresh-btn");
              btn.textContent = autoRefreshEnabled ? "Pause refresh" : "Resume refresh";
            }}

            setInterval(() => {{
              if (autoRefreshEnabled) {{
                window.location.reload();
              }}
            }}, refreshMs);
          </script>
        </head>
        <body>
          <div class="wrap">
            <div class="topbar">
              <div>
                <h1 style="margin:0;">FlowSMS Viewer</h1>
                <div class="subtitle">Flowroute webhook: {callback_url() or "(not set)"}</div>
                <div class="subtitle">BulkVS webhook: /webhook/bulkvs</div>
                <div class="subtitle">Live refresh every {live_refresh_seconds}s</div>
              </div>
              <div class="actions">
                <a href="/settings">Settings</a>
                <a href="/api/messages" target="_blank">JSON</a>
                <a href="/api/account-numbers" target="_blank">Flowroute numbers</a>
                <a href="/api/did-labels" target="_blank">DID labels</a>
                <a href="/api/bulkvs/account" target="_blank">BulkVS account</a>
                <a href="/api/bulkvs/mdr?type=sms" target="_blank">BulkVS MDR SMS</a>
                <a href="/api/bulkvs/mdr?type=mms" target="_blank">BulkVS MDR MMS</a>
                <button id="poll-now-btn" onclick="pollNow()">Poll now</button>
                <button id="toggle-refresh-btn" onclick="toggleAutoRefresh()">Pause refresh</button>
                <button onclick="postAndReload('/api/clear')">Clear inbox</button>
                <button onclick="postAndReload('/api/clear-probes')">Clear probes</button>
              </div>
            </div>

            <div class="stats">
              <div class="stat">
                <div class="stat-label">Total messages</div>
                <div class="stat-value">{stats["total"]}</div>
              </div>
              <div class="stat">
                <div class="stat-label">SMS</div>
                <div class="stat-value">{stats["sms"]}</div>
              </div>
              <div class="stat">
                <div class="stat-label">MMS</div>
                <div class="stat-value">{stats["mms"]}</div>
              </div>
              <div class="stat">
                <div class="stat-label">With media metadata</div>
                <div class="stat-value">{stats["with_media"]}</div>
              </div>
            </div>

            {''.join(cards) or '<p>No messages yet.</p>'}

            <div class="footer-note">
              Frontend refresh and backend polling are separate. Lower poll interval in settings if you want near-real-time behavior.
            </div>
          </div>
        </body>
        </html>
        """
        return html

    @app.get("/settings")
    def settings_page():
        discord_test = request.args.get("discord_test", "").strip()
        discord_msg = request.args.get("msg", "").strip()
        save_status = request.args.get("save", "").strip()
        bulkvs_test = request.args.get("bulkvs_test", "").strip()
        bulkvs_msg = request.args.get("bulkvs_msg", "").strip()

        notice_html = ""
        if save_status == "ok":
            notice_html += render_notice("ok", "Settings saved.")
        elif save_status == "fail":
            notice_html += render_notice("fail", "Settings save failed.")

        if discord_test == "ok":
            notice_html += render_notice("ok", "Discord webhook test succeeded.")
        elif discord_test == "fail":
            notice_html += render_notice("fail", f"Discord webhook test failed: {discord_msg}")

        if bulkvs_test == "ok":
            notice_html += render_notice("ok", f"BulkVS connectivity test succeeded: {bulkvs_msg}")
        elif bulkvs_test == "fail":
            notice_html += render_notice("fail", f"BulkVS connectivity test failed: {bulkvs_msg}")

        html = f"""
        <!doctype html>
        <html>
        <head>
          <meta charset="utf-8">
          <title>FlowSMS Settings</title>
          <style>
            body {{
              font-family: Arial, sans-serif;
              background:#0f1115;
              color:#e8ecf1;
              padding:24px;
            }}
            .wrap {{
              max-width: 1000px;
              margin: 0 auto;
            }}
            .panel {{
              background:#171a21;
              border:1px solid #2a3040;
              border-radius:14px;
              padding:20px;
              margin-bottom:18px;
            }}
            label {{
              display:block;
              margin-top:14px;
              margin-bottom:6px;
            }}
            input {{
              width:100%;
              padding:10px;
              border-radius:8px;
              border:1px solid #334155;
              background:#0f141b;
              color:#fff;
            }}
            .row {{
              display:grid;
              grid-template-columns: 1fr 1fr;
              gap: 14px;
            }}
            button, a.btn {{
              display:inline-block;
              margin-top:16px;
              margin-right:8px;
              padding:10px 14px;
              border-radius:8px;
              background:#243042;
              color:#fff;
              border:none;
              text-decoration:none;
              cursor:pointer;
            }}
            .muted {{
              color:#91a0b4;
              font-size:12px;
              margin-top:6px;
            }}
            h2 {{
              margin-top: 0;
            }}
          </style>
        </head>
        <body>
          <div class="wrap">
            <h1>Settings</h1>
            {notice_html}

            <div class="panel">
              <h2>Flowroute</h2>
              <form method="post" action="/settings">
                <div class="row">
                  <div>
                    <label>Flowroute Access Key</label>
                    <input name="flowroute_access_key" value="{setting_str('flowroute_access_key')}">
                  </div>
                  <div>
                    <label>Flowroute Secret Key</label>
                    <input name="flowroute_secret_key" value="{setting_str('flowroute_secret_key')}">
                  </div>
                </div>

                <div class="row">
                  <div>
                    <label>Webhook Token</label>
                    <input name="webhook_token" value="{setting_str('webhook_token')}">
                  </div>
                  <div>
                    <label>Discord Webhook URL</label>
                    <input name="discord_webhook_url" value="{setting_str('discord_webhook_url')}">
                  </div>
                </div>

                <label>Public Base URL</label>
                <input name="public_base_url" value="{setting_str('public_base_url')}">
                <div class="muted">Expected Flowroute callback: {callback_url() or "(not set yet)"}</div>

                <label>Default DID</label>
                <input name="default_phone_number" value="{setting_str('default_phone_number')}">
                <div class="muted">Convenience default only. Monitoring still covers all owned numbers.</div>

                <div class="row">
                  <div>
                    <label>Poll Interval Seconds</label>
                    <input name="poll_interval_seconds" value="{setting_int('poll_interval_seconds')}">
                  </div>
                  <div>
                    <label>Poll Limit</label>
                    <input name="poll_limit" value="{setting_int('poll_limit')}">
                  </div>
                </div>

                <div class="row">
                  <div>
                    <label>Start Lookback Minutes</label>
                    <input name="start_lookback_minutes" value="{setting_int('start_lookback_minutes')}">
                  </div>
                  <div>
                    <label>Live Refresh Seconds</label>
                    <input name="live_refresh_seconds" value="{setting_int('live_refresh_seconds')}">
                  </div>
                </div>

                <button type="submit">Save settings</button>
                <a class="btn" href="/">Back</a>
              </form>

              <form method="post" action="/api/test-discord">
                <button type="submit">Send Discord test</button>
              </form>
            </div>

            <div class="panel">
              <h2>BulkVS</h2>
              <form method="post" action="/settings">
                <div class="row">
                  <div>
                    <label>BulkVS API URL</label>
                    <input name="bulkvs_api_url" value="{setting_str('bulkvs_api_url') or 'https://portal.bulkvs.com/api/v1.0'}">
                  </div>
                  <div>
                    <label>BulkVS Username</label>
                    <input name="bulkvs_username" value="{setting_str('bulkvs_username')}">
                  </div>
                </div>

                <div class="row">
                  <div>
                    <label>BulkVS Token / Password</label>
                    <input name="bulkvs_token" value="{setting_str('bulkvs_token')}">
                  </div>
                  <div>
                    <label>BulkVS Webhook Token</label>
                    <input name="bulkvs_webhook_token" value="{setting_str('bulkvs_webhook_token')}">
                  </div>
                </div>

                <div class="muted">Expected BulkVS callback path: /webhook/bulkvs?token=YOUR_BULKVS_WEBHOOK_TOKEN</div>

                <button type="submit">Save BulkVS settings</button>
              </form>

              <form method="post" action="/api/test-bulkvs">
                <button type="submit">Test BulkVS account API</button>
              </form>
            </div>
          </div>
        </body>
        </html>
        """
        return html

    @app.post("/settings")
    def save_settings_route():
        numeric_keys = {"poll_interval_seconds", "poll_limit", "start_lookback_minutes", "live_refresh_seconds"}
        updates = {}

        for key in SETTINGS.keys():
            if key in request.form:
                val = request.form.get(key, "")
                updates[key] = int(val) if key in numeric_keys else val

        save_settings(updates)
        return redirect("/settings?save=ok")

    @app.get("/api/messages")
    def api_messages():
        return jsonify(load_log_records(limit=500))

    @app.get("/api/detail/<record_id>")
    def api_detail(record_id: str):
        detail = get_message_detail(record_id)
        if not detail:
            return jsonify({"status": "error", "message": "not found"}), 404
        return jsonify(detail)

    @app.get("/api/probe/<record_id>")
    def api_probe(record_id: str):
        path = PROBE_DIR / f"probe_{record_id}.json"
        if not path.exists():
            return jsonify({"status": "error", "message": "probe not found"}), 404
        return jsonify(json.loads(path.read_text(encoding="utf-8")))

    @app.get("/api/account-numbers")
    def api_account_numbers():
        status, data = flowroute_request(
            "GET",
            "/v2/numbers?limit=200",
            None,
            {"Accept": "application/vnd.api+json"},
        )
        return jsonify({"http_status": status, "response": data}), status if status else 500

    @app.get("/api/did-labels")
    def api_did_labels():
        return jsonify(NUMBER_LABELS)

    @app.get("/api/bulkvs/account")
    def api_bulkvs_account():
        status, data = bulkvs_request("GET", "/accountDetail")
        return jsonify({"http_status": status, "response": data}), status if status else 500

    @app.get("/api/bulkvs/mdr")
    def api_bulkvs_mdr():
        msg_type = request.args.get("type", "sms").strip().lower()
        if msg_type not in {"sms", "mms"}:
            return jsonify({"status": "error", "message": "type must be sms or mms"}), 400

        status, data = bulkvs_request("GET", f"/mdr?type={msg_type}")
        return jsonify({"http_status": status, "response": data}), status if status else 500

    @app.post("/api/clear")
    def api_clear():
        clear_all_data()
        return jsonify({"status": "ok"})

    @app.post("/api/clear-probes")
    def api_clear_probes():
        for folder in [PROBE_DIR, DETAIL_DUMPS_DIR]:
            folder.mkdir(exist_ok=True)
            for child in folder.iterdir():
                if child.is_file():
                    child.unlink()
        return jsonify({"status": "ok"})

    @app.post("/api/test-discord")
    def api_test_discord():
        from main import discord_post
        ok, result = discord_post("FlowSMS test ping from settings page.")
        if ok:
            return redirect("/settings?discord_test=ok")
        return redirect(f"/settings?discord_test=fail&msg={result}")

    @app.post("/api/test-bulkvs")
    def api_test_bulkvs():
        status, data = bulkvs_request("GET", "/accountDetail")
        if status == 200:
            return redirect("/settings?bulkvs_test=ok&bulkvs_msg=HTTP%20200")
        return redirect(f"/settings?bulkvs_test=fail&bulkvs_msg=HTTP%20{status}")

    @app.post("/api/poll-now")
    def api_poll_now():
        processed = 0
        messages = get_messages(limit=setting_int("poll_limit"))

        for item in messages:
            if not isinstance(item, dict):
                continue
            attrs = item.get("attributes", {})
            if not isinstance(attrs, dict):
                continue
            if str(attrs.get("direction", "")).strip().lower() != "inbound":
                continue

            if bool(attrs.get("is_mms", False)):
                process_mms_message(item, source="manual_poll", webhook_payload=None)
            else:
                process_sms_message(item, source="manual_poll")

            processed += 1

        return jsonify({
            "status": "ok",
            "processed_candidates": processed,
            "time": datetime.utcnow().isoformat() + "Z"
        })

    @app.post("/api/send-mms-test")
    def api_send_mms_test():
        payload = request.get_json(silent=True) or {}
        from_number = str(payload.get("from", "")).strip()
        to_number = str(payload.get("to", "")).strip()
        body = str(payload.get("body", "")).strip()
        media_urls = payload.get("media_urls", [])

        if not from_number or not to_number or not isinstance(media_urls, list) or not media_urls:
            return jsonify({"status": "error", "message": "from, to, and media_urls[] are required"}), 400

        status, data = send_mms_v21(from_number, to_number, body, media_urls)
        return jsonify({"http_status": status, "response": data}), status if status else 500

    @app.get("/attachments/<path:filename>")
    def attachment(filename: str):
        target = ATTACHMENTS_DIR / filename
        if not target.exists():
            abort(404)
        return send_from_directory(ATTACHMENTS_DIR, filename)

    @app.route("/webhook", methods=["GET", "POST"])
    def webhook():
        if request.method == "GET":
            return jsonify({"status": "ok", "message": "Flowroute webhook receiver running"})

        token = request.args.get("token", "").strip()
        expected = setting_str("webhook_token")
        if expected and token != expected:
            return jsonify({"status": "error", "message": "invalid token"}), 403

        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify({"status": "error", "message": "invalid json"}), 400

        data = payload.get("data", {})
        if not isinstance(data, dict):
            return jsonify({"status": "error", "message": "missing data object"}), 400

        attrs = data.get("attributes", {})
        if not isinstance(attrs, dict):
            return jsonify({"status": "error", "message": "missing attributes object"}), 400

        message_id = str(data.get("id", "")).strip()
        if not message_id:
            return jsonify({"status": "error", "message": "missing message id"}), 400

        if "included" in payload and isinstance(payload["included"], list):
            data["included"] = payload["included"]

        if bool(attrs.get("is_mms", False)):
            process_mms_message(data, source="webhook", webhook_payload=payload)
        else:
            process_sms_message(data, source="webhook")

        return jsonify({"status": "ok", "id": message_id})

    @app.route("/webhook/bulkvs", methods=["GET", "POST"])
    def bulkvs_webhook():
        if request.method == "GET":
            return jsonify({"status": "ok", "message": "BulkVS webhook receiver running"})

        expected = setting_str("bulkvs_webhook_token")
        token = request.args.get("token", "").strip()
        if expected and token != expected:
            return jsonify({"status": "error", "message": "invalid token"}), 403

        payload = request.get_json(silent=True)
        if payload is None:
            payload = request.form.to_dict() if request.form else None

        if payload is None:
            return jsonify({"status": "error", "message": "invalid or empty payload"}), 400

        save_path = save_bulkvs_payload("bulkvs_webhook", payload)

        if not isinstance(payload, dict):
            return jsonify({"status": "ok", "saved": save_path, "note": "non-dict payload saved only"})

        parsed = extract_bulkvs_message(payload)

        attrs = parsed.get("attributes", {})
        if bool(attrs.get("is_mms", False)):
            process_mms_message(parsed, source="bulkvs_webhook", webhook_payload=payload)
        else:
            process_sms_message(parsed, source="bulkvs_webhook")

        return jsonify({"status": "ok", "saved": save_path, "id": parsed.get("id")})

    return app