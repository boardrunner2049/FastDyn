# B1 rule for O4-r2 (settled before any campaign-B timing)

Written 2026-11-26 by the P6 step, before any B1, B2 or H2 plan was made and before any
B timing existed (`/data/scfix-o4/{packet,uart,cli}/timing-B*` and `timing-H2*` did not exist).

## The open point (P5)

decide.py's registered composition (`p5/P5.decide.json` `composition.o4r2`, sha256
`10675e1b…be9`) lists I1, X1 and X4 only, with `contains_addon: false`, so `make_plan.py B1`
would apply the REMOVE rule. The O4-r2 that P5 built also carries A5 (o4_pwhopr: firmware-inline
mode exhaustive, cost_model profile), absorbed under BEST because campaign H showed it faster
than O4-r1 (cli d +1.97%, pooled d +0.93%, 95% [+0.44%, +1.42%]).

## Choice: ADD-confirm

B1 uses the ADD-confirm rule of O4-design §3.8: **pooled LB95 > 0 and LB95_w > log 0.98 on
every timed workload** (G0 required), not REMOVE.

Why:

- A5 is in O4-r2 only because it was measured faster. It is a speed claim, which is what §3.8's
  "if O4-r2 contains an add-on" exists to confirm. REMOVE would let O4-r2 be adopted while
  showing no gain at all (it only bounds the loss at 1%), so the speed claim that put A5 in O4
  would never be confirmed for the composition.
- ADD-confirm is the stricter of the two rules: anything it passes, REMOVE passes too.
- The user's rule is that O4 is the fastest configuration; confirming that the new revision is
  faster than the old one is the direct test of that.

## x_composition written into the B1 plans

`P5.decide.json`'s `composition.o4r2`, amended (recorded in each plan as
`x_composition_amendment`):

- `addons`: `["o4_pwhopr"]` (A5, absorbed under BEST; role "challenger" in decide.py's ITEMS,
  treated as the composition's add-on for B1)
- `contains_addon`: `true`
- `items`: `["A5", "X1", "X4", "I1"]` (add-ons, removals, fidelity: the order make_plan.py writes on a retry)
- `a2_pooled_d["o4_pwhopr"]`: `0.009299589472600199`, its pooled d in campaign H (it was not
  in A2), so that decide.py's retry rule ("drop the accepted add-on with the smallest A2 pooled
  d") names it if B1 fails.
- everything else as registered: `removals` `["o4_nohi", "o4_nospec"]`, `fidelity`
  `["o4_isb"]`, `changes_firmware_inline` and `changes_host_inline` `true`.

## If B1 fails

decide.py then names o4_pwhopr as the add-on to drop (the only one). O4-r2b = O4-r1 + I1 + X1 +
X4 is built on CPUs 21-31 with no timing running, gated, and B1 is repeated once
(`make_plan.py B1 --retry --drop o4_pwhopr`, REMOVE rule, since no add-on remains). If that
fails too, O4 stays at r1. Either way BEST in H already showed o4_pwhopr faster than O4-r1,
which would then be reported as blocked.
