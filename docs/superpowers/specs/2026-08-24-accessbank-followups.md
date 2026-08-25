# Access Bank provider — known follow-ups

Recorded 2026-08-24, when the provider branch finished review. Nothing here blocks
merge; each item was found by a review, judged, and deliberately deferred. They are
written down so they are not rediscovered from scratch later.

## Needs a live observation against the bank

These cannot be closed by reasoning — only by watching the real integration run once.

1. **~~The transactions `userID` source is an inference.~~ RESOLVED 2026-08-25 by a live
   run.** A real connection imported 83 transactions across the full ~90-day window, so
   the claim-sourced `userID` is accepted by the bank. Note the two values may simply be
   identical — the login username is 10 characters and the captured claim was also 10 —
   so this is proof that the current code WORKS, not proof that the claim is what the
   bank requires. The comments and `session.login_user_id` stay in place as the fallback
   if a future account behaves differently.

2. **The `accountStatus` vocabulary is still unverified.** Only `"ACTIVE"` has been
   observed, including on the 2026-08-25 live run. The code skips a known-closed set (`CLOSED`, `DORMANT`, `INACTIVE`) with an
   exact-string comparison rather than filtering on `!= "ACTIVE"`, deliberately: an
   unobserved word must fall through to a loud failure, never a silent drop.

3. **The `accountType` vocabulary is still partly unverified.** Only `SAVINGS` has been
   seen live (2026-08-25). `SAVINGS` and `CURRENT` map;
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

- **~~The connect UI does not exist.~~ BUILT 2026-08-25** on branch
  `feat/accessbank-connect-ui`. `credentials-connect-dialog.tsx` handles the
  `flow_type: "credentials"` case, and `handleReconnectClick` no longer falls through
  into the Pluggy widget branch. Verified live through the browser.

- **A currency filter set after an account is already imported freezes that account
  rather than removing it.** `connection_service` skips accounts the provider omits, so
  the stale balance persists. Inherent to filtering in the provider plus Securo's
  existing sync semantics. Close the account by hand if this happens.

- **Encryption at rest is not, on its own, protection against backup theft.** The
  password is Fernet-encrypted using a key derived from the application `SECRET_KEY`.
  Whether that helps depends entirely on where `SECRET_KEY` lives relative to the
  database backup: if a single archive carries both the ciphertext and the key, the
  encryption buys nothing against whoever holds that archive. It does defend against
  database-level exposure — a stray dump, a stolen volume, read access to Postgres —
  which is a real and separate threat.

  Deployers should check two things in their own environment: whether cluster-level
  secret encryption is enabled, and whether the key and the data end up in the same
  backup. Both are deployment concerns rather than application concerns, and neither is
  visible from this repository.

## Live verification, 2026-08-25

A real connection against the owner's own account, through the UI:

- The USD account mapped; the naira account was correctly excluded by
  `ACCESSBANK_IMPORT_CURRENCIES=USD`.
- 83 transactions imported, 2026-05-25 to 2026-08-24 — the full rolling window.
- 10 credits, 73 debits, one currency, no negative amounts.
- **`SUM(credits) - SUM(debits)` equals the bank's own reported balance exactly.** This
  is the strongest available end-to-end proof: a single amount mangled by a float, or a
  single transaction type mapped the wrong way, would break the reconciliation.
- The stored credential is a Fernet token (`gAAAAA...`); the connection row holds only
  `user_id` and `password_enc`, with no plaintext `password` key.
- FX is live: the OXR key resolves NGN at the real rate rather than the 1:1 fallback,
  and the historical endpoint works on this plan, so per-date conversion is real.

## Durability: the backup window is shorter than the data's reach

Recorded 2026-08-25, measured by homelab-security and independently confirmed
against the backup terraform.

The important numbers, side by side:

- The bank serves a rolling **~90 days** of transaction history. Anything older
  cannot be re-fetched at any price.
- The nightly archive retains roughly **2 days** (a 1-day object expiration plus
  a 1-day noncurrent tail on a versioned bucket).
- The volume class is `local-path` with `reclaimPolicy: Delete`, so deleting the
  PVC deletes the data. There is no orphaned volume to recover from.

An earlier note claimed PVCs were not backed up at all. That was wrong — they
are, because they live on the node's disk and the whole VM is imaged nightly.
The real constraint is **retention, not existence**, which is a different and
sharper problem: lose the volume on a Wednesday, notice on Friday, and the
history is gone permanently.

**This gets worse the longer the app is used, which is the part worth stating
plainly.** In the first three months, losing the volume costs a reconnect and a
re-import — the bank still holds everything. After a year it costs nine months
that no longer exist anywhere. The exposure grows every day the app runs, and
nothing about the system signals that.

Not solved here, and not this repository's lane to solve. Options, roughly in
increasing cost: accept it and re-import the 90 days; lengthen the archive
retention; or add an application-level dump on its own schedule to a separate
volume — which is the pattern Paperless already uses in this estate for exactly
this reason, and is therefore the least novel answer available.

Worth knowing: securo has its own workspace-backup feature, including
password-encrypted exports. That may be the cheapest path to a second copy that
does not depend on the VM image at all.
