"""Drive report: once a drive is closed, HR downloads one PDF that documents the whole drive.

build() turns DB rows into plain JSON-able data (no I/O, unit-tested); render() hands that data to
the Typst template as JSON, so candidate text is never parsed as markup."""

import json
import statistics
from datetime import datetime
from pathlib import Path
from uuid import UUID
from zoneinfo import ZoneInfo

import typst
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.auth.schemas import StaffUser
from app.core.audit import audit
from app.core.errors import AppError
from app.notifications.round_invites import LABELS
from app.reports import repo

TEMPLATE = Path(__file__).parent / "templates" / "drive_report.typ"
# shortcut: one display zone for every reader; take it from the drive if campuses span zones
ZONE = ZoneInfo("Asia/Kolkata")
TEST_ROUNDS = {"aptitude", "coding", "technical"}  # scored in percent; the rest are HR-decided
PASSED = {"passed", "selected"}
FAILED = {"failed", "rejected"}
LOCK_AT = 3  # tab switches that lock an attempt (D6)


def _label(round_type: str) -> str:
    return LABELS.get(round_type, round_type.replace("_", " ").title())


def _num(v) -> str:
    """93.0 -> '93', 90.456 -> '90.5'."""
    return f"{float(v):.1f}".removesuffix(".0")


def _day(dt: datetime | None) -> str:
    return dt.astimezone(ZONE).strftime("%d %b %Y") if dt else "—"


def _score(round_type: str, v) -> str:
    if v is None:
        return "—"
    if round_type in TEST_ROUNDS:
        return f"{_num(v)}%"
    return f"{round(float(v), 2):g} / 5"  # 1-5 average: keep 4.25, not 4.2


