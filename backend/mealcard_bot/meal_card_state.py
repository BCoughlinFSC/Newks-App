"""Durable period journal: never repeat an uncertain wallet submission."""
import json
import os
from pathlib import Path
from contextlib import contextmanager


@contextmanager
def local_run_lock(path):
    """Prevent overlapping runs from the same installation, including reports."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError as exc:
        raise RuntimeError("Another run is active, or a prior run stopped unexpectedly. "
                           "Reconcile pending transactions before removing state/run.lock.") from exc
    try:
        os.write(fd, str(os.getpid()).encode())
        os.fsync(fd)
        yield
    finally:
        os.close(fd)
        path.unlink()


class PendingSubmission(RuntimeError):
    """Transaction history must be reconciled before another submission."""


class RunState:
    def __init__(self, path, app_url):
        self.path = Path(path)
        self.data = {"app_url": app_url.rstrip("/"), "cards": {}}
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            if loaded["app_url"] != self.data["app_url"]:
                raise RuntimeError("Period journal belongs to a different reporting app")
            self.data = loaded

    def get(self, card):
        return self.data["cards"].get(str(card))

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(self.data, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, self.path)

    def pending(self, card, before, amount, target):
        if self.get(card):
            raise PendingSubmission("Card already has a submission in this period journal")
        self.data["cards"][str(card)] = {"status": "pending", "balance_before": before,
                                        "amount": amount, "target": target}
        self.save()

    def verified(self, card, after):
        self.data["cards"][str(card)].update(status="verified", balance_after=after)
        self.save()
