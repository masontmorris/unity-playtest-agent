import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import shutil

from playtest.preflight.scene_selection import select_scene


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--unity')
    parser.add_argument('--stop-on-identified', action='store_true')
    args = parser.parse_args()
    settings = (args.project / 'ProjectSettings/EditorBuildSettings.asset').read_text(encoding='utf-8')
    scenes = re.findall(r'(?m)^  - enabled: 1\s*\n\s*path: (.+?)\s*$', settings)
    if not scenes:
        raise ValueError('No enabled build scenes found')
    args.output.mkdir(parents=True, exist_ok=False)
    scripts = Path(__file__).resolve().parent
    rows = [{"scene_path": scene, "measurement": {"error": "not measured"}} for scene in scenes]
    (args.output / "scene-selection.json").write_text(json.dumps(select_scene(rows), indent=2), encoding="utf-8")
    for index, scene in enumerate(scenes):
        directory = args.output / f'{index:02d}'
        directory.mkdir()
        env = dict(os.environ, UPA_SCENE_PATH=scene)
        measurement = directory / 'entity.json'
        errors = []
        for script, opts in [('capture-preflight.ps1', ['-OutputDirectory', str(directory / 'discovery')]),
                             ('measure-controlled-entity.ps1', ['-OutputPath', str(measurement)])]:
            cmd = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(scripts / script),
                   '-ProjectPath', str(args.project), *opts]
            if args.unity:
                cmd += ['-UnityPath', args.unity]
            with (directory / (script + '.log')).open('w', encoding='utf-8') as log:
                result = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
            results_dir = scripts.parent.parent / 'runs' / 'bridge-tests' / args.project.name
            platforms = ('playmode',) if script == 'measure-controlled-entity.ps1' else ('editmode', 'playmode')
            for platform in platforms:
                for suffix in ('-results.xml', '.log'):
                    source = results_dir / (platform + suffix)
                    if source.exists():
                        shutil.copy2(source, directory / (script + '-' + source.name))
            if result.returncode:
                errors.append(f'{script} exited {result.returncode}; see log')
        data = json.loads(measurement.read_text(encoding='utf-8-sig')) if measurement.exists() else {'error': '; '.join(errors) or 'measurement unavailable'}
        rows[index] = {'scene_path': scene, 'measurement': data, 'measurement_path': str(measurement) if measurement.exists() else None}
        selection = select_scene(rows)
        (args.output / 'scene-selection.json').write_text(json.dumps(selection, indent=2), encoding='utf-8')
        if args.stop_on_identified and selection['status'] == 'selected':
            break
    return 0

if __name__ == '__main__':
    raise SystemExit(main())
