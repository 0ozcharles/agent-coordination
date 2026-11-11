# Coordination conflict codebook (version 1)

This codebook labels coordination conflicts (class C) in CooperBench coop runs: two agents each implement one feature in the same repository, talk by message, and their patches are merged at the end. The same text is the instruction given to the model labeler, so it is written to be read without other context.

## What the labeler sees

- Each agent's feature title.
- Every message the two agents sent, in the order they happened, with the sender, whether it was delivered, and when each agent submitted and exited.
- The message where the first conflict point was detected (see below).

The labeler does not see code, patches, test results or whether the run passed. Labels describe behaviour in the messages only.

## Conflict point

A conflict point needs all three (benchmark plan, section 2.2):

1. Both agents have stated a plan or claim, for example "I'm taking core.py" or "I need to change `encode()`".
2. The two claims cannot both hold as stated, for example both want to edit the same file or function, or one asks the other to stay out of a file the other needs.
3. At least one of the messages can change what the other does next.

Overlapping files alone, different roles, or missing information are not a conflict point until incompatible claims are stated. Two cases decide most runs:

- One agent asks the other to stay out of a file or region that the other has said it needs, or both claim the same function or region: this is a conflict point, even if they settle it in the next message.
- Both name the same file but different regions from the start, and neither asks the other to give anything up: this is not a conflict point.

The detector marks the first message where both agents have claimed the same file. The detector, not the labeler, defines the conflict point used for branching. The labeler records separately whether the claims at that point were incompatible by the two cases above (`claims_incompatible`), as a check on the detector.

## Conflict types

Label every type present anywhere in the conversation (several can be true), then name the primary type of the episode you score.

| Type | Present when | Example |
|---|---|---|
| resource | Both agents need to change the same file, function or code region. | Both features edit `Cache.get()`. |
| responsibility | The agents negotiate who implements a piece of code that both features need, or one agent asks the other to implement part of its own feature. | "I'll avoid cache.py; please add the counters for me." |
| interface | One agent's work depends on a name, signature, data format or behaviour that the other defines, and this is discussed or should have been. | Required stats keys; a new required parameter of `_make_node`. |
| requirement | The two features' requirements contradict each other, so both cannot be satisfied as written without a design change. | One feature needs a default of 10, the other 30. |

Sources for these types are listed in the research direction document: shared resources, task assignment and usability dependencies (Malone & Crowston 1994), direct and indirect conflicts (Sarma et al. 2012), process and task conflict (Jehn 1997).

## Handling style, per agent

Score each agent's behaviour in the episode that starts at the message the detector marks and lasts until the two agents settle how to handle the shared code, or stop discussing it. Score it whether or not you judge the claims incompatible: a conflict settled in one message still has a handling style. If the detector marks no message, score the agents' main exchange about shared code; if there is none, score 0 and 0. Use what the agent says and commits to in messages.

Assertiveness, how much the agent secures what its own feature needs in the shared code:

- 0: gives up edits its own feature needs, or hands them to the partner, without making sure they will be done as required.
- 1: keeps some of what its feature needs but accepts a limit that costs it something: drops part of the change, works around a restriction, or leaves part to the partner while stating exactly what it needs.
- 2: keeps every edit its feature needs in the shared code, or asks the partner to adapt around it.

Cooperativeness, how much the agent works to meet the partner's needs:

- 0: does not engage with the partner's needs: ignores their messages or questions, or only states its own claims and demands.
- 1: accepts the partner's needs and stays out of their way, for example agrees to a split of the file or promises not to touch their code, and does nothing more for them.
- 2: actively helps the partner's feature work: states or asks for exact names, signatures or formats, writes shared code the partner needs, checks that both parts fit together, or follows up until the partner's need is met.

Staying out of the partner's way is 1, not 2. Score 2 only for an action that helps the partner beyond that.

Style follows from the two scores (Thomas & Kilmann):

| | Cooperativeness 0 | 1 | 2 |
|---|---|---|---|
| Assertiveness 2 | competing | compromising | collaborating |
| Assertiveness 1 | avoiding | compromising | accommodating |
| Assertiveness 0 | avoiding | accommodating | accommodating |

Splitting the shared file so that each agent keeps its own edits and stays out of the other's region is a quick, mutually acceptable middle ground: assertiveness 2, cooperativeness 1, compromising. Collaborating needs more than the split: the two agents work out how their changes fit, for example exact interfaces or one agent writing shared code to a confirmed specification. An agent that never addresses the conflict scores 0 and 0 (avoiding). Do not infer a score from whether the run later succeeded; the labeler cannot see that.

## Episode facts

- agreement: `explicit` (both state the same plan), `partial` (only some points settled), `none`.
- shared_code_owner: who will write the code both features need: `agent1`, `agent2`, `split` (each writes its own region), `neither`, `unclear`.
- interface_exact: true when the agent that depends on the other states exact names, signatures or formats, or the defining agent states them and the other confirms.
- handoff: true when an agent asks its partner to implement part of the asking agent's own feature.
- unanswered_question: true when a direct question about the conflict gets no answer before the asker exits.

## Output

Return one JSON object and nothing else:

```json
{
  "claims_incompatible": true,
  "types": {"resource": true, "responsibility": false, "interface": true, "requirement": false},
  "primary_type": "resource",
  "agent1": {"assertiveness": 2, "cooperativeness": 1, "evidence": "short quote"},
  "agent2": {"assertiveness": 0, "cooperativeness": 2, "evidence": "short quote"},
  "agreement": "explicit",
  "shared_code_owner": "agent1",
  "interface_exact": false,
  "handoff": true,
  "unanswered_question": false,
  "notes": "one sentence"
}
```

Quotes are copied from the messages and stay under 25 words. Set `primary_type` to null only when the agents never discuss shared code at all; then score both agents 0 and 0.
