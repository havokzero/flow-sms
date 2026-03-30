import base64
import json
import mimetypes
import threading
import time
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

# Prevent duplicate module state when running `python main.py`
# and other files import `main`.
sys.modules["main"] = sys.modules[__name__]

BASE_DIR = Path(__file__).resolve().parent
SETTINGS_PATH = BASE_DIR / "settings.json"
LOG_PATH = BASE_DIR / "received_messages.log"
SEEN_IDS_PATH = BASE_DIR / "seen_ids.json"
DETAIL_DUMPS_DIR = BASE_DIR / "detail_dumps"
ATTACHMENTS_DIR = BASE_DIR / "attachments"
PROBE_DIR = BASE_DIR / "probe_results"

ATTACHMENTS_DIR.mkdir(exist_ok=True)
DETAIL_DUMPS_DIR.mkdir(exist_ok=True)
PROBE_DIR.mkdir(exist_ok=True)

DEFAULT_SETTINGS = {
    "host": "0.0.0.0",
    "port": 8080,
    "public_base_url": "",
    "webhook_token": "",
    "discord_webhook_url": "",
    "flowroute_access_key": "",
    "flowroute_secret_key": "",
    "default_phone_number": "",
    "poll_interval_seconds": 10,
    "poll_limit": 25,
    "start_lookback_minutes": 1440,
    "auto_poll": True,
    "quiet_success": True,
    "live_refresh_seconds": 5,
    "terminal_message_debug": False,
    "bulkvs_api_url": "https://portal.bulkvs.com/api/v1.0",
    "bulkvs_username": "",
    "bulkvs_token": "",
    "bulkvs_webhook_token": "",
    "bulkvs_soap_wsdl": "https://portal.bulkvs.com/api?wsdl",
    "bulkvs_soap_key": "",
    "bulkvs_soap_secret": "",
    "callback_scope": "number",
    "auto_webhook_server": True,
}

JSON_API_HEADERS = {
    "Accept": "application/vnd.api+json",
    "Content-Type": "application/vnd.api+json",
}


class C:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    GRAY = "\033[90m"


LOCK = threading.Lock()
NUMBER_LABELS: dict[str, str] = {}
POLL_THREAD_STARTED = False


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def utc_now_str() -> str:
    return utc_now().isoformat()


def ensure_settings_file() -> None:
    if not SETTINGS_PATH.exists():
        SETTINGS_PATH.write_text(json.dumps(DEFAULT_SETTINGS, indent=2), encoding="utf-8")


def load_settings() -> dict[str, Any]:
    ensure_settings_file()
    with SETTINGS_PATH.open("r", encoding="utf-8") as f:
        raw = json.load(f)
    merged = dict(DEFAULT_SETTINGS)
    merged.update(raw if isinstance(raw, dict) else {})
    return merged


SETTINGS = load_settings()


def save_settings(new_values: dict[str, Any]) -> dict[str, Any]:
    global SETTINGS
    merged = dict(SETTINGS)
    merged.update(new_values)
    SETTINGS_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    SETTINGS = merged
    return SETTINGS


def setting_str(key: str) -> str:
    return str(SETTINGS.get(key, "")).strip()


def setting_int(key: str) -> int:
    try:
        return int(SETTINGS.get(key, DEFAULT_SETTINGS[key]))
    except Exception:
        return int(DEFAULT_SETTINGS[key])


def setting_bool(key: str) -> bool:
    return bool(SETTINGS.get(key, DEFAULT_SETTINGS[key]))


def auth_header() -> str:
    access = setting_str("flowroute_access_key")
    secret = setting_str("flowroute_secret_key")
    raw = f"{access}:{secret}".encode("utf-8")
    return "Basic " + base64.b64encode(raw).decode("ascii")


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


def callback_url() -> str:
    base = setting_str("public_base_url").rstrip("/")
    token = setting_str("webhook_token")
    if not base:
        return ""
    if token:
        return f"{base}/webhook?token={token}"
    return f"{base}/webhook"


def bulkvs_callback_url() -> str:
    base = setting_str("public_base_url").rstrip("/")
    token = setting_str("bulkvs_webhook_token")
    if not base:
        return ""
    if token:
        return f"{base}/webhook/bulkvs?token={token}"
    return f"{base}/webhook/bulkvs"


