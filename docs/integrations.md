# Connecting your systems

Every connector in this repo is one of three things, and the table says which.
We will not tell you an integration exists when it does not.

| Badge | Means |
|---|---|
| **built** | Written against the real API and tested against it. |
| **universal** | Works with any system through a common protocol: IMAP/SMTP, CSV, a webhook. |
| **stub** | Interface only. Calling it raises a clear error with a recipe for adding it. |

Check what is actually working on your machine at any time:

```bash
make doctor
```

## Status

<a id="pms"></a>

### PMS - `systems.pms.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/hotel/*.json`. What `make demo` uses. |
| `csv` | universal | a CSV export | Reads `data/imports/*.csv`. **Start here.** Works with every PMS. |
| `cloudbeds` | built | OAuth app + refresh token | Live reads and writes. |
| `cli` | universal | a JSON-speaking CLI | Advanced. Bridges to a vendor command line tool. |

**`csv` - the one that always works.** Export from your PMS and drop the files in
`data/imports/`:

- `reservations.csv` - `id, status, check_in, check_out, room_type_id,
  room_type_name, room_id, adults, children, source, total, balance, currency,
  guest_email, guest_first_name, guest_last_name, guest_phone, guest_country`
- `guests.csv` - `id, first_name, last_name, email, phone, country, language, vip`
- `rooms.csv` - `id, name, max_occupancy, count, rank`
- `rates.csv` - `date, room_type_id, price, currency, min_los, available, closed`

Headers are matched loosely: `checkIn`, `check_in` and `Check In` all work, and
extra columns are kept. Dates must be `YYYY-MM-DD`. Only `reservations.csv` is
required; the rest add capability.

In CSV mode the agent cannot write back to your PMS, so anything it wants to
change is appended to `data/exports/pms_writes.csv` with everything a person
needs to apply it by hand. That is a feature: it is how you check the agent's
judgement before you give it write access.

**`cloudbeds`.** Create an app in the Cloudbeds developer portal, authorise it
once against your property, and put the result in `.env`:

```
CLOUDBEDS_CLIENT_ID=
CLOUDBEDS_CLIENT_SECRET=
CLOUDBEDS_REFRESH_TOKEN=
CLOUDBEDS_PROPERTY_ID=
```

Scopes: `read:reservation`, `write:reservation`, `read:guest`, `read:room`,
`read:rate`, `write:rate`, `read:hotel`. The access token refreshes itself.

**`cli`.** If your PMS already has a command line tool that prints JSON, point at
it. See the profiles at the top of `core/adapters/pms_cli.py`.

<a id="email"></a>

### Email - `systems.email.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/inbound/*.eml` and `*.json`. |
| `imap` | universal | mailbox + app password | Any provider. **Start here.** |
| `gmail` | built | Google OAuth desktop client | Adds Gmail labels and threads. |

**`imap`.** In `.env`:

```
EMAIL_ADDRESS=reservations@example.com
EMAIL_PASSWORD=            # an APP password, never your login password
IMAP_HOST=imap.example.com
SMTP_HOST=smtp.example.com
SMTP_PORT=587              # 587 STARTTLS, 465 implicit TLS
```

Google, Microsoft and Fastmail all issue app-specific passwords. Two-factor stays
on and you can revoke the password without touching the account.

Replies carry `In-Reply-To` and `References`, so they land inside the guest's
existing thread rather than starting a new one.

**`gmail`.** Google Cloud Console: enable the Gmail API, configure the consent
screen, create an OAuth client of type **Desktop app**, download the JSON to
`credentials.json`. Then `pip install google-api-python-client google-auth-oauthlib`
and run `make doctor`; a browser opens once and writes `token.json`. Scopes:
`gmail.readonly`, `gmail.send`, `gmail.modify`.

<a id="messaging"></a>

### Messaging - `systems.messaging.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `mock` | universal | nothing | Reads `fixtures/inbound/messages.json`. |
| `unipile` | built | your own UniPile account | WhatsApp on your own number. |
| `webhook` | universal | any URL | POST to Zapier, Make, n8n, or your own endpoint. |

**`unipile`.** You create the account, you connect your number by QR code, you
own the credentials: `UNIPILE_DSN`, `UNIPILE_API_KEY`, `UNIPILE_ACCOUNT_ID`.
WhatsApp Business policy limits what you may send outside a guest-initiated
window; read your provider's rules before turning this on.

**`webhook`.** The simplest possible outbound: set `MESSAGING_WEBHOOK_URL` and
the agent POSTs `{chat_id, text, kind, hotel, sent_at}`. Your automation tool
delivers it however you like. Send-only.

<a id="sheets"></a>

### Sheets - `systems.sheets.adapter`

| Adapter | Status | Needs | Notes |
|---|---|---|---|
| `csv` | universal | nothing | Writes `data/exports/<sheet>.csv`. |
| `google` | built | service account JSON | A live shared spreadsheet. |

