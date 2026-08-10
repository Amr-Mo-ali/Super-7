"""Deterministic representation of already accepted event candidates."""

from services.event_arbitration.arbitrator import EventArbitrator
from services.event_arbitration.models import EventCandidateRef

__all__ = ["EventArbitrator", "EventCandidateRef"]
