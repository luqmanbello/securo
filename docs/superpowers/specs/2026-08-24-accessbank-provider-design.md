# Access Bank provider for Securo — design

Date: 2026-08-24
Status: approved, not yet implemented
Repository: `luqmanbello/securo` (fork of `securo-finance/securo`), branch `feat/accessbank-provider`

## Why

The owner is migrating from `worth` (a Go balance-tracker) to Securo. Securo ships
providers for Brazil (Pluggy), Europe (Enable Banking) and the US (SimpleFIN). There
is no Nigerian provider, so Access Bank balances would have to be typed by hand.

`worth` already contains a proven, read-only Access Bank client
(`internal/bank/bank.go`, 433 lines, 786 lines of tests). This design ports that
client to Python and extends it with transaction history, which `worth` deliberately
never implemented.

The scope change is explicit: `worth` forbade transaction history structurally, and
its `internal/guard` failed the build if such an endpoint appeared. Securo is a
transaction-driven application, and the owner asked on 2026-08-24 for transaction
history so the ledger actually populates. That widening is intentional and recorded
here rather than discovered later.

## Non-goals

No transfers, payments, beneficiaries, statement generation, cheque services, token
requests, or OTP/device-challenge handling. Those endpoints exist on the bank's
gateway; none of them appear in this client.

No full historical backfill. The bank caps history (see "History depth" below).

No multi-tenant use. This is a single-owner instance reading the owner's own account.

## Architecture

One new file plus two registration lines. Nothing else upstream is modified, which
keeps `git merge upstream/main` cheap — the explicit reason the owner chose to stay
on Securo's rails.

    backend/app/providers/accessbank.py     new — the provider
    backend/app/providers/__init__.py       +1 register_provider() call, +1 KNOWN_PROVIDERS entry
    backend/tests/test_accessbank.py        new — tests

`AccessBankProvider` implements `BankProvider` (`backend/app/providers/base.py`).
Access Bank uses a username/password handshake, not OAuth, so it follows the
established non-OAuth pattern SimpleFIN already sets:

- `flow_type()` returns a credentials flow, not `"oauth"`.
- `get_oauth_url()` and `reauth_url()` raise `NotImplementedError` with a message
  naming the real flow, exactly as `simplefin.py:253` does.
- `handle_oauth_callback()` is repurposed as the credential-claim step: it takes the
  submitted credential blob, performs one authentication, and returns `ConnectionData`
  carrying the accounts and the credentials to persist.

### Internal structure of the module

A thin private HTTP layer under the provider, mirroring `worth`'s separation:

- `_public_key(client)` — fetch and validate the RSA key
- `_authenticate(client, enc_password)` — exchange for a bearer token, read claims
- `_fetch_accounts(client, token, customer_id)` — balances
- `_fetch_transactions(client, token, customer_id, user_id, account_no, days)` — paged history
- `_parse_money(raw)`, `_parse_date(raw)`, `_map_type(raw)` — fail-closed parsers

The public `BankProvider` methods compose these. No public method performs more than
one logical bank operation.

## Wire contract

All paths are on `https://ibank.accessbankplc.com` and are declared as module-level
literal constants, so editing one is a visible diff rather than a runtime surprise.

    _PATH_CONFIG        = "/api/config/"
    _PATH_AUTHENTICATE  = "/gateway/api/session-manager/session/authenticate"
    _PATH_ACCOUNTS      = "/gateway/api/customer-detail/fetch-customer-account-details"
    _PATH_TRANSACTIONS  = "/gateway/api/query-transaction/transaction-history"

The first three are ported verbatim from `worth`. The fourth was captured from a live
session on 2026-08-24 and is new.

### 1. Public key — `GET /api/config/`

Response contains `env.NEXT_PUBLIC_ENCRYPTION`: a base64 (standard alphabet) DER
PKIX public key.

Rules, all ported from `worth` and all fail-closed:

- Missing or empty field → schema error, abort.
- Not valid base64, or not parseable as PKIX → key error, abort.
- Not an RSA key → key error, abort.
- Modulus under 2048 bits → key error, abort. An undersized modulus means the
  document was swapped for a weak key; the password must not be encrypted to it.

Python: `cryptography.hazmat.primitives.serialization.load_der_public_key`, then
assert `isinstance(key, rsa.RSAPublicKey)` and `key.key_size >= 2048`.

### 2. Authenticate — `POST /session-manager/session/authenticate`

The password is encrypted with **RSA-OAEP using SHA-256 for both the digest and the
MGF1 mask**, with no label, then base64-encoded (standard alphabet).

