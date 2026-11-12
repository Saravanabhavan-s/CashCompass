"""Debate ON vs OFF evaluation.

  python scripts/run_eval.py --dry-run                     # plumbing only; stamped "DRY RUN - not results"
  python scripts/run_eval.py --dataset eval/queries.sample.json --out eval/out.json
      (needs LLM_PROVIDER=anthropic|ollama and a reachable Postgres via DATABASE_URL)

Exit code 2 when no real LLM is configured and --dry-run was not given.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.eval.cli import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
