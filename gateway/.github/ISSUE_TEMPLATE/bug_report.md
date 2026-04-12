---
name: Bug report
about: Something isn't working correctly
title: '[Bug] '
labels: bug
assignees: kochj23
---

## Describe the bug
A clear description of what went wrong.

## To Reproduce
Steps to reproduce:
1. 
2. 
3. 

## Expected behavior
What you expected to happen.

## Actual behavior
What actually happened. Include the full error message or response.

## Environment
- macOS version:
- Python version (`python3 --version`):
- Gateway version (commit hash or date):
- Backends running: (Ollama / MLXCode / SwarmUI / ComfyUI)
- Ollama models installed (`ollama list`):

## Gateway logs
```
# Paste relevant lines from: tail -50 ~/.nova_gateway/gateway.log
```

## Request/response (if applicable)
```json
// Request
{
  "query": "...",
  "task_type": "..."
}

// Response
{
  "error": "..."
}
```

## Security assessment
- [ ] This bug has security implications (auth bypass, data exposure, injection, etc.)
  - If yes, use GitHub's private advisory reporting instead: Security → Report a vulnerability.
