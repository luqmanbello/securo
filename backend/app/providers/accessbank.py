"""Read-only Access Bank (Nigeria) internet-banking reader.

Ported from the Go client in the `worth` repository. It reads balances and
transaction history for the owner's own accounts and does nothing else: there
is no transfer, payment, beneficiary, statement, cheque or token-request call
here, and no OTP or device-challenge handling.

Only the four paths below may ever be requested. `test_only_four_paths_are_
declared` fails the suite if a fifth appears — the review step that replaces
`worth`'s build-time guard.

Nothing is persisted by this module. The password, the RSA ciphertext, the
bearer token and every raw response body live for the duration of one
operation. Errors carry a stage and a category, never a response body.
"""
from __future__ import annotations

import base64
import httpx
import json
import ssl
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.providers.base import ProviderRateLimited, ProviderUserActionRequired

# The complete set of paths this module may ever request.
_PATH_CONFIG = "/api/config/"
_PATH_AUTHENTICATE = "/gateway/api/session-manager/session/authenticate"
_PATH_ACCOUNTS = "/gateway/api/customer-detail/fetch-customer-account-details"
_PATH_TRANSACTIONS = "/gateway/api/query-transaction/transaction-history"

# One whole operation, not one hop.
ACCESSBANK_HTTP_TIMEOUT = 30.0

# Cap the body that is parsed. All call sites use non-streaming HTTP methods,
# so the body is buffered by the caller before this check runs. This bounds
# what is JSON-parsed and handed downstream, not what is downloaded.
_MAX_BODY_BYTES = 1 << 20

# The bank's own web client sends 20. Match it exactly.
_PAGE_SIZE = "20"

# The response carries no total, so paging ends on a short page. This cap
# stops a server that always returns a full page from looping forever.
_MAX_PAGES = 50

# The bank's UI offers 30 days and 3 months; the parameter is a rolling
# window. There is no observed way to ask for older data.
_MAX_DAYS = 90

_CERT_PATH = Path(__file__).with_name("certum-dv-tls-g2-r39.pem")


class AccessBankError(RuntimeError):
    """A failure at one stage of the read.

    Carries a stage and a category only. A response body, header, URL, token
    or password must never reach this message.
    """

    def __init__(self, stage: str, category: str) -> None:
        self.stage = stage
        self.category = category
        super().__init__(f"accessbank: {stage} failed ({category})")


def _ssl_context() -> ssl.SSLContext:
    """System roots plus the intermediate the bank fails to send.

    Verification is never disabled. If the chain cannot be built, the read
    fails.
    """
    ctx = ssl.create_default_context()
    ctx.load_verify_locations(cafile=str(_CERT_PATH))
    return ctx


# Explicit month table. `strptime("%b")` resolves against the active locale,
# so a container with a non-English locale would fail — or worse, parse
# differently — for reasons unrelated to the bank.
_MONTHS = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}

# The bank sends uppercase. Compare exactly; do not normalise case, because a
# changed casing is a changed contract and should be reviewed, not absorbed.
_TXN_TYPES = {"CREDIT": "credit", "DEBIT": "debit"}


def _loads(body: bytes | str, stage: str) -> Any:
    """Decode a bank response with money-safe number handling.

    `parse_float=Decimal` is the reason this helper exists. Transaction
    amounts arrive as bare JSON numbers, and the default decoder turns those
    into binary floats, which silently rounds money.
    """
    try:
        return json.loads(body, parse_float=Decimal)
    except (ValueError, TypeError) as exc:
        raise AccessBankError(stage, "schema") from exc


def _parse_money(raw: Any, stage: str) -> Decimal:
    """Exact money, or an error. Never an approximation.

    A `float` argument means the caller did not use `_loads`; that is a bug in
    this module and is raised rather than tolerated.
    """
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
            # A three-decimal balance means the schema drifted. Abort rather than
            # round the owner's money on the bank's behalf.
            raise AccessBankError(stage, "schema")
    except InvalidOperation as exc:
        raise AccessBankError(stage, "schema") from exc
    return value


def _parse_date(raw: Any, stage: str) -> date:
    """Parse the bank's `DD-MMM-YYYY` with an uppercase month.

    Exactly one format is accepted. A date that parses under the wrong format
    lands a transaction on the wrong day, which moves the net-worth trend
    line, which is the product.
    """
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
    """Map the bank's CREDIT/DEBIT onto Securo's credit/debit.

    Fails closed. Securo's balance arithmetic treats anything that is not
    exactly "credit" as money out, and validates nothing, so an unrecognised
    value must stop the read rather than pick a direction.
    """
    if not isinstance(raw, str) or raw not in _TXN_TYPES:
        raise AccessBankError(stage, "type")
    return _TXN_TYPES[raw]


def _raise_for_status(status: int, stage: str) -> None:
    """Map a bank status onto Securo's provider exceptions.

    401, 403 and 429 are terminal and are never retried: retrying a stored
    bank credential is how a wrong password walks into an account lockout.
    """
    if status in (401, 403):
        raise ProviderUserActionRequired(
            "Access Bank rejected the stored credential. Re-enter it to reconnect.",
            code="accessbank_credential_rejected",
        )
    if status == 429:
        raise ProviderRateLimited("Access Bank is rate-limiting requests. Try again later.")
    if status >= 400:
        raise AccessBankError(stage, "http")
    return None


def _read_body(response: httpx.Response, stage: str) -> bytes:
    """Return the response body, capped for downstream parsing.

    The body is already buffered by the caller (non-streaming HTTP methods),
    so this bounds what is JSON-parsed and handed downstream, not what is
    downloaded from the network.
    """
    body = response.content
    if len(body) > _MAX_BODY_BYTES:
        raise AccessBankError(stage, "oversize")
    return body


def _decode_public_key(doc: Any) -> rsa.RSAPublicKey:
    """Read the RSA key the login form encrypts against.

    Every failure here is fatal rather than recoverable: without a key that
    the bank published and that is strong enough to matter, there is nothing
    safe to encrypt the password to.
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
    except (ValueError, TypeError) as exc:
        raise AccessBankError("config", "key") from exc
    try:
        key = serialization.load_der_public_key(der)
    except Exception as exc:  # cryptography raises several unrelated types
        raise AccessBankError("config", "key") from exc
    if not isinstance(key, rsa.RSAPublicKey):
        raise AccessBankError("config", "key")
    if key.key_size < 2048:
        # An undersized modulus means the document was swapped for a weak
        # key; refuse rather than encrypt the password to it.
        raise AccessBankError("config", "key")
    return key


@dataclass
class AccessBankSession:
    """One authenticated session. Lives for the duration of one operation."""

    token: str
    customer_id: str
    user_id: str


def _encrypt_password(key: rsa.RSAPublicKey, password: str) -> str:
    """RSA-OAEP with SHA-256 for both the digest and the MGF1 mask.

    Ported from Go's `rsa.EncryptOAEP(sha256.New(), ...)`, which uses the one
    supplied hash for both roles — so this is an exact match, not an
    approximation.
    """
    ciphertext = key.encrypt(
        password.encode("utf-8"),
        padding.OAEP(
            mgf=padding.MGF1(algorithm=hashes.SHA256()),
            algorithm=hashes.SHA256(),
            label=None,
        ),
    )
    return base64.b64encode(ciphertext).decode("ascii")


def _token_claims(token: Any, stage: str) -> dict:
    """Decode a JWT payload WITHOUT verifying the signature.

    The claims are not trusted for any security decision. They only supply the
    ids the next request must echo, exactly as the bank's own browser client
    does.
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
