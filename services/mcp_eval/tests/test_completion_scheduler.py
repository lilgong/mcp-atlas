import asyncio
import os
from pathlib import Path

import pandas as pd
import pytest

from mcp_completion.account_guard import FatalAccountError


# mcp_completion_script intentionally writes its operational log relative to
# the evaluator directory. Import it through that same working-directory
# contract so this test also works when pytest is launched from the repo root.
EVAL_DIR = Path(__file__).resolve().parents[1]
original_cwd = Path.cwd()
try:
    os.chdir(EVAL_DIR)
    from mcp_completion_script import AsyncMCPTrajectoryGenerator
finally:
    os.chdir(original_cwd)


def test_unexpected_task_exception_does_not_cancel_siblings(tmp_path):
    async def run():
        generator = AsyncMCPTrajectoryGenerator("test-model")
        completed = []

        async def process(row, _output, _index, _total):
            task_id = row["TASK"]
            if task_id == "bad":
                raise RuntimeError("broken task-local parser")
            await asyncio.sleep(0.01)
            completed.append(task_id)
            return {"TASK": task_id}

        generator.process_single_task = process
        frame = pd.DataFrame([{"TASK": "bad"}, {"TASK": "good-1"}, {"TASK": "good-2"}])

        result = await generator.evaluate_dataset_async(
            frame,
            str(tmp_path / "results.csv"),
            max_concurrent_requests=3,
        )

        assert sorted(completed) == ["good-1", "good-2"]
        assert sorted(result["TASK"].tolist()) == ["good-1", "good-2"]

    asyncio.run(run())


def test_fatal_account_exception_still_cancels_siblings(tmp_path):
    async def run():
        generator = AsyncMCPTrajectoryGenerator("test-model")
        sibling_cancelled = asyncio.Event()

        async def process(row, _output, _index, _total):
            if row["TASK"] == "fatal":
                await asyncio.sleep(0)
                raise FatalAccountError("out of funds")
            try:
                await asyncio.sleep(60)
            except asyncio.CancelledError:
                sibling_cancelled.set()
                raise

        generator.process_single_task = process
        frame = pd.DataFrame([{"TASK": "fatal"}, {"TASK": "sibling"}])

        with pytest.raises(FatalAccountError, match="out of funds"):
            await generator.evaluate_dataset_async(
                frame,
                str(tmp_path / "results.csv"),
                max_concurrent_requests=2,
            )

        assert sibling_cancelled.is_set()

    asyncio.run(run())
