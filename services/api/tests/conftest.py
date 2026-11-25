import os

# Generous limits for the suite; tests/test_limits_and_v1.py sets tight ones explicitly.
os.environ.setdefault("RATE_LIMIT_RUNS_PER_MINUTE", "10000")
os.environ.setdefault("RATE_LIMIT_REQUESTS_PER_MINUTE", "10000")
