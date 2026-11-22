"""One tube through scan, weigh and heat with the controller's labware operations.

    python -m workcell_sim --time-scale 20 --demo-racks     # terminal 1 (or the ROS2 VM)
    python examples/run_demo.py [--host 192.168.64.4]        # terminal 2
"""

import argparse
import asyncio

from workcell_controller.operations import Workcell


async def main(host: str) -> None:
    cell = await Workcell.connect(host)
    try:
        await cell.enable()
        await cell.pick("3 A1")
        await cell.place("scanner")
        code = await cell.scan()
        await cell.pick("scanner")
        await cell.place("balance")
        grams = await cell.weigh()
        await cell.pick("balance")
        await cell.place("heater")
        await cell.heat(45)
        while not (await cell.temperature())["stable"]:     # waiting is this script's choice
            await asyncio.sleep(0.5)
        await cell.heater_off()
        await cell.pick("heater")
        await cell.place("8 A1")
        await cell.go("home")
        print(f"code {code}, {grams:.5f} g, heated to 45 C")
    finally:
        await cell.close()


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    asyncio.run(main(ap.parse_args().host))
