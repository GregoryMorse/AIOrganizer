from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QObject, QThread, Signal, Slot
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QProgressBar,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ai_organizer.adapters.filesystem import (
    DiscoveryProgress,
    FileSystemInventory,
    MetadataIndexer,
    ScanCancelled,
    content_fingerprint,
    metadata_fingerprint,
)
from ai_organizer.application.services import InventoryRun
from ai_organizer.domain.models import ItemSnapshot, SourceRoot, new_id


@dataclass(frozen=True, slots=True)
class InventoryScanResult:
    runs: tuple[InventoryRun, ...]
    metadata_updates: dict[tuple[str, str], dict[str, Any]]
    capabilities: dict[str, Any]


class InventoryScanWorker(QObject):
    progress = Signal(dict)
    completed = Signal(object)
    failed = Signal(str)
    cancelled = Signal()

    def __init__(
        self,
        sources: tuple[SourceRoot, ...],
        cached_metadata: dict[tuple[str, str], dict[str, Any]],
        fingerprint_mode: str = "none",
    ) -> None:
        super().__init__()
        self.sources = sources
        self.cached_metadata = cached_metadata
        self.fingerprint_mode = fingerprint_mode
        self._cancelled = False
        self._last_progress = 0.0

    def request_cancel(self) -> None:
        self._cancelled = True

    @Slot()
    def run(self) -> None:
        scanner = FileSystemInventory()
        indexer = MetadataIndexer()
        runs: list[InventoryRun] = []
        capabilities: dict[str, Any] = {}
        try:
            discovered_items = 0
            discovered_files = 0
            discovered_bytes = 0
            source_total = len(self.sources)
            for source_index, source in enumerate(self.sources, 1):
                if self._cancelled:
                    raise ScanCancelled("Inventory scan cancelled")
                capability = scanner.capabilities(source.path)
                if not capability.reachable:
                    raise FileNotFoundError(source.path)
                capabilities[source.id] = capability
                base_items = discovered_items
                base_files = discovered_files
                base_bytes = discovered_bytes

                def report(
                    value: DiscoveryProgress,
                    current_source: SourceRoot = source,
                    current_source_index: int = source_index,
                    item_offset: int = base_items,
                    file_offset: int = base_files,
                    byte_offset: int = base_bytes,
                ) -> None:
                    self._emit_progress(
                        {
                            "phase": "discovering",
                            "source_name": current_source.name,
                            "source_index": current_source_index,
                            "source_total": source_total,
                            "discovered_items": item_offset + value.item_count,
                            "discovered_files": file_offset + value.file_count,
                            "discovered_bytes": byte_offset + value.discovered_bytes,
                            "current_path": str(current_source.path / value.current_path),
                        }
                    )

                items = scanner.scan(
                    source.id,
                    source.path,
                    source.exclusions,
                    progress=report,
                    cancelled=lambda: self._cancelled,
                )
                discovered_items += len(items)
                discovered_files += sum(not item.is_dir for item in items)
                discovered_bytes += sum(item.size for item in items if not item.is_dir)
                runs.append(InventoryRun(new_id("snapshot"), source.id, tuple(items)))

            total_items = sum(len(run.items) for run in runs)
            total_bytes = sum(
                item.size for run in runs for item in run.items if not item.is_dir
            )
            processed_items = 0
            processed_bytes = 0
            metadata_updates: dict[tuple[str, str], dict[str, Any]] = {}
            metadata_by_key: dict[tuple[str, str], dict[str, Any]] = {}
            sources_by_id = {source.id: source for source in self.sources}
            self._emit_progress(
                {
                    "phase": "metadata",
                    "processed_items": 0,
                    "total_items": total_items,
                    "processed_bytes": 0,
                    "total_bytes": total_bytes,
                    "current_path": "Preparing metadata extraction…",
                },
                force=True,
            )
            pending: list[tuple[ItemSnapshot, Path]] = []
            for run in runs:
                for item in run.items:
                    if self._cancelled:
                        raise ScanCancelled("Inventory scan cancelled")
                    key = (item.root_id, item.relative_path)
                    metadata = self._cached(item, source.path / item.relative_path)
                    source = sources_by_id[item.root_id]
                    if metadata is not None:
                        metadata_by_key[key] = metadata
                    else:
                        pending.append((item, source.path / item.relative_path))
                        continue
                    processed_items += 1
                    if not item.is_dir:
                        processed_bytes += max(0, item.size)
                    self._emit_progress(
                        {
                            "phase": "metadata",
                            "processed_items": processed_items,
                            "total_items": total_items,
                            "processed_bytes": processed_bytes,
                            "total_bytes": total_bytes,
                            "current_path": str(source.path / item.relative_path),
                        },
                        force=processed_items == total_items,
                    )
            with ThreadPoolExecutor(
                max_workers=_metadata_worker_count(), thread_name_prefix="aiorganizer-metadata"
            ) as executor:
                futures = {
                    executor.submit(
                        _extract_metadata,
                        indexer,
                        path,
                        item,
                        self.fingerprint_mode,
                    ): (item, path)
                    for item, path in pending
                }
                for future in as_completed(futures):
                    if self._cancelled:
                        for outstanding in futures:
                            outstanding.cancel()
                        raise ScanCancelled("Inventory scan cancelled")
                    item, path = futures[future]
                    metadata = future.result()
                    key = (item.root_id, item.relative_path)
                    metadata_by_key[key] = metadata
                    metadata_updates[key] = metadata
                    processed_items += 1
                    if not item.is_dir:
                        processed_bytes += max(0, item.size)
                    self._emit_progress(
                        {
                            "phase": "metadata",
                            "processed_items": processed_items,
                            "total_items": total_items,
                            "processed_bytes": processed_bytes,
                            "total_bytes": total_bytes,
                            "current_path": str(path),
                        },
                        force=processed_items == total_items,
                    )
            enriched_runs = [
                InventoryRun(
                    run.id,
                    run.root_id,
                    tuple(
                        replace(
                            item,
                            metadata=metadata_by_key[(item.root_id, item.relative_path)],
                        )
                        for item in run.items
                    ),
                )
                for run in runs
            ]
            self.completed.emit(
                InventoryScanResult(tuple(enriched_runs), metadata_updates, capabilities)
            )
        except ScanCancelled:
            self.cancelled.emit()
        except Exception as error:
            self.failed.emit(f"{type(error).__name__}: {error}")

    def _cached(self, item: ItemSnapshot, path: Path) -> dict[str, Any] | None:
        record = self.cached_metadata.get((item.root_id, item.relative_path))
        if not record or record.get("fingerprint") != metadata_fingerprint(item):
            return None
        payload = dict(record.get("payload", {}))
        if self.fingerprint_mode in {"crc32", "sha256"}:
            stored = payload.get("content_fingerprint", {})
            if stored.get("algorithm") != self.fingerprint_mode:
                return None
            if content_fingerprint(path, self.fingerprint_mode)["value"] != stored.get("value"):
                return None
        return {
            **payload,
            "_cache": {
                "updated_at": record.get("updated_at", ""),
                "fresh": True,
                "validated_by": "size+modified_ns",
            },
        }

    def _emit_progress(self, payload: dict[str, Any], force: bool = False) -> None:
        now = time.monotonic()
        if force or now - self._last_progress >= 0.05:
            self._last_progress = now
            self.progress.emit(payload)


