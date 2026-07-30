"""Unified aspect-ratio + resolution -> exact size resolution.

Single place that maps (model, aspect_ratio, resolution) to an exact size
string, driven entirely by models.yaml (size_map / supported_sizes). Business
code (Pipeline, providers, future platforms like Amazon/TikTok) must call
this instead of scattering size strings around.
"""
from __future__ import annotations

from src.core.models import ModelConfig

# Resolution tokens understood in request.size (e.g. CLI --resolution 1K).
RESOLUTION_TOKENS = ("0.5K", "1K", "2K", "4K")


def is_resolution_token(value: str | None) -> bool:
    return bool(value) and value in RESOLUTION_TOKENS


def resolve_image_size(
    model_cfg: ModelConfig,
    aspect_ratio: str | None = None,
    resolution: str | None = None,
) -> str | None:
    """Resolve the exact "WxH" size for a model.

    Models with a ``size_map`` (e.g. gpt-image-2-vip) resolve via
    aspect_ratio + resolution. Models without one fall back to
    ``default_size`` / the first entry of ``supported_sizes``.
    Returns None when the model has no exact-size configuration.
    Raises ValueError for unsupported ratio/resolution combinations.
    """
    size_map = model_cfg.size_map or {}
    if not size_map:
        return model_cfg.default_size or (
            model_cfg.supported_sizes[0] if model_cfg.supported_sizes else None
        )

    ratio = aspect_ratio or model_cfg.default_aspect_ratio
    if ratio not in size_map:
        raise ValueError(
            f"Aspect ratio '{ratio}' is not supported by {model_cfg.model}. "
            f"Supported: {', '.join(size_map)}"
        )
    levels = size_map[ratio]
    res = resolution or model_cfg.default_resolution
    if res not in levels:
        raise ValueError(
            f"Resolution '{res}' is not supported by {model_cfg.model} for {ratio}. "
            f"Supported: {', '.join(levels)}"
        )
    size = levels[res]
    if model_cfg.supported_sizes and size not in model_cfg.supported_sizes:
        raise ValueError(
            f"Resolved size '{size}' ({ratio} {res}) is not in the supported size "
            f"table of {model_cfg.model}"
        )
    return size
