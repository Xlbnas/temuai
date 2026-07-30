"""Offline analyzer boundary; production calls are deliberately not wired."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path

from src.studio.models import (
    Asset,
    AssetAnalysis,
    ContentKind,
    DetailRegion,
    Importance,
    NormalizedBBox,
    OverrideValue,
    SourceKind,
)


class AnalyzerNotConfigured(RuntimeError):
    pass


class AssetAnalyzer(ABC):
    name: str
    version: str

    @abstractmethod
    def analyze(self, image_path: Path, asset: Asset, schema_version: str) -> AssetAnalysis: ...


class MockAssetAnalyzer(AssetAnalyzer):
    """Deterministic test/demo analyzer, optionally keyed by image SHA-256."""

    name = "mock"
    version = "1.0.0"

    def __init__(self, configured: dict[str, AssetAnalysis] | None = None) -> None:
        self.configured = configured or {}

    def analyze(self, image_path: Path, asset: Asset, schema_version: str) -> AssetAnalysis:
        if asset.sha256 in self.configured:
            result = self.configured[asset.sha256].model_copy(deep=True)
            result.asset_id = asset.id
            result.source_image_sha256 = asset.sha256
            return result
        name = asset.original_filename.lower()
        content = (
            ContentKind.PRODUCT_FULL_FRONT
            if "front" in name
            else ContentKind.DETAIL
            if "detail" in name
            else ContentKind.UNKNOWN
        )
        regions: list[DetailRegion] = []
        if content == ContentKind.DETAIL:
            regions.append(
                DetailRegion(
                    asset_id=asset.id,
                    detail_type=OverrideValue(model_value="construction_detail"),
                    importance=OverrideValue(model_value=Importance.HIGH),
                    label=OverrideValue(model_value="Product detail"),
                    normalized_bbox=NormalizedBBox(x=0.2, y=0.2, width=0.5, height=0.5),
                    confidence=0.85,
                )
            )
        return AssetAnalysis(
            asset_id=asset.id,
            source_kind=OverrideValue(model_value=SourceKind.OWN_CAPTURE),
            content_kind=OverrideValue(model_value=content),
            detail_types=OverrideValue(
                model_value=[r.detail_type.effective_value for r in regions]
            ),
            detail_regions=regions,
            confidence=0.75,
            reason="Deterministic offline mock result",
            visual_facts=["Imported asset requires human confirmation"],
            analyzer_name=self.name,
            analyzer_version=self.version,
            schema_version=schema_version,
            source_image_sha256=asset.sha256,
        )


class NotConfiguredAssetAnalyzer(AssetAnalyzer):
    """Production integration point until a verified vision client is available."""

    name = "not_configured"
    version = "0"

    def analyze(self, image_path: Path, asset: Asset, schema_version: str) -> AssetAnalysis:
        raise AnalyzerNotConfigured(
            "No verified production vision analyzer is configured; use MockAssetAnalyzer for M1."
        )
