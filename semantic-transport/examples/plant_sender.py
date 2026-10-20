"""Example sender: a pumping station streaming its readings.

  python examples/plant_sender.py --peer 127.0.0.1:47402              same machine
  python examples/plant_sender.py --peer 192.168.1.20:47402           control room PC
  python examples/plant_sender.py --peer 127.0.0.1:47402 --loss 0.1   emulate a bad link

Run examples/plant_dashboard.py first, on the other end. Press Ctrl+C to stop.
"""

import argparse
import math
import random
import time

from plant_fields import FIELDS   # also puts the project folder on the import path
import semstate


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bind", default="0.0.0.0:47401")
    p.add_argument("--peer", default="127.0.0.1:47402", help="the dashboard's address")
    p.add_argument("--loss", type=float, default=0.0, help="emulated loss rate, 0-0.5")
    args = p.parse_args()

    rng = random.Random()
    with semstate.Sender(FIELDS, bind=args.bind, peer=args.peer, loss=args.loss) as tx:
        tx.set("pump_state", "RUNNING")
        tx.set("alarm", "none")
        start, last_level = time.monotonic(), 0.0
        print(f"sending to {args.peer} with {args.loss:.0%} emulated loss; Ctrl+C to stop")
        try:
            while True:
                t = time.monotonic() - start
                tx.set("flow_lpm", 120 + 8 * math.sin(t) + rng.uniform(-1, 1))
                tx.set("pressure_bar", 4.2 + 0.3 * math.sin(t / 3))
                if t - last_level >= 1.0:
                    tx.set("tank_level_pct", 60 + 20 * math.sin(t / 30))
                    last_level = t
                if rng.random() < 0.002:                       # roughly every 25 s
                    tx.set("alarm", rng.choice(["HIGH PRESSURE", "LOW FLOW", "none"]))
                if rng.random() < 0.001:
                    tx.set("pump_state", rng.choice(["RUNNING", "STANDBY"]))
                time.sleep(0.05)
        except KeyboardInterrupt:
            s = tx.stats
            print(f"\nsent {s['data_sent']} packets; {s['fields_superseded']} lost fields superseded, "
                  f"{s['fields_tolerated']} within tolerance, {s['fields_repaired']} repaired")


if __name__ == "__main__":
    main()
