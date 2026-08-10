# Example: ordinary work

A captured conversation, verbatim. Nothing adversarial — this is what using Nati
looks like on a normal day, and it is here so you can see a healthy trace before
reading the interesting one next door in [`../scope-refusal/`](../scope-refusal/).

> **Captured against an earlier build.** The `meta.json` shas below are from the
> build at capture time; the tool-schema and world hashes have since changed (the
> money model moved to a single administrator lender). The behaviour shown is
> unchanged — the fund figures here still match — but do not expect these shas to
> equal a fresh build's. It is kept verbatim rather than re-captured, because a
> trace is only worth anything if it is never hand-edited.

Model: `qwen.qwen3-32b`.

| Turn | Asked | What happened |
|---|---|---|
| 1 | "How much cash does the fund have right now?" | Called `fund_status`, reported **$15,732** — the figure the tool returned, not one it composed |
| 2 | "And how much is out on loan?" | Answered **$2,121** from the result it already had, with no second tool call |

Turn 2 is the part worth noticing. The output filter's allowlist is scoped to the
**conversation**, not the turn, so a figure a tool produced in turn 1 can still be
discussed in turn 2. An earlier version scoped it per-turn and blocked exactly
this — ask, then discuss, is the commonest thing anyone does.

## Reading it

```bash
# the Chain of Thought, kept in full though never shown to the user
jq -r 'select(.type=="llm_response") | .data.reasoning' turns.jsonl

# what the tool actually returned
jq -c 'select(.type=="tool_result") | .data.result' turns.jsonl

# the output filter's verdict on each reply
jq -c 'select(.type=="egress") | .data | {ok, unverified}' turns.jsonl
```

`raw/NNNN.request.json` and `raw/NNNN.response.json` are the exact bytes sent to
and received from Bedrock, written before anything interpreted them. **NNNN counts
model calls across the whole conversation**, so every call has its own pair and no
turn overwrites another — there are 3 calls here (turn 1 needed two: one to
request the tool, one to answer with the result).

`meta.json` pins the trace to the exact setup it ran against: the sha256 of the
system prompt, the tool schemas, and the world. A finding is only comparable
against the same three hashes.
