"""Read-only Access Bank (Nigeria) internet-banking reader.

Ported from the Go client in the `worth` repository. It reads balances and
transaction history for the owner's own accounts and does nothing else: there
is no transfer, payment, beneficiary, statement, cheque or token-request call
here, and no OTP or device-challenge handling.

Only the four paths below may ever be requested. `test_only_four_paths_are_
declared` checks the module's declared `_PATH_*` globals against that exact
set; `test_no_forbidden_capability_in_paths` separately parses this file's
source and checks every `/`-leading string literal in it against the same
set, so an inline path literal slipped into a call site cannot sail through
undetected. Together they are the review step that replaces `worth`'s
build-time guard.

Nothing is persisted by this module. The password, the RSA ciphertext, the
bearer token and every raw response body live for the duration of one
operation. Errors carry a stage and a category, never a response body.
"""
from __future__ import annotations

import base64
import httpx
import json
import logging
import ssl
from collections import Counter
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Optional

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from app.agents.services.crypto import decrypt, encrypt
from app.providers.base import (
    AccountData,
    BankProvider,
    ConnectionData,
    ProviderRateLimited,
    ProviderUserActionRequired,
    SessionExpiredError,
    TransactionData,
    mask_last4,
)

# The complete set of paths this module may ever request.
_PATH_CONFIG = "/api/config/"
_PATH_AUTHENTICATE = "/gateway/api/session-manager/session/authenticate"
_PATH_ACCOUNTS = "/gateway/api/customer-detail/fetch-customer-account-details"
_PATH_TRANSACTIONS = "/gateway/api/query-transaction/transaction-history"

# httpx applies a bare float independently to EACH request phase (connect,
# read, write, pool acquisition), not once to a whole call — so this is a
# per-phase ceiling, not a ceiling on one operation. With _MAX_PAGES=50, one
# get_transactions call legitimately issues up to 50 requests, each phase of
# each individually capped at this value, so the call as a whole can
# legitimately run for many minutes.
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

logger = logging.getLogger(__name__)


