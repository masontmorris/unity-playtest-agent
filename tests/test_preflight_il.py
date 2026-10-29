"""The IL write scan runs outside the bridge; its document is merged into the discovery snapshot.

No test runs the real tool: a fake runner stands in for `subprocess.run`.
"""

from __future__ import annotations

import copy
import functools
import json
import subprocess
import tomllib
from pathlib import Path

import pytest

from playtest.contracts.bridge import InspectResult
from playtest.preflight import cache, il

REPOSITORY = Path(__file__).resolve().parents[1]
TOOL = REPOSITORY / "tools" / "ilscan" / "src" / "Playtest.IlScan.csproj"
DATA_KEYS = ["il_write_sets", "il_unlisted_writes", "il_issues", "il_scope", "il_complete"]
CAP_KEYS = ["il_types", "il_fields", "il_methods", "il_writes", "il_issues"]
SNAPSHOT_ERROR = "il_script:Assets/a.cs: InvalidOperationException"
TOOL_ERROR = "il_assembly:Scripts.Extra: not loaded (caf\u00e9)"
WALL_UTC = "2026-10-29T12:00:00.1234567Z"
INPUTS = {
    "assemblies": [
        {"name": "Scripts.Main", "path": "C:\\proj\\Library\\ScriptAssemblies\\Scripts.Main.dll"},
        {"name": "Scripts.Extra", "path": "C:\\Users\\Zo\u00eb\\proj\\Library\\ScriptAssemblies\\Scripts.Extra.dll"},
    ],
    "references": ["C:\\Unity\\Editor\\Data\\Managed\\mscorlib.dll", "C:\\proj\\Library\\ScriptAssemblies\\Lib.dll"],
    "complete": True,
}


def cap(count: int = 0, limit: int = 100) -> dict:
    return {"limit": limit, "observed": count, "returned": count, "truncated": False}


def snapshot() -> dict:
    """A discovery response as the bridge sends it: the scan's inputs, but not its results."""
    return {
        "protocol": "upa/1", "type": "inspection", "session": "s-test", "seq": 3, "game_s": 1.5,
        "wall_utc": WALL_UTC, "stage": "discovery", "in_reply_to": 2,
        "data": {"scenes": [{"id": "scene", "path": "Assets/main.unity"}], "il_inputs": copy.deepcopy(INPUTS)},
        "caps": {"scenes": cap(1), "il_script_files": cap(2), "il_assemblies": cap(2)},
        "errors": [SNAPSHOT_ERROR],
    }


def document() -> dict:
    """What the scan tool writes."""
    return {
        "data": {"il_write_sets": [{"field_id": "Scripts.Main:Scripts.Main.Widget:size", "direct_writers": []}],
                 "il_unlisted_writes": [], "il_issues": [], "il_scope": "direct_stores_in_scanned_assemblies",
                 "il_complete": True},
        "caps": {name: cap(count) for name, count in zip(CAP_KEYS, (4, 1, 7, 0, 0))},
        "errors": [TOOL_ERROR],
    }


class Tool:
    """Stands in for `subprocess.run`: records the call, then behaves like the scan tool."""

    def __init__(self, written=None, *, returncode=0, stderr="", stdout="", write=True, raises=None):
        self.written, self.returncode, self.stderr, self.stdout = written, returncode, stderr, stdout
        self.write, self.raises = write, raises
        self.calls: list[tuple[list[str], dict]] = []
        self.inputs = None
        self.directory: Path | None = None

    def __call__(self, command, **kwargs):
        self.calls.append((command, kwargs))
        source, target = (Path(command[command.index(flag) + 1]) for flag in ("--input", "--output"))
        self.directory = source.parent
        self.inputs = json.loads(source.read_bytes().decode("utf-8"))
        assert not target.exists(), "the real tool refuses an output that already exists"
        if self.raises is not None:
            raise self.raises
        if self.write:
            body = self.written if isinstance(self.written, (str, bytes)) else json.dumps(self.written)
            target.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
        return subprocess.CompletedProcess(command, self.returncode, self.stdout, self.stderr)


def at(root, path):
    for key in path:
        root = root[key]
    return root


def drop(*path):
    def change(root):
        del at(root, path[:-1])[path[-1]]
        return root
    return change


def put(value, *path):
    def change(root):
        at(root, path[:-1])[path[-1]] = value
        return root
    return change


