"""The selected name/params survive binding; an action default never rewrites a skill bound."""
import asyncio
from datetime import datetime, timezone

from playtest.contracts.bridge import ExecutionRecord, ExecStatus, RejectReason
from playtest.contracts.loop import Choice, ChoiceKind, ChoiceSource
from playtest.core.execution import ExecutionController


def test_action_defaults_and_explicit_skill_bounds_are_bound_without_choice_repair():
    class Client:
        def __init__(self):
            self.sent = []

        async def act(self, **request):
            self.sent.append(request)
            rejected = bool(request["params"])
            return [ExecutionRecord(
                session="s", seq=len(self.sent), wall_utc=datetime.now(timezone.utc),
                action_id=request["action_id"], status=ExecStatus.rejected if rejected else ExecStatus.completed,
                reject_reason=RejectReason.invalid_params if rejected else None)]

    async def run():
        client = Client()
        executor = ExecutionController(client, lease_s=0.2)
        action = Choice(id="a", kind=ChoiceKind.action, action="combined", label="Combined",
                        source=ChoiceSource.manifest)
        skill = Choice(id="s", kind=ChoiceKind.skill, skill="sequence", label="Sequence",
                       source=ChoiceSource.manifest)
        await executor.execute(action, based_on_seq=5)
        await executor.execute(skill, based_on_seq=6)
        await executor.execute(skill, based_on_seq=7, lease_s=1.5)
        invalid = action.model_copy(update={"params": {"unsupported": "value"}})
        result = await executor.execute(invalid, based_on_seq=8)
        assert [request["action"] for request in client.sent] == ["combined", "sequence", "sequence", "combined"]
        assert [request["lease_s"] for request in client.sent] == [0.2, None, 1.5, 0.2]
        assert [request["based_on_seq"] for request in client.sent] == [5, 6, 7, 8]
        assert client.sent[-1]["params"] == invalid.params
        assert result.status is ExecStatus.rejected
        assert result.reject_reason is RejectReason.invalid_params
        assert len(client.sent) == 4  # no repair, retry or substitute after rejection

    asyncio.run(run())


def test_host_leases_bind_by_choice_and_never_replace_a_skills_own_bound():
    class Client:
        def __init__(self):
            self.sent = []

        async def act(self, **request):
            self.sent.append(request)
            return [ExecutionRecord(
                session="s", seq=len(self.sent), wall_utc=datetime.now(timezone.utc),
                action_id=request["action_id"], status=ExecStatus.completed)]

    async def run():
        client = Client()
        executor = ExecutionController(client, lease_s=0.2, leases={"mapped_action": 0.3, "mapped_skill": 1.5})
        action = Choice(id="mapped_action", kind=ChoiceKind.action, action="combined", label="Combined",
                        source=ChoiceSource.manifest)
        skill = Choice(id="mapped_skill", kind=ChoiceKind.skill, skill="sequence", label="Sequence",
                       source=ChoiceSource.manifest)
        unbounded_skill = Choice(id="other_skill", kind=ChoiceKind.skill, skill="other", label="Other",
                                 source=ChoiceSource.manifest)
        await executor.execute(action, based_on_seq=1)
        await executor.execute(skill, based_on_seq=2)
        await executor.execute(unbounded_skill, based_on_seq=3)
        assert [request["lease_s"] for request in client.sent] == [0.3, 1.5, None]

    asyncio.run(run())
