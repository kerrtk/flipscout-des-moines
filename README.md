# FlipScout Des Moines — Backend

A FastAPI backend that searches eBay for resale candidates, normalizes listings
into a marketplace-neutral shape, and computes **transparent, fully itemized**
profit estimates in exact decimal arithmetic.

This phase implements eBay only. The data model is deliberately
marketplace-neutral so Facebook Marketplace can be added later without
reshaping anything downstream.

---

## Read this first: two things this API will not pretend

### 1. eBay current listings are **not** sold comparables

`GET /api/ebay/search` uses the eBay **Browse** API, which returns **active
listings — asking prices**. An asking price is what a seller *hopes* to get.
It is not evidence of a sale.

Anyone can list a $12 item for $900 and leave it unsold for a year. If you feed
that $900 into a profit estimate, the arithmetic will be flawless and the
conclusion worthless.

**A low asking price does not make an item profitable, and a high asking price
does not establish resale value.** Before this becomes a real resale
estimator, `resale_price` must come from **authorized sold/completed
comparable data**:

- eBay **Marketplace Insights API** — returns sold data for the last 90 days.
  Access is **restricted**: you must apply and be approved by eBay.
- Another licensed sold-data provider under its terms of service.

Until such a source is wired in, treat `resale_price` as *your* assumption,
supplied by you, and do not attach a confidence score to it. This backend
computes; it does not appraise.

### 2. A 5× resale multiple is **400% gross ROI**, not 500% ROI

This trips people up constantly, and getting it wrong overstates a deal by a
full turn of capital.

Buy at **$100**, resell at **$500**:

| Quantity | Formula | Value |
| --- | --- | --- |
| `gross_multiple` | `500 / 100` | **5.0×** |
| `gross_roi_percent` | `(500 − 100) / 100 × 100` | **400%** |

Your $100 of capital *comes back to you*. Only the other $400 is profit. To
actually earn **500% gross ROI** you need a **6× multiple** ($100 → $600).

So the "500% potential" flag in this API is a **resale multiple** test:

```
qualifies_for_500_percent_resale_multiple = (gross_multiple >= 5)
```

It is returned as a separate field from `gross_roi_percent` precisely so the
two can never be read as each other. And note: qualifying on the multiple is
**not** a claim of profitability — a 5× item can still lose money once fees,
shipping, and repairs land (there is a test asserting exactly that).

---

## The daily scan

Beyond the HTTP API there is a CLI built for a cron job: it runs your saved
searches, filters by how far each find is from a route you already drive,
remembers what it has shown you, and ranks by **net profit per mile**.

```bash
flipscout check     # validate watchlist.yaml, no network calls
flipscout scan      # run every enabled search, print a ranked report
flipscout stats     # database counts + estimator calibration
```

Run them as `python -m app.cli <command>` without installing an entry point.

### Why distance is priced in

Two identical listings at the same price are not the same deal. One sits on a
route you already drive; the other is 100 miles the wrong way. The scanner
charges the difference:

- **On-route detour** — marginal fuel only, round trip (you have to come back).
- **Dedicated trip** — fuel *plus* your time, because that drive exists only
  for this item.

Ranking is `net_profit / round_trip_miles`, so a modest find 3 miles off the
highway beats a better one 100 miles away — which is usually the correct call
and rarely the intuitive one.

Fuel defaults assume a **box truck** (10 mpg), not a car. Overstating mileage
is how a "profitable" 200-mile round trip quietly loses money.

### The truck edge

`local_pickup_only: true` searches inventory that **cannot be shipped**. That
inventory is priced for whoever can drive to it, which is a small pool — and
that discount is the entire opportunity. Cabinet saws, restaurant equipment,
power racks, generators: heavy, awkward, and cheap precisely because most
buyers are shut out.

eBay's pickup filter is a single postal code plus a radius, so a corridor is
searched as **several overlapping circles** — one API call per waypoint. A
7-stop route is 7 calls per search. Budget accordingly: Browse allows roughly
5,000 calls/day on a standard keyset.

### Routes

