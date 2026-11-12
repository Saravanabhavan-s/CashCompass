"""CLI behind scripts/run_eval.py. Exit codes: 0 ok, 2 no real LLM configured
(and not --dry-run), 3 bad dataset."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from sqlalchemy.orm import Session

from app.debate.llm import get_llm_client
from app.eval.dataset import DatasetError, load_dataset
from app.eval.runner import run_eval
from app.exceptions import LLMNotConfigured


def main(argv: list[str] | None = None, *, session: Session | None = None) -> int:
    ap = argparse.ArgumentParser(description="Debate ON vs OFF evaluation on identical evidence snapshots.")
    ap.add_argument("--dataset", default="eval/queries.sample.json")
    ap.add_argument("--out", default="eval/out.json", help="JSON results path; a .md table is written beside it")
    ap.add_argument("--as-of", default=None, help="ISO date used for '+Nd' dates and the projection (default today)")
    ap.add_argument("--dry-run", action="store_true", help="ScriptedLLM plumbing check; output is stamped, NOT results")
    args = ap.parse_args(argv)

    as_of = date.fromisoformat(args.as_of) if args.as_of else date.today()
    try:
        queries = load_dataset(args.dataset, as_of)
    except DatasetError as e:
        print(f"error: {e}", file=sys.stderr)
        return 3

    llm = None
    if not args.dry_run:
        try:
            llm = get_llm_client()
        except LLMNotConfigured as e:
            print(f"error: {e}. Evaluation results need a real model; use --dry-run for a plumbing check.", file=sys.stderr)
            return 2

    owns = session is None
    if owns:
        from app.db import engine

        session = Session(engine)
    try:
        run = run_eval(session, queries, llm, dry_run=args.dry_run, as_of=as_of)
    finally:
        if owns:
            session.close()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(run.model_dump(mode="json"), indent=2, ensure_ascii=False), encoding="utf-8")
    out.with_suffix(".md").write_text(run.markdown, encoding="utf-8")
    print(run.markdown)
    print(f"wrote {out} and {out.with_suffix('.md')}")
    return 0
