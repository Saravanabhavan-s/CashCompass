"""Evaluation harness: dataset, both arms on one snapshot, metrics, dry-run stamp, refusal."""
import json
import subprocess
import sys
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from app.config import settings
from app.debate.llm import ScriptedLLM
from app.debate.schemas import Plan
from app.eval import cli
from app.eval.dataset import DatasetError, Expectations, load_dataset, parse_dataset
from app.eval.metrics import ArmResult, QueryResult, check_labels, summarize, to_markdown
from app.eval.runner import DRY_RUN_STAMP, dry_run_responder, run_eval
from app.exceptions import LLMNotConfigured
from app.models.hurdle import Hurdle
from app.services.evidence import get_latest_snapshot
from tests.debate_helpers import handles_in, plan

ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "eval" / "queries.sample.json"
D = Decimal
AS_OF = date(2026, 3, 1)


@pytest.fixture(autouse=True)
def block_network(monkeypatch):
    def boom(*a, **k):
        raise AssertionError("network access attempted")

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)


# ---- dataset ----------------------------------------------------------------
def test_sample_dataset_loads_and_resolves_relative_dates():
    qs = load_dataset(SAMPLE, as_of=AS_OF)
    assert len(qs) == 10 and len({q.id for q in qs}) == 10
    wedding = next(q for q in qs if q.id == "wedding_8m")
    assert wedding.primitives[0].due_date == AS_OF + timedelta(days=240)
    assert wedding.state.savings == D(800000) and wedding.monthly_living_expense == D(30000)
    assert wedding.expectations.must_be_feasible is True and wedding.expectations.max_equity_pct == 0


GOAL = {"type": "GOAL", "target_amount": "1000", "deadline": "+30d", "priority": 1}


@pytest.mark.parametrize(
    "raw",
    [
        [],
        {"queries": []},
        "not a list",
        [{"id": "a", "primitives": [GOAL]}, {"id": "a", "primitives": [GOAL]}],  # duplicate id
        [{"primitives": [GOAL]}],  # missing id
        [{"id": "a", "primitives": []}],  # no primitives
        [{"id": "a", "primitives": [{"type": "MYSTERY"}]}],  # unknown primitive
        [{"id": "a", "primitives": [GOAL], "expectations": {"must_fly": True}}],  # unknown expectation
        [{"id": "a", "primitives": [GOAL], "expectations": {"max_equity_pct": 150}}],
        [{"id": "a", "primitives": [GOAL], "monthly_living_expense": -1}],
        [{"id": "a", "primitives": [GOAL], "surprise": 1}],  # extra field
    ],
)
def test_invalid_datasets_rejected(raw):
    with pytest.raises(DatasetError):
        parse_dataset(raw, AS_OF)


def test_load_dataset_file_errors(tmp_path):
    with pytest.raises(DatasetError):
        load_dataset(tmp_path / "missing.json")
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(DatasetError):
        load_dataset(bad)


def test_dict_form_and_absolute_dates():
    qs = parse_dataset({"queries": [{"id": "x", "primitives": [{**GOAL, "deadline": "2027-01-31"}]}]}, AS_OF)
    assert qs[0].primitives[0].deadline == date(2027, 1, 31)


# ---- label checks -----------------------------------------------------------
def test_check_labels():
    p = plan(30, 70, 0, 0, buf=3, shift=0)
    e = Expectations(must_be_feasible=True, max_equity_pct=0, min_buffer_months=3, must_shift_deadline=False)
    assert check_labels(e, p, True) == {"must_be_feasible": True, "max_equity_pct": True, "min_buffer_months": True, "must_shift_deadline": True}
    e2 = Expectations(must_be_feasible=False, max_equity_pct=0, min_buffer_months=4, must_shift_deadline=True)
    assert check_labels(e2, p, True) == {"must_be_feasible": False, "max_equity_pct": True, "min_buffer_months": False, "must_shift_deadline": False}
    # no plan: only the feasibility label can pass
    assert check_labels(e2, None, False) == {"must_be_feasible": True, "max_equity_pct": False, "min_buffer_months": False, "must_shift_deadline": False}
    assert check_labels(Expectations(), p, True) == {}


# ---- metrics arithmetic -----------------------------------------------------
def _arm(arm, **kw):
    base = dict(arm=arm, status="OK", snapshot_hash="h")
    base.update(kw)
    return ArmResult(**base)