def append_log(record: dict[str, Any]) -> None:
    with LOCK:
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_log_records(limit: int = 200) -> list[dict[str, Any]]:
    if not LOG_PATH.exists():
        return []

    with LOCK:
        lines = LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()

    records: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict):
                records.append(obj)
        except Exception:
            continue
    return records


def load_seen_ids() -> set[str]:
    if not SEEN_IDS_PATH.exists():
        return set()
    try:
        with SEEN_IDS_PATH.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return set(data if isinstance(data, list) else [])
    except Exception:
        return set()


def save_seen_ids(seen_ids: set[str]) -> None:
    with LOCK:
        with SEEN_IDS_PATH.open("w", encoding="utf-8") as f:
            json.dump(sorted(seen_ids), f, indent=2)


SEEN_IDS = load_seen_ids()


def safe_filename(name: str) -> str:
    cleaned = "".join(ch for ch in str(name) if ch.isalnum() or ch in ("-", "_", ".", " "))
    cleaned = cleaned.strip().replace(" ", "_")
    return cleaned or "attachment"


def dump_json_to_file(folder: Path, prefix: str, record_id: str, payload: dict[str, Any]) -> str:
    folder.mkdir(exist_ok=True)
    safe_id = safe_filename(record_id or "unknown")
    target = folder / f"{prefix}_{safe_id}.json"
    with target.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    return str(target)


def flowroute_request(
    method: str,
    path: str,
    body: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, Any]:
    url = f"https://api.flowroute.com{path}"
    payload = None
    headers = dict(JSON_API_HEADERS)
    headers["Authorization"] = auth_header()

    if extra_headers:
        headers.update(extra_headers)

    if body is not None:
        payload = json.dumps(body).encode("utf-8")

    req = Request(url, data=payload, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(raw) if raw else {}
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(raw) if raw else {}
        except Exception:
            parsed = {"raw": raw}
        return exc.code, parsed
    except URLError as exc:
        return 0, {"error": str(exc)}


def bulkvs_request(method: str, path: str, body: dict[str, Any] | None = None) -> tuple[int, Any]:
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


def discord_post(content: str) -> tuple[bool, str]:
    webhook = setting_str("discord_webhook_url")
    if not webhook:
        return False, "Discord webhook URL is empty"

    payload = json.dumps({"content": content}).encode("utf-8")
    req = Request(
        webhook,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "FlowSMS/1.0",
        },
        method="POST",
    )

    try:
        with urlopen(req, timeout=10) as resp:
            resp.read()
            return True, f"HTTP {resp.status}"
    except HTTPError as exc:
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return False, f"HTTP {exc.code}: {body or exc.reason}"
    except Exception as exc:
        return False, str(exc)


def normalize_number(num: str) -> str:
    digits = "".join(ch for ch in str(num) if ch.isdigit())
    if len(digits) == 10:
        digits = "1" + digits
    return digits


def format_local_timestamp(ts: str) -> str:
    if not ts:
        return "unknown"
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        local_dt = dt.astimezone()
        return local_dt.strftime("%Y-%m-%d %I:%M:%S %p %Z")
    except Exception:
        return ts


def did_label(number: str) -> str:
    num = normalize_number(number)
    label = NUMBER_LABELS.get(num, "")
    return f"{label} ({num})" if label else num


def populate_number_labels() -> None:
    status, data = flowroute_request(
        "GET",
        "/v2/numbers?limit=200",
        None,
        {"Accept": "application/vnd.api+json"},
    )
    if status != 200:
        print(f"{C.RED}[numbers] failed to load account numbers: HTTP {status}{C.RESET}")
        return

    NUMBER_LABELS.clear()
    for item in data.get("data", []):
        if not isinstance(item, dict):
            continue
        attrs = item.get("attributes", {})
        if not isinstance(attrs, dict):
            continue

        value = normalize_number(str(attrs.get("value", "")).strip())
        alias = str(attrs.get("alias", "") or "").strip()
        number_type = str(attrs.get("number_type", "") or "").strip()
        state = str(attrs.get("state", "") or "").upper()
        rate_center = str(attrs.get("rate_center", "") or "").strip()

        parts = []
        if alias:
            parts.append(alias)
        if number_type:
            parts.append(number_type)
        if state:
            parts.append(state)
        if rate_center:
            parts.append(rate_center)

        NUMBER_LABELS[value] = " | ".join(parts)