class InventoryScanDialog(QDialog):
    def __init__(
        self,
        sources: tuple[SourceRoot, ...],
        cached_metadata: dict[tuple[str, str], dict[str, Any]],
        fingerprint_mode: str = "none",
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Scanning inventory")
        self.setModal(True)
        self.setMinimumWidth(620)
        self.result_value: InventoryScanResult | None = None
        self.error_message = ""
        layout = QVBoxLayout(self)
        self.phase = QLabel("Starting inventory scan…")
        self.phase.setWordWrap(True)
        layout.addWidget(self.phase)
        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        layout.addWidget(self.progress_bar)
        self.current = QLabel("")
        self.current.setWordWrap(True)
        layout.addWidget(self.current)
        self.buttons = QDialogButtonBox()
        self.cancel_button = QPushButton("Cancel")
        self.buttons.addButton(self.cancel_button, QDialogButtonBox.ButtonRole.RejectRole)
        layout.addWidget(self.buttons)

        self.thread = QThread(self)
        self.worker = InventoryScanWorker(sources, cached_metadata, fingerprint_mode)
        self.worker.moveToThread(self.thread)
        self.thread.started.connect(self.worker.run)
        self.worker.progress.connect(self._progress)
        self.worker.completed.connect(self._completed)
        self.worker.failed.connect(self._failed)
        self.worker.cancelled.connect(self._was_cancelled)
        self.worker.completed.connect(self.thread.quit)
        self.worker.failed.connect(self.thread.quit)
        self.worker.cancelled.connect(self.thread.quit)
        self.cancel_button.clicked.connect(self._cancel)

    def start(self) -> int:
        self.thread.start()
        code = self.exec()
        self.thread.quit()
        self.thread.wait()
        return code

    @Slot(dict)
    def _progress(self, value: dict[str, Any]) -> None:
        if value.get("phase") == "discovering":
            self.progress_bar.setRange(0, 0)
            self.phase.setText(
                f"Discovering source {value['source_index']}/{value['source_total']}: "
                f"{value['source_name']} — {value['discovered_files']:,} files, "
                f"{_format_bytes(int(value['discovered_bytes']))} found"
            )
        else:
            processed_items = int(value.get("processed_items", 0))
            total_items = int(value.get("total_items", 0))
            processed_bytes = int(value.get("processed_bytes", 0))
            total_bytes = int(value.get("total_bytes", 0))
            if total_bytes > 0:
                self.progress_bar.setRange(0, 10_000)
                self.progress_bar.setValue(min(10_000, int(processed_bytes * 10_000 / total_bytes)))
            else:
                self.progress_bar.setRange(0, max(1, total_items))
                self.progress_bar.setValue(processed_items)
            self.phase.setText(
                f"Extracting metadata: {processed_items:,}/{total_items:,} items — "
                f"{_format_bytes(processed_bytes)}/{_format_bytes(total_bytes)}"
            )
        self.current.setText(str(value.get("current_path", "")))

    @Slot(object)
    def _completed(self, value: object) -> None:
        self.result_value = value if isinstance(value, InventoryScanResult) else None
        self.accept()

    @Slot(str)
    def _failed(self, message: str) -> None:
        self.error_message = message
        QDialog.reject(self)

    @Slot()
    def _was_cancelled(self) -> None:
        QDialog.reject(self)

    @Slot()
    def _cancel(self) -> None:
        self.cancel_button.setEnabled(False)
        self.cancel_button.setText("Cancelling…")
        self.phase.setText("Cancelling safely; no partial inventory will be saved…")
        self.worker.request_cancel()

    def reject(self) -> None:
        if self.thread.isRunning() and not self._cancelled_requested():
            self._cancel()
            return
        super().reject()

    def _cancelled_requested(self) -> bool:
        return not self.cancel_button.isEnabled()


def _format_bytes(value: int) -> str:
    amount = float(max(0, value))
    for suffix in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or suffix == "TiB":
            return f"{amount:,.1f} {suffix}" if suffix != "B" else f"{int(amount):,} B"
        amount /= 1024
    return f"{int(value):,} B"


def _metadata_worker_count() -> int:
    try:
        return max(1, min(8, int(os.getenv("AIORGANIZER_METADATA_WORKERS", "4"))))
    except ValueError:
        return 4


def _extract_metadata(
    indexer: MetadataIndexer,
    path: Path,
    item: ItemSnapshot,
    fingerprint_mode: str,
) -> dict[str, Any]:
    metadata = indexer.extract(path, item)
    if not item.is_dir and fingerprint_mode in {"crc32", "sha256"}:
        metadata["content_fingerprint"] = content_fingerprint(path, fingerprint_mode)
    return metadata