Go source being ported: `rsa.EncryptOAEP(sha256.New(), rand.Reader, pub, password, nil)`.
Python equivalent:

    key.encrypt(
        password.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )

Go's `EncryptOAEP` uses the single supplied hash for both roles, so this is an exact
match, not an approximation.

The response yields `data.idToken`, a JWT. Its payload segment (base64url, unpadded)
carries `customerId` and `userId`. These are read to echo back on later requests,
exactly as the browser client does. **They are not trusted for any security
decision** — the same caveat `worth` documents.

### 3. Balances — `POST /fetch-customer-account-details`

Confirmed still live and unchanged on 2026-08-24.

Per-account fields consumed: `accountNumber`, `accountType`, `accountStatus`,
`accountCurrency`, `availableBalance`.

Mapped to `AccountData`. Account numbers are masked at parse time via the existing
`mask_last4` helper in `base.py`; no full account number is retained in memory
beyond the parse, matching `worth`'s posture.

Ambiguity rules, ported unchanged:

- Two accounts sharing one currency → map neither, and report which currency was
  ambiguous. Guessing which is which is worse than refusing.
- Missing field, three-decimal balance, or scientific notation → abort the whole
  read. A partial read that fills a wrong number is the failure mode being prevented.

### 4. Transactions — `POST /query-transaction/transaction-history`

Request body. Every value is a string, including the numeric ones:

    {
      "customerID": "<from JWT claims>",
      "accountNo":  "<account number>",
      "pageNumber": "1",
      "pageSize":   "20",
      "noOfDays":   "90",
      "userID":     "<from JWT claims>"
    }

Response:

    {
      "statusCode":  <number>,
      "description": <null or string>,
      "data": [
        {
          "transactionReferenceNo": "<string>",
          "externalRef":            <null or string>,
          "transactionAmount":      <JSON number>,
          "transactionCurrency":    "<3-letter code>",
          "transactionDate":        "<11-character string>",
          "transactionType":        "<string>",
          "banktype":               <null or string>,
          "beneficiarybank":        <null or string>,
          "transactionNarration":   "<string>",
          "sender":                 "<string>",
          "beneficiary":            "<string, may be empty>",
          "beneficiaryaccount":     <null or string>,
          "transactionCategory":    "<string>"
        }
      ]
    }

## Handling rules that are not obvious

### Money must never pass through a float

Balances arrive as strings. Transaction amounts arrive as **bare JSON numbers**. A
default `json.loads` turns those into Python floats, which silently rounds money.

Every response body in this module is parsed with:

    json.loads(body, parse_float=Decimal)

This is the single most important line in the port. `worth` avoided the problem by
holding balances as `json.RawMessage` and parsing exactly; the transaction endpoint
gives no such option, so the decoder itself must be configured.

Amounts are then converted to Securo's `Decimal` money fields with no intermediate
`float()` call anywhere in the path.

### Confirmed values (verified against the USD account, 2026-08-24)

A second live session confirmed the contract against **Dom Savings Classic**, the
domiciliary account, and paged forward one page. Results:

- The USD account returns an **identical shape** to the naira account. Same fields,
  same types, same date format. No domiciliary-specific handling is needed.
- `transactionType` is exactly `"CREDIT"` or `"DEBIT"` — **uppercase**. Both values
  were observed on one page. This is the complete set as far as observation goes.
- `transactionDate` is `DD-MMM-YYYY` with an **uppercase** month abbreviation:
  `09-AUG-2026`, `23-AUG-2026`.
- `transactionCurrency` was uniform per account (`USD` throughout on the USD
  account), consistent with it describing the account's own denomination.
- `transactionCategory` varies: `ATM`, `POS`, `Others` observed. Not used for any
  money decision, so the set need not be exhaustive.
- **The bank lists exactly two accounts**, one NGN and one USD. The refuse-to-guess
  rule therefore will not fire today, but it is retained: a second USD account opened
  later must not silently start mapping.

Field nullability, counted over one 15-row page:

| Field | Behaviour |
|---|---|
| `externalRef` | null on some rows, populated on others — handle both |
| `banktype`, `beneficiarybank`, `beneficiaryaccount` | null on every observed row |
| `beneficiary` | never null; **empty string** marks absence |
| `sender` | never null; empty string on some rows |

So absence is expressed as `null` on some fields and `""` on others. Neither may be
treated as the other, and `""` must not be read as a meaningful value.

No amount in the sample carried more than two decimal places. That is reassurance,
not a guarantee, and does not remove the need for `parse_float=Decimal`.

### Transaction type must map explicitly and fail closed

