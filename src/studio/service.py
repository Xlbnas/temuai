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
from src.studio.models import (
    Asset,
    AssetAnalysis,
    CanonicalProductSpec,
    ContentKind,
    DetailRegion,
    Importance,
    NormalizedBBox,
    ProductFact,
    ReferenceBundle,
    ReferenceItem,
    ReferenceRole,
    SourceKind,
    StudioPlatform,
    StudioProject,
    StudioRecord,
    StylePack,
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

    @staticmethod
    def _invalidate_spec(record: StudioRecord) -> None:
        record.product_spec = None

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
