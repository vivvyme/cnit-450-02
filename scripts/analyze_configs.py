import argparse
import os
import shutil
import sys
import tempfile
from pathlib import Path
REPO = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO / "configs"
SNAPSHOT_NAME = "cnit-450-02"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
DIM = "\033[2m"
RESET = "\033[0m"


def color(text, c):
    return f"{c}{text}{RESET}" if sys.stdout.isatty() else text


def heading(text):
    print(f"\n{color('=' * 70, DIM)}")
    print(f" {text}")
    print(f"{color('=' * 70, DIM)}\n")


def build_snapshot(tmpdir):
    dest = Path(tmpdir) / "configs"
    dest.mkdir(parents=True)
    count = 0
    for path in sorted(CONFIG_DIR.rglob("*")):
        if not path.is_file() or path.name == ".gitkeep":
            continue
        target = dest / path.name
        if target.exists():
            print(color(f"skipping duplicates: {path}", YELLOW))
            continue
        shutil.copyfile(path, target)
        count += 1
    return count


def parse_report(session):
    heading("PARSE REPORT")

    status = session.q.fileParseStatus().answer().frame()
    if status.empty:
        print(color("Batfish did not parse.", RED))
        return 0, 0
    ok, partial, bad = 0, 0, 0
    for _, row in status.iterrows():
        filename = str(row.get("File_Name", "?"))
        state = str(row.get("Status", "?"))
        nodes = row.get("Nodes")
        nodes = ", ".join(str(n) for n in nodes) if nodes is not None and len(nodes) else "(no device)"
        if state == "PASSED":
            ok += 1
            print(f"  {color('OK     ', GREEN)} {filename:<28} -> {nodes}")
        elif state == "PARTIALLY_UNRECOGNIZED":
            partial += 1
            print(f"  {color('PARTIAL', YELLOW)} {filename:<28} -> {nodes}")
        else:
            bad += 1
            print(f"  {color('FAIL   ', RED)} {filename:<28} -> {state}")

    print(f"\n  {ok} parsed, {partial} partially, {bad} failed, {ok + partial + bad} files total.")
    if partial or bad:
        print(color("\n PARTIAL and FAIL are expected for the command scripts.", DIM))
    return ok + partial, bad


def show(session, title, question, note=None):
    heading(title)
    if note:
        print(color(f"  {note}\n", DIM))
    try:
        frame = question.answer().frame()
    except Exception as err:
        print(color(f"question failed: {err}", RED))
        return
    if frame.empty:
        print(color("nothing found", GREEN))
        return
    print(frame.to_string(max_colwidth=60))
    print(f"\n  {len(frame)} row(s).")


def main():
    parser = argparse.ArgumentParser(description="Analyze current configs with Batfish.")
    parser.add_argument(
        "--host",
        default=os.environ.get("BATFISH_HOST", "localhost"),
        help="host running the Batfish service (default: $BATFISH_HOST, else localhost)",
    )
    args = parser.parse_args()

    try:
        from pybatfish.client.session import Session
    except ImportError:
        print("pybatfish is not installed. Run: pip install -r deps/requirements.txt")
        return 1

    print(f"Connecting to Batfish at {args.host} ...")
    try:
        session = Session(host=args.host)
    except Exception as err:
        print(color(f"could not reach a Batfish service at {args.host}: {err}", RED))
        print("Is the container running? See docs/batfish.md.")
        return 1

    with tempfile.TemporaryDirectory() as tmpdir:
        devices = build_snapshot(tmpdir)
        print(f"Built a snapshot from {devices} config file(s).")
        try:
            session.init_snapshot(tmpdir, name=SNAPSHOT_NAME, overwrite=True)
        except Exception as err:
            print(color(f"Batfish could not load the snapshot: {err}", RED))
            return 1

        ok, _ = parse_report(session)
        if ok == 0:
            print(color("\nNothing parsed. Stopping here.", RED))
            return 0

        show(
            session,
            "PARSE WARNINGS",
            session.q.parseWarning(),
            "This is the to-do list for conversion. "
            "Batfish did not understand the following files.",
        )
        show(
            session,
            "DEVICES CONFIGURED",
            session.q.nodeProperties(properties="Configuration_Format,Hostname"),
            "If a device is missing then its config did not parse.",
        )
        show(
            session,
            "UNDEFINED REFERENCES",
            session.q.undefinedReferences(),
            "Config that points at something undefined. ",
        )
        show(
            session,
            "UNUSED STRUCTURES",
            session.q.unusedStructures(),
            "Defined but unreferenced.",
        )
        show(
            session,
            "ROUTING LOOPS",
            session.q.detectLoops(),
            "Shows routing loops.",
        )
        show(
            session,
            "LAYER ADJACENCIES",
            session.q.layer3Edges(),
            "Devices detected as neighbors. Compare with Cabling.",
        )

    heading("DONE")
    print("This is report-only.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())