"""Common M14 entry point for the app and an experiment. No model defaults."""
from dataclasses import asdict
from pathlib import Path

try:
    from .call_guard import CallGuard, Outcome, Policy
except ImportError:  # python3 app/server.py
    from call_guard import CallGuard, Outcome, Policy

DEFAULT_LEDGER = Path(__file__).resolve().parent.parent / "data_local" / "ege_checker" / "spending"


class CheckingService:
    def __init__(self, ledger=DEFAULT_LEDGER, *, policy=None, transport=None, validator=None):
        self.ledger = Path(ledger)
        self.policy = policy or Policy()
        self.transport = transport
        self.validator = validator

    def availability(self):
        return {"paid_api_enabled": False,
                "reason": self.policy.validate() or "mathematical_pipeline_not_connected",
                "assessment": None,
                "error": "Математическая проверка ещё не подключена. Материалы можно сохранить локально."}

    def run_stage(self, request, *, retry=False):
        """For future M04/M05; never touches or replaces a saved student result."""
        error = self.policy.validate()
        if error:
            return Outcome(error)
        if self.transport is None or self.validator is None:
            return Outcome("stage_not_connected")
        guard = CallGuard(self.ledger, self.policy)
        return guard.execute(request, self.transport, self.validator, retry=retry)