def reorder(*path):
    """Move the first key of the mapping at `path` to the end."""
    def change(root):
        mapping = at(root, path)
        first = next(iter(mapping))
        mapping[first] = mapping.pop(first)
        return root
    return change


# --- running the tool -------------------------------------------------------------


def test_scan_runs_the_tool_on_exactly_the_bridge_inputs():
    tool = Tool(document())
    assert il.scan(INPUTS, tool_project=TOOL, run=tool) == document()
    [(command, kwargs)] = tool.calls
    source, target = command[10], command[12]
    assert command == ["dotnet", "run", "--project", str(TOOL), "--configuration", "Release",
                       "--verbosity", "quiet", "--", "--input", source, "--output", target]
    assert (Path(source).name, Path(target).name) == ("inputs.json", "document.json")
    assert Path(source).parent == Path(target).parent
    assert kwargs == {"capture_output": True, "text": True, "encoding": "utf-8", "errors": "replace",
                      "check": False, "timeout": 900}
    assert tool.inputs == INPUTS
    assert not tool.directory.exists()


def test_the_default_tool_is_the_one_in_this_repository():
    assert il.TOOL_PROJECT == TOOL
    tool = Tool(document())
    il.scan(INPUTS, run=tool)
    assert tool.calls[0][0][3] == str(TOOL)


def test_a_document_with_a_byte_order_mark_is_read():
    tool = Tool(b"\xef\xbb\xbf" + json.dumps(document()).encode("utf-8"))
    assert il.scan(INPUTS, tool_project=TOOL, run=tool) == document()


@pytest.mark.parametrize("inputs", [
    {"assemblies": [], "references": [], "complete": False},
    {"assemblies": [{"name": "Scripts.Main", "path": "a.dll"}], "references": [], "complete": True},
])
def test_well_formed_inputs_reach_the_tool_unchanged(inputs):
    tool = Tool(document())
    snap = snapshot()
    snap["data"]["il_inputs"] = copy.deepcopy(inputs)
    il.complete(snap, tool_project=TOOL, run=tool)
    assert tool.inputs == inputs


# --- merging ----------------------------------------------------------------------


def test_complete_appends_the_tool_sections_after_the_snapshot_sections():
    tool = Tool(document())
    merged = il.complete(snapshot(), tool_project=TOOL, run=tool)
    assert tool.inputs == INPUTS
    assert list(merged) == list(snapshot())
    assert list(merged["data"]) == ["scenes", *DATA_KEYS]
    assert list(merged["caps"]) == ["scenes", "il_script_files", "il_assemblies", *CAP_KEYS]
    assert merged["errors"] == [SNAPSHOT_ERROR, TOOL_ERROR]
    expected = snapshot()
    del expected["data"]["il_inputs"]
    expected["data"].update(document()["data"])
    expected["caps"].update(document()["caps"])
    expected["errors"] += document()["errors"]
    assert merged == expected


def test_merge_drops_il_inputs_and_keeps_every_other_value_as_parsed():
    source, scanned = snapshot(), document()
    source_before, scanned_before = copy.deepcopy(source), copy.deepcopy(scanned)
    merged = il.merge(source, scanned)
    assert source == source_before and scanned == scanned_before
    assert "il_inputs" not in merged["data"]
    envelope = lambda value: {key: item for key, item in value.items() if key not in ("data", "caps", "errors")}
    assert envelope(merged) == envelope(source_before)
    # A round trip through the contract model would shorten this to microseconds.
    assert merged["wall_utc"] == WALL_UTC
    assert merged["data"]["scenes"] == source_before["data"]["scenes"]
    assert all(merged["caps"][name] == source_before["caps"][name] for name in source_before["caps"])


def test_the_merged_discovery_is_a_valid_inspection_result():
    result = InspectResult.model_validate(il.merge(snapshot(), document()))
    assert result.stage == "discovery"
    assert result.errors == [SNAPSHOT_ERROR, TOOL_ERROR]


