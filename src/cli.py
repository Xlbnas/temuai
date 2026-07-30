"""TEMU Image Factory CLI."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import click
import yaml

from src.core.config import AppConfig, get_config
from src.core.pipeline import Pipeline
from src.core.provider import GenerateRequest
from src.providers.registry import create_provider
from src.utils.paths import sku_output_path
from src.utils.secrets import mask_string


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
        model_cfg = router.get_model_config(model) if model else router.model_chain(task_category)[0]
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
