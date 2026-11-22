# Direct-slot lookup in the SC6 runtime

The optional `qemu-continuity-direct.patch` builds an index of direct-slot links
when it loads a validated SC6 extension. Each ordinary chain slot keeps a
singleton pointer when it has only one direct source. That case needs no hash
allocation or lookup, even when the extension has many unrelated SPEC links.
A second direct source in the same slot promotes it to a GLib direct-pointer
hash table keyed by the source TB's cache address. Ordinary return/static
specialization links are absent from the index.

An ordinary dispatcher chaining attempt checks its slot's singleton or performs
one hash lookup for its source. It retains the exact slot, target PC,
live-source and live-destination checks, then delegates to the existing
`sc6_try_arm()` conditions and jump-list
ownership. Unmatched attempts use QEMU's ordinary linking path. The dispatcher
does not call the lookup when the loaded artifact contains no direct links.

This removes the scan of every SC6 link from each direct-link lookup. The index
is immutable and lives with the loaded extension. Its cache addresses remain
fixed across flush epochs; the existing `sc6.live`, armed/disarmed flags and
invalidation rules still control whether a link can be used. Clearing live
identities during a flush makes old index entries ineligible until their TBs
are activated again. This index does not make stale TBs executable.

`tests/unit/test_direct_slot_index.py` compiles the actual index and lookup
functions extracted from the overlay, links the system GLib runtime and checks
singleton and multiple sources, both slots, misses, live-identity changes and
delegation to the arm gate. The fixture substitutes a recorder for QEMU's existing arm
operation; it is a correctness test, not an invalidation integration test or a
performance workload. It requires no fixed-address mappings or ASLR change.

Apply the overlay to the pinned QEMU source and rebuild before deploying it.
The CPU execution-loop overlay is unchanged, so its reviewed helper-effect
source hash is unchanged. The resulting executable identity changes: collect
new runtime-bound cache artifacts or establish their compatibility with the
existing runtime checks. Earlier campaign results predate this index.
A matched real-application campaign is still needed to measure its
effect, including dispatcher-heavy runs and multiple direct links.