`watchlist.yaml` defines routes as ordered waypoints. Distance is measured
perpendicular to the **path**, not as a radius around home — a circle around
Des Moines would miss the whole corridor and wrongly include towns the wrong
direction.

```yaml
routes:
  - name: sioux-city-us20
    max_detour_miles: 35
    cadence_days: 14
    waypoints:
      - { name: "Des Moines", postal_code: "50309", lat: 41.5868, lon: -93.6250 }
      - { name: "Fort Dodge", postal_code: "50501", lat: 42.4975, lon: -94.1680 }
      - { name: "Sioux City", postal_code: "51101", lat: 42.4963, lon: -96.4049 }
```

Ships with four: two Des Moines→Sioux City variants (US-20 via Fort Dodge,
US-30 via Carroll), an Iowa-statewide net, and a tri-state set for the day
you are parked in Sioux City with Sioux Falls and Omaha within 90 minutes.

### Memory

`flipscout.db` (SQLite, stdlib) holds three tables:

| Table | Purpose |
| --- | --- |
| `seen_items` | Dedup, so a daily report shows only what is genuinely new |
| `verdicts` | Items you rejected, which never resurface |
| `outcomes` | What you actually paid and actually sold for |

`outcomes` is the important one. `flipscout stats` compares predicted resale
against realised resale and reports the median ratio: **below 1.0 means your
estimates are optimistic and should be haircut by that factor.** After ~20
closed sales this turns the tool from a guess into something calibrated.

Money is stored as `TEXT`, not `REAL` — SQLite's `REAL` is a binary float,
which is exactly what `Decimal` exists to avoid.

### Scheduling

```cron
# 6am and 6pm daily
0 6,18 * * * cd /path/to/flipscout && .venv/bin/python -m app.cli scan >> scan.log 2>&1
```

A once-a-day scan will **not** win underpriced Buy It Now listings — those are
taken within minutes by continuous scanners. What it does catch is local-pickup
inventory, which moves far slower because the buyer pool is small. That is the
niche this tool is built for.

### When time, not distance, is the constraint

If you have a job, the binding limit is not how far something is — it is
whether you can get there before it sells. Every candidate is classified by
**how you would actually collect it**, and the report is grouped and sorted by
that before score:

| Tier | Meaning |
| --- | --- |
| `quick` | Inside your round-trip budget from a base you already sit at |
| `on_route` | A detour on a drive you already make |
| `special_trip` | Neither — has to justify burning a Saturday |

```yaml
bases:
  - { name: "home", postal_code: "50309", lat: 41.5868, lon: -93.6250 }
  - { name: "work", postal_code: "50309", lat: 41.5868, lon: -93.6250 }

availability:
  max_pickup_minutes: 45      # ROUND TRIP, not one way
  average_speed_mph: 35
  windows:
    - { day: "wed", start: "17:30", end: "20:00" }
    - { day: "sat", start: "09:00", end: "13:00" }
```

`max_pickup_minutes` is round trip: 45 minutes at 35 mph is roughly 13 miles
each way, less in town. Set it honestly — an inflated budget just fills the
report with things you will never go get, and a report you stop trusting is a
report you stop reading.

A decent find you can grab on a lunch break outranks a better one that needs a
day you do not have.

### Regulated categories

High-multiple finds cluster in categories with compliance friction — medical
devices especially — precisely because that friction deters casual flippers.
That is an edge once you learn the rules and a liability if you do not. Before
listing anything medical, verify: whether the device is prescription-only,
what the marketplace's medical-device policy allows, and whether skin-contact
consumables need replacing. The tool scores margin; it does not know what you
are allowed to sell.

### The technician edge: repair economics

If you can diagnose and repair, "for parts / not working" listings are worth
far more to you than to whoever else is bidding. That asymmetry is modelled
explicitly rather than left to intuition:

```yaml
- name: patient-monitors
  q: "patient monitor vital signs"
  condition: FOR_PARTS_OR_NOT_WORKING
  assumed_resale_price: 600
  repairable: true
  estimated_repair_cost: 90
  repair_success_rate: 0.5     # you revive half of them
```

