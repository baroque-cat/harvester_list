# run-lock Specification

## Purpose

Provides mutual exclusion between the pipeline and destructive commands (purge, future restore) via a filesystem lock file at `<workspace>/.harvester.lock`. The lock is acquired atomically with `O_CREAT|O_EXCL|O_WRONLY` and mode 0600, contains only `{"pid": <int>, "boot_ts": <float>, "holder": "pipeline"|"purge"}`, and is removed on graceful shutdown. A live lock (pid alive via `os.kill(pid, 0)`) blocks the second acquirer unconditionally (fail-CLOSED). A stale lock (dead pid) is taken over with a loud INFO log message.

This is the pipeline half of the mutual exclusion mechanism (slice 4.1, wave 4). The purge half lands in slice 4.2.

## Requirements

### Requirement: Atomic lock creation

The lock file SHALL be created atomically using `os.open(path, O_CREAT|O_EXCL|O_WRONLY, 0o600)`. The file mode SHALL be 0600 (umask discipline, AGENTS.md §1). The content SHALL be JSON with exactly three fields: `pid` (int, `os.getpid()`), `boot_ts` (float, `time.time()`), `holder` (str, `"pipeline"` or `"purge"`). No secrets SHALL appear in the lock file or in log messages derived from it.

#### Scenario: Pipeline acquires lock on start

- **WHEN** the pipeline starts (`Pipeline._on_start`)
- **THEN** `<workspace>/.harvester.lock` is created with mode 0600, containing the current pid, boot timestamp, and `holder="pipeline"`

#### Scenario: Lock file contains no secrets

- **WHEN** the lock file is read
- **THEN** it contains only `pid`, `boot_ts`, `holder` — no tokens, sessions, cookies, or other credentials

### Requirement: Fail-CLOSED mutual exclusion

When `acquire_lock` is called and a lock file exists with a live pid (verified via `os.kill(pid, 0)`), it SHALL raise `LockHeldError(RuntimeError)`. `PermissionError` from `os.kill` SHALL be treated as "alive" (fail-CLOSED: another process exists but we cannot signal it). The error message SHALL include the pid, holder, and boot_ts from the existing lock.

#### Scenario: Second pipeline refuses when first is live

- **WHEN** a pipeline is running (lock exists, pid is alive)
- **AND** a second pipeline attempts to start
- **THEN** the second pipeline raises `LockHeldError` and does NOT proceed

#### Scenario: Purge refuses when pipeline is live

- **WHEN** a pipeline is running (lock exists with `holder="pipeline"`, pid is alive)
- **AND** a purge command attempts to acquire the lock
- **THEN** the purge command raises `LockHeldError` and does NOT proceed (purge half lands in slice 4.2)

### Requirement: Stale lock takeover

When `acquire_lock` is called and a lock file exists with a dead pid (`os.kill(pid, 0)` raises `ProcessLookupError`), it SHALL log a loud INFO message `"stale lock ignored (holder=..., pid=...)"`, atomically remove the stale lock (`os.unlink`), and create a new lock. A race between two processes attempting stale takeover simultaneously is acceptable: one succeeds at `O_EXCL`, the other sees a live lock and raises `LockHeldError`.

#### Scenario: Dead pid → takeover with loud INFO

- **WHEN** a lock file exists with a dead pid
- **THEN** an INFO log message `"stale lock ignored"` is emitted
- **AND** the lock file is replaced with the new process's pid/holder/boot_ts

#### Scenario: Crash recovery

- **WHEN** a pipeline crashes without calling `release_lock` (lock file remains)
- **AND** the next pipeline starts
- **THEN** the stale lock is taken over and the new pipeline proceeds normally

### Requirement: Corrupt lock refuses with typed error

When `acquire_lock` finds a lock file that cannot be parsed as a live/stale record (corrupt JSON or missing required fields), it SHALL raise `LockHeldError` naming the corrupt/unreadable condition — never a bare `FileExistsError` — and SHALL NOT delete the ambiguous file (fail-CLOSED; 05 §5.3.3: ambiguity refuses BEFORE any deletion). Manual removal by the operator is the documented recovery.

#### Scenario: Corrupt lock file → typed refusal, file preserved

- **WHEN** `.harvester.lock` exists but contains invalid JSON (or lacks pid/holder)
- **AND** `acquire_lock` is called
- **THEN** `LockHeldError` is raised with a message naming the corrupt/unreadable condition
- **AND** the lock file is still present afterwards (not silently removed)

