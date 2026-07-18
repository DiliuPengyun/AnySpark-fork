"""Tests for SessionStateMachine — ghost-state reclaim, release ownership,
and the queued-input drain path.

Regression context:
- A cancelled / client-disconnected run could leave the session stuck in
  RUNNING (the loop's ``finally`` never released it), so the next user
  message was wrongly queued ("消息已排队").
- ``release()`` used to set IDLE even when the handle didn't match,
  letting a stale generator clobber a newer run's state.
"""

import pytest

from core.session_state import SessionState, SessionStateMachine


@pytest.fixture
def sm():
    return SessionStateMachine()


async def test_start_or_queue_starts_when_idle(sm):
    handle = await sm.start_or_queue("s1", "hello")
    assert handle is not None
    assert sm.get_state("s1") == SessionState.RUNNING


async def test_start_or_queue_queues_when_running(sm):
    handle = await sm.start_or_queue("s1", "first")
    assert handle is not None
    queued = await sm.start_or_queue("s1", "second")
    assert queued is None  # message was queued
    assert sm.has_queued_input("s1")
    items = await sm.drain_queued("s1")
    assert [i.text for i in items] == ["second"]


async def test_start_or_queue_reclaims_cancelled_handle(sm):
    """A cancelled-but-not-released session must not swallow new messages."""
    handle = await sm.start_or_queue("s1", "first")
    assert handle is not None
    await sm.cancel("s1")  # user clicked stop; loop may never release

    new_handle = await sm.start_or_queue("s1", "second")
    assert new_handle is not None, "ghost RUNNING state should be reclaimed"
    assert new_handle is not handle
    assert sm.get_state("s1") == SessionState.RUNNING


async def test_start_or_queue_reclaims_missing_handle(sm):
    """State non-IDLE with no handle is a ghost — reclaim it."""
    await sm.start_or_queue("s1", "first")
    # Simulate an abandoned generator: state stuck, handle lost.
    sm._handles.pop("s1", None)

    new_handle = await sm.start_or_queue("s1", "second")
    assert new_handle is not None
    assert sm.get_state("s1") == SessionState.RUNNING


async def test_release_owner_handle_returns_to_idle(sm):
    handle = await sm.start_or_queue("s1", "first")
    await sm.release("s1", handle)
    assert sm.get_state("s1") == SessionState.IDLE
    assert sm.get_handle("s1") is None


async def test_stale_release_does_not_clobber_new_run(sm):
    """A late release from an abandoned generator must not reset a newer run."""
    old_handle = await sm.start_or_queue("s1", "first")
    await sm.cancel("s1")
    # Ghost reclaim: user sends a new message, new run starts.
    new_handle = await sm.start_or_queue("s1", "second")
    assert new_handle is not None

    # The old generator's finally fires late — must not touch the new run.
    await sm.release("s1", old_handle)
    assert sm.get_state("s1") == SessionState.RUNNING
    assert sm.get_handle("s1") is new_handle


async def test_queued_message_survives_reclaim_for_drain(sm):
    """Messages queued while a loop runs remain available for the loop to drain."""
    await sm.start_or_queue("s1", "first")
    assert await sm.start_or_queue("s1", " steering note ") is None
    items = await sm.drain_queued("s1")
    assert len(items) == 1
    assert items[0].text.strip() == "steering note"
    # Drained queue is empty afterwards.
    assert await sm.drain_queued("s1") == []
