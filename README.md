# Salesforce Follow-Up Agent

An AI agent that reads pipeline and engagement data out of Salesforce, reasons about
what should happen next on each deal, and either proposes or writes back the activity
logs and follow-up tasks. It exists to remove the two chores that quietly rot a CRM:
interactions that never get logged, and follow-ups that never get scheduled.

Built on Claude (`claude-opus-5`) using the Anthropic SDK's tool runner.

> **Scope, stated up front.** This repo ships an in-memory Salesforce org rather than
> connecting to a live one, so it clones and runs with no credentials. Everything above
> the CRM boundary — the tool surface, the policy, the agent loop, the audit trail — is
> the real implementation. Swapping in a live org means writing one class against the
> `CrmClient` interface; there's a sketch at the bottom of this file.

---

## What it actually does

Given a pipeline, the agent:

1. **Scores every open deal against a cadence policy** — how long a deal in each stage
   may go without a customer touch, tightened inside the closing window.
2. **Reads the deals that need attention** — the opportunity record, the account, the
   contacts and their roles, and a merged timeline of tasks, meetings and email
   (including whether outbound mail was actually opened).
3. **Decides on the smallest action that moves the deal** — log an interaction the
   record evidences but never captured, schedule a specific follow-up, or fix an empty
   `NextStep` field.
4. **Writes back, or doesn't** — depending on the mode it was run in.
5. **Reports** — per deal: what the record showed, what it concluded, what it did, and
   what it deliberately left alone.

Every tool call, proposal and commit lands in a JSONL audit trail.

---

## Quickstart

```bash
git clone https://github.com/danceharry-png/salesforce-followup-agent.git
cd salesforce-followup-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

Look at the fixture org and its cadence status. **No API key needed:**

```console
$ sfagent inspect

Open pipeline (5 opportunities)

  OVERDUE  Northwind Logistics - Fleet Analytics Platform  $185,000
           Negotiation/Review | closes in 8d | silent 4d of 2d allowed
           next step: Legal to return redlines on MSA

  OVERDUE  Fenwick Manufacturing - Predictive Maintenance Pilot  $35,000
           Qualification | closes in 12d | silent 11d of 2d allowed
           next step: (empty)

  ON_TRACK Tessellate Software - Platform Expansion  $64,000
           Value Proposition | closes in 21d | silent 1d of 7d allowed
           next step: Send security questionnaire responses by Friday

  OVERDUE  Calderwood Health - Clinical Data Warehouse  $420,000
           Proposal/Price Quote | closes in 34d | silent 6d of 5d allowed
           next step: (empty)

  ON_TRACK Bellweather Retail Group - Store Systems Rollout  $780,000
           Needs Analysis | closes in 76d | silent 7d of 10d allowed
           next step: (empty)
```

Then run the agent (this one needs `ANTHROPIC_API_KEY`):

```bash
export ANTHROPIC_API_KEY=sk-ant-...

