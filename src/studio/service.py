"""Core use cases shared by Studio CLI and Web routes."""

from __future__ import annotations

import hashlib
import os
import tempfile
import warnings
from collections import defaultdict
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageOps

from src.core.config import AppConfig
from src.studio.analyzers import AssetAnalyzer, NotConfiguredAssetAnalyzer
from src.studio.generation import (
    MockImageGenerationProvider,
    blocking_reasons,
    compile_prompt,
    default_budget_policy,
    default_shots,
    plan_hash,
    safe_error,
    stable_hash,
)
from src.studio.models import (
    Asset,
    AssetAnalysis,
    BudgetPolicy,
    Candidate,
    CandidateStatus,
    CanonicalProductSpec,
    ContentKind,
    DetailRegion,
    GenerationAttempt,
    GenerationJob,
    GenerationStatus,
    Importance,
    NormalizedBBox,
    PlanStatus,
    ProductFact,
    PromptPackage,
    ProviderCapability,
    ReferenceBundle,
    ReferenceItem,
    ReferenceRole,
    ShotPlan,
    ShotSpec,
    SourceKind,
    StudioPlatform,
    StudioProject,
    StudioRecord,
    StylePack,
    new_id,
    utc_now,
)
from src.studio.rendering import render_annotations, render_detail_board
from src.studio.store import StudioStore
from src.utils.paths import resolve_within

ALLOWED_FORMATS = {
    "JPEG": ("image/jpeg", ".jpg"),
    "PNG": ("image/png", ".png"),
    "WEBP": ("image/webp", ".webp"),
}
MAX_IMAGE_PIXELS = 40_000_000
MAX_IMAGE_DIMENSION = 10_000
IMPORTANCE_RANK = {
    Importance.CRITICAL: 0,
    Importance.HIGH: 1,
    Importance.MEDIUM: 2,
    Importance.LOW: 3,
}


def builtin_style_packs() -> list[StylePack]:
    forbidden = [
        "weapons",
        "military emblems",
        "national flags",
        "ranks",
        "battlefields",
        "tactical vests",
    ]
    preservation = [
        "Preserve exact product construction",
        "Do not add logos",
        "Do not add or remove pockets",
    ]
    return [
        StylePack(
            id="temu-clean-catalog-v1",
            platform=StudioPlatform.TEMU,
            version="1.0.0",
            name="TEMU Clean Catalog",
            composition="Centered full product with breathing room",
            lighting="Soft even studio",
            background="Pure white",
            visual_tone="Clear commercial",
            product_preservation_rules=preservation,
            forbidden_elements=forbidden,
            output_aspect_ratio="3:4",
        ),
        StylePack(
            id="temu-detail-story-v1",
            platform=StudioPlatform.TEMU,
            version="1.0.0",
            name="TEMU Detail Story",
            composition="Product-led detail sequence",
            lighting="Natural softbox",
            background="Clean neutral",
            visual_tone="Trustworthy detail",
            product_preservation_rules=preservation,
            forbidden_elements=forbidden,
            output_aspect_ratio="3:4",
        ),
        StylePack(
            id="tiktok-lifestyle-hook-v1",
            platform=StudioPlatform.TIKTOK_SHOP,
            version="1.0.0",
            name="TikTok Shop Lifestyle Hook",
            composition="Product clear in first frame",
            lighting="Bright natural",
            background="Modern everyday",
            visual_tone="Energetic social commerce",
            product_preservation_rules=preservation,
            forbidden_elements=forbidden,
            output_aspect_ratio="9:16",
        ),
        StylePack(
            id="tiktok-ugc-detail-v1",
            platform=StudioPlatform.TIKTOK_SHOP,
            version="1.0.0",
            name="TikTok Shop UGC Detail",
            composition="Close detail paired with full product",
            lighting="Soft daylight",
            background="Simple home studio",
            visual_tone="Approachable",
            product_preservation_rules=preservation,
            forbidden_elements=forbidden,
            output_aspect_ratio="9:16",
        ),
    ]


