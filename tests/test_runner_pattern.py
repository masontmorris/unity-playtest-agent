import asyncio
from types import SimpleNamespace as N
from playtest.runner.pattern import PatternFacts

class Inner:
    async def facts(self, *args): return ['base']

def report(choice, rejected=False):
    return N(executed=[N(choice=N(id=choice), execution=N(rejected=rejected))])

def run(choices, rejected=()):
    async def go():
        f=PatternFacts(Inner(), ['yaw', 'constant', 'vector', 'flag'])
        reports=[]
        for i in range(len(choices)+1):
            lines=await f.facts(reports, N(public={'yaw': i % 2, 'constant': 1, 'vector': [i], 'flag': True, 'secret': i}))
            if i < len(choices): reports.append(report(choices[i], i in rejected))
        return lines
    return asyncio.run(go())

def test_alternation_and_filtering():
    assert run('aba') == ['base']
    assert run('abac') == ['base']
    line=run('abab')[1]
    assert 'last 4' in line and 'public.yaw' in line
    assert all(x not in line for x in ('constant', 'vector', 'flag', 'secret'))
    assert all(x not in line.lower() for x in ('should', 'try', 'instead', 'stuck', 'avoid', 'recommend'))

def test_rejected_does_not_count():
    assert run('abab', (2,)) == ['base']
    assert 'last 4' in run('abxab', (2,))[1]

def test_rebind_clears_history():
    f=PatternFacts(Inner(), ['yaw'])
    asyncio.run(f.facts([], N(public={'yaw': 1})))
    f.rebind()
    assert not f._history


def test_cli_flag_and_environment(monkeypatch):
    from playtest.runner import cli
    required = ["--project", "p", "--entity", "e", "--calibration", "c", "--output", "o"]
    monkeypatch.delenv("UPA_RUN_PATTERN_FACTS", raising=False)
    assert not cli._parser().parse_args(required).pattern_facts
    assert cli._parser().parse_args(required + ["--pattern-facts"]).pattern_facts
    monkeypatch.setenv("UPA_RUN_PATTERN_FACTS", "1")
    assert cli._parser().parse_args(required).pattern_facts


def test_float_noise_reads_as_zero_and_an_exact_cycle_is_stated_once():
    """A heading of 3.4e-06 after turning back is 0.0; eight readings alternating two values compress."""
    async def go():
        facts = PatternFacts(Inner(), ['yaw'])
        reports = []
        noise = [0.0, 38.39847, 1.7e-06, 38.39847, 3.4e-06, 38.39847, 3.4e-06, 38.39847, 3.4e-06]
        for i, yaw in enumerate(noise):
            lines = await facts.facts(reports, N(public={'yaw': yaw}))
            if i < len(noise) - 1:
                reports.append(report('right' if i % 2 == 0 else 'left'))
        return lines
    line = asyncio.run(go())[1]
    assert 'e-0' not in line
    assert 'public.yaw alternated 0.0 and 38.4 (8 readings) and now 0.0 (net change 0.0)' in line


from test_speed_finish_options import plan_run  # noqa: E402,F401  (the fixture that runs main to the plan)


def test_the_plan_records_pattern_facts_only_when_on(plan_run, monkeypatch):
    monkeypatch.delenv("UPA_RUN_PATTERN_FACTS", raising=False)
    assert "pattern_facts" not in plan_run()
    assert plan_run("--pattern-facts")["pattern_facts"] is True
