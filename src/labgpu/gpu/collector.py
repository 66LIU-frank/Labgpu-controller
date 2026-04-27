from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class GPUCollector(ABC):
    @abstractmethod
    def collect(self) -> dict[str, Any]:
        """Return GPU status payload."""
