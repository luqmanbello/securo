"""Read-only Access Bank (Nigeria) internet-banking reader, as a Securo plugin.

Ported from the fork's native provider (`backend/app/providers/accessbank.py`),
which was ported from the Go client in the `worth` repository. It reads
balances and transaction history for the owner's own accounts and does nothing
else: there is no transfer, payment, beneficiary, statement, cheque or
token-request call here, and no OTP or device-challenge handling.

Only the four paths below may ever be requested. The plugin's tests check the
module's declared `_PATH_*` globals against that exact set, and separately
parse this file's source and check every `/`-leading string literal in it
against the same set, so an inline path literal slipped into a call site
cannot sail through undetected.

Account and transaction `external_id`s are the native provider's, byte for
byte: the full account number and the bank's transaction reference. Rows the
native provider imported are matched on them, so changing either duplicates
or loses money rows.

Standard library only (the runner starts plugins with `python -I -S`): HTTP is
urllib, which goes out through the runner's `HTTPS_PROXY`, and the password is
encrypted to the bank's RSA key with RSA-OAEP (SHA-256 for the digest and for
MGF1) written out below.

Nothing is persisted but `state["started"]`: the first sync of a connection
only links it and returns no data (see `Plugin.sync`). The password, the RSA ciphertext,
the bearer token and every raw response body live for the duration of one
sync. Errors carry a stage and a category, never a response body.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
import ssl
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

# The complete set of paths this module may ever request.
_PATH_CONFIG = "/api/config/"
_PATH_AUTHENTICATE = "/gateway/api/session-manager/session/authenticate"
_PATH_ACCOUNTS = "/gateway/api/customer-detail/fetch-customer-account-details"
_PATH_TRANSACTIONS = "/gateway/api/query-transaction/transaction-history"

#: The only host; the manifest's `network` declares it.
BASE_URL = "https://ibank.accessbankplc.com"

# A per-request socket timeout. The runner caps the whole sync call (60 s by
# default), so this is not a ceiling on one sync.
HTTP_TIMEOUT = 30.0

# Cap the body that is read and parsed.
_MAX_BODY_BYTES = 1 << 20

# The bank's own web client sends 20. Match it exactly.
_PAGE_SIZE = "20"

# The response carries no total, so paging ends on a short page. This cap
# stops a server that always returns a full page from looping forever.
_MAX_PAGES = 50

# The bank's UI offers 30 days and 3 months; the parameter is a rolling
# window. There is no observed way to ask for older data.
_MAX_DAYS = 90

# The contract's limits on a transaction's text fields.
_MAX_DESCRIPTION = 500
_MAX_PAYEE = 200
# The contract refuses an empty description; the bank can send no narration.
_NO_NARRATION = "Access Bank transaction"

_CERT_PATH = Path(__file__).with_name("certum-dv-tls-g2-r39.pem")

# DER for the rsaEncryption OID, 1.2.840.113549.1.1.1.
_RSA_OID = bytes.fromhex("2a864886f70d010101")

logger = logging.getLogger(__name__)


class NeedsAuth(Exception):
    """The bank rejected the stored user ID or password, or they are missing.

    Securo goes by the class name: the connection asks the owner to reconnect
    and stops syncing until they do. A wrong password is never retried, since
    retrying a stored bank credential is how it walks into an account lockout.
    """


class RateLimited(Exception):
    """The bank asked us to slow down. Not a failure: Securo tries again on the next run."""


class AccessBankError(RuntimeError):
    """A failure at one stage of the read.

    Carries a stage and a category, plus an optional detail. A response body,
    header, URL, token or password must never reach this message: `detail`
    exists only for values that are not sensitive (a currency code).
    """

    def __init__(self, stage: str, category: str, detail: str | None = None) -> None:
        self.stage = stage
        self.category = category
        self.detail = detail
        message = f"accessbank: {stage} failed ({category})"
        if detail:
            message = f"accessbank: {stage} failed ({category}: {detail})"
        super().__init__(message)


def _ssl_context() -> ssl.SSLContext:
    """System roots plus the intermediate the bank fails to send.

    Verification is never disabled. If the chain cannot be built, the read
    fails.
    """
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(_CERT_PATH))
    return ctx


# Explicit month table. `strptime("%b")` resolves against the active locale.
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# The bank sends uppercase. Compare exactly; a changed casing is a changed
# contract and should be reviewed, not absorbed.
_TXN_TYPES = {"CREDIT": "credit", "DEBIT": "debit"}

# An unmapped account type defaults to "savings", except one that names a
# card or a loan: defaulting debt to "savings" would report it as an asset,
# so that fails closed (see the native provider's comment on this table).
_ACCOUNT_TYPES = {"SAVINGS": "savings", "CURRENT": "checking"}

# Known-closed statuses, skipped before the ambiguity check. An allowlist, not
# `!= "ACTIVE"`: a status word never seen before still maps.
_CLOSED_ACCOUNT_STATUSES = {"CLOSED", "DORMANT", "INACTIVE"}


def _loads(body: bytes | str, stage: str) -> Any:
    """Decode a bank response with money-safe number handling.

    Transaction amounts arrive as bare JSON numbers; `parse_float=Decimal`
    keeps them exact instead of binary floats.
    """
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, TypeError) as exc:
        raise AccessBankError(stage, "schema") from exc


def _parse_money(raw: Any, stage: str) -> Decimal:
    """Exact money, or an error. Never an approximation."""
    if isinstance(raw, float):
        raise AccessBankError(stage, "float")
    if isinstance(raw, bool):
        raise AccessBankError(stage, "schema")
    if raw is None or raw == "":
        raise AccessBankError(stage, "schema")
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, int):
        value = Decimal(raw)
    elif isinstance(raw, str):
        text = raw.strip()
        # Scientific notation and anything non-numeric are refused outright.
        if not text or any(c in text for c in "eE"):
            raise AccessBankError(stage, "schema")
        try:
            value = Decimal(text)
        except InvalidOperation as exc:
            raise AccessBankError(stage, "schema") from exc
    else:
        raise AccessBankError(stage, "schema")

    if not value.is_finite():
        raise AccessBankError(stage, "schema")

    try:
        if value != value.quantize(Decimal("0.01")):
            # A three-decimal balance means the schema drifted. Abort rather
            # than round the owner's money on the bank's behalf.
            raise AccessBankError(stage, "schema")
    except InvalidOperation as exc:
        raise AccessBankError(stage, "schema") from exc
    return value


def _parse_date(raw: Any, stage: str) -> date:
    """Parse the bank's `DD-MMM-YYYY` with an uppercase month. Exactly one format."""
    if not isinstance(raw, str):
        raise AccessBankError(stage, "date")
    parts = raw.strip().split("-")
    if len(parts) != 3:
        raise AccessBankError(stage, "date")
    day, mon, year = parts
    if len(day) != 2 or len(year) != 4 or mon not in _MONTHS:
        raise AccessBankError(stage, "date")
    try:
        return date(int(year), _MONTHS[mon], int(day))
    except ValueError as exc:
        raise AccessBankError(stage, "date") from exc


