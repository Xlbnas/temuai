# M3-A0 Image Set Orchestration & Human Review Queue Architecture Freeze

**Status:** frozen design; docs-only milestone
**Code baseline audited:** `57b9fd5d87e85fd4ff1c62a342315239e9086f25`

## 1. Executive Summary

M3 will make Studio capable of producing a complete product-image set without turning
“batch generation” into an uncontrolled paid operation. A frozen Product Facts revision
and Shot Plan revision materialize stable per-shot work items. Each work item can be
generated, reviewed, rejected, locally redone and exported independently. A complete set
is represented by an immutable Publishable Image Set Revision only after a deterministic,
fail-closed completeness evaluation.

This design retains M2C-B's strongest boundaries: one-project atomic JSON persistence,
POSIX project locking, deterministic prompt/request/export identities, single-shot Live
gating, versioned exact pricing, no automatic Provider retry, and an export operation that
never accepts a Candidate. It deliberately adds the missing history boundaries: immutable
input revisions, independent Review Decisions, durable Generation Authorization, per-shot
state dimensions, stale propagation and a publishable-set artifact.

**Frozen decision:** an ImageSetRun has immutable inputs and a guarded operational
projection. ImageSetShot owns mutable operational selections but only appends history.
ReviewDecision and PublishableImageSetRevision are immutable. Stale is an independent
invalidation dimension, never a destructive replacement for generation/review/export state.
No APIYI call is authorized by M3-A0.

## 2. Current-state Code Audit

| Area | Confirmed implementation | Code basis |
| --- | --- | --- |
| Project and facts | One StudioRecord owns StudioProject, assets, analyses and a mutable CanonicalProductSpec. Facts have IDs/evidence but no fact-revision aggregate. | src/studio/models.py: StudioRecord, ProductFact, CanonicalProductSpec |
| Shot Plan | ShotPlan has plan ID, version, content hash and stable ShotSpec.id. Draft plans are edited in place; confirmed plans require a replacement. enabled exists; required/optional does not. | models.py: ShotPlan/ShotSpec; service.py: update_single_shot, confirm_shot_plan |
| Prompt package | A package persists rendered/negative prompt, structured inputs, reference IDs, content hash and stale flag. Recompile replaces current packages for a Shot. | models.py: PromptPackage; service.py: compile_prompt_packages; generation.py: compile_prompt |
| Generation | Jobs/attempts persist request hash, idempotency, pricing and reconciliation fields. Live requires one Shot, paid confirmation, human identity, known price, hard max, key and Live=true. | models.py: GenerationJob/GenerationAttempt; service.py: create_generation_job, run_apiyi_generation_job |
| Candidate review | Candidate's mutable generated/accepted/rejected status holds review. Accepting another Candidate clears the first Candidate's acceptance fields. | models.py: Candidate; service.py: accept_candidate, reject_candidate |
| Derived export | Provenance-rich exports use source/output hash checks and deterministic idempotency under project lock. Export never accepts a Candidate. | models.py: DerivedExport; service.py: create_derived_export, backfill_derived_export |
| Persistence | One JSON aggregate per project, POSIX flock, atomic replacement/fsync and additive v1/v2->v3 loader. No DB unique indexes or cross-project transaction. | src/studio/store.py: StudioStore |
| UI / CLI | Generation page and CLI support plan/prompt/Mock/one Live Shot/review/export. No image-set dashboard, global queue or publish gate. | src/web/routes/studio.py, templates/studio_generation.html, src/cli.py |

### Confirmed existing capabilities

- Stable ShotSpec IDs, ShotPlan and PromptPackage hashes, request hashes, and export
  idempotency keys are usable inputs for M3 identities.
- Attempts are persisted and claimed under the project lock. Live post-submission failures
  become reconcile_required; they are never automatically resent.
- M2C-B export is source-hash-bound and verifies file facts. The TEMU front profile produces
  1350x1800 RGB JPEG.
- Web Live uses the Core gate; Web review has CSRF/authenticated identity and CLI acceptance
  requires --accepted-by. Relevant coverage exists in tests/test_studio_m2*.py and
  tests/test_web_candidates.py.

### Gaps and risks

1. Product facts and prompt packages are snapshots but not immutable revision chains.
2. Required/optional is absent, so whole-set completeness cannot be evaluated.
3. Candidate status mixes artifact and review state; replacing acceptance erases historical
   acceptance evidence.