def test_the_cache_still_judges_the_merged_il_evidence():
    names = ["scenes", "game_objects", "components", "fields", "bindings", "input_asset_actions",
             "input_asset_bindings"]

    def merged(scanned):
        snap = snapshot()
        snap["data"] = {**{name: [] for name in names}, "il_inputs": copy.deepcopy(INPUTS)}
        snap["caps"] = {**{name: cap(0) for name in [*names, "source_files", "source_characters"]},
                        "il_script_files": cap(2), "il_assemblies": cap(2)}
        snap["errors"] = []
        scanned["errors"] = []
        return InspectResult.model_validate(il.merge(snap, scanned))

    cache._complete(merged(document()), "discovery", cache._DISCOVERY_CAPS)

    recorded = document()
    recorded["data"]["il_complete"] = False
    recorded["data"]["il_issues"] = [{"kind": "IndirectWrite", "method": "Scripts.Main:Widget:Apply()",
                                      "field_id": None, "il_offset": 7, "reason": "potential indirect write"}]
    recorded["caps"]["il_issues"] = {"limit": 100, "observed": None, "returned": 1, "truncated": None}
    cache._complete(merged(recorded), "discovery", cache._DISCOVERY_CAPS)

    # The merge does not turn an incomplete scan into a complete one, or the reverse.
    unrecorded = document()
    unrecorded["data"]["il_complete"] = False
    with pytest.raises(ValueError, match="unrecorded reason"):
        cache._complete(merged(unrecorded), "discovery", cache._DISCOVERY_CAPS)


def test_merge_accepts_unknown_totals_and_an_incomplete_scan():
    scanned = document()
    scanned["caps"]["il_types"] = {"limit": 10, "observed": None, "returned": 4, "truncated": None}
    scanned["caps"]["il_fields"] = {"limit": 3, "observed": 9, "returned": 3, "truncated": True}
    scanned["data"]["il_complete"] = False
    scanned["errors"] = []
    merged = il.merge(snapshot(), scanned)
    assert merged["caps"]["il_types"] == scanned["caps"]["il_types"]
    assert merged["caps"]["il_fields"] == scanned["caps"]["il_fields"]
    assert merged["data"]["il_complete"] is False
    assert merged["errors"] == [SNAPSHOT_ERROR]


@pytest.mark.parametrize("where,key", [("data", key) for key in DATA_KEYS] + [("caps", key) for key in CAP_KEYS])
def test_merge_refuses_a_key_the_snapshot_already_has(where, key):
    snap = snapshot()
    snap[where][key] = cap(0) if where == "caps" else []
    with pytest.raises(ValueError, match=f"already has IL {where}: {key}"):
        il.merge(snap, document())


def test_merge_refuses_a_snapshot_without_il_inputs():
    snap = snapshot()
    del snap["data"]["il_inputs"]
    with pytest.raises(ValueError, match="no il_inputs"):
        il.merge(snap, document())


BAD_SNAPSHOTS = {
    "list": lambda s: [s],
    "transforms stage": put("transforms", "stage"),
    "fields stage": put("fields", "stage"),
    "no caps": drop("caps"),
    "caps not an object": put([], "caps"),
    "errors not a list": put("boom", "errors"),
    "no session": drop("session"),
    "bad cap": put({"limit": 0, "observed": 0, "returned": 0, "truncated": False}, "caps", "scenes"),
}


@pytest.mark.parametrize("change", BAD_SNAPSHOTS.values(), ids=list(BAD_SNAPSHOTS))
def test_a_snapshot_that_is_not_a_discovery_inspection_is_refused_before_the_scan(change):
    tool = Tool(document())
    with pytest.raises(ValueError):
        il.complete(change(snapshot()), tool_project=TOOL, run=tool)
    assert tool.calls == []
    with pytest.raises(ValueError):
        il.merge(change(snapshot()), document())


# --- il_inputs --------------------------------------------------------------------


BAD_INPUTS = {
    "list": lambda v: [v],
    "null": lambda v: None,
    "text": lambda v: "text",
    "no assemblies": drop("assemblies"),
    "no references": drop("references"),
    "no complete": drop("complete"),
    "extra key": put(1, "extra"),
    "assemblies object": put({}, "assemblies"),
    "assemblies null": put(None, "assemblies"),
    "assembly text": put(["Scripts.Main"], "assemblies"),
    "assembly null": put([None], "assemblies"),
    "assembly without name": drop("assemblies", 0, "name"),
    "assembly without path": drop("assemblies", 0, "path"),
    "assembly extra key": put("1.0", "assemblies", 0, "version"),
    "empty name": put("", "assemblies", 0, "name"),
    "empty path": put("", "assemblies", 0, "path"),
    "numeric name": put(5, "assemblies", 0, "name"),
    "null path": put(None, "assemblies", 0, "path"),
    "duplicate name": put("Scripts.Main", "assemblies", 1, "name"),
    "references object": put({}, "references"),
    "references null": put(None, "references"),
    "numeric reference": put([5], "references"),
    "null reference": put([None], "references"),
    "empty reference": put(["Lib.dll", ""], "references"),
    "complete as one": put(1, "complete"),
    "complete as zero": put(0, "complete"),
    "complete as text": put("true", "complete"),
    "complete as null": put(None, "complete"),
}


