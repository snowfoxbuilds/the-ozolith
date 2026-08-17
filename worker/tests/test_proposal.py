"""The Output Proposal schema and the format-output / view-output CLI
(ADR-0046): per-mode field allowlists, write-time fail-loud rules, fill-state
echo, and the schema-version assert."""

from __future__ import annotations

from pathlib import Path

from theozolith_worker import jobdir, proposal
from theozolith_worker.formatoutput import format_output_main, view_output_main
from theozolith_worker.runner import commit_message_with_trailer, compose_pr_body


def make_job(
    tmp_path: Path,
    *,
    mode: str = jobdir.MODE_RUN,
    round_number: int = 1,
    budget: int = 0,
    schema_version: int = proposal.SCHEMA_VERSION,
) -> Path:
    job = jobdir.create_job_dir(tmp_path, "j1")
    jobdir.write_manifest(
        job,
        jobdir.Manifest(
            run_id="j1",
            mode=mode,
            adapter="claude",
            round=round_number,
            round_budget=budget,
            schema_version=schema_version,
        ),
    )
    return job


# -- run-mode validation (driver side) ----------------------------------------


def run_doc(fields: dict) -> dict:
    return {"schema_version": proposal.SCHEMA_VERSION, "mode": jobdir.MODE_RUN, "fields": fields}


FULL_RUN_FIELDS = {
    "pr-title": "add the widget",
    "pr-description": "Adds the widget the issue asked for.",
    "commit-message": "add the widget\n\nWhy, decisions, dead ends.",
}


def test_run_proposal_requires_the_creation_fields_on_round_one():
    validated, errors = proposal.validate_run(run_doc({}), round_number=1)
    assert validated is None
    assert errors == [
        "pr-title (missing)",
        "pr-description (missing)",
        "commit-message (missing)",
    ]

    validated, errors = proposal.validate_run(run_doc(FULL_RUN_FIELDS), round_number=1)
    assert validated is not None and errors == []
    assert validated.pr_title == "add the widget"
    assert validated.commit_message.startswith("add the widget")


def test_run_proposal_resume_rounds_require_only_the_commit_message():
    """Absent field = no-op, never clear (ADR-0046): on a resume round the
    title and narrative may be omitted to keep what exists."""
    only_commit = {"commit-message": "round 2: tighten the loop\n\ndetails"}
    validated, errors = proposal.validate_run(run_doc(only_commit), round_number=2)
    assert validated is not None and errors == []
    assert validated.pr_title == "" and validated.pr_description == ""

    validated, errors = proposal.validate_run(run_doc({}), round_number=2)
    assert validated is None and errors == ["commit-message (missing)"]


def test_run_proposal_forbidden_mutations_are_unrepresentable():
    """No field can express a label change, an issue transition, or another
    PR — an off-schema key rejects the proposal as a whole (ADR-0046)."""
    for poison in ("labels", "issue-state", "base-branch", "needs_human", "close-pr"):
        doc = run_doc({**FULL_RUN_FIELDS, poison: "x"})
        validated, errors = proposal.validate_run(doc, round_number=1)
        assert validated is None
        assert any(f"unknown field {poison!r}" in error for error in errors)


def test_run_proposal_decisions_fields_compose_the_section():
    doc = run_doc(
        {
            **FULL_RUN_FIELDS,
            "decisions": [{"what": "used sqlite", "why": "no server"}],
            "open-questions": ["is auth in scope?"],
            "remaining-work": ["wire the CLI"],
            "dead-ends": ["ORM X: version conflict"],
            "process-issues": [{"friction": "slow gate"}],
        }
    )
    validated, errors = proposal.validate_run(doc, round_number=1)
    assert validated is not None, errors
    section = validated.section
    assert section.decisions[0].what == "used sqlite"
    assert section.open_questions == ["is auth in scope?"]
    assert section.remaining_work == ["wire the CLI"]
    assert section.dead_ends == ["ORM X: version conflict"]
    assert section.process_issues[0].friction == "slow gate"


def test_run_proposal_malformed_decisions_are_an_error_not_a_drop():
    doc = run_doc({**FULL_RUN_FIELDS, "decisions": [{"why": "no what"}]})
    validated, errors = proposal.validate_run(doc, round_number=1)
    assert validated is None
    assert any("decisions" in error and "what" in error for error in errors)