4. GenerationJob is a request batch, not an Image Set Run; it has no durable set identity,
   authorization record, per-shot selection history or completeness.
5. Current stale handling is prompt/plan-local; it does not propagate to outputs or a set.
6. Project locking protects the JSON aggregate, but M3 must add durable unique identities and
   selection-conflict checks under that lock. UI disabled buttons are not concurrency controls.

## 3. Final Domain Model

All IDs are opaque. All records include project_id; cross-project references are forbidden.
Revision fingerprints are SHA-256 over canonical JSON and never include local paths or secrets.

### 3.1 Immutable inputs

**ProductFactsRevision** is a new immutable snapshot: id, project_id, parent_revision_id,
ordered canonical facts (fact ID/key/value/evidence asset SHA/confirmation), scope tags,
fingerprint, creator and timestamp. CanonicalProductSpec remains the editable working copy;
an Image Set snapshots it. No prior revision is edited.

**ShotPlanRevision** is a new immutable snapshot: id, source shot_plan_id/version/content hash,
platform, style-pack ID/version, ProductFactsRevision ID, ordered shot definitions,
fingerprint, creator and timestamp. A definition carries the existing stable shot_definition_id,
required boolean, type, scene, output dimensions/aspect, composition/rules, references and
export_profile_id. Sequence is display order, not business identity.

**PromptPackageRevision** is immutable: current package payload plus ProductFactsRevision,
ShotPlanRevision, definition IDs, rule/compiler version, provider/model configuration revision
and ordered role-labelled reference asset SHA identities. Same fingerprint returns the same
record; recompile never overwrites an old package.

### 3.2 ImageSetRun and ImageSetShot

**ImageSetRun** represents a requested set from frozen inputs. It contains id, project ID,
facts/plan revision IDs, platform, run_kind (mock or live_capable), creator/time, fingerprint,
required/optional counts, maximum request count, maximum contract estimate, pricing-contract
revision, hard max, lifecycle state, completeness projection, stale state/reasons and
supersedes/superseded-by IDs.

Its input identity and budget ceiling are immutable. Only lifecycle/completeness/supersession
projection transitions are allowed. A changed facts/plan revision creates a new Run; it never
edits the old Run. At most one non-terminal non-superseded current Run exists for
(project, platform, run_kind). A caller that intentionally needs another parallel Run must
supply a distinct explicit run_request_nonce and audit reason; it never silently replaces current.

**ImageSetShot** is created exactly once for each frozen definition when a Run is materialized.
It has id, Run/definition/revision references, required/order/platform/type/scene, current
PromptPackageRevision, current attempt, immutable candidate history IDs, current decision,
selected accepted Candidate, selected Derived Export, five state dimensions, block reasons and
selection_version. It may accumulate attempts/Candidates/decisions/exports but never deletes
them. It selects at most one current accepted Candidate and one profile-matching Export.

### 3.3 Generation Authorization

GenerationAuthorization is durable human evidence, not a checkbox or GenerationJob field:
id; project/Run/Shot scope; provider/model; prompt fingerprint; pricing contract; max attempts,
requests and estimated cost; hard max; authorized_by/at; expiry; consumed count; revocation;
reason; idempotency key and replay policy.

**Decision:** default scope is one Shot, one request, one attempt, expiring after 30 minutes or
when inputs stale. Within one project-lock transaction, service re-reads it, validates scope,
increments consumed_count and creates exactly one attempt. Pre-submission failure is safe but
still requires an explicitly new authorization for another Live call. Submitted/unknown/
reconcile-required consumes the allowance permanently; after resolution, another attempt
requires new human authorization. Automatic retry is always 0. A future batch approval may only
be an envelope with independently atomic per-shot child consumptions.

### 3.4 Review, exports and profile

**ReviewDecision** is immutable and append-only: id, Candidate/work-item ID, decision
(accept/reject/supersede), human reviewer identity, source (interactive/cli/migrated), time,
stable reason codes, note, imported provenance and supersession links. Candidate is not mutated
to store review. Effective review is the newest non-superseded decision; work-item selection is
recorded separately. Rejected Candidate may later be accepted via a new superseding human
decision. Imported M2C-B acceptance stays imported with null decision time when that is unknown.

