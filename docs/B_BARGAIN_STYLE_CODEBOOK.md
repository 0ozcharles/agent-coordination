# Multi-issue bargaining style codebook (version 1)

This codebook scores how each of two players handles a conflict of interest while they negotiate one shared design with several decisions. It is the multi-issue counterpart of the negotiation codebook (docs/N_NEGOTIATION_STYLE_CODEBOOK.md): the same two dimensions and the same style grid. The same text is the instruction given to the model labeler, so it is written to be read without other context.

## What the labeler sees

- Agent 1 and Agent 2 must agree on one option for each of several decisions. Each has private scores for the options; the labeler does not see them.
- Every move in the order it happened: the player, what it did (a full proposal naming an option for every decision, ACCEPT of the other's latest proposal, WALK AWAY, or no valid move) and the message sent with it.

The labeler does not see the scores, the private reasoning, or who gained how much. Labels describe behaviour in the moves and messages only.

## Handling style, per player

Score each player over the whole negotiation.

Assertiveness, how much the player pushes for the design its own side wants:

- 0: accepts the other side's proposal without pushing back, gives up most of its position in its first move, or ends the negotiation without having argued for its own design at all.
- 1: holds some ground: makes counter-proposals but gives way steadily, or accepts after the other side has moved only a little toward it.
- 2: holds firm: makes small or no concessions, insists that the other side move, names what it must have and keeps to it. Walking away counts as 2 only after the player has pushed for its design; a quick WALK without any own proposal is 0.

Cooperativeness, how much the player works toward a design that is good for the other side as well:

- 0: does not engage with the other side: ignores their messages or stated priorities, only states demands, or ends the negotiation without trying to find a deal.
- 1: engages enough to reach a deal: answers counter-proposals, moves partway toward the other side, keeps the exchange civil, but does nothing beyond trading proposals.
- 2: works on the other side's problem: asks which decisions matter most to them, explains its own priorities so that trades across decisions can be found, or proposes packages that give the other side what they said matters most.

Trading proposals politely is 1, however friendly the wording. Score 2 only when the player asks about or acts on the other side's priorities.

Style follows from the two scores (Thomas & Kilmann):

| | Cooperativeness 0 | 1 | 2 |
|---|---|---|---|
| Assertiveness 2 | competing | compromising | collaborating |
| Assertiveness 1 | avoiding | compromising | accommodating |
| Assertiveness 0 | avoiding | accommodating | accommodating |

Splitting the decisions down the middle, or taking the middle option everywhere, after a few rounds is compromising. Collaborating needs more than a split: the players exchange what matters to each and trade decisions so that each gets its way where it matters most. A player that accepts the first proposal it is offered is accommodating. A player that ends the negotiation at once, or barely responds, is avoiding. Do not infer a score from the final design alone; the labeler cannot see what either side was worth.

## Episode facts

- first_concession: the first player to move its own proposal toward the other side's: `agent1`, `agent2`, or `none` if neither moved.
- asks_needs, per player: true when the player asks which decisions matter to the other side, what they need, or invites them to say so.
- discloses_priorities, per player: true when the player says which decisions matter most to it, or gives its scores.

## Output

Return one JSON object and nothing else:

```json
{"agent1": {"assertiveness": 0, "cooperativeness": 0, "asks_needs": false, "discloses_priorities": false},
 "agent2": {"assertiveness": 0, "cooperativeness": 0, "asks_needs": false, "discloses_priorities": false},
 "first_concession": "agent1 | agent2 | none"}
```
