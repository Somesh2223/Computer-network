"""Example receiver: the control room's live view of the pumping station.

  python examples/plant_dashboard.py --peer 127.0.0.1:47401           same machine
  python examples/plant_dashboard.py --peer 192.168.1.10:47401        station's address

Start this first, then examples/plant_sender.py. Press Ctrl+C to stop.
"""

import argparse
import time

from plant_fields import FIELDS   # also puts the project folder on the import path
import semstate


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--bind", default="0.0.0.0:47402")
    p.add_argument("--peer", default="127.0.0.1:47401", help="the station's address")
    args = p.parse_args()

    with semstate.Receiver(FIELDS, bind=args.bind, peer=args.peer) as rx:
        print("waiting for the station; Ctrl+C to stop")
        try:
            while True:
                v = rx.snapshot()
                fmt = lambda x, spec: "  --  " if x is None else format(x, spec)
                line = (f"flow {fmt(v['flow_lpm'], '6.1f')} L/min  pressure {fmt(v['pressure_bar'], '4.2f')} bar  "
                        f"tank {fmt(v['tank_level_pct'], '5.1f')}%  pump {v['pump_state'] or '--':8}  "
                        f"alarm {v['alarm'] or '--'}")
                print("\r" + line.ljust(110), end="", flush=True)
                time.sleep(0.2)
        except KeyboardInterrupt:
            s = rx.stats
            print(f"\nNACKs sent {s['nacks_sent']}, heartbeat reconciliations {s['hb_fields_reconciled']}")


if __name__ == "__main__":
    main()
