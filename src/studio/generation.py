"""Deterministic M2 shot planning, prompting, and offline image generation.

This module intentionally does not adapt a paid API protocol.  The legacy
APIYI adapters are for the pre-Studio pipeline and are not a verified M2
contract, so M2 exposes a safe NotConfigured boundary until that contract is
supplied and reviewed.
"""
from __future__ import annotations

import hashlib
import json
import re
from io import BytesIO
from typing import Any

from PIL import Image, ImageDraw

from src.studio.models import (
    Asset,
    BudgetPolicy,
    ContentKind,
    GenerationAttempt,
    Importance,
    PromptPackage,
    ProviderCapability,
    ShotSpec,
    StudioPlatform,
    StudioRecord,
    StylePack,
)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def default_shots(platform: StudioPlatform, pack: StylePack) -> list[ShotSpec]:
    if platform == StudioPlatform.TEMU:
        definitions = [
            ("temu_hero", "Hero product", "Clear product-first catalog image", "full front"),
            ("temu_model_full_front", "Model full front", "Show fit from the front", "full body front"),
            ("temu_model_half_front", "Model half front detail", "Show upper product details", "half body front"),
            ("temu_model_full_back", "Model full back", "Verify back construction", "full body back"),
            ("temu_outdoor_commercial", "Commercial lifestyle", "Product-led outdoor context", "full product"),
        ]
    else:
        definitions = [
            ("tiktok_hook_cover", "Hook cover", "Product clear in the opening frame", "full front"),
            ("tiktok_lifestyle", "Lifestyle", "Everyday product use", "full product"),
            ("tiktok_motion", "Motion moment", "Natural movement while product stays clear", "full product"),
            ("tiktok_product_front", "Product front", "Clear front product view", "full front"),
            ("tiktok_product_back", "Product back", "Clear back product view", "full back"),
        ]
    width, height = ((1500, 2000) if pack.output_aspect_ratio == "3:4" else (1080, 1920))
    return [
        ShotSpec(
            shot_type=shot_type,
            title=title,
            purpose=purpose,
            aspect_ratio=pack.output_aspect_ratio,
            width=width,
            height=height,
            composition=f"{pack.composition}; {view}",
            subject_requirements=[view],
            required_fact_keys=["pocket", "zipper"] if "detail" in title.lower() else [],
            forbidden_elements=list(pack.forbidden_elements),
            sequence=index,
        )
        for index, (shot_type, title, purpose, view) in enumerate(definitions, 1)
    ]


def plan_hash(record: StudioRecord, shots: list[ShotSpec], pack: StylePack) -> str:
    return stable_hash(
        {
            "project": record.project.id,
            "platform": record.project.target_platform.value,
            "pack": {"id": pack.id, "version": pack.version},
            "spec": (record.product_spec.model_dump(mode="json") if record.product_spec else None),
            "shots": [shot.model_dump(mode="json") for shot in shots],
        }
    )


def blocking_reasons(record: StudioRecord) -> list[str]:
    if record.product_spec is None:
        return ["Canonical Product Spec must be compiled and confirmed before planning."]
    reasons: list[str] = []
    conflicts = [
        fact.key
        for fact in record.product_spec.facts
        if fact.status != "strong" and fact.priority in {Importance.CRITICAL, Importance.HIGH}
    ]
    if conflicts:
        reasons.append(f"Product Spec has unresolved facts: {', '.join(sorted(set(conflicts)))}.")
    analyses = {item.asset_id: item for item in record.analyses}
    views = {
        analyses[asset.id].content_kind.effective_value
        for asset in record.assets
        if asset.id in analyses
        and analyses[asset.id].source_kind.effective_value.value == "own_capture"
    }
    if ContentKind.PRODUCT_FULL_FRONT not in views:
        reasons.append("A clean own-capture full-front product reference is required.")
    if ContentKind.PRODUCT_FULL_BACK not in views:
        reasons.append("A clean own-capture full-back product reference is required.")
    return reasons


def select_references(
    record: StudioRecord, shot: ShotSpec, capability: ProviderCapability
) -> dict[str, list[str]]:
    """Select reproducibly by role; annotation previews are never provider input."""
    analyses = {item.asset_id: item for item in record.analyses}
    assets = {item.id: item for item in record.assets}
    own = [
        item for item in record.assets
        if item.id in analyses and analyses[item.id].source_kind.effective_value.value == "own_capture"
    ]
    def rank(asset_id: str) -> tuple[int, int, str]:
        asset = assets[asset_id]
        analysis = analyses[asset_id]
        kind = analysis.content_kind.effective_value
        wants_back = "back" in shot.shot_type
        primary = ContentKind.PRODUCT_FULL_BACK if wants_back else ContentKind.PRODUCT_FULL_FRONT
        return (0 if kind == primary else 1, -(asset.width * asset.height), asset.sha256)
    product = sorted(
        [item for item in own if analyses[item.id].content_kind.effective_value in {ContentKind.PRODUCT_FULL_FRONT, ContentKind.PRODUCT_FULL_BACK}],
        key=lambda item: rank(item.id),
    )
    required_keys = set(shot.required_fact_keys)
    details = sorted(
        [
            item
            for item in own
            if analyses[item.id].content_kind.effective_value == ContentKind.DETAIL
            and (
                not required_keys
                or any(
                    region.detail_type.effective_value in required_keys
                    for region in analyses[item.id].detail_regions
                )
            )
        ],
        key=lambda item: (-(item.width * item.height), item.sha256, item.id),
    )
    style = sorted(
        [item for item in record.assets if item.id in analyses and analyses[item.id].source_kind.effective_value.value == "competitor_reference"],
        key=lambda item: (item.sha256, item.id),
    )
    selected: dict[str, list[str]] = {"product": [], "detail": [], "style": [], "annotation": []}
    # Preserve a clean primary product reference first.  With more capacity,
    # a required detail and one style reference each receive a deliberate slot
    # before secondary product images are considered.
    candidates: dict[str, list[Asset]] = {"product": product, "detail": details, "style": style}
    dedupe: set[str] = set()

    def take(label: str) -> bool:
        for asset in candidates[label]:
            if asset.sha256 not in dedupe:
                dedupe.add(asset.sha256)
                selected[label].append(asset.id)
                return True
        return False

    selected["annotation"] = [
        asset.id for asset in own if asset.annotation_path and asset.id not in selected["product"]
    ]
    limit = capability.max_reference_images
    if limit > 0:
        take("product")
        # At capacity two, product plus a critical requested detail takes
        # precedence; otherwise reserve the second slot for style.
        if len(dedupe) < limit and (not details or limit >= 3):
            take("style")
        if len(dedupe) < limit:
            take("detail")
        if len(dedupe) < limit:
            take("style")
        while len(dedupe) < limit and (take("product") or take("detail") or take("style")):
            pass
    return selected


