## Summary
<!-- 1-3 bullet points describing what this PR does -->

## Changes
- 

## Testing
- [ ] Tested gateway startup (`./run.sh`)
- [ ] Tested `/health` endpoint
- [ ] Tested routing to affected backends
- [ ] Tested fallback behavior when backend is unavailable
- [ ] Verified no secrets or credentials in committed files

## Security checklist
- [ ] No hardcoded credentials, API keys, or tokens
- [ ] SQL queries use parameterized statements (no string interpolation)
- [ ] User input validated before use (Pydantic models or explicit checks)
- [ ] No new network calls beyond localhost
- [ ] CORS policy not widened
- [ ] kwargs passed to backends are explicitly allowlisted
- [ ] No memory leaks in new async code (connections closed in `lifespan`)

## Breaking changes
<!-- List any changes to the API contract, config schema, or request/response shapes -->
None / [describe here]