**ExportProfileRevision** is immutable and includes dimensions/ratio/format/color/quality,
pad-crop/watermark policy, platform spec and pipeline version. The first profile is
temu_3x4_white_pad_v1. A Candidate can have multiple profile exports; a work item selects one.

## 4. Aggregate Boundaries and Persistence

M3-A1 extends StudioRecord with new collections and retains the current one-project JSON
aggregate and StudioStore.lock(project_id) transaction boundary. That is the safest first
implementation: all relationships are project-scoped and atomic replace/fsync is already present.

| Boundary | Owns | Atomicity |
| --- | --- | --- |
| Studio project aggregate | revisions, Runs, work items, authorizations, decisions, selections, export references, audit events | all read-check-write operations hold project lock |
| Candidate/DerivedExport bytes | immutable files addressed by recorded SHA-256 | write new file, then atomically persist reference; cleanup only new orphan on failed write |
| Future DB, not A1 | global queue/index projections | indexes mirror, never weaken, aggregate identities |

A separate OS shot lock is not introduced in A1: an ImageSetShot single_flight_key and
selection_version are checked while holding the project lock. If a future database worker allows
long-running dispatch, add a shot lease keyed by image_set_shot_id, while the project transaction
still creates/consumes authorization and attempts.

Required uniqueness, enforced in aggregate now and later in database indexes:

- Run: project + platform + run kind + frozen input fingerprint + explicit nonce.
- Work item: image_set_run_id + shot_definition_id.
- Prompt: prompt fingerprint.
- Authorization: scope + idempotency key; one active authorization per exact Shot/input.
- Attempt: image_set_shot_id + authorization_id + ordinal; one active single_flight_key.
- Export: source SHA + export profile revision + pipeline/transforms/format (M2C-B rule).
- Publish revision: Run + canonical selected-export binding fingerprint.

## 5. State Machines and Transition Guards

### 5.1 ImageSetRun lifecycle

| State | Entry / allowed action | Exit, prohibition and completeness |
| --- | --- | --- |
| draft | frozen Run created; materialize or cancel | no generation/publish; materialize -> ready; incomplete |
| ready | all work items exist; prompt/authorize actions | activity -> in_progress; no publish; incomplete |
| in_progress | at least one authorized attempt active | monitor/reconcile/review; no publish; incomplete |
| review_required | no active attempt and a required item needs review/export | review/export/local redo; no publish; incomplete |
| blocked | unknown attempt, invalid input, authorization/selection/completeness conflict | reconcile/fix via guarded action only; incomplete |
| complete | evaluator true and publish revision exists | inspect/download/supersede; later invalidation -> stale |
| stale | frozen dependency not current/valid | inspect/create successor only; no new work in stale Run |
| superseded | successor formally replaces Run | historic inspection only; terminal/not current |
| cancelled | human cancellation before completion | historic inspection only; terminal/incomplete |

Complete is computed plus a publish artifact, never a manual toggle. Blocked cannot be bypassed
in UI/CLI. Frozen input changes create a successor, not a return to draft.

### 5.2 ImageSetShot state dimensions

| Dimension | States and guard |
| --- | --- |
| generation | planned -> prompt_ready -> authorization_required -> authorized -> generating -> generated; proven pre-submit failure -> prompt_ready; submitted uncertainty -> blocked |
| review | unreviewed -> accepted or rejected; a later immutable decision supersedes either |
| export | not_required, required, creating, ready, failed; export failure never revokes review |
| stale | fresh, stale, superseded with reason evidence; overlays other states |
| authorization | not_required (Mock), required, authorized, consumed, expired, revoked, exhausted |

Rejected does not regenerate. A new Candidate does not replace old acceptance automatically.
Multiple unreviewed Candidates are permitted, but selection is blocked until human review picks
exactly one. Accepted and exported must remain separate dimensions: accepted-but-export-failed is
a meaningful state.

### 5.3 GenerationAttempt

| State | Meaning and recovery |
| --- | --- |
| created / authorized | durable authorization consumption and pre-dispatch validation |
| started | provider dispatch began; submitted_at recorded before releasing lock |
| succeeded | one validated Candidate durably persisted; actual cost remains null if absent |
| failed | proven never sent or explicit pre-submission rejection; new human authorization required |
| cancelled | only before submission |
| unknown / reconcile_required | provider may have received/billed it; blocks matching dispatch and completion |