@pytest.mark.parametrize("change", BAD_INPUTS.values(), ids=list(BAD_INPUTS))
def test_malformed_il_inputs_are_refused_before_the_tool_runs(change):
    snap = snapshot()
    snap["data"]["il_inputs"] = change(copy.deepcopy(INPUTS))
    tool = Tool(document())
    with pytest.raises(ValueError, match="il_inputs"):
        il.complete(snap, tool_project=TOOL, run=tool)
    assert tool.calls == []


def test_a_snapshot_without_il_inputs_is_refused_before_the_tool_runs():
    snap = snapshot()
    del snap["data"]["il_inputs"]
    tool = Tool(document())
    with pytest.raises(ValueError, match="il_inputs"):
        il.complete(snap, tool_project=TOOL, run=tool)
    assert tool.calls == []


# --- the tool's document ----------------------------------------------------------


BAD_DOCUMENTS = {
    "list": lambda d: [d],
    "null": lambda d: None,
    "no data": drop("data"),
    "no caps": drop("caps"),
    "no errors": drop("errors"),
    "extra top-level key": put([], "extra"),
    "top-level order": reorder(),
    "data not an object": put([], "data"),
    "caps not an object": put([], "caps"),
    "extra data key": put(1, "data", "il_extra"),
    "data order": reorder("data"),
    "extra cap": put(cap(0), "caps", "il_extra"),
    "cap order": reorder("caps"),
    "il_inputs left in data": put(INPUTS, "data", "il_inputs"),
    "empty scope": put("", "data", "il_scope"),
    "numeric scope": put(5, "data", "il_scope"),
    "null scope": put(None, "data", "il_scope"),
    "il_complete as one": put(1, "data", "il_complete"),
    "il_complete as zero": put(0, "data", "il_complete"),
    "il_complete as text": put("true", "data", "il_complete"),
    "il_complete as null": put(None, "data", "il_complete"),
    "errors text": put("boom", "errors"),
    "errors object": put({}, "errors"),
    "numeric error": put([1], "errors"),
    "null error": put([None], "errors"),
}
for _key in DATA_KEYS:
    BAD_DOCUMENTS[f"no {_key}"] = drop("data", _key)
for _key in ("il_write_sets", "il_unlisted_writes", "il_issues"):
    BAD_DOCUMENTS[f"{_key} object"] = put({}, "data", _key)
    BAD_DOCUMENTS[f"{_key} null"] = put(None, "data", _key)
for _key in CAP_KEYS:
    BAD_DOCUMENTS[f"no {_key} cap"] = drop("caps", _key)
    BAD_DOCUMENTS[f"{_key} cap not an object"] = put([], "caps", _key)
    BAD_DOCUMENTS[f"{_key} cap without limit"] = drop("caps", _key, "limit")
    BAD_DOCUMENTS[f"{_key} cap without observed"] = drop("caps", _key, "observed")
    BAD_DOCUMENTS[f"{_key} cap extra key"] = put(1, "caps", _key, "extra")
    BAD_DOCUMENTS[f"{_key} cap with no limit"] = put(0, "caps", _key, "limit")
    BAD_DOCUMENTS[f"{_key} cap returned over limit"] = put(
        {"limit": 5, "observed": 6, "returned": 6, "truncated": False}, "caps", _key)
    BAD_DOCUMENTS[f"{_key} cap observed under returned"] = put(
        {"limit": 100, "observed": 1, "returned": 2, "truncated": False}, "caps", _key)
    BAD_DOCUMENTS[f"{_key} cap truncated though complete"] = put(
        {"limit": 100, "observed": 3, "returned": 3, "truncated": True}, "caps", _key)
    BAD_DOCUMENTS[f"{_key} cap complete though cut short"] = put(
        {"limit": 100, "observed": 5, "returned": 3, "truncated": False}, "caps", _key)
    BAD_DOCUMENTS[f"{_key} cap truncated as zero"] = put(0, "caps", _key, "truncated")
    BAD_DOCUMENTS[f"{_key} cap limit as flag"] = put(True, "caps", _key, "limit")
    BAD_DOCUMENTS[f"{_key} cap limit as text"] = put("100", "caps", _key, "limit")