def test_run_proposal_malformed_process_issues_never_invalidate():
    """Advisory content cannot invalidate a proposal (shared doctrine with
    the verdict path): malformed entries drop at composition."""
    doc = run_doc({**FULL_RUN_FIELDS, "process-issues": [42, {"no": "friction"}]})
    validated, errors = proposal.validate_run(doc, round_number=1)
    assert validated is not None, errors
    assert validated.section.process_issues == []


def test_document_level_refusals():
    validated, errors = proposal.validate_run(None, round_number=1)
    assert validated is None and "no output proposal" in errors[0]

    stale = {"schema_version": 99, "mode": jobdir.MODE_RUN, "fields": dict(FULL_RUN_FIELDS)}
    validated, errors = proposal.validate_run(stale, round_number=1)
    assert validated is None and any("schema_version" in e for e in errors)

    crossed = {
        "schema_version": proposal.SCHEMA_VERSION,
        "mode": jobdir.MODE_REVIEW,
        "fields": dict(FULL_RUN_FIELDS),
    }
    validated, errors = proposal.validate_run(crossed, round_number=1)
    assert validated is None and any("mode" in e for e in errors)


def test_lenient_section_recovers_what_an_invalid_proposal_recorded():
    doc = run_doc({"decisions": [{"what": "partial work recorded"}]})  # commit-message missing
    validated, _ = proposal.validate_run(doc, round_number=1)
    assert validated is None
    section = proposal.lenient_section(doc)
    assert section.decisions[0].what == "partial work recorded"


# -- driver composition helpers ------------------------------------------------


def test_commit_message_trailer_carries_provenance():
    message = commit_message_with_trailer("subject line\n\nbody text\n", "r-9", 7, 2)
    assert message.startswith("subject line\n\nbody text\n\n")
    assert "Ozolith-Run: r-9\n" in message
    assert "Ozolith-Issue: #7\n" in message
    assert message.endswith("Ozolith-Round: 2\n")


def test_compose_pr_body_zones():
    from theozolith_worker import decisions

    section = decisions.DecisionsSection(decisions=[decisions.Decision(what="did it", why="asked")])
    body = compose_pr_body(7, "The narrative zone.", section)
    closes = body.index("Closes #7.")
    narrative = body.index("The narrative zone.")
    section_at = body.index(decisions.BEGIN)
    assert closes < narrative < section_at
    assert decisions.parse(body) == section

    with_intro = compose_pr_body(7, "Narrative.", section, no_change_intro="No change needed.")
    assert with_intro.index("No change needed.") < with_intro.index("Narrative.")


# -- the CLI: write-time rules, echo, status, view ----------------------------


def test_cli_writes_fields_and_echoes_fill_state(tmp_path, capsys):
    job = make_job(tmp_path)
    assert format_output_main(["--job", str(job), "pr-title", "add the widget"]) == 0
    out = capsys.readouterr().out
    assert "pr-title: 14 chars" in out
    assert "still missing required: pr-description, commit-message" in out

    assert format_output_main(["--job", str(job), "pr-description", "The narrative."]) == 0
    assert format_output_main(["--job", str(job), "commit-message", "subject\n\nbody"]) == 0
    out = capsys.readouterr().out
    assert "still missing" not in out.splitlines()[-1]

    raw = proposal.read_raw(job)
    assert raw is not None and raw["schema_version"] == proposal.SCHEMA_VERSION
    assert raw["fields"]["pr-title"] == "add the widget"


def test_cli_reads_multiline_values_from_stdin_and_file(tmp_path, capsys, monkeypatch):
    import io

    job = make_job(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO("subject\n\nlong body from stdin\n"))
    assert format_output_main(["--job", str(job), "commit-message"]) == 0
    fields = proposal.pending_fields(proposal.read_raw(job))
    assert fields["commit-message"] == "subject\n\nlong body from stdin"

    source = tmp_path / "narrative.md"
    source.write_text("narrative from a file\n")
    assert format_output_main(["--job", str(job), "pr-description", "--file", str(source)]) == 0
    fields = proposal.pending_fields(proposal.read_raw(job))
    assert fields["pr-description"] == "narrative from a file"
    capsys.readouterr()


def test_cli_refuses_unknown_fields_listing_the_allowlist(tmp_path, capsys):
    job = make_job(tmp_path)
    assert format_output_main(["--job", str(job), "labels", "needs_human"]) == 1
    err = capsys.readouterr().err
    assert "unknown field 'labels'" in err
    assert "unrepresentable" in err
    assert proposal.read_raw(job) is None  # nothing was written


