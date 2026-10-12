# Public BMS Agent deployment brief

## Confirmed

- User explicitly requests a non-local public URL, superseding the previous local-only publishing boundary.
- Preserve the completed Python v0.2 local app unchanged in E:/GPT/AI/bms-log-agent.
- Keep BYOK: each visitor explicitly supplies their own key for Agent mode; no author/shared environment key, no real API tests or new keys.
- Publish only application source and hand-authored synthetic fixtures, not local logs/reports/uploads/secrets.
- Sites project ID is persisted in .openai/hosting.json; public access is available to the selected owner account.

## Implementation assumptions and limits

- Sites hosting runs Workers, not the original Python HTTP server. A separate JavaScript cloud adaptation preserves deterministic tools and exact evidence semantics.
- Anonymous rule analysis is available; Agent requires that request's explicit key/model. Both CSV and optional key reach the HTTPS hosted backend; uploaded data is not persisted by this application.
- Public demo resource bounds: 512KiB CSV, 3,000 samples, 16 channels, 20 rules, 5,000 returned events. The local version retains its larger bounds.
- No cross-request analysis cache, saved reports or remote checkpoint store. CLI checkpoint restoration remains available in the original local app.
- Same-origin API, bounded request size/work, transient per-isolate throttling, fixed safe errors and tracing disabled. This is not a promise of enterprise authentication or distributed rate limiting.
- Official Agents SDK remains the agent runtime. Real model quality/latency/cost unmeasured; tests use a scripted model and no network.

## Open verification

- Cloud build, independent fixture parity, request safety tests, local browser QA and native public deployment completion.