Existing queued/submitting/running/provider_pending/downloading/succeeded/failed/unknown/
reconcile_required evidence maps to this model without discarding raw detail. Timeout after
possible send, malformed success, post-submit download/storage failure and restart during
submission are uncertain. Current verified APIYI contracts have no status lookup; reconciliation
retains provider request ID when available, writes only safe notes and never resends. A non-null
provider request ID is unique per provider/model; null remains valid for synchronous success.

## 6. Idempotency, Concurrency and Duplicate-charge Prevention

1. **Run create:** repeated request with same identity/idempotency key returns same Run; explicit
   nonce is required for intentional parallel work.
2. **Materialize:** project lock creates/reuses exactly one work item per frozen definition.
3. **Prompt:** fingerprint contains facts/definition/platform/scene/rule set/provider config and
   role-labelled reference SHA identities. Same fingerprint reuses immutable record.
4. **Authorization:** locked transaction validates expiry/revocation/remaining count, consumes,
   creates attempt and emits audit event together. Never read-then-async increment.
5. **Attempt:** persist and claim single_flight_key under lock before network I/O. Matching active
   or uncertain attempt returns conflict/reconciliation-required, never another Provider call.
   Send a client request ID only if the verified Provider contract supports it.
6. **Export:** reuse M2C-B's source-hash/profile/pipeline/transform identity with profile
   revision, and verify existing file/hash before reuse.
7. **Publish:** sorted (work-item, Candidate SHA, export ID/SHA/profile) bindings plus Run and
   platform are canonical fingerprint. Same binding reuses immutable revision; a changed export
   creates a new revision.

## 7. Live Authorization and Budget Preflight Contract

Creating an Image Set or PromptPackage is never Live approval. Authorization creation requires
human review of exact scope, prompt/reference revision, provider/model, price contract, max
request/attempt count, maximum contract estimate and expiry. Generate consumes an existing
authorization only; the same endpoint must never create and consume it.

Current contract: nano_banana_2 / gemini-3.1-flash-image / APIYI /
apiyi-nb2-per-request-2026-03-01 / **$0.055 per request**. It is a contract estimate, not actual
cost; absent Provider actual remains null/unknown.

Preflight returns required/optional totals, completed totals, maximum remaining requests,
per-request contract price, maximum remaining contract estimate, existing ledger estimate,
actual known/unknown summary, hard max and exact input fingerprints. It fails closed for unknown
pricing, hard-max breach, stale inputs, matching unknown attempt or authorization scope mismatch.
Key balance is never an invoice.

Every dispatch path (Web, CLI, Core, worker, script, test route) checks at attempt creation:

~~~
LIVE_GENERATION_ENABLED=true
AND provider_status=unlocked/ready
AND valid unconsumed human GenerationAuthorization
AND exact pricing contract
AND hard max not exceeded
AND one Shot work item
AND no matching active or uncertain attempt
~~~

Any false condition produces safe error and no Provider call. Automatic retry remains **0**.

## 8. Review Queue and Reject Taxonomy

M3-A2 has one query model with project-local and authenticated global views. Global is a
projection; decisions execute in the owner project aggregate. Filters: project, Run, platform,
shot type, generated time, unreviewed/rejected/accepted/stale, missing export and required block.

Rows show product/Run/Shot, required flag, prompt revision, attempt/safe short Provider ID,
Candidate dimensions/SHA/time, contract estimate, actual known/unknown, clean references, older
Candidates, review/reject history, selected Export and stale/block reasons. Never show secret,
header, full Provider body or binary payload.

Stable reject codes: cropped_subject, cropped_feet, missing_shoes, wrong_garment_structure,
wrong_color, wrong_material_texture, wrong_pocket_layout, wrong_fastener, wrong_model_pose,
wrong_scene, wrong_aspect_ratio, bad_composition, artifact, text_or_logo, policy_risk,
reference_mismatch, other. A decision stores one or more codes and optional human note/type
applicability. Analytics may aggregate but may not change prompts or call Provider. Reject never
auto-generates.

## 9. Completeness Gate

evaluate_completeness(run_id) is deterministic, explainable and read-only over persisted records
plus explicit file/hash verification. It returns:

~~~json
{
  "required_total": 0,
  "required_prompt_ready": 0,
  "required_generated": 0,
  "required_reviewed": 0,
  "required_accepted": 0,
  "required_exported": 0,
  "required_stale": 0,
  "required_blocked": 0,
  "optional_total": 0,
  "optional_exported": 0,
  "is_complete": false,
  "blocking_reasons": []
}
~~~

Complete requires every required frozen Shot, no stale reason, exactly one selected human-accepted
Candidate, one selected valid profile-matching DerivedExport, hash/file/manifest verification, no
pending/unknown/reconcile-required attempt and no selection/revision conflict. Optional absence
does not block; an optional chosen for a publish revision must meet the same checks. The evaluator
fails closed for unreviewed/rejected candidates, missing human review/export, hash mismatch,
changed facts/plan/prompt, deleted required Shot without supersession, unknown attempt, conflicting
selection or malformed/unreadable records. It does not mutate data.

## 10. Stale / Invalidation Graph

~~~text
ProductFactsRevision --+-> ShotPlanRevision -> ImageSetRun -> ImageSetShot
StylePackRevision -----+                             |              |
Reference SHA/rules/provider config -----------------+              v
                                              PromptPackageRevision -> Attempt -> Candidate
                                                                        -> Review -> DerivedExport
ExportProfileRevision ----------------------------------------------------------^    |
                                                                                     v
                                                              PublishableImageSetRevision
Pricing contract -> future authorization/preflight/audit, never old media mutation
~~~

Stale evidence is append-only: scope, reason code, upstream revision, detected time/actor.
No historic Candidate, decision or Export is deleted.

| Upstream change | Stales | Preserves |
| --- | --- | --- |
| visual fact: color/fabric/pockets/zipper/Velcro/size/structure/compliance | affected prompts/work-item selections/exports/publish set; all Shots unless fact scope narrows | historic bytes/decisions |
| copy-only fact | copy/publish metadata only when scoped as non-visual | unrelated media |
| type/scene/composition/reference/provider/model/rule | affected Shot prompt and selection/publish set | unrelated Shot items |
| required add/remove or optional->required | Run completeness; old Run gets successor/supersession | history |
| sequence-only change | presentation projection | media, unless platform defines order contractually |
| export profile encoding/ratio/pad/color/watermark | Export selection and publish set | Candidate/review |
| pricing contract | future authorization/preflight only | old accounting/media |

Accepted historic-but-not-current is **superseded**; a dependency mismatch is **stale**; failed
file/hash/manifest integrity is **invalid**. Stale artifacts remain visible with warning; invalid
artifacts cannot be publishable/downloadable as a valid result. Local redo touches only affected
work item links.

## 11. Local Regeneration and Publishable Set

Redo one Shot keeps the work-item ID and appends history:

~~~text
choose/reuse immutable PromptPackageRevision
-> explicit one-Shot authorization (or Mock path)
-> new GenerationAttempt -> Candidate -> human ReviewDecision
-> selected Candidate -> DerivedExport -> completeness -> new Publishable Set revision
~~~

It never deletes Candidates/decisions/exports, overwrites prompts/attempts or rewrites historic
acceptance. Selection writes include expected selection_version; mismatch returns 409
review_conflict with fresh history. Later acceptance supersedes selection, not prior evidence.

PublishableImageSetRevision is immutable, not a dynamic view: id, Run/fingerprint/platform,
profiles, sorted required and included-optional Shot->Candidate->Export bindings, completeness
snapshot, canonical fingerprint, time/actor and manifest/hash/stale-invalid evidence. Publish
runs evaluator first. Same binding returns existing revision; a redone/exported Shot produces a
new one. Older revisions are viewable. Stale revision download requires warning; invalid revision
is not publishable/downloadable.

## 12. API, UI and CLI Draft

All writes require authenticated actor, browser CSRF, Idempotency-Key, correlation ID, project
lock and safe audit event. Agent may prepare/read Mock work only; it cannot make human review or
Live authorization.

