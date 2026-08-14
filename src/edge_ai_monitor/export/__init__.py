"""Optional northbound export of errors and proposals.

Inert unless sinks are configured. See docs/northbound-export.md.
"""

from .exporter import Exporter, build_exporter
from .records import (
    DEFAULT_LEVEL,
    LEVEL_ANALYSIS,
    LEVEL_FULL,
    LEVEL_METADATA,
    REDACTION_LEVELS,
    build_record,
)
from .spool import Spool

__all__ = [
    "Exporter",
    "build_exporter",
    "build_record",
    "Spool",
    "DEFAULT_LEVEL",
    "LEVEL_METADATA",
    "LEVEL_ANALYSIS",
    "LEVEL_FULL",
    "REDACTION_LEVELS",
]