class AccessBankError(RuntimeError):
    """A failure at one stage of the read.

    Carries a stage and a category, plus an optional detail. A response body,
    header, URL, token or password must never reach this message — `detail`
    exists only for values that are not sensitive (e.g. a currency code) and
    that materially help diagnose which of several instances of a category
    fired. `stage` and `category` alone must remain enough to match on for
    existing callers; `detail` is additive and never changes either.
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

# The bank sends uppercase. Compare exactly, same as _TXN_TYPES.
#
# Unlike every other fail-closed rule in this module, an unrecognised value
# here is NOT terminal, WITH ONE EXCEPTION below. Every other check (the
# amount, the date, the direction) protects money correctness. Account type
# does not affect money in Securo except for "credit_card", which flips the
# balance sign in `_account_balance_at` — so an account type must never
# default to "credit_card". Aborting an entire balance read because the bank
# labelled an account "DOMICILIARY" would be a worse trade than a defaulted
# display label, so `_map_accounts` defaults an unmapped type to "savings"
# and logs a warning instead of raising.
#
# THE EXCEPTION: "savings" is the right default for a deposit product, but it
# is wrong for a liability product — defaulting a credit card or a loan
# account to "savings" reports DEBT AS A POSITIVE ASSET and inflates net
# worth, which is money-visible, not a display label, and is exactly the
# class of bug every other fail-closed rule in this module exists to
# prevent. So `_map_accounts` raises `AccessBankError("accounts", "type")`
# for any unmapped type whose string contains "CARD" or "LOAN" (case
# insensitive), and only defaults everything else. Do not "tighten" this
# further, and do not remove the card/loan check, without re-reading this
# comment.
_ACCOUNT_TYPES = {"SAVINGS": "savings", "CURRENT": "checking"}

# Known-closed account statuses. `_map_accounts` skips these BEFORE the
# ambiguity check runs, so a closed duplicate-currency account cannot abort
# an otherwise-good balance read (see F6). The bank's status vocabulary is
# UNVERIFIED beyond "ACTIVE" — this set is what the design's original
# fixtures assumed, not something captured from the live API — so it is
# deliberately a known-closed ALLOWLIST rather than an `!= "ACTIVE"`
# denylist: a status word this module has never seen must fall through to
# mapping/ambiguity, not be silently dropped. Needs one live observation to
# confirm or extend.
_CLOSED_ACCOUNT_STATUSES = {"CLOSED", "DORMANT", "INACTIVE"}


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
    """Map a bank status onto Securo's provider exceptions, stage-aware.

    401/403 means something different at each stage:
    - `config`: this GET is UNAUTHENTICATED — no credential has been sent
      yet — so 401/403 here cannot be a rejected credential. It is far more
      likely a WAF/Cloudflare block on the reader's (datacenter) IP. Treating
      it as a rejected credential would flip the connection to an error state
      and send the owner into a pointless password re-entry loop, so this
      stays a plain `AccessBankError`.
    - `authenticate`: the credential really was just presented and really was
      rejected, so this is `ProviderUserActionRequired`.
    - `accounts` / `transactions`: the bearer token from a prior successful
      authenticate went stale mid-operation. That is a session expiry, not a
      bad credential, so this raises `SessionExpiredError`.
    Any other stage falls through to the generic `AccessBankError(stage,
    "http")` below rather than being guessed at.

    429 is rate limiting at every stage. Nothing here is ever retried:
    retrying a stored bank credential is how a wrong password walks into an
    account lockout.
    """
    if status in (401, 403):
        if stage == "config":
            raise AccessBankError("config", "http")
        if stage == "authenticate":
            raise ProviderUserActionRequired(
                "Access Bank rejected the stored credential. Re-enter it to reconnect.",
                code="accessbank_credential_rejected",
            )
        if stage in ("accounts", "transactions"):
            raise SessionExpiredError("Access Bank session expired mid-operation.")
        raise AccessBankError(stage, "http")
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


def _as_dict(value: Any, stage: str) -> dict:
    """Treat a missing field as empty; never index a non-dict.

    A field the bank omits is normal and yields an empty dict, so the caller
    can `.get()` its way through a chain of optional keys. A field the bank
    sends but that is not an object (a top-level list, string or number,
    e.g. the whole response body being `[]`) means the schema drifted from
    what this module expects, and must raise AccessBankError here rather
    than an uncaught AttributeError from the next `.get()` down the chain.
    Same defensive style as `_decode_public_key`'s `isinstance(env, dict)`
    check.
    """
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise AccessBankError(stage, "schema")
    return value


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


class AccessBankProvider(BankProvider):
    """Read-only Access Bank reader.

    Access Bank authenticates with an RSA-encrypted username and password
    rather than OAuth, so this follows the pattern SimpleFIN established for
    non-redirect providers: the OAuth-only methods raise, and
    `handle_oauth_callback` is repurposed as the credential-claim step.
    """

    @property
    def name(self) -> str:
        return "accessbank"

    @property
    def flow_type(self) -> str:
        return "credentials"

    def get_oauth_url(self, *args, **kwargs):  # type: ignore[override]
        raise NotImplementedError(
            "Access Bank uses an internet-banking username and password, not OAuth"
        )

    def _client(self) -> httpx.AsyncClient:
        from app.core.config import get_settings

        return httpx.AsyncClient(
            base_url=get_settings().accessbank_base_url,
            timeout=ACCESSBANK_HTTP_TIMEOUT,
            verify=_ssl_context(),
        )

    async def _post(
        self,
        client: httpx.AsyncClient,
        path: str,
        payload: dict,
        stage: str,
        token: Optional[str] = None,
    ) -> Any:
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            response = await client.post(path, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            raise AccessBankError(stage, "network") from exc
        _raise_for_status(response.status_code, stage)
        return _loads(_read_body(response, stage), stage)

    async def _open_session(
        self, client: httpx.AsyncClient, credentials: dict
    ) -> AccessBankSession:
        """One config read and one authentication. Never retried."""
        user_id = credentials.get("user_id") or ""
        password_enc = credentials.get("password_enc") or ""
        if not user_id or not password_enc:
            raise ProviderUserActionRequired(
                "Access Bank credentials are missing. Re-enter them to reconnect.",
                code="accessbank_credential_missing",
            )
        password = decrypt(password_enc)
        if not password:
            # Present but unreadable (e.g. a rotated SECRET_KEY) is NOT the
            # same as missing: silently treating it as "not configured"
            # would disable the importer without telling the owner why.
            raise ProviderUserActionRequired(
                "The stored Access Bank credential could not be read. "
                "Re-enter it to reconnect.",
                code="accessbank_credential_unreadable",
            )
        try:
            response = await client.get(_PATH_CONFIG, headers={"Accept": "application/json"})
        except httpx.HTTPError as exc:
            raise AccessBankError("config", "network") from exc
        _raise_for_status(response.status_code, "config")
        key = _decode_public_key(_loads(_read_body(response, "config"), "config"))

        doc = await self._post(
            client,
            _PATH_AUTHENTICATE,
            {"userId": user_id, "password": _encrypt_password(key, password)},
            "authenticate",
        )
        data = _as_dict(_as_dict(doc, "authenticate").get("data"), "authenticate")
        token = data.get("idToken") or ""
        if not token:
            # A 200 with no token is either drifted schema or a rejected
            # credential. Both are terminal; neither is retried.
            raise AccessBankError("authenticate", "schema")
        claims = _token_claims(token, "authenticate")
        # Every value on the wire is a string (see M3): coerce with str() at
        # the point each claim is read, not wherever it later gets used.
        customer_id = str(claims.get("customerId") or "")
        if not customer_id:
            raise AccessBankError("authenticate", "schema")
        # The claim, NOT the login username: the bank's internal userId can
        # differ from what the owner typed to sign in, and later requests
        # must echo what the bank itself calls the session — exactly like
        # customerId above, and exactly as the bank's own browser client
        # does (see the module docstring on `_token_claims`).
        claim_user_id = str(claims.get("userId") or "")
        if not claim_user_id:
            raise AccessBankError("authenticate", "schema")
        return AccessBankSession(token=token, customer_id=customer_id, user_id=claim_user_id)

    def _map_accounts(self, rows: Any) -> list[AccountData]:
        if not isinstance(rows, list):
            raise AccessBankError("accounts", "schema")

        parsed = []
        for row in rows:
            if not isinstance(row, dict):
                raise AccessBankError("accounts", "schema")
            status = row.get("accountStatus")
            if isinstance(status, str) and status in _CLOSED_ACCOUNT_STATUSES:
                # See _CLOSED_ACCOUNT_STATUSES: skip BEFORE the ambiguity
                # check below runs, so a closed duplicate-currency account
                # cannot abort an otherwise-good read.
                logger.info(
                    "accessbank: skipping account with status %r", status
                )
                continue
            number = row.get("accountNumber")
            currency = row.get("accountCurrency")
            acct_type = row.get("accountType")
            if not number or not currency or not acct_type:
                raise AccessBankError("accounts", "schema")
            account_type = _ACCOUNT_TYPES.get(str(acct_type))
            if account_type is None:
                # See the comment on _ACCOUNT_TYPES: defaulted, not raised —
                # EXCEPT a card or loan product, which fails closed rather
                # than reporting debt as a positive asset.
                if "CARD" in str(acct_type).upper() or "LOAN" in str(acct_type).upper():
                    raise AccessBankError("accounts", "type")
                logger.warning(
                    "accessbank: unmapped account type %r; defaulting to savings",
                    acct_type,
                )
                account_type = "savings"
            parsed.append((str(number), str(currency), account_type,
                           _parse_money(row.get("availableBalance"), "accounts")))

        # Two accounts in one currency: map neither, rather than guess which
        # holding each belongs to. Name the ambiguous currency(-ies) in the
        # error — currency codes are not sensitive, and naming them is what
        # makes this diagnosable instead of just "something is ambiguous".
        duplicates = sorted(c for c, n in Counter(c for _, c, _, _ in parsed).items() if n > 1)
        if duplicates:
            raise AccessBankError("accounts", "ambiguous", detail=",".join(duplicates))

        return [
            AccountData(
                external_id=number,
                name=f"Access Bank {currency}",
                type=account_type,
                balance=balance,
                currency=currency,
                masked_number=mask_last4(number),
            )
            for number, currency, account_type, balance in parsed
        ]

    async def get_accounts(self, credentials: dict) -> list[AccountData]:
        async with self._client() as client:
            session = await self._open_session(client, credentials)
            doc = await self._post(
                client,
                _PATH_ACCOUNTS,
                # Note the casing: this endpoint takes customerId/userId.
                {"customerId": session.customer_id, "userId": session.user_id},
                "accounts",
                token=session.token,
            )
            return self._map_accounts(_as_dict(doc, "accounts").get("data"))

    def _map_transaction(self, row: Any) -> TransactionData:
        if not isinstance(row, dict):
            raise AccessBankError("transactions", "schema")
        ref = row.get("transactionReferenceNo")
        if not ref:
            raise AccessBankError("transactions", "schema")
        currency = row.get("transactionCurrency")
        if not currency:
            raise AccessBankError("transactions", "schema")
        # `beneficiary` and `sender` use "" for absence while the *bank* fields
        # use null. Neither may be read as the other.
        narration = row.get("transactionNarration") or ""
        amount = _parse_money(row.get("transactionAmount"), "transactions")
        if amount < 0:
            # Guarded HERE, not in `_parse_money`: a negative balance is a
            # legitimate overdraft on an account and must keep working, but a
            # negative transaction amount double-negates through Securo's
            # `case(type=='credit', +amt, else -amt)` — a negative DEBIT
            # would become a credit, moving money the wrong way. Zero is
            # allowed; only strictly-negative is refused.
            raise AccessBankError("transactions", "schema")
        return TransactionData(
            external_id=str(ref),
            description=narration,
            amount=amount,
            date=_parse_date(row.get("transactionDate"), "transactions"),
            type=_map_txn_type(row.get("transactionType"), "transactions"),
            currency=str(currency),
            payee=(row.get("sender") or None),
        )

    def _window_days(self, since: Optional[date]) -> int:
        """Days to request. `since` narrows the window; it can never widen it
        past the bank's own cap, which no parameter appears to lift.

        This requests EXACTLY `elapsed` days — no margin of its own. The
        spec's "comfortably wider than the gap since last_sync_at" holds only
        because the caller (`app/services/connection_service.py`, the
        `get_transactions` call site around where it computes `since` from
        `last_sync_at`) subtracts an extra ~14 days before passing `since`
        in. That upstream margin is a dependency of this method's
        correctness, not an implementation detail of it — do not remove it
        from the caller without adding one here instead.
        """
        if since is None:
            return _MAX_DAYS
        elapsed = (date.today() - since).days
        if elapsed < 1:
            return 1
        return min(elapsed, _MAX_DAYS)

    async def get_transactions(
        self,
        credentials: dict,
        account_external_id: str,
        since: Optional[date] = None,
        payee_source: str = "auto",
    ) -> list[TransactionData]:
        days = str(self._window_days(since))
        collected: list[TransactionData] = []

        async with self._client() as client:
            session = await self._open_session(client, credentials)
            for page in range(1, _MAX_PAGES + 1):
                doc = await self._post(
                    client,
                    _PATH_TRANSACTIONS,
                    {
                        # Casing differs from the accounts endpoint. Not a typo.
                        "customerID": session.customer_id,
                        "accountNo": str(account_external_id),
                        "pageNumber": str(page),
                        "pageSize": _PAGE_SIZE,
                        "noOfDays": days,
                        "userID": session.user_id,
                    },
                    "transactions",
                    token=session.token,
                )
                rows = _as_dict(doc, "transactions").get("data")
                if not isinstance(rows, list):
                    raise AccessBankError("transactions", "schema")
                collected.extend(self._map_transaction(row) for row in rows)
                # No total is returned, so a short page is the only signal
                # that the history has been exhausted.
                if len(rows) < int(_PAGE_SIZE):
                    break

        return collected

    @staticmethod
    def _decode_claim_payload(code: str) -> dict:
        """The connect screen posts the credentials as a JSON blob.

        `handle_oauth_callback` is the interface's entry point for claiming a
        connection, so it carries the credentials here rather than an OAuth
        code — the same repurposing SimpleFIN does for its setup token.
        """
        try:
            payload = json.loads(code)
        except (ValueError, TypeError) as exc:
            raise AccessBankError("connect", "schema") from exc
        if not isinstance(payload, dict):
            raise AccessBankError("connect", "schema")
        user_id = payload.get("user_id") or ""
        password = payload.get("password") or ""
        if not user_id or not password:
            raise ProviderUserActionRequired(
                "Enter both your Access Bank user id and password.",
                code="accessbank_credential_missing",
            )
        return {"user_id": user_id, "password": password}

    async def handle_oauth_callback(self, code: str) -> ConnectionData:
        plaintext = self._decode_claim_payload(code)
        password_enc = encrypt(plaintext["password"])
        if not password_enc:
            # _decode_claim_payload already guarantees a non-empty password,
            # so a None here means encrypt() itself is broken, not that the
            # credential was absent. Never fall back to storing plaintext.
            raise AccessBankError("connect", "crypto")
        credentials = {"user_id": plaintext["user_id"], "password_enc": password_enc}
        async with self._client() as client:
            session = await self._open_session(client, credentials)
            doc = await self._post(
                client,
                _PATH_ACCOUNTS,
                {"customerId": session.customer_id, "userId": session.user_id},
                "accounts",
                token=session.token,
            )
            accounts = self._map_accounts(_as_dict(doc, "accounts").get("data"))

        return ConnectionData(
            external_id=session.customer_id,
            institution_name="Access Bank",
            credentials=credentials,
            accounts=accounts,
        )

    async def refresh_credentials(self, credentials: dict) -> dict:
        """No-op. There is no refresh token; every operation authenticates
        afresh and the bearer token never outlives it."""
        return credentials
