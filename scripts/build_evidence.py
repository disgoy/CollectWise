"""Build the evidence report's data (evidence/data.json) from the project's real
result files. Free: reads local files only.

Sources: the agent eval (.claude/hillclimb/agent_conversations), the voice
checks (results-*.json in the git-ignored recordings folder), live-call
timings (live_turn_metrics, joined with conversation_events tool calls) and
the hand-curated evidence/facts.json.

Aggregates only: counts, rates, timings, phrase ids and the fact heard. No
transcript, reply, prompt or judge reasoning is copied, and check_private()
refuses to write output containing them.

Usage, from the repo root:
    python -m scripts.build_evidence
"""

import argparse
import json
import sys
from pathlib import Path

from backend.app.services.evidence import (  # noqa: F401  re-exported for the tests and older callers
    EVAL_DIR,
    FACTS,
    METRICS,
    OUTPUT,
    PRIVATE_KEYS,
    REPO,
    VOICE_RESULTS,
    build,
    check_private,
    datasets,
    eval_version,
    load_eval,
    load_live,
    load_voice,
)


def write_datasets(data: dict, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for name, rows in datasets(data).items():
        if name != "notes":  # the curated notes are prose by design
            check_private({name: rows})
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(rows, indent=1, ensure_ascii=False) + "\n")
        written.append(path)
    return written


def main(argv: list[str] | None = None, session_factory=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=OUTPUT)
    args = parser.parse_args(argv)
    if session_factory is None:
        from backend.app.db.session import SessionLocal, require_current_schema

        require_current_schema()
        session_factory = SessionLocal
    with session_factory() as db:
        data = build(db)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    write_datasets(data, args.out.parent / "datasets")
    versions = ", ".join(f"{v['version']} {v['rates']['all_pass']}%" for v in data["eval"]["versions"])
    print(f"Wrote {args.out.relative_to(REPO) if args.out.is_relative_to(REPO) else args.out}: eval {versions}; "
          f"{data['voice'].get('clips', 0)} voice clips; {data['live']['turns']} live turns")
    return 0


if __name__ == "__main__":
    sys.exit(main())