def guess_extension(file_name: str, mime_type: str) -> str:
    suffix = Path(file_name).suffix
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    if guessed:
        return guessed
    return ".bin"


def get_message_detail(record_id: str) -> dict[str, Any] | None:
    status, data = flowroute_request(
        "GET",
        f"/v2.1/messages/{record_id}",
        None,
        {"Accept": "application/vnd.api+json"},
    )

    if isinstance(data, dict):
        dump_json_to_file(DETAIL_DUMPS_DIR, "message_detail", record_id, data)

    if status != 200:
        print(f"{C.RED}[message_detail] HTTP {status} for {record_id}{C.RESET}")
        return None

    if not isinstance(data, dict):
        return None

    message = data.get("data", {})
    if not isinstance(message, dict):
        return None

    included = data.get("included", [])
    if isinstance(included, list):
        message["included"] = included

    return message


def send_mms_v21(from_number: str, to_number: str, body: str, media_urls: list[str]) -> tuple[int, Any]:
    payload = {
        "data": {
            "type": "message",
            "attributes": {
                "from": from_number,
                "to": to_number,
                "body": body,
                "is_mms": "true",
                "media_urls": media_urls,
            }
        }
    }
    return flowroute_request(
        "POST",
        "/v2.1/messages",
        payload,
        {"Content-Type": "application/vnd.api+json"},
    )


def clear_all_data() -> None:
    global SEEN_IDS
    with LOCK:
        for path in [LOG_PATH, SEEN_IDS_PATH]:
            if path.exists():
                path.unlink()

        for folder in [ATTACHMENTS_DIR, DETAIL_DUMPS_DIR, PROBE_DIR]:
            folder.mkdir(exist_ok=True)
            for child in folder.iterdir():
                if child.is_file():
                    child.unlink()

    SEEN_IDS = set()


def get_messages(limit: int | None = None, start_date: str | None = None) -> list[dict[str, Any]]:
    if limit is None:
        limit = setting_int("poll_limit")
    if not start_date:
        lookback = setting_int("start_lookback_minutes")
        start_date = (utc_now() - timedelta(minutes=lookback)).strftime("%Y-%m-%dT%H:%M:%SZ")

    qs = urlencode({"start_date": start_date, "limit": limit})
    status, data = flowroute_request(
        "GET",
        f"/v2.1/messages?{qs}",
        None,
        {"Accept": "application/vnd.api+json"},
    )

    if status != 200:
        print(f"{C.RED}[get_messages] HTTP {status}{C.RESET}")
        return []

    if not setting_bool("quiet_success"):
        print(f"{C.GRAY}[get_messages] HTTP 200{C.RESET}")

    items = data.get("data", [])
    return items if isinstance(items, list) else []


def poll_messages_forever() -> None:
    from receive_sms import process_sms_message
    from receive_mms import process_mms_message

    print(f"{C.BLUE}[poller]{C.RESET} polling every {setting_int('poll_interval_seconds')}s")
    while True:
        try:
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
                    process_mms_message(item, source="poll", webhook_payload=None)
                else:
                    process_sms_message(item, source="poll")
        except Exception as exc:
            print(f"{C.RED}[poller] error: {exc}{C.RESET}")
        time.sleep(setting_int("poll_interval_seconds"))


def start_poller_once() -> None:
    global POLL_THREAD_STARTED
    if POLL_THREAD_STARTED:
        return
    if setting_bool("auto_poll"):
        threading.Thread(target=poll_messages_forever, daemon=True).start()
        POLL_THREAD_STARTED = True


def print_json(title: str, payload: Any) -> None:
    print(f"\n{C.CYAN}{C.BOLD}{title}{C.RESET}")
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def list_flowroute_dids_terminal() -> None:
    if not NUMBER_LABELS:
        populate_number_labels()

    print(f"\n{C.GREEN}{C.BOLD}Flowroute DIDs{C.RESET}")
    if not NUMBER_LABELS:
        print("No DIDs loaded.")
        return

    for idx, (num, label) in enumerate(sorted(NUMBER_LABELS.items()), start=1):
        print(f"{idx:>2}. {num}  ->  {label}")


def show_flowroute_numbers_raw() -> None:
    status, data = flowroute_request(
        "GET",
        "/v2/numbers?limit=200",
        None,
        {"Accept": "application/vnd.api+json"},
    )
    print_json(f"Flowroute /v2/numbers (HTTP {status})", data)


