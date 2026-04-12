"""
consensus.py — Cross-model validation and consensus scoring.

Runs a query through multiple backends, compares responses,
and produces a consensus score. Used for critical outputs where
accuracy matters more than speed.

Scoring approach:
  - Cosine similarity on word-frequency vectors (no ML deps required)
  - Score ≥ threshold → consensus reached → return longest response
  - Score < threshold → flag discrepancy, return best-scoring response

Author: Jordan Koch
"""

import asyncio
import logging
import math
from collections import Counter
from typing import Optional, Any
from .. import config

logger = logging.getLogger(__name__)


def _word_vector(text: str) -> Counter:
    words = text.lower().split()
    return Counter(words)


def _cosine_similarity(a: Counter, b: Counter) -> float:
    if not a or not b:
        return 0.0
    dot = sum(a[k] * b[k] for k in a if k in b)
    mag_a = math.sqrt(sum(v * v for v in a.values()))
    mag_b = math.sqrt(sum(v * v for v in b.values()))
    if mag_a == 0 or mag_b == 0:
        return 0.0
    return dot / (mag_a * mag_b)


def _pairwise_score(responses: list[str]) -> float:
    """Average pairwise cosine similarity across all response pairs."""
    if len(responses) < 2:
        return 1.0
    vectors = [_word_vector(r) for r in responses]
    scores = []
    for i in range(len(vectors)):
        for j in range(i + 1, len(vectors)):
            scores.append(_cosine_similarity(vectors[i], vectors[j]))
    return sum(scores) / len(scores) if scores else 0.0


class ConsensusValidator:
    def __init__(self, backends: dict):
        # backends: {name: BackendInstance}
        self._backends = backends

    async def validate(
        self,
        prompt: str,
        primary_response: str,
        primary_backend: str,
        n_validators: int = 2,
        model_override: Optional[str] = None,
        **kwargs
    ) -> dict[str, Any]:
        """
        Run prompt through additional backends and compare.
        Returns consensus result with score and recommended response.
        """
        threshold = config.consensus_threshold()
        timeout = config.get().get("validation", {}).get("timeout_seconds", 30)

        # Pick validator backends (not the primary)
        candidates = [name for name in self._backends if name != primary_backend
                      and name not in ("swarmui", "comfyui")]
        validators = candidates[:n_validators - 1]  # primary already ran, add N-1 more

        responses = [primary_response]
        backends_used = [primary_backend]

        tasks = []
        for name in validators:
            backend = self._backends[name]
            tasks.append(self._run_backend(backend, prompt, model_override, kwargs, timeout))

        if tasks:
            results = await asyncio.gather(*tasks, return_exceptions=True)
            for name, result in zip(validators, results):
                if isinstance(result, Exception):
                    logger.warning(f"Validator {name} failed: {result}")
                    continue
                responses.append(result.get("response", ""))
                backends_used.append(name)

        score = _pairwise_score(responses)
        consensus = score >= threshold
        # Pick the longest response as "recommended" — more detail is usually better
        recommended = max(responses, key=len) if responses else primary_response

        logger.info(
            f"Consensus: score={score:.2f} threshold={threshold} "
            f"backends={backends_used} consensus={'YES' if consensus else 'NO'}"
        )

        if not consensus:
            logger.warning(
                f"Consensus NOT reached (score={score:.2f}). "
                f"Discrepancy between: {backends_used}"
            )

        return {
            "consensus": consensus,
            "score": round(score, 3),
            "responses": responses,
            "backends_used": backends_used,
            "recommended": recommended,
        }

    async def _run_backend(self, backend, prompt: str, model: Optional[str], kwargs: dict, timeout: float) -> dict:
        return await asyncio.wait_for(
            backend.query(prompt, model=model, **kwargs),
            timeout=timeout
        )
