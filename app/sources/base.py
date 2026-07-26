from abc import ABC, abstractmethod

from app.db import RawStatement


class Source(ABC):
    """Eine Quelle liefert bei jedem poll() neue, noch nicht gesehene RawStatements."""

    name: str

    # Grund des letzten fehlgeschlagenen Abrufs, oder None.
    #
    # Warum das noetig ist: Alle Quellen fangen ihre Netzwerk-/Parse-Fehler selbst ab
    # und liefern dann eine leere Liste - bewusst, damit ein einzelner kaputter Feed
    # weder die anderen Feeds noch den restlichen Poll-Zyklus mitreisst. Der
    # Orchestrator setzt source_health["last_error"] aber nur, wenn eine Ausnahme bis
    # zu ihm durchdringt. Dadurch war eine dauerhaft kaputte Quelle von "gerade keine
    # passenden Meldungen" NICHT unterscheidbar: last_error blieb null, total_fetched
    # 0, und Dashboard wie Live-Signal zeigten unveraendert ein gruenes Haekchen.
    #
    # Klassenattribut (kein __init__), weil die Unterklassen eigene __init__ haben, die
    # super() nicht aufrufen - so ist das Attribut trotzdem immer lesbar.
    last_failure: str | None = None

    def note_failure(self, exc: BaseException) -> None:
        """Von den Quellen in ihren except-Bloecken aufzurufen, damit der Fehlschlag
        sichtbar wird, obwohl die Ausnahme absichtlich nicht weitergereicht wird."""
        self.last_failure = f"{type(exc).__name__}: {exc}"

    @abstractmethod
    async def poll(self) -> list[RawStatement]:
        ...
