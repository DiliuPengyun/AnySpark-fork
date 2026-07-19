"""Tests for ``_wait_question_answer`` — the indefinite question wait.

Regression context: the old ``_await_answer`` sliced the wait into 10s
``wait_for`` chunks, and each chunk timeout cancelled the shared
pending-answer future — so a user answering after the first chunk got a
404 and the loop treated the question as rejected (effectively a 10s
timeout, not the documented 300s). The new helper waits indefinitely,
never cancels the pending future while slicing, and emits keepalive
progress events so the SSE connection stays alive.
"""

import asyncio

import pytest

from core.agent_loop import _wait_question_answer
from core.question import manager as question_manager
from core.session_state import CancelledError, RunHandle


async def _collect(question_id, handle=None, keepalive=0.05):
    keepalives = []
    answers = None
    async for ev in _wait_question_answer(question_id, handle=handle, keepalive=keepalive):
        if ev.type == "question_answered":
            answers = ev.data["answers"]
        else:
            keepalives.append(ev)
    return keepalives, answers


async def test_answer_immediate_no_keepalive():
    q = question_manager.create_question([{"question": "?", "options": []}])
    assert question_manager.reply(q.id, [["确认"]])
    keepalives, answers = await _collect(q.id)
    assert answers == [["确认"]]
    assert keepalives == []  # answered before the first keepalive tick


async def test_late_answer_still_accepted():
    """An answer arriving after several keepalive slices must still resolve —
    the pending future must not be cancelled by the slicing (old bug)."""
    q = question_manager.create_question([{"question": "?", "options": []}])

    async def reply_later():
        await asyncio.sleep(0.2)  # several 0.05s keepalive ticks
        assert question_manager.reply(q.id, [["按此方案执行"]])

    task = asyncio.create_task(reply_later())
    keepalives, answers = await _collect(q.id, keepalive=0.05)
    await task
    assert answers == [["按此方案执行"]]
    assert any(e.type == "progress" for e in keepalives)  # keepalives emitted


async def test_reject_propagates():
    q = question_manager.create_question([{"question": "?", "options": []}])

    async def reject_later():
        await asyncio.sleep(0.05)
        question_manager.reject(q.id)

    task = asyncio.create_task(reject_later())
    with pytest.raises(Exception, match="用户取消了提问"):
        async for _ev in _wait_question_answer(q.id, keepalive=0.05):
            pass
    await task


async def test_run_cancel_aborts_wait():
    q = question_manager.create_question([{"question": "?", "options": []}])
    handle = RunHandle(session_id="s_cancel")
    handle.cancel()
    with pytest.raises(CancelledError):
        async for _ev in _wait_question_answer(q.id, handle=handle, keepalive=0.05):
            pass
    # Let the cancelled waiter task run its cleanup before checking.
    await asyncio.sleep(0.05)
    # The question is cleaned up — a late reply finds nothing to resolve.
    assert question_manager.reply(q.id, [["确认"]]) is False
