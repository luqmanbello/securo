"""Tests for LLM-assisted categorization of what rules missed.

No network: every test injects a scripted provider through the single
`_provider_and_model_for_user` seam, the same pattern the agents executor
tests use.
"""
import json
import re
import uuid
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.agents.providers.base import ChatResponse, LLMError, Usage
from app.models.category import Category
from app.models.transaction import Transaction
from app.models.user import User
from app.services import auto_categorize_service
from app.services.auto_categorize_service import auto_categorize_workspace


class ScriptedProvider:
    """Returns a canned reply and records the prompt it was handed."""

    name = "scripted"

    def __init__(self, content: str, *, raises: Exception | None = None):
        self._content = content
        self._raises = raises
        self.calls: list[dict] = []

    async def chat(self, messages, *, model, tools=None, temperature=0.4, max_tokens=None):
        self.calls.append(
            {
                "messages": messages,
                "model": model,
                "temperature": temperature,
            }
        )
        if self._raises is not None:
            raise self._raises
        return ChatResponse(content=self._content, usage=Usage())

    @property
    def last_prompt(self) -> str:
        return self.calls[-1]["messages"][-1].content


@pytest.fixture(autouse=True)
def _agents_on(monkeypatch):
    """Agents default to off; every test here needs them on except the
    one that explicitly checks the off path."""
    from app.agents.config import get_agent_settings

    get_agent_settings.cache_clear()
    monkeypatch.setenv("AGENTS_ENABLED", "true")
    yield
    get_agent_settings.cache_clear()


def _install_provider(monkeypatch, provider, model="test-model"):
    async def _fake(session, user_id):
        return provider, model

    monkeypatch.setattr(
        auto_categorize_service, "_provider_and_model_for_user", _fake
    )
    return provider


def _reply(assignments) -> str:
    return json.dumps({"assignments": assignments})


def _prompt_indices(prompt: str) -> tuple[dict[str, int], dict[str, int]]:
    """Recover the numbering the service handed the model.

    The service sorts categories by name and transactions by date, so
    hard-coding "category 1" in a test asserts the sort order rather than
    the behaviour under test. Read the numbering back out instead.
    """
    categories: dict[str, int] = {}
    transactions: dict[str, int] = {}
    section = None
    for line in prompt.splitlines():
        if line.startswith("CATEGORIES:"):
            section = "cat"
            continue
        if line.startswith("TRANSACTIONS TO CATEGORIZE:"):
            section = "tx"
            continue
        if line.startswith("EXAMPLES"):
            section = None
            continue
        match = re.match(r"^(\d+)\.\s+(.*)$", line)
        if not match or section is None:
            continue
        idx, body = int(match.group(1)), match.group(2)
        if section == "cat":
            categories[body.strip()] = idx
        else:
            transactions[body] = idx
    return categories, transactions


def _cat_at(prompt: str, categories, index: int):
    """The category object the service assigned this number."""
    cats, _ = _prompt_indices(prompt)
    return next(c for c in categories if cats.get(c.name) == index)


def _tx_index(transactions: dict[str, int], needle: str) -> int:
    for label, idx in transactions.items():
        if needle in label:
            return idx
    raise AssertionError(f"{needle!r} not found in prompt transactions: {transactions}")


async def _add_tx(
    session,
    user: User,
    account,
    *,
    description: str,
    amount: str = "10.00",
    category_id=None,
    source: str = "manual",
    transfer_pair_id=None,
    tx_type: str = "debit",
    payee: str | None = None,
) -> Transaction:
    tx = Transaction(
        id=uuid.uuid4(),
        user_id=user.id,
        account_id=account.id,
        category_id=category_id,
        description=description,
        payee=payee,
        amount=Decimal(amount),
        date=date.today(),
        type=tx_type,
        source=source,
        transfer_pair_id=transfer_pair_id,
        created_at=datetime.now(timezone.utc),
    )
    session.add(tx)
    await session.commit()
    await session.refresh(tx)
    return tx


async def _category_of(session, tx_id) -> uuid.UUID | None:
    return await session.scalar(
        select(Transaction.category_id).where(Transaction.id == tx_id)
    )


# --- The off switches ------------------------------------------------------


@pytest.mark.asyncio
async def test_returns_disabled_when_agents_are_off(
    session, test_user, test_workspace, monkeypatch
):
    from app.agents.config import get_agent_settings

    get_agent_settings.cache_clear()
    monkeypatch.setenv("AGENTS_ENABLED", "false")

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.status == "disabled"
    assert result.categorized == 0


@pytest.mark.asyncio
async def test_no_candidates_short_circuits_before_calling_the_model(
    session, test_user, test_workspace, test_categories, monkeypatch
):
    provider = _install_provider(monkeypatch, ScriptedProvider(_reply([])))

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.status == "no_candidates"
    assert provider.calls == [], "must not spend a token when there is nothing to do"


