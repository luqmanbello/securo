"""LLM-assisted categorization for transactions no rule matched.

Rules run first and are deterministic; this is the fallback for what they
miss. The design constraints, in order of importance:

1. **It writes directly.** This is not an agent proposal — there is no
   Apply button in the loop. The MCP `propose_*` tools keep their
   `external AND apply` gate untouched; this service is a separate path
   that owns its own write, so nothing here weakens that boundary.

2. **The model never sees or returns a UUID.** Categories and
   transactions are handed over as small integer indices and mapped back
   locally. A hallucinated index is out of range and gets dropped, which
   is a far cheaper failure than a hallucinated UUID that happens to
   parse.

3. **Low confidence means "leave it alone".** An uncategorized row is a
   visible prompt on the dashboard; a confidently wrong one is silent.
   When in doubt we prefer the former, so anything under the threshold
   is left for the human.

4. **It fails quiet.** Every failure mode — agents off, no connection,
   provider down, unparseable reply — returns a status and zero writes.
   This runs after every bank sync, and a sync must never fail because
   an LLM did.
"""
from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.category import Category
from app.models.transaction import Transaction
from app.services import transaction_service

logger = logging.getLogger(__name__)


# How many uncategorized rows to send in one call. The prompt stays well
# inside a small model's context at this size, and a bank sync rarely
# lands more than a handful at once.
DEFAULT_LIMIT = 60

# Below this, we leave the transaction uncategorized for the human. 0.7 is
# deliberately cautious: the cost of a wrong category is a wrong report,
# and the cost of a skip is a badge the user already knows how to clear.
DEFAULT_MIN_CONFIDENCE = 0.7

# Already-categorized rows shown to the model as examples. This is where
# the accuracy comes from — it teaches the user's own vocabulary
# ("Lending", "Family Upkeep") which no general model would guess.
MAX_EXAMPLES = 60

_STATUS_OK = "ok"
_STATUS_DISABLED = "disabled"
_STATUS_NO_CANDIDATES = "no_candidates"
_STATUS_NO_CATEGORIES = "no_categories"
_STATUS_NO_PROVIDER = "no_provider"
_STATUS_LLM_ERROR = "llm_error"
_STATUS_UNPARSEABLE = "unparseable"


@dataclass
class AutoCategorizeResult:
    status: str
    considered: int = 0
    categorized: int = 0
    skipped_low_confidence: int = 0
    detail: str = ""
    # category name -> count, for logging and the API response
    by_category: dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "considered": self.considered,
            "categorized": self.categorized,
            "skipped_low_confidence": self.skipped_low_confidence,
            "detail": self.detail,
            "by_category": self.by_category,
        }


def _candidate_filters(workspace_id: uuid.UUID) -> list:
    """The uncategorized set, defined exactly as the dashboard badge defines it.

    Kept deliberately in lockstep with `dashboard_service.pending_cat_filters`
    so that clearing the badge and running this service converge on the same
    rows. Opening balances and settlements are bookkeeping artefacts rather
    than spending, and a paired transfer already has both legs accounted for.
    """
    return [
        Transaction.workspace_id == workspace_id,
        Transaction.category_id.is_(None),
        Transaction.source != "opening_balance",
        Transaction.source != "settlement",
        Transaction.transfer_pair_id.is_(None),
    ]


