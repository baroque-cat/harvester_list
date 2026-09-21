#!/usr/bin/env python3

"""
Type-safe queue persistence system for task recovery and state management.
Handles serialization/deserialization with enum-based stage management.
"""

import json
import shutil
import signal
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Union

from core.enums import (
    PipelineStage,
    QueueStateField,
    QueueStateProvider,
    QueueStateStatus,
)
from core.models import ProviderTask
from stage.base import BasePipelineStage
from stage.factory import TaskFactory
from state.models import QueueStateMetrics
from storage.atomic import AtomicFileWriter
from storage.persistence import MultiResultManager
from tools.logger import get_logger

from .base import PeriodicTaskManager

logger = get_logger("manager")


@dataclass
class QueueConfig:
    """Lightweight configuration for queue management system"""

    persistence_dir: Path
    save_interval: float = 60.0
    max_age_hours: int = 24
    backup_count: int = 3
    compression_enabled: bool = False

    def __post_init__(self):
        """Ensure persistence directory exists"""
        if isinstance(self.persistence_dir, str):
            self.persistence_dir = Path(self.persistence_dir)
        self.persistence_dir.mkdir(parents=True, exist_ok=True)

    def get_queue_file_path(self, stage: PipelineStage) -> Path:
        """Get file path for specific stage queue"""
        filename = f"{stage.value}_queue.json"
        return self.persistence_dir / filename

    def get_backup_path(self, stage: PipelineStage, backup_index: int) -> Path:
        """Get backup file path for specific stage"""
        filename = f"{stage.value}_queue.backup.{backup_index}.json"
        return self.persistence_dir / filename

    def get_all_stage_files(self) -> Dict[PipelineStage, Path]:
        """Get all stage file paths as dictionary"""
        return {stage: self.get_queue_file_path(stage) for stage in PipelineStage}

    @classmethod
    def from_workspace(cls, workspace: str, **kwargs) -> "QueueConfig":
        """Create config from workspace directory"""
        persistence_dir = Path(workspace) / "queue_state"
        return cls(persistence_dir=persistence_dir, **kwargs)


