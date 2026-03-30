from typing import Any

from main import (
    SEEN_IDS,
    append_log,
    did_label,
    discord_post,
    format_local_timestamp,
    normalize_number,
    save_seen_ids,
    setting_bool,
    utc_now_str,
    C,
)


def infer_provider(message: dict[str, Any], source: str = "") -> str:
    attrs = message.get("attributes", {}) if isinstance(message, dict) else {}
    if isinstance(attrs, dict):
        msg_type = str(attrs.get("message_type", "")).strip().lower()
        if "bulkvs" in msg_type:
            return "bulkvs"
        if "flowroute" in msg_type or msg_type == "longcode":
            return "flowroute"

    msg_id = str(message.get("id", "")).strip().lower() if isinstance(message, dict) else ""
    if msg_id.startswith("mdr2-"):
        return "flowroute"
    if msg_id.startswith("bulkvs-"):
        return "bulkvs"
    if "bulkvs" in (source or "").lower():
        return "bulkvs"
    return "flowroute"


def format_sms_block(message: dict[str, Any], provider: str) -> str:
    attrs = message.get("attributes", {})
    msg_id = str(message.get("id", "")).strip()
    from_num = normalize_number(str(attrs.get("from", "")).strip())
    to_num = normalize_number(str(attrs.get("to", "")).strip())
    raw_body = attrs.get("body")
    body = "" if raw_body is None else str(raw_body).strip()
    status = str(attrs.get("status", "")).strip() or "(none)"
    timestamp = format_local_timestamp(str(attrs.get("timestamp", "")).strip())

    lines = [
        f"{C.GREEN}{C.BOLD}=== NEW INBOUND SMS ({provider.upper()}) ==={C.RESET}",
        f"{C.GRAY}ID:{C.RESET}        {msg_id}",
        f"{C.GRAY}FROM:{C.RESET}      {from_num}",
        f"{C.GRAY}TO:{C.RESET}        {did_label(to_num)}",
        f"{C.GRAY}STATUS:{C.RESET}    {status}",
        f"{C.GRAY}TIME:{C.RESET}      {timestamp}",
        f"{C.GRAY}BODY:{C.RESET}      {body or '(empty)'}",
        f"{C.GREEN}{'=' * 28}{C.RESET}",
    ]
    return "\n".join(lines) + "\n"


def process_sms_message(message: dict[str, Any], source: str) -> None:
    msg_id = str(message.get("id", "")).strip()
    if not msg_id or msg_id in SEEN_IDS:
        return

    attrs = message.get("attributes", {})
    if not isinstance(attrs, dict):
        return

    provider = infer_provider(message, source=source)

    SEEN_IDS.add(msg_id)
    save_seen_ids(SEEN_IDS)

    record = {
        "received_at": utc_now_str(),
        "source": source,
        "provider": provider,
        "kind": "sms",
        "message": message,
        "media": [],
        "downloaded_files": [],
    }
    append_log(record)

    if setting_bool("terminal_message_debug"):
        print()
        print(format_sms_block(message, provider))

    try:
        raw_body = attrs.get("body")
        body = "" if raw_body is None else str(raw_body).strip()

        discord_lines = [
            f"**Inbound SMS Received ({provider.upper()})**",
            f"**From:** {attrs.get('from', '')}",
            f"**To:** {attrs.get('to', '')}",
            f"**Status:** {attrs.get('status', '') or '(none)'}",
            f"**Message ID:** {message.get('id', '')}",
            f"**Body:**\n{body or '(empty)'}",
        ]
        ok, result = discord_post("\n".join(discord_lines))
        if setting_bool("terminal_message_debug") and not ok:
            print(f"{C.YELLOW}[discord sms] failed: {result}{C.RESET}")
    except Exception as exc:
        if setting_bool("terminal_message_debug"):
            print(f"{C.YELLOW}[discord sms] failed: {exc}{C.RESET}")