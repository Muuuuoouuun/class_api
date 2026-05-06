# Live API Checks

`diagnose-apis --live` uses non-mutating probes. It may contact external services, but it must not create ClassIn lessons, Notion rows, Claude report artifacts, or Kakao messages.

## ClassIn

### v1 SSO probe

The probe sends a signed `getLoginLinked` request with dummy IDs.

- `OK` with parameter error: ClassIn server received the signed request; network and v1 request shape are usable.
- Strong verification still needs a real `uid/course_id/class_id/telephone` tuple via `classin-toolkit sso-link`.
- Any printed SSO URL is sensitive. Do not paste it into chat unless explicitly sanitized.

### v2 LMS signature probe

The probe sends an empty `/lms/unit/create` body.

- Validation/required-field error: signing path is likely accepted.
- `签名异常`, `signature`, or `errno=101002005`: check v2 signing key, SID, system time, and LMS API entitlement.
- Unexpected success means the API accepted an empty create call. Treat as warning and inspect ClassIn dashboard for unwanted test data.

## Notion

The probe calls `databases.retrieve` for each configured DB ID.

- `OK`: token can see that DB.
- `failed`: check DB ID and whether the Integration was shared with the database.
- `missing`: token or DB ID is placeholder/blank.

## Claude

The probe makes one very short `messages.create` call.

- `OK`: API key, billing, and configured model are reachable.
- `failed`: check key validity, usage/billing limits, and model name.

## Aligo

The probe calls Kakao `heartinfo` balance lookup only.

- `OK`: API key/user id can access Aligo Kakao balance.
- `missing`: `notify.aligo.api_key`, `notify.aligo.user_id`, or `notify.aligo.sender` is blank/placeholder.
- Live sending is still blocked until templates and `_send_via_aligo` implementation are confirmed.

## Current Known Good Signal

On 2026-04-30, live diagnostics reached ClassIn v1/v2 successfully with the configured ClassIn values:

- v1 SSO dummy request reached ClassIn and returned a parameter error.
- v2 LMS signature path was accepted and the empty body returned a validation error (`errno=121601018`).
- Notion, Claude, and Aligo were still missing or placeholder-configured in `config.yaml`.
