import json
import re
from pathlib import Path
from typing import Any
from urllib.request import Request, urlopen
from urllib.error import HTTPError, URLError

from main import (
    ATTACHMENTS_DIR,
    PROBE_DIR,
    SEEN_IDS,
    append_log,
    auth_header,
    bulkvs_auth_header,
    bulkvs_is_configured,
    did_label,
    discord_post,
    dump_json_to_file,
    format_local_timestamp,
    get_message_detail,
    guess_extension,
    normalize_number,
    safe_filename,
    save_seen_ids,
    setting_bool,
    utc_now_str,
    C,
)

URL_RE = re.compile(r"^https?://", re.I)
FLOWROUTE_API_RE = re.compile(r"^https://api\.flowroute\.com/", re.I)
BULKVS_API_RE = re.compile(r"^https://portal\.bulkvs\.com/", re.I)


def flatten(obj: Any, prefix: str = "") -> list[tuple[str, Any]]:
    rows: list[tuple[str, Any]] = []

    if isinstance(obj, dict):
        for k, v in obj.items():
            new_prefix = f"{prefix}.{k}" if prefix else k
            rows.extend(flatten(v, new_prefix))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            new_prefix = f"{prefix}[{i}]"
            rows.extend(flatten(v, new_prefix))
    else:
        rows.append((prefix, obj))

    return rows


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


def provider_auth_header_for_url(url: str) -> str | None:
    if not isinstance(url, str):
        return None
    if FLOWROUTE_API_RE.match(url):
        return auth_header()
    if BULKVS_API_RE.match(url) and bulkvs_is_configured():
        return bulkvs_auth_header()
    return None


def probe_http(url: str, method: str = "GET", use_auth: bool = False) -> dict[str, Any]:
    headers = {}
    if use_auth:
        auth = provider_auth_header_for_url(url)
        if auth:
            headers["Authorization"] = auth

    req = Request(url, headers=headers, method=method)
    try:
        with urlopen(req, timeout=20) as resp:
            return {
                "ok": True,
                "status": resp.status,
                "method": method,
                "use_auth": use_auth,
                "content_type": resp.headers.get("Content-Type"),
                "content_length": resp.headers.get("Content-Length"),
                "final_url": resp.geturl(),
            }
    except HTTPError as exc:
        return {
            "ok": False,
            "status": exc.code,
            "method": method,
            "use_auth": use_auth,
            "reason": str(exc),
        }
    except URLError as exc:
        return {
            "ok": False,
            "status": 0,
            "method": method,
            "use_auth": use_auth,
            "reason": str(exc),
        }


