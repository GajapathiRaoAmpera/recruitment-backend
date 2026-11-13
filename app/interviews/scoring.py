"""Scores a finished interview with the existing NVIDIA client. Output is schema-validated before
it is stored (D10, AC-8); invalid output is retried once, then the row gets {"error": code} so HR
sees the failure and can re-run it. Scores are advice for HR, never a decision.
"""

import logging
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from uuid import UUID

from pydantic import ValidationError

from app.core.errors import AppError
from app.interviews import prompts, repo
from app.interviews.schemas import AiScores, GeneratedScores
from app.papers.agents import LlmClient, LlmOutputError, NvidiaClient
from app.papers.service import open_session

log = logging.getLogger(__name__)

MIN_CANDIDATE_TURNS = 1


class ScoringFailed(Exception):
    def __init__(self, code: str):
        self.code = code


async def score_transcript(
    client: LlmClient,
    model: str,
    turns: Sequence[dict],
    overrides: Mapping[str, str] | None = None,
) -> AiScores:
    if sum(1 for t in turns if t.get("role") == "candidate") < MIN_CANDIDATE_TURNS:
        raise ScoringFailed("EMPTY_TRANSCRIPT")
    system = prompts.get_prompt(prompts.PROMPT_EVAL, overrides)
    user = "Transcript:\n<<<\n" + prompts.format_transcript(turns) + "\n>>>"
    for attempt in (1, 2):
        try:
            raw = await client.complete_json(system=system, user=user)
            generated = GeneratedScores.model_validate(raw)
            return AiScores(**generated.model_dump(), model=model, evaluated_at=datetime.now(UTC))
        except (LlmOutputError, ValidationError):
            log.info("interview scoring output invalid (attempt %d/2)", attempt)
    raise ScoringFailed("INVALID_OUTPUT")


async def run_scoring(interview_id: UUID, client: LlmClient | None = None) -> None:
    """Background task: its own session (the request's is closed by then). Never raises."""
    try:
        async with open_session() as session:
            turns = await repo.transcript(session, interview_id)
            overrides = await repo.prompt_overrides(session)
            await session.commit()  # don't hold a transaction open across the model call
            try:
                llm = client or NvidiaClient(temperature=0.0)
                model = getattr(llm, "model", "unknown")
                scores = (await score_transcript(llm, model, turns, overrides)).model_dump(
                    mode="json"
                )
            except ScoringFailed as e:
                scores = {"error": e.code}
            except AppError as e:  # not configured, rate limited, provider down
                scores = {"error": e.code}
            await repo.save_scores(session, interview_id, scores)
            await session.commit()
    except Exception:
        log.exception("interview scoring crashed")  # the row stays pending; HR can re-run