@dataclass
class QueueStateInfo:
    """Type-safe state information for task queue persistence"""

    stage: PipelineStage
    provider: QueueStateProvider
    task_count: int = 0
    saved_at: datetime = None
    tasks: List[Dict[str, Any]] = None
    status: QueueStateStatus = QueueStateStatus.ACTIVE

    def __post_init__(self):
        if self.tasks is None:
            self.tasks = []
        if self.saved_at is None:
            self.saved_at = datetime.now()

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization"""
        return {
            QueueStateField.STAGE.value: self.stage.value,
            QueueStateField.PROVIDER.value: self.provider.value,
            QueueStateField.TASK_COUNT.value: self.task_count,
            QueueStateField.SAVED_AT.value: self.saved_at.isoformat(),
            QueueStateField.TASKS.value: self.tasks,
            QueueStateField.STATUS.value: self.status.value,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QueueStateInfo":
        """Create instance from dictionary with flexible timestamp handling"""
        # Handle timestamp conversion
        saved_at_raw = data[QueueStateField.SAVED_AT.value]
        if isinstance(saved_at_raw, str):
            saved_at = datetime.fromisoformat(saved_at_raw)
        elif isinstance(saved_at_raw, (int, float)):
            saved_at = datetime.fromtimestamp(saved_at_raw)
        else:
            saved_at = datetime.now()

        return cls(
            stage=PipelineStage(data[QueueStateField.STAGE.value]),
            provider=QueueStateProvider(data[QueueStateField.PROVIDER.value]),
            task_count=data[QueueStateField.TASK_COUNT.value],
            saved_at=saved_at,
            tasks=data[QueueStateField.TASKS.value],
            status=QueueStateStatus(data.get(QueueStateField.STATUS.value, QueueStateStatus.ACTIVE.value)),
        )


class QueueManager(PeriodicTaskManager):
    """Type-safe queue manager with enum-based stage management"""

    def __init__(
        self,
        workspace: str,
        save_interval: float = 60.0,
        shutdown_timeout: float = 5.0,
        backend: str = "memory",
        visibility_timeout_s: float = 300.0,
        max_age_hours: float = 24.0,
    ):
        # Initialize base class
        super().__init__("QueueManager", save_interval, shutdown_timeout)

        # Durable backend selection (fix-queue-persistence-under-load).  Under
        # ``sqlite`` the JSON snapshot paths stay untouched for the memory
        # backend, and are replaced by maintenance/import for durable stages.
        self.backend = str(backend).strip().lower()
        self.visibility_timeout_s = float(visibility_timeout_s)

        # Create configuration
        self.config = QueueConfig.from_workspace(
            workspace, save_interval=save_interval, max_age_hours=max_age_hours
        )

        # Type-safe stage file mapping
        self.stage_files = self.config.get_all_stage_files()

        # Thread safety
        self.lock = threading.Lock()

        # Stages to save (set by start_periodic_save/attach_stages)
        self.stages = None

        logger.info(f"Initialized type-safe queue manager at: {self.config.persistence_dir}")

    def attach_stages(self, stages: Dict[str, BasePipelineStage]) -> None:
        """Attach created stages so durable maintenance can reach their queues.

        Called by ``Pipeline`` right after ``_create_stages`` (design D11/D12);
        the facade never opens a durable db file itself.
        """
        self.stages = stages

    @staticmethod
    def _durable_queue(stage: BasePipelineStage):
        """Return the stage's durable queue, or None for the memory backend."""
        queue_obj = getattr(stage, "queue", None)
        if queue_obj is not None and hasattr(queue_obj, "checkpoint") and hasattr(queue_obj, "counts"):
            return queue_obj
        return None

    def _get_queue_filepath(self, stage: Union[PipelineStage, str]) -> Path:
        """Get filepath for a stage with type-safe enum support"""
        if isinstance(stage, str):
            # Legacy string support - convert to enum
            try:
                stage_enum = PipelineStage(stage)
                return self.stage_files[stage_enum]
            except ValueError:
                # Dynamic stage name fallback
                return self.config.persistence_dir / f"{stage}_queue.json"
        else:
            # Type-safe enum path
            return self.stage_files[stage]

    def start_periodic_save(self, stages: Dict[str, BasePipelineStage]) -> None:
        """Start periodic queue state saving"""
        self.stages = stages
        self.start()  # Use base class start method

    def save_queue_state(self, stage: Union[PipelineStage, str], task_list: List[ProviderTask]) -> None:
        """Type-safe save queue state for a specific stage"""
        # Convert string to enum for type safety
        if isinstance(stage, str):
            try:
                stage_enum = PipelineStage(stage)
            except ValueError:
                logger.error(f"Invalid stage name: {stage}")
                return
        else:
            stage_enum = stage

        if not task_list:
            # Save empty state to indicate stage is clean
            self._save_empty_state(stage_enum)
            return

        try:
            # Group tasks by provider
            provider_tasks = {}
            for task in task_list:
                provider = task.provider
                if provider not in provider_tasks:
                    provider_tasks[provider] = []
                provider_tasks[provider].append(task.to_dict())

            # Create type-safe queue state
            state = QueueStateInfo(
                stage=stage_enum,
                provider=QueueStateProvider.MULTI,
                task_count=len(task_list),
                saved_at=datetime.now(),
                tasks=[],
                status=QueueStateStatus.ACTIVE,
            )

            # Add all tasks
            for provider, provider_task_list in provider_tasks.items():
                state.tasks.extend(provider_task_list)

            # Save to file with type-safe path
            filepath = self._get_queue_filepath(stage_enum)
            content = json.dumps(state.to_dict(), indent=2, ensure_ascii=False)

            AtomicFileWriter.write_atomic(str(filepath), content)
            logger.info(f"Saved {len(task_list)} tasks for {stage_enum.value} stage")

        except Exception as e:
            logger.error(f"Failed to save queue state for {stage_enum.value}: {e}")

    def load_queue_state(self, stage: Union[PipelineStage, str]) -> List[ProviderTask]:
        """Type-safe load queue state for a specific stage"""
        # Convert string to enum for type safety
        if isinstance(stage, str):
            try:
                stage_enum = PipelineStage(stage)
            except ValueError:
                logger.error(f"Invalid stage name: {stage}")
                return []
        else:
            stage_enum = stage

        filepath = self._get_queue_filepath(stage_enum)
        if not filepath.exists():
            return []

        try:
            with open(filepath, encoding="utf-8") as f:
                data = json.load(f)

            # Try to load as new format first
            try:
                state = QueueStateInfo.from_dict(data)
                task_list = []
                for task_data in state.tasks:
                    try:
                        task = TaskFactory.from_dict(task_data)
                        task_list.append(task)
                    except Exception as e:
                        logger.warning(f"Failed to deserialize task: {e}")
                        continue

                # Check if state is recent
                age_hours = (datetime.now() - state.saved_at).total_seconds() / 3600

            except (KeyError, ValueError):
                # Fallback to legacy format
                task_list = []
                for task_data in data.get(QueueStateField.TASKS.value, []):
                    try:
                        task = TaskFactory.from_dict(task_data)
                        task_list.append(task)
                    except Exception as e:
                        logger.warning(f"Failed to deserialize task: {e}")
                        continue

                # Legacy age calculation
                saved_at = data.get(QueueStateField.SAVED_AT.value, 0)
                if isinstance(saved_at, str):
                    try:
                        saved_at = datetime.fromisoformat(saved_at).timestamp()
                    except ValueError:
                        # If string format is invalid, treat as timestamp
                        saved_at = float(saved_at) if saved_at else 0
                elif not isinstance(saved_at, (int, float)):
                    saved_at = 0
                age_hours = (time.time() - saved_at) / 3600

            if age_hours > self.config.max_age_hours:
                logger.warning(f"Queue state for {stage_enum.value} is {age_hours:.1f} hours old, skipping recovery")
                return []

            if task_list:
                logger.info(f"Loaded {len(task_list)} tasks for {stage_enum.value} stage (age: {age_hours:.1f}h)")

            return task_list
        except Exception as e:
            logger.error(f"Failed to load queue state for {stage_enum.value}: {e}")
            return []

    def save_all_queues(self, stages: Dict[str, BasePipelineStage]) -> None:
        """Save state for all queues with type-safe stage handling.

        Durable stages are maintained in place (expired-claim sweep + passive
        checkpoint) with no drain and no JSON serialization (design D12); the
        memory backend keeps the historical snapshot path verbatim.
        """
        for stage_name, stage in stages.items():
            durable = self._durable_queue(stage)
            if durable is not None:
                try:
                    swept = durable.sweep_expired_claims()
                    if swept:
                        logger.info(f"Swept {swept} expired claim(s) for {stage_name}")
                    durable.checkpoint()
                except Exception as e:
                    logger.error(f"Durable queue maintenance failed for {stage_name}: {e}")
                continue

            try:
                # Convert to enum for type safety
                stage_enum = PipelineStage(stage_name)

                tasks = stage.get_pending_tasks()
                self.save_queue_state(stage_enum, tasks)
            except ValueError:
                logger.warning(f"Unknown stage name: {stage_name}, skipping save")
                continue

    def load_all_queues(self) -> Dict[str, List[ProviderTask]]:
        """Load state for all queues with type-safe stage enumeration.

        Under ``sqlite`` the one-shot legacy importer runs and every durable
        stage recovers in place (constructor reclaim + age purge), so no
        re-``put_task`` list is returned for it (design D12/D14).  A stage that
        failed open to the memory backend still recovers through the legacy
        snapshot path.
        """
        if self.backend == "sqlite":
            self._import_legacy_snapshots()
            all_tasks = {}
            for stage_enum in PipelineStage:
                stage = (self.stages or {}).get(stage_enum.value)
                if self._durable_queue(stage) is not None:
                    continue  # already recovered in place
                task_list = self.load_queue_state(stage_enum)
                if task_list:
                    all_tasks[stage_enum.value] = task_list

            total_tasks = sum(len(task_list) for task_list in all_tasks.values())
            if total_tasks > 0:
                logger.info(f"Loaded {total_tasks} total tasks from previous session")
            return all_tasks

        all_tasks = {}

        for stage_enum in PipelineStage:
            task_list = self.load_queue_state(stage_enum)
            if task_list:
                all_tasks[stage_enum.value] = task_list

        total_tasks = sum(len(task_list) for task_list in all_tasks.values())
        if total_tasks > 0:
            logger.info(f"Loaded {total_tasks} total tasks from previous session")

        return all_tasks

    # ------------------------------------------------------------------
    # One-shot legacy importer (design D13)
    # ------------------------------------------------------------------
    @staticmethod
    def _utc_stamp() -> str:
        return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")

    def _parse_snapshot_payload(self, data: Dict[str, Any]) -> "tuple[List[ProviderTask], float]":
        """Parse both known envelope shapes, returning (tasks, age_hours).

        Mirrors ``load_queue_state`` exactly: current typed envelope first, the
        legacy fallback (numeric ``saved_at`` + bare ``tasks``) second.  Per-task
        deserialization failures are skipped loudly, never fatal.
        """
        try:
            state = QueueStateInfo.from_dict(data)
            task_list = []
            for task_data in state.tasks:
                try:
                    task_list.append(TaskFactory.from_dict(task_data))
                except Exception as e:
                    logger.warning(f"Failed to deserialize task: {e}")
                    continue
            age_hours = (datetime.now() - state.saved_at).total_seconds() / 3600
            return task_list, age_hours
        except (KeyError, ValueError, TypeError):
            task_list = []
            for task_data in data.get(QueueStateField.TASKS.value, []):
                try:
                    task_list.append(TaskFactory.from_dict(task_data))
                except Exception as e:
                    logger.warning(f"Failed to deserialize task: {e}")
                    continue

            saved_at = data.get(QueueStateField.SAVED_AT.value, 0)
            if isinstance(saved_at, str):
                try:
                    saved_at = datetime.fromisoformat(saved_at).timestamp()
                except ValueError:
                    saved_at = float(saved_at) if saved_at else 0
            elif not isinstance(saved_at, (int, float)):
                saved_at = 0
            age_hours = (time.time() - saved_at) / 3600
            return task_list, age_hours

    def _import_legacy_snapshots(self) -> None:
        """Import legacy ``{stage}_queue.json`` snapshots once, then neutralize.

        Idempotent by two guards: the durable store must be empty, and the file
        is renamed on success.  Aged snapshots are parked ``.expired-``; corrupt
        files are skipped loudly and left in place.
        """
        for stage_name, stage in (self.stages or {}).items():
            durable = self._durable_queue(stage)
            if durable is None:
                continue
            try:
                stage_enum = PipelineStage(stage_name)
            except ValueError:
                continue

            json_path = self._get_queue_filepath(stage_enum)
            if not json_path.exists():
                continue

            # Zero-rows guard: never double-import into a non-empty store.
            try:
                counts = durable.counts()
            except Exception as e:
                logger.error(f"Failed to inspect durable queue for {stage_name}: {e}")
                continue
            if counts.get("pending", 0) or counts.get("claimed", 0):
                continue

            try:
                with open(json_path, encoding="utf-8") as f:
                    data = json.load(f)
            except Exception as e:
                logger.warning(
                    f"Failed to parse legacy queue snapshot for {stage_name}: {e}; leaving file in place"
                )
                continue

            if not isinstance(data, dict):
                logger.warning(f"Legacy queue snapshot for {stage_name} is not an object; skipped")
                continue

            tasks, age_hours = self._parse_snapshot_payload(data)
            if age_hours > float(self.config.max_age_hours):
                dest = json_path.with_name(f"{json_path.name}.expired-{self._utc_stamp()}")
                try:
                    json_path.rename(dest)
                except OSError as e:
                    logger.error(f"Failed to park aged snapshot for {stage_name}: {e}")
                    continue
                logger.warning(
                    f"Legacy queue snapshot for {stage_name} is {age_hours:.1f}h old "
                    f"(> {self.config.max_age_hours}h); parked as {dest.name}, not imported"
                )
                continue

            if not tasks:
                continue

            try:
                durable.bulk_insert(tasks)
            except Exception as e:
                logger.error(f"Failed to import legacy snapshot for {stage_name}: {e}")
                continue

            dest = json_path.with_name(f"{json_path.name}.imported-{self._utc_stamp()}")
            try:
                json_path.rename(dest)
            except OSError as e:
                logger.error(f"Imported snapshot for {stage_name} but failed to rename it: {e}")
                continue
            logger.info(f"Imported {len(tasks)} task(s) from legacy snapshot for {stage_name} as {dest.name}")

    def clear_queue_state(self, stage: Union[PipelineStage, str]) -> None:
        """Clear saved state for a stage with type-safe enum support"""
        if isinstance(stage, str):
            try:
                stage_enum = PipelineStage(stage)
            except ValueError:
                logger.error(f"Invalid stage name: {stage}")
                return
        else:
            stage_enum = stage

        filepath = self._get_queue_filepath(stage_enum)
        if filepath.exists():
            try:
                filepath.unlink()
                logger.info(f"Cleared queue state for {stage_enum.value}")
            except Exception as e:
                logger.error(f"Failed to clear queue state for {stage_enum.value}: {e}")

    def clear_all_states(self) -> None:
        """Clear all saved queue states using type-safe enumeration"""
        for stage_enum in PipelineStage:
            self.clear_queue_state(stage_enum)

    def get_state_info(self) -> Dict[str, QueueStateMetrics]:
        """Get type-safe information about saved queue states"""
        info = {}

        for stage_enum in PipelineStage:
            # Durable stages report live counts + db file size, not JSON lists.
            stage = (self.stages or {}).get(stage_enum.value)
            durable = self._durable_queue(stage) if stage is not None else None
            if durable is not None:
                try:
                    counts = durable.counts()
                    pending = int(counts.get("pending", 0))
                    db_path = self.config.persistence_dir / f"{stage_enum.value}_queue.sqlite"
                    size = db_path.stat().st_size if db_path.exists() else 0
                    status_enum = QueueStateStatus.ACTIVE if pending else QueueStateStatus.EMPTY
                    metrics = QueueStateMetrics(
                        stage=stage_enum.value,
                        tasks=pending,
                        saved_at=datetime.now(),
                        file_size=size,
                        status=status_enum,
                    )
                    metrics.calculate_age()
                    info[stage_enum.value] = metrics
                except Exception as e:
                    info[stage_enum.value] = QueueStateMetrics(
                        stage=stage_enum.value,
                        tasks=0,
                        saved_at=datetime.now(),
                        file_size=0,
                        status=QueueStateStatus.ERROR,
                        error_message=str(e),
                    )
                continue

            filepath = self._get_queue_filepath(stage_enum)

            if filepath.exists():
                try:
                    with open(filepath, encoding="utf-8") as f:
                        data = json.load(f)

                    # Try new format first
                    try:
                        state = QueueStateInfo.from_dict(data)
                        saved_at = state.saved_at
                        task_count = state.task_count
                        status = state.status.value
                    except (KeyError, ValueError):
                        # Fallback to legacy format
                        saved_at_raw = data.get(QueueStateField.SAVED_AT.value, 0)
                        if isinstance(saved_at_raw, str):
                            try:
                                saved_at = datetime.fromisoformat(saved_at_raw)
                            except ValueError:
                                # If string format is invalid, treat as timestamp
                                saved_at = datetime.fromtimestamp(float(saved_at_raw) if saved_at_raw else 0)
                        elif isinstance(saved_at_raw, (int, float)):
                            saved_at = datetime.fromtimestamp(saved_at_raw)
                        else:
                            saved_at = datetime.now()
                        task_count = data.get(QueueStateField.TASK_COUNT.value, 0)
                        status = QueueStateStatus.ACTIVE.value

                    # Convert string status to enum
                    if isinstance(status, str):
                        try:
                            status_enum = QueueStateStatus(status)
                        except ValueError:
                            status_enum = QueueStateStatus.UNKNOWN
                    else:
                        status_enum = status

                    metrics = QueueStateMetrics(
                        stage=stage_enum.value,
                        tasks=task_count,
                        saved_at=saved_at,
                        file_size=filepath.stat().st_size,
                        status=status_enum,
                    )
                    metrics.calculate_age()
                    info[stage_enum.value] = metrics

                except Exception as e:
                    # Create error metrics
                    info[stage_enum.value] = QueueStateMetrics(
                        stage=stage_enum.value,
                        tasks=0,
                        saved_at=datetime.now(),
                        file_size=0,
                        status=QueueStateStatus.ERROR,
                        error_message=str(e),
                    )
            else:
                # Create empty metrics
                info[stage_enum.value] = QueueStateMetrics(
                    stage=stage_enum.value,
                    tasks=0,
                    saved_at=datetime.now(),
                    file_size=0,
                    status=QueueStateStatus.EMPTY,
                )

        return info

    def get_queue_metrics(self, stage: Union[PipelineStage, str]) -> QueueStateMetrics:
        """Get queue metrics for a specific stage"""
        if isinstance(stage, str):
            try:
                stage_enum = PipelineStage(stage)
            except ValueError:
                logger.error(f"Invalid stage name: {stage}")
                return QueueStateMetrics(
                    stage=stage,
                    tasks=0,
                    saved_at=datetime.now(),
                    file_size=0,
                    status=QueueStateStatus.ERROR,
                    error_message=f"Invalid stage: {stage}",
                )
        else:
            stage_enum = stage

        state_info = self.get_state_info()
        return state_info.get(
            stage_enum.value,
            QueueStateMetrics(
                stage=stage_enum.value,
                tasks=0,
                saved_at=datetime.now(),
                file_size=0,
                status=QueueStateStatus.EMPTY,
            ),
        )

    def _execute_periodic_task(self) -> None:
        """Execute periodic queue save task"""
        if self.stages:
            self.save_all_queues(self.stages)

    def _save_empty_state(self, stage: PipelineStage) -> None:
        """Save empty state to indicate clean stage with type safety"""
        filepath = self._get_queue_filepath(stage)
        try:
            # Create empty state with type safety
            state = QueueStateInfo(
                stage=stage,
                provider=QueueStateProvider.MULTI,
                task_count=0,
                saved_at=datetime.now(),
                tasks=[],
                status=QueueStateStatus.EMPTY,
            )

            content = json.dumps(state.to_dict(), indent=2)
            AtomicFileWriter.write_atomic(str(filepath), content)

        except Exception as e:
            logger.error(f"Failed to save empty state for {stage.value}: {e}")

    def _extract_tasks_from_queue(self, stage) -> List[ProviderTask]:
        """Extract tasks from a queue object (fallback method)"""
        task_list = []
        # Try to get tasks without removing them
        temp_tasks = []

        # Extract all tasks
        while not stage.queue.empty():
            try:
                task = stage.queue.get_nowait()
                if isinstance(task, ProviderTask):
                    task_list.append(task)
                    temp_tasks.append(task)
            except:
                break

        # Put tasks back
        for task in temp_tasks:
            try:
                stage.queue.put_nowait(task)
            except:
                pass

        return task_list


