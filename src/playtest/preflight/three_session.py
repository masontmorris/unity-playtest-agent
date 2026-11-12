from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from playtest.contracts.bridge import Engine
from playtest.core.bridge_client import BridgeClient
from playtest.preflight import calibrate, entity, input_probe
from playtest.preflight.catalog import serialize
from playtest.preflight.three import GameDeclarations, ThreeStudy, compile_study, load_declarations, study_dict

ROOT = Path(__file__).resolve().parents[3]
RELAY = ROOT / "bridge-three" / "relay" / "relay.mjs"
PAGE = ROOT / "scripts" / "three" / "open_page.mjs"


def _dump(value: Any) -> bytes:
    return (json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n").encode("utf-8")


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class Journal:
    def __init__(self, path: Path):
        self.path = path

    def append(self, kind: str, value: Any) -> None:
        with self.path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(json.dumps({"kind": kind, "value": value}, ensure_ascii=False, allow_nan=False) + "\n")
            stream.flush()
            os.fsync(stream.fileno())


def _alive(pid: int) -> bool:
    if os.name == "nt":
        result = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True)
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


@contextmanager
def game_claim(game: Path) -> Iterator[None]:
    temp = game / "Temp"
    temp.mkdir(parents=True, exist_ok=True)
    claim = temp / "playtest-preflight.claim"
    fd = os.open(claim, os.O_RDWR | os.O_CREAT, 0o600)
    stream = os.fdopen(fd, "r+b")
    if os.fstat(fd).st_size == 0:
        stream.write(b" ")
        stream.flush()
    stream.seek(0)
    try:
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        stream.close()
        raise ValueError(f"another preflight holds {claim}") from None
    claimed = False
    try:
        stream.seek(0)
        previous = stream.read().strip()
        if previous:
            try:
                pid = int(json.loads(previous)["pid"])
            except (ValueError, KeyError, TypeError):
                raise ValueError(f"cannot establish the previous owner of {claim}") from None
            if _alive(pid):
                raise ValueError(f"preflight process {pid} holds {claim}")
        stream.seek(0)
        stream.truncate()
        stream.write(json.dumps({"pid": os.getpid()}).encode())
        stream.flush()
        claimed = True
        lock = temp / "playtest-bridge.lock"
        if lock.exists():
            try:
                pid = int(json.loads(lock.read_text(encoding="utf-8"))["pid"])
            except (ValueError, KeyError, OSError, TypeError):
                raise ValueError(f"cannot establish the owner of {lock}") from None
            if _alive(pid):
                raise ValueError(f"another bridge process {pid} holds {lock}")
        yield
    finally:
        if claimed:
            stream.seek(0)
            stream.truncate()
            stream.write(b" ")
            stream.flush()
        stream.seek(0)
        if os.name == "nt":
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
        stream.close()


class ProcessJob:
    """Windows closes the job on supervisor death and kills every owned process tree."""
    def __init__(self):
        self.handle = None
        if os.name == "nt":
            from ctypes import wintypes
            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]
            class IO(ctypes.Structure):
                _fields_ = [(key, ctypes.c_ulonglong) for key in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]
            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO), ("ProcessMemoryLimit", ctypes.c_size_t),
                            ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.CreateJobObjectW.restype = wintypes.HANDLE
            kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            self.kernel = kernel
            self.handle = kernel.CreateJobObjectW(None, None)
            if not self.handle:
                raise ctypes.WinError(ctypes.get_last_error())
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000
            if not kernel.SetInformationJobObject(self.handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
                self.close()
                raise ctypes.WinError(ctypes.get_last_error())

    def add(self, proc: subprocess.Popen) -> None:
        if self.handle and not self.kernel.AssignProcessToJobObject(self.handle, int(proc._handle)):
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self) -> None:
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _start_owned(job: ProcessJob, command: list[str], **kwargs: Any) -> subprocess.Popen:
    gate = ("import subprocess,sys; sys.stdin.readline(); "
            "sys.exit(subprocess.call(sys.argv[1:], stdin=sys.stdin))")
    kwargs["stdin"] = subprocess.PIPE
    kwargs["text"] = True
    kwargs.setdefault("encoding", "utf-8")
    proc = subprocess.Popen([sys.executable, "-u", "-c", gate, *command], **kwargs)
    try:
        job.add(proc)
        proc.stdin.write("start\n")
        proc.stdin.flush()
        return proc
    except BaseException:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        raise


