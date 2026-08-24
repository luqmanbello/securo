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

import ssl
from pathlib import Path

# The complete set of paths this module may ever request.
_PATH_CONFIG = "/api/config/"
_PATH_AUTHENTICATE = "/gateway/api/session-manager/session/authenticate"
_PATH_ACCOUNTS = "/gateway/api/customer-detail/fetch-customer-account-details"
_PATH_TRANSACTIONS = "/gateway/api/query-transaction/transaction-history"

# One whole operation, not one hop.
ACCESSBANK_HTTP_TIMEOUT = 30.0

# Cap every response read. A bank that starts streaming fails closed rather
# than exhausting the container.
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
