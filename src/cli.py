"""TEMU Image Factory CLI."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from src.core.config import AppConfig, get_config
from src.core.pipeline import Pipeline
from src.studio.analyzers import MockAssetAnalyzer
from src.studio.models import StudioPlatform
from src.studio.service import StudioService
from src.utils.paths import sku_output_path


@click.group()
@click.option("--config-dir", type=click.Path(), default=None)
@click.option("--live", is_flag=True, default=False, help="Enable real paid API calls.")
@click.pass_context
def cli(ctx: click.Context, config_dir: str | None, live: bool) -> None:
    """TEMU Image Factory CLI."""
    ctx.ensure_object(dict)
    if config_dir:
        import os

        os.environ["CONFIG_DIR"] = config_dir
    config = get_config()
    ctx.obj["config"] = config
    ctx.obj["live"] = live


@cli.command()
@click.argument("sku")
@click.option("--platform", default="temu")
@click.pass_context
def validate(ctx: click.Context, sku: str, platform: str) -> None:
    """Validate SKU input, images, and platform config."""
    config: AppConfig = ctx.obj["config"]
    pipeline = Pipeline(config, live=False)
    result = pipeline.validate(sku, platform)
    click.echo(json.dumps(result, indent=2, ensure_ascii=False))
    sys.exit(0 if result["valid"] else 1)


@cli.command()
@click.argument("sku")
@click.option("--platform", default="temu")
@click.option("--model", default=None, help="Override model for A/B testing.")
@click.pass_context
def build(ctx: click.Context, sku: str, platform: str, model: str | None) -> None:
    """Build all images for SKU/platform. Default dry-run; use --live for real API."""
    config: AppConfig = ctx.obj["config"]
    live: bool = ctx.obj["live"]
    pipeline = Pipeline(config, live=live)
    if live:
        click.echo("LIVE mode enabled. Real API calls will be made and may incur cost.")
        click.confirm("Do you want to continue?", abort=True)
    try:
        manifest = pipeline.build(sku, platform, model_override=model)
        click.echo(f"Build complete: {sku}/{platform}")
        click.echo(f"Estimated cost: ${manifest.total_estimated_cost_usd:.4f}")
        click.echo(f"Output: {sku_output_path(sku, platform)}")
    except Exception as e:
        click.echo(f"Build failed: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("sku")
@click.argument("task")
@click.option("--platform", default="temu")
@click.option("--model", default=None)
@click.option("--count", default=1, help="Number of candidates to generate.")
@click.option(
    "--resolution",
    type=click.Choice(["0.5K", "1K", "2K", "4K"]),
    default=None,
    help="Resolution override (mapped to exact size per model config).",
)
@click.pass_context
def generate(
    ctx: click.Context,
    sku: str,
    task: str,
    platform: str,
    model: str | None,
    count: int,
    resolution: str | None,
) -> None:
    """Generate candidates for a single task. Default dry-run; use --live for real API."""
    config: AppConfig = ctx.obj["config"]
    live: bool = ctx.obj["live"]
    pipeline = Pipeline(config, live=live)

    if live:
        from src.core.routing import TaskRouter

        router = TaskRouter(config)
        task_category = task
        model_cfg = (
            router.get_model_config(model) if model else router.model_chain(task_category)[0]
        )
        estimated = model_cfg.estimated_cost_usd * count
        guard = pipeline.guard
        check = guard.check_task_budget(sku, platform, task, estimated)
        click.echo(guard.format_cost_preview(sku, platform, task, model_cfg.name, count, estimated))
        if not check.allowed:
            click.echo(f"Budget check failed: {check.reason}", err=True)
            sys.exit(1)
        click.confirm("Confirm generate?", abort=True)

    try:
        task_manifest = pipeline.run_task(
            sku, platform, task, model_override=model, count=count, resolution=resolution
        )
        click.echo(json.dumps(task_manifest.model_dump(mode="json"), indent=2, ensure_ascii=False))
        if task_manifest.status == "failed":
            sys.exit(1)
    except Exception as e:
        click.echo(f"Generate failed: {e}", err=True)
        sys.exit(1)


@cli.command()
@click.argument("sku")
@click.argument("task")
@click.argument("candidate", type=int)
@click.option("--platform", default="temu")
@click.pass_context
def accept(ctx: click.Context, sku: str, task: str, candidate: int, platform: str) -> None:
    """Accept a candidate as the final image for a task."""
    config: AppConfig = ctx.obj["config"]
    pipeline = Pipeline(config, live=False)
    try:
        dest = pipeline.accept_candidate(sku, platform, task, candidate)
        click.echo(f"Accepted candidate {candidate} -> {dest}")
    except Exception as e:
        click.echo(f"Accept failed: {e}", err=True)
        sys.exit(1)


@cli.group()
def studio() -> None:
    """Product Image Studio commands (always offline in M1)."""


@studio.command("create")
@click.argument("name")
@click.option("--platform", type=click.Choice(["temu", "tiktok_shop"]), default="temu")
@click.pass_context
def studio_create(ctx: click.Context, name: str, platform: str) -> None:
    project = StudioService(ctx.obj["config"]).create_project(name, StudioPlatform(platform))
    click.echo(project.id)


@studio.command("import")
@click.argument("project_id")
@click.argument("images", nargs=-1, type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.pass_context
def studio_import(ctx: click.Context, project_id: str, images: tuple[Path, ...]) -> None:
    """Archive images in a Studio project; duplicate SHA-256s are skipped."""
    if not images or len(images) > 20:
        raise click.UsageError("Provide between 1 and 20 image files")
    service = StudioService(ctx.obj["config"])
    max_bytes = int(ctx.obj["config"].safe_env("MAX_UPLOAD_MB", "30") or "30") * 1024 * 1024
    for image in images:
        asset, duplicate = service.import_asset(
            project_id, image.name, image.read_bytes(), max_bytes
        )
        click.echo(
            f"{'duplicate' if duplicate else 'imported'} {asset.id} {asset.original_filename}"
        )


@studio.command("analyze")
@click.argument("project_id")
@click.argument("asset_id")
@click.option("--mock", "use_mock", is_flag=True, help="Run the explicit offline M1 mock.")
@click.pass_context
def studio_analyze(
    ctx: click.Context, project_id: str, asset_id: str, use_mock: bool
) -> None:
    if not use_mock:
        raise click.UsageError("M1 has no production analyzer; pass --mock for offline simulation")
    analysis = StudioService(ctx.obj["config"]).analyze_asset(
        project_id, asset_id, analyzer=MockAssetAnalyzer()
    )
    click.echo(analysis.model_dump_json(indent=2))


@studio.command("render-annotations")
@click.argument("project_id")
@click.argument("asset_id")
@click.pass_context
def studio_render_annotations(ctx: click.Context, project_id: str, asset_id: str) -> None:
    config: AppConfig = ctx.obj["config"]
    path = StudioService(config).render_annotations(project_id, asset_id)
    click.echo(path.relative_to(config.data_dir))


@studio.command("compile-spec")
@click.argument("project_id")
@click.pass_context
def studio_compile_spec(ctx: click.Context, project_id: str) -> None:
    click.echo(
        StudioService(ctx.obj["config"]).compile_product_spec(project_id).model_dump_json(indent=2)
    )


@studio.command("compile-bundle")
@click.argument("project_id")
@click.pass_context
def studio_compile_bundle(ctx: click.Context, project_id: str) -> None:
    click.echo(
        StudioService(ctx.obj["config"])
        .compile_reference_bundle(project_id)
        .model_dump_json(indent=2)
    )


@studio.command("compile-plan")
@click.argument("project_id")
@click.pass_context
def studio_compile_plan(ctx: click.Context, project_id: str) -> None:
    """Create the platform's default five-shot M2 plan."""
    plan = StudioService(ctx.obj["config"]).compile_shot_plan(project_id)
    click.echo(plan.model_dump_json(indent=2))