def collect_url_findings(payload: dict[str, Any]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []

    for path, value in flatten(payload):
        entry = None

        if isinstance(value, str) and URL_RE.match(value):
            entry = {
                "path": path,
                "value": value,
                "probe_get_no_auth": probe_http(value, use_auth=False, method="GET"),
                "probe_get_with_auth": probe_http(value, use_auth=True, method="GET"),
                "probe_head_no_auth": probe_http(value, use_auth=False, method="HEAD"),
                "probe_head_with_auth": probe_http(value, use_auth=True, method="HEAD"),
            }
        elif value is None and path.endswith(".url"):
            entry = {
                "path": path,
                "value": None,
                "probe_get_no_auth": None,
                "probe_get_with_auth": None,
                "probe_head_no_auth": None,
                "probe_head_with_auth": None,
            }

        if entry is not None:
            findings.append(entry)

    return findings


def classify_media_item(media: dict[str, Any]) -> str:
    url = str(media.get("url", "") or "").strip()
    local_path = str(media.get("local_path", "") or "").strip()

    if local_path:
        return "downloaded"
    if url:
        return "signed_url_available"
    return "signed_url_missing"


def extract_mms_media(message: dict[str, Any]) -> list[dict[str, Any]]:
    relationships = message.get("relationships", {})
    included = message.get("included", [])

    if not isinstance(relationships, dict):
        relationships = {}
    if not isinstance(included, list):
        included = []

    media_items: list[dict[str, Any]] = []
    included_by_id: dict[str, dict[str, Any]] = {}

    for item in included:
        if isinstance(item, dict):
            item_id = str(item.get("id", "")).strip()
            if item_id:
                included_by_id[item_id] = item

    media_refs = []
    media_relationship = relationships.get("media", {})
    if isinstance(media_relationship, dict):
        media_data = media_relationship.get("data", [])
        if isinstance(media_data, list):
            media_refs = media_data

    for ref in media_refs:
        if not isinstance(ref, dict):
            continue

        media_id = str(ref.get("id", "")).strip()
        included_item = included_by_id.get(media_id, {})

        attrs = included_item.get("attributes", {}) if isinstance(included_item, dict) else {}
        if not isinstance(attrs, dict):
            attrs = {}

        links = included_item.get("links", {}) if isinstance(included_item, dict) else {}
        if not isinstance(links, dict):
            links = {}

        raw_url = attrs.get("url")
        url = raw_url.strip() if isinstance(raw_url, str) else ""

        raw_file_name = attrs.get("file_name")
        file_name = raw_file_name.strip() if isinstance(raw_file_name, str) else ""

        raw_mime = attrs.get("mime_type")
        mime_type = raw_mime.strip() if isinstance(raw_mime, str) else ""

        raw_self = links.get("self")
        self_link = raw_self.strip() if isinstance(raw_self, str) else ""

        media = {
            "id": media_id,
            "file_name": file_name,
            "file_size": attrs.get("file_size"),
            "mime_type": mime_type,
            "url": url,
            "self_link": self_link,
            "provider_uri_accessible": False,
        }
        media["download_status"] = classify_media_item(media)
        media_items.append(media)

    if not media_items:
        for key in ("media", "media_urls", "attachments", "files", "mms", "mediaUrls"):
            value = message.get(key)
            if isinstance(value, list):
                for idx, item in enumerate(value, start=1):
                    if isinstance(item, str) and item.strip():
                        media = {
                            "id": f"inline-{idx}",
                            "file_name": f"attachment_{idx}",
                            "file_size": None,
                            "mime_type": "",
                            "url": item.strip(),
                            "self_link": item.strip(),
                            "provider_uri_accessible": True,
                        }
                        media["download_status"] = classify_media_item(media)
                        media_items.append(media)
                    elif isinstance(item, dict):
                        maybe_url = (
                            item.get("url")
                            or item.get("media_url")
                            or item.get("download_url")
                            or item.get("href")
                            or ""
                        )
                        media = {
                            "id": str(item.get("id", f"inline-{idx}")),
                            "file_name": str(item.get("file_name") or item.get("name") or f"attachment_{idx}"),
                            "file_size": item.get("file_size") or item.get("size"),
                            "mime_type": str(item.get("mime_type") or item.get("content_type") or ""),
                            "url": str(maybe_url).strip(),
                            "self_link": str(item.get("self") or maybe_url or "").strip(),
                            "provider_uri_accessible": True,
                        }
                        media["download_status"] = classify_media_item(media)
                        media_items.append(media)

    return media_items


def mms_media_is_complete(media_items: list[dict[str, Any]]) -> bool:
    for media in media_items:
        url = media.get("url", "")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return True
    return False


def merge_message_details(base_message: dict[str, Any], detailed_message: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base_message)
    merged.update(detailed_message)

    base_attrs = base_message.get("attributes", {})
    detail_attrs = detailed_message.get("attributes", {})
    if isinstance(base_attrs, dict) and isinstance(detail_attrs, dict):
        attrs = dict(base_attrs)
        attrs.update(detail_attrs)
        merged["attributes"] = attrs

    if "relationships" in detailed_message:
        merged["relationships"] = detailed_message["relationships"]

    if "included" in detailed_message and isinstance(detailed_message["included"], list):
        merged["included"] = detailed_message["included"]

    return merged


def enrich_mms_message(message: dict[str, Any], source: str = "") -> dict[str, Any]:
    attrs = message.get("attributes", {})
    if not isinstance(attrs, dict) or not bool(attrs.get("is_mms", False)):
        return message

    provider = infer_provider(message, source=source)
    if provider != "flowroute":
        return message

    record_id = str(message.get("id", "")).strip()
    if not record_id:
        return message

    current_media = extract_mms_media(message)
    if mms_media_is_complete(current_media):
        return message

    detailed = get_message_detail(record_id)
    if not detailed:
        return message

    detailed_media = extract_mms_media(detailed)
    if detailed_media:
        return merge_message_details(message, detailed)

    return detailed


def download_media_file(url: str, record_id: str, index: int, target_name: str, mime_type: str) -> str | None:
    if not isinstance(url, str):
        return None

    url = url.strip()
    if not url.startswith(("http://", "https://")):
        return None

    ext = guess_extension(target_name, mime_type)
    base_name = safe_filename(Path(target_name).stem or f"{record_id}_{index}")
    filename = f"{record_id}_{index}_{base_name}{ext}"
    target_path = ATTACHMENTS_DIR / filename

    if target_path.exists():
        return str(target_path)

    headers = {}
    auth = provider_auth_header_for_url(url)
    if auth:
        headers["Authorization"] = auth

    try:
        req = Request(url, headers=headers, method="GET")
        with urlopen(req, timeout=30) as resp:
            content = resp.read()

        with target_path.open("wb") as f:
            f.write(content)

        return str(target_path)
    except Exception as exc:
        if setting_bool("terminal_message_debug"):
            print(f"{C.YELLOW}[media] failed to download {filename}: {exc}{C.RESET}")
        return None


def write_probe_bundle(
    message_id: str,
    provider: str,
    webhook_payload: dict[str, Any] | None,
    mdr_payload: dict[str, Any] | None,
) -> None:
    bundle = {
        "message_id": message_id,
        "provider": provider,
        "webhook": {
            "payload": webhook_payload,
            "url_findings": collect_url_findings(webhook_payload) if isinstance(webhook_payload, dict) else [],
        },
        "mdr": {
            "payload": mdr_payload,
            "url_findings": collect_url_findings(mdr_payload) if isinstance(mdr_payload, dict) else [],
        },
    }
    dump_json_to_file(PROBE_DIR, "probe", message_id, bundle)


def format_mms_block(message: dict[str, Any], provider: str) -> str:
    attrs = message.get("attributes", {})
    msg_id = str(message.get("id", "")).strip()
    from_num = normalize_number(str(attrs.get("from", "")).strip())
    to_num = normalize_number(str(attrs.get("to", "")).strip())
    raw_body = attrs.get("body")
    body = "" if raw_body is None else str(raw_body).strip()
    status = str(attrs.get("status", "")).strip() or "(none)"
    timestamp = format_local_timestamp(str(attrs.get("timestamp", "")).strip())

    media_items = extract_mms_media(message)

    lines = [
        f"{C.MAGENTA}{C.BOLD}=== NEW INBOUND MMS ({provider.upper()}) ==={C.RESET}",
        f"{C.GRAY}ID:{C.RESET}        {msg_id}",
        f"{C.GRAY}FROM:{C.RESET}      {from_num}",
        f"{C.GRAY}TO:{C.RESET}        {did_label(to_num)}",
        f"{C.GRAY}STATUS:{C.RESET}    {status}",
        f"{C.GRAY}TIME:{C.RESET}      {timestamp}",
        f"{C.GRAY}BODY:{C.RESET}      {body or '(empty)'}",
        f"{C.GRAY}MEDIA:{C.RESET}     {len(media_items)} attachment(s)",
    ]

    for idx, media in enumerate(media_items, start=1):
        name = media.get("file_name") or "(unnamed)"
        mime = media.get("mime_type") or "(unknown mime)"
        size = media.get("file_size")
        url = media.get("url") or ""
        self_link = media.get("self_link") or ""
        local_path = media.get("local_path") or ""
        status_text = media.get("download_status", "unknown")

        lines.append(f"           [{idx}] {name} | {mime} | {size} bytes | {status_text}")
        if url:
            lines.append(f"               URL:   {url}")
        if self_link:
            lines.append(f"               SELF:  {self_link}")
        if local_path:
            lines.append(f"               FILE:  {local_path}")

    lines.append(f"{C.MAGENTA}{'=' * 28}{C.RESET}")
    return "\n".join(lines) + "\n"


def process_mms_message(message: dict[str, Any], source: str, webhook_payload: dict[str, Any] | None) -> None:
    msg_id = str(message.get("id", "")).strip()
    if not msg_id or msg_id in SEEN_IDS:
        return

    provider = infer_provider(message, source=source)
    enriched = enrich_mms_message(message, source=source)
    attrs = enriched.get("attributes", {}) if isinstance(enriched, dict) else {}
    if not isinstance(attrs, dict):
        return

    mdr_payload = {
        "data": enriched,
        "included": enriched.get("included", []),
        "provider": provider,
    } if isinstance(enriched, dict) else None

    write_probe_bundle(msg_id, provider, webhook_payload, mdr_payload)

    media_items = extract_mms_media(enriched)
    downloaded_files: list[str] = []

    for idx, media in enumerate(media_items, start=1):
        file_name = media.get("file_name") or media.get("id") or "attachment"
        url = str(media.get("url", "") or "").strip()

        if url:
            local_path = download_media_file(
                url,
                msg_id,
                idx,
                file_name,
                media.get("mime_type", ""),
            )
            if local_path:
                media["local_path"] = local_path
                media["download_status"] = "downloaded"
                media["downloaded_at"] = utc_now_str()
                downloaded_files.append(local_path)
            else:
                media["download_status"] = "download_failed"
        else:
            media["download_status"] = "signed_url_missing"

    SEEN_IDS.add(msg_id)
    save_seen_ids(SEEN_IDS)

    record = {
        "received_at": utc_now_str(),
        "source": source,
        "provider": provider,
        "kind": "mms",
        "message": enriched,
        "media": media_items,
        "downloaded_files": downloaded_files,
    }
    append_log(record)

    if setting_bool("terminal_message_debug"):
        print()
        print(format_mms_block(enriched, provider))

    try:
        raw_body = attrs.get("body")
        body = "" if raw_body is None else str(raw_body).strip()

        discord_lines = [
            f"**Inbound MMS Received ({provider.upper()})**",
            f"**From:** {attrs.get('from', '')}",
            f"**To:** {attrs.get('to', '')}",
            f"**Status:** {attrs.get('status', '') or '(none)'}",
            f"**Message ID:** {enriched.get('id', '')}",
            f"**Body:**\n{body or '(empty)'}",
        ]

        if media_items:
            discord_lines.append(f"**Attachments:** {len(media_items)}")
            for media in media_items:
                name = media.get("file_name") or media.get("id") or "attachment"
                status_text = media.get("download_status", "unknown")

                if media.get("local_path"):
                    discord_lines.append(f"{name}: downloaded -> {media['local_path']}")
                elif status_text == "signed_url_missing":
                    discord_lines.append(f"{name}: signed URL missing from provider payload")
                elif status_text == "download_failed":
                    discord_lines.append(f"{name}: signed URL present but download failed")
                elif media.get("url"):
                    discord_lines.append(f"{name}: media URL present -> {media['url']}")
                else:
                    discord_lines.append(f"{name}: media metadata only")
        else:
            discord_lines.append("**Attachments:** 0")

        ok, result = discord_post("\n".join(discord_lines))
        if setting_bool("terminal_message_debug") and not ok:
            print(f"{C.YELLOW}[discord mms] failed: {result}{C.RESET}")
    except Exception as exc:
        if setting_bool("terminal_message_debug"):
            print(f"{C.YELLOW}[discord mms] failed: {exc}{C.RESET}")