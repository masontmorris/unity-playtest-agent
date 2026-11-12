import os
import subprocess
import sys
import time
from pathlib import Path

import pytest


@pytest.mark.skipif(os.name != "nt", reason="Windows job object lifecycle")
def test_supervisor_death_kills_owned_process_tree(tmp_path):
    marker = tmp_path / "pids.json"
    child = "import json,os,pathlib,subprocess,sys,time; p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(120)']); pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),p.pid])); time.sleep(120)"
    supervisor = "from playtest.preflight.three_session import ProcessJob,_start_owned; import os,pathlib,subprocess,sys,time; j=ProcessJob(); p=_start_owned(j,[sys.executable,'-c',sys.argv[2],sys.argv[1]],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); target=pathlib.Path(sys.argv[1]); deadline=time.monotonic()+15; " + "\nwhile not target.exists() and time.monotonic()<deadline: time.sleep(.05)\n" + "os._exit(0 if target.exists() else 2)"
    result = subprocess.run([sys.executable, "-c", supervisor, str(marker), child], timeout=25)
    assert result.returncode == 0
    import json
    from playtest.preflight.three_session import _alive
    pids = json.loads(marker.read_text())
    deadline = time.monotonic() + 10
    while any(_alive(pid) for pid in pids) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not any(_alive(pid) for pid in pids)