def test_metrics_arithmetic_on_a_tiny_scripted_set():
    on1 = _arm("debate_on", facts_total=4, facts_flagged=1, proposals=3, proposals_policy_violating=1, plan_feasible=True, final_feasible=True,
               confidence=D("0.9000"), label_checks={"a": True, "b": False}, llm_calls=18)
    on2 = _arm("debate_on", facts_total=6, facts_flagged=0, proposals=2, proposals_policy_violating=0, plan_feasible=False, final_feasible=True,
               confidence=D("0.6000"), label_checks={"a": True}, llm_calls=18)
    off1 = _arm("debate_off", facts_total=2, facts_flagged=1, proposals=1, proposals_policy_violating=1, plan_feasible=True, final_feasible=True,
                confidence=D("0.5000"), label_checks={"a": False, "b": False}, llm_calls=1)
    off2 = _arm("debate_off", status="FAILED", error="boom")  # excluded from rates denominators
    s = summarize([QueryResult(id="q1", snapshot_hash="h", debate_on=on1, debate_off=off1),
                   QueryResult(id="q2", snapshot_hash="h", debate_on=on2, debate_off=off2)])
    on, off = s["debate_on"], s["debate_off"]
    assert (on.hallucination_rate, on.facts_flagged, on.facts_total) == (D("0.1000"), 1, 10)  # 1 / 10
    assert off.hallucination_rate == D("0.5000")  # 1 / 2
    assert on.feasibility_rate == D("0.5000") and on.final_feasible_rate == D("1.0000")  # 1/2 ; 2/2
    assert off.feasibility_rate == D("1.0000") and off.queries == 2 and off.failed == 1  # 1/1 ok query
    assert on.policy_violation_rate == D("0.2000") and on.proposals == 5  # 1 / 5
    assert off.policy_violation_rate == D("1.0000")  # 1 / 1
    assert on.label_agreement == D("0.6667") and on.label_checks == 3  # 2 / 3
    assert off.label_agreement == D("0.0000")
    assert on.mean_confidence == D("0.7500") and off.mean_confidence == D("0.5000")
    assert (on.llm_calls, off.llm_calls) == (36, 1)


def test_summary_handles_empty_denominators():
    a = _arm("debate_on")
    s = summarize([QueryResult(id="q", snapshot_hash="h", debate_on=a, debate_off=a.model_copy(update={"arm": "debate_off"}))])
    assert s["debate_on"].hallucination_rate is None and s["debate_on"].label_agreement is None
    md = to_markdown(s)
    assert "n/a" in md and "DRY RUN" not in md


# ---- end-to-end on the DB (ScriptedLLM) -------------------------------------
QUERIES = {
    "queries": [
        {
            "id": "wedding",
            "description": "wedding",
            "primitives": [
                {"type": "ONE_TIME_OUTFLOW", "amount": "500000", "due_date": "+240d"},
                {"type": "GOAL", "target_amount": "500000", "deadline": "+240d", "priority": 1},
                {"type": "RISK_CONSTRAINT", "min_liquidity": "100000", "reason": "buffer"},
            ],
            "state": {"savings": 800000, "recurring_income": 75000},
            "monthly_living_expense": 30000,
            "expectations": {"must_be_feasible": True, "max_equity_pct": 0, "min_buffer_months": 1},
        },
        {
            "id": "impossible",
            "primitives": [{"type": "GOAL", "target_amount": "3000000", "deadline": "+180d", "priority": 1}],
            "state": {"savings": 50000, "recurring_income": 40000},
            "monthly_living_expense": 25000,
            "expectations": {"must_be_feasible": False},
        },
    ]
}


@pytest.fixture
def qs():
    return parse_dataset(QUERIES)


def test_both_arms_same_snapshot_and_call_counts(db_session, qs):
    llm = ScriptedLLM(dry_run_responder)
    run = run_eval(db_session, qs, llm)  # explicit scripted llm, not --dry-run
    assert run.stamp is None and run.dry_run is False and run.n_queries == 2
    w, imp = run.queries
    assert w.debate_on.snapshot_hash == w.debate_off.snapshot_hash == w.snapshot_hash
    hurdle = db_session.query(Hurdle).filter(Hurdle.label == "eval:wedding").one()
    assert get_latest_snapshot(db_session, hurdle.id).snapshot_hash == w.snapshot_hash
    assert (w.debate_on.llm_calls, w.debate_off.llm_calls) == (18, 1)
    assert w.debate_on.status == "OK" and w.debate_off.status == "OK"
    assert w.debate_on.label_checks == {"must_be_feasible": True, "max_equity_pct": True, "min_buffer_months": True}
    assert w.debate_off.label_checks == w.debate_on.label_checks
    # infeasible: both arms short-circuit identically, with no model call
    assert imp.debate_on.status == imp.debate_off.status == "INFEASIBLE"
    assert (imp.debate_on.llm_calls, imp.debate_off.llm_calls) == (0, 0)
    assert imp.debate_on.label_checks == {"must_be_feasible": True}
    assert len(llm.calls) == 19


