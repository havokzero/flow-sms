# FlowSMS

FlowSMS is a lightweight Flask-based SMS/MMS viewer and webhook receiver for **Flowroute** and **BulkVS**.

It is designed for:

- receiving inbound SMS and MMS by webhook
- polling recent messages from providers
- viewing messages in a local web UI
- saving MMS attachments when a valid signed media URL is provided
- forwarding inbound message details to Discord via webhook
- running cleanly in a Debian-based LXC on **Proxmox**

## Features

- Web UI for inbound message review
- Flowroute support
  - inbound SMS
  - inbound MMS
  - message polling
  - DID label lookup
- BulkVS support
  - inbound SMS/MMS webhook ingestion
  - account/API testing routes
- MMS attachment handling
  - saves attachments locally when provider returns a valid media URL
  - records metadata and probe results when provider does **not** return a valid download URL
- Discord webhook notifications
- Systemd service support
- Proxmox helper script support

## Important note about Flowroute MMS media

Flowroute documentation indicates inbound MMS media should be delivered via `included[].attributes.url` as a temporary signed URL, while the `links.self` media URI is not currently accessible. In live testing, some inbound MMS payloads may contain `url: null`, which prevents automatic media download even though the attachment metadata is present.

FlowSMS handles this by:

- saving the webhook and probe data
- marking the attachment as `signed_url_missing`
- showing metadata in the UI even if the image cannot be downloaded

## Project layout

```text
.
├── ct/
│   └── flowsms.sh
├── attachments/
├── detail_dumps/
├── probe_results/
├── flask_app.py
├── flowsms.service
├── install.sh
├── main.py
├── receive_mms.py
├── receive_sms.py
├── requirements.txt
├── settings.json
└── README.md
```

## Requirements

- Python 3.11+
- Debian 12/13 or similar
- A Flowroute account for Flowroute support
- A BulkVS account for BulkVS support
- Public callback URL if you want live inbound webhook delivery
- Optional:
  - Discord webhook URL
  - ngrok or reverse proxy for testing webhooks externally

## Quick start

### Manual install in a Debian container or VM

```bash
apt-get update
apt-get install -y curl git python3 python3-venv python3-pip ca-certificates

git clone https://github.com/havokzero/flow-sms.git /opt/flowsms
cd /opt/flowsms

python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

cp flowsms.service /etc/systemd/system/flowsms.service
systemctl daemon-reload
systemctl enable --now flowsms
```

Then edit:

```bash
nano /opt/flowsms/settings.json
```

And restart:

```bash
systemctl restart flowsms
```

### Manual install with installer script

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/havokzero/flow-sms/master/install.sh)"
```

## Proxmox helper script

Run this on the **Proxmox host**, not inside an existing container:

```bash
bash -c "$(curl -fsSL https://raw.githubusercontent.com/havokzero/flow-sms/master/ct/flowsms.sh)"
```

After deployment, the helper prints the container IP and web UI URL.

## Web UI

By default, FlowSMS runs on port `8080`.

Example:

```text
http://<container-ip>:8080
```

The web UI provides:

- recent message list
- attachment previews for downloaded MMS media
- settings page
- JSON/API inspection routes
- manual poll trigger
- provider debug routes

## Configuration

Edit `settings.json`.

Example placeholder config:

```json
{
  "host": "0.0.0.0",
  "port": 8080,
  "public_base_url": "https://your-public-url.example",
  "webhook_token": "replace_me",
  "discord_webhook_url": "",
  "flowroute_access_key": "",
  "flowroute_secret_key": "",
  "default_phone_number": "",
  "poll_interval_seconds": 3,
  "poll_limit": 25,
  "start_lookback_minutes": 1440,
  "auto_poll": true,
  "quiet_success": true,
  "live_refresh_seconds": 5,
  "bulkvs_api_url": "https://portal.bulkvs.com/api/v1.0",
  "bulkvs_username": "",
  "bulkvs_token": "",
  "bulkvs_webhook_token": "",
  "terminal_message_debug": false,
  "callback_scope": "number",
  "auto_webhook_server": true,
  "bulkvs_soap_wsdl": "https://portal.bulkvs.com/api?wsdl",
  "bulkvs_soap_key": "",
  "bulkvs_soap_secret": "",
  "terminal_menu_enabled": false
}
```

## Flowroute configuration

For Flowroute, configure your callback URL in the Flowroute portal or through the API.

Typical webhook route used by FlowSMS:

```text
https://your-public-url.example/webhook?token=YOUR_WEBHOOK_TOKEN
```

FlowSMS supports:

- polling recent messages
- inbound SMS webhook handling
- inbound MMS webhook handling
- message detail probing for MMS troubleshooting

## BulkVS configuration

Typical BulkVS webhook route used by FlowSMS:

```text
https://your-public-url.example/webhook/bulkvs?token=YOUR_BULKVS_WEBHOOK_TOKEN
```

FlowSMS currently supports BulkVS through:

- REST credentials
- webhook ingestion
- account/API inspection routes in the web UI

SOAP fields are present in config for future work, but current message handling is focused on REST/webhook processing.

## API and debug routes

Examples:

```text
/
 /settings
 /api/messages
 /api/account-numbers
 /api/did-labels
 /api/detail/<message_id>
 /api/probe/<message_id>
 /api/bulkvs/account
 /api/bulkvs/mdr?type=sms
 /api/bulkvs/mdr?type=mms
 /webhook
 /webhook/bulkvs
```

## Logs and stored data

FlowSMS stores runtime artifacts under `/opt/flowsms`:

- `received_messages.log`
- `seen_ids.json`
- `attachments/`
- `detail_dumps/`
- `probe_results/`

These are useful when debugging:

- missing MMS media URLs
- provider callback payload differences
- Discord webhook output
- duplicate message handling

## Systemd

Service file:

```text
/etc/systemd/system/flowsms.service
```

Useful commands:

```bash
systemctl status flowsms --no-pager -l
journalctl -u flowsms -n 100 --no-pager
systemctl restart flowsms
```

## Troubleshooting

### Web UI loads but no messages appear

Check service status:

```bash
systemctl status flowsms --no-pager -l
journalctl -u flowsms -n 100 --no-pager
```

### Flowroute polling returns HTTP 401

Your Flowroute credentials in `settings.json` are wrong, missing, or still placeholder values.

### MMS metadata appears but image is not downloaded

If the provider payload contains attachment metadata but `included[].attributes.url` is empty or null, FlowSMS will show the attachment metadata but cannot download the file. This is expected behavior for that failure case.

### Wrong IP shown during deployment

If you previously installed on the Proxmox host by mistake, remove the host copy and redeploy. The helper script should install inside the LXC and report the container IP.

## Security notes

- Do **not** commit real credentials to GitHub
- Use placeholder values in `settings.json` or maintain a separate local config
- Protect webhook endpoints with tokens
- If exposing the UI externally, place it behind a reverse proxy and authentication
- Review provider callback IP restrictions if desired

## Development notes

This project is intentionally practical and provider-focused. MMS handling includes probe logging because provider behavior does not always match published documentation, especially around Flowroute signed media URLs.

## License

MIT
