# Access Bank provider — known follow-ups

Recorded 2026-08-24, when the provider branch finished review. Nothing here blocks
merge; each item was found by a review, judged, and deliberately deferred. They are
written down so they are not rediscovered from scratch later.

## Needs a live observation against the bank

These cannot be closed by reasoning — only by watching the real integration run once.

1. **The transactions `userID` source is an inference.** The accounts request sends the
   login username, which is evidence-backed: it is `worth`'s proven shape. The
   transactions request sends the JWT claim's `userId`, which is an inference from a
   captured request body that showed only a 10-character string, never its source. **If
   live transactions come back empty, try `session.login_user_id` first** — the comments
   at `_open_session` and the transactions call site both say so.

2. **The `accountStatus` vocabulary is unverified.** Only `"ACTIVE"` has ever been
   observed. The code skips a known-closed set (`CLOSED`, `DORMANT`, `INACTIVE`) with an
   exact-string comparison rather than filtering on `!= "ACTIVE"`, deliberately: an
   unobserved word must fall through to a loud failure, never a silent drop.

3. **The `accountType` vocabulary is partly unverified.** `SAVINGS` and `CURRENT` map;
   anything else defaults to `savings` with a warning, EXCEPT a type containing `CARD` or
   `LOAN`, which fails closed — defaulting a liability product to savings would report
   debt as a positive asset.

## Deferred minor findings

Each was raised by a review and judged not worth fixing now.

| # | Item | Why it can wait |
|---|---|---|
| 1 | `_parse_money` accepts a leading `+` (`"+5.00"`) | The value is not misrepresented, only more lenient than the bank's format. |
| 2 | The 2048-bit RSA floor is tested at 1024 and 2048, not 2047/2049 | RSA keys are not generated at odd sizes; the boundary is untestable in practice. |
| 3 | `_read_body` bounds what is parsed, not what is downloaded | Real bounded reading needs `client.stream()`. The comment now describes the mechanism honestly, which was the actual defect. |
| 4 | `_encrypt_password` does not wrap `cryptography`'s `ValueError` | Reachable only via a password longer than the OAEP limit (~190 bytes). |
| 5 | The `"a.!!!.c"` malformed-token test is caught by the JSON parse, not base64 | The assertion is correct; only its stated intent is off. |
| 6 | `test_get_transactions_maps_fields_and_keeps_money_exact` no longer independently proves precision | The real guarantee lives in `test_get_transactions_survives_a_raw_float_in_the_payload`, which hand-writes JSON with a bare numeric literal. Wants a cross-reference comment. |
| 7 | A future-dated `since` clamps to a 1-day window | Safe, untested. |
| 8 | Hitting `_MAX_PAGES` truncates at 1000 transactions with no log | Every other unusual outcome here is raised or logged; this one is silent. |
| 9 | `accessbank_base_url` is an unvalidated string | An `http://` value would silently defeat the pinned-TLS posture. The four paths are guarded by a test; the host is not. |
| 10 | Payee is direction-blind | `payee` is always `sender`. For a DEBIT the counterparty is `beneficiary`, so every debit files under the owner's own name. `payee_source` is also accepted and discarded, unlike the other providers. |
| 11 | The config `GET` is hand-rolled while the other three calls go through `_post` | Error handling is equivalent today — which is the point. A `_get` twin would make the four uniform by construction. |
| 12 | Every public method opens a fresh session | One sync is 1 + N full logins against the bank. Spec-conformant, and not a retry, but worth knowing before pointing a scheduler at it. |

## Larger follow-ups

- **The connect UI does not exist.** `flow_type: "credentials"` is a value Securo's
  frontend has never handled, so Access Bank appears clickable in the connector picker
  and does nothing, and `handleReconnectClick` falls through into the Pluggy widget
  branch. Connecting currently requires POSTing the credential blob to the connections
  callback endpoint. This was scoped out deliberately; it needs its own plan.

- **A currency filter set after an account is already imported freezes that account
  rather than removing it.** `connection_service` skips accounts the provider omits, so
  the stale balance persists. Inherent to filtering in the provider plus Securo's
  existing sync semantics. Close the account by hand if this happens.

- **Encryption does not defend against backup theft in this homelab.** The password is
  Fernet-encrypted at rest, keyed on `SECRET_KEY` — which lives in a k8s Secret on the
  same VM, and `k3s secrets-encrypt` is disabled there, so both the ciphertext and the
  key ride the same nightly archive. The encryption is still worth having: it defends
  against database-level exposure. The control for the backup threat is enabling
  `k3s secrets-encrypt` on VM 190, which is homelab work, not application work.