def test_off_arm_sees_all_items_and_on_agents_only_their_own(db_session, qs):
    llm = ScriptedLLM(dry_run_responder)
    run_eval(db_session, qs[:1], llm)
    hurdle = db_session.query(Hurdle).filter(Hurdle.label == "eval:wedding").one()
    total = get_latest_snapshot(db_session, hurdle.id).item_count
    off = [c for c in llm.calls if c.tag["persona"] == "generalist"]
    on = [c for c in llm.calls if c.tag["persona"] != "generalist"]
    assert len(off) == 1 and len(set(handles_in(off[0].prompt))) == total
    assert all(len(set(handles_in(c.prompt))) < total for c in on if c.tag["round"] == 1)


def test_prompts_in_both_arms_have_no_exact_amounts(db_session, qs):
    llm = ScriptedLLM(dry_run_responder)
    run_eval(db_session, qs[:1], llm)
    for req in llm.calls:
        blob = req.system + req.prompt
        for bad in ("800000", "8,00,000", "800,000", "75000", "75,000", "500000", "5,00,000", "100000", "1,00,000", "30000", "TKN_"):
            assert bad not in blob, (req.tag, bad)


def test_pooled_hallucination_rate_from_real_run(db_session, qs):
    run = run_eval(db_session, qs[:1], ScriptedLLM(dry_run_responder))
    on, off = run.summary["debate_on"], run.summary["debate_off"]
    assert on.facts_total == 18 and on.facts_flagged == 0 and on.hallucination_rate == D("0.0000")  # 6 agents x 3 rounds x 1 FACT
    assert off.facts_total == 1 and off.facts_flagged == 0
    assert on.queries == off.queries == 1 and on.mean_confidence is not None


def test_dry_run_stamp_everywhere(db_session, qs, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "none")  # dry-run must not need any provider
    run = run_eval(db_session, qs[:1], None, dry_run=True)
    assert run.stamp == DRY_RUN_STAMP == "DRY RUN - not results" and run.dry_run is True
    assert run.markdown.startswith("> **DRY RUN - not results**")
    assert "DRY RUN - not results" in run.model_dump_json()
    assert run.llm == "scripted"
    non_dry = run_eval(db_session, qs[:1], ScriptedLLM(dry_run_responder))
    assert "DRY RUN" not in non_dry.markdown and non_dry.stamp is None


def test_refuses_without_llm_unless_dry_run(db_session, qs, monkeypatch):
    monkeypatch.setattr(settings, "llm_provider", "none")
    with pytest.raises(LLMNotConfigured):
        run_eval(db_session, qs, None, dry_run=False)
    assert db_session.query(Hurdle).filter(Hurdle.label.like("eval:%")).count() == 0  # refused before touching the DB


def test_cli_refuses_with_exit_code_2(monkeypatch, capsys):
    monkeypatch.setattr(settings, "llm_provider", "none")
    assert cli.main(["--dataset", str(SAMPLE)]) == 2
    assert "--dry-run" in capsys.readouterr().err


def test_script_exit_code_2_without_llm():
    import os

    env = {**os.environ, "LLM_PROVIDER": "none"}
    p = subprocess.run([sys.executable, str(ROOT / "scripts" / "run_eval.py"), "--dataset", str(SAMPLE)],
                       capture_output=True, text=True, env=env, cwd=ROOT, timeout=120)
    assert p.returncode == 2 and "error" in p.stderr


def test_cli_bad_dataset_exit_code_3(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("[]")
    assert cli.main(["--dataset", str(bad), "--dry-run"]) == 3


def test_cli_dry_run_on_shipped_sample(db_session, tmp_path):
    out = tmp_path / "res.json"
    assert cli.main(["--dataset", str(SAMPLE), "--out", str(out), "--dry-run"], session=db_session) == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["stamp"] == "DRY RUN - not results" and data["n_queries"] == 10
    assert set(data["summary"]) == {"debate_on", "debate_off"}
    assert all(q["debate_on"]["snapshot_hash"] == q["debate_off"]["snapshot_hash"] for q in data["queries"])
    md = out.with_suffix(".md").read_text(encoding="utf-8")
    assert md.startswith("> **DRY RUN - not results**") and "| metric | Debate ON | Debate OFF |" in md
