# Negotiation style codebook (version 1)

This codebook scores how each player handles the conflict of interest in a two-player buy-sell negotiation. It is the negotiation counterpart of the coordination conflict codebook (docs/C_CONFLICT_CODEBOOK.md): the same two dimensions and the same style grid, with the anchors rewritten for haggling over a price. The same text is the instruction given to the model labeler, so it is written to be read without other context.

## What the labeler sees

- Agent 1 (Player RED) sells one object X. Agent 2 (Player BLUE) buys it with ZUP.
- Every move in the order it happened: the player, what it did (a proposal with a price, ACCEPT of the last proposal, or REJECT, which ends the negotiation) and the message it sent with the move.

The labeler does not see either player's private cost or limit, their private reasoning, or who gained how much. Labels describe behaviour in the moves and messages only.

## Handling style, per player

Score each player over the whole negotiation.

Assertiveness, how much the player pushes for a price good for its own side:

- 0: accepts the other side's proposal without pushing back, or gives up most of the distance in its first move, or ends the negotiation without having argued for a better price at all. Makes no case for its own price.
- 1: holds some ground: makes counter-proposals but concedes steadily, or accepts after the other side has moved only a little toward it.
- 2: holds firm: makes small or no concessions, insists that the other side move, states a floor or ceiling and keeps to it. Walking away counts as 2 only after the player has pushed for its price; a quick REJECT without any own proposal is 0.

Cooperativeness, how much the player works toward a deal that is good for the other side as well:

- 0: does not engage with the other side: ignores their messages or stated constraints, only states demands, or ends the negotiation without trying to find a deal.
- 1: engages enough to reach a deal: answers counter-proposals, moves partway toward the other side, keeps the exchange civil, but does nothing beyond trading numbers.
- 2: works on the other side's problem: asks what they need or can afford, explains its own constraints so that a workable price can be found, or proposes terms that answer a need the other side stated.

Trading numbers politely is 1, however friendly the wording. Score 2 only when the player asks about or acts on the other side's situation.

Style follows from the two scores (Thomas & Kilmann):

| | Cooperativeness 0 | 1 | 2 |
|---|---|---|---|
| Assertiveness 2 | competing | compromising | collaborating |
| Assertiveness 1 | avoiding | compromising | accommodating |
| Assertiveness 0 | avoiding | accommodating | accommodating |

Meeting in the middle after a few rounds is compromising. Collaborating needs more than a split of the difference: the players exchange what they need and settle on a price that answers it. A player that accepts the first price it is offered is accommodating. A player that ends the negotiation at once, or barely responds, is avoiding. Do not infer a score from the final price alone; the labeler cannot see what either side was worth.

## Episode facts

- first_concession: the first player to move its own proposal toward the other side's: `agent1`, `agent2`, or `none` if neither moved (for example an immediate accept or reject).
- asks_needs, per player: true when the player asks about the other side's constraints, budget, cost or reasons, or explicitly invites them to say what they need.

## Output

Return one JSON object and nothing else:

```json
{"agent1": {"assertiveness": 0, "cooperativeness": 0, "asks_needs": false},
 "agent2": {"assertiveness": 0, "cooperativeness": 0, "asks_needs": false},
 "first_concession": "agent1 | agent2 | none"}
```
