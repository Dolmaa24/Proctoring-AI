"""Headless simulator: scripted candidates and hostile clients, no webcam required."""

from .client import SimulatedClient, Tamper
from .scenarios import BEHAVIOURAL, TICK_MS, ScriptedEvent

__all__ = ["BEHAVIOURAL", "TICK_MS", "ScriptedEvent", "SimulatedClient", "Tamper"]
