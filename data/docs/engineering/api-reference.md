# Vellora Cloud API v2

## Basics

Base URL `https://api.vellora.example/v2`, JSON over HTTPS, timestamps in UTC. Authenticate with a bearer token in the
`Authorization` header; tokens are created in the web portal under Organisation → API tokens. The rate limit is
**120 requests per minute per token**; above it the API returns `429 Too Many Requests` with a `Retry-After` header.
List endpoints accept `limit` (max 500) and return `next_cursor`. Telemetry is kept for **13 months**.

## Endpoints and webhooks

- `GET /controllers`, `GET /controllers/{id}`: list controllers and get details (firmware, status, last seen).
- `GET /controllers/{id}/config`, `PATCH /controllers/{id}/config`: read or change configuration keys.
- `GET /controllers/{id}/events`: warnings and alarms with error codes.
- `GET /controllers/{id}/telemetry`: time series (`zone`, `metrics`, `from`, `to`, `resolution`).
- `POST /webhooks`: subscribe to `alarm.raised`, `alarm.cleared`, `telemetry.updated`, `firmware.updated`,
  `config.changed`. Each request carries an **`X-Vellora-Signature`** header, an **HMAC-SHA256** of the body using the
  webhook secret. Failed deliveries are retried up to 5 times.

## Migrating from v1

API v1 is deprecated and returns **HTTP 410 Gone** after **31 March 2027**. In v2 the `api_key` query parameter is
replaced by the bearer token, devices are identified by controller `id` instead of serial number (look up with
`GET /controllers`), and `GET /v1/devices/{serial}/climate` becomes `GET /v2/controllers/{id}/telemetry`.