class StudioService:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.store = StudioStore(config.data_dir)

    def create_project(
        self, name: str, platform: StudioPlatform = StudioPlatform.TEMU
    ) -> StudioProject:
        project = StudioProject(name=name.strip(), target_platform=platform)
        self.store.save(StudioRecord(project=project))
        return project

    def list_projects(self) -> list[StudioProject]:
        return self.store.list_projects()

    def get_record(self, project_id: str) -> StudioRecord:
        return self.store.load(project_id)

    def _project_path(self, project_id: str, *parts: str) -> Path:
        return resolve_within(self.store.project_dir(project_id), *parts)

    def import_asset(
        self, project_id: str, filename: str, content: bytes, max_bytes: int
    ) -> tuple[Asset, bool]:
        """Validate bytes before atomically retaining the original and thumbnail."""
        if not filename or len(filename) > 255 or not content:
            raise ValueError("Image filename and content are required")
        if len(content) > max_bytes:
            raise ValueError("Image exceeds maximum upload size")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as probe:
                    image_format = probe.format
                    if getattr(probe, "n_frames", 1) != 1:
                        raise ValueError("Animated or multi-frame images are not supported")
                    probe.verify()
                with Image.open(BytesIO(content)) as image:
                    width, height = ImageOps.exif_transpose(image).size
                    if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION:
                        raise ValueError("Image dimensions exceed the allowed limit")
                    if width * height > MAX_IMAGE_PIXELS:
                        raise ValueError("Image pixel count exceeds the allowed limit")
                    exif = image.getexif()
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            ValueError,
        ) as exc:
            raise ValueError("File is not a valid supported image") from exc
        if image_format not in ALLOWED_FORMATS or width <= 0 or height <= 0:
            raise ValueError("Unsupported image format")
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            digest = hashlib.sha256(content).hexdigest()
            existing = next((asset for asset in record.assets if asset.sha256 == digest), None)
            if existing:
                return existing, True
            mime_type, extension = ALLOWED_FORMATS[image_format]
            asset = Asset(
                project_id=project_id,
                original_filename=Path(filename).name,
                stored_path=f"assets/originals/{digest}{extension}",
                thumbnail_path=f"assets/thumbnails/{digest}.png",
                sha256=digest,
                mime_type=mime_type,
                width=width,
                height=height,
                exif_summary={
                    str(key): str(value)[:200]
                    for key, value in exif.items()
                    if key in {271, 272, 306, 315}
                },
            )
            original = self._project_path(project_id, asset.stored_path)
            thumbnail = self._project_path(project_id, asset.thumbnail_path or "")
            self._atomic_bytes(original, content)
            try:
                self._write_thumbnail(content, thumbnail)
            except Exception:
                original.unlink(missing_ok=True)
                raise
            record.assets.append(asset)
            self._invalidate_spec(record)
            record.project.updated_at = utc_now()
            self.store.save(record)
            return asset, False

    @staticmethod
    def _atomic_bytes(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise

    @staticmethod
    def _write_thumbnail(content: bytes, destination: Path) -> None:
        with Image.open(BytesIO(content)) as opened:
            image = ImageOps.exif_transpose(opened).convert("RGB")
        image.thumbnail((480, 480))
        destination.parent.mkdir(parents=True, exist_ok=True)
        image.save(destination, format="PNG")

    def analyze_asset(
        self,
        project_id: str,
        asset_id: str,
        analyzer: AssetAnalyzer | None = None,
        schema_version: str = "1",
    ) -> AssetAnalysis:
        analyzer = analyzer or NotConfiguredAssetAnalyzer()
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            asset = self._asset(record, asset_id)
            cached = next(
                (
                    analysis
                    for analysis in record.analyses
                    if analysis.asset_id == asset_id
                    and analysis.analyzer_name == analyzer.name
                    and analysis.analyzer_version == analyzer.version
                    and analysis.config_version == analyzer.config_version
                    and analysis.schema_version == schema_version
                    and analysis.source_image_sha256 == asset.sha256
                ),
                None,
            )
            if cached:
                return cached
            analysis = analyzer.analyze(
                self._project_path(project_id, asset.stored_path), asset, schema_version
            )
            record.analyses = [item for item in record.analyses if item.asset_id != asset_id]
            record.analyses.append(analysis)
            self._invalidate_spec(record)
            record.project.updated_at = utc_now()
            self.store.save(record)
            return analysis

    def update_analysis(
        self,
        project_id: str,
        asset_id: str,
        source_kind: SourceKind | None = None,
        content_kind: ContentKind | None = None,
        detail_types: list[str] | None = None,
    ) -> AssetAnalysis:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            analysis = self._analysis(record, asset_id)
            if source_kind is not None:
                analysis.source_kind.user_override = source_kind
            if content_kind is not None:
                analysis.content_kind.user_override = content_kind
            if detail_types is not None:
                analysis.detail_types.user_override = [
                    item.strip() for item in detail_types if item.strip()
                ]
            self._invalidate_spec(record)
            record.project.updated_at = utc_now()
            self.store.save(record)
            return analysis

    def update_region(
        self,
        project_id: str,
        asset_id: str,
        region_id: str,
        *,
        detail_type: str | None = None,
        importance: Importance | None = None,
        label: str | None = None,
        confirmed: bool | None = None,
        bbox: NormalizedBBox | None = None,
        delete: bool = False,
    ) -> AssetAnalysis:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            analysis = self._analysis(record, asset_id)
            region = next((item for item in analysis.detail_regions if item.id == region_id), None)
            if region is None:
                raise KeyError("Detail region not found")
            if delete:
                analysis.detail_regions = [
                    item for item in analysis.detail_regions if item.id != region_id
                ]
            else:
                if detail_type is not None:
                    region.detail_type.user_override = self._required_text(detail_type, "Detail type")
                if importance is not None:
                    region.importance.user_override = importance
                if label is not None:
                    region.label.user_override = self._required_text(label, "Label")
                if confirmed is not None:
                    region.user_confirmed = confirmed
                if bbox is not None:
                    region.normalized_bbox = bbox
            self._invalidate_spec(record)
            record.project.updated_at = utc_now()
            self.store.save(record)
            return analysis

    def reset_analysis_overrides(self, project_id: str, asset_id: str) -> AssetAnalysis:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            analysis = self._analysis(record, asset_id)
            analysis.source_kind.user_override = None
            analysis.content_kind.user_override = None
            analysis.detail_types.user_override = None
            for region in analysis.detail_regions:
                region.detail_type.user_override = None
                region.importance.user_override = None
                region.label.user_override = None
            self._invalidate_spec(record)
            record.project.updated_at = utc_now()
            self.store.save(record)
            return analysis

    def render_annotations(self, project_id: str, asset_id: str) -> Path:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            asset = self._asset(record, asset_id)
            analysis = self._analysis(record, asset_id)
            relative = f"assets/annotations/{asset.id}.png"
            path = self._project_path(project_id, relative)
            render_annotations(
                self._project_path(project_id, asset.stored_path), path, analysis.detail_regions
            )
            asset.annotation_path = relative
            record.project.updated_at = utc_now()
            self.store.save(record)
            return path

    def compile_product_spec(self, project_id: str) -> CanonicalProductSpec:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            spec = self._build_product_spec(record, project_id)
            record.product_spec = spec
            record.project.updated_at = utc_now()
            self.store.save(record)
            return spec

    @staticmethod
    def _build_product_spec(record: StudioRecord, project_id: str) -> CanonicalProductSpec:
        facts_by_key: dict[str, list[ProductFact]] = defaultdict(list)
        for analysis in record.analyses:
            if analysis.source_kind.effective_value != SourceKind.OWN_CAPTURE:
                continue
            for region in analysis.detail_regions:
                strong = region.user_confirmed or (
                    region.confidence >= 0.90
                    and region.importance.effective_value in {Importance.CRITICAL, Importance.HIGH}
                )
                if not strong:
                    continue
                key = region.detail_type.effective_value
                facts_by_key[key].append(
                    ProductFact(
                        key=key,
                        value=region.label.effective_value,
                        description="Confirmed own-capture detail",
                        priority=region.importance.effective_value,
                        evidence_asset_ids=[analysis.asset_id],
                        user_confirmed=region.user_confirmed,
                        status="strong",
                    )
                )
        facts: list[ProductFact] = []
        for key, candidates in facts_by_key.items():
            confirmed = [fact for fact in candidates if fact.user_confirmed]
            if confirmed:
                confirmed_values = {fact.value for fact in confirmed}
                if len(confirmed_values) == 1:
                    fact = confirmed[0]
                    fact.evidence_asset_ids = sorted(
                        {aid for candidate in confirmed for aid in candidate.evidence_asset_ids}
                    )
                    facts.append(fact)
                    continue
            values = {fact.value for fact in candidates}
            if len(values) == 1:
                fact = candidates[0]
                fact.evidence_asset_ids = sorted(
                    {aid for candidate in candidates for aid in candidate.evidence_asset_ids}
                )
                facts.append(fact)
            else:
                facts.extend(
                    [candidate.model_copy(update={"status": "review"}) for candidate in candidates]
                )
        return CanonicalProductSpec(project_id=project_id, facts=facts)

    def style_packs(self, platform: StudioPlatform | None = None) -> list[StylePack]:
        return [
            pack for pack in builtin_style_packs() if platform is None or pack.platform == platform
        ]

    def select_style_pack(self, project_id: str, style_pack_id: str) -> StudioProject:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            pack = next(
                (
                    item
                    for item in self.style_packs(record.project.target_platform)
                    if item.id == style_pack_id
                ),
                None,
            )
            if pack is None:
                raise KeyError("Style pack not found for this project platform")
            record.project.selected_style_pack_id = pack.id
            record.project.selected_style_pack = pack
            self._invalidate_generation(record)
            record.project.updated_at = utc_now()
            self.store.save(record)
            return record.project

    def compile_reference_bundle(self, project_id: str) -> ReferenceBundle:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            spec = record.product_spec or self._build_product_spec(record, project_id)
            if record.product_spec is None:
                record.product_spec = spec
                record.project.updated_at = utc_now()
                self.store.save(record)
            return self._build_reference_bundle(record, spec)

    def _build_reference_bundle(
        self, record: StudioRecord, spec: CanonicalProductSpec
    ) -> ReferenceBundle:
        project_id = record.project.id
        analysis_by_asset = {analysis.asset_id: analysis for analysis in record.analyses}
        product: list[Asset] = []
        details: list[tuple[Asset, DetailRegion]] = []
        style: list[Asset] = []
        for asset in record.assets:
            analysis = analysis_by_asset.get(asset.id)
            if not analysis:
                continue
            if analysis.source_kind.effective_value == SourceKind.COMPETITOR_REFERENCE:
                style.append(asset)
            elif analysis.source_kind.effective_value == SourceKind.OWN_CAPTURE:
                if analysis.content_kind.effective_value in {
                    ContentKind.PRODUCT_FULL_FRONT,
                    ContentKind.PRODUCT_FULL_BACK,
                }:
                    product.append(asset)
                details.extend(
                    (asset, region)
                    for region in analysis.detail_regions
                    if region.importance.effective_value in {Importance.CRITICAL, Importance.HIGH}
                )
        product.sort(
            key=lambda asset: (
                0
                if analysis_by_asset[asset.id].content_kind.effective_value
                == ContentKind.PRODUCT_FULL_FRONT
                else 1,
                -(asset.width * asset.height),
            )
        )
        unique_product = self._unique_assets(product)
        details.sort(
            key=lambda item: (
                IMPORTANCE_RANK[item[1].importance.effective_value],
                -(item[0].width * item[0].height),
            )
        )
        clean_details = self._unique_assets([asset for asset, _ in details])
        # The board is a human-readable clean contact sheet; annotation previews
        # remain separately labelled and never become image-generation references.
        board_items = [
            (asset, self._project_path(project_id, asset.stored_path)) for asset in clean_details
        ]
        board_relative = "assets/boards/detail-reference-board.png"
        board_path = self._project_path(project_id, board_relative)
        render_detail_board(board_items, board_path)
        style = sorted(style, key=lambda asset: (asset.sha256, asset.id))
        annotation_previews = [asset for asset in clean_details if asset.annotation_path]
        references = [
            *[
                ReferenceItem(role=ReferenceRole.PRODUCT_REFERENCE_CLEAN, asset_id=asset.id)
                for asset in unique_product
            ],
            *[
                ReferenceItem(role=ReferenceRole.DETAIL_REFERENCE_CLEAN, asset_id=asset.id)
                for asset in clean_details
            ],
            *[
                ReferenceItem(
                    role=ReferenceRole.HUMAN_ANNOTATION_PREVIEW,
                    asset_id=asset.id,
                    relative_path=asset.annotation_path,
                )
                for asset in annotation_previews
            ],
            *[
                ReferenceItem(role=ReferenceRole.STYLE_REFERENCE, asset_id=asset.id)
                for asset in style
            ],
            ReferenceItem(role=ReferenceRole.CANONICAL_PRODUCT_SPEC),
        ]
        selected_pack = record.project.selected_style_pack
        if selected_pack is not None:
            references.append(ReferenceItem(role=ReferenceRole.STYLE_PACK))
        return ReferenceBundle(
            project_id=project_id,
            product_asset_ids=[asset.id for asset in unique_product],
            style_asset_ids=[asset.id for asset in style],
            detail_board_path=board_relative,
            product_spec=spec,
            style_pack=selected_pack,
            references=references,
        )

    def resolve_asset_path(
        self, project_id: str, asset_id: str, variant: str = "original"
    ) -> Path:
        record = self.store.load(project_id)
        asset = self._asset(record, asset_id)
        relative = {
            "original": asset.stored_path,
            "thumbnail": asset.thumbnail_path,
            "annotation": asset.annotation_path,
        }.get(variant)
        if not relative:
            raise KeyError("Asset variant not found")
        path = self._project_path(project_id, relative)
        if not path.is_file():
            raise FileNotFoundError("Asset file not found")
        return path

    def reference_board_path(self, project_id: str) -> Path:
        path = self._project_path(project_id, "assets/boards/detail-reference-board.png")
        if not path.is_file():
            raise FileNotFoundError("Reference board has not been compiled")
        return path

    # --- M2 generation core: shared by Click and FastAPI routes. ---

    @staticmethod
    def mock_capability() -> ProviderCapability:
        return MockImageGenerationProvider().capability()

    def compile_shot_plan(self, project_id: str) -> ShotPlan:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            pack = record.project.selected_style_pack
            if pack is None:
                raise ValueError("Select a Style Pack before compiling a Shot Plan")
            reasons = blocking_reasons(record)
            shots = default_shots(record.project.target_platform, pack)
            plan = ShotPlan(
                project_id=project_id,
                platform=record.project.target_platform,
                style_pack_id=pack.id,
                style_pack_version=pack.version,
                product_spec_version=(record.product_spec.compiled_at if record.product_spec else "missing"),
                version=max((item.version for item in record.shot_plans), default=0) + 1,
                status=PlanStatus.BLOCKED if reasons else PlanStatus.DRAFT,
                shots=shots,
                content_hash=plan_hash(record, shots, pack),
                blocking_reasons=reasons,
            )
            record.shot_plans.append(plan)
            record.project.updated_at = utc_now()
            self.store.save(record)
            return plan

    def update_shot_plan(self, project_id: str, plan_id: str, shots: list[ShotSpec]) -> ShotPlan:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            plan = self._shot_plan(record, plan_id)
            if plan.status == PlanStatus.CONFIRMED:
                raise ValueError("A confirmed Shot Plan cannot be edited; compile a replacement")
            pack = record.project.selected_style_pack
            if pack is None:
                raise ValueError("Style Pack is missing")
            plan.shots = sorted(shots, key=lambda shot: shot.sequence)
            plan.blocking_reasons = blocking_reasons(record)
            plan.status = PlanStatus.BLOCKED if plan.blocking_reasons else PlanStatus.DRAFT
            plan.content_hash = plan_hash(record, plan.shots, pack)
            plan.updated_at = utc_now()
            for package in record.prompt_packages:
                if package.shot_id in {shot.id for shot in plan.shots}:
                    package.stale = True
            record.project.updated_at = utc_now()
            self.store.save(record)
            return plan

    def confirm_shot_plan(self, project_id: str, plan_id: str, confirmed_by: str) -> ShotPlan:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            plan = self._shot_plan(record, plan_id)
            if plan.status in {PlanStatus.STALE, PlanStatus.BLOCKED} or plan.blocking_reasons:
                raise ValueError("Shot Plan is stale or blocked and cannot be confirmed")
            plan.status = PlanStatus.CONFIRMED
            plan.confirmed_at = utc_now()
            plan.confirmed_by = confirmed_by
            plan.updated_at = utc_now()
            self.store.save(record)
            return plan

    def compile_prompt_packages(
        self, project_id: str, plan_id: str, capability: ProviderCapability | None = None
    ) -> list[PromptPackage]:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            plan = self._shot_plan(record, plan_id)
            if plan.status in {PlanStatus.BLOCKED, PlanStatus.STALE}:
                raise ValueError("Shot Plan must be valid before compiling prompts")
            if record.product_spec is None or record.project.selected_style_pack is None:
                raise ValueError("Product Spec and Style Pack are required")
            cap = capability or self.mock_capability()
            packages = [
                compile_prompt(record, shot, record.project.selected_style_pack, cap)
                for shot in plan.shots if shot.enabled
            ]
            record.prompt_packages = [
                item for item in record.prompt_packages if item.shot_id not in {shot.id for shot in plan.shots}
            ] + packages
            record.project.updated_at = utc_now()
            self.store.save(record)
            return packages

    def create_generation_job(
        self,
        project_id: str,
        plan_id: str,
        *,
        mode: str = "mock",
        provider: str = "mock",
        model: str = "mock-image-v1",
        budget_policy: BudgetPolicy | None = None,
        shot_id: str | None = None,
        paid_confirmation: bool = False,
    ) -> GenerationJob:
        if mode != "mock":
            # LIVE_GENERATION_ENABLED deliberately defaults false even when a key exists.
            if self.config.safe_env("LIVE_GENERATION_ENABLED", "false").lower() != "true":
                raise ValueError("Live generation is disabled by LIVE_GENERATION_ENABLED=false")
            if provider != "apiyi" or not paid_confirmation:
                raise ValueError("Live generation requires verified provider and explicit paid confirmation")
            raise ValueError("NotConfigured: APIYI Studio adapter has no verified request contract")
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            plan = self._shot_plan(record, plan_id)
            if plan.status != PlanStatus.CONFIRMED:
                raise ValueError("Confirm the Shot Plan before generation")
            shot_ids = {shot.id for shot in plan.shots if shot.enabled}
            if shot_id is not None:
                if shot_id not in shot_ids:
                    raise KeyError("Enabled Shot not found")
                shot_ids = {shot_id}
            packages = {package.shot_id: package for package in record.prompt_packages if not package.stale}
            missing = shot_ids - packages.keys()
            if missing:
                raise ValueError("Compile current Prompt Packages before generation")
            policy = budget_policy or default_budget_policy()
            job = GenerationJob(
                project_id=project_id, shot_plan_id=plan_id, mode="mock", provider="mock", model=model,
                budget_policy=policy, estimated_total_cost=0.0, reserved_cost=0.0, confirmed_at=utc_now(),
            )
            record.generation_jobs.append(job)
            for current_shot_id in sorted(shot_ids):
                package = packages[current_shot_id]
                previous = [item for item in record.generation_attempts if item.shot_id == current_shot_id]
                number = max((item.attempt_number for item in previous), default=0) + 1
                request_hash = stable_hash({"package": package.content_hash, "mode": "mock"})
                if any(
                    item.request_hash == request_hash and item.status in {GenerationStatus.RUNNING, GenerationStatus.SUCCEEDED}
                    for item in record.generation_attempts
                ):
                    raise ValueError("An identical request is already running or has succeeded")
                attempt = GenerationAttempt(
                    job_id=job.id, shot_id=current_shot_id, attempt_number=number,
                    request_hash=request_hash, prompt_package_id=package.id,
                    reference_asset_ids=package.product_reference_ids + package.detail_reference_ids + package.style_reference_ids,
                    estimated_cost=0.0, idempotency_key=stable_hash({"job": job.id, "shot": current_shot_id}),
                )
                record.generation_attempts.append(attempt)
            record.project.updated_at = utc_now()
            self.store.save(record)
            return job

    def run_generation_job(self, project_id: str, job_id: str, fail_shot_id: str | None = None) -> GenerationJob:
        """Claim persisted attempts one at a time; no automatic retry is performed."""
        provider = MockImageGenerationProvider()
        while True:
            with self.store.lock(project_id):
                record = self.store.load(project_id)
                job = self._generation_job(record, job_id)
                attempts = [item for item in record.generation_attempts if item.job_id == job_id]
                next_attempt = next((item for item in attempts if item.status == GenerationStatus.QUEUED), None)
                if next_attempt is None:
                    terminal = [item.status for item in attempts]
                    job.status = GenerationStatus.SUCCEEDED if all(state == GenerationStatus.SUCCEEDED for state in terminal) else GenerationStatus.FAILED
                    job.finished_at = utc_now()
                    job.actual_total_cost = 0.0
                    self.store.save(record)
                    return job
                next_attempt.status = GenerationStatus.RUNNING
                next_attempt.claimed_at = next_attempt.started_at = utc_now()
                job.status = GenerationStatus.RUNNING
                job.started_at = job.started_at or utc_now()
                self.store.save(record)
                shot = self._shot_by_id(record, next_attempt.shot_id)
                package = self._prompt_package(record, next_attempt.prompt_package_id)
            try:
                if fail_shot_id == next_attempt.shot_id:
                    raise RuntimeError("Simulated M2 mock failure")
                content = provider.generate(package, next_attempt, shot)
                with self.store.lock(project_id):
                    record = self.store.load(project_id)
                    attempt = self._attempt(record, next_attempt.id)
                    self._persist_candidate(record, project_id, shot, attempt, content)
                    attempt.provider_request_id = f"mock-{attempt.idempotency_key[:16]}"
                    attempt.actual_cost = 0.0
                    attempt.status = GenerationStatus.SUCCEEDED
                    attempt.finished_at = utc_now()
                    self.store.save(record)
            except (OSError, RuntimeError, ValueError) as exc:
                code, message = safe_error(exc)
                with self.store.lock(project_id):
                    record = self.store.load(project_id)
                    attempt = self._attempt(record, next_attempt.id)
                    attempt.status = GenerationStatus.FAILED
                    attempt.error_code = code
                    attempt.error_message_safe = message
                    attempt.finished_at = utc_now()
                    self.store.save(record)

    def recover_interrupted_jobs(self, project_id: str) -> int:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            changed = 0
            for attempt in record.generation_attempts:
                if attempt.status == GenerationStatus.RUNNING:
                    attempt.status = GenerationStatus.INTERRUPTED
                    attempt.finished_at = utc_now()
                    attempt.error_code = "interrupted"
                    attempt.error_message_safe = "Recovered after process interruption; no automatic resend."
                    changed += 1
            for job in record.generation_jobs:
                if job.status == GenerationStatus.RUNNING:
                    job.status = GenerationStatus.INTERRUPTED
                    job.finished_at = utc_now()
            if changed:
                self.store.save(record)
            return changed

    def accept_candidate(self, project_id: str, candidate_id: str) -> Candidate:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            candidate = self._candidate(record, candidate_id)
            for current in record.candidates:
                if current.shot_id == candidate.shot_id and current.status == CandidateStatus.ACCEPTED:
                    current.status = CandidateStatus.GENERATED
                    current.accepted_at = None
            candidate.status = CandidateStatus.ACCEPTED
            candidate.accepted_at = utc_now()
            candidate.rejected_at = candidate.rejection_reason = None
            self.store.save(record)
            return candidate

    def reject_candidate(self, project_id: str, candidate_id: str, reason: str) -> Candidate:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            candidate = self._candidate(record, candidate_id)
            if candidate.status == CandidateStatus.ACCEPTED:
                raise ValueError("Accept another Candidate before rejecting the current accepted Candidate")
            candidate.status = CandidateStatus.REJECTED
            candidate.rejected_at = utc_now()
            candidate.rejection_reason = self._required_text(reason, "Rejection reason")
            self.store.save(record)
            return candidate

    def resolve_candidate_path(self, project_id: str, candidate_id: str) -> Path:
        record = self.store.load(project_id)
        candidate = self._candidate(record, candidate_id)
        path = self._project_path(project_id, candidate.stored_path)
        if not path.is_file():
            raise FileNotFoundError("Candidate file not found")
        return path

    def _persist_candidate(self, record: StudioRecord, project_id: str, shot: ShotSpec, attempt: GenerationAttempt, content: bytes) -> Candidate:
        digest = hashlib.sha256(content).hexdigest()
        relative = f"generation/candidates/{attempt.id}/{new_id()}.png"
        path = self._project_path(project_id, relative)
        self._atomic_bytes(path, content)
        try:
            with Image.open(BytesIO(content)) as image:
                image.verify()
            with Image.open(BytesIO(content)) as image:
                width, height = image.size
            if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
                raise ValueError("Candidate dimensions exceed limits")
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
            raise ValueError("Provider result is not a valid safe image")
        candidate = Candidate(project_id=project_id, shot_id=shot.id, attempt_id=attempt.id, stored_path=relative, sha256=digest, width=width, height=height, mime_type="image/png")
        record.candidates.append(candidate)
        return candidate

    @staticmethod
    def _invalidate_spec(record: StudioRecord) -> None:
        record.product_spec = None
        StudioService._invalidate_generation(record)

    @staticmethod
    def _invalidate_generation(record: StudioRecord) -> None:
        for plan in record.shot_plans:
            if plan.status != PlanStatus.STALE:
                plan.status = PlanStatus.STALE
                plan.updated_at = utc_now()
        for package in record.prompt_packages:
            package.stale = True

    @staticmethod
    def _required_text(value: str, field_name: str) -> str:
        cleaned = value.strip()
        if not cleaned or len(cleaned) > 200:
            raise ValueError(f"{field_name} must contain between 1 and 200 characters")
        return cleaned

    @staticmethod
    def _unique_assets(assets: list[Asset]) -> list[Asset]:
        seen: set[str] = set()
        unique: list[Asset] = []
        for asset in assets:
            if asset.sha256 not in seen:
                seen.add(asset.sha256)
                unique.append(asset)
        return unique

    @staticmethod
    def _asset(record: StudioRecord, asset_id: str) -> Asset:
        asset = next((item for item in record.assets if item.id == asset_id), None)
        if asset is None:
            raise KeyError("Asset not found")
        return asset

    @staticmethod
    def _analysis(record: StudioRecord, asset_id: str) -> AssetAnalysis:
        analysis = next((item for item in record.analyses if item.asset_id == asset_id), None)
        if analysis is None:
            raise KeyError("Asset has not been analyzed")
        return analysis

    @staticmethod
    def _shot_plan(record: StudioRecord, plan_id: str) -> ShotPlan:
        plan = next((item for item in record.shot_plans if item.id == plan_id), None)
        if plan is None:
            raise KeyError("Shot Plan not found")
        return plan

    @staticmethod
    def _shot_by_id(record: StudioRecord, shot_id: str) -> ShotSpec:
        for plan in record.shot_plans:
            shot = next((item for item in plan.shots if item.id == shot_id), None)
            if shot is not None:
                return shot
        raise KeyError("Shot not found")

    @staticmethod
    def _prompt_package(record: StudioRecord, package_id: str) -> PromptPackage:
        package = next((item for item in record.prompt_packages if item.id == package_id), None)
        if package is None:
            raise KeyError("Prompt Package not found")
        return package

    @staticmethod
    def _generation_job(record: StudioRecord, job_id: str) -> GenerationJob:
        job = next((item for item in record.generation_jobs if item.id == job_id), None)
        if job is None:
            raise KeyError("Generation Job not found")
        return job

    @staticmethod
    def _attempt(record: StudioRecord, attempt_id: str) -> GenerationAttempt:
        attempt = next((item for item in record.generation_attempts if item.id == attempt_id), None)
        if attempt is None:
            raise KeyError("Generation Attempt not found")
        return attempt

    @staticmethod
    def _candidate(record: StudioRecord, candidate_id: str) -> Candidate:
        candidate = next((item for item in record.candidates if item.id == candidate_id), None)
        if candidate is None:
            raise KeyError("Candidate not found")
        return candidate