### Requirement: Pid-guarded release

`release_lock(workspace)` SHALL remove the lock file ONLY if the pid in the file matches `os.getpid()`. It SHALL NOT remove a lock held by another process. It SHALL be idempotent: calling it when no lock file exists returns `False` without raising.

#### Scenario: Release removes own lock

- **WHEN** the lock file contains the current process's pid
- **THEN** `release_lock` removes the file and returns `True`

#### Scenario: Release does not remove other's lock

- **WHEN** the lock file contains a different process's pid
- **THEN** `release_lock` returns `False` and the file remains

#### Scenario: Release is idempotent

- **WHEN** no lock file exists
- **THEN** `release_lock` returns `False` without raising

### Requirement: Pipeline lifecycle integration

The pipeline SHALL acquire the lock in `Pipeline._on_start` (before stages start) and release it in `Pipeline._on_stop` (in a `finally` block, after all cleanup). The lock SHALL be held for the entire duration of the pipeline run. `LockHeldError` from `acquire_lock` SHALL propagate to the caller (main.py or test harness).

#### Scenario: Pipeline start/stop lifecycle

- **WHEN** `Pipeline.start()` is called
- **THEN** the lock is acquired before stages start
- **WHEN** `Pipeline.stop()` is called
- **THEN** the lock is released after all cleanup completes

#### Scenario: Pipeline start refuses with live lock

- **WHEN** a live lock exists
- **AND** `Pipeline.start()` is called
- **THEN** `LockHeldError` is raised and the pipeline does NOT start

### Requirement: main.py startup refuse

`main.py` SHALL check for a live lock BEFORE creating `HarvesterApp` or initializing providers. The check SHALL parse the workspace directly from the YAML (without full config validation, so it works even with invalid credentials/tasks). A live lock SHALL cause `logger.critical` + `print(..., file=sys.stderr)` + `sys.exit(1)`. A stale lock SHALL log INFO and continue (the pipeline will take over on start). The `--validate` and `--create-config` branches SHALL NOT perform the lock check (they are handled before the refuse point and do not start the pipeline).

#### Scenario: main.py refuses with live lock

- **WHEN** a live lock exists in the workspace
- **AND** `main.py -c <config>` is invoked (run path)
- **THEN** the process exits with rc≠0
- **AND** stderr contains the pid and holder from the lock

#### Scenario: --validate passes with live lock

- **WHEN** a live lock exists
- **AND** `main.py -c <config> --validate` is invoked
- **THEN** the process exits with rc=0 (validation succeeds or fails on its own merits, lock check is skipped)

#### Scenario: --create-config passes with live lock

- **WHEN** a live lock exists
- **AND** `main.py --create-config` is invoked
- **THEN** the process exits with rc=0 (config creation succeeds, lock check is skipped)

### Requirement: Corrupt lock file handling

`read_lock(workspace)` SHALL return `None` if the lock file is absent, contains invalid JSON, or is missing required fields (`pid`, `holder`). A WARNING SHALL be logged for corrupt files. A corrupt lock SHALL NOT be treated as live (fail-CLOSED direction: if we cannot parse it, we cannot verify liveness, so we treat it as absent and let `acquire_lock` overwrite it).

#### Scenario: Corrupt JSON → None

- **WHEN** the lock file contains invalid JSON
- **THEN** `read_lock` returns `None`
- **AND** a WARNING is logged

#### Scenario: Missing fields → None

- **WHEN** the lock file is valid JSON but missing `pid` or `holder`
- **THEN** `read_lock` returns `None`
- **AND** a WARNING is logged

## Implementation notes

- **Module**: `tools/run_lock.py` (small, self-contained, follows `tools/jsonl_rotate.py` pattern).
- **Exception**: `core.exceptions.LockHeldError(RuntimeError)` (follows `RegistryUnavailableError` precedent).
- **Integration points**: `manager/pipeline.py:_on_start` (acquire), `manager/pipeline.py:_on_stop` (release in finally), `main.py` (refuse check before `HarvesterApp` creation).
- **Workspace resolution**: `config.global_config.workspace` for pipeline; direct YAML parse for main.py refuse check (avoids full config validation).
- **Test stub compatibility**: `release_lock` in `_on_stop` is wrapped in `try/except AttributeError` to handle test stubs that call `_on_stop` directly on a SimpleNamespace without a `config` attribute.
