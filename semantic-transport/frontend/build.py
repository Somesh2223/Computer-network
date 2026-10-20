"""Builds frontend/explorer.html: the visual explainer, with real data baked in.

  python frontend/build.py

Nothing in the page re-implements the protocol. Every packet, loss, decision
and byte it shows comes from src/sim.py (seed 23, 10% burst loss, the same
run `python demo.py --seed 23` narrates), and every chart from the CSVs in
results/ that experiments.py and netem_bench.py wrote. Edit template.html,
then re-run this script. `python app.py` serves the same page in live mode.
"""

import csv
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)

from scenario import run_scenario                # noqa: E402
from src import header                           # noqa: E402

EPISODES = (90, 862, 89)      # superseded, repaired, within tolerance (see demo.py --seed 23)


def read_csv(name):
    with open(os.path.join(ROOT, "results", name), newline="") as fh:
        rows = list(csv.DictReader(fh))
    out = []
    for r in rows:
        row = {}
        for k, v in r.items():
            try:
                row[k] = float(v)
            except ValueError:
                row[k] = v
        out.append(row)
    return out


def example_packet():
    """The real bytes of a DATA packet carrying attitude and lidar_scan."""
    updates = {0: (7, bytes(4)), 2: (3, bytes(900))}
    wire = header.pack(header.Packet(header.DATA, 90, header.bitmap_of(updates), 0, updates))
    return dict(header_hex=wire[:5].hex(" "), length=len(wire), seq=90, bitmap=header.bitmap_of(updates))


def main():
    data = run_scenario(seed=23, duration=15.0, episodes=EPISODES)
    data["packet"] = example_packet()
    data["results"] = dict(
        crossover=read_csv("crossover_sweep.csv"), policy=read_csv("policy_summary.csv"),
        fields=read_csv("per_field.csv"), workload=read_csv("workload_sweep.csv"),
        fec=read_csv("fec_study.csv"), realnet=read_csv("realnet_summary.csv"),
        realnet_sweep=read_csv("realnet_sweep.csv"))
    blob = json.dumps(data, separators=(",", ":"))
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as fh:
        page = fh.read()
    page = page.replace("/*__DATA__*/null", blob)
    # The template is page content only (the form the Artifact viewer expects).
    # explorer.html wraps it into a complete document so it also opens offline.
    split = page.index("<nav")
    document = ("<!doctype html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
                "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1, viewport-fit=cover\">\n"
                + page[:split] + "</head>\n<body>\n" + page[split:] + "\n</body>\n</html>\n")
    out = os.path.join(HERE, "explorer.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(document)
    if len(sys.argv) > 1:                       # optional: content-only copy for publishing
        with open(sys.argv[1], "w", encoding="utf-8") as fh:
            fh.write(page)
    print(f"wrote {out} ({len(document) / 1024:.0f} KB, data {len(blob) / 1024:.0f} KB)")


if __name__ == "__main__":
    main()