| Endpoint | Preconditions/conflict | Provider/human boundary |
| --- | --- | --- |
| POST /projects/{project_id}/image-sets | frozen input + key; 409 conflicting current Run | no call; human/operator |
| GET /projects/{project_id}/image-sets; GET /image-sets/{id} | project access | no call |
| POST /image-sets/{id}/materialize | draft/fresh; unique work items | no call; agent allowed |
| GET /image-sets/{id}/shots; GET /image-sets/{id}/completeness | access | no call/read-only evaluator |
| POST /image-set-shots/{id}/prompt-packages | fresh frozen inputs + fingerprint key | no call; agent allowed |
| POST /image-set-shots/{id}/generation-authorizations | human, shown preflight/exact price/hard max | no call; agent prohibited |
| POST /image-set-shots/{id}/generate | existing valid authorization; 409 active/unknown | Provider only after all gates; human Live action |
| GET /review-queue | filtered access | no call |
| POST /candidates/{id}/reviews | human, expected selection version | no call; agent prohibited |
| POST /image-set-shots/{id}/exports | selected accepted candidate/profile/key | local only; human/operator |
| POST /image-sets/{id}/publish | completeness true/binding key | no Provider call |
| GET /publishable-image-sets/{id} | access/integrity | no call |

Image Set Overview shows frozen inputs/progress/cost/Live lock/per-Shot matrix. Shot Detail shows
prompt/references/attempts/Candidates/decisions/selection/export/stale and distinct authorize/
redo actions. Review Queue supports compare/accept/reject structured reason. Publish Gate shows
the exact evaluator blockers. CLI group:

~~~text
image-set create | show | materialize | completeness | shots
image-set authorize-shot | generate-shot | review-candidate | export-shot | publish
~~~

authorize-shot prints a redacted immutable preflight receipt; generate-shot accepts existing
authorization ID and cannot create one.

## 13. Audit Events, Failure and Recovery

Events: image_set_created, image_set_materialized, shot_work_item_created,
prompt_package_created, generation_authorized, generation_authorization_consumed,
generation_started, generation_succeeded, generation_failed, generation_unknown,
candidate_created, candidate_reviewed, candidate_rejected, candidate_accepted,
derived_export_created, image_set_completeness_evaluated, publishable_image_set_created,
artifact_marked_stale, image_set_superseded.

Every event holds event ID, project/aggregate IDs, actor type/identity, time, correlation/
idempotency IDs, previous/new safe state, reason and safe metadata. Never key/header/cookie/full
Provider payload/.env/image binary.

| Failure | Auto retry | Cost/unknown | Recovery/completeness |
| --- | --- | --- | --- |
| validation, missing/exhausted/revoked auth, Live disabled, locked, stale, hard-max | 0 | no call | correct input/new auth; blocks relevant Shot |
| provider 4xx/proven pre-send connect failure | 0 | no confirmed charge; failed | new explicit auth if desired; required Shot blocks |
| post-send timeout/5xx/malformed result/download-storage-manifest failure | 0 | possible charge; unknown/reconcile_required, actual null unless known | reconcile without resend; new auth only after human decision; blocks |
| hash/file failure | 0 | no new charge | mark invalid; repair local export only if source valid; blocks |
| duplicate/lock/selection conflict | 0 | no call | return existing or 409/refetch; unresolved conflict blocks |
| review conflict | 0 | no call | reload history/new superseding human decision |
| export failure | 0 | no call | idempotent local retry; acceptance retained; required publish blocks |

## 14. Security Boundaries

- APIYI key remains Production .env only; it is absent from JSON, manifest, audit, UI, CLI
  output and Git. Status reports configured yes/no only.
- Default Live remains false. Provider lock + Core authorization/hard-max gates are enforced
  service-side, not by UI convention. This milestone changes neither .env, Compose nor container.
- Only authenticated humans create Live authorization or accept/reject. Agents may draft/Mock
  within product policy but cannot impersonate a reviewer.
- Candidate/export files stay project-local and hash-verified; M2B safe-result/SSRF/logging
  controls remain in force.

## 15. Migration Strategy

M3-A1 is additive StudioRecord v3->v4: new collections load empty, existing records remain
untouched and next atomic write preserves all historical fields. Back up each project first.
A migration may create a frozen revision/Run only after a human selects a source confirmed plan;
it imports existing acceptance as ReviewDecision(source=migrated), preserving null decision time.
It must not infer required/optional, create authorization or auto-publish.