def compile_prompt(record: StudioRecord, shot: ShotSpec, pack: StylePack, capability: ProviderCapability) -> PromptPackage:
    references = select_references(record, shot, capability)
    assert record.product_spec is not None
    facts = [
        {"key": fact.key, "value": fact.value, "source": "user_confirmed" if fact.user_confirmed else "confirmed_own_capture"}
        for fact in record.product_spec.facts
        if fact.status == "strong"
    ]
    style_rules = [*pack.product_preservation_rules, *pack.forbidden_elements]
    negative = (
        "text, logo, watermark, size chart, parameters, official insignia, national symbols, "
        "status markings, conflict scenes, extra accessories, altered pockets, altered closures, "
        "altered cuffs, altered seams"
    )
    rendered = "\n".join(
        [
            "PRODUCT IDENTITY: Use the real product in clean product references as the source of truth.",
            "IMMUTABLE FACTS: " + ("; ".join(f"{f['key']}: {f['value']}" for f in facts) or "No confirmed facts supplied."),
            f"SHOT PURPOSE: {shot.purpose}",
            f"COMPOSITION: {shot.composition}. {shot.user_instruction}".strip(),
            f"PLATFORM STYLE: {pack.visual_tone}; lighting: {pack.lighting}; background: {pack.background}.",
            "REFERENCE ROLES: product references determine construction; style references only determine composition, scene, lighting and visual language.",
            "Do not copy competitor brand, text, watermark, person identity, or distinctive layout. Preserve all other areas when emphasizing a detail.",
            f"OUTPUT: {shot.width}x{shot.height}, {shot.aspect_ratio}; no text, logo, watermark, sizing or parameters.",
        ]
    )
    package_input = {
        "facts": facts, "shot": shot.model_dump(mode="json"), "pack": pack.model_dump(mode="json"),
        "references": references, "compiler": "m2a-1", "negative": negative,
    }
    return PromptPackage(
        project_id=record.project.id,
        shot_id=shot.id,
        rendered_prompt=rendered,
        negative_prompt=negative,
        structured_product_facts=facts,
        structured_style_rules=style_rules,
        structured_composition={"composition": shot.composition, "purpose": shot.purpose},
        product_reference_ids=references["product"], detail_reference_ids=references["detail"],
        style_reference_ids=references["style"], annotation_preview_ids=references["annotation"],
        content_hash=stable_hash(package_input),
    )


class NotConfiguredImageGenerationProvider:
    """Deliberate M2 boundary until an APIYI Studio request schema is verified."""
    name = "apiyi"

    def generate(self, *_: Any, **__: Any) -> None:
        raise RuntimeError("NotConfigured: APIYI Studio generation contract is not verified")


class MockImageGenerationProvider:
    name = "mock"

    def capability(self, model: str = "mock-image-v1") -> ProviderCapability:
        return ProviderCapability(
            provider=self.name, model=model, supports_image_references=True,
            supports_multiple_references=True, max_reference_images=4,
            supports_negative_prompt=True, supported_aspect_ratios=["3:4", "9:16"],
            pricing_version="mock-0", supported_output_sizes=["1500x2000", "1080x1920"],
        )

    def generate(self, package: PromptPackage, attempt: GenerationAttempt, shot: ShotSpec) -> bytes:
        digest = stable_hash({"prompt": package.content_hash, "attempt": attempt.idempotency_key})
        # Fixed content for a stable request idempotency key; clearly unusable as product media.
        image = Image.new("RGB", (shot.width, shot.height), color=(35, 42, 56))
        draw = ImageDraw.Draw(image)
        draw.rectangle((30, 30, shot.width - 30, shot.height - 30), outline=(255, 182, 40), width=12)
        draw.text((70, 80), "MOCK GENERATION\nNOT A PRODUCT IMAGE\n" + digest[:16], fill=(255, 220, 120))
        output = BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()


def safe_error(exc: Exception) -> tuple[str, str]:
    """Keep provider diagnostics useful without trying to sanitize every secret."""
    text = " ".join(str(exc).split())
    if re.search(
        r"(?i)(authorization|api[_-]?key|access[_-]?token|token|secret|password|bearer|"
        r"https?://[^\s/@]+:[^\s/@]+@|base64)",
        text,
    ):
        return ("provider_error", "Provider request failed; sensitive provider details were withheld.")
    return ("provider_error", text[:300] or "Provider request failed.")


def default_budget_policy() -> BudgetPolicy:
    return BudgetPolicy(project_limit=0.0, job_limit=0.0, shot_limit=0.0, pricing_version="mock-0")
