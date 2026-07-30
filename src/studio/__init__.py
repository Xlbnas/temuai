"""Independent Product Image Studio domain.

This package deliberately has no dependency on the legacy SKU pipeline.  Both
the Click commands and the Web routes call :class:`StudioService`.
"""

from src.studio.service import StudioService

__all__ = ["StudioService"]
