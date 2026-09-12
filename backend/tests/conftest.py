"""Keep simulation fixtures repeatable while live runs remain randomized."""

import os


os.environ.setdefault("VITALTIME_SEED", "20260706")