sfagent run                                    # suggest mode: proposes, writes nothing
sfagent run --mode review                      # asks y/n before each write
sfagent run --mode auto                        # writes as it goes
sfagent run --opportunity 0065g00000XyZ01AAF   # one deal only
```

Each run writes `runs/<timestamp>/` containing `audit.jsonl`, `result.json`, and — when
the run was allowed to write — `org-after.json`, the org state as the agent left it.

---

## The three modes

The agent is trusted with write access to a system of record, so the write path is the
part of the design that got the most attention.

| Mode | Behaviour | Tool result the model sees |
|---|---|---|
| `suggest` *(default)* | Nothing is written. Intended writes are recorded as proposals. | `PROPOSED (not written to Salesforce…)` |
| `review` | Each write pauses for a y/n at the terminal. | `COMMITTED …` or `DECLINED by the user…` |
| `auto` | Writes are committed as the agent makes them. | `COMMITTED to Salesforce as 00T5g…` |

Two things make this more than a flag:

**Every write goes through one method.** No tool holds a path to the CRM client that
bypasses [`RunContext.propose`](src/sfagent/config.py). That's why "suggest mode cannot
write" is a property of the code rather than a promise, and why it's [testable in one
assertion](tests/test_tools.py).

**The model is told the truth about what happened.** The mode statement is in the system
prompt, and each tool result says plainly whether the write landed. An agent that thinks
it saved something it didn't will cheerfully tell you the work is done — and a declined
action is returned with an explicit instruction not to retry, so a refusal doesn't turn
into a loop.

---

## Tool surface

Three reads, three writes. The writes map one-to-one onto the only three things this
agent may change.

| Tool | Kind | What it does |
|---|---|---|
| `review_pipeline` | read | Open deals with cadence status: `ON_TRACK` / `DUE` / `OVERDUE` |
| `get_opportunity_detail` | read | Record, account, contacts with roles, **and already-open tasks** |
| `get_engagement_history` | read | Tasks + meetings + email merged, newest first, with open state |
| `log_activity` | write | Insert a completed `Task` — an interaction that happened but was never logged |
| `schedule_followup` | write | Insert an open `Task` with a due date and an executable subject |
| `set_next_step` | write | Set `Opportunity.NextStep` |

A few of these choices are load-bearing:

- `get_opportunity_detail` returns **open tasks** because otherwise the agent has no way
  to notice that the next step is already scheduled, and it will duplicate work.
- The timeline distinguishes an **opened** outbound email from an unopened one. The
  policy treats an unopened email as an attempt rather than a touch, which is only
  enforceable if the signal reaches the model.
- Due dates are computed in the tool, not by the model — including the "never schedule
  onto a weekend" rule. Date arithmetic is exactly the kind of thing to take away from
  an LLM.
- Bad input returns an error *string*, not an exception. An exception ends the run; a
  string lets the agent correct itself.

---

## The policy

The interesting judgement lives in [`src/sfagent/playbook.py`](src/sfagent/playbook.py),
in one file, deliberately. A sales manager should be able to read it without reading any
Python, disagree with a number, change it, and get different behaviour on the next run.

It covers cadence SLAs per stage (3 days in Negotiation, 10 in Qualification, dropping to
2 inside the closing window), what counts as a touch, how to read engagement signals —
inbound replies, repeated unanswered outbound, single-threaded deals, named blockers —
and, just as importantly, when to do nothing.

The SLA table is used in two places: rendered into the system prompt, and used by
`review_pipeline` to score each deal. Neither hardcodes the numbers, and [a test
asserts the prompt and the table can't drift apart](tests/test_playbook.py).

---

## How it's put together

```
src/sfagent/
  crm/
    base.py       CrmClient — the interface everything above talks to
    mock.py       MockSalesforceClient — the in-memory org
    models.py     ActivityEntry, ProposedAction
  playbook.py     cadence SLAs + the policy text in the system prompt
  tools.py        the six tools, built per-run and bound to a RunContext
  config.py       AgentConfig, RunContext (the write gate), RunResult
  agent.py        prompt assembly + the tool-runner loop
  cli.py          sfagent inspect / sfagent run
data/org.json     the fixture org
```

Some notes on the implementation:

**Tools are built by a factory, not declared at module scope.** `build_tools(ctx)`
returns `@beta_tool`-decorated closures bound to that run's CRM client and context. No
module-level global, and a test can build a tool set over a throwaway org and call one
tool directly.

**The system prompt is split at a cache breakpoint.** Role, policy and working
instructions are identical on every run and sit in a cached block; only the mode
statement varies, and it sits *after* the breakpoint so switching modes doesn't
invalidate the prefix.

**The fixture org stores relative dates.** `{"days_ago": 12}` rather than a literal
date, materialised at load. A deal that has been quiet for twelve days stays twelve days
quiet whenever the repo is cloned — the demo never goes stale, and the tests pin a fixed
"today" so weekday-sensitive assertions don't flip depending on when CI runs.

**The mock derives `LastActivityDate` rather than storing it**, as Salesforce does. So
logging an activity moves the deal off `OVERDUE` within the same run, which is [tested
end-to-end through the tool layer](tests/test_tools.py).

**Guards on the loop:** a turn cap that reports itself in the summary rather than
silently truncating, and a `stop_reason == "refusal"` branch that says nothing was
written instead of claiming success.

---

## Tests

```console
$ pytest -q
49 passed
```

The suite stubs the Anthropic client, so it needs no API key and costs nothing to run —
that's what lets CI run it on every push across Python 3.10–3.13. It covers the mock
org's Salesforce-like behaviour, the cadence policy's edges, the mode guarantee in all
three modes, input validation, the audit trail, and the agent loop's wiring.

What it deliberately does **not** test is the model's judgement. Asserting that Claude
picks a particular follow-up for a particular deal would be an expensive, flaky test of
something that legitimately varies. Evaluating output quality is a different exercise
from testing the harness, and conflating them gets you a suite you stop trusting.

---

## Pointing it at a real org

The agent never touches Salesforce directly — it talks to
[`CrmClient`](src/sfagent/crm/base.py), an eight-method protocol. A live implementation
is that protocol against SOQL and the sObject REST endpoints:

```python
from datetime import date
from simple_salesforce import Salesforce

