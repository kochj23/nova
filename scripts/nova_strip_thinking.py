"""
nova_strip_thinking.py — Utility to strip qwen3 reasoning leakage from responses.

Import and call strip_thinking(response) anywhere Nova generates text.
Written by Jordan Koch.
"""

import re

_REASONING_STARTERS = re.compile(
    r'^(okay[,. ]|ok[,. ]|so[, ]|sure[,. ]?|let me|i need to|first[,. ]|'
    r'alright[,. ]?|well[,. ]|looking at|the user|nova is|this email|'
    r'i should|to reply|the email|checking|let\'s see|here\'s|based on|'
    r'now[,. ]|to write|i\'ll |i will |hmm|wait[,. ]|actually[,. ]|'
    r'drafting|draft|so,|two,|three,|also[,. ]|need to|the context|'
    r'nova\'s|check the|re-read|re read|the prior|the previous)',
    re.IGNORECASE
)

# Phrases that appear mid-response as reasoning — strip from here to next blank line
_REASONING_MIDTEXT = re.compile(
    r'\n(hmm[,. ]|wait[,. ]|actually[,. ]|so[,. ] (the|i|we)|'
    r'need to check|let me (re|re-)|also[,. ] check|the word (limit|count))',
    re.IGNORECASE
)


def strip_thinking(response: str) -> str:
    """
    Remove qwen3 chain-of-thought leakage from a model response.
    Handles <think>...</think> blocks and leading/embedded reasoning paragraphs.
    """
    if not response:
        return response

    response = response.strip()

    # Strip explicit <think> block
    if "</think>" in response:
        response = response.split("</think>", 1)[-1].strip()

    # Iteratively strip leading reasoning paragraphs
    # (model sometimes has multiple reasoning blocks before the real content)
    for _ in range(5):
        lines = response.split("\n")
        if not lines:
            break
        if _REASONING_STARTERS.match(lines[0].strip()):
            # Find the first blank line and take everything after it
            stripped = False
            for i, line in enumerate(lines):
                if line.strip() == "" and i > 0:
                    candidate = "\n".join(lines[i + 1:]).strip()
                    if len(candidate) > 20:
                        response = candidate
                        stripped = True
                        break
            if not stripped:
                break
        else:
            break

    return response.strip()
