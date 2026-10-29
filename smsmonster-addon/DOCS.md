# SMS Monster add-on

Turns one or more SIM800C USB modems plugged into your Home Assistant host into an SMS gateway with a web UI (via Ingress), a REST API and webhooks.

## Hardware

- SIM800C breakout with its own USB-serial chip (CH340 / CP2102 / PL2303), one USB port per modem.
- A SIM card with PIN disabled, or set the `sim_pin` option.
- A stable 5 V supply (SIM800 draws up to 2 A bursts; use a powered hub on a Raspberry Pi).

## Options

| Option | Default | Description |
|---|---|---|
| `devices` | `auto` | `auto` scans `/dev/serial/by-id`, `/dev/ttyUSB*`, `/dev/ttyACM*`. Or a comma separated list of device paths. |
| `baudrates` | `115200,9600` | Baud rates tried when probing a modem. |
| `sim_pin` | | PIN entered automatically if the SIM asks for one. |
| `api_token` | | When set, `/api/*` requires `Authorization: Bearer <token>` (Ingress is exempt). **Set this if you expose port 8080**; anyone with the token or open port can read all SMS and fully control the modem. |
| `log_level` | `info` | Log verbosity. |
| `hangup_calls` | `true` | Reject incoming calls after logging them. |
| `delivery_reports` | `false` | Request SMS delivery reports (status becomes `delivered`). |

## Using it from Home Assistant

Send an SMS with a `rest_command`:

```yaml
rest_command:
  send_sms:
    url: "http://<host>:8080/api/messages"
    method: POST
    headers:
      Authorization: "Bearer YOUR_TOKEN"
    content_type: application/json
    payload: '{"to": "{{ to }}", "text": "{{ text }}"}'
```

Receive SMS by adding a webhook in the add-on UI pointing at a Home Assistant webhook trigger
(`http://homeassistant.local:8123/api/webhook/<your-id>`) for the `message.received` event.
The payload is `{"event": "message.received", "data": {"number": "...", "text": "...", ...}}`,
so an automation can use `{{ trigger.json.data.number }}` and `{{ trigger.json.data.text }}`.
