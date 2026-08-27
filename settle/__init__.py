"""settle — a recovery agent that is correct when it cannot trust what it is
told about outcomes.

Package layout is load-bearing, not cosmetic:

    settle/schema/   frozen contracts. Importable by every other package.
    settle/sim/      the world, including hidden truth. Importable by NOTHING
                     under settle/agent/, settle/policy/ or settle/schema/.

INV-8 is enforced by location, not by discipline. See tests/test_schema.py
(SCH-3), which walks the AST of every module under settle/schema/ and asserts
none of them imports from settle.sim.
"""

__all__ = []
