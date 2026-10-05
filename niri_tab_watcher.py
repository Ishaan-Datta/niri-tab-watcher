#!/usr/bin/env python3

"""Track niri focus transitions and restore the previous stable window."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import os
import signal
import socket
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_DEBOUNCE_MS = 750
DEFAULT_EXPANDED_WIDTH_RATIO = 0.9
RESTORE_EVENT_TIMEOUT = 2.0


@dataclass(frozen=True)
class WindowSnapshot:
    id: int
    workspace_id: int | None
    position: tuple[int, int] | None
    is_floating: bool
    is_expanded: bool
    entered_from_right: bool


@dataclass
class PendingFocus:
    candidate_id: int | None
    departed: WindowSnapshot
    deadline: float | None


@dataclass(frozen=True)
class RestorePlan:
    target_id: int
    focus_ids: tuple[int, ...]


@dataclass
class RestoreTransaction:
    target: WindowSnapshot
    expected_ids: list[int]
    previous_after: WindowSnapshot | None
    stable_after: int


class FocusTracker:
    """Pure state machine fed by decoded niri event-stream messages."""

    def __init__(self, debounce_seconds: float, expanded_width_ratio: float) -> None:
        self.debounce_seconds = debounce_seconds
        self.expanded_width_ratio = expanded_width_ratio
        self.windows: dict[int, dict[str, Any]] = {}
        self.workspaces: dict[int, dict[str, Any]] = {}
        self.output_widths: dict[str, float] = {}

        self.observed_id: int | None = None
        self.observed_entry_source_id: int | None = None
        self.stable_id: int | None = None
        self.stable_entry_source_id: int | None = None
        self.previous: WindowSnapshot | None = None
        self.pending: PendingFocus | None = None
        self.restore_transaction: RestoreTransaction | None = None
        self.initialized = False

    @property
    def pending_deadline(self) -> float | None:
        if self.pending is None:
            return None
        return self.pending.deadline

    def set_outputs(self, outputs: dict[str, Any]) -> None:
        widths: dict[str, float] = {}
        for name, output in outputs.items():
            logical = output.get("logical")
            if isinstance(logical, dict) and isinstance(
                logical.get("width"), (int, float)
            ):
                widths[name] = float(logical["width"])
        self.output_widths = widths

    def apply_event(self, event: dict[str, Any], now: float) -> bool:
        """Apply an event and return whether the debounce timer may have changed."""
        if "WorkspacesChanged" in event:
            workspaces = event["WorkspacesChanged"].get("workspaces", [])
            self.workspaces = {
                int(workspace["id"]): workspace
                for workspace in workspaces
                if isinstance(workspace, dict) and "id" in workspace
            }
            return False

        if "WorkspaceActivated" in event:
            return False

        if "WindowsChanged" in event:
            windows = event["WindowsChanged"].get("windows", [])
            self.windows = {
                int(window["id"]): window
                for window in windows
                if isinstance(window, dict) and "id" in window
            }
            focused = next(
                (
                    window_id
                    for window_id, window in self.windows.items()
                    if window.get("is_focused")
                ),
                None,
            )
            if not self.initialized:
                self.observed_id = focused
                self.stable_id = focused
                self.initialized = True
                return False
            return self._focus_changed(focused, now)

        if "WindowOpenedOrChanged" in event:
            window = event["WindowOpenedOrChanged"].get("window")
            if not isinstance(window, dict) or "id" not in window:
                return False
            window_id = int(window["id"])
            self.windows[window_id] = window
            if window.get("is_focused"):
                return self._focus_changed(window_id, now)
            return False

        if "WindowLayoutsChanged" in event:
            for change in event["WindowLayoutsChanged"].get("changes", []):
                if not isinstance(change, list) or len(change) != 2:
                    continue
                window_id, layout = change
                window = self.windows.get(int(window_id))
                if window is not None and isinstance(layout, dict):
                    window["layout"] = layout
            return False

        if "WindowFocusChanged" in event:
            window_id = event["WindowFocusChanged"].get("id")
            window_id = int(window_id) if window_id is not None else None
            return self._focus_changed(window_id, now)

        if "WindowClosed" in event:
            window_id = int(event["WindowClosed"]["id"])
            self._window_closed(window_id)
            return True

        return False

    def _window_closed(self, window_id: int) -> None:
        self.windows.pop(window_id, None)

        if self.previous is not None and self.previous.id == window_id:
            self.previous = None
        if self.observed_entry_source_id == window_id:
            self.observed_entry_source_id = None
        if self.stable_entry_source_id == window_id:
            self.stable_entry_source_id = None

        if self.restore_transaction is not None and (
            self.restore_transaction.target.id == window_id
            or window_id in self.restore_transaction.expected_ids
        ):
            self.restore_transaction = None

        if self.pending is not None:
            if self.pending.departed.id == window_id:
                # There is no valid old stable window to debounce back to.
                replacement = self.observed_id
                if replacement not in self.windows:
                    replacement = None
                self.stable_id = replacement
                self.stable_entry_source_id = self.observed_entry_source_id
                self.pending = None
            elif self.pending.candidate_id == window_id:
                self.pending.candidate_id = None
                self.pending.deadline = None

        if self.observed_id == window_id:
            self.observed_id = None
        if self.stable_id == window_id:
            replacement = self.observed_id
            if replacement not in self.windows:
                replacement = None
            self.stable_id = replacement
            self.stable_entry_source_id = self.observed_entry_source_id

    def _focus_changed(self, new_id: int | None, now: float) -> bool:
        old_id = self.observed_id
        if new_id == old_id:
            return False

        if old_id in self.windows:
            self.windows[old_id]["is_focused"] = False
        if new_id in self.windows:
            self.windows[new_id]["is_focused"] = True

        transaction = self.restore_transaction
        if transaction is not None:
            if transaction.expected_ids and new_id == transaction.expected_ids[0]:
                transaction.expected_ids.pop(0)
                self.observed_id = new_id
                if not transaction.expected_ids:
                    self.stable_id = transaction.stable_after
                    self.stable_entry_source_id = self._entry_source_for_snapshot(
                        transaction.target
                    )
                    self.previous = transaction.previous_after
                    self.pending = None
                    self.restore_transaction = None
                return True

            # An unexpected focus cancels all synthetic actions not yet submitted.
            self.restore_transaction = None

        source_id = old_id if old_id is not None else self.stable_id
        self.observed_id = new_id
        self.observed_entry_source_id = source_id

        if self.stable_id is None:
            if new_id is not None:
                self.stable_id = new_id
                self.stable_entry_source_id = source_id
            self.pending = None
            return True

        if new_id == self.stable_id:
            self.observed_entry_source_id = self.stable_entry_source_id
            self.pending = None
            return True

        if self.pending is None:
            departed = self._snapshot(self.stable_id, self.stable_entry_source_id)
            if departed is None:
                self.pending = None
                return True
            self.pending = PendingFocus(None, departed, None)

        self.pending.candidate_id = new_id
        self.pending.deadline = (
            now + self.debounce_seconds if new_id is not None else None
        )
        return True

    def commit_due(self, now: float) -> bool:
        pending = self.pending
        if (
            pending is None
            or pending.candidate_id is None
            or pending.deadline is None
            or now < pending.deadline
            or self.observed_id != pending.candidate_id
        ):
            return False

        self.previous = pending.departed
        self.stable_id = pending.candidate_id
        self.stable_entry_source_id = self.observed_entry_source_id
        self.pending = None
        return True

    def begin_restore(self) -> RestorePlan | None:
        if self.restore_transaction is not None:
            raise RuntimeError("a restore is already in progress")

        returning_from_pending = self.pending is not None
        target = self.pending.departed if returning_from_pending else self.previous
        if target is None or target.id not in self.windows:
            return None

        current_snapshot = None
        if not returning_from_pending and self.stable_id is not None:
            current_snapshot = self._snapshot(
                self.stable_id, self.stable_entry_source_id
            )

        anchor_id = self._select_anchor(target)
        focus_ids = tuple(
            window_id
            for window_id in (anchor_id, target.id)
            if window_id is not None and window_id != self.observed_id
        )

        if not focus_ids:
            self.pending = None
            return RestorePlan(target.id, ())

        previous_after = self.previous if returning_from_pending else current_snapshot
        self.restore_transaction = RestoreTransaction(
            target=target,
            expected_ids=list(focus_ids),
            previous_after=previous_after,
            stable_after=target.id,
        )
        return RestorePlan(target.id, focus_ids)

    def abort_restore(self, now: float) -> None:
        if self.restore_transaction is None:
            return
        self.restore_transaction = None
        actual_id = self.observed_id
        self.observed_id = self.stable_id
        self._focus_changed(actual_id, now)

    def _snapshot(
        self, window_id: int, entry_source_id: int | None
    ) -> WindowSnapshot | None:
        window = self.windows.get(window_id)
        if window is None:
            return None

        position = self._position(window)
        entered_from_right = False
        source = (
            self.windows.get(entry_source_id) if entry_source_id is not None else None
        )
        source_position = self._position(source) if source is not None else None
        if (
            position is not None
            and source_position is not None
            and source.get("workspace_id") == window.get("workspace_id")
        ):
            entered_from_right = source_position[0] > position[0]

        return WindowSnapshot(
            id=window_id,
            workspace_id=window.get("workspace_id"),
            position=position,
            is_floating=bool(window.get("is_floating")),
            is_expanded=self._is_expanded(window),
            entered_from_right=entered_from_right,
        )

    def _select_anchor(self, snapshot: WindowSnapshot) -> int | None:
        target = self.windows.get(snapshot.id)
        if target is None:
            return None
        position = self._position(target)
        if (
            snapshot.is_floating
            or snapshot.is_expanded
            or self._is_expanded(target)
            or not snapshot.entered_from_right
            or position is None
            or snapshot.workspace_id != target.get("workspace_id")
        ):
            return None

        target_col, target_row = position
        candidates: list[tuple[int, int, int, int]] = []
        for window_id, window in self.windows.items():
            if window_id == snapshot.id or window.get("is_floating"):
                continue
            if window.get("workspace_id") != snapshot.workspace_id:
                continue
            candidate_position = self._position(window)
            if candidate_position is None or candidate_position[0] <= target_col:
                continue
            col, row = candidate_position
            candidates.append((col, abs(row - target_row), row, window_id))

        return min(candidates)[3] if candidates else None

    def _is_expanded(self, window: dict[str, Any]) -> bool:
        layout = window.get("layout")
        if not isinstance(layout, dict):
            return False
        tile_size = layout.get("tile_size")
        if not isinstance(tile_size, list) or not tile_size:
            return False
        workspace = self.workspaces.get(window.get("workspace_id"))
        if workspace is None:
            return False
        output = workspace.get("output")
        output_width = self.output_widths.get(output)
        if output_width is None or output_width <= 0:
            return False
        return float(tile_size[0]) >= output_width * self.expanded_width_ratio

    @staticmethod
    def _position(window: dict[str, Any] | None) -> tuple[int, int] | None:
        if not isinstance(window, dict):
            return None
        layout = window.get("layout")
        if not isinstance(layout, dict):
            return None
        position = layout.get("pos_in_scrolling_layout")
        if not isinstance(position, list) or len(position) != 2:
            return None
        return int(position[0]), int(position[1])

    def _entry_source_for_snapshot(self, snapshot: WindowSnapshot) -> int | None:
        if not snapshot.entered_from_right or snapshot.position is None:
            return None
        target_col, target_row = snapshot.position
        candidates: list[tuple[int, int, int, int]] = []
        for window_id, window in self.windows.items():
            if window.get("workspace_id") != snapshot.workspace_id:
                continue
            position = self._position(window)
            if position is None or position[0] <= target_col:
                continue
            col, row = position
            candidates.append((col, abs(row - target_row), row, window_id))
        return min(candidates)[3] if candidates else None


def control_socket_path() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime_dir:
        raise RuntimeError("XDG_RUNTIME_DIR is not set")
    return Path(runtime_dir) / "niri-tab-watcher.sock"


class WatcherDaemon:
    def __init__(self, debounce_ms: int, expanded_width_ratio: float) -> None:
        self.tracker = FocusTracker(debounce_ms / 1000, expanded_width_ratio)
        self.ready = asyncio.Event()
        self.focus_condition = asyncio.Condition()
        self.restore_lock = asyncio.Lock()
        self.commit_handle: asyncio.TimerHandle | None = None
        self.event_process: asyncio.subprocess.Process | None = None
        self.server: asyncio.AbstractServer | None = None
        self.output_refresh_task: asyncio.Task[None] | None = None
        self.socket_path = control_socket_path()
        self.socket_identity: tuple[int, int] | None = None
        self.lock_file: Any = None

    async def run(self) -> None:
        if not os.environ.get("NIRI_SOCKET"):
            raise RuntimeError(
                "NIRI_SOCKET is not set; start this service inside the niri session"
            )

        self._acquire_lock()
        event_task: asyncio.Task[None] | None = None
        try:
            self.tracker.set_outputs(await self._query_outputs())
            self.event_process = await asyncio.create_subprocess_exec(
                "niri", "msg", "--json", "event-stream", stdout=asyncio.subprocess.PIPE
            )
            event_task = asyncio.create_task(self._read_events())
            await asyncio.wait_for(self.ready.wait(), timeout=5)
            self.server = await self._start_control_server()
            print(f"listening on {self.socket_path}", file=sys.stderr, flush=True)
            await event_task
            raise RuntimeError("niri event stream ended")
        finally:
            if self.commit_handle is not None:
                self.commit_handle.cancel()
            if self.server is not None:
                self.server.close()
                await self.server.wait_closed()
            self._unlink_owned_socket()
            if event_task is not None and not event_task.done():
                event_task.cancel()
            if event_task is not None:
                await asyncio.gather(event_task, return_exceptions=True)
            if self.output_refresh_task is not None:
                self.output_refresh_task.cancel()
                await asyncio.gather(self.output_refresh_task, return_exceptions=True)
            await self._stop_event_process()
            if self.lock_file is not None:
                self.lock_file.close()
                self.lock_file = None

    def _acquire_lock(self) -> None:
        lock_path = self.socket_path.with_suffix(".lock")
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        lock_file = os.fdopen(descriptor, "w")
        try:
            fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock_file.close()
            raise RuntimeError(
                "another niri-tab-watcher daemon is already running"
            ) from None
        self.lock_file = lock_file

    async def _read_events(self) -> None:
        assert self.event_process is not None and self.event_process.stdout is not None
        while line := await self.event_process.stdout.readline():
            try:
                event = json.loads(line)
                timer_changed = self.tracker.apply_event(
                    event, asyncio.get_running_loop().time()
                )
            except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                print(
                    f"ignoring malformed niri event: {error}",
                    file=sys.stderr,
                    flush=True,
                )
                continue

            if "WorkspacesChanged" in event:
                self._request_output_refresh()
            if "WindowsChanged" in event:
                self.ready.set()
            if timer_changed:
                self._schedule_commit()
            async with self.focus_condition:
                self.focus_condition.notify_all()

        return_code = await self.event_process.wait()
        raise RuntimeError(f"niri event stream exited with status {return_code}")

    def _request_output_refresh(self) -> None:
        if self.output_refresh_task is None or self.output_refresh_task.done():
            self.output_refresh_task = asyncio.create_task(self._refresh_outputs())

    async def _refresh_outputs(self) -> None:
        try:
            self.tracker.set_outputs(await self._query_outputs())
        except (OSError, RuntimeError, asyncio.TimeoutError, json.JSONDecodeError) as error:
            print(
                f"could not refresh niri outputs: {error}",
                file=sys.stderr,
                flush=True,
            )

    def _schedule_commit(self) -> None:
        if self.commit_handle is not None:
            self.commit_handle.cancel()
            self.commit_handle = None
        deadline = self.tracker.pending_deadline
        if deadline is None:
            return
        loop = asyncio.get_running_loop()
        self.commit_handle = loop.call_at(deadline, self._commit_pending)

    def _commit_pending(self) -> None:
        self.commit_handle = None
        self.tracker.commit_due(asyncio.get_running_loop().time())

    async def _start_control_server(self) -> asyncio.AbstractServer:
        path = self.socket_path
        if path.exists():
            try:
                reader, writer = await asyncio.open_unix_connection(path)
            except (ConnectionRefusedError, FileNotFoundError):
                path.unlink(missing_ok=True)
            else:
                writer.close()
                await writer.wait_closed()
                raise RuntimeError(f"another daemon is already listening on {path}")

        old_umask = os.umask(0o077)
        try:
            server = await asyncio.start_unix_server(self._handle_client, path)
        finally:
            os.umask(old_umask)
        path.chmod(0o600)
        stat = path.stat()
        self.socket_identity = (stat.st_dev, stat.st_ino)
        return server

    def _unlink_owned_socket(self) -> None:
        if self.socket_identity is None:
            return
        try:
            stat = self.socket_path.stat()
        except FileNotFoundError:
            return
        if (stat.st_dev, stat.st_ino) == self.socket_identity:
            self.socket_path.unlink()
        self.socket_identity = None

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        response: dict[str, Any]
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=2)
            request = json.loads(line)
            if request.get("command") != "restore":
                response = {"ok": False, "error": "unknown command"}
            else:
                response = await self._restore()
        except (asyncio.TimeoutError, json.JSONDecodeError, AttributeError) as error:
            response = {"ok": False, "error": f"invalid request: {error}"}

        writer.write(json.dumps(response, separators=(",", ":")).encode() + b"\n")
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    async def _restore(self) -> dict[str, Any]:
        async with self.restore_lock:
            try:
                plan = self.tracker.begin_restore()
            except RuntimeError as error:
                return {"ok": False, "error": str(error)}
            if plan is None:
                return {"ok": False, "error": "no previous stable window is available"}
            if not plan.focus_ids:
                return {"ok": True, "target_id": plan.target_id}

            transaction = self.tracker.restore_transaction
            assert transaction is not None
            try:
                for window_id in plan.focus_ids:
                    if (
                        self.tracker.restore_transaction is not transaction
                        or not transaction.expected_ids
                        or transaction.expected_ids[0] != window_id
                    ):
                        raise RuntimeError(
                            "restore was interrupted by another focus change"
                        )
                    await self._focus_window(window_id)
                    async with self.focus_condition:
                        await asyncio.wait_for(
                            self.focus_condition.wait_for(
                                lambda: (
                                    self.tracker.restore_transaction is not transaction
                                    or not transaction.expected_ids
                                    or transaction.expected_ids[0] != window_id
                                )
                            ),
                            timeout=RESTORE_EVENT_TIMEOUT,
                        )
                    if self.tracker.restore_transaction is not transaction:
                        if (
                            self.tracker.stable_id == plan.target_id
                            and self.tracker.observed_id == plan.target_id
                        ):
                            break
                        raise RuntimeError(
                            "restore was interrupted by another focus change"
                        )
            except (RuntimeError, asyncio.TimeoutError) as error:
                self.tracker.abort_restore(asyncio.get_running_loop().time())
                self._schedule_commit()
                return {"ok": False, "error": f"restore failed: {error}"}

            self._schedule_commit()
            return {"ok": True, "target_id": plan.target_id}

    @staticmethod
    async def _focus_window(window_id: int) -> None:
        await run_command(
            "niri",
            "msg",
            "action",
            "focus-window",
            "--id",
            str(window_id),
            timeout=RESTORE_EVENT_TIMEOUT,
        )

    @staticmethod
    async def _query_outputs() -> dict[str, Any]:
        stdout, _stderr = await run_command(
            "niri", "msg", "--json", "outputs", timeout=RESTORE_EVENT_TIMEOUT
        )
        output = json.loads(stdout)
        if not isinstance(output, dict):
            raise RuntimeError("niri returned an invalid outputs response")
        return output

    async def _stop_event_process(self) -> None:
        process = self.event_process
        if process is None or process.returncode is not None:
            return
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=1)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()


async def run_command(*command: str, timeout: float) -> tuple[bytes, bytes]:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        if process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        raise
    if process.returncode != 0:
        message = stderr.decode().strip() or stdout.decode().strip()
        command_text = " ".join(command)
        raise RuntimeError(
            message or f"{command_text} exited with status {process.returncode}"
        )
    return stdout, stderr


def restore_client(timeout: float) -> int:
    path = control_socket_path()
    request = json.dumps({"command": "restore"}, separators=(",", ":")).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(str(path))
            client.sendall(request)
            response_data = b""
            while not response_data.endswith(b"\n"):
                chunk = client.recv(4096)
                if not chunk:
                    break
                response_data += chunk
    except (FileNotFoundError, ConnectionRefusedError, socket.timeout) as error:
        print(f"niri-tab-watcher: daemon unavailable: {error}", file=sys.stderr)
        return 1

    try:
        response = json.loads(response_data)
    except json.JSONDecodeError:
        print("niri-tab-watcher: daemon returned an invalid response", file=sys.stderr)
        return 1
    if not response.get("ok"):
        print(
            f"niri-tab-watcher: {response.get('error', 'restore failed')}",
            file=sys.stderr,
        )
        return 1
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="niri-tab-watcher", description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    daemon = subparsers.add_parser(
        "daemon", help="subscribe to niri as a foreground daemon"
    )
    daemon.add_argument("--debounce-ms", type=int, default=DEFAULT_DEBOUNCE_MS)
    daemon.add_argument(
        "--expanded-width-ratio", type=float, default=DEFAULT_EXPANDED_WIDTH_RATIO
    )

    restore = subparsers.add_parser(
        "restore", help="restore the previous stable window"
    )
    restore.add_argument("--timeout", type=float, default=12)
    return parser.parse_args()


async def run_daemon(args: argparse.Namespace) -> None:
    if args.debounce_ms < 0:
        raise RuntimeError("--debounce-ms must not be negative")
    if not 0 < args.expanded_width_ratio <= 1:
        raise RuntimeError("--expanded-width-ratio must be between 0 and 1")

    daemon = WatcherDaemon(args.debounce_ms, args.expanded_width_ratio)
    loop = asyncio.get_running_loop()
    task = asyncio.create_task(daemon.run())
    for signum in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(signum, task.cancel)
    try:
        await task
    except asyncio.CancelledError:
        pass


def main() -> int:
    args = parse_args()
    try:
        if args.command == "restore":
            return restore_client(args.timeout)
        asyncio.run(run_daemon(args))
        return 0
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(f"niri-tab-watcher: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