@studio.command("show-plan")
@click.argument("project_id")
@click.argument("plan_id")
@click.pass_context
def studio_show_plan(ctx: click.Context, project_id: str, plan_id: str) -> None:
    record = StudioService(ctx.obj["config"]).get_record(project_id)
    plan = next((item for item in record.shot_plans if item.id == plan_id), None)
    if plan is None:
        raise click.ClickException("Shot Plan not found")
    click.echo(plan.model_dump_json(indent=2))


@studio.command("confirm-plan")
@click.argument("project_id")
@click.argument("plan_id")
@click.option("--by", default="cli", show_default=True)
@click.pass_context
def studio_confirm_plan(ctx: click.Context, project_id: str, plan_id: str, by: str) -> None:
    plan = StudioService(ctx.obj["config"]).confirm_shot_plan(project_id, plan_id, by)
    click.echo(plan.status.value)


@studio.command("compile-prompts")
@click.argument("project_id")
@click.argument("plan_id")
@click.pass_context
def studio_compile_prompts(ctx: click.Context, project_id: str, plan_id: str) -> None:
    packages = StudioService(ctx.obj["config"]).compile_prompt_packages(project_id, plan_id)
    click.echo(json.dumps([item.model_dump(mode="json") for item in packages], indent=2))


@studio.command("cost-preview")
@click.argument("project_id")
@click.argument("plan_id")
@click.pass_context
def studio_cost_preview(ctx: click.Context, project_id: str, plan_id: str) -> None:
    record = StudioService(ctx.obj["config"]).get_record(project_id)
    plan = next((item for item in record.shot_plans if item.id == plan_id), None)
    if plan is None:
        raise click.ClickException("Shot Plan not found")
    click.echo(json.dumps({"provider": "mock", "pricing_version": "mock-0", "per_shot": 0, "total": 0, "enabled_shots": sum(s.enabled for s in plan.shots)}, indent=2))


