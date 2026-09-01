"""Live third-party edges. SPEC §16.

Simulated at scale, real at the edges. Everything else in `settle/` runs against
a simulator whose ground truth we construct; this package is the one place that
talks to a system we do not control, and it is deliberately small.

The rule the package exists to enforce: every record it returns carries an
explicit `source`, and a synthetic record can never be mistaken for a real one.
"""