class GracefulShutdown:
    """Handles graceful shutdown with queue state preservation"""

    def __init__(
        self, queue_manager: QueueManager, result_manager: MultiResultManager, stages: Dict[str, BasePipelineStage]
    ):
        self.queue_manager = queue_manager
        self.result_manager = result_manager
        self.stages = stages
        self.shutdown_timeout = 30

        # Register signal handlers
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)

        logger.info("Registered graceful shutdown handlers")

    def _signal_handler(self, signum, _):
        """Handle shutdown signals"""
        logger.info(f"Received signal {signum}, initiating graceful shutdown...")
        self.shutdown()

    def shutdown(self) -> None:
        """Perform graceful shutdown"""
        start_time = time.time()

        try:
            # 1. Stop accepting new tasks
            logger.info("Stopping task acceptance...")
            for stage in self.stages.values():
                stage.stop_accepting()

            # 2. Flush all result buffers
            logger.info("Flushing result buffers...")
            self.result_manager.flush_all()

            # 3. Save all queue states
            logger.info("Saving queue states...")
            self.queue_manager.save_all_queues(self.stages)

            # 4. Wait for current tasks to complete (with timeout)
            remaining_time = self.shutdown_timeout - (time.time() - start_time)
            if remaining_time > 0:
                logger.info(f"Waiting up to {remaining_time:.1f}s for tasks to complete...")
                self._wait_for_completion(remaining_time)

            # 5. Force save final state
            logger.info("Final state save...")
            self.queue_manager.save_all_queues(self.stages)

            # 6. Stop managers
            self.queue_manager.stop()
            self.result_manager.stop_all()

            logger.info("Graceful shutdown completed")

        except Exception as e:
            logger.error(f"Error during graceful shutdown: {e}")

        finally:
            sys.exit(0)

    def _wait_for_completion(self, timeout: float) -> None:
        """Wait for tasks to complete"""
        start_time = time.time()

        while time.time() - start_time < timeout:
            all_idle = True

            for stage in self.stages.values():
                if stage.is_busy():
                    all_idle = False
                    break
                elif not stage.queue.empty():
                    all_idle = False
                    break

            if all_idle:
                logger.info("All stages idle, shutdown can proceed")
                break

            time.sleep(1)


