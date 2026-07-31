# Product Image Studio M2B — Verified APIYI Adapter

## Verified protocol sources

M2B uses only contracts already implemented and offline-tested in this
repository. `src/providers/apiyi_gemini.py` defines the APIYI Gemini request as
`POST /v1beta/models/{model}:generateContent`, `Authorization: Bearer`, with
`contents[0].parts[].inlineData` (pure base64 and a real `mimeType`) followed
by text, and `generationConfig.responseModalities=["IMAGE"]`. Images arrive in
`candidates[].content.parts[].inlineData`. `src/providers/apiyi_openai.py`
defines APIYI's OpenAI-compatible `POST /v1/images/generations` JSON and
`POST /v1/images/edits` multipart contracts, with `model`, `prompt`, optional
`size`, `image[0..3]`, and `data[].url` or `data[].b64_json` results.
Base URLs continue to come from the existing `APIYI_GEMINI_BASE_URL` and
`APIYI_OPENAI_BASE_URL` environment settings (with their repository defaults),
and the key is read only at dispatch time from `APIYI_API_KEY`.

The verified contracts are synchronous. A returned response `id` is persisted
as `provider_request_id`; no APIYI polling URL or async task schema is present
in the repository, so M2B deliberately does not invent one. Status lookup is
an explicit `reconciliation_required` result.

## Capability and pricing

Capabilities originate from `config/models.yaml`, not from StudioService.
Configured models include Nano Banana Lite / Nano Banana 2 (Gemini inline
references), GPT-Image-2-VIP, and GPT-Image-2 (OpenAI-compatible Images).
Each model declares provider, model ID, edit/reference support, mask support,
output sizes, and a four-reference ceiling where the existing adapter supports
multi-image edits.

The existing `estimated_cost_usd` values have now been labelled
`repository-estimate-2026-07`, `pricing_status: unknown`, and their source.
They are not treated as exact prices. Live requests therefore reject with
`pricing_unknown` until a separately verified, versioned price contract is
approved. This is intentional: no `--max-cost` bypass exists.

## Reference mapping and idempotency

Only clean product, clean detail, and style roles are compiled. The order is
deterministic: product, detail, style. Each persisted attempt stores role,
asset ID, and SHA; annotations are display-only and never sent. Local paths,
annotations, and other project assets never enter a provider body.

The request hash includes PromptPackage content hash, role-labelled reference
SHAs, provider, model, output dimensions/aspect ratio, mode, and a manual
regeneration nonce. That hash is the Studio idempotency key. A duplicate active
or completed request is rejected; explicit regeneration has a new nonce and
rechecks budget.

## State machine and reconciliation

`queued -> submitting -> downloading -> succeeded` is the normal synchronous
path. The client has no retry loop. A submission timeout, missing response ID,
or restart during a live request becomes `reconcile_required`; it is never
resent. A retained `provider_request_id` is shown only as a short identifier.
Known rejected requests become `failed`. `studio reconcile-job` and
`studio reconcile-attempt` preserve this boundary and never call an invented
status endpoint.

## Result safety and accounting

Base64 has a strict encoded-size limit and strict decoding. URL results accept
HTTPS only, reject userinfo and non-global resolved IPs, disallow unlimited
redirects, cap download size, and are passed through existing candidate image
verification (real image format, one frame, dimension/pixel limits, canonical
re-encoding, SHA, project-local storage, and orphan cleanup). Raw result URLs,
base64, credentials, and local paths are not put in the ledger or UI.

Live jobs reserve the verified estimated amount before dispatch and append a
safe ledger entry at settlement or an unknown/reconciliation outcome. Until
pricing becomes exact, the reservation path remains unreachable by design.

## Live gate and first single-shot runbook

Production remains `LIVE_GENERATION_ENABLED=false`. Even when it is enabled,
APIYI needs a configured key, a configured capability, exact versioned price,
a confirmed current plan/prompt/reference set, one enabled `--shot-id`, a hard
max cost, and an explicit paid confirmation. Web and CLI share this Core gate;
there is no whole-plan live action.

After a separate explicit user authorization and a price-configuration review,
the first run must be exactly one `temu_model_full_front` output, no retries,
no regeneration, and no M3 QA. Before confirmation, display the project,
shot, provider/model, three clean reference roles, the annotation exclusion,
full PromptPackage, size, price version, estimate/hard max, and shortened
idempotency key. Record provider request ID, submission/completion time,
candidate SHA, ledger outcome, reconciliation state, and manual structural
assessment.

## Non-goals

M2B does not call APIYI, supply keys, enable Production Live, deploy, create a
queue/database, implement M3 QA/repair, infer an APIYI task-status endpoint,
or automatically retry a paid operation.
