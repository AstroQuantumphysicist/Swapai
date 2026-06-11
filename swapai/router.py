"""Account router: picks the active account and fails over on limits."""

from __future__ import annotations

import threading
import time

from . import accounts, usage
from .accounts import Account


class Router:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.accounts: list[Account] = []
        self.active_index: int = 0
        self.reload()

    def reload(self) -> None:
        with self._lock:
            self.accounts = accounts.list_accounts()
            if self.active_index >= len(self.accounts):
                self.active_index = 0

    def common_models(self) -> list[str]:
        """Models available across ALL logged-in accounts (intersection)."""
        accs = [a for a in self.accounts if a.models]
        if not accs:
            return []
        common = set(accs[0].models)
        for a in accs[1:]:
            common &= set(a.models)
        # preserve candidate ordering
        from .config import CANDIDATE_MODELS
        return [m for m in CANDIDATE_MODELS if m in common]

    def _usable(self, acc: Account) -> bool:
        if acc.is_rate_limited:
            return False
        return bool(acc.access_token)

    def active_account(self) -> Account | None:
        with self._lock:
            if not self.accounts:
                return None
            n = len(self.accounts)
            for off in range(n):
                idx = (self.active_index + off) % n
                if self._usable(self.accounts[idx]):
                    self.active_index = idx
                    return self.accounts[idx]
            return None

    def mark_limited(self, acc: Account) -> None:
        with self._lock:
            if acc.disabled_until < time.time() + 60:
                acc.disabled_until = time.time() + 300
                acc.save()
            self.active_index = (self.active_index + 1) % max(
                1, len(self.accounts))


router = Router()
