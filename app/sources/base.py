from abc import ABC, abstractmethod

from app.db import RawStatement


class Source(ABC):
    """Eine Quelle liefert bei jedem poll() neue, noch nicht gesehene RawStatements."""

    name: str

    @abstractmethod
    async def poll(self) -> list[RawStatement]:
        ...
