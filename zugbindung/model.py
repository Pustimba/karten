"""Gemeinsame Datenstruktur für eine Teilstrecke einer Verbindung."""
import re
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Leg:
    """Eine Teilstrecke. Fußwege haben walk=True und keine Zugdaten.

    Zeiten sind Ortszeit (Europe/Berlin) ohne Zeitzone, so wie sie auch in der
    Verspätungsdatenbank stehen.
    """
    name: str                 # Anzeigename, z. B. "RE2 3123" oder "ICE 854"
    num: str                  # Zugnummer ohne führende Nullen ("" wenn unbekannt)
    line: str                 # Linie wie "RE2", "S47" ("" wenn unbekannt)
    from_name: str
    to_name: str
    dep: datetime
    arr: datetime
    walk: bool = False
    from_eva: str = ""        # 8-stellige EVA-Nummer, falls bekannt (bahn.de)
    to_eva: str = ""
    operator: str = ""        # Betreiber, falls bekannt
    product: str = ""         # Gattung, z. B. "ICE", "EC_IC", "REGIONAL", "HIGHSPEED_RAIL"
    stops: list = field(default_factory=list)   # alle Halte der Teilstrecke (bahn.de), inkl. Start und Ziel

    def long_distance(self) -> bool:
        """Fernzug? Nur damit wird ein Ticket zum Fernverkehrsticket, das nach
        aufgehobener Zugbindung auch für ICE und IC gilt."""
        if self.walk:
            return False
        if self.product in ("ICE", "EC_IC", "IR", "HIGHSPEED_RAIL", "LONG_DISTANCE"):
            return True
        return bool(re.match(r"^(ICE|IC|EC|ECE|RJX?)\b", self.name))

    def label(self) -> str:
        return f"{self.name} {self.from_name} {self.dep:%H:%M} → {self.to_name} {self.arr:%H:%M}"