class SimpleSalesforceClient:
    """CrmClient backed by a live org. Same eight methods, same shapes."""

    def __init__(self, **credentials):
        self.sf = Salesforce(**credentials)   # username/password/security_token, or OAuth
        self.today = date.today()

    def list_opportunities(self, *, open_only=True, stage=None, owner_id=None):
        where = ["IsClosed = false"] if open_only else []
        if stage:
            where.append(f"StageName = '{stage}'")
        if owner_id:
            where.append(f"OwnerId = '{owner_id}'")
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        soql = (
            "SELECT Id, Name, AccountId, Account.Name, OwnerId, StageName, Amount, "
            "Probability, CloseDate, NextStep, Type, LeadSource, Description, "
            "LastActivityDate, IsClosed "
            f"FROM Opportunity {clause} ORDER BY CloseDate ASC"
        )
        return [self._decorate(r) for r in self.sf.query_all(soql)["records"]]

    def create_task(self, *, opportunity_id, contact_id, subject, activity_type,
                    status, priority, activity_date, description):
        return self.sf.Task.create({
            "WhatId": opportunity_id, "WhoId": contact_id or None,
            "Subject": subject, "Type": activity_type, "Status": status,
            "Priority": priority, "ActivityDate": activity_date,
            "Description": description,
        })["id"]

    def update_opportunity(self, opportunity_id, fields):
        self.sf.Opportunity.update(opportunity_id, fields)

    # ... get_opportunity, get_account, get_contact, list_contacts,
    #     get_activity_timeline (Task + Event + EmailMessage, merged)
```

The field names in `mock.py` are already the real API names — `StageName`, `WhatId`,
`WhoId`, `ActivityDate`, `NextStep` — so the prompt the model sees doesn't change. The
one thing that does: `LastActivityDate` comes back as a real field instead of being
derived, and `DaysSinceLastActivity` / `DaysToClose` still need computing in `_decorate`.

Install the extra with `pip install -e ".[salesforce]"`. Point it at a free
[Developer Edition org](https://developer.salesforce.com/signup) before anything
resembling production, and run in `suggest` mode until you trust what it proposes.

---

## Limitations

- **The org is a fixture.** It behaves like Salesforce in the ways the agent depends on;
  it does not emulate SOQL, validation rules, record types, or sharing. Those are exactly
  why the live client is a separate implementation rather than a subclass.
- **No eval harness.** There are no measured numbers on the quality of the agent's
  follow-up decisions, and this README doesn't claim any. That's the obvious next thing
  to build: a labelled set of deals with expected actions, scored per run.
- **No dedupe against prior runs.** Within a run the agent sees the tasks it created;
  across runs it relies on `get_opportunity_detail` surfacing open tasks. Two `auto`
  runs an hour apart could plausibly schedule overlapping work.
- **Single owner.** The fixture has one AE and the tools don't filter by owner, so
  multi-rep pipelines would need `owner_id` threading through the tool surface.

## Licence

MIT — see [LICENSE](LICENSE).