def _map_txn_type(raw: Any, stage: str) -> str:
    """Map CREDIT/DEBIT onto credit/debit. Anything else stops the read."""
    if not isinstance(raw, str) or raw not in _TXN_TYPES:
        raise AccessBankError(stage, "type")
    return _TXN_TYPES[raw]


def _parse_currency_allowlist(raw: Any) -> set[str]:
    """The `import_currencies` field: comma-separated, whitespace tolerated,
    compared uppercase. Empty means import every currency."""
    if not isinstance(raw, str):
        return set()
    return {code.strip().upper() for code in raw.split(",") if code.strip()}


def _mask_last4(value: str) -> str | None:
    """The last four alphanumeric characters, as `app.providers.base.mask_last4`."""
    cleaned = "".join(ch for ch in value if ch.isalnum())
    if len(cleaned) < 4:
        return None
    return cleaned[-4:]


def _raise_for_status(status: int, stage: str) -> None:
    """Map a bank status onto the plugin's errors, stage-aware.

    401/403 means something different at each stage:
    - `config`: this GET is unauthenticated, so it cannot be a rejected
      credential; most likely a WAF block on the runner's IP. A plain error,
      so the owner isn't sent into a pointless password re-entry loop.
    - `authenticate`: the credential was presented and rejected: `NeedsAuth`.
    - `accounts` / `transactions`: the bearer token from this sync's own
      sign-in went stale. A plain error: the next sync signs in afresh.
    429 is rate limiting at every stage. Nothing is ever retried here.
    """
    if status in (401, 403):
        if stage == "authenticate":
            raise NeedsAuth("Access Bank rejected the stored user ID or password. Re-enter them to reconnect.")
        if stage in ("accounts", "transactions"):
            raise AccessBankError(stage, "session")
        raise AccessBankError(stage, "http")
    if status == 429:
        raise RateLimited("Access Bank is rate-limiting requests. Try again later.")
    if status < 200 or status >= 300:
        raise AccessBankError(stage, "http")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """A redirect could lead off the four paths: report it as its status instead."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


def _opener() -> urllib.request.OpenerDirector:
    # The default ProxyHandler reads HTTPS_PROXY, which the runner points at
    # its egress proxy; TLS to the bank passes through that tunnel.
    return urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=_ssl_context()),
        _NoRedirect(),
    )


def _send(opener: Any, method: str, path: str, body: bytes | None, headers: dict[str, str]) -> tuple[int, bytes]:
    """One request to `path` on the bank. Returns the status and at most `_MAX_BODY_BYTES + 1` bytes."""
    request = urllib.request.Request(BASE_URL + path, data=body, headers=headers, method=method)
    try:
        with opener.open(request, timeout=HTTP_TIMEOUT) as response:
            return response.status, response.read(_MAX_BODY_BYTES + 1)
    except urllib.error.HTTPError as exc:
        # The body of an error is never read: it must not reach a message.
        status = exc.code
        exc.close()
        return status, b""


def _as_dict(value: Any, stage: str) -> dict:
    """Treat a missing field as empty; never index a non-dict."""
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AccessBankError(stage, "schema")
    return value


# RSA ----------------------------------------------------------------------


def _der(buf: bytes, pos: int, tag: int) -> tuple[bytes, int]:
    """Read one DER element with `tag` at `pos`: its value and where the next element starts."""
    if pos + 2 > len(buf) or buf[pos] != tag:
        raise ValueError("unexpected DER tag")
    length = buf[pos + 1]
    pos += 2
    if length & 0x80:
        count = length & 0x7F
        # 0x80 is BER's indefinite length, never DER; four bytes is far past any key.
        if count == 0 or count > 4 or pos + count > len(buf):
            raise ValueError("bad DER length")
        length = int.from_bytes(buf[pos:pos + count], "big")
        pos += count
    if pos + length > len(buf):
        raise ValueError("truncated DER")
    return buf[pos:pos + length], pos + length


def _der_uint(value: bytes) -> int:
    if not value or value[0] & 0x80:
        raise ValueError("not a positive DER integer")
    return int.from_bytes(value, "big")


def _rsa_public_key(der: bytes) -> tuple[int, int]:
    """(n, e) from a DER SubjectPublicKeyInfo holding an RSA key; ValueError otherwise.

    SubjectPublicKeyInfo ::= SEQUENCE { SEQUENCE { OID, params }, BIT STRING }
    and the bit string holds RSAPublicKey ::= SEQUENCE { INTEGER n, INTEGER e }.
    """
    spki, end = _der(der, 0, 0x30)
    if end != len(der):
        raise ValueError("trailing bytes")
    algorithm, pos = _der(spki, 0, 0x30)
    oid, _ = _der(algorithm, 0, 0x06)
    if oid != _RSA_OID:
        raise ValueError("not an RSA key")
    bits, pos = _der(spki, pos, 0x03)
    if pos != len(spki) or not bits or bits[0] != 0:
        raise ValueError("bad public key bit string")
    key, end = _der(bits[1:], 0, 0x30)
    if end != len(bits) - 1:
        raise ValueError("trailing bytes")
    modulus, pos = _der(key, 0, 0x02)
    exponent, pos = _der(key, pos, 0x02)
    if pos != len(key):
        raise ValueError("trailing bytes")
    n, e = _der_uint(modulus), _der_uint(exponent)
    if e < 3 or e % 2 == 0 or n % 2 == 0 or e >= n:
        raise ValueError("not a usable RSA key")
    return n, e


def _decode_public_key(doc: Any) -> tuple[int, int]:
    """Read the RSA key the login form encrypts against, as (n, e).

    Every failure is fatal: without a key the bank published, strong enough
    to matter, there is nothing safe to encrypt the password to.
    """
    if not isinstance(doc, dict):
        raise AccessBankError("config", "schema")
    env = doc.get("env")
    if not isinstance(env, dict):
        raise AccessBankError("config", "schema")
    encoded = env.get("NEXT_PUBLIC_ENCRYPTION") or ""
    if not isinstance(encoded, str) or not encoded.strip():
        raise AccessBankError("config", "schema")
    try:
        der = base64.b64decode(encoded.strip(), validate=True)
        n, e = _rsa_public_key(der)
    except (ValueError, TypeError) as exc:
        raise AccessBankError("config", "key") from exc
    if n.bit_length() < 2048:
        # An undersized modulus means the document was swapped for a weak key.
        raise AccessBankError("config", "key")
    return n, e


def _mgf1(seed: bytes, length: int) -> bytes:
    """MGF1 with SHA-256 (RFC 8017, B.2.1)."""
    out = b""
    counter = 0
    while len(out) < length:
        out += hashlib.sha256(seed + counter.to_bytes(4, "big")).digest()
        counter += 1
    return out[:length]


def _xor(a: bytes, b: bytes) -> bytes:
    return bytes(x ^ y for x, y in zip(a, b, strict=True))


def _encrypt_password(key: tuple[int, int], password: str) -> str:
    """RSAES-OAEP (RFC 8017, 7.1.1) with SHA-256 for the digest and MGF1, empty label.

    The same scheme as the native provider's `cryptography` call and Go's
    `rsa.EncryptOAEP(sha256.New(), ...)`. Base64 of the ciphertext.
    """
    n, e = key
    message = password.encode("utf-8")
    k = (n.bit_length() + 7) // 8
    h_len = hashlib.sha256().digest_size
    if len(message) > k - 2 * h_len - 2:
        raise AccessBankError("authenticate", "password")
    l_hash = hashlib.sha256(b"").digest()
    db = l_hash + b"\x00" * (k - len(message) - 2 * h_len - 2) + b"\x01" + message
    seed = secrets.token_bytes(h_len)
    masked_db = _xor(db, _mgf1(seed, k - h_len - 1))
    masked_seed = _xor(seed, _mgf1(masked_db, h_len))
    encoded = int.from_bytes(b"\x00" + masked_seed + masked_db, "big")
    ciphertext = pow(encoded, e, n).to_bytes(k, "big")
    return base64.b64encode(ciphertext).decode("ascii")


def _token_claims(token: Any, stage: str) -> dict:
    """Decode a JWT payload WITHOUT verifying the signature.

    The claims supply ids echoed on later requests and are never used for any
    security decision.
    """
    if not isinstance(token, str) or token.count(".") != 2:
        raise AccessBankError(stage, "schema")
    payload = token.split(".")[1]
    padding_needed = "=" * (-len(payload) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload + padding_needed)
    except (ValueError, TypeError) as exc:
        raise AccessBankError(stage, "schema") from exc
    claims = _loads(raw, stage)
    if not isinstance(claims, dict):
        raise AccessBankError(stage, "schema")
    return claims


# The read ------------------------------------------------------------------


@dataclass
class _Session:
    """One authenticated session; lives for one sync.

    `login_user_id` is the owner's login username, sent as `userId` on the
    ACCOUNTS request (worth's proven request shape). `user_id` is the
    `userId` claim from the token, sent as `userID` on the TRANSACTIONS
    request (an inference; if live transactions come back empty, try
    `login_user_id` there first). They are not interchangeable.
    """

    token: str
    customer_id: str
    user_id: str
    login_user_id: str


class _Bank:
    def __init__(self) -> None:
        self._opener = _opener()

    def _get(self, path: str, stage: str) -> Any:
        return self._call("GET", path, None, stage, None)

    def _post(self, path: str, payload: dict, stage: str, token: str | None = None) -> Any:
        return self._call("POST", path, json.dumps(payload).encode("utf-8"), stage, token)

    def _call(self, method: str, path: str, body: bytes | None, stage: str, token: str | None) -> Any:
        headers = {"Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            status, raw = _send(self._opener, method, path, body, headers)
        except (OSError, ValueError) as exc:  # URLError, TLS and timeouts are OSErrors
            raise AccessBankError(stage, "network") from exc
        _raise_for_status(status, stage)
        if len(raw) > _MAX_BODY_BYTES:
            raise AccessBankError(stage, "oversize")
        return _loads(raw, stage)

    def open_session(self, user_id: str, password: str) -> _Session:
        """One config read and one authentication. Never retried."""
        key = _decode_public_key(self._get(_PATH_CONFIG, "config"))
        doc = self._post(
            _PATH_AUTHENTICATE,
            {"userId": user_id, "password": _encrypt_password(key, password)},
            "authenticate",
        )
        data = _as_dict(_as_dict(doc, "authenticate").get("data"), "authenticate")
        token = data.get("idToken") or ""
        if not token:
            # A 200 with no token is drifted schema or a rejected credential;
            # both are terminal and neither is retried.
            raise AccessBankError("authenticate", "schema")
        claims = _token_claims(token, "authenticate")
        # Every value on the wire is a string: coerce each claim as it is read.
        customer_id = str(claims.get("customerId") or "")
        claim_user_id = str(claims.get("userId") or "")
        if not customer_id or not claim_user_id:
            raise AccessBankError("authenticate", "schema")
        return _Session(token=token, customer_id=customer_id, user_id=claim_user_id, login_user_id=user_id)

    def accounts(self, session: _Session) -> Any:
        doc = self._post(
            _PATH_ACCOUNTS,
            # This endpoint takes customerId/userId, and userId is the LOGIN
            # username, not the claim. Do not swap these.
            {"customerId": session.customer_id, "userId": session.login_user_id},
            "accounts",
            token=session.token,
        )
        return _as_dict(doc, "accounts").get("data")

    def transactions(self, session: _Session, account_number: str, days: int) -> list[dict]:
        collected: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            doc = self._post(
                _PATH_TRANSACTIONS,
                {
                    # Casing differs from the accounts endpoint. Not a typo.
                    "customerID": session.customer_id,
                    "accountNo": account_number,
                    "pageNumber": str(page),
                    "pageSize": _PAGE_SIZE,
                    "noOfDays": str(days),
                    # The token's userId claim, not the login username.
                    "userID": session.user_id,
                },
                "transactions",
                token=session.token,
            )
            rows = _as_dict(doc, "transactions").get("data")
            if not isinstance(rows, list):
                raise AccessBankError("transactions", "schema")
            collected.extend(_map_transaction(row) for row in rows)
            # No total is returned, so a short page is the only end signal.
            if len(rows) < int(_PAGE_SIZE):
                break
        return collected


def _map_accounts(rows: Any, allowed_currencies: set[str]) -> list[dict]:
    if not isinstance(rows, list):
        raise AccessBankError("accounts", "schema")

    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            raise AccessBankError("accounts", "schema")
        status = row.get("accountStatus")
        if isinstance(status, str) and status in _CLOSED_ACCOUNT_STATUSES:
            continue
        number = row.get("accountNumber")
        currency = row.get("accountCurrency")
        acct_type = row.get("accountType")
        if not number or not currency or not acct_type:
            raise AccessBankError("accounts", "schema")
        account_type = _ACCOUNT_TYPES.get(str(acct_type))
        if account_type is None:
            if "CARD" in str(acct_type).upper() or "LOAN" in str(acct_type).upper():
                raise AccessBankError("accounts", "type")
            logger.warning("accessbank: unmapped account type %r; defaulting to savings", acct_type)
            account_type = "savings"
        parsed.append((str(number), str(currency), account_type, _parse_money(row.get("availableBalance"), "accounts")))

    # The owner's currency filter runs BEFORE the ambiguity check, so a
    # currency they didn't ask for can never abort an import of one they did.
    if allowed_currencies:
        parsed = [p for p in parsed if p[1].upper() in allowed_currencies]

    # Two accounts in one currency: map neither, rather than guess.
    duplicates = sorted(c for c, n in Counter(c for _, c, _, _ in parsed).items() if n > 1)
    if duplicates:
        raise AccessBankError("accounts", "ambiguous", detail=",".join(duplicates))

    return [
        {
            # The FULL account number, as the native provider: the
            # transactions endpoint takes it, and existing rows match on it.
            "external_id": number,
            "name": f"Access Bank {currency}",
            "type": account_type,
            "balance": str(balance),
            "currency": currency,
            "masked_number": _mask_last4(number),
        }
        for number, currency, account_type, balance in parsed
    ]


def _map_transaction(row: Any) -> dict:
    if not isinstance(row, dict):
        raise AccessBankError("transactions", "schema")
    ref = row.get("transactionReferenceNo")
    if not ref:
        raise AccessBankError("transactions", "schema")
    currency = row.get("transactionCurrency")
    if not currency:
        raise AccessBankError("transactions", "schema")
    # `beneficiary` and `sender` use "" for absence while the bank fields use
    # null. Neither may be read as the other.
    narration = row.get("transactionNarration") or ""
    amount = _parse_money(row.get("transactionAmount"), "transactions")
    if amount < 0:
        # A negative amount double-negates through `type`: a negative DEBIT
        # would become a credit. Zero is allowed. (A negative BALANCE is a
        # legitimate overdraft and stays allowed.)
        raise AccessBankError("transactions", "schema")
    payee = row.get("sender") or None
    return {
        "external_id": str(ref),
        "description": (str(narration) or _NO_NARRATION)[:_MAX_DESCRIPTION],
        "amount": str(amount),
        "date": _parse_date(row.get("transactionDate"), "transactions").isoformat(),
        "type": _map_txn_type(row.get("transactionType"), "transactions"),
        "currency": str(currency),
        "payee": str(payee)[:_MAX_PAYEE] if payee else None,
    }


def _window_days(since: str | None) -> int:
    """Days to request. `since` narrows the window; it never widens it past
    the bank's cap. Securo already rewinds `since` 14 days behind the last
    sync, which is the overlap this relies on."""
    if not since:
        return _MAX_DAYS
    elapsed = (date.today() - date.fromisoformat(since)).days
    if elapsed < 1:
        return 1
    return min(elapsed, _MAX_DAYS)


class Plugin:
    def sync(self, params: dict, context) -> dict:
        state = dict(params.get("state") or {})
        if not state.get("started"):
            # The first sync of a connection only links it, and never reaches
            # the bank: the owner's native Access Bank accounts are moved onto
            # this connection before the second sync, which then matches them
            # by external_id instead of importing duplicates. `data: null`
            # writes nothing and Securo keeps the state.
            return {"state": {**state, "started": True}, "data": None}
        user_id = context.secrets.get("user_id") or ""
        password = context.secrets.get("password") or ""
        if not user_id or not password:
            raise NeedsAuth("Access Bank credentials are missing. Re-enter them to reconnect.")
        allowed = _parse_currency_allowlist(context.config.get("import_currencies"))
        days = _window_days(params.get("since"))

        bank = _Bank()
        session = bank.open_session(str(user_id), str(password))
        accounts = _map_accounts(bank.accounts(session), allowed)
        for account in accounts:
            try:
                account["transactions"] = bank.transactions(session, account["external_id"], days)
            except RateLimited:
                # As the native sync does: skip this account's transactions
                # this run (a partial feed, nothing removed) and keep the rest.
                account["transactions"] = None
        return {"state": state, "data": {"contract": 1, "accounts": accounts}}


plugin = Plugin()