def test_cli_refuses_invalid_enums_at_write_time(tmp_path, capsys):
    job = make_job(tmp_path, mode=jobdir.MODE_REVIEW, round_number=1, budget=3)
    assert format_output_main(["--job", str(job), "verdict", "merge"]) == 1
    assert "must be one of approve, revise, escalate" in capsys.readouterr().err
    assert format_output_main(["--job", str(job), "deviation", "extreme"]) == 1
    assert "must be one of low, medium, high" in capsys.readouterr().err
    assert proposal.read_raw(job) is None


def test_cli_refuses_a_final_round_revise_at_write_time(tmp_path, capsys):
    """The absorbed validate-verdict contract's sharpest edge (ADR-0046):
    the refusal happens while the session can still change course."""
    job = make_job(tmp_path, mode=jobdir.MODE_REVIEW, round_number=3, budget=3)
    assert format_output_main(["--job", str(job), "verdict", "revise"]) == 1
    err = capsys.readouterr().err
    assert "final-round rule" in err and "approve or escalate" in err
    assert proposal.read_raw(job) is None
    # approve and escalate still write.
    assert format_output_main(["--job", str(job), "verdict", "escalate"]) == 0
    capsys.readouterr()


def test_cli_status_prints_the_table_and_validates(tmp_path, capsys):
    job = make_job(tmp_path)
    assert format_output_main(["--job", str(job), "status"]) == 1
    out = capsys.readouterr().out
    assert "run mode, round 1" in out
    assert "pr-title" in out and "(required)" in out
    assert "INVALID" in out

    for name, value in FULL_RUN_FIELDS.items():
        assert format_output_main(["--job", str(job), name, value]) == 0
    capsys.readouterr()
    assert format_output_main(["--job", str(job), "status"]) == 0
    out = capsys.readouterr().out
    assert "valid: the proposal is complete" in out


def test_cli_view_output_reads_pending_state(tmp_path, capsys):
    job = make_job(tmp_path)
    assert view_output_main(["--job", str(job), "pr-title"]) == 1
    assert "(empty)" in capsys.readouterr().err
    format_output_main(["--job", str(job), "pr-title", "add the widget"])
    capsys.readouterr()
    assert view_output_main(["--job", str(job), "pr-title"]) == 0
    assert capsys.readouterr().out.strip() == "add the widget"
    assert view_output_main(["--job", str(job), "nonsense"]) == 2


def test_cli_asserts_the_manifest_schema_stamp(tmp_path, capsys):
    """Version skew is refused at every invocation — the same contract the
    harness enforces pre-work (defense in depth, ADR-0046)."""
    job = make_job(tmp_path, schema_version=proposal.SCHEMA_VERSION + 1)
    assert format_output_main(["--job", str(job), "pr-title", "x"]) == 2
    assert "out of step" in capsys.readouterr().err
    assert view_output_main(["--job", str(job), "pr-title"]) == 2
    capsys.readouterr()


def test_cli_json_array_fields_are_validated_at_write_time(tmp_path, capsys):
    job = make_job(tmp_path)
    ok = format_output_main(
        ["--job", str(job), "decisions", '[{"what": "did it", "why": "asked"}]']
    )
    assert ok == 0
    assert "decisions: 1 entry" in capsys.readouterr().out

    assert format_output_main(["--job", str(job), "decisions", '[{"why": "no what"}]']) == 1
    assert "what" in capsys.readouterr().err
    assert format_output_main(["--job", str(job), "open-questions", "not json"]) == 1
    assert "JSON array" in capsys.readouterr().err
    assert format_output_main(["--job", str(job), "dead-ends", '["", "empty entry"]']) == 1
    assert "non-empty strings" in capsys.readouterr().err


def test_cli_single_line_fields_reject_newlines(tmp_path, capsys):
    job = make_job(tmp_path)
    assert format_output_main(["--job", str(job), "pr-title", "two\nlines"]) == 1
    assert "single line" in capsys.readouterr().err


def test_cli_refuses_modes_without_a_schema(tmp_path, capsys):
    job = make_job(tmp_path, mode=jobdir.MODE_DRYRUN)
    assert format_output_main(["--job", str(job), "status"]) == 2
    assert "no output-proposal schema" in capsys.readouterr().err
