"""
Agent → Journey → Persona mapping importer.

Reads a static CSV that maps each agent_id to a journey and persona type.
This seeds the dim_agent_journey_persona dimension table, which drives
the experience-based XLA scoring model (Persona → Journey → XLA).

CSV format (imports/agent_journey_persona_map.csv):
  agent_id, agent_name, journey_name, persona_type
"""
import csv
from pathlib import Path

_VALID_JOURNEYS  = {"Get Help", "Complete Task", "Find Info", "Automate Work"}
_VALID_PERSONAS  = {"end_user", "it_support", "hr", "knowledge_worker", "operations"}


def _read(path: str) -> list[dict]:
    p = Path(path)
    if not p.exists():
        return []
    with open(p, newline="", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


class JourneyMapImporter:
    """Reads the static agent→journey→persona mapping CSV."""

    def __init__(self, map_path: str = "") -> None:
        self._path = map_path

    def fetch_mapping(self) -> list[dict]:
        out = []
        for r in _read(self._path):
            agent_id     = r.get("agent_id", "").strip()
            agent_name   = r.get("agent_name", "").strip()
            journey_name = r.get("journey_name", "").strip()
            persona_type = r.get("persona_type", "").strip()
            if not agent_id or not journey_name or not persona_type:
                continue
            out.append({
                "agent_id":     agent_id,
                "agent_name":   agent_name,
                "journey_name": journey_name,
                "persona_type": persona_type,
            })
        return out