**One exception, worth knowing.** This agent's found-items log is the one
thing read *through* this setting that is not one of the agent's own
`data/exports` reports - it is a file the hotel maintains, so with
`adapter: csv` it is read from `data/imports/found_items.csv` instead (same
convention as `systems.pms.adapter: csv`; see `tools/engine.py:
load_found_items_rows` and `workflows/00-setup.md` step 5). Everything else
this adapter touches (the courier log, `make report`) is a normal
`data/exports` write. With `adapter: google` there is no such split - both
directions are the same live spreadsheet.

For `google`: enable the Sheets API, create a service account and a JSON key,
save it as `service_account.json`, and share your spreadsheet with the service
account's email address as an Editor. Set `systems.sheets.spreadsheet_id` to the
long id from the sheet's URL.

### Everything else

`pos`, `accounting`, `reviews`, `calendar`, `payments`, `procurement`, `locks`
and `courier` are **stubs**: the interface exists, nothing is implemented.
Calling one raises an error that tells you exactly this. If your agent needs
one, use the recipe below.

**Courier is the one this agent actually cares about.** The roster promise is
"arranges paid return shipping with a courier ... and closes the case with
tracking" — today that is simulated, honestly: `tools/engine.py:ship_claim`
generates its own tracking id (`LF-<claim>-<sequence>`) and never calls a real
shipping API. `core.adapters.get_stub("courier", settings)` exists
(`core/adapters/base.py:Courier` — `track` and `create_shipment`, both raising
`AdapterNotImplemented`), so a real integration has an interface to build
against, but nothing in this repo calls it yet. To wire up a real carrier:
follow the five steps below, register it under `courier` rather than
`pos`/`email`/etc in `core/adapters/__init__.py`, then change
`tools/engine.py:ship_claim` to call `create_shipment(...)` and use the
tracking id it returns instead of minting one locally.

## Implement your own

<a id="implement-your-own"></a>

The interface is small on purpose, and your Claude Code session can do this with
you in an afternoon. Open `claude` in this folder and paste:

> Read `docs/integrations.md#implement-your-own` and `core/adapters/base.py`.
> I need a PMS adapter for **<your system>**. Its API docs are at **<url>** and
> I have credentials in `.env` as `<VAR names>`. Copy `core/adapters/pms_csv.py`
> as the shape, implement `ping`, `capabilities` and the read methods first,
> register it in `core/adapters/__init__.py`, and stop before the write methods
> so I can check the reads with `make doctor`.

### The five steps

**1. Copy the closest existing adapter.**
`core/adapters/pms_csv.py` for a PMS, `email_imap.py` for a mailbox,
`messaging_webhook.py` for a chat channel. They are short and heavily commented.

**2. Implement `ping()` and `capabilities()` first.**

```python
def ping(self) -> HealthCheck:
    """Never raises. Returns ok=False with a fix_hint a hotel can act on."""

def capabilities(self) -> set[str]:
    """The method names that actually do something on this adapter."""
```

`make doctor` reads both. Getting them right first means the rest of the work has
a feedback loop.

**3. Implement the reads.** Map the vendor's fields onto the dataclasses in
`core/adapters/base.py` (`Reservation`, `Guest`, `RoomType`, `RateRow`,
`EmailMessage`, `ChatMessage`). Put anything you do not map into `.extra` rather
than dropping it. Dates are ISO `YYYY-MM-DD`. Money is a float in the hotel's
currency.

**4. Implement the writes, each with the guard.**

```python
from core.adapters.base import guarded_write

@guarded_write("pms_write")
def add_note(self, reservation_id: str, text: str) -> dict:
    ...
```

The decorator is not optional. Without it your adapter can write while the agent
is in shadow mode, which defeats the entire safety model. The action name should
be one of the values in `review.require_approval_for`.

**5. Register it.** One line in `core/adapters/__init__.py`:

```python
REGISTRY["pms"]["yoursystem"] = "core.adapters.pms_yoursystem:YourSystemPMS"
```

Then set `systems.pms.adapter: yoursystem` in `config/hotel.yaml` and run
`make doctor`.

### Rules that matter

- **`ping()` never raises.** It returns `HealthCheck(ok=False, ...)` with a hint.
  A broken adapter must still produce a readable doctor table.
- **Every write is decorated.** No exceptions.
- **Rate limits belong in the adapter.** Use `core/adapters/_http.py:RateLimiter`.
  Retry 429 and 5xx with backoff; never retry a 4xx.
- **Never log a credential.** `core/log.py` masks anything whose key looks like a
  secret, but do not rely on it.
- **Redact on ingestion.** Any guest-written text goes through
  `core.redact.redact()` before it is stored or shown to a model.
- **Write a test.** Copy `tests/test_core_adapters_mock_csv.py`. It should run
  with no network: feed your parser a fixture, check the dataclass that comes out.

### `core/` is shared

`core/` is identical in all 28 agents in this family. If you change something in
`core/`, keep it generic - a hotel-specific tweak belongs in `tools/` or in your
own adapter file, not in the shared runtime.
