# Product Image Studio V1 — M1 implementation notes

## Audited architecture

The baseline commit `ba8e652` has a Python 3.10+ package (the Docker image is
Python 3.12). Its old workflow is `Click or FastAPI route -> Pipeline ->
providers/processors -> YAML input and JSON output manifest`. Web pages use
Jinja2, with HTMX loaded globally, and session-cookie authentication plus CSRF
tokens. SKU input is YAML under `INPUT_DIR`; output manifests are JSON under
`OUTPUT_DIR`; jobs are explicitly in-memory. Docker mounts `DATA_DIR`,
`INPUT_DIR`, `OUTPUT_DIR`, cache and logs persistently.

The attachment describes an older development path (`~/projects/...`) than the
actual checkout used here. The actual repo has no Studio domain, no SQLite
store, and no verified vision-analysis client; it does have safe path helpers,
Pillow, a provider abstraction, cost ledger, authenticated Jinja2 routes, and
Docker data volumes.

## M1 boundary and model

Studio is an isolated aggregate at `DATA_DIR/studio/<project-id>/project.json`.
It stores `StudioProject`, archived `Asset`, strict `AssetAnalysis` and
`DetailRegion` objects, and a versioned `CanonicalProductSpec`. Each analysis
contains an explicit `OverrideValue` with model, user override, and effective
values. Asset originals, thumbnails, annotations, and boards live below the
same persistent project directory. JSON and original file writes use
write-temp-then-replace.

M1 reuses image validation, Pillow, safe path resolution, config directories,
authentication and templates. It does **not** call or alter Pipeline state,
legacy SKU YAML, Candidates, providers, ledger, or paid APIs.

## User flow

`/studio` creates a project, then its project page archives up to 20 images,
runs the deterministic `MockAssetAnalyzer`, lets an operator correct source,
content, tags, and regions, renders a Pillow annotation, compiles facts, picks
one of two TEMU or two TikTok Shop packs, and previews the offline reference
bundle. The equivalent shared-core commands are `tif studio create`, `import`,
`analyze`, `render-annotations`, `compile-spec`, and `compile-bundle`.

## Risks and non-goals

The mock analyzer only offers deterministic demonstration data; the production
boundary returns `NotConfigured` because this repository contains no verified
vision-analysis request/response contract. M1 has no generation, paid AI call,
web crawling, database migration, or deployment. The generated reference board
is previewed locally and is never sent to a provider.

## Acceptance criteria

The implementation accepts and de-duplicates safe images, retains originals,
renders annotation PNGs without modifying originals, preserves evidence chains
and unresolved fact conflicts, isolates competitor images to style references,
offers four versioned compliant packs, and shares one Core service between CLI
and Web. Offline tests exercise these behaviors and legacy tests must continue
to pass.