def _round(r, rows: list) -> dict:
    is_test = r["type"] in TEST_ROUNDS
    scores = [float(x["score"]) for x in rows if x["score"] is not None]
    buckets = [0] * 10
    if is_test:
        for s in scores:
            buckets[min(int(s // 10), 9)] += 1  # 100% belongs in the 90-100 band
    top = sorted((x for x in rows if x["score"] is not None), key=lambda x: -float(x["score"]))
    absent = sum(1 for x in rows if x["status"] == "absent")
    if not is_test:
        cutoff = "HR decision"
    elif r["threshold"] is not None:
        cutoff = f"{_num(r['threshold'])}%"
    else:
        cutoff = "—"
    return {
        "name": _label(r["type"]),
        "is_test": is_test,
        "date": _day(r["first_at"]),
        "duration_min": round(r["duration_sec"] / 60) if r["duration_sec"] else None,
        "cutoff": cutoff,
        "invited": len(rows),
        "sat": len(rows) - absent,
        "absent": absent,
        "passed": sum(1 for x in rows if x["status"] in PASSED),
        "failed": sum(1 for x in rows if x["status"] in FAILED),
        "avg": _score(r["type"], statistics.fmean(scores)) if scores else "—",
        "median": _score(r["type"], statistics.median(scores)) if scores else "—",
        "max": _score(r["type"], max(scores)) if scores else "—",
        "buckets": buckets,
        "has_scores": bool(scores),
        "top": [
            {"name": x["full_name"], "sid": x["student_id"], "score": _score(r["type"], x["score"])}
            for x in top[:3]
        ],
        "flags": {
            "tab_switches": sum(1 for x in rows if x["tab_switches"] > 0),
            "locked": sum(1 for x in rows if x["tab_switches"] >= LOCK_AT),
        },
        # HR-decided rounds: every student, the AI's advice next to HR's call
        "decisions": []
        if is_test
        else [
            {
                "name": x["full_name"],
                "sid": x["student_id"],
                "ai": _score(r["type"], x["score"]),
                "decision": "Pass"
                if x["status"] in PASSED
                else "Fail"
                if x["status"] in FAILED
                else "—",
                "note": x["hr_note"] or "",
            }
            for x in rows
        ],
    }


def _describe(entry, labels: dict[str, str]) -> str:
    d = entry["diff"] if isinstance(entry["diff"], dict) else json.loads(entry["diff"] or "{}")
    rnd = labels.get(str(entry["entity_id"]), "a round")
    match entry["action"]:
        case "drive.create":
            return "Created the drive"
        case "drive.reassign":
            return "Handed the drive to another HR owner"
        case "candidate.import":
            return f"Imported {d.get('created', 0)} new candidates, updated {d.get('updated', 0)}"
        case "candidate.shortlist":
            verb = "Shortlisted" if d.get("shortlisted") else "Removed from shortlist"
            return f"{verb} {d.get('count', '?')} candidates"
        case "round.advance":
            cut = f" at {_num(d['threshold'])}% cut-off" if d.get("threshold") is not None else ""
            return (
                f"Advanced the {rnd}{cut}: {d.get('passed', 0)} passed, "
                f"{d.get('failed', 0)} failed, {d.get('absent', 0)} absent"
            )
        case "round.decide":
            passed = d.get("passed", [])
            n = len(passed) if isinstance(passed, list) else passed
            closed = "; drive closed" if d.get("drive_closed") else ""
            return f"Decided the {rnd}: {n} passed, {d.get('failed', 0)} failed{closed}"
    return entry["action"]


def build(drive, rounds: list, rows: list, log: list, by: StaffUser, now: datetime) -> dict:
    by_round: dict = {}
    for x in rows:
        by_round.setdefault(x["round_id"], []).append(x)
    scored = [r for r in rounds if r["type"] != "shortlist"]
    labels = {str(r["id"]): _label(r["type"]) for r in rounds}
    sections = [_round(r, by_round.get(r["id"], [])) for r in scored]

    funnel = [{"stage": "Applied", "count": drive["applied"]}]
    if any(r["type"] == "shortlist" for r in rounds) or drive["shortlisted"]:
        funnel.append({"stage": "Shortlisted", "count": drive["shortlisted"]})
    for r, s in zip(scored, sections, strict=True):
        stage = "Selected" if r["type"] == "final" else f"Passed {s['name']}"
        funnel.append({"stage": stage, "count": s["passed"]})

    last = scored[-1] if scored else None
    finalists = [x for x in by_round.get(last["id"], []) if x["status"] in PASSED] if last else []
    selected = len(finalists)
    scores = {(x["round_id"], x["candidate_id"]): x["score"] for x in rows}
    ended = max((r["last_at"] for r in rounds if r["last_at"]), default=None)
    days = (ended - drive["created_at"]).days + 1 if ended else None
    applied = drive["applied"]
    return {
        "report_id": f"RPT-{str(drive['id'])[:8].upper()}",
        "generated_at": now.astimezone(ZONE).strftime("%d %b %Y, %H:%M IST"),
        "generated_by": by.full_name,
        "drive": {
            "company": drive["company"],
            "role": drive["role_title"],
            "college": drive["campus"],
            "start": _day(drive["created_at"]),
            "end": _day(ended),
            "hr_owner": drive["owner_name"] or "—",
        },
        "headline": [
            {"label": "Applications", "value": str(applied)},
            {"label": "Selected", "value": str(selected)},
            {
                "label": "Selection ratio",
                "value": f"{_num(100 * selected / applied)}%" if applied else "—",
            },
            {"label": "Drive length", "value": f"{days} days" if days else "—"},
        ],
        "funnel": funnel,
        "rounds": sections,
        "score_heads": [s["name"] for s in sections],
        "selected": [
            {
                "name": x["full_name"],
                "sid": x["student_id"],
                "scores": [
                    _score(r["type"], scores.get((r["id"], x["candidate_id"]))) for r in scored
                ],
            }
            for x in finalists
        ],
        "log": [
            {
                "when": e["created_at"].astimezone(ZONE).strftime("%d %b, %H:%M"),
                "who": e["actor"] or "—",
                "action": _describe(e, labels),
            }
            for e in log
        ],
    }


def render(data: dict) -> bytes:
    return typst.compile(str(TEMPLATE), sys_inputs={"data": json.dumps(data)})


async def drive_report(session: AsyncSession, user: StaffUser, drive_id: UUID) -> tuple[str, bytes]:
    """(file name, PDF). Only for a closed drive: an interim report would be outdated next hour."""
    owner = None if user.role == "super_admin" else user.id
    drive = await repo.get_drive(session, drive_id, owner)
    if drive is None:
        raise AppError(404, "DRIVE_NOT_FOUND", "Drive not found")
    if drive["status"] != "closed":
        raise AppError(
            409,
            "DRIVE_NOT_CLOSED",
            "The report is available once the drive's final round is decided",
        )
    data = build(
        drive,
        await repo.rounds(session, drive_id),
        await repo.candidate_rows(session, drive_id),
        await repo.decision_log(session, drive_id),
        user,
        datetime.now(ZONE),
    )
    pdf = await run_in_threadpool(render, data)  # CPU-bound compile off the event loop
    # A document with candidate results is leaving the system: record who took it.
    await audit(
        session, user.id, "drive.report", "drive", drive_id, {"report_id": data["report_id"]}
    )
    await session.commit()
    name = f"{drive['company']}-{drive['role_title']}-report.pdf"
    return "".join(ch if ch.isalnum() or ch in "-." else "-" for ch in name), pdf
