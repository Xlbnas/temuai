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
from src.core.ledger import CostLedger
from src.studio.analyzers import AssetAnalyzer, NotConfiguredAssetAnalyzer
from src.studio.apiyi import (
    APIYIGenerationRequest,
    APIYIImageGenerationProvider,
    APIYIProviderError,
    APIYIProviderErrorCode,
    APIYIReference,
    _model_config,
    load_pricing_contract,
    safe_provider_error,
)
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
        self.ledger = CostLedger(config.logs_dir)

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

    def apiyi_capability(self, model: str) -> ProviderCapability:
        """Expose configured capability without reading or displaying a secret."""
        return APIYIImageGenerationProvider.capability_for(self.config, model)

    def provider_status(self, model: str) -> dict[str, object]:
        capability = self.apiyi_capability(model)
        configured = bool(os.getenv("APIYI_API_KEY"))
        live_enabled = self.config.safe_env("LIVE_GENERATION_ENABLED", "false").lower() == "true"
        pricing_known = capability.pricing_status != "unknown" and capability.estimated_price_usd is not None
        # "ready" must mean the live gate would actually accept a request; a
        # configured key with unknown pricing or disabled Live is locked.
        if not configured:
            status = "not_configured"
        elif live_enabled and pricing_known:
            status = "ready"
        else:
            status = "locked"
        return {
            "provider": "apiyi",
            "model": capability.model,
            "status": status,
            "live_enabled": live_enabled,
            "capability": capability.model_dump(mode="json"),
        }

    def cost_preview(self, project_id: str, plan_id: str, provider: str, model: str, shot_id: str) -> dict[str, object]:
        record = self.store.load(project_id)
        plan = self._shot_plan(record, plan_id)
        shot = self._shot_by_id(record, shot_id)
        if shot not in plan.shots or not shot.enabled:
            raise KeyError("Enabled Shot not found")
        if provider != "apiyi":
            raise ValueError("Only apiyi has a Studio live cost preview")
        capability = self.apiyi_capability(model)
        preview: dict[str, object] = {
            "provider": provider, "model": capability.model, "shot_id": shot_id,
            "output_size": f"{shot.width}x{shot.height}", "aspect_ratio": shot.aspect_ratio,
            "quantity": 1,
            "pricing_status": capability.pricing_status,
            "pricing_version": capability.pricing_version,
            "pricing_source": capability.pricing_source,
            "estimated_cost": capability.estimated_price_usd if capability.pricing_status != "unknown" else None,
            "note": "Preview only — no provider call is made.",
        }
        if capability.pricing_status != "exact":
            # Never dress an unknown price up as a precise number.
            preview["estimated_cost"] = None
            preview["display"] = "Pricing unavailable / Live locked"
            return preview
        contract = load_pricing_contract(self.config, _model_config(self.config, model))
        if contract is not None:
            preview.update(
                {
                    "provider_model_id": contract.provider_model_id,
                    "unit": contract.unit,
                    "unit_price": contract.amount,
                    "currency": contract.currency,
                    "estimated_total": contract.amount,
                    "effective_at": contract.effective_at,
                    "request_mode": contract.request_mode,
                    "reference_price_policy": contract.reference_policy,
                    "hard_max_recommendation": round(contract.amount * 1.1, 4) if contract.amount else None,
                    "pricing_digest": contract.evidence_digest,
                }
            )
        return preview

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
            if {shot.id for shot in shots} != {shot.id for shot in plan.shots}:
                raise ValueError("Shot Plan updates must not add or remove shots")
            sequences = [shot.sequence for shot in shots]
            if sorted(sequences) != list(range(1, len(shots) + 1)):
                raise ValueError("Shot sequences must be unique and contiguous")
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

    def update_single_shot(
        self,
        project_id: str,
        plan_id: str,
        shot_id: str,
        *,
        sequence: int,
        composition: str,
        user_instruction: str,
        enabled: bool,
    ) -> ShotPlan:
        """Atomically edit one draft shot without replacing a stale shot list."""
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            plan = self._shot_plan(record, plan_id)
            if plan.status == PlanStatus.CONFIRMED:
                raise ValueError("A confirmed Shot Plan cannot be edited; compile a replacement")
            if not 1 <= sequence <= len(plan.shots):
                raise ValueError("Shot sequence is outside the plan range")
            shot = next((item for item in plan.shots if item.id == shot_id), None)
            if shot is None:
                raise KeyError("Shot not found")
            shot.composition = self._required_text(composition, "Composition")
            shot.user_instruction = self._optional_text(user_instruction, "Instruction")
            shot.enabled = enabled
            ordered = sorted((item for item in plan.shots if item.id != shot_id), key=lambda item: item.sequence)
            ordered.insert(sequence - 1, shot)
            for index, item in enumerate(ordered, 1):
                item.sequence = index
            plan.shots = ordered
            self._refresh_plan_after_edit(record, plan)
            self.store.save(record)
            return plan

    def _refresh_plan_after_edit(self, record: StudioRecord, plan: ShotPlan) -> None:
        pack = record.project.selected_style_pack
        if pack is None:
            raise ValueError("Style Pack is missing")
        plan.blocking_reasons = blocking_reasons(record)
        plan.status = PlanStatus.BLOCKED if plan.blocking_reasons else PlanStatus.DRAFT
        plan.content_hash = plan_hash(record, plan.shots, pack)
        plan.updated_at = utc_now()
        shot_ids = {shot.id for shot in plan.shots}
        for package in record.prompt_packages:
            if package.shot_id in shot_ids:
                package.stale = True
        record.project.updated_at = utc_now()

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
        manual_regeneration: bool = False,
        confirmed_by: str | None = None,
        generation_nonce: str | None = None,
    ) -> GenerationJob:
        capability: ProviderCapability | None = None
        if mode != "mock":
            if mode != "live":
                raise ValueError("Generation mode must be mock or live")
            # LIVE_GENERATION_ENABLED deliberately defaults false even when a key exists.
            live_policy = budget_policy or default_budget_policy()
            if live_policy.job_limit is None or live_policy.job_limit <= 0:
                raise ValueError("Live generation requires a positive max cost")
            if self.config.safe_env("LIVE_GENERATION_ENABLED", "false").lower() != "true":
                raise ValueError("Live generation is disabled by LIVE_GENERATION_ENABLED=false")
            if provider != "apiyi" or not paid_confirmation:
                raise ValueError("Live generation requires verified provider and explicit paid confirmation")
            if not os.getenv("APIYI_API_KEY"):
                raise ValueError("NotConfigured: APIYI is not configured")
            if not confirmed_by:
                raise ValueError("Live generation requires an explicit user confirmation")
            capability = self.apiyi_capability(model)
            if capability.pricing_status == "unknown" or capability.estimated_price_usd is None:
                raise ValueError("pricing_unknown: live generation requires a verified versioned price")
            if shot_id is None:
                raise ValueError("Live generation requires exactly one --shot-id")
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            plan = self._shot_plan(record, plan_id)
            if plan.status != PlanStatus.CONFIRMED:
                raise ValueError("Confirm the Shot Plan before generation")
            selected_shots = [shot for shot in sorted(plan.shots, key=lambda item: item.sequence) if shot.enabled]
            if shot_id is not None:
                selected_shots = [shot for shot in selected_shots if shot.id == shot_id]
                if not selected_shots:
                    raise KeyError("Enabled Shot not found")
            if not selected_shots:
                raise ValueError("Enable at least one Shot before generation")
            if mode == "live" and len(selected_shots) != 1:
                raise ValueError("Live generation requires exactly one enabled shot")
            shot_ids = {shot.id for shot in selected_shots}
            packages = {package.shot_id: package for package in record.prompt_packages if not package.stale}
            missing = shot_ids - packages.keys()
            if missing:
                raise ValueError("Compile current Prompt Packages before generation")
            if manual_regeneration and not confirmed_by:
                raise ValueError("Manual regeneration requires an explicit confirmed_by value")
            intent = "manual_regeneration" if manual_regeneration else "initial"
            nonce = generation_nonce or (new_id() if manual_regeneration else None)
            policy = budget_policy or default_budget_policy()
            unit_cost = 0.0 if mode == "mock" else capability.estimated_price_usd if capability else None
            if unit_cost is None:
                raise ValueError("pricing_unknown: live generation requires a verified versioned price")
            estimated_total = unit_cost * len(selected_shots)
            if mode == "live":
                if policy.shot_limit is not None and unit_cost > policy.shot_limit:
                    raise ValueError("budget_rejected: shot budget would be exceeded")
                if policy.job_limit is not None and estimated_total > policy.job_limit:
                    raise ValueError("budget_rejected: job budget would be exceeded")
                previous_reserved = sum(
                    item.reserved_cost or 0.0 for item in record.generation_jobs
                    if item.mode == "live" and item.status in {
                        GenerationStatus.QUEUED, GenerationStatus.SUBMITTING,
                        GenerationStatus.RUNNING, GenerationStatus.PROVIDER_PENDING,
                        GenerationStatus.DOWNLOADING, GenerationStatus.UNKNOWN,
                        GenerationStatus.RECONCILE_REQUIRED,
                    }
                )
                if policy.project_limit is not None and previous_reserved + estimated_total > policy.project_limit:
                    raise ValueError("budget_rejected: project budget would be exceeded")
            job = GenerationJob(
                project_id=project_id, shot_plan_id=plan_id, mode=mode, provider=provider, model=model,
                budget_policy=policy, estimated_total_cost=estimated_total, reserved_cost=estimated_total, confirmed_at=utc_now(),
                generation_intent=intent, confirmed_by=confirmed_by,
                pricing_version=capability.pricing_version if capability else None,
                pricing_digest=capability.pricing_digest if capability else None,
            )
            for shot in selected_shots:
                current_shot_id = shot.id
                package = packages[current_shot_id]
                previous = [item for item in record.generation_attempts if item.shot_id == current_shot_id]
                number = max((item.attempt_number for item in previous), default=0) + 1
                request_hash = self._request_hash(
                    record, shot, package, mode, provider, model, nonce,
                    pricing_version=capability.pricing_version if capability else None,
                    pricing_digest=capability.pricing_digest if capability else None,
                )
                same_request = [item for item in record.generation_attempts if item.request_hash == request_hash]
                active_statuses = {
                    GenerationStatus.QUEUED, GenerationStatus.SUBMITTING, GenerationStatus.RUNNING,
                    GenerationStatus.PROVIDER_PENDING, GenerationStatus.DOWNLOADING,
                }
                if any(item.status in active_statuses for item in same_request):
                    raise ValueError("An identical request is already queued, running, or has succeeded")
                if not manual_regeneration and any(
                    item.status == GenerationStatus.SUCCEEDED for item in same_request
                ):
                    raise ValueError("An identical request is already queued, running, or has succeeded")
                if any(item.status in {GenerationStatus.UNKNOWN, GenerationStatus.RECONCILE_REQUIRED} for item in same_request):
                    # An uncertain paid outcome must be reconciled by a human before
                    # the identical request may ever be created again.
                    raise ValueError("An identical request has an uncertain outcome and must be reconciled before resubmission")
                attempt = GenerationAttempt(
                    job_id=job.id, shot_id=current_shot_id, attempt_number=number,
                    request_hash=request_hash, prompt_package_id=package.id,
                    reference_asset_ids=package.product_reference_ids + package.detail_reference_ids + package.style_reference_ids,
                    estimated_cost=unit_cost, idempotency_key=request_hash,
                    generation_intent=intent, generation_nonce=nonce, confirmed_by=confirmed_by,
                    pricing_version=capability.pricing_version if capability else None,
                    pricing_digest=capability.pricing_digest if capability else None,
                    unit_price_usd=unit_cost if mode == "live" else None,
                )
                attempt.reference_manifest = self._reference_manifest(record, package)
                record.generation_attempts.append(attempt)
            record.generation_jobs.append(job)
            record.project.updated_at = utc_now()
            self.store.save(record)
            return job

    def run_generation_job(self, project_id: str, job_id: str, fail_shot_id: str | None = None) -> GenerationJob:
        """Claim persisted attempts one at a time; no automatic retry is performed."""
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            existing_job = self._generation_job(record, job_id)
            live_job = existing_job.mode == "live"
        if live_job:
            return self.run_apiyi_generation_job(project_id, job_id)
        provider = MockImageGenerationProvider()
        while True:
            with self.store.lock(project_id):
                record = self.store.load(project_id)
                job = self._generation_job(record, job_id)
                attempts = [item for item in record.generation_attempts if item.job_id == job_id]
                next_attempt = next((item for item in attempts if item.status == GenerationStatus.QUEUED), None)
                if next_attempt is None:
                    self._refresh_job_status(job, attempts)
                    self.store.save(record)
                    return job
                plan = self._shot_plan(record, job.shot_plan_id)
                package = self._prompt_package(record, next_attempt.prompt_package_id)
                if plan.status != PlanStatus.CONFIRMED or package.stale:
                    next_attempt.status = GenerationStatus.FAILED
                    next_attempt.error_code = "stale_prompt"
                    next_attempt.error_message_safe = "Prompt or Shot Plan became stale before dispatch."
                    next_attempt.finished_at = utc_now()
                    self.store.save(record)
                    continue
                next_attempt.status = GenerationStatus.RUNNING
                next_attempt.claimed_at = next_attempt.started_at = utc_now()
                job.status = GenerationStatus.RUNNING
                job.started_at = job.started_at or utc_now()
                self.store.save(record)
                shot = self._shot_by_id(record, next_attempt.shot_id)
            try:
                if fail_shot_id == next_attempt.shot_id:
                    raise RuntimeError("Simulated M2 mock failure")
                content = provider.generate(package, next_attempt, shot)
                with self.store.lock(project_id):
                    record = self.store.load(project_id)
                    attempt = self._attempt(record, next_attempt.id)
                    if attempt.status != GenerationStatus.RUNNING:
                        continue
                    candidate = self._persist_candidate(record, project_id, shot, attempt, content)
                    attempt.provider_request_id = f"mock-{attempt.idempotency_key[:16]}"
                    attempt.actual_cost = 0.0
                    attempt.status = GenerationStatus.SUCCEEDED
                    attempt.finished_at = utc_now()
                    try:
                        self.store.save(record)
                    except BaseException:
                        record.candidates = [item for item in record.candidates if item.id != candidate.id]
                        self._project_path(project_id, candidate.stored_path).unlink(missing_ok=True)
                        raise
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

    def run_apiyi_generation_job(self, project_id: str, job_id: str) -> GenerationJob:
        """Submit one persisted live attempt once; timeout paths never resend it."""
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            job = self._generation_job(record, job_id)
            if job.mode != "live" or job.provider != "apiyi":
                raise ValueError("Job is not an APIYI live generation job")
            attempt = next((item for item in record.generation_attempts if item.job_id == job_id and item.status == GenerationStatus.QUEUED), None)
            if attempt is None:
                self._refresh_job_status(job, [item for item in record.generation_attempts if item.job_id == job_id])
                self.store.save(record)
                return job
            plan = self._shot_plan(record, job.shot_plan_id)
            package = self._prompt_package(record, attempt.prompt_package_id)
            if plan.status != PlanStatus.CONFIRMED or package.stale:
                attempt.status = GenerationStatus.FAILED
                attempt.error_code = "stale_prompt"
                attempt.error_message_safe = "Prompt or Shot Plan became stale before dispatch."
                attempt.finished_at = utc_now()
                self._refresh_job_status(job, [item for item in record.generation_attempts if item.job_id == job_id])
                self.store.save(record)
                return job
            shot = self._shot_by_id(record, attempt.shot_id)
            request = self._apiyi_request(record, package, shot, attempt, job.model)
            attempt.status = GenerationStatus.SUBMITTING
            attempt.claimed_at = attempt.started_at = attempt.submitted_at = utc_now()
            job.status = GenerationStatus.SUBMITTING
            job.started_at = job.started_at or utc_now()
            self.store.save(record)
        submitted = False
        try:
            adapter = APIYIImageGenerationProvider(self.config, job.model, os.getenv("APIYI_API_KEY", ""))
            submission = adapter.submit(request)
            # From here on the provider has done paid work: any failure is an
            # uncertain outcome, never a safely retryable FAILED.
            submitted = True
            # A complete success without a provider request ID is still a
            # success; neither verified sync contract guarantees a response ID.
            if len(submission.results) != 1:
                raise APIYIProviderError(
                    APIYIProviderErrorCode.MALFORMED_RESPONSE,
                    "Single-shot Studio request returned an unexpected number of results.",
                )
            with self.store.lock(project_id):
                record = self.store.load(project_id)
                stored = self._attempt(record, attempt.id)
                stored.provider_request_id = submission.provider_request_id
                stored.status = GenerationStatus.DOWNLOADING
                self._generation_job(record, job_id).status = GenerationStatus.DOWNLOADING
                self.store.save(record)
            content = adapter.client.download_result(submission.results[0])
            with self.store.lock(project_id):
                record = self.store.load(project_id)
                stored = self._attempt(record, attempt.id)
                candidate = self._persist_candidate(record, project_id, shot, stored, content)
                stored.actual_cost = submission.actual_cost_usd
                stored.status = GenerationStatus.SUCCEEDED
                stored.finished_at = utc_now()
                current_job = self._generation_job(record, job_id)
                self._refresh_job_status(current_job, [item for item in record.generation_attempts if item.job_id == job_id])
                try:
                    self.store.save(record)
                except BaseException:
                    record.candidates = [item for item in record.candidates if item.id != candidate.id]
                    self._project_path(project_id, candidate.stored_path).unlink(missing_ok=True)
                    raise
                # Ledger is appended only after the result is durably settled.
                self._record_live_ledger(current_job, stored, shot, "succeeded")
                return current_job
        except (OSError, RuntimeError, ValueError) as exc:
            code, message = safe_provider_error(exc)
            with self.store.lock(project_id):
                record = self.store.load(project_id)
                stored = self._attempt(record, attempt.id)
                current_job = self._generation_job(record, job_id)
                if stored.status == GenerationStatus.SUCCEEDED:
                    # The result and metadata were already durably saved; only the
                    # trailing ledger append failed.  Never downgrade a settled
                    # success — surface the original failure instead.
                    raise
                # Once the request may have left the process, neither recovery nor a
                # worker may resend it.  malformed_response/unsafe_result are only
                # raised after the provider answered, and ``submitted`` covers every
                # later local failure (download, candidate validation, metadata save).
                if submitted or code in {
                    APIYIProviderErrorCode.TIMEOUT_AFTER_SUBMISSION.value,
                    APIYIProviderErrorCode.RECONCILIATION_REQUIRED.value,
                    APIYIProviderErrorCode.MALFORMED_RESPONSE.value,
                    APIYIProviderErrorCode.UNSAFE_RESULT.value,
                }:
                    stored.status = GenerationStatus.RECONCILE_REQUIRED
                    stored.reconciliation_note = message
                else:
                    stored.status = GenerationStatus.FAILED
                stored.error_code = code
                stored.error_message_safe = message
                stored.finished_at = utc_now()
                self._refresh_job_status(current_job, [item for item in record.generation_attempts if item.job_id == job_id])
                self._record_live_ledger(current_job, stored, shot, stored.status.value)
                self.store.save(record)
                return current_job

    def reconcile_attempt(self, project_id: str, attempt_id: str) -> GenerationAttempt:
        """Safe reconciliation boundary.  No verified sync endpoint means no blind resend."""
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            attempt = self._attempt(record, attempt_id)
            reconcilable = {
                GenerationStatus.QUEUED, GenerationStatus.SUBMITTING, GenerationStatus.RUNNING,
                GenerationStatus.PROVIDER_PENDING, GenerationStatus.DOWNLOADING,
                GenerationStatus.UNKNOWN, GenerationStatus.RECONCILE_REQUIRED,
            }
            if attempt.status not in reconcilable:
                raise ValueError("Only an unresolved attempt can be reconciled")
            if attempt.status == GenerationStatus.QUEUED and attempt.submitted_at is None:
                # Never dispatched: nothing reached the provider, so this is a safe,
                # final failure rather than an uncertain paid outcome.
                attempt.status = GenerationStatus.FAILED
                attempt.error_code = "never_submitted"
                attempt.error_message_safe = "Attempt was never dispatched to the provider; a new confirmed job is safe."
                attempt.reconciliation_note = None
            else:
                attempt.status = GenerationStatus.RECONCILE_REQUIRED
                if attempt.provider_request_id:
                    attempt.reconciliation_note = "Provider request ID retained; no verified APIYI status endpoint is available."
                else:
                    attempt.reconciliation_note = "Submission outcome is unknown and no provider request ID was retained."
                attempt.error_code = "reconciliation_required"
                attempt.error_message_safe = attempt.reconciliation_note
            attempt.finished_at = attempt.finished_at or utc_now()
            job = self._generation_job(record, attempt.job_id)
            self._refresh_job_status(job, [item for item in record.generation_attempts if item.job_id == job.id])
            self.store.save(record)
            return attempt

    def reconcile_job(self, project_id: str, job_id: str) -> list[GenerationAttempt]:
        record = self.store.load(project_id)
        return [self.reconcile_attempt(project_id, item.id) for item in record.generation_attempts if item.job_id == job_id and item.status in {GenerationStatus.QUEUED, GenerationStatus.UNKNOWN, GenerationStatus.RECONCILE_REQUIRED, GenerationStatus.SUBMITTING, GenerationStatus.RUNNING, GenerationStatus.PROVIDER_PENDING, GenerationStatus.DOWNLOADING}]

    def recover_interrupted_jobs(self, project_id: str) -> int:
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            changed = 0
            for attempt in record.generation_attempts:
                if attempt.status in {GenerationStatus.SUBMITTING, GenerationStatus.RUNNING, GenerationStatus.DOWNLOADING}:
                    # A live request may already have reached APIYI; never turn it
                    # into retryable work after a process restart.
                    attempt.status = (
                        GenerationStatus.RECONCILE_REQUIRED
                        if self._generation_job(record, attempt.job_id).mode == "live"
                        else GenerationStatus.INTERRUPTED
                    )
                    attempt.finished_at = utc_now()
                    attempt.error_code = "reconciliation_required" if attempt.status == GenerationStatus.RECONCILE_REQUIRED else "interrupted"
                    attempt.error_message_safe = "Recovered after process interruption; no automatic resend."
                    changed += 1
            for job in record.generation_jobs:
                attempts = [item for item in record.generation_attempts if item.job_id == job.id]
                if any(item.status == GenerationStatus.QUEUED for item in attempts) and job.mode == "mock":
                    job.status = GenerationStatus.QUEUED
                    job.finished_at = None
                elif job.status == GenerationStatus.RUNNING or changed:
                    self._refresh_job_status(job, attempts)
            if changed or any(job.status == GenerationStatus.QUEUED for job in record.generation_jobs):
                self.store.save(record)
            return changed

    def recover_pending_mock_jobs(self) -> list[tuple[str, str]]:
        """Startup recovery: only durable Mock QUEUED work may be rescheduled."""
        pending: list[tuple[str, str]] = []
        for project_id in self.store.project_ids():
            self.recover_interrupted_jobs(project_id)
            with self.store.lock(project_id):
                record = self.store.load(project_id)
                for job in record.generation_jobs:
                    if job.mode == "mock" and any(
                        attempt.job_id == job.id and attempt.status == GenerationStatus.QUEUED
                        for attempt in record.generation_attempts
                    ):
                        pending.append((project_id, job.id))
        return pending

    def resume_generation_job(self, project_id: str, job_id: str) -> GenerationJob:
        """Explicit recovery seam; Live attempts require future provider reconciliation."""
        with self.store.lock(project_id):
            record = self.store.load(project_id)
            job = self._generation_job(record, job_id)
            if job.mode != "mock":
                raise ValueError("Live generation requires explicit provider reconciliation and is not resumable")
        return self.run_generation_job(project_id, job_id)

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

    def _persist_candidate(
        self,
        record: StudioRecord,
        project_id: str,
        shot: ShotSpec,
        attempt: GenerationAttempt,
        content: bytes,
    ) -> Candidate:
        """Decode provider bytes before storage; never trust a supplied MIME or filename."""
        if any(candidate.attempt_id == attempt.id for candidate in record.candidates):
            raise ValueError("A successful attempt may persist only one Candidate")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(BytesIO(content)) as image:
                    image_format = image.format
                    if getattr(image, "n_frames", 1) != 1:
                        raise ValueError("Animated or multi-frame candidate images are not supported")
                    image.verify()
                with Image.open(BytesIO(content)) as image:
                    normalized = ImageOps.exif_transpose(image).convert("RGB")
                    width, height = normalized.size
        except (
            Image.DecompressionBombError,
            Image.DecompressionBombWarning,
            OSError,
            ValueError,
        ) as exc:
            raise ValueError("Provider result is not a valid safe image") from exc
        if image_format not in ALLOWED_FORMATS:
            raise ValueError("Provider result has an unsupported image format")
        if width > MAX_IMAGE_DIMENSION or height > MAX_IMAGE_DIMENSION or width * height > MAX_IMAGE_PIXELS:
            raise ValueError("Candidate dimensions exceed limits")
        mime_type, extension = ALLOWED_FORMATS[image_format]
        # Persist only a freshly encoded bitmap.  This removes source EXIF,
        # ICC and trailing bytes accepted by decoders while retaining the
        # verified image format recorded in the Candidate metadata.
        output = BytesIO()
        normalized.save(output, format=image_format)
        canonical_content = output.getvalue()
        digest = hashlib.sha256(canonical_content).hexdigest()
        relative = f"generation/candidates/{attempt.id}/{new_id()}{extension}"
        path = self._project_path(project_id, relative)
        self._atomic_bytes(path, canonical_content)
        candidate = Candidate(
            project_id=project_id,
            shot_id=shot.id,
            attempt_id=attempt.id,
            stored_path=relative,
            sha256=digest,
            width=width,
            height=height,
            mime_type=mime_type,
        )
        record.candidates.append(candidate)
        return candidate

    @staticmethod
    def _refresh_job_status(job: GenerationJob, attempts: list[GenerationAttempt]) -> None:
        statuses = [attempt.status for attempt in attempts]
        if any(status in {GenerationStatus.SUBMITTING, GenerationStatus.RUNNING, GenerationStatus.DOWNLOADING} for status in statuses):
            job.status = GenerationStatus.RUNNING
            job.finished_at = None
            return
        if any(status == GenerationStatus.PROVIDER_PENDING for status in statuses):
            job.status = GenerationStatus.PROVIDER_PENDING
            job.finished_at = None
            return
        if any(status == GenerationStatus.QUEUED for status in statuses):
            job.status = GenerationStatus.QUEUED
            job.finished_at = None
            return
        if statuses and all(status == GenerationStatus.SUCCEEDED for status in statuses):
            job.status = GenerationStatus.SUCCEEDED
        elif GenerationStatus.RECONCILE_REQUIRED in statuses or GenerationStatus.UNKNOWN in statuses:
            job.status = GenerationStatus.RECONCILE_REQUIRED
        elif GenerationStatus.INTERRUPTED in statuses:
            job.status = GenerationStatus.INTERRUPTED
        else:
            job.status = GenerationStatus.FAILED
        job.finished_at = utc_now()
        costs = [attempt.actual_cost for attempt in attempts]
        job.actual_total_cost = sum(cost for cost in costs if cost is not None) if any(cost is not None for cost in costs) else None

    @staticmethod
    def _request_hash(
        record: StudioRecord,
        shot: ShotSpec,
        package: PromptPackage,
        mode: str,
        provider: str,
        model: str,
        generation_nonce: str | None,
        pricing_version: str | None = None,
        pricing_digest: str | None = None,
    ) -> str:
        assets = {asset.id: asset for asset in record.assets}
        references: list[dict[str, str]] = []
        for role, asset_ids in (
            ("product", package.product_reference_ids),
            ("detail", package.detail_reference_ids),
            ("style", package.style_reference_ids),
        ):
            references.extend(
                {"role": role, "sha256": assets[asset_id].sha256}
                for asset_id in asset_ids
                if asset_id in assets
            )
        payload: dict[str, object] = {
            "prompt_package": package.content_hash,
            "provider": provider,
            "model": model,
            "mode": mode,
            "output": {"width": shot.width, "height": shot.height, "aspect_ratio": shot.aspect_ratio},
            "references": references,
            "generation_nonce": generation_nonce,
        }
        if pricing_version is not None:
            # The verified pricing contract version/digest is part of the paid
            # request identity; mock hashes (no contract) stay unchanged.
            payload["pricing"] = {"version": pricing_version, "digest": pricing_digest}
        return stable_hash(payload)

    def _reference_manifest(self, record: StudioRecord, package: PromptPackage) -> list[dict[str, str]]:
        assets = {asset.id: asset for asset in record.assets}
        manifest: list[dict[str, str]] = []
        for role, asset_ids in (
            ("product_reference_clean", package.product_reference_ids),
            ("detail_reference_clean", package.detail_reference_ids),
            ("style_reference", package.style_reference_ids),
        ):
            for asset_id in asset_ids:
                asset = assets.get(asset_id)
                if asset:
                    manifest.append({"role": role, "asset_id": asset.id, "sha256": asset.sha256})
        return manifest

    def _apiyi_request(
        self, record: StudioRecord, package: PromptPackage, shot: ShotSpec, attempt: GenerationAttempt, model: str
    ) -> APIYIGenerationRequest:
        assets = {asset.id: asset for asset in record.assets}
        references: list[APIYIReference] = []
        for role, asset_ids in (
            ("product_reference_clean", package.product_reference_ids),
            ("detail_reference_clean", package.detail_reference_ids),
            ("style_reference", package.style_reference_ids),
        ):
            for asset_id in asset_ids:
                asset = assets.get(asset_id)
                if asset is None:
                    raise ValueError("Referenced asset is missing")
                references.append(APIYIReference(
                    role=role, asset_id=asset.id, sha256=asset.sha256, mime_type=asset.mime_type,
                    path=self._project_path(record.project.id, asset.stored_path),
                ))
        return APIYIGenerationRequest(
            model=model, prompt=package.rendered_prompt, negative_prompt=package.negative_prompt,
            references=references, width=shot.width, height=shot.height,
            aspect_ratio=shot.aspect_ratio, idempotency_key=attempt.idempotency_key,
        )

    def _record_live_ledger(
        self, job: GenerationJob, attempt: GenerationAttempt, shot: ShotSpec, status_value: str
    ) -> None:
        """Append only safe identifiers and SHA values; never paths or provider URLs."""
        self.ledger.record_call(
            sku=f"studio-{job.project_id[:12]}", platform="studio", task=job.id,
            provider=job.provider, model=job.model, request_id=attempt.provider_request_id,
            attempt=attempt.attempt_number, input_images=[item["sha256"] for item in attempt.reference_manifest],
            requested_size=f"{shot.width}x{shot.height}", aspect_ratio=shot.aspect_ratio,
            estimated_cost_usd=attempt.estimated_cost or 0.0, actual_cost_usd=attempt.actual_cost,
            status=status_value, accepted=False, error=attempt.error_code, duration_seconds=0.0,
            pricing_version=attempt.pricing_version,
        )

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
    def _optional_text(value: str, field_name: str) -> str:
        cleaned = value.strip()
        if len(cleaned) > 200:
            raise ValueError(f"{field_name} must contain at most 200 characters")
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
