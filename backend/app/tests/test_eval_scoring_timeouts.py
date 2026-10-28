import asyncio
from types import SimpleNamespace

import pytest

from app.core.config import settings
from app.services.evals.ragas_runner import RagasCollectionsScorer


class FakeMetric:
    def __init__(self, delay, fail=False):
        self.delay = delay
        self.fail = fail
        self.active = 0
        self.peak = 0
        self.completed = 0

    async def ascore(self, **kwargs):
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(self.delay)
            if self.fail and kwargs.get("response", "").startswith("B"):
                raise ValueError("invalid judge output")
            self.completed += 1
            return SimpleNamespace(value=0.75)
        finally:
            self.active -= 1


def scorer_with_metrics(**metrics):
    # Bypass SDK/client initialization: these tests make no external requests.
    scorer = RagasCollectionsScorer.__new__(RagasCollectionsScorer)
    scorer.metrics = metrics
    scorer._messages = lambda *args: []
    return scorer


async def score(scorer, response, metric_names=None):
    return await scorer.score(
        user_input="question",
        response=response,
        retrieved_contexts=["evidence"],
        reference="reference",
        tool_calls=[],
        tool_observations=[],
        expected_tool_calls=[],
        metric_names=metric_names or {"faithfulness"},
    )


@pytest.mark.asyncio
async def test_queued_faithfulness_chunks_have_time_to_finish(monkeypatch):
    monkeypatch.setattr(settings, "ragas_timeout_seconds", 0.3)
    metric = FakeMetric(0.2)
    scorer = scorer_with_metrics(faithfulness=metric)

    # Three chunks require two batches: each fits 0.3s, together they do not.
    scores, errors = await score(scorer, "A" * 700 + "B" * 700 + "C" * 700)

    assert scores == {"faithfulness": 0.75}
    assert errors == {}
    assert metric.completed == 3
    assert metric.peak == 2
    assert metric.active == 0


@pytest.mark.asyncio
async def test_stalled_chunks_remain_bounded_without_losing_other_scores(monkeypatch):
    monkeypatch.setattr(settings, "ragas_timeout_seconds", 0.03)
    stalled = FakeMetric(60)
    scorer = scorer_with_metrics(faithfulness=stalled, context_precision=FakeMetric(0))

    scores, errors = await asyncio.wait_for(
        score(scorer, "A" * 2100, {"faithfulness", "context_precision"}), timeout=1
    )

    assert scores == {"context_precision": 0.75}
    assert "TimeoutError" in errors["faithfulness"]
    assert "0.03s" in errors["faithfulness"]
    assert stalled.active == 0


@pytest.mark.asyncio
async def test_failed_chunk_does_not_become_partial_success(monkeypatch):
    monkeypatch.setattr(settings, "ragas_timeout_seconds", 1)
    scorer = scorer_with_metrics(faithfulness=FakeMetric(0, fail=True))

    scores, errors = await score(scorer, "A" * 700 + "B" * 700)

    assert "faithfulness" not in scores
    assert "1/2" in errors["faithfulness"]
    assert "invalid judge output" in errors["faithfulness"]


@pytest.mark.asyncio
async def test_other_metrics_keep_their_original_timeout(monkeypatch):
    monkeypatch.setattr(settings, "ragas_timeout_seconds", 0.03)
    metric = FakeMetric(60)
    scorer = scorer_with_metrics(context_precision=metric)

    scores, errors = await asyncio.wait_for(
        score(scorer, "answer", {"context_precision"}), timeout=1
    )

    assert scores == {}
    assert errors == {"context_precision": "TimeoutError: context_precision scoring exceeded 0.03s"}
    assert metric.active == 0


@pytest.mark.asyncio
async def test_cancel_scoring_stops_active_and_queued_chunks(monkeypatch):
    monkeypatch.setattr(settings, "ragas_timeout_seconds", 10)
    metric = FakeMetric(60)
    scorer = scorer_with_metrics(faithfulness=metric)
    task = asyncio.create_task(score(scorer, "A" * 2100))
    try:
        async with asyncio.timeout(1):
            while metric.active < 2:
                await asyncio.sleep(0)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    assert metric.active == 0
    assert metric.completed == 0
