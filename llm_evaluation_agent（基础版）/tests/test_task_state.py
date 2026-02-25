import asyncio

import pytest

from app.main import TaskState
from app.models import StepStatus, StreamEvent


@pytest.mark.asyncio
async def test_task_state_publish_and_listen():
    task = TaskState(request_id="req123", product_name="demo")
    queue = task.listen()

    event = StreamEvent(
        request_id="req123",
        step="analysis",
        status=StepStatus.STARTED,
        message="开始分析",
    )

    await task.publish(event)
    assert task.events == [event]
    assert await queue.get() == event

    await task.complete()
    assert await queue.get() is None