The scanner computes an **expected value**: resale × revival odds, minus parts.
A $600 monitor at 50% odds is worth $300 in expectation, not $600 — and the
half you cannot fix have to be carried by the half you can.

`repair_success_rate` multiplies resale, so an optimistic value inflates every
estimate in that category at once. **Start pessimistic and raise it only when
`flipscout stats` proves you out.** A 10× multiple at 10% odds is not a 10×
multiple, and there is a test asserting the scanner rejects exactly that case.

### Work territory and helpers

Two more ways a find becomes cheap to collect:

```yaml
service_areas:
  - name: "central-iowa"
    radius_miles: 100
    center: { name: "Des Moines", lat: 41.5868, lon: -93.6250 }

helpers:
  - name: "coworker-ames"
    lat: 42.0308
    lon: -93.6319
    max_detour_miles: 15
    favor_cost: 15
```

A **service area** is territory you move through routinely for work without
planning trips into it — cheaper to collect from than raw mileage implies. A
**helper** is someone who can collect on your behalf; the scanner charges
`favor_cost` instead of fuel, because what you actually spend is social
capital. Favours are finite, and pricing them at zero is how you burn goodwill
on thin margins.

Full tier precedence, best first:

| Tier | Cost to you |
| --- | --- |
| `quick` | A short errand from a base you already sit at |
| `helper` | A favour — no driving at all |
| `on_route` | A detour on a drive already planned |
| `in_territory` | You will be near it anyway |
| `special_trip` | A dedicated day |

### Regulated categories, again

High-multiple finds cluster where compliance friction deters casual flippers —
medical devices especially. That friction is an edge if you know the rules and
a liability if you do not. **The tool scores margin; it does not know what you
are allowed to sell.** Prescription status, marketplace policy, and
skin-contact consumables are human checks, every time. If your day job is in
the same category, check your employer's conflict-of-interest policy before
sourcing — that is a one-time question with a permanent answer.

### Still the same caveat

Every price in the report is an **asking price**, and every resale figure comes
from `assumed_resale_price` in your watchlist — a number you typed. The report
says so at the bottom of every run, deliberately. Set those figures from
Terapeak **sold/completed** listings, not active ones, and haircut for
condition risk. See the sold-comparables warning above.

---

## Requirements

- Python **3.11+**
- An eBay developer account (only for the live search endpoint)

---

## Installation

```bash
# 1. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt          # runtime only
pip install -r requirements-dev.txt      # runtime + pytest
```

---

## Environment variables

```bash
cp .env.example .env      # then edit .env with your real values
```

`.env` is git-ignored. **Never commit real credentials.**

| Variable | Required | Default | Purpose |
| --- | --- | --- | --- |
| `EBAY_CLIENT_ID` | for search | — | eBay **App ID (Client ID)** |
| `EBAY_CLIENT_SECRET` | for search | — | eBay **Cert ID (Client Secret)** |
| `EBAY_API_BASE` | no | `https://api.ebay.com` | Set to `https://api.sandbox.ebay.com` for sandbox |
| `EBAY_MARKETPLACE_ID` | no | `EBAY_US` | Sent as `X-EBAY-C-MARKETPLACE-ID` |
| `REQUEST_TIMEOUT_SECONDS` | no | `20` | Per-request HTTP timeout |

The app **boots without eBay credentials**. `/health`, `/api/normalize/ebay`,
and `/api/profit-estimate` all work offline; only `/api/ebay/search` returns
`503` until credentials are set.

This project reads variables from the **process environment**. It does not
auto-load `.env`. Load it yourself:

```bash
set -a && source .env && set +a       # bash/zsh
```

…or use your process manager's env-file support (`docker run --env-file`,
systemd `EnvironmentFile=`, your platform's secret manager).

### Getting eBay developer credentials

1. Register at **https://developer.ebay.com/** (free).
2. Go to **Developer Portal → Application Keys**.
3. You get two keysets: **Sandbox** and **Production**.
4. Copy the **App ID (Client ID)** → `EBAY_CLIENT_ID`
   and the **Cert ID (Client Secret)** → `EBAY_CLIENT_SECRET`.