async def _load_candidates(
    session: AsyncSession, workspace_id: uuid.UUID, limit: int
) -> list[Transaction]:
    result = await session.execute(
        select(Transaction)
        .where(*_candidate_filters(workspace_id))
        .order_by(Transaction.date.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def _load_categories(
    session: AsyncSession, workspace_id: uuid.UUID
) -> list[Category]:
    """Assignable categories.

    Hidden categories are excluded: the user hid them precisely so new
    transactions stop landing there, and honouring that is the difference
    between a helpful assistant and one that undoes your settings.
    """
    result = await session.execute(
        select(Category)
        .where(
            Category.workspace_id == workspace_id,
            Category.is_hidden.is_(False),
        )
        .order_by(Category.name)
    )
    return list(result.scalars().all())


def _label(payee: Optional[str], description: Optional[str]) -> str:
    """Identify a transaction using both fields, never one instead of the other.

    Which field holds the merchant is provider-specific. Enable Banking puts
    it in `payee` ("Il Gelataio"), and `description` is often noise. Access
    Bank puts the *account holder's own name* in `payee` for every row and
    the merchant in `description` ("WEB PYMT WWW.AMAZON.* LUXEMBOURG LU").

    Preferring `payee` shipped on 2026-08-26 and immediately mis-filed four
    Access Bank rows — two Amazon purchases, a PayPal payment and a travel
    -allowance fee — as Food & Dining, because every one of them looked to
    the model like the same opaque personal name. Confidence did not save it:
    an earlier row with that same payee was already filed under Food &
    Dining, so the examples actively taught the wrong answer. A safeguard
    against uncertainty cannot catch a model that is consistently wrong.

    So concatenate. A redundant field costs a few tokens; a missing one costs
    a wrong category the user has to find and fix by hand.
    """
    p = (payee or "").strip()
    d = (description or "").strip()
    if p and d and p.lower() != d.lower():
        return f"{p} — {d}"
    return p or d


async def _load_examples(
    session: AsyncSession, workspace_id: uuid.UUID, limit: int = MAX_EXAMPLES
) -> list[tuple[str, uuid.UUID]]:
    """Recent (label, category_id) pairs the user already settled."""
    result = await session.execute(
        select(Transaction.description, Transaction.payee, Transaction.category_id)
        .where(
            Transaction.workspace_id == workspace_id,
            Transaction.category_id.is_not(None),
        )
        .order_by(Transaction.date.desc())
        .limit(limit * 3)
    )

    seen: set[str] = set()
    pairs: list[tuple[str, uuid.UUID]] = []
    for description, payee, category_id in result.all():
        label = _label(payee, description)
        if not label or category_id is None:
            continue
        key = label.lower()
        if key in seen:
            continue
        seen.add(key)
        pairs.append((label, category_id))
        if len(pairs) >= limit:
            break
    return pairs


def _describe(tx: Transaction) -> str:
    """One compact line per transaction. Amount sign carries the direction,
    which is often the only signal distinguishing income from a refund."""
    parts = [str(tx.date)]
    parts.append(_label(tx.payee, tx.description) or "(no description)")
    sign = "-" if (tx.type or "").lower() == "debit" else "+"
    parts.append(f"{sign}{abs(tx.amount)} {tx.currency}")
    if tx.notes:
        parts.append(f"note: {tx.notes[:80]}")
    return " | ".join(parts)


_SYSTEM_PROMPT = (
    "You categorize personal finance transactions. You are given a numbered "
    "list of categories and a numbered list of transactions.\n"
    "\n"
    "Reply with JSON only — no prose, no markdown fence — in this exact shape:\n"
    '{"assignments": [{"t": <transaction number>, "c": <category number>, '
    '"confidence": <0.0-1.0>}]}\n'
    "\n"
    "Rules:\n"
    "- Use only the category numbers listed. Never invent one.\n"
    "- The EXAMPLES show how this user already categorizes their own "
    "spending. Their conventions win over your general knowledge — if a "
    "merchant appears in the examples, reuse that category.\n"
    "- confidence is your genuine certainty. Use a low value when the "
    "description is opaque; a skipped transaction is far better than a "
    "wrongly categorized one.\n"
    "- Omit any transaction you cannot place at all.\n"
)


def _build_user_prompt(
    categories: list[Category],
    examples: list[tuple[str, uuid.UUID]],
    transactions: list[Transaction],
) -> str:
    cat_index = {cat.id: i + 1 for i, cat in enumerate(categories)}

    lines = ["CATEGORIES:"]
    for cat in categories:
        lines.append(f"{cat_index[cat.id]}. {cat.name}")

    example_lines = [
        f"- {label} -> {cat_index[cat_id]}"
        for label, cat_id in examples
        if cat_id in cat_index
    ]
    if example_lines:
        lines.append("")
        lines.append("EXAMPLES (this user's own past choices):")
        lines.extend(example_lines)

    lines.append("")
    lines.append("TRANSACTIONS TO CATEGORIZE:")
    for i, tx in enumerate(transactions, start=1):
        lines.append(f"{i}. {_describe(tx)}")

    return "\n".join(lines)


def _extract_json(content: str) -> Optional[dict]:
    """Parse the model's reply, tolerating a markdown fence around it.

    Small models wrap JSON in ```json fences despite instructions. That is
    a formatting quirk, not a refusal, so we unwrap rather than discard.
    """
    if not content:
        return None
    text = content.strip()
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        parsed = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        # Last resort: the first balanced-looking object in the reply.
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            return None
    return parsed if isinstance(parsed, dict) else None


async def _provider_and_model_for_user(session: AsyncSession, user_id: uuid.UUID):
    """Resolve (provider, model) for a background run.

    Mirrors the executor's resolution order minus the per-agent step,
    since no agent is involved here. Kept as one function so tests can
    monkey-patch a single seam.

    The sole-connection step matters more than it looks. `is_default` is
    only set when a user explicitly ticks the box, and most people who
    configure one connection never do — their agent points at it by
    `connection_id`, so nothing ever forced the flag on. Without this
    fallback the feature would report `no_provider` on exactly the setup
    it was written for, and look broken rather than unconfigured.
    """
    import os

    from sqlalchemy import func, select as _select

    from app.agents.models.connection import LlmConnection
    from app.agents.services import connection_service

    conn = await connection_service.get_default_connection(session, user_id)
    if conn is None:
        count = await session.scalar(
            _select(func.count())
            .select_from(LlmConnection)
            .where(LlmConnection.user_id == user_id)
        )
        if count == 1:
            conn = (
                await session.execute(
                    _select(LlmConnection).where(LlmConnection.user_id == user_id)
                )
            ).scalar_one_or_none()
    if conn is not None:
        provider = connection_service.build_provider_for_connection(conn)
        model = conn.default_model or os.getenv("AGENTS_DEFAULT_MODEL", "")
        return provider, model

    name = os.getenv("AGENTS_DEFAULT_PROVIDER", "")
    if not name:
        return None, ""
    from app.agents.providers.registry import build_provider

    api_key = ""
    base_url = None
    if name == "openai":
        api_key = os.getenv("AGENTS_OPENAI_API_KEY", "")
    elif name == "anthropic":
        api_key = os.getenv("AGENTS_ANTHROPIC_API_KEY", "")
    elif name == "ollama":
        base_url = os.getenv("AGENTS_OLLAMA_BASE_URL", "http://ollama:11434")
    elif name == "openai_compatible":
        api_key = os.getenv("AGENTS_OPENAI_COMPAT_API_KEY", "")
        base_url = os.getenv("AGENTS_OPENAI_COMPAT_BASE_URL")
    model = os.getenv("AGENTS_DEFAULT_MODEL", "")
    try:
        return build_provider(name, api_key=api_key, base_url=base_url, model=model), model
    except ValueError:
        return None, ""


async def auto_categorize_workspace(
    session: AsyncSession,
    workspace_id: uuid.UUID,
    user_id: uuid.UUID,
    *,
    limit: int = DEFAULT_LIMIT,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> AutoCategorizeResult:
    """Categorize what rules missed. Returns a result; never raises."""
    from app.agents.config import get_agent_settings
    from app.agents.providers.base import ChatMessage, LLMError

    if not get_agent_settings().enabled:
        return AutoCategorizeResult(
            status=_STATUS_DISABLED, detail="Agents feature is disabled"
        )

    transactions = await _load_candidates(session, workspace_id, limit)
    if not transactions:
        return AutoCategorizeResult(status=_STATUS_NO_CANDIDATES)

    categories = await _load_categories(session, workspace_id)
    if not categories:
        return AutoCategorizeResult(
            status=_STATUS_NO_CATEGORIES,
            considered=len(transactions),
            detail="Workspace has no assignable categories",
        )

    provider, model = await _provider_and_model_for_user(session, user_id)
    if provider is None or not model:
        return AutoCategorizeResult(
            status=_STATUS_NO_PROVIDER,
            considered=len(transactions),
            detail="No LLM connection configured",
        )

    examples = await _load_examples(session, workspace_id)
    prompt = _build_user_prompt(categories, examples, transactions)

    try:
        response = await provider.chat(
            [
                ChatMessage(role="system", content=_SYSTEM_PROMPT),
                ChatMessage(role="user", content=prompt),
            ],
            model=model,
            # Categorization is a lookup, not a creative task. Low
            # temperature keeps repeat runs on the same rows consistent.
            temperature=0.1,
        )
    except LLMError as exc:
        logger.warning("Auto-categorize LLM call failed: %s", exc)
        return AutoCategorizeResult(
            status=_STATUS_LLM_ERROR, considered=len(transactions), detail=str(exc)
        )
    except Exception as exc:  # noqa: BLE001 — a sync must not die for this
        logger.exception("Auto-categorize failed unexpectedly")
        return AutoCategorizeResult(
            status=_STATUS_LLM_ERROR, considered=len(transactions), detail=str(exc)
        )

    parsed = _extract_json(response.content or "")
    if parsed is None or not isinstance(parsed.get("assignments"), list):
        logger.warning(
            "Auto-categorize could not parse model reply (%d chars)",
            len(response.content or ""),
        )
        return AutoCategorizeResult(
            status=_STATUS_UNPARSEABLE,
            considered=len(transactions),
            detail="Model reply was not valid JSON",
        )

    result = AutoCategorizeResult(status=_STATUS_OK, considered=len(transactions))
    # category_id -> [transaction_id], so each category is one UPDATE.
    grouped: dict[uuid.UUID, list[uuid.UUID]] = {}
    claimed: set[uuid.UUID] = set()

    for item in parsed["assignments"]:
        if not isinstance(item, dict):
            continue
        t_idx = _as_int(item.get("t"))
        c_idx = _as_int(item.get("c"))
        confidence = _as_float(item.get("confidence"))
        if t_idx is None or c_idx is None:
            continue
        if not (1 <= t_idx <= len(transactions)) or not (1 <= c_idx <= len(categories)):
            # Hallucinated index. Dropping it is the whole point of
            # indices over UUIDs.
            continue
        tx = transactions[t_idx - 1]
        if tx.id in claimed:
            continue
        if confidence is None or confidence < min_confidence:
            result.skipped_low_confidence += 1
            continue
        category = categories[c_idx - 1]
        claimed.add(tx.id)
        grouped.setdefault(category.id, []).append(tx.id)
        result.by_category[category.name] = result.by_category.get(category.name, 0) + 1

    for category_id, tx_ids in grouped.items():
        updated = await transaction_service.bulk_update_category(
            session, workspace_id, tx_ids, category_id
        )
        result.categorized += updated

    logger.info(
        "Auto-categorize: %d/%d categorized, %d below confidence (workspace=%s)",
        result.categorized,
        result.considered,
        result.skipped_low_confidence,
        workspace_id,
    )
    return result


def _as_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_float(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None
