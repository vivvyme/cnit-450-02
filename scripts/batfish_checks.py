import logging
import os
import sys
import time
from pybatfish.client.session import Session
from pybatfish.exception import BatfishAssertException

HOST = os.environ.get("BATFISH_HOST", "localhost")
SNAPSHOT_DIR = os.environ.get("SNAPSHOT_DIR", "snapshots/lab")
NETWORK = os.environ.get("BATFISH_NETWORK", "ci_network")
SNAPSHOT = os.environ.get("BATFISH_SNAPSHOT", "ci_snapshot")
WAIT_SECONDS = int(os.environ.get("BATFISH_WAIT_SECONDS", "180"))

CHECKS = [
    ("No undefined references", "assert_no_undefined_references", {}),
    ("No duplicate router IDs (OSPF/BGP)", "assert_no_duplicate_router_ids",
     {"protocols": ["ospf", "bgp"]}),
    ("No incompatible BGP sessions", "assert_no_incompatible_bgp_sessions", {}),
    ("No incompatible OSPF sessions", "assert_no_incompatible_ospf_sessions", {}),
    ("No forwarding loops", "assert_no_forwarding_loops", {}),
]

logging.getLogger("pybatfish").setLevel(logging.WARNING)


def connect():
    deadline = time.time() + WAIT_SECONDS
    while True:
        try:
            return Session(host=HOST)
        except Exception as exc:
            if time.time() > deadline:
                sys.exit(f"ERROR: Batfish not reachable at {HOST}:9996 "
                         f"after {WAIT_SECONDS}s: {exc}")
            print("Waiting for Batfish to start.", flush=True)
            time.sleep(5)


def write_summary(results):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    with open(path, "a", encoding="utf-8") as f:
        f.write("## Batfish results\n\n| Check | Result |\n|---|---|\n")
        for label, ok in results:
            f.write(f"| {label} | {'PASS' if ok else 'FAIL'} |\n")


def main():
    if not os.path.isdir(os.path.join(SNAPSHOT_DIR, "configs")):
        sys.exit(f"ERROR: '{SNAPSHOT_DIR}/configs' not found.")

    bf = connect()
    bf.set_network(NETWORK)
    bf.init_snapshot(SNAPSHOT_DIR, name=SNAPSHOT, overwrite=True)

    nodes = bf.q.nodeProperties().answer().frame()
    if nodes.empty:
        sys.exit(f"ERROR: Batfish loaded no devices from '{SNAPSHOT_DIR}/configs'.")
    print("Devices loaded:", ", ".join(sorted(nodes["Node"])))

    parse = bf.q.fileParseStatus().answer().frame()
    print(parse.to_string(index=False))
    results = [("No config file failed to parse", not (parse["Status"] == "FAILED").any())]

    for label, method, kwargs in CHECKS:
        try:
            getattr(bf.asserts, method)(**kwargs)
            results.append((label, True))
            print(f"PASS  {label}")
        except BatfishAssertException as exc:
            results.append((label, False))
            print(f"FAIL  {label}\n{exc}\n")

    write_summary(results)
    failed = [label for label, ok in results if not ok]
    if failed:
        sys.exit(f"{len(failed)} checks failed: " + "; ".join(failed))
    print("All checks passed.")


if __name__ == "__main__":
    main()