def show_bulkvs_account() -> None:
    status, data = bulkvs_request("GET", "/accountDetail")
    print_json(f"BulkVS /accountDetail (HTTP {status})", data)


def show_bulkvs_webhooks() -> None:
    status, data = bulkvs_request("GET", "/webHooks")
    print_json(f"BulkVS /webHooks (HTTP {status})", data)


def show_bulkvs_mdr(msg_type: str = "sms") -> None:
    status, data = bulkvs_request("GET", f"/mdr?type={msg_type}")
    print_json(f"BulkVS /mdr?type={msg_type} (HTTP {status})", data)


def test_discord_terminal() -> None:
    ok, result = discord_post("FlowSMS terminal test ping.")
    color = C.GREEN if ok else C.RED
    print(f"{color}Discord webhook result:{C.RESET} {result}")


def terminal_menu_loop() -> None:
    while True:
        try:
            print(f"""
{C.BOLD}FlowSMS Terminal Control{C.RESET}
  1. Refresh Flowroute DID labels
  2. List Flowroute DIDs
  3. Show Flowroute numbers raw JSON
  4. Show BulkVS account info
  5. Show BulkVS webhooks
  6. Show BulkVS MDR sample (sms)
  7. Show BulkVS MDR sample (mms)
  8. Test Discord webhook
  9. Show callback URLs
  0. Quit terminal menu
""")
            choice = input("Select: ").strip()

            if choice == "1":
                populate_number_labels()
                print(f"{C.GREEN}DID labels refreshed.{C.RESET}")
            elif choice == "2":
                list_flowroute_dids_terminal()
            elif choice == "3":
                show_flowroute_numbers_raw()
            elif choice == "4":
                show_bulkvs_account()
            elif choice == "5":
                show_bulkvs_webhooks()
            elif choice == "6":
                show_bulkvs_mdr("sms")
            elif choice == "7":
                show_bulkvs_mdr("mms")
            elif choice == "8":
                test_discord_terminal()
            elif choice == "9":
                print(f"Flowroute callback: {callback_url() or '(not set)'}")
                print(f"BulkVS callback:    {bulkvs_callback_url() or '(not set)'}")
            elif choice == "0":
                print("Leaving terminal menu. Web app keeps running.")
                break
            else:
                print(f"{C.YELLOW}Invalid choice.{C.RESET}")
        except KeyboardInterrupt:
            print("\nLeaving terminal menu. Web app keeps running.")
            break
        except Exception as exc:
            print(f"{C.RED}Menu error:{C.RESET} {exc}")


if __name__ == "__main__":
    from flask_app import create_app

    if not setting_str("flowroute_access_key") or not setting_str("flowroute_secret_key"):
        raise RuntimeError("Flowroute credentials missing in settings.json")

    populate_number_labels()

    app = create_app()
    port = setting_int("port")

    print(f"{C.GRAY}settings:{C.RESET} {SETTINGS_PATH}")
    print(f"{C.GRAY}log file:{C.RESET} {LOG_PATH}")
    print(f"{C.GRAY}attachments:{C.RESET} {ATTACHMENTS_DIR}")
    print(f"{C.GRAY}detail dumps:{C.RESET} {DETAIL_DUMPS_DIR}")
    print(f"{C.GRAY}probe dumps:{C.RESET} {PROBE_DIR}")
    print(f"{C.GRAY}flowroute callback:{C.RESET} {callback_url() or '(not set)'}")
    print(f"{C.GRAY}bulkvs callback:{C.RESET} {bulkvs_callback_url() or '(not set)'}")
    print(f"{C.GRAY}loaded DIDs:{C.RESET} {len(NUMBER_LABELS)}")
    print(f"{C.BLUE}[web]{C.RESET} http://127.0.0.1:{port}/")
    print(f"{C.BLUE}[webhook]{C.RESET} http://127.0.0.1:{port}/webhook")
    print(f"{C.BLUE}[webhook bulkvs]{C.RESET} http://127.0.0.1:{port}/webhook/bulkvs")

    log = logging.getLogger("werkzeug")
    log.setLevel(logging.ERROR)

    web_thread = threading.Thread(
        target=lambda: app.run(
            host=setting_str("host") or "0.0.0.0",
            port=port,
            debug=False,
            use_reloader=False,
        ),
        daemon=True,
    )
    web_thread.start()

    start_poller_once()
    terminal_menu_loop()

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\nShutting down.")