def _stop(proc: subprocess.Popen, *, graceful: bool = False) -> None:
    if graceful and proc.stdin:
        try:
            proc.stdin.close()
            proc.wait(timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if proc.poll() is None:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        else:
            proc.kill()
        proc.wait(timeout=10)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def serve(game: Path, catalog: Path, query: str, page_log: Path | None = None) -> Iterator[None]:
    node = shutil.which("node")
    if node is None:
        raise ValueError("node is not installed")
    processes: list[subprocess.Popen] = []
    job = ProcessJob()
    try:
        def start(command: list[str], **kwargs: Any) -> subprocess.Popen:
            proc = _start_owned(job, command, **kwargs)
            processes.append(proc)
            return proc
        relay = start([node, str(RELAY), "--project", str(game), "--catalog", str(catalog)],
                      stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, errors="replace")
        endpoint = json.loads(relay.stdout.readline())["playtest"]
        port = _free_port()
        start([node, str(game / "node_modules" / "vite" / "bin" / "vite.js"), "--port", str(port), "--strictPort", "--host", "127.0.0.1"],
              cwd=game, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + 90
        while True:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    raise ValueError("the game dev server did not start") from None
                time.sleep(0.5)
        page_query = "playtest=" + urllib.parse.quote(endpoint, safe="") + ("&" + query if query else "")
        page = start([node, str(PAGE), str(game), f"http://127.0.0.1:{port}/?{page_query}"],
                     stdout=subprocess.PIPE, stderr=subprocess.STDOUT, errors="replace")
        def drain() -> None:
            # The page reports its console errors and its health every 5 s; keep them for diagnosis.
            if page_log is None:
                for _ in page.stdout:
                    pass
                return
            with page_log.open("a", encoding="utf-8", newline="\n") as stream:
                for line in page.stdout:
                    stream.write(f"{time.time():.3f} {line}")
                    stream.flush()
        threading.Thread(target=drain, daemon=True).start()
        yield
    finally:
        try:
            for index, process in enumerate(reversed(processes)):
                try:
                    _stop(process, graceful=(index == 0 and len(processes) == 3))
                except (OSError, subprocess.SubprocessError) as exc:
                    print(f"could not stop child {process.pid}: {exc}", file=sys.stderr)
        finally:
            job.close()


async def connect(game: Path, *, timeout_s: float = 10.0) -> BridgeClient:
    deadline = time.monotonic() + 120
    while True:
        try:
            client = await BridgeClient.connect_from_project(game, timeout_s=timeout_s)
            if client.status.engine is not Engine.three:
                await client.aclose()
                raise ValueError("the bridge is not three.js")
            return client
        except ValueError:
            raise
        except Exception:
            if time.monotonic() >= deadline:
                raise
            await asyncio.sleep(1)


def _name_path(rows: dict[str, dict[str, Any]], ident: str) -> list[str | None]:
    names = []
    seen = set()
    while ident is not None:
        if ident in seen or ident not in rows:
            raise ValueError("transform hierarchy is incomplete or cyclic")
        seen.add(ident)
        row = rows[ident]
        names.append(row.get("name"))
        ident = row["parent_id"]
    return list(reversed(names))


def _identity(data: dict[str, Any]) -> dict[str, tuple[Any, ...]]:
    rows = entity._objects(data)
    return {ident: (row["parent_id"], tuple(_name_path(rows, ident))) for ident, row in rows.items()}


async def _snapshot(client: BridgeClient, journal: Journal) -> dict[str, Any]:
    result = entity._complete(await client.inspect("transforms"), "transforms")
    journal.append("transforms", result)
    return result


async def _quiet(client: BridgeClient, entity_id: str, journal: Journal) -> tuple[dict[str, Any], dict[str, Any]] | None:
    deadline = time.monotonic() + 3
    previous = await calibrate._read_entity(client, entity_id)
    journal.append("quiet", previous)
    still = 0
    while time.monotonic() < deadline:
        await asyncio.sleep(0.12)
        current = await calibrate._read_entity(client, entity_id)
        journal.append("quiet", current)
        a, b = calibrate._vector(previous["position"]), calibrate._vector(current["position"])
        if a is None or b is None:
            raise ValueError("entity position is unknown")
        still = still + 1 if calibrate._norm(calibrate._sub(b, a)) <= calibrate.IDLE_TOLERANCE else 0
        if still >= calibrate.QUIET_INTERVALS:
            return previous, current
        previous = current
    return None


async def measure(decl: GameDeclarations, entity_action: str, settle_s: float, query: str,
                  directory: Path, entity_id: str | None = None) -> ThreeStudy:
    journal = Journal(directory / "session.partial.jsonl")
    client = await connect(decl.game)
    session = client.status.session
    trials, attempts = [], []
    fatal = None
    stop = None
    initial = None
    entity_row = None
    image = None
    try:
        await asyncio.sleep(settle_s)
        initial_sample = await client.observe_once(with_frame=True)
        initial = initial_sample.model_dump(mode="json")
        journal.append("initial_observation", initial)
        captured = input_probe.frame_record(initial_sample.frame_ref, archive_dir=directory / "frames")
        journal.append("initial_frame", captured)
        if not captured.get("readable") or not captured.get("archived") or not captured.get("width") or not captured.get("height"):
            raise ValueError("initial capture self-test or frame archive did not pass")
        image = {"mime": "image/png" if str(captured["ref"]).lower().endswith(".png") else "image/jpeg",
                 "bytes": captured["bytes"], "sha256": captured["sha256"], "provenance": "three bridge measurement capture",
                 "same_session": True, "capture_game_s": initial_sample.game_s}
        identities = None
        for repeat in (1, 2):
            idle = []
            for index in range(entity.IDLE_INTERVALS + 1):
                if index:
                    await asyncio.sleep(entity.IDLE_INTERVAL_S)
                snap = await _snapshot(client, journal)
                current = _identity(snap)
                if identities is None:
                    identities = current
                elif current != identities:
                    raise ValueError("object identities changed during entity measurement")
                idle.append(snap)
            before = idle[-1]
            observation = await client.observe_once()
            journal.append("entity_observation", observation.model_dump(mode="json"))
            records = await client.act(action_id=f"entity-{uuid.uuid4().hex}", choice_id=entity_action,
                                       action=entity_action, based_on_seq=observation.seq, max_age_s=2,
                                       params={}, depends_on=[], lease_s=decl.catalog.actions[entity_action].default_lease_s)
            journal.append("entity_execution", [r.model_dump(mode="json") for r in records])
            terminal = records[-1]
            declaration = input_probe._catalog_declaration(decl.catalog, entity_action)
            judged = input_probe.evaluate_attempt(declaration, records, None)
            if (not judged["valid"] or terminal.validated_against_seq != observation.seq
                    or any(record.session != session for record in records)):
                raise ValueError("entity measurement lacked exact, sequence-bound, release-verified receipts")
            after = await _snapshot(client, journal)
            # A transforms read of a large scene is slow (Kinema: about 0.5 s for 1,783 objects), so the idle window
            # outlasts the action and entity.analyze finds them incomparable. Read again until they span alike.
            idle_span = idle[-1]["real_time_s"] - idle[0]["real_time_s"]
            for _ in range(entity.IDLE_INTERVALS):
                if after["real_time_s"] - before["real_time_s"] >= idle_span:
                    break
                await asyncio.sleep(entity.IDLE_INTERVAL_S)
                after = await _snapshot(client, journal)
            if _identity(after) != identities:
                raise ValueError("object identities changed during entity measurement")
            trials.append({"control": entity_action, "idle": idle, "before": before, "after": after,
                           "execution": [r.model_dump(mode="json") for r in records]})
            await asyncio.sleep(entity.SETTLE_S)
        analysis = entity.analyze(trials)
        journal.append("entity_analysis", analysis)
        candidates = analysis["independent_candidates"]
        if analysis["status"] == "identified" and entity_id in (None, analysis["entity_id"]):
            ident, selection = analysis["entity_id"], {"by": "measurement"}
        elif analysis["status"] == "ambiguous" and entity_id in candidates:
            # The operator names one of the measured responsive roots; code never picks among them.
            ident, selection = entity_id, {"by": "operator", "independent_candidates": candidates}
        else:
            raise ValueError(f"entity identification is {analysis['status']}: {candidates}"
                             + (f"; the operator's entity {entity_id!r} is not among them" if entity_id else ""))
        journal.append("entity_selection", {"entity_id": ident, **selection})
        rows = entity._objects(trials[-1]["after"])
        entity_row = {**rows[ident], "name_path": _name_path(rows, ident), "selection": selection}
        for name in decl.catalog.actions:
            definition = input_probe._catalog_declaration(decl.catalog, name)
            for repeat in (1, 2):
                quiet = await _quiet(client, ident, journal)
                plan = input_probe.ProbePlan(schema_version=1, actions=[input_probe.ProbeTarget(name=name, repeats=1)],
                                             public_fields=list(decl.readers))
                result = await input_probe.measure(client, plan, [definition], entity_id=ident, scene_id=None,
                                                   readers=[], media_dir=directory / "frames", on_reading=journal.append)
                if result[-1].get("fatal_error"):
                    raise ValueError(result[-1]["fatal_error"])
                attempt = result[0]["attempts"][0]
                attempt.update(name=name, repeat=repeat, idle_start=quiet[0] if quiet else None)
                journal.append("attempt", attempt)
                attempts.append(attempt)
                if attempt["status"] == "rejected":
                    break
                for frame in attempt["frames"].values():
                    if not frame.get("readable") or not frame.get("archived") or not frame.get("width") or not frame.get("height"):
                        raise ValueError("a probe frame could not be archived and checked")
                    if image is None:
                        image = {"mime": "image/png" if str(frame["ref"]).lower().endswith(".png") else "image/jpeg",
                                 "bytes": frame["bytes"], "sha256": frame["sha256"], "provenance": "three bridge measurement capture",
                                 "same_session": True, "capture_game_s": frame["game_s"]}
    except Exception as exc:
        fatal = f"{type(exc).__name__}: {exc}"
        journal.append("fatal", fatal)
    finally:
        try:
            stopped = await client.stop()
            stop = stopped.model_dump(mode="json")
            journal.append("stop", stop)
            if stopped.release_verified is not True:
                fatal = fatal or "stop did not verify release"
        except Exception as exc:
            fatal = fatal or f"stop failed: {type(exc).__name__}: {exc}"
            journal.append("stop_error", fatal)
        await client.aclose()
    study = ThreeStudy(decl, session, initial, entity_row, trials, attempts, stop, image,
                       {"query": query, "settle_s": settle_s}, fatal)
    (directory / "study.json").write_bytes(_dump(study_dict(study)))
    if fatal:
        raise ValueError(f"preflight failed; diagnostics in {directory}: {fatal}")
    return study


def prepare(decl: GameDeclarations, entity_action: str, output: Path, *, query: str = "", settle_s: float = 60,
            entity_id: str | None = None) -> Path:
    definition = decl.catalog.actions.get(entity_action)
    if definition is None or definition.is_skill or len(definition.inputs) != 1:
        raise ValueError("entity action must name one declared single-input action")
    if settle_s < 0:
        raise ValueError("settle must be nonnegative")
    directory = output.resolve() / f"attempt-{uuid.uuid4().hex}"
    directory.mkdir(parents=True, exist_ok=False)
    with game_claim(decl.game):
        with serve(decl.game, decl.catalog_path, query, directory / "page.log"):
            study = asyncio.run(measure(decl, entity_action, settle_s, query, directory, entity_id))
    if load_declarations(decl.game, decl.catalog_path).source != decl.source:
        raise ValueError(f"game source changed during acquisition; diagnostics in {directory}")
    compiled = compile_study(study)
    files = {"manifest.json": compiled.manifest.model_dump(mode="json"),
             "calibration.json": compiled.calibration.model_dump(mode="json"), "entity.json": compiled.entity,
             "catalog.json": serialize(compiled.catalog)}
    for name, value in files.items():
        (directory / name).write_bytes(_dump(value))
    from playtest.runner.catalog import verify_host_catalog
    verify_host_catalog(compiled.manifest, directory / "catalog.json", _sha(directory / "catalog.json"))
    marker = {"schema_version": 1, "game": str(decl.game), "catalog_path": str(decl.catalog_path),
              "source": decl.source, "launch": study.launch, "entity_action": entity_action,
              "files": {name: _sha(directory / name) for name in ["study.json", *files]}}
    temporary = directory / "complete.json.tmp"
    temporary.write_bytes(_dump(marker))
    temporary.replace(directory / "complete.json")
    return directory / "complete.json"