@pytest.mark.parametrize("change", BAD_DOCUMENTS.values(), ids=list(BAD_DOCUMENTS))
def test_merge_refuses_a_malformed_tool_document(change):
    snap = snapshot()
    before = copy.deepcopy(snap)
    with pytest.raises(ValueError):
        il.merge(snap, change(document()))
    assert snap == before


@pytest.mark.parametrize("change", [put(1, "data", "il_complete"), drop("caps", "il_methods"), put([], "errors", 0)])
def test_complete_refuses_a_malformed_document_the_tool_wrote(change):
    tool = Tool(change(document()))
    with pytest.raises(ValueError):
        il.complete(snapshot(), tool_project=TOOL, run=tool)
    assert len(tool.calls) == 1
    assert not tool.directory.exists()


# --- tool failures ----------------------------------------------------------------


def test_a_failing_tool_is_reported_with_its_trimmed_stderr_even_if_it_wrote_a_document():
    tool = Tool(document(), returncode=3, stderr="\n  cannot read the input: boom \n", stdout="ignored")
    with pytest.raises(ValueError) as caught:
        il.scan(INPUTS, tool_project=TOOL, run=tool)
    assert str(caught.value) == "the IL scan tool failed (exit 3): cannot read the input: boom"
    assert not tool.directory.exists()


def test_a_failing_build_is_reported_from_stdout_when_stderr_is_empty():
    tool = Tool(write=False, returncode=1, stdout="  error CS1002: ; expected\n")
    with pytest.raises(ValueError, match=r"exit 1\): error CS1002: ; expected$"):
        il.scan(INPUTS, tool_project=TOOL, run=tool)


def test_a_failing_tool_with_no_output_still_fails():
    with pytest.raises(ValueError, match=r"exit 2\): no output$"):
        il.scan(INPUTS, tool_project=TOOL, run=Tool(write=False, returncode=2))


FAILING_TOOLS = {
    "non-zero exit": (lambda: Tool(document(), returncode=1, stderr="boom"), "failed"),
    "no dotnet": (lambda: Tool(raises=FileNotFoundError(2, "dotnet")), r"needs the \.NET SDK \(dotnet\) on PATH"),
    "timeout": (lambda: Tool(raises=subprocess.TimeoutExpired("dotnet", 900)), "900 seconds"),
    "no document": (lambda: Tool(write=False), "wrote no document"),
    "not JSON": (lambda: Tool("{not json"), "not valid UTF-8 JSON"),
    "not UTF-8": (lambda: Tool(b"\xff\xfe{}"), "not valid UTF-8 JSON"),
    "empty file": (lambda: Tool(""), "not valid UTF-8 JSON"),
    "list": (lambda: Tool("[]"), "not a JSON object"),
    "null": (lambda: Tool("null"), "not a JSON object"),
    "text": (lambda: Tool('"text"'), "not a JSON object"),
    "number": (lambda: Tool("1"), "not a JSON object"),
}


@pytest.mark.parametrize("make,message", FAILING_TOOLS.values(), ids=list(FAILING_TOOLS))
def test_scan_raises_and_keeps_nothing_when_the_tool_fails(make, message):
    tool = make()
    with pytest.raises(ValueError, match=message):
        il.scan(INPUTS, tool_project=TOOL, run=tool)
    assert len(tool.calls) == 1
    assert not tool.directory.exists()


# --- the command line -------------------------------------------------------------


def saved(tmp_path: Path, value=None, *, text: str | None = None) -> Path:
    path = tmp_path / "discovery-bridge.json"
    path.write_text(json.dumps(snapshot() if value is None else value, ensure_ascii=False) if text is None else text,
                    encoding="utf-8")
    return path


def use(monkeypatch, tool: Tool) -> None:
    monkeypatch.setattr(il, "scan", functools.partial(il.scan, run=tool))


def test_main_writes_the_completed_discovery_with_exclusive_create(monkeypatch, tmp_path: Path, capsys):
    tool = Tool(document())
    use(monkeypatch, tool)
    output = tmp_path / "out" / "discovery.json"
    args = ["--discovery", str(saved(tmp_path)), "--output", str(output), "--tool-project", str(TOOL)]
    assert il.main(args) == 0
    expected = json.dumps(il.merge(snapshot(), document()), indent=2, ensure_ascii=False) + "\n"
    assert output.read_bytes() == expected.encode("utf-8")
    assert "caf\u00e9" in output.read_text(encoding="utf-8")
    assert tool.calls[0][0][3] == str(TOOL)
    printed = capsys.readouterr()
    assert printed.out.count("\n") == 1 and str(output.resolve()) in printed.out
    assert printed.err == ""
    # The saved file is the very document a later stage reads back.
    assert InspectResult.model_validate_json(output.read_bytes()).errors == [SNAPSHOT_ERROR, TOOL_ERROR]