Rollback is code rollback plus pre-migration JSON backup where no later business write occurred.
Old v3 code must reject v4 rather than silently drop fields. Tests: v3 load/v4 round trip,
atomic crash safety, provenance-preserving migration, no historical byte changes.

## 16. Rollout Plan and M3-A1 Handoff

| Phase | Scope | Gate/tests | Prohibited / rollback |
| --- | --- | --- | --- |
| **M3-A1** | v4 schema, immutable revisions, Runs/work items, state machine, completeness, stale records | model/transition/uniqueness/concurrent-consume/evaluator/migration tests; Live false | no APIYI, queue, review rewrite; backup/code rollback |
| **M3-A2** | materialization UI/CLI, Mock per-shot orchestration, queues, structured reviews, local redo | integration/concurrency/UI/CLI tests | no APIYI/Live; preserve history |
| **M3-A3** | single-Shot Live auth/preflight/atomic consume/reconciliation UX | full gate/duplicate/unknown/budget tests and a new explicit user authorization | no batch Live/retry; disabling Live preserves attempt |
| **M3-A4** | profile selection, publish revisions, stale propagation, history comparison | hash/file/completeness/stale/publish idempotency tests | no destructive cleanup |

M3-A0/A1/A2 prohibit APIYI Live calls. M3-A3 cannot start automatically and is the earliest
phase that could call APIYI after newly explicit user authorization. M3-A0 neither deploys nor
restarts Production.

M3-A1 expected files: src/studio/models.py, store.py, service.py and focused tests; potentially
a migration/backup helper. It must not change src/studio/apiyi.py, runtime Compose or .env.
Required tests cover all state transitions, migrations, materialization idempotency, concurrent
authorization consumption, completeness fail-closed, stale propagation, audit redaction and
existing M2/M2B/M2C regressions.

## 17. ADR-style Final Decisions

| Decision | Rationale, trade-off and consequence |
| --- | --- |
| Immutable Run inputs + guarded projection | Frozen reproducibility without expensive fully event-sourced JSON. Test changed plan produces successor, not mutation. |
| Immutable publish revision | Dynamic view cannot prove published binding. Test same fingerprint reuses, changed export creates new revision. |
| Independent immutable ReviewDecision | Current Candidate mutable status erases history. Test accept/reject/supersede retention. |
| Acceptance supersedes, never overwrites | Better later Candidate preserves evidence. Test stale old acceptance remains. |
| Five work-item dimensions | Single status hides accepted-but-export-failed/stale. Test each dimension independently. |
| Stale is independent evidence | Status replacement destroys history. Test reasons/evaluator behavior. |
| Atomic one-shot authorization | Least privilege and duplicate-charge prevention. Test concurrent consumers create one attempt. |
| Submitted failure needs new human authorization | Unknown may be billed; no replay. Test no blind resend. |
| Unknown recovery is manual | No verified Provider status API. Test unknown blocks completeness. |
| Same input returns same Run | Prevents duplicate sets; explicit nonce permits intentional parallel work. |
| Project lock owns transaction; logical shot key owns single-flight | One JSON file cannot safely have independent writes. Future DB mirrors keys. |
| Prompt profile and publish fingerprints are explicit | Explainability and safe idempotency. Test every input change changes expected fingerprint. |
| Required governs completion; optional governs inclusion | Core set should not be blocked by absent optional content. Test selected optional must still validate. |
| Human/agent service boundary | UI-only policy is bypassable. Test actor guards on Live/review. |

## 18. Explicit Non-goals

M3-A0 implements no schema/migration/runtime/API/UI/CLI code, no queue, no Provider call, no
image generation, Candidate/Export/acceptance change, no pricing/key/.env/Compose alteration,
no Production deployment/restart and no M3-A1 work.

## 19. Remaining Open Questions

1. Which named human roles may publish, and whether publication needs two-person approval?
2. Which TEMU Shot types are required by product category? M3-A1 models the flag but must not
   infer category policy from one ACU set.
3. May stale historic publishable files be downloaded externally, and for how long?
4. Does APIYI later document a verified request-status endpoint? Until audited in adapter/docs,
   unknown remains fail-closed and non-retryable.

Core architecture is not deferred: revision ownership, review history, authorization consumption,
unknown recovery, state dimensions, completeness, stale propagation, idempotency and publish
identity are frozen above.
