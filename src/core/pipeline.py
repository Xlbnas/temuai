"""Main build/generate pipeline orchestrator."""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

from src.core.config import AppConfig
from src.core.costing import CostGuard
from src.core.ledger import CostLedger
from src.core.manifest import ManifestManager
from src.core.models import (
    BuildManifest,
    CandidateInfo,
    ModelConfig,
    PlatformConfig,
    PlatformTask,
    ProductInfo,
    TaskManifest,
    TaskStatus,
    TaskType,
)
from src.core.provider import EditRequest, GenerateRequest, ImageProvider
from src.core.routing import TaskRouter
from src.layouts.temu import TemuLayout
from src.processors.image import DeterministicProcessor
from src.processors.size_guide import SizeGuideProcessor
from src.providers.registry import create_provider
from src.utils.paths import safe_filename
from src.utils.secrets import mask_message


class Pipeline:
    def __init__(self, config: AppConfig, live: bool = False) -> None:
        self.config = config
        self.live = live
        self.router = TaskRouter(config)
        self.guard = CostGuard(config)
        self.manifest_manager = ManifestManager(config.output_dir)
        self.ledger = CostLedger(config.logs_dir)
        self.layout = TemuLayout(config)
        self.deterministic = DeterministicProcessor(config)
        self.size_guide = SizeGuideProcessor(config)

    def _sku_path(self, sku: str) -> Path:
        return self.config.input_dir / safe_filename(sku)

    def _sku_output_path(self, sku: str, platform: str) -> Path:
        return self.config.output_dir / safe_filename(sku) / safe_filename(platform)

    def _candidate_dir(self, sku: str, platform: str, task_id: str) -> Path:
        return self._sku_output_path(sku, platform) / "candidates" / safe_filename(task_id)

    def load_product(self, sku: str) -> ProductInfo:
        path = self._sku_path(sku) / "product.yaml"
        if not path.exists():
            raise FileNotFoundError(f"Product file not found: {path}")
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        data["sku"] = sku
        return ProductInfo(**data)

    def validate(self, sku: str, platform: str) -> dict[str, Any]:
        errors: list[str] = []
        warnings: list[str] = []

        try:
            product = self.load_product(sku)
        except Exception as e:
            return {"valid": False, "errors": [str(e)], "warnings": []}

        sku_dir = self._sku_path(sku)
        originals_dir = sku_dir / "originals"
        if not originals_dir.exists():
            errors.append(f"Missing originals directory: {originals_dir}")

        try:
            platform_cfg = PlatformConfig(**self.config.get_platform_config(platform))
        except Exception as e:
            return {"valid": False, "errors": [f"Invalid platform config: {e}"], "warnings": []}

        for img_key, img_path in product.images.items():
            full_path = sku_dir / img_path
            if not full_path.exists():
                errors.append(f"Missing image '{img_key}': {full_path}")
            else:
                from src.utils.image import is_valid_image

                if not is_valid_image(full_path):
                    errors.append(f"Image '{img_key}' is not a valid image: {full_path}")

        for task in platform_cfg.tasks:
            if task.type in (TaskType.AI_GENERATE, TaskType.AI_EDIT):
                if not task.task_category:
                    errors.append(f"Task {task.id} missing task_category")
                elif task.task_category not in self.config.routing.get("routes", {}):
                    warnings.append(f"Task {task.id} category '{task.task_category}' not in routing")
            if task.required_images:
                for img_key in task.required_images:
                    if img_key not in product.images:
                        errors.append(f"Task {task.id} requires image '{img_key}' not in product.yaml")

        return {"valid": len(errors) == 0, "errors": errors, "warnings": warnings}

    def build_prompt(
        self,
        product: ProductInfo,
        platform_cfg: PlatformConfig,
        task: PlatformTask,
    ) -> str:
        template_name = task.task_category or task.id
        template = self.config.get_prompt_template(template_name)
        base = template.get("base", "")

        preservation = "\n".join(f"- {r}" for r in platform_cfg.garment_preservation)
        avoid = "\n".join(f"- {r}" for r in platform_cfg.content_rules.get("avoid", []))
        direction = platform_cfg.model_direction

        context = {
            "sku": product.sku,
            "color": product.product.get("color", ""),
            "category": product.product.get("category", ""),
            "fabric_name": product.fabric.get("name", ""),
            "ethnicity": direction.get("ethnicity", "western adult male"),
            "scene": direction.get("scene", ""),
            "style": direction.get("style", ""),
            "preservation_rules": preservation,
            "avoid_rules": avoid,
        }
        return self._render_template(base, context)

    def _render_template(self, template: str, context: dict[str, Any]) -> str:
        result = template
        for key, value in context.items():
            result = result.replace(f"{{{{ {key} }}}}", str(value))
            result = result.replace(f"{{{{{key}}}}}", str(value))
        return result

    def _resolve_aspect_ratio(self, model_cfg: ModelConfig, platform_cfg: PlatformConfig) -> str:
        return model_cfg.default_aspect_ratio or platform_cfg.ratio

    def _resolve_size(self, model_cfg: ModelConfig, resolution: str | None = None) -> str | None:
        if resolution:
            return resolution
        return (
            model_cfg.default_image_size
            or model_cfg.image_size
            or model_cfg.default_resolution
        )

    def _save_prompt(
        self,
        sku: str,
        platform: str,
        task_id: str,
        attempt: int,
        prompt: str,
        template_name: str,
        template_version: str,
    ) -> tuple[Path, str]:
        meta_dir = self._sku_output_path(sku, platform) / "metadata" / "prompts"
        meta_dir.mkdir(parents=True, exist_ok=True)
        filename = f"{safe_filename(task_id)}_attempt_{attempt:02d}.txt"
        path = meta_dir / filename
        content = (
            f"Task: {task_id}\n"
            f"Attempt: {attempt}\n"
            f"Template: {template_name}\n"
            f"Version: {template_version}\n"
            f"Prompt:\n{prompt}\n"
        )
        path.write_text(content, encoding="utf-8")
        prompt_hash = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]
        return path, prompt_hash

    def run_task(
        self,
        sku: str,
        platform: str,
        task_id: str,
        model_override: str | None = None,
        count: int = 1,
        resolution: str | None = None,
    ) -> TaskManifest:
        product = self.load_product(sku)
        platform_cfg = PlatformConfig(**self.config.get_platform_config(platform))
        task = next((t for t in platform_cfg.tasks if t.id == task_id), None)
        if task is None:
            return TaskManifest(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error=f"Task {task_id} not found in platform {platform}",
            )

        if task.type == TaskType.DETERMINISTIC:
            return self._run_deterministic(product, platform_cfg, sku, platform, task)

        return self._run_ai_task(
            product, platform_cfg, sku, platform, task, model_override, count, resolution
        )

    def _run_deterministic(
        self,
        product: ProductInfo,
        platform_cfg: PlatformConfig,
        sku: str,
        platform: str,
        task: Any,
    ) -> TaskManifest:
        task_id = task.id
        try:
            if task_id == "01_main":
                dest = self._sku_output_path(sku, platform) / f"{safe_filename(task_id)}.png"
                self.deterministic.create_main_image(product, platform_cfg, dest)
            elif task_id == "08_size_guide":
                dest = self._sku_output_path(sku, platform) / f"{safe_filename(task_id)}.png"
                self.size_guide.create_size_guide(product, platform_cfg, dest)
            else:
                return TaskManifest(
                    task_id=task_id,
                    status=TaskStatus.SKIPPED,
                    error="No deterministic handler for this task",
                )
            return TaskManifest(
                task_id=task_id,
                status=TaskStatus.ACCEPTED,
                estimated_cost_usd=0.0,
                actual_cost_usd=0.0,
            )
        except Exception as e:
            return TaskManifest(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error=mask_message(str(e)),
            )

    def _run_ai_task(
        self,
        product: ProductInfo,
        platform_cfg: PlatformConfig,
        sku: str,
        platform: str,
        task: Any,
        model_override: str | None,
        count: int,
        resolution: str | None = None,
    ) -> TaskManifest:
        task_id = task.id
        task_category = task.task_category or task_id
        model_chain = self.router.model_chain(task_category)
        if model_override:
            model_chain = [self.router.get_model_config(model_override)] + model_chain

        if not model_chain:
            return TaskManifest(
                task_id=task_id,
                status=TaskStatus.FAILED,
                error="No model available for task",
            )

        prompt = self.build_prompt(product, platform_cfg, task)
        template = self.config.get_prompt_template(task_category)
        template_name = task_category
        template_version = template.get("version", "1.0.0")

        last_error: str | None = None
        for attempt, model_cfg in enumerate(model_chain[: self.guard.max_attempts], start=1):
            estimated = model_cfg.estimated_cost_usd * count
            budget_check = self.guard.check_task_budget(sku, platform, task_id, estimated, attempt)
            if not budget_check.allowed:
                last_error = budget_check.reason
                continue

            if not self.live:
                # Dry-run: still produce mock candidates
                provider = create_provider(model_cfg, force_mock=True)
            else:
                provider = create_provider(model_cfg)

            out_dir = self._candidate_dir(sku, platform, task_id)
            prompt_path, prompt_hash = self._save_prompt(
                sku, platform, task_id, attempt, prompt, template_name, template_version
            )

            try:
                if task.type == TaskType.AI_EDIT or task.required_images:
                    ref_images = [
                        self._sku_path(sku) / product.images[k]
                        for k in task.required_images
                        if k in product.images
                    ]
                    if task.type == TaskType.AI_GENERATE and not ref_images:
                        result = self._call_generate(
                            provider, model_cfg, sku, platform, task_id, prompt, count, resolution
                        )
                    else:
                        result = self._call_edit(
                            provider, model_cfg, sku, platform, task_id, prompt, ref_images, count, resolution
                        )
                else:
                    result = self._call_generate(
                        provider, model_cfg, sku, platform, task_id, prompt, count, resolution
                    )
            except Exception as e:
                last_error = mask_message(str(e))
                self.ledger.record_call(
                    sku=sku,
                    platform=platform,
                    task=task_id,
                    provider=provider.name,
                    model=model_cfg.model,
                    request_id=None,
                    attempt=attempt,
                    input_images=[str(p) for p in (task.required_images or [])],
                    requested_size=self._resolve_size(model_cfg, resolution),
                    aspect_ratio=self._resolve_aspect_ratio(model_cfg, platform_cfg),
                    estimated_cost_usd=estimated,
                    actual_cost_usd=None,
                    status="failed",
                    accepted=False,
                    error=last_error,
                    duration_seconds=0.0,
                )
                continue

            candidates = [
                CandidateInfo(
                    index=i + 1,
                    filename=img.path.name,
                    path=img.path,
                    status=TaskStatus.PENDING,
                    estimated_cost_usd=model_cfg.estimated_cost_usd,
                )
                for i, img in enumerate(result.images)
            ]

            actual = result.actual_cost_usd
            self.ledger.record_call(
                sku=sku,
                platform=platform,
                task=task_id,
                provider=result.provider,
                model=result.model,
                request_id=result.request_id,
                attempt=attempt,
                input_images=[str(p) for p in (task.required_images or [])],
                requested_size=self._resolve_size(model_cfg, resolution),
                aspect_ratio=self._resolve_aspect_ratio(model_cfg, platform_cfg),
                estimated_cost_usd=result.estimated_cost_usd,
                actual_cost_usd=actual,
                status="success",
                accepted=False,
                error=None,
                duration_seconds=result.duration_seconds,
            )

            return TaskManifest(
                task_id=task_id,
                status=TaskStatus.GENERATED,
                model=model_cfg.name,
                provider=result.provider,
                candidates=candidates,
                estimated_cost_usd=result.estimated_cost_usd,
                actual_cost_usd=actual,
                prompt_hash=prompt_hash,
                prompt_template=template_name,
                prompt_version=template_version,
            )

        return TaskManifest(
            task_id=task_id,
            status=TaskStatus.FAILED,
            error=last_error or "All model attempts failed",
        )

    def _call_generate(
        self,
        provider: ImageProvider,
        model_cfg: ModelConfig,
        sku: str,
        platform: str,
        task_id: str,
        prompt: str,
        count: int,
        resolution: str | None = None,
    ) -> Any:
        platform_cfg = PlatformConfig(**self.config.get_platform_config(platform))
        request = GenerateRequest(
            prompt=prompt,
            sku=sku,
            task_id=task_id,
            platform=platform,
            aspect_ratio=self._resolve_aspect_ratio(model_cfg, platform_cfg),
            size=self._resolve_size(model_cfg, resolution),
            n=count,
        )
        out_dir = self._candidate_dir(sku, platform, task_id)
        return provider.generate(request, out_dir)

    def _call_edit(
        self,
        provider: ImageProvider,
        model_cfg: ModelConfig,
        sku: str,
        platform: str,
        task_id: str,
        prompt: str,
        reference_images: list[Path],
        count: int,
        resolution: str | None = None,
    ) -> Any:
        platform_cfg = PlatformConfig(**self.config.get_platform_config(platform))
        request = EditRequest(
            prompt=prompt,
            sku=sku,
            task_id=task_id,
            platform=platform,
            reference_images=reference_images,
            aspect_ratio=self._resolve_aspect_ratio(model_cfg, platform_cfg),
            size=self._resolve_size(model_cfg, resolution),
            n=count,
        )
        out_dir = self._candidate_dir(sku, platform, task_id)
        return provider.edit(request, out_dir)

    def build(self, sku: str, platform: str, model_override: str | None = None) -> BuildManifest:
        validation = self.validate(sku, platform)
        if not validation["valid"]:
            raise ValueError(f"Validation failed: {'; '.join(validation['errors'])}")

        platform_cfg = PlatformConfig(**self.config.get_platform_config(platform))
        manifest = BuildManifest(
            sku=sku,
            platform=platform,
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        for task in platform_cfg.tasks:
            task_manifest = self.run_task(sku, platform, task.id, model_override)
            manifest.tasks.append(task_manifest)
            self.manifest_manager.update_task(sku, platform, task_manifest)

        manifest.total_estimated_cost_usd = sum(t.estimated_cost_usd for t in manifest.tasks)
        manifest.total_actual_cost_usd = sum((t.actual_cost_usd or 0.0) for t in manifest.tasks)
        self.manifest_manager.save(manifest)
        self._write_cost_report(manifest)
        return manifest

    def _write_cost_report(self, manifest: BuildManifest) -> Path:
        report_path = self._sku_output_path(manifest.sku, manifest.platform) / "cost-report.json"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        providers: dict[str, float] = {}
        for t in manifest.tasks:
            key = t.provider or "unknown"
            providers[key] = providers.get(key, 0.0) + (t.actual_cost_usd or t.estimated_cost_usd)

        report = {
            "sku": manifest.sku,
            "platform": manifest.platform,
            "api_calls": len([t for t in manifest.tasks if t.provider]),
            "successful_calls": len([t for t in manifest.tasks if t.provider and t.status != "failed"]),
            "failed_calls": len([t for t in manifest.tasks if t.provider and t.status == "failed"]),
            "estimated_cost_usd": round(manifest.total_estimated_cost_usd, 4),
            "actual_cost_usd": round(manifest.total_actual_cost_usd, 4),
            "providers": {k: round(v, 4) for k, v in providers.items() if k != "unknown"},
        }
        report_path.write_text(
            json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        return report_path

    def accept_candidate(
        self,
        sku: str,
        platform: str,
        task_id: str,
        candidate_index: int,
    ) -> Path:
        manifest = self.manifest_manager.load(sku, platform)
        if not manifest:
            raise FileNotFoundError(f"No manifest found for {sku}/{platform}")
        task = next((t for t in manifest.tasks if t.task_id == task_id), None)
        if not task:
            raise ValueError(f"Task {task_id} not found in manifest")

        cand = next((c for c in task.candidates if c.index == candidate_index), None)
        if not cand:
            raise ValueError(f"Candidate {candidate_index} not found for task {task_id}")

        src = cand.path
        dest = self._sku_output_path(sku, platform) / f"{safe_filename(task_id)}.png"
        import shutil

        shutil.copy(src, dest)
        task.status = TaskStatus.ACCEPTED
        task.accepted_candidate = candidate_index
        # Only the accepted candidate changes state; other candidates stay
        # untouched (pending/rejected) and their files are always kept.
        for c in task.candidates:
            if c.index == candidate_index:
                c.status = TaskStatus.ACCEPTED
            elif c.status == TaskStatus.ACCEPTED:
                # A previously accepted candidate is demoted back to pending
                c.status = TaskStatus.PENDING
        self.manifest_manager.save(manifest)
        return dest

    def reject_candidate(
        self,
        sku: str,
        platform: str,
        task_id: str,
        candidate_index: int,
    ) -> None:
        """Mark a candidate as rejected. The file is kept for later analysis."""
        manifest = self.manifest_manager.load(sku, platform)
        if not manifest:
            raise FileNotFoundError(f"No manifest found for {sku}/{platform}")
        task = next((t for t in manifest.tasks if t.task_id == task_id), None)
        if not task:
            raise ValueError(f"Task {task_id} not found in manifest")

        cand = next((c for c in task.candidates if c.index == candidate_index), None)
        if not cand:
            raise ValueError(f"Candidate {candidate_index} not found for task {task_id}")
        if cand.status == TaskStatus.ACCEPTED:
            raise ValueError("Cannot reject the accepted candidate; accept another candidate first")

        cand.status = TaskStatus.REJECTED
        self.manifest_manager.save(manifest)