5. Match the keyset to `EBAY_API_BASE`. **Sandbox keys do not work against
   production and vice versa** — that mismatch is the single most common cause
   of a `502 upstream_auth_error`.

Production Browse API access requires your application to be in good standing
under eBay's API License Agreement. Review eBay's rate limits before running
this at volume.

---

## Running the server

```bash
uvicorn app.main:app --reload --port 8000
```

Then open the interactive API documentation:

- **Swagger UI** → http://127.0.0.1:8000/docs
- **ReDoc** → http://127.0.0.1:8000/redoc
- **Raw OpenAPI schema** → http://127.0.0.1:8000/openapi.json

For production, drop `--reload` and run behind a TLS-terminating reverse proxy:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

---

## Endpoints

### `GET /health`

Liveness plus a configuration check. Reports **whether** credentials are
present — never their values.

```bash
curl -s http://127.0.0.1:8000/health
```

```json
{
  "status": "ok",
  "version": "0.1.0",
  "ebay_api_base": "https://api.ebay.com",
  "ebay_marketplace_id": "EBAY_US",
  "ebay_credentials_configured": false
}
```

### `GET /api/ebay/search`

Searches eBay for **fixed-price and auction** inventory and returns normalized
listings. Remember: these are **asking prices**.

| Param | Type | Default | Notes |
| --- | --- | --- | --- |
| `q` | string | *required* | 1–350 characters |
| `limit` | int | `50` | 1–200 (eBay's maximum) |
| `offset` | int | `0` | 0–9999 (eBay's maximum) |
| `condition` | enum | — | e.g. `NEW`, `USED_GOOD`, `FOR_PARTS_OR_NOT_WORKING` |
| `max_price` | number | — | Must be > 0; applied in `USD` |

```bash
# Basic search
curl -s "http://127.0.0.1:8000/api/ebay/search?q=vintage%20pyrex&limit=10"

# With condition and a price ceiling
curl -s "http://127.0.0.1:8000/api/ebay/search?q=nintendo%2064&limit=25&offset=0&condition=USED_GOOD&max_price=75"
```

```json
{
  "total": 137,
  "offset": 0,
  "limit": 10,
  "listings": [
    {
      "source": "ebay",
      "source_item_id": "v1|123456789012|0",
      "title": "Vintage Pyrex Mixing Bowl Set",
      "url": "https://www.ebay.com/itm/123456789012",
      "price_value": "24.99",
      "price_currency": "USD",
      "shipping_cost": "8.45",
      "condition": "Used",
      "location_text": "Des Moines, IA, US",
      "listing_type": "FIXED_PRICE",
      "captured_at": "2026-08-30T12:00:00Z",
      "raw": { "...": "original eBay object, credentials redacted" }
    }
  ]
}
```

### `POST /api/normalize/ebay`

Normalizes a single raw eBay `itemSummary` — useful for debugging against real
payloads **without spending an API call**.

```bash
curl -s -X POST http://127.0.0.1:8000/api/normalize/ebay \
  -H 'Content-Type: application/json' \
  -d '{
        "itemId": "v1|123456789012|0",
        "title": "Vintage Pyrex Mixing Bowl Set",
        "itemWebUrl": "https://www.ebay.com/itm/123456789012",
        "price": {"value": "24.99", "currency": "USD"},
        "shippingOptions": [{"shippingCost": {"value": "8.45", "currency": "USD"}}],
        "condition": "Used",
        "seller": {"username": "desmoines_finds", "feedbackScore": 1423},
        "itemLocation": {"city": "Des Moines", "stateOrProvince": "IA", "country": "US"},
        "buyingOptions": ["FIXED_PRICE"]
      }'
```

### `POST /api/profit-estimate`

```bash
curl -s -X POST http://127.0.0.1:8000/api/profit-estimate \
  -H 'Content-Type: application/json' \
  -d '{
        "resale_price": "200.00",
        "purchase_price": "40.00",
        "marketplace_fee_rate": "0.1325",
        "payment_fee_rate": "0.0299",
        "shipping_cost": "12.00",
        "taxes": "2.80",
        "fuel_cost": "6.00",
        "repair_cost": "5.00",
        "cleaning_cost": "3.00",
        "packaging_cost": "2.20",
        "other_costs": "1.00"
      }'
```

```json
{
  "resale_price": "200.00",
  "purchase_price": "40.00",
  "gross_profit": "160.00",
  "total_selling_fees": "32.48",
  "total_other_costs": "32.00",
  "net_profit": "95.52",
  "gross_multiple": "5.0000",
  "gross_roi_percent": "400.00",
  "net_roi_percent": "91.42",
  "qualifies_for_500_percent_resale_multiple": true
}
```

Only `resale_price` and `purchase_price` are required; every cost defaults to
`0`. `marketplace_fee_rate` defaults to `0.1325`, and `payment_fee_rate`
defaults to `0` because **eBay's managed-payments final value fee already
bundles payment processing** — set it explicitly only for a marketplace that
bills processing separately.

#### Formulas

```
gross_profit      = resale_price − purchase_price
selling_fees      = resale_price × (marketplace_fee_rate + payment_fee_rate)
total_other_costs = shipping + taxes + fuel + repair + cleaning + packaging + other
net_profit        = resale_price − purchase_price − selling_fees − total_other_costs
gross_multiple    = resale_price / purchase_price
gross_roi_percent = gross_profit / purchase_price × 100
net_roi_percent   = net_profit / (purchase_price + selling_fees + total_other_costs) × 100

qualifies_for_500_percent_resale_multiple = gross_multiple >= 5
```

`purchase_price` must be **> 0** — it is the denominator of every ratio.

### Status codes

| Code | Meaning |
| --- | --- |
| `200` | Success |
| `400` | Invalid search input caught by the client layer |
| `422` | Request failed schema validation (bad type, negative money, unknown condition) |
| `429` | eBay rate-limited us — back off and retry |
| `502` | Upstream eBay failure (auth rejected, 5xx, malformed response) |
| `503` | **Server** misconfiguration — a required env var is missing |
| `504` | eBay did not respond within `REQUEST_TIMEOUT_SECONDS` |

`429` and `504` are used instead of a blanket `502` because both have canonical
meanings clients can act on automatically (back off; retry).

An eBay **auth** failure maps to `502`, not `401`: the caller of *this* API is
not the party who failed to authenticate — our upstream credential exchange is.

---

## Money is `Decimal`, and it serializes as a string

Every monetary value is a `decimal.Decimal` end to end. Binary floating point
is never used for prices, fees, or profit — in IEEE-754, `0.1 + 0.2 != 0.3`,
and a resale tool that is a cent off is a resale tool nobody trusts.

Consequently the API emits money as **JSON strings** (`"24.99"`, not `24.99`).
This is deliberate: a JSON number gets re-parsed as a float by nearly every
client, reintroducing exactly the error we removed. Parse these with your
language's decimal type — `Decimal("24.99")`, `new BigDecimal("24.99")`,
`new Decimal("24.99")` (decimal.js) — not `parseFloat`.

---

## Security

- **Secrets come from the environment only.** Never hardcode them, never put
  them in frontend code, never commit them. `.env` is git-ignored.
- **`EBAY_CLIENT_SECRET` is never exposed.** It is wrapped in
  `pydantic.SecretStr`, so an accidental `repr()` or log line prints
  `**********`. It appears in exactly one place: the `Basic` auth header of the
  token request. It is never in a response body, an exception message, or a log.
- **`raw` is sanitized.** The original eBay object is preserved for debugging,
  but any key resembling a credential (`authorization`, `access_token`,
  `client_secret`, `api_key`, `password`, …) is replaced with `[redacted]`
  recursively before it can reach a response or a log aggregator.
- **Tokens are never logged**, and `_CachedToken.__repr__` prints `'***'`.

Before deploying to production:

- Put the API behind **TLS** and a reverse proxy. Do not expose uvicorn directly.
- **Add authentication.** These endpoints are unauthenticated as written. Anyone
  who can reach `/api/ebay/search` is spending *your* eBay API quota.
- **Add rate limiting** at the proxy, both to protect your eBay quota and to
  stay inside eBay's published call limits.
- **Configure CORS explicitly** if a browser will call this. No CORS middleware
  is enabled by default — that is intentional, so nobody ships `allow_origins=["*"]`
  by accident.
- **Rotate credentials** if a secret is ever committed, logged, or shared.
  Rotation is the only real remedy; deleting the commit is not.

### Conduct

This backend uses **eBay's official, documented REST APIs** over HTTPS with a
properly obtained OAuth token. It does **not** scrape HTML, solve or bypass
CAPTCHAs, bypass authentication, ignore `robots.txt`, or access marketplaces
that have not published a supported API. Only documented Browse query
parameters and filters are sent — no invented filters. Please keep it that way.

---

## Testing

```bash
python -m compileall app tests
pytest -q
```

**No test makes a live eBay call.** The OAuth and Browse round-trips are served
by `httpx.MockTransport`, and a `conftest.py` fixture strips every `EBAY_*`
variable from the environment so a developer machine with real credentials
cannot accidentally reach production.

Coverage includes: OAuth request construction, token reuse before expiry,
expiry/refresh, concurrent refresh serialization, search parameter
construction, input validation, normalization of complete and degenerate
payloads, invalid/missing prices, shipping-cost handling (including free vs.
unknown), credential redaction, the exact-5× boundary, sub-5× rejection, the
multiple-vs-ROI distinction, full fee-and-expense arithmetic, and every error
status code.

---

## Project layout

```
app/
  __init__.py
  main.py                    HTTP routing and exception -> status mapping
  config.py                  Environment settings; SecretStr-wrapped credentials
  models.py                  NormalizedListing, ProfitAssumptions, ProfitEstimate
  services/
    __init__.py
    ebay_client.py           OAuth + Browse transport, token cache, exceptions
    normalization.py         normalize_ebay_item() and coercion helpers
    profitability.py         Decimal profit arithmetic
tests/
  conftest.py                Mock transport and fixtures
  test_ebay_client.py
  test_normalization.py
  test_profitability.py
  test_api.py
requirements.txt              Runtime dependencies
requirements-dev.txt          + pytest
.env.example                  Placeholders only - never real credentials
pytest.ini                    Test discovery
ruff.toml                     Lint and format configuration
```

Transport, normalization, arithmetic, and routing are separate modules on
purpose: adding a marketplace should touch **one** new transport module and
**one** new normalizer, and nothing else.

The only global mutable state is the eBay token cache — a single
process-wide `EbayClient` whose cache is guarded by a `threading.Lock` with
double-checked locking, so N concurrent requests trigger exactly one token
handshake (there is a test for this).

---

## Next: Facebook Marketplace

`NormalizedListing` is already marketplace-neutral, and `source` accepts
`"facebook"` today. Adding it should require no change to the profitability
engine, the response envelope, or any client.

**Planned shape:**

1. `app/services/facebook_client.py` — transport, mirroring `EbayClient`:
   OAuth token handling, a locked token cache, explicit timeouts, and the same
   exception vocabulary (`AuthError` / `RateLimitError` / `ApiError` /
   `ResponseError`) so `main.py`'s handlers map it to the same status codes.
2. `normalize_facebook_item(raw_item: dict) -> NormalizedListing` in
   `app/services/normalization.py`, sharing the existing `to_decimal` /
   `to_text` / `to_datetime` / `sanitize_raw` helpers.
3. `GET /api/facebook/search` returning the **same** `ListingSearchResponse`
   envelope, plus an optional `sources` parameter on a combined search endpoint.
4. Fields with no Facebook equivalent (`seller_feedback_score`, `item_end_time`)
   stay `None` — which is precisely why every field is optional.

**The blocker is access, not code.** Facebook Marketplace has **no public
listing-search API**. Legitimate paths are limited, and scraping is not one of
them — it violates Meta's Terms of Service, and this project will not do it.
A production integration requires an approved Meta commerce/catalog API
partnership or an equivalent authorized data source. Until such access is
granted, the Facebook client should remain unimplemented rather than faked.

The same discipline applies here as to eBay: whatever the source, a listing
price is an **asking** price until sold-comparable data says otherwise.
