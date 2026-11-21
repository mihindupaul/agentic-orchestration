import argparse
import asyncio

from .config import Timing
from .simulator import Simulator, load_demo_racks, run_forever


def main() -> None:
    ap = argparse.ArgumentParser(description="Run the sample-handling workcell simulator")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--time-scale", type=float, default=1.0, help=">1 runs faster than real time")
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--demo-racks", action="store_true", help="full rack in bay 3, empty rack in bay 8")
    args = ap.parse_args()

    sim = Simulator(timing=Timing(time_scale=args.time_scale), seed=args.seed)
    if args.demo_racks:
        load_demo_racks(sim.world)
    try:
        asyncio.run(run_forever(sim, args.host))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