Securo's balance arithmetic is
`case(Transaction.type == "credit", +amount, else_=-amount)`
(`backend/app/services/dashboard_service.py:1154`). **Anything that is not exactly
`"credit"` is subtracted.** There is no validation of the `type` field anywhere in
the API layer — a wrong or unexpected value produces a silently wrong balance rather
than an error. This was confirmed empirically during the probe: a transaction sent
as `type: "income"` was accepted and subtracted.

Therefore the bank's `transactionType` is mapped through an explicit table to
Securo's `"credit"` / `"debit"`, and **an unrecognised value aborts the read**. It is
never defaulted. Defaulting in either direction produces wrong money quietly, which
is the exact failure class this whole client is built to avoid.

The confirmed mapping is:

    "CREDIT" -> "credit"     # money in
    "DEBIT"  -> "debit"      # money out

Comparison is on the exact uppercase string. Anything else aborts the read. It is
never defaulted in either direction.

### Date format must be pinned, not sniffed

`transactionDate` is `DD-MMM-YYYY` with an uppercase month: `23-AUG-2026`.

It must be parsed with **an explicit month-name table**, not `strptime("%d-%b-%Y")`.
`%b` resolves against the active locale, so a container with a non-English locale
would fail to parse — or worse, parse differently — for reasons entirely unrelated to
the bank. A twelve-entry dict keyed on the uppercase abbreviation is deterministic
everywhere.

Multi-format sniffing is forbidden. A date that parses under the wrong format lands a
transaction on the wrong day, which moves the net-worth trend line, which is the
product. Anything not matching the pinned format aborts the read.

### History depth is capped by the bank

The transaction UI offers only "Last 30 days" and "Last 3 months", and the parameter
is a rolling `noOfDays` rather than a date range. There is no observed way to request
older data.

Consequences, which must be stated in the UI rather than discovered:

- The first sync retrieves at most ~90 days.
- History deepens only as the app keeps syncing forward.
- Importing years of past transactions from this endpoint is not possible.

Incremental syncs request a window comfortably wider than the gap since
`last_sync_at` (clamped to 90), and rely on `transactionReferenceNo` for dedupe via
the existing `external_id` mechanism.

### Pagination has no total

The response carries no count, page count, or "has more" flag. The loop is therefore
"request page N until a page returns fewer than `pageSize` rows", with a hard
maximum page count. Without that cap, a server that always returns a full page loops
forever.

**Verified 2026-08-24.** `pageNumber` is honoured server-side: page 1 of the USD
account returned a full 20 rows and page 2 returned 15, so the short-page terminator
is real rather than assumed.

`pageSize` stays at **20**, the value the bank's own web client sends. Whether a
larger page is accepted was deliberately not probed: it would mean sending a
hand-crafted request the real client never sends, and the only prize is fewer round
trips. Matching the browser exactly is the safer default for a scraped API.

Note that the credit/debit filter in the bank's UI is applied **client-side** — it
triggers no request. Only the account, page number and day window reach the server.

### TLS chain

Access Bank does not send a complete certificate chain. `worth` embeds the Certum
intermediate (`internal/bank/certum-dv-tls-g2-r39.pem`) and builds a pool containing
it. The Python client ships the same PEM and passes it via the SSL context.

TLS verification is never disabled. If the chain fails, the read fails.

## Credentials

The owner chose Securo's native storage over `worth`'s env-var posture, explicitly so
that upstream updates and patches do not conflict with a local deviation.

Credentials are therefore persisted on `bank_connections.credentials`, encrypted with
`app.agents.services.crypto.encrypt` — Fernet, keyed by PBKDF2-SHA256 over the
application `SECRET_KEY` — the same mechanism SimpleFIN and Enable Banking use.

Two consequences are accepted, and are recorded here rather than left implicit:

1. **The ciphertext lives in Postgres and travels in every backup.** The key derives
   from `SECRET_KEY`, which lives in the same cluster. An attacker holding both a
   backup and the environment holds the password. Unlike a PSD2 token, an Access
   Bank internet-banking password is full account access, not read-only.
2. **Rotating `SECRET_KEY` invalidates the stored credential.** This is upstream's
   documented, intended behaviour.

One local hardening, chosen because it does not touch upstream files: upstream's
`decrypt()` returns `None` on failure, so a rotated key would present as "no
credentials" and the importer would quietly do nothing. This provider treats a
`None` decrypt of a credential blob that is present as an explicit
`ProviderUserActionRequired` — "stored credential unreadable, please re-enter" —
rather than as absence.

## Error handling

Bank failures map onto Securo's existing provider exceptions so the UI already knows
how to render them:

| Condition | Raised |
|---|---|
| 401, 403 — credential rejected | `ProviderUserActionRequired` |
| 429 — throttled | `ProviderRateLimited` |
| Token no longer valid | `SessionExpiredError` |
| Schema drift, bad key, bad date, unknown type | `RuntimeError` with a category |

**Nothing is ever retried.** 401, 403 and 429 are terminal. A retry loop against a
bank is how a stored credential walks into an account lockout; `worth` made this rule
explicit and it carries over unchanged.

Error messages carry a category only — never a response body, header, URL, or
credential. Raw responses, the bearer token, and the encrypted password exist only
for the duration of one operation.

Every response read is capped (1 MiB, as in `worth`) so a bank that starts streaming
fails closed rather than exhausting the container.

## Testing

`backend/tests/test_accessbank.py`, run under the existing `pytest` suite.

- Every credential, key and token used in a test is generated by that test. No
  fixture contains a real secret.
- All HTTP is served by a local stub. **No test contacts the real bank.** This
  mirrors `worth`'s rule and is the reason its suite can run in CI.
- An RSA keypair is generated per test; the OAEP round-trip is verified by decrypting
  in the test rather than by asserting on ciphertext bytes.

Cases that must exist, each corresponding to a rule above:

1. Happy path — accounts and transactions map correctly.
2. Undersized RSA modulus is refused.
3. Malformed or missing `NEXT_PUBLIC_ENCRYPTION` is refused.
4. Two accounts in one currency map nothing and name the currency.
5. Three-decimal and scientific-notation balances abort the read.
6. A transaction amount with many decimal places survives as an exact `Decimal`
   (guards the `parse_float=Decimal` line specifically).
7. An unrecognised `transactionType` aborts rather than defaulting.
8. A `transactionDate` not matching the pinned format aborts.
9. Pagination stops on a short page.
10. Pagination stops at the hard cap when every page is full.
11. 401, 403 and 429 raise the mapped exception and are not retried — asserted by
    counting stub requests.
12. A present-but-undecryptable credential raises "re-enter", not "not configured".
13. No error message contains the password, the token, or a response body.

## Accepted risks

- **Read-only is convention here, not construction.** `worth` enforced it with
  `internal/guard`, which failed the build when a forbidden path appeared. Securo has
  no equivalent, and `BankProvider.get_transactions` is an `@abstractmethod`, so the
  interface expects the transaction read. The tests above check the path list, but a
  future edit can add an endpoint without the build objecting.
- **Scraping a bank's own web client is brittle by nature.** Access Bank can change
  these endpoints without notice. The fail-closed parsing means a change produces a
  visible error rather than a wrong number, which is the best available outcome.
- **The bank password is stored**, as described above.

## The account that actually matters is the USD one

The owner clarified on 2026-08-24 that the Access Bank **USD (domiciliary)** account
is the one worth importing; the naira account is not needed.

This barely moves the design — the provider returns every account the bank lists and
Securo links the ones the owner picks, so "only USD" is a linking choice, not a code
path. Three things do change:

1. **~~The discovery was performed against the naira account.~~ Re-verified
   2026-08-24 against Dom Savings Classic: the USD account returns an identical
   contract.** No domiciliary-specific handling is required. See "Confirmed values"
   below.
2. **FX largely disappears for this account.** With Securo's primary currency set to
   USD and the account denominated in USD, no conversion happens: no rate lookup, no
   frozen rate, no OpenExchangeRates dependency, and none of the 1:1-fallback hazard
   noted during the probe. That hazard applies only to holdings in other currencies.
3. **The ambiguity rule now hinges on the USD side.** `worth`'s rule — two accounts
   sharing one currency map nothing — fires only if the bank lists more than one USD
   account. Whether it does is unknown and is part of the re-verification above.

NGN support is a separate question from this provider, and the owner confirmed on
2026-08-24 that naira holdings **are** still tracked in Securo, entered by hand. So
the NGN currency work stays on the migration list — it is simply not fed by this
importer.

## Open items

All four items raised by the first draft were resolved in the 2026-08-24
verification session:

1. ~~Re-verify the contract against the USD account~~ — done; identical.
2. ~~The exact `transactionDate` format~~ — `DD-MMM-YYYY`, uppercase month.
3. ~~The complete set of `transactionType` values~~ — `CREDIT` and `DEBIT`.
4. ~~Whether `pageSize` may exceed 20~~ — deliberately not probed; pinned at 20 to
   match the bank's own client.

One assumption remains and cannot be closed by observation: the value sets above are
what a live account produced on one day. They are treated as the complete set, and
every parser fails closed on anything outside them — which is precisely how a
surprise becomes a visible error instead of wrong money.