def test_main_uses_the_repository_tool_unless_told_otherwise(monkeypatch, tmp_path: Path):
    seen: list[dict] = []

    def scan(inputs, **kwargs):
        seen.append(kwargs)
        return document()

    monkeypatch.setattr(il, "scan", scan)
    source = saved(tmp_path)
    assert il.main(["--discovery", str(source), "--output", str(tmp_path / "default.json")]) == 0
    other = tmp_path / "Other.csproj"
    assert il.main(["--discovery", str(source), "--output", str(tmp_path / "other.json"),
                    "--tool-project", str(other)]) == 0
    assert seen == [{"tool_project": il.TOOL_PROJECT}, {"tool_project": other}]


@pytest.mark.parametrize("make,message", FAILING_TOOLS.values(), ids=list(FAILING_TOOLS))
def test_main_writes_nothing_when_the_scan_fails(monkeypatch, tmp_path: Path, capsys, make, message):
    use(monkeypatch, make())
    output = tmp_path / "out" / "discovery.json"
    args = ["--discovery", str(saved(tmp_path)), "--output", str(output), "--tool-project", str(TOOL)]
    assert il.main(args) == 1
    assert "discovery completion failed:" in capsys.readouterr().err
    assert not output.parent.exists()


def test_main_writes_nothing_when_the_tool_document_is_malformed(monkeypatch, tmp_path: Path, capsys):
    use(monkeypatch, Tool(put(1, "data", "il_complete")(document())))
    output = tmp_path / "discovery.json"
    args = ["--discovery", str(saved(tmp_path)), "--output", str(output), "--tool-project", str(TOOL)]
    assert il.main(args) == 1
    assert "il_complete must be a boolean" in capsys.readouterr().err
    assert not output.exists()


@pytest.mark.parametrize("text", ["{not json", "[]", "null", json.dumps({**snapshot(), "stage": "transforms"}),
                                  json.dumps({**snapshot(), "data": {}})],
                         ids=["not JSON", "list", "null", "wrong stage", "no il_inputs"])
def test_main_refuses_a_snapshot_it_cannot_complete_without_running_the_tool(monkeypatch, tmp_path: Path, capsys, text):
    tool = Tool(document())
    use(monkeypatch, tool)
    output = tmp_path / "out" / "discovery.json"
    assert il.main(["--discovery", str(saved(tmp_path, text=text)), "--output", str(output)]) == 1
    assert "discovery completion failed:" in capsys.readouterr().err
    assert tool.calls == []
    assert not output.parent.exists()


def test_main_reports_a_missing_snapshot_file(tmp_path: Path, capsys):
    output = tmp_path / "discovery.json"
    assert il.main(["--discovery", str(tmp_path / "absent.json"), "--output", str(output)]) == 1
    assert "discovery completion failed:" in capsys.readouterr().err
    assert not output.exists()


def test_main_refuses_to_overwrite_an_existing_output(monkeypatch, tmp_path: Path):
    tool = Tool(document())
    use(monkeypatch, tool)
    existing = tmp_path / "discovery.json"
    existing.write_text("keep", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        il.main(["--discovery", str(saved(tmp_path)), "--output", str(existing)])
    assert exc.value.code == 2
    assert existing.read_text(encoding="utf-8") == "keep"
    assert tool.calls == []


def test_main_does_not_replace_an_output_that_appears_during_the_scan(monkeypatch, tmp_path: Path, capsys):
    output = tmp_path / "discovery.json"

    def complete(value, **kwargs):
        output.write_text("someone else's", encoding="utf-8")
        return il.merge(value, document())

    monkeypatch.setattr(il, "complete", complete)
    assert il.main(["--discovery", str(saved(tmp_path)), "--output", str(output)]) == 1
    assert "discovery completion failed:" in capsys.readouterr().err
    assert output.read_text(encoding="utf-8") == "someone else's"


def test_the_merge_command_is_registered():
    scripts = tomllib.loads((REPOSITORY / "pyproject.toml").read_text(encoding="utf-8"))["project"]["scripts"]
    assert scripts["playtest-merge-il"] == "playtest.preflight.il:main"
