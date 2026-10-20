# Public BMS Agent deployment brief

## Default free model update (2026-10-20)

- Confirmed: user selected A, Qwen3-1.7B, as the default free model. No shared API key, no paid API calls or automatic paid fallback. Paid OpenAI remains explicit per-request BYOK.
- Implementation: browser-only WebLLM inference, pinned Qwen3-1.7B quantized model; user acknowledges model download before initialization. GPU resources and network transfer are not free compute supplied by this site.
- Keep existing SDK orchestration and trusted numeric evidence. Browser model selects evidence from a bounded set, application copies exact facts and validates citations. Initialization tool reads are program-driven and disclosed, not attributed to model reasoning. Full deterministic scans and model coverage stay distinct.
- Free mode processes CSV locally and never posts to the hosted model API. Only public model artifacts are downloaded/cached; log data, questions and keys are not persisted. Paid and hosted rules modes retain their existing privacy disclosures.
- Unsupported GPU, download errors, invalid model output, timeout and cancellation must be visible; no claim that rule fallback used AI. GitHub Pages placeholder and original Python project remain unchanged.
- Verify the real SDK path with a scripted local engine, no-network guards, worker/device checks, build and public deployment. Completed: 93 Node tests, production build and native public deployment; real Qwen3-1.7B inference on the synthetic demo (31 samples, 3 protection events, 3/3 cited evidence, 426 local input / 21 output tokens). This is a functional smoke test, not a model-quality benchmark.

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
- Official Agents SDK remains the agent runtime. Paid models were not called; deterministic/SDK tests use a scripted model and no network. Free Qwen3-1.7B also passed the limited real-browser demo above; broader quality/latency remains unmeasured.

## Open verification

- Baseline checks, public build/deployment, browser cancellation and local evidence-window smoke tests completed. Additional devices, production logs and model-quality benchmarking remain unverified.

