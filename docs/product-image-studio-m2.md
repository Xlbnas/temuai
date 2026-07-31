# Product Image Studio M2A — Shot Planning and Generation Foundation

## Audited architecture and persistence

The deployed baseline is `origin/master` at `d7201a0`.  Studio is an isolated
JSON aggregate protected by a POSIX project lock and atomic replace writes.
Its real path is `DATA_DIR/studio/<project-id>/project.json`; the Compose
mapping makes this `/app/data/studio/<project-id>/project.json` in the
container and `./data/app/studio/<project-id>/project.json` on the host.
Studio originals and generated candidates live below that same project folder.
This is intentionally separate from legacy Pipeline manifests and its
in-memory `src/jobs` abstraction.

Compose's persisted runtime root is specifically `./data/app`, not an
ambiguous generic `DATA_DIR`.  Production builds retain the existing
host-network proxy override requirement; M2 neither changes Docker networking
nor deploys an image.

## M2 data and state

`StudioRecord` schema 2 adds strict Pydantic `ShotPlan`, `ShotSpec`,
`PromptPackage`, `ProviderCapability`, `BudgetPolicy`, `GenerationJob`,
`GenerationAttempt`, and `Candidate` collections. Schema 1 records are read
additively and written as schema 2 on their next save.

Shot Plans transition `draft -> confirmed`; upstream asset/spec/style changes
make plans and prompt packages `stale`. Blocking product-spec conflicts or
missing full front/back clean own-capture references produce `blocked` plans.
Attempts transition `queued -> running -> succeeded|failed|interrupted` and
Jobs are terminal only after all their attempts reach terminal states. At app
startup, every persisted `running` attempt is marked `interrupted`; it is never
resent. Persisted Mock `queued` attempts are then safely re-dispatched by the
bounded local executor. The per-project POSIX lock and persisted claim make
this safe when more than one app worker observes startup recovery. Future Live
requests remain interrupted/unknown until an explicit provider reconciliation
interface is implemented; they are never automatically resent.

## Planning, prompts, and references

TEMU and TikTok Shop each compile five deterministic default shots. Operators
may edit sequence, enablement, composition, and an instruction before
confirmation. The plan hash covers product spec, selected versioned Style Pack
and all shots.

The prompt compiler keeps structured confirmed own-capture facts separate from
rendered text. Competitor assets are style-only and cannot become immutable
product facts. It emits a stable hash, explicit preservation/forbidden rules,
negative prompt, output limits, and reference IDs. Annotation previews are
retained for UI explanation but never enter a provider reference set. Reference
selection is deterministic, SHA de-duplicated, front/back aware, and caps its
selection using `ProviderCapability.max_reference_images`.

## Provider, cost, and safety boundary

`MockImageGenerationProvider` is deterministic, offline, free, and writes
large `MOCK GENERATION / NOT A PRODUCT IMAGE` markings. It creates no legacy
cost-ledger entry. The existing APIYI adapters are legacy Pipeline adapters;
they do not establish a verified Studio request schema. M2 therefore provides
only an explicit `NotConfigured` boundary for APIYI Studio Live generation.
No endpoint URL, schema, or cost response is invented.

`LIVE_GENERATION_ENABLED=false` is the default hard gate even with a key.
Live requests additionally require a confirmed non-stale plan, explicit
`mode=live`, provider `apiyi`, paid confirmation, idempotency and budget
checks. M2A rejects before any network action because the provider contract
and reliable pricing contract are unavailable. The CLI requires `--mode live
--provider apiyi --max-cost ... --confirm-paid-generation`; the Web UI exposes
only offline Mock generation.

Mock has estimated/actual cost zero. The strict budget model carries project,
job, and shot limits plus versioned pricing and unknown-pricing policy for the
future live adapter. A live adapter must reserve before dispatch, settle or
record unknown billing, and append the existing ledger rather than create a
second ledger.

## Web, CLI, and execution

The project page links to a generation page with Shot Plan edit/confirm,
Prompt Package preview, actual provider reference lists, cost preview, job
states, authenticated Candidate gallery, and Accept/Reject. Accepting a new
Candidate demotes the prior accepted Candidate for that shot; reject retains
the file and audit state. Candidate media is resolved within the requested
project only.

`tif studio` shares `StudioService` for plan/prompt/preview/mock generation,
jobs, candidates, accept and reject. Web POST creates a durable Job then uses
the bounded local executor as an immediate background trigger; startup
recovery closes the response-before-BackgroundTask gap. Persisted claims
prevent two executors from taking the same Attempt. There is no external queue,
automatic Live retry, or M3 visual QA/repair.

## Non-goals, acceptance, and M3 seam

M2A excludes automatic visual QA, repair, web style search, text/size-chart
generation, external queues, database migration, and real paid API calls.
Its offline acceptance path supports compile/edit/confirm of five shots,
Prompt/Reference inspection, partial Mock failure, manual regeneration as a
new Attempt, candidate decisions, project isolation, and persisted reload.

M3 can add an explicitly versioned APIYI Studio adapter at the provider
boundary, a verified response/cost parser, optional annotation input only for
authorised local repair, and visual QA. It must keep M2's idempotency,
reservation, audit, and role-separation guarantees.
