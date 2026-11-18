"""Enhanced Batch Queue Manager (V2, 2026-11): the data model behind the batch
Treeview in the GUI (spec §10).

BatchItem tracks per-video status / progress / output; BatchManager owns the
queue with reorder, remove, retry and summary. The GUI thread renders it into a
ttk.Treeview; the worker thread mutates items and pushes refresh events.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path


class BatchItem:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.status: str = "pending"   # pending, processing, done, failed, skipped
        self.progress: float = 0.0     # 0..100 for the current video
        self.error_message: str = ""
        self.output_path: Path | None = None
        self.duration: float = 0.0     # seconds spent processing (for the report)
        self.started_at: float = 0.0

    # ---------------------------------------------------------------- helpers
    def start(self) -> None:
        self.status = "processing"
        self.progress = 0.0
        self.error_message = ""
        self.started_at = time.monotonic()

    def finish(self, output: Path | None) -> None:
        self.status = "done"
        self.progress = 100.0
        self.output_path = output
        self.duration = time.monotonic() - self.started_at if self.started_at else self.duration

    def fail(self, error: str) -> None:
        self.status = "failed"
        self.error_message = str(error or "unknown error")
        self.duration = time.monotonic() - self.started_at if self.started_at else self.duration

    def skip(self, reason: str = "already completed") -> None:
        self.status = "skipped"
        self.error_message = reason

    def retryable(self) -> bool:
        return self.status in ("failed", "pending", "skipped")

    @property
    def display_duration(self) -> str:
        seconds = int(self.duration)
        if seconds <= 0:
            return "--:--"
        h, rem = divmod(seconds, 3600)
        m, s = divmod(rem, 60)
        return f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"

    def row_values(self) -> tuple[str, str, str, str, str]:
        icons = {"pending": "⏳ Pending", "processing": "🔄 Processing", "done": "✅ Done",
                 "failed": "❌ Failed", "skipped": "⏭️ Skipped"}
        return (
            self.path.name,
            icons.get(self.status, self.status),
            f"{int(round(self.progress))}%",
            self.display_duration,
            self.error_message[:80],
        )


class BatchManager:
    def __init__(self) -> None:
        self.queue: list[BatchItem] = []
        self.is_running: bool = False
        self.skip_completed: bool = True
        self._lock = threading.RLock()

    # ---------------------------------------------------------------- queries
    def __len__(self) -> int:
        return len(self.queue)

    def item_at(self, index: int) -> BatchItem | None:
        with self._lock:
            if 0 <= index < len(self.queue):
                return self.queue[index]
        return None

    def index_of(self, item: BatchItem) -> int:
        with self._lock:
            return self.queue.index(item)

    def get_next_pending(self) -> BatchItem | None:
        with self._lock:
            for item in self.queue:
                if item.status == "pending":
                    return item
        return None

    def failed_items(self) -> list[BatchItem]:
        with self._lock:
            return [i for i in self.queue if i.status == "failed"]

    # ---------------------------------------------------------------- mutations
    def add_files(self, paths: list[Path]) -> list[BatchItem]:
        with self._lock:
            known = {i.path.resolve() for i in self.queue}
            added = []
            for p in paths:
                path = Path(p)
                if path.resolve() not in known:
                    item = BatchItem(path)
                    self.queue.append(item)
                    added.append(item)
        return added

    def remove_item(self, index: int) -> BatchItem | None:
        with self._lock:
            if 0 <= index < len(self.queue):
                return self.queue.pop(index)
        return None

    def move_up(self, index: int) -> bool:
        with self._lock:
            if 1 <= index < len(self.queue):
                self.queue[index - 1], self.queue[index] = self.queue[index], self.queue[index - 1]
                return True
        return False

    def move_down(self, index: int) -> bool:
        with self._lock:
            if 0 <= index < len(self.queue) - 1:
                self.queue[index + 1], self.queue[index] = self.queue[index], self.queue[index + 1]
                return True
        return False

    def move_to(self, index: int, target: int) -> bool:
        """Drag-to-reorder support (spec §10 BQ-05)."""
        with self._lock:
            if index == target or not (0 <= index < len(self.queue)) or not (0 <= target < len(self.queue)):
                return False
            item = self.queue.pop(index)
            self.queue.insert(target, item)
            return True

    def clear_finished(self) -> None:
        with self._lock:
            self.queue = [i for i in self.queue if i.status not in ("done", "skipped")]

    def retry_failed(self) -> int:
        with self._lock:
            count = 0
            for item in self.queue:
                if item.status in ("failed", "skipped"):
                    item.status = "pending"
                    item.progress = 0.0
                    item.error_message = ""
                    count += 1
        return count

    def mark_done(self, item: BatchItem, output: Path) -> None:
        item.finish(Path(output))

    def mark_failed(self, item: BatchItem, error: str) -> None:
        item.fail(error)

    def reset_all(self) -> None:
        with self._lock:
            for item in self.queue:
                item.status = "pending"
                item.progress = 0.0
                item.error_message = ""
                item.output_path = None
                item.duration = 0.0

    # ---------------------------------------------------------------- report
    def get_summary(self) -> dict:
        with self._lock:
            by = {"pending": 0, "processing": 0, "done": 0, "failed": 0, "skipped": 0}
            for item in self.queue:
                by[item.status] = by.get(item.status, 0) + 1
            return {"total": len(self.queue), **by}
