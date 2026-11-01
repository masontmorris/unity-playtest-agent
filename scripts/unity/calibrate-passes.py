import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess

from playtest.preflight.calibrate import (
    merge_prior, new_state_changes, remaining_controls, unattributed_events,
)

# A state change is only carried when the trial that recorded it attributed the change to its own press: its
# own clock and scene window saw the game change across that press. A change the game made with no input is a
# session event, and it is never carried as a control's state change.
IN_TRIAL_ATTRIBUTION = ('stopped', 'scene_changed')


def event_summary(event):
    """The part of a session event a driver summary keeps: where it happened and what it measured."""
    return {'trial': event.get('trial'), 'effect': event.get('effect'), 'cause': event.get('cause'),
            'position': event.get('position'), 'last_control': event.get('last_control'),
            'reason': event.get('reason')}


def stopped_before_press(new):
    """The new state changes whose own trial evidence does not attribute the change to its press.

    A state change is carried into the next pass only when the trial that recorded it saw the game change
    across its own press: its own clock and scene window reports `stopped` or `scene_changed`. A trial whose
    window could not be judged, or whose clock kept running, is not evidence that the control changed
    anything, so the pass that produced it is not trusted onward. A session event is not a state change and
    never appears here.
    """
    return [trial['control'] for trial in new
            if (trial.get('clock') or {}).get('verdict') not in IN_TRIAL_ATTRIBUTION]


def ended_by_session_event(record):
    """Whether this pass's own record ended because the game changed its own state between two trials.

    Such a pass finishes by design: its record holds the session event that ended it, and nothing more was
    measured in that session, so what it did finish is carried onward. The calibration client signals this
    with its own exit code, which the host accepts; the driver still reads the record's evidence itself. An
    event an earlier pass measured and this record only carried does not count: that is the previous
    session's ending, not this one's.
    """
    return any(event.get('prior_pass') is None for event in unattributed_events(record))


def main():
    parser = argparse.ArgumentParser(description="Run fresh PlayMode calibration passes until every control is measured.")
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--entity', type=Path, required=True)
    parser.add_argument('--scene', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--unity')
    parser.add_argument('--max-passes', type=int, default=16)
    args = parser.parse_args()
    if args.max_passes < 1:
        parser.error('--max-passes must be positive')
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    prior = {'schema_version': 1, 'state_changes': [], 'sessions': []}
    summary = {'max_passes': args.max_passes, 'scene': args.scene, 'passes': [], 'complete': False}
    summary_path = output / 'passes.json'
    script = Path(__file__).resolve().parent / 'calibrate-preflight.ps1'
    # A pass with nothing left to measure only assembles the record on a fresh session. If that pass still does
    # not finish, another one would repeat it, so only one is allowed.
    assembling = False
    for index in range(1, args.max_passes + 1):
        directory = output / f'{index:02d}'
        directory.mkdir()
        prior_path = directory / 'prior.json'
        prior_path.write_text(json.dumps(prior, separators=(',', ':')), encoding='utf-8')
        env = dict(os.environ, UPA_SCENE_PATH=args.scene, UPA_CALIBRATION_PRIOR_STATE_CHANGES=str(prior_path))
        cmd = ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', str(script),
               '-ProjectPath', str(args.project.resolve()), '-EntityPath', str(args.entity.resolve()), '-OutputDirectory', str(directory)]
        if args.unity:
            cmd += ['-UnityPath', args.unity]
        with (directory / 'host.log').open('w', encoding='utf-8') as log:
            result = subprocess.run(cmd, env=env, stdout=log, stderr=subprocess.STDOUT)
        results = Path(__file__).resolve().parents[2] / 'runs' / 'bridge-tests' / args.project.name
        for name in ('playmode.log', 'playmode-results.xml'):
            if (results / name).exists():
                shutil.copy2(results / name, directory / name)
        record_path = directory / 'calibration.json'
        record = json.loads(record_path.read_text(encoding='utf-8-sig')) if record_path.exists() else None
        new = new_state_changes(record, prior) if record else []
        events = unattributed_events(record) if record else []
        remaining = remaining_controls(record, prior) if record else []
        # A stopped baseline cannot establish which input changed the state. Do not carry
        # its control into another pass as if the action caused the stop.
        untrusted = stopped_before_press(new)
        # A pass the game ended between trials is a finished pass, not a failed one: the host reports the
        # inconclusive client as a failure, but its record holds the event that ended it and everything it
        # measured before that, so those results are carried onward.
        ended = record is not None and ended_by_session_event(record)
        summary['passes'].append({'output': str(record_path) if record else None,
                                  'status': record.get('status') if record else None,
                                  'new_state_changes': new, 'session_events': [event_summary(event) for event in events],
                                  'remaining_controls': remaining, 'host_exit': result.returncode,
                                  'ended_by_session_event': ended, 'stopped_before_press': untrusted})
        summary_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')
        if record is None or untrusted or (result.returncode and not ended):
            return 1
        prior = merge_prior(prior, record, index)
        if not remaining:
            if record.get('status') == 'measured_with_gaps':
                final_record = directory / 'record.json'
                if not final_record.is_file():
                    return 1
                summary['final_record'] = str(final_record)
                summary['complete'] = True
                summary_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')
                return 0
            if assembling:
                return 1
            assembling = True
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
