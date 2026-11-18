"""``hostsym``: the B3/B4 host-level validator kernel (design §9.2-9.3). WP19.

Modules: :mod:`.state` (terms, byte memory, path state), :mod:`.semantics`
(x86-64 semantics), :mod:`.program` (what is executed), :mod:`.paths` (path
enumeration with the LDST/call/exit abstractions), :mod:`.equiv` (V12 trace
equivalence, the guarded-chain rule, V14 fact certification),
:mod:`.selftest` (V18 native self-test), :mod:`.mutants` (the §9.3 mutation
suite), :mod:`.asm` (a small x86 encoder for synthetic units), :mod:`.link_check`
(the kernel on a NativeLinker v2 result: the ``hostsym=`` inputs of
``validate_b3``), :mod:`.link_mutants` (§9.3 guard/link mutants built with
WP18's emitter and placement).
"""

from .equiv import FactCert, Verdict, certify_fact, check_equiv, check_guarded_chain
from .paths import Exploration, explore
from .program import GSlot, GuardAtom, HelperRef, LdstBlock, Program, World, patched, program_of, world_of

__all__ = ["FactCert", "Verdict", "certify_fact", "check_equiv", "check_guarded_chain",
           "Exploration", "explore", "GSlot", "GuardAtom", "HelperRef", "LdstBlock", "Program",
           "World", "patched", "program_of", "world_of"]
