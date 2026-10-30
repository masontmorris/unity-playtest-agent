"""The names the session host, the run script and the client share are written in three languages; they must agree.

The Unity host, the PowerShell script and the Python client cannot be run together here, so the strings that
join them are checked in source: the module the host treats as a session, the variables both sides read, the
order of the host's steps, and that the keyboard catalog stays the other modules' own. The C# behaviour itself
is run by `BridgeSessionCatalogTests` under `dotnet test`.
"""

from __future__ import annotations

import re
from pathlib import Path

from playtest.runner import cli
from playtest.runner.catalog import PATH_VARIABLE, SHA256_VARIABLE

ROOT = Path(__file__).resolve().parents[1]
HOST = (ROOT / "bridge/Tests/PlayMode/BenchRecordingPlayModeTests.cs").read_text(encoding="utf-8")
SELECTOR = (ROOT / "bridge/Runtime/Actions/BridgeSessionCatalog.cs").read_text(encoding="utf-8")
SCRIPT = (ROOT / "scripts/unity/run-session.ps1").read_text(encoding="utf-8")
LIVE_SCRIPT = (ROOT / "scripts/unity/run-live-s1.ps1").read_text(encoding="utf-8")


def test_the_host_and_the_client_name_the_same_module_and_variables():
    assert f'SessionModule = "{cli.__name__}"' in SELECTOR and cli.__name__ == "playtest.runner.cli"
    assert f'PathVariable = "{PATH_VARIABLE}"' in SELECTOR
    assert f'Sha256Variable = "{SHA256_VARIABLE}"' in SELECTOR
    assert f"$env:UPA_BENCH_MODULE = '{cli.__name__}'" in SCRIPT


def test_the_script_exports_the_catalog_pins_it_and_restores_what_it_sets():
    export = SCRIPT.index("playtest.preflight.catalog --manifest")
    launch = SCRIPT.index("& powershell.exe @runnerArgs")
    assert export < SCRIPT.index("Get-FileHash") < SCRIPT.index(f"$env:{PATH_VARIABLE} =") < launch   # exported, pinned, then hosted
    assert f"$env:{SHA256_VARIABLE} = $catalogSha" in SCRIPT and SCRIPT.index(f"$env:{SHA256_VARIABLE} =") < launch
    names = re.search(r"\$names = @\((.*?)\)", SCRIPT, re.S).group(1)
    for variable in (PATH_VARIABLE, SHA256_VARIABLE):
        assert f"'{variable}'" in names                       # saved before the run and put back when it ends
    set_here = set(re.findall(r"\$env:(UPA_[A-Z0-9_]+)\s*=", SCRIPT))
    assert set_here <= set(re.findall(r"'(UPA_[A-Z0-9_]+)'", names))


def test_the_host_selects_the_catalog_before_the_scene_loads_and_serves_what_it_selected():
    select = HOST.index("BridgeSessionCatalog.Select(")
    assert select < HOST.index("SceneManager.LoadSceneAsync")        # a bad session fails before anything starts
    assert "BridgeEditorHost.StartForPreflight(catalog);" in HOST
    assert "BridgeEditorHost.StartForPreflight(BridgeActionCatalog.FromDefinitions" not in HOST
    assert f"BridgeSessionCatalog.PathVariable" in HOST and "BridgeSessionCatalog.Sha256Variable" in HOST


def test_the_script_turns_the_frontier_and_the_recorder_on_only_when_asked():
    from playtest.runner import session_video

    assert "$env:UPA_RUN_FRONTIER = if ($Frontier) { '1' } else { '' }" in SCRIPT
    assert cli.switch("1") is True and "UPA_RUN_FRONTIER" in Path(cli.__file__).read_text(encoding="utf-8")
    assert "$env:UPA_RUN_MAX_FRONTIER_CALLS = if ($Frontier)" in SCRIPT
    launch = SCRIPT.index("& powershell.exe @runnerArgs")
    start = SCRIPT.index(f"-m {session_video.__name__} ")
    assert SCRIPT.index("if ($RecordFrames) {") < start < launch       # recording starts before Unity does
    finish = SCRIPT.index("finally {", launch)
    assert finish < SCRIPT.index("New-Item -ItemType File -Path $recordStop") # and stops only after Unity exits


def test_the_scripts_s1_credentials_match_the_arm_table():
    from playtest.bench import replay

    table = SCRIPT[SCRIPT.index("$s1Credentials = @{"):SCRIPT.index("if (-not $s1Credentials.ContainsKey")]
    declared = {arm: re.findall(r"'([A-Z0-9_.]+)'", names)
                for arm, names in re.findall(r"'([a-z0-9.-]+)' = @\(([^)]*)\)", table)}
    assert declared == {arm: [v for v in (key, url) if v] for arm, (_, key, url) in replay.ARMS.items()}
    assert "$env:UPA_RUN_S1_ARM = $S1Arm" in SCRIPT and "UPA_RUN_S1_ARM" in Path(cli.__file__).read_text(encoding="utf-8")


def test_the_keyboard_bench_catalog_is_still_the_other_modules_own():
    method = HOST[HOST.index("private static BridgeActionCatalog KeyboardBenchCatalog()"):]
    assert '"preflight_key_" + control' in method and "DefaultLeaseS = 0.4" in method
    assert "KeyboardBenchCatalog);" in HOST                            # handed to Select, which a session never calls
    assert "UPA_SESSION_CATALOG" not in LIVE_SCRIPT                    # the live smoke's script does not set or read it
