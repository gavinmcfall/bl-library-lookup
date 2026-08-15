"""blmeta -- exact-ISBN metadata resolution for Black Library books."""

__version__ = "1.0.0"

from .record import Confidence, ResolvedRecord, Status
from .resolver import Resolver

__all__ = ["Confidence", "ResolvedRecord", "Status", "Resolver", "__version__"]
