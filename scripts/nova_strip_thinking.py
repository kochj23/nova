"""
nova_strip_thinking.py — Utility to strip qwen3 reasoning leakage from responses.

Import and call strip_thinking(response) anywhere Nova generates text.
Written by Jordan Koch.
"""

import re

_REASONING_STARTERS = re.compile(
    r'^(okay[,.]?|ok[,.]?|so[,.]|sure[,.]?|let me|i need to|first[,.]|'
    r'alright[,.]?|well[,.]|looking at|the user|nova is|this email|'
    r'i should|to reply|the email|checking|let\'s see|here\'s|based on|'
    r'now[,.]|to write|i\'ll|i will)',
    re.IGNORECASE
)


def strip_thinking(response: str) -> str:
    """
    Remove qwen3 chain-of-thought leakage from a model response.
    Handles both <think>...</think> blocks and plain reasoning paragraphs.
    """
    if not response:
        return response

    response = response.strip()

    # Strip explicit <think> block
    if "</think>" in response:
        response = response.split("</think>", 1)[-1].strip()

    # Strip leading reasoning paragraph (model narrates before getting to content)
    lines = response.split("\n")
    if lines and _REASONING_STARTERS.match(lines[0].strip()):
        # Find the first blank line and take everything after it
        for i, line in enumerate(lines):
            if line.strip() == "" and i > 0:
                candidate = "\n".join(lines[i + 1:]).strip()
                if len(candidate) > 20:
                    response = candidate
                    break

    return response.strip()
