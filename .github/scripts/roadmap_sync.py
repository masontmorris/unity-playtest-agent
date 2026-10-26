"""Reconcile the roadmap project with the repo's issues and pull requests.

Idempotent: it reads the whole state and changes only what differs, so a missed or out-of-order
event is fixed on the next run. For every issue in the repo:

- it is on the project;
- `Day`, `Start` and `Target` follow its "Day N – ..." milestone (Day N = 25 Oct 2026 + N days);
- `Status` is Done when the issue is closed, and In Progress when an open PR says it closes the
  issue. Otherwise a status someone set by hand is left alone; a new item gets Todo.

Needs GH_TOKEN with the `project` scope. `--dry-run` prints the changes without making them.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from datetime import date, timedelta

OWNER, REPO, PROJECT = "masontmorris", "unity-playtest-agent", 2
DAY0 = date(2026, 10, 25)
DRY = "--dry-run" in sys.argv


def gql(query: str, **variables) -> dict:
    args = ["gh", "api", "graphql", "-f", f"query={query}"]
    for k, v in variables.items():
        args += ["-F" if isinstance(v, int) else "-f", f"{k}={v}"]
    out = subprocess.run(args, capture_output=True, text=True, encoding="utf-8")
    if out.returncode:
        raise SystemExit(out.stderr)
    return json.loads(out.stdout)["data"]


PROJECT_Q = """
query($owner: String!, $number: Int!, $after: String) {
  user(login: $owner) { projectV2(number: $number) {
    id
    fields(first: 50) { nodes {
      ... on ProjectV2Field { id name }
      ... on ProjectV2SingleSelectField { id name options { id name } } } }
    items(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { id
        content { ... on Issue { number repository { name } } }
        fieldValues(first: 20) { nodes {
          ... on ProjectV2ItemFieldDateValue { date field { ... on ProjectV2Field { name } } }
          ... on ProjectV2ItemFieldSingleSelectValue { name field { ... on ProjectV2SingleSelectField { name } } } } } } } } }
}"""

ISSUES_Q = """
query($owner: String!, $repo: String!, $after: String) {
  repository(owner: $owner, name: $repo) {
    issues(first: 100, after: $after) {
      pageInfo { hasNextPage endCursor }
      nodes { id number state milestone { title }
        closedByPullRequestsReferences(first: 10, includeClosedPrs: false) { nodes { state } } } } }
}"""


def paged(query: str, path: list[str], **v) -> tuple[dict, list[dict]]:
    after, nodes, root = None, [], None
    while True:
        data = gql(query, **v, **({"after": after} if after else {}))
        node = data
        for key in path:
            node = node[key]
        root = root or data
        nodes += node["nodes"]
        if not node["pageInfo"]["hasNextPage"]:
            return root, nodes
        after = node["pageInfo"]["endCursor"]


def main() -> int:
    root, items = paged(PROJECT_Q, ["user", "projectV2", "items"], owner=OWNER, number=PROJECT)
    project = root["user"]["projectV2"]
    fields = {f["name"]: f for f in project["fields"]["nodes"] if f}
    for name in ("Status", "Day", "Start", "Target"):
        if name not in fields:
            raise SystemExit(f"project field {name!r} is missing")
    option = {name: {o["name"]: o["id"] for o in fields[name]["options"]} for name in ("Status", "Day")}

    current: dict[int, tuple[str, dict]] = {}
    for it in items:
        c = it["content"] or {}
        if c.get("repository", {}).get("name") == REPO:
            vals = {fv["field"]["name"]: fv.get("date") or fv.get("name")
                    for fv in it["fieldValues"]["nodes"] if fv and fv.get("field")}
            current[c["number"]] = (it["id"], vals)

    _, issues = paged(ISSUES_Q, ["repository", "issues"], owner=OWNER, repo=REPO)
    changes = 0

    def set_field(item: str, field: str, *, date_: str | None = None, opt: str | None = None) -> None:
        nonlocal changes
        changes += 1
        if DRY:
            return
        value = f'{{date: "{date_}"}}' if date_ else f'{{singleSelectOptionId: "{opt}"}}'
        gql(f"""mutation($p: ID!, $i: ID!, $f: ID!) {{ updateProjectV2ItemFieldValue(input:
            {{projectId: $p, itemId: $i, fieldId: $f, value: {value}}}) {{ projectV2Item {{ id }} }} }}""",
            p=project["id"], i=item, f=fields[field]["id"])

    for issue in sorted(issues, key=lambda i: i["number"]):
        n = issue["number"]
        if n not in current:
            print(f"#{n}: add to project")
            changes += 1
            item = "DRY" if DRY else gql(
                "mutation($p: ID!, $c: ID!) { addProjectV2ItemById(input: {projectId: $p, contentId: $c}) { item { id } } }",
                p=project["id"], c=issue["id"])["addProjectV2ItemById"]["item"]["id"]
            current[n] = (item, {})
        item, vals = current[n]

        m = re.match(r"Day (\d+)\b", (issue["milestone"] or {}).get("title", ""))
        if m:
            day = int(m.group(1))
            when = (DAY0 + timedelta(days=day)).isoformat()
            if vals.get("Day") != f"Day {day}" and f"Day {day}" in option["Day"]:
                print(f"#{n}: Day {vals.get('Day')} -> Day {day}")
                set_field(item, "Day", opt=option["Day"][f"Day {day}"])
            for f in ("Start", "Target"):
                if vals.get(f) != when:
                    print(f"#{n}: {f} {vals.get(f)} -> {when}")
                    set_field(item, f, date_=when)

        open_pr = any(pr["state"] == "OPEN" for pr in issue["closedByPullRequestsReferences"]["nodes"])
        status = vals.get("Status")
        want = "Done" if issue["state"] == "CLOSED" else "In Progress" if open_pr else (status or "Todo")
        if issue["state"] == "OPEN" and status == "Done":
            want = "In Progress" if open_pr else "Todo"  # reopened
        if want != status:
            print(f"#{n}: Status {status} -> {want}")
            set_field(item, "Status", opt=option["Status"][want])

    print(f"{'would make' if DRY else 'made'} {changes} change(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