if __name__ == "__main__":
    # Create temporary workspace
    workspace = tempfile.mkdtemp()
    logger.info(f"Testing in workspace: {workspace}")

    try:
        # Create queue manager
        qm = QueueManager(workspace, save_interval=2)

        # Create some test tasks
        test_tasks = [
            TaskFactory.create_search_task("openai", '"test"', 1),
            TaskFactory.create_search_task("gemini", '"test"', 2),
            TaskFactory.create_acquisition_task("openai", "http://example.com", {"key_pattern": "sk-.*"}),
        ]

        # Save queue state
        qm.save_queue_state("search", test_tasks[:2])
        qm.save_queue_state("gather", test_tasks[2:])

        # Load queue state
        loaded_search = qm.load_queue_state("search")
        loaded_gather = qm.load_queue_state("gather")

        logger.info(f"Saved {len(test_tasks[:2])} search tasks, loaded {len(loaded_search)}")
        logger.info(f"Saved {len(test_tasks[2:])} gather tasks, loaded {len(loaded_gather)}")

        # Check state info
        info = qm.get_state_info()
        logger.info(f"State info: {info}")

        # Stop manager
        qm.stop()

        logger.info("Queue manager test completed!")
    finally:
        # Cleanup
        shutil.rmtree(workspace)