@pytest.mark.asyncio
async def test_missing_connection_reports_no_provider(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    await _add_tx(session, test_user, test_account, description="MYSTERY SHOP")

    async def _none(session_, user_id):
        return None, ""

    monkeypatch.setattr(auto_categorize_service, "_provider_and_model_for_user", _none)

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.status == "no_provider"
    assert result.considered == 1
    assert result.categorized == 0


# --- The candidate set -----------------------------------------------------


@pytest.mark.asyncio
async def test_opening_balances_settlements_and_transfers_are_never_candidates(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    """The badge on the dashboard excludes these three; so must we, or the
    service would categorize rows the user was never asked about."""
    await _add_tx(
        session, test_user, test_account, description="Saldo inicial",
        source="opening_balance",
    )
    await _add_tx(
        session, test_user, test_account, description="Group payback",
        source="settlement",
    )
    await _add_tx(
        session, test_user, test_account, description="Moved to savings",
        transfer_pair_id=uuid.uuid4(),
    )
    real = await _add_tx(session, test_user, test_account, description="REWE SAGT DANKE")

    provider = _install_provider(
        monkeypatch, ScriptedProvider(_reply([{"t": 1, "c": 1, "confidence": 0.95}]))
    )

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.considered == 1, "only the real transaction is a candidate"
    assert "REWE SAGT DANKE" in provider.last_prompt
    assert "Saldo inicial" not in provider.last_prompt
    assert "Group payback" not in provider.last_prompt
    assert "Moved to savings" not in provider.last_prompt
    expected = _cat_at(provider.last_prompt, test_categories, 1)
    assert await _category_of(session, real.id) == expected.id


@pytest.mark.asyncio
async def test_hidden_categories_are_not_offered_to_the_model(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    """Hiding a category is how the user says 'stop putting things here'."""
    hidden = Category(
        id=uuid.uuid4(),
        user_id=test_user.id,
        workspace_id=test_workspace.id,
        name="Retired Category",
        is_hidden=True,
    )
    session.add(hidden)
    await session.commit()

    await _add_tx(session, test_user, test_account, description="SOMETHING")
    provider = _install_provider(monkeypatch, ScriptedProvider(_reply([])))

    await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert "Retired Category" not in provider.last_prompt


@pytest.mark.asyncio
async def test_limit_caps_the_batch(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    for i in range(5):
        await _add_tx(session, test_user, test_account, description=f"TX {i}")
    _install_provider(monkeypatch, ScriptedProvider(_reply([])))

    result = await auto_categorize_workspace(
        session, test_workspace.id, test_user.id, limit=2
    )

    assert result.considered == 2


# --- Applying the answer ---------------------------------------------------


@pytest.mark.asyncio
async def test_confident_assignments_are_written(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    a = await _add_tx(session, test_user, test_account, description="ALDI SUED")
    b = await _add_tx(session, test_user, test_account, description="BVG TICKET")

    # A dry run first, purely to read back the numbering the service uses.
    provider = _install_provider(monkeypatch, ScriptedProvider(_reply([])))
    await auto_categorize_workspace(session, test_workspace.id, test_user.id)
    cats, txs = _prompt_indices(provider.last_prompt)

    _install_provider(
        monkeypatch,
        ScriptedProvider(
            _reply(
                [
                    {
                        "t": _tx_index(txs, "ALDI SUED"),
                        "c": cats[test_categories[0].name],
                        "confidence": 0.95,
                    },
                    {
                        "t": _tx_index(txs, "BVG TICKET"),
                        "c": cats[test_categories[1].name],
                        "confidence": 0.88,
                    },
                ]
            )
        ),
    )

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.status == "ok"
    assert result.categorized == 2
    assert result.skipped_low_confidence == 0
    assert await _category_of(session, a.id) == test_categories[0].id
    assert await _category_of(session, b.id) == test_categories[1].id


@pytest.mark.asyncio
async def test_low_confidence_is_left_for_the_human(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    tx = await _add_tx(session, test_user, test_account, description="PAYPAL *XYZ")
    _install_provider(
        monkeypatch, ScriptedProvider(_reply([{"t": 1, "c": 1, "confidence": 0.4}]))
    )

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.categorized == 0
    assert result.skipped_low_confidence == 1
    assert await _category_of(session, tx.id) is None


@pytest.mark.asyncio
async def test_missing_confidence_is_treated_as_not_confident(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    tx = await _add_tx(session, test_user, test_account, description="UNKNOWN")
    _install_provider(monkeypatch, ScriptedProvider(_reply([{"t": 1, "c": 1}])))

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.categorized == 0
    assert result.skipped_low_confidence == 1
    assert await _category_of(session, tx.id) is None


@pytest.mark.asyncio
async def test_min_confidence_is_configurable(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    tx = await _add_tx(session, test_user, test_account, description="EDGE CASE")
    provider = _install_provider(
        monkeypatch, ScriptedProvider(_reply([{"t": 1, "c": 1, "confidence": 0.55}]))
    )

    result = await auto_categorize_workspace(
        session, test_workspace.id, test_user.id, min_confidence=0.5
    )

    assert result.categorized == 1
    expected = _cat_at(provider.last_prompt, test_categories, 1)
    assert await _category_of(session, tx.id) == expected.id


# --- Hostile model output --------------------------------------------------


@pytest.mark.asyncio
async def test_out_of_range_indices_are_dropped(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    """An index the model invented cannot name a real row — that is the
    reason indices are used instead of UUIDs."""
    tx = await _add_tx(session, test_user, test_account, description="ONLY ONE")
    _install_provider(
        monkeypatch,
        ScriptedProvider(
            _reply(
                [
                    {"t": 99, "c": 1, "confidence": 0.99},
                    {"t": 1, "c": 99, "confidence": 0.99},
                    {"t": 0, "c": 1, "confidence": 0.99},
                    {"t": -1, "c": 1, "confidence": 0.99},
                ]
            )
        ),
    )

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.status == "ok"
    assert result.categorized == 0
    assert await _category_of(session, tx.id) is None


@pytest.mark.asyncio
async def test_duplicate_assignments_for_one_transaction_take_the_first(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    tx = await _add_tx(session, test_user, test_account, description="TWICE NAMED")
    provider = _install_provider(
        monkeypatch,
        ScriptedProvider(
            _reply(
                [
                    {"t": 1, "c": 1, "confidence": 0.9},
                    {"t": 1, "c": 2, "confidence": 0.9},
                ]
            )
        ),
    )

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.categorized == 1
    expected = _cat_at(provider.last_prompt, test_categories, 1)
    assert await _category_of(session, tx.id) == expected.id


@pytest.mark.asyncio
async def test_json_wrapped_in_a_markdown_fence_still_parses(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    tx = await _add_tx(session, test_user, test_account, description="FENCED")
    body = _reply([{"t": 1, "c": 1, "confidence": 0.9}])
    provider = _install_provider(
        monkeypatch, ScriptedProvider(f"Sure!\n```json\n{body}\n```\n")
    )

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.categorized == 1
    expected = _cat_at(provider.last_prompt, test_categories, 1)
    assert await _category_of(session, tx.id) == expected.id


@pytest.mark.asyncio
async def test_unparseable_reply_changes_nothing(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    tx = await _add_tx(session, test_user, test_account, description="GARBAGE IN")
    _install_provider(monkeypatch, ScriptedProvider("I'm afraid I can't do that."))

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.status == "unparseable"
    assert result.categorized == 0
    assert await _category_of(session, tx.id) is None


@pytest.mark.asyncio
async def test_provider_failure_is_swallowed(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    """This runs after every bank sync. A dead LLM must not fail the sync."""
    tx = await _add_tx(session, test_user, test_account, description="NO ANSWER")
    _install_provider(
        monkeypatch, ScriptedProvider("", raises=LLMError("upstream is down"))
    )

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.status == "llm_error"
    assert result.categorized == 0
    assert await _category_of(session, tx.id) is None


@pytest.mark.asyncio
async def test_unexpected_exception_is_also_swallowed(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    await _add_tx(session, test_user, test_account, description="BOOM")
    _install_provider(
        monkeypatch, ScriptedProvider("", raises=RuntimeError("socket exploded"))
    )

    result = await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert result.status == "llm_error"
    assert result.categorized == 0


# --- The prompt ------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_teaches_the_model_this_users_own_conventions(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    """The examples are where accuracy on custom categories comes from."""
    await _add_tx(
        session, test_user, test_account,
        description="MONTHLY UPKEEP", payee="MORYAM",
        category_id=test_categories[2].id,
    )
    await _add_tx(session, test_user, test_account, description="NEW ONE")
    provider = _install_provider(monkeypatch, ScriptedProvider(_reply([])))

    await auto_categorize_workspace(session, test_workspace.id, test_user.id)
    prompt = provider.last_prompt

    assert "EXAMPLES" in prompt
    assert "MORYAM" in prompt, "payee is preferred over description as the label"
    assert "CATEGORIES:" in prompt
    assert "TRANSACTIONS TO CATEGORIZE:" in prompt
    assert "NEW ONE" in prompt


@pytest.mark.asyncio
async def test_prompt_marks_direction_and_amount(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    """A credit and a debit for the same merchant mean different things."""
    await _add_tx(
        session, test_user, test_account, description="REFUND",
        amount="42.00", tx_type="credit",
    )
    provider = _install_provider(monkeypatch, ScriptedProvider(_reply([])))

    await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert "+42.00" in provider.last_prompt


@pytest.mark.asyncio
async def test_temperature_is_low_so_repeat_runs_agree(
    session, test_user, test_workspace, test_account, test_categories, monkeypatch
):
    await _add_tx(session, test_user, test_account, description="ANY")
    provider = _install_provider(monkeypatch, ScriptedProvider(_reply([])))

    await auto_categorize_workspace(session, test_workspace.id, test_user.id)

    assert provider.calls[-1]["temperature"] <= 0.2