@studio.command("generate-mock")
@click.argument("project_id")
@click.argument("plan_id")
@click.option("--shot-id", default=None, help="Generate only one enabled shot.")
@click.option("--fail-shot-id", default=None, help="Offline test hook: mark one shot failed.")
@click.pass_context
def studio_generate_mock(
    ctx: click.Context, project_id: str, plan_id: str, shot_id: str | None, fail_shot_id: str | None
) -> None:
    service = StudioService(ctx.obj["config"])
    job = service.create_generation_job(project_id, plan_id, shot_id=shot_id)
    click.echo(service.run_generation_job(project_id, job.id, fail_shot_id).model_dump_json(indent=2))


@studio.command("generate-live")
@click.argument("project_id")
@click.argument("plan_id")
@click.option("--mode", type=click.Choice(["live"]), required=True)
@click.option("--provider", type=click.Choice(["apiyi"]), required=True)
@click.option("--max-cost", type=float, required=True)
@click.option("--confirm-paid-generation", is_flag=True, required=True)
@click.pass_context
def studio_generate_live(
    ctx: click.Context,
    project_id: str,
    plan_id: str,
    mode: str,
    provider: str,
    max_cost: float,
    confirm_paid_generation: bool,
) -> None:
    """Reserved paid interface; M2A safely rejects unverified APIYI Studio calls."""
    if max_cost < 0:
        raise click.UsageError("--max-cost must be non-negative")
    from src.studio.models import BudgetPolicy

    service = StudioService(ctx.obj["config"])
    try:
        service.create_generation_job(
            project_id,
            plan_id,
            mode=mode,
            provider=provider,
            budget_policy=BudgetPolicy(project_limit=max_cost, job_limit=max_cost, shot_limit=max_cost),
            paid_confirmation=confirm_paid_generation,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc


@studio.command("show-job")
@click.argument("project_id")
@click.argument("job_id")
@click.pass_context
def studio_show_job(ctx: click.Context, project_id: str, job_id: str) -> None:
    record = StudioService(ctx.obj["config"]).get_record(project_id)
    job = next((item for item in record.generation_jobs if item.id == job_id), None)
    if job is None:
        raise click.ClickException("Generation Job not found")
    attempts = [item for item in record.generation_attempts if item.job_id == job_id]
    click.echo(json.dumps({"job": job.model_dump(mode="json"), "attempts": [item.model_dump(mode="json") for item in attempts]}, indent=2))


@studio.command("list-candidates")
@click.argument("project_id")
@click.pass_context
def studio_list_candidates(ctx: click.Context, project_id: str) -> None:
    record = StudioService(ctx.obj["config"]).get_record(project_id)
    click.echo(json.dumps([item.model_dump(mode="json") for item in record.candidates], indent=2))


@studio.command("accept-candidate")
@click.argument("project_id")
@click.argument("candidate_id")
@click.pass_context
def studio_accept_candidate(ctx: click.Context, project_id: str, candidate_id: str) -> None:
    click.echo(StudioService(ctx.obj["config"]).accept_candidate(project_id, candidate_id).status.value)


@studio.command("reject-candidate")
@click.argument("project_id")
@click.argument("candidate_id")
@click.option("--reason", required=True)
@click.pass_context
def studio_reject_candidate(ctx: click.Context, project_id: str, candidate_id: str, reason: str) -> None:
    click.echo(StudioService(ctx.obj["config"]).reject_candidate(project_id, candidate_id, reason).status.value)


@cli.group()
def auth() -> None:
    """Authentication utilities."""
    pass


@auth.command("hash-password")
def hash_password() -> None:
    """Generate Argon2id hash for APP_PASSWORD_HASH."""
    import getpass

    import argon2

    password = getpass.getpass("Enter password: ")
    confirm = getpass.getpass("Confirm password: ")
    if password != confirm:
        click.echo("Passwords do not match.", err=True)
        sys.exit(1)
    hasher = argon2.PasswordHasher(time_cost=3, memory_cost=65536, parallelism=1)
    hash_value = hasher.hash(password)
    click.echo("\nGenerated Argon2id hash (paste into APP_PASSWORD_HASH):")
    click.echo(hash_value)


def main() -> None:
    cli()
