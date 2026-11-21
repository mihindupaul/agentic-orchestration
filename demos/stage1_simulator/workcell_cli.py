"""Interactive command line for the workcell: plain commands, plus raw access to every device.

The devices only understand coordinates and simple commands. Names like "bay 3 A1" or "scanner" are
turned into coordinates by the controller layer on this machine (`vialctl.deck`, `vialctl.operations`),
using the deck file `src/vialctl/decks/workcell.json`.

    python demos/stage1_simulator/workcell_cli.py --host 192.168.64.4
    python demos/stage1_simulator/workcell_cli.py --host 192.168.64.4 -c "enable; pick 3 A1; move scanner"
    python demos/stage1_simulator/workcell_cli.py --host 192.168.64.4 run demo.txt

Every line sent to a device is printed with its reply, so you see exactly what crosses the wire.
Type `help` for the command list, `help <command>` for details.
"""

from __future__ import annotations

import argparse
import asyncio
import cmd
import os
import shlex
import sys
import time

try:
    import vialctl  # noqa: F401
except ImportError:          # running from a fresh clone without `make install`
    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src"))

from vialctl.deck import Deck
from vialctl.errors import VialCtlError
from vialctl.operations import Workcell

STATIONS = ("scanner", "heater", "balance")
TTY = sys.stdout.isatty()


def _style(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if TTY else text


def echo(device: str, text: str, reply: str) -> None:
    print(_style(f"   {device:>9} > {text:<36} < {reply}", "2"))


class WorkcellShell(cmd.Cmd):
    intro = "Workcell CLI. Type help for commands, quit to exit."
    prompt = _style("workcell> ", "1;36")

    def __init__(self, cell: Workcell, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.cell, self.loop = cell, loop

    # ------------------------------------------------------------- plumbing
    def do(self, coro):
        """Run one controller operation to completion."""
        return self.loop.run_until_complete(coro)

    def onecmd(self, line: str) -> bool:
        try:
            return super().onecmd(line)
        except (VialCtlError, ValueError, KeyError, IndexError) as exc:
            print(_style(f"error: {exc}", "1;31"))
            self.cell.forget_position()      # the arm may have stopped anywhere: lift first on the next move
        except OSError as exc:
            print(_style(f"connection error: {exc}", "1;31"))
        return False

    def emptyline(self) -> bool:
        return False

    def default(self, line: str) -> None:
        print(f"unknown command {line.split()[0]!r}; type help")

    GROUPS = [("Arm", ["enable", "disable", "speed", "clear", "estop"]),
              ("Motion", ["move", "down", "up", "grip", "release"]),
              ("Vials", ["pick", "place"]),
              ("Instruments", ["scan", "weigh", "tare", "heat", "temp", "heater", "status"]),
              ("Raw device access", ["raw"]),
              ("Simulator setup", ["rack", "load", "unload", "fault"]),
              ("Scripting", ["run", "wait", "quit"])]

    def do_help(self, arg):
        """help [command]      List commands, or explain one."""
        if arg:
            return super().do_help(arg)
        for title, names in self.GROUPS:
            print(_style(title, "1"))
            for name in names:
                print("   " + getattr(self, f"do_{name}").__doc__.splitlines()[0])

    def say(self, text: str) -> None:
        print(_style(text, "1;32"))

    def admin(self, **request) -> dict:
        """The simulator's back door (racks, faults). Real hardware has no equivalent."""
        if self.cell.dev.admin is None:
            raise ValueError("no simulator admin port")
        return self.do(self.cell.dev.admin.call(**request))

    # --------------------------------------------------------------- arm power
    def do_enable(self, _):
        """enable              Power the arm (needed before any motion)."""
        self.do(self.cell.enable())
        self.say("arm enabled")

    def do_disable(self, _):
        """disable             Power the arm down."""
        self.do(self.cell.arm.disable())
        self.say("arm disabled")

    def do_clear(self, _):
        """clear               Clear an arm error or collision so it can move again."""
        self.do(self.cell.arm.clear_error())
        self.say("error cleared")

    def do_estop(self, _):
        """estop               Emergency stop: halts the arm immediately."""
        self.do(self.cell.arm.emergency_stop())
        self.say("emergency stop")

    def do_speed(self, arg):
        """speed <1-100>       Set arm speed in percent."""
        self.do(self.cell.arm.speed(int(arg)))
        self.say(f"speed {int(arg)}%")

    # ----------------------------------------------------------------- motion
    def do_move(self, arg):
        """move <where>        Travel at safe height to: scanner | heater | balance | home | <bay> <well> | <x> <y>
                    e.g.  move scanner     move 3 A1     move 0 300"""
        t = self.do(self.cell.go(shlex.split(arg)))
        self.say(f"at {t.name}" + (f" ({t.xy[0]:g}, {t.xy[1]:g})" if t.kind == "well" else ""))

    def do_down(self, _):
        """down                Lower to work height here."""
        self.do(self.cell.down())
        self.say(f"down at {self.cell.at.name}")

    def do_up(self, _):
        """up                  Raise to safe height here."""
        self.do(self.cell.up())
        self.say(f"up at {self.cell.at.name if self.cell.at else 'current position'}")

    def do_grip(self, _):
        """grip                Close the gripper and report whether it holds a vial."""
        self.say("holding a vial" if self.do(self.cell.grip()) else "gripper closed on nothing")

    def do_release(self, _):
        """release             Open the gripper."""
        self.do(self.cell.release())
        self.say("gripper open")

    # ---------------------------------------------------------- vial handling
    def do_pick(self, arg):
        """pick <bay> <well>   Fetch a vial: move, open, lower, grip, check, raise.   e.g. pick 3 A1
pick scanner|balance|heater  Take the vial back out of that station."""
        t = self.do(self.cell.pick(shlex.split(arg)))
        self.say(f"vial lifted out of the {t.name}" if t.docks else f"picked vial from {t.name}")

    def do_place(self, arg):
        """place <bay> <well>  Put the held vial into a rack well.   e.g. place 3 A1
place scanner|balance|heater  Set the vial down in that station and let go (the arm is free meanwhile)."""
        t = self.do(self.cell.place(shlex.split(arg)))
        self.say(f"vial docked in the {t.name}" if t.docks else f"placed vial at {t.name}")

    # ------------------------------------------------------------ instruments
    def do_scan(self, _):
        """scan                Read the 2D code of the vial in the code reader."""
        code = self.do(self.cell.scan())
        self.say(f"code: {code}" if code else "scan failed: no read")

    def do_heat(self, arg):
        """heat <C>            Start heating the heater well (25-100 C). Returns at once; check with `temp`."""
        r = self.do(self.cell.heat(float(arg)))
        self.say(f"heating to {r['target']:g} C, now {r['celsius']:.1f} C")

    def do_temp(self, _):
        """temp                Heater temperature, target and state (OFF, HEATING or STABLE)."""
        r = self.do(self.cell.temperature())
        target = "off" if r["target"] is None else f"target {r['target']:g} C"
        self.say(f"heater {r['celsius']:.1f} C, {target}, {r['state']}")

    def do_heater(self, arg):
        """heater off          Switch the heater off; the well cools back toward room temperature."""
        if arg.strip().lower() != "off":
            raise ValueError("usage: heater off")
        self.do(self.cell.heater_off())
        self.say("heater off")

    def do_weigh(self, _):
        """weigh               Wait for a stable reading and print it."""
        self.say(f"weight: {self.do(self.cell.weigh()):.5f} g")

    def do_tare(self, _):
        """tare                Zero the balance."""
        self.do(self.cell.tare())
        self.say("balance tared")

    def do_status(self, _):
        """status              Arm mode, pose and gripper, plus each instrument's state."""
        arm, dev = self.cell.arm, self.cell.dev
        for coro in (arm.mode(), arm.pose(), arm.holding(), dev.scanner.status(), dev.heater.status(),
                     dev.balance.status()):
            self.do(coro)

    # --------------------------------------------------------------- raw access
    def do_raw(self, arg):
        """raw <device> <text> Send one line exactly as typed, e.g. raw arm MovL(0,300,100,0)
Devices: dashboard, motion (alias arm), balance, scanner, heater, admin.
e.g.  raw arm GetPose()     raw balance SI     raw scanner STATUS     raw admin {"cmd":"state"}"""
        device, _, text = arg.strip().partition(" ")
        device = {"arm": "motion"}.get(device, device)
        lines = self.cell.dev.lines()
        if device not in lines or not text:
            raise ValueError("usage: raw <device> <text>; devices: " + ", ".join(list(lines) + ["arm"]))
        reply = self.do(lines[device].ask(text))
        if device == "admin":          # the admin port is not traced: show its reply here
            echo("admin", text, reply)

    # ------------------------------------------------- simulator setup (admin)
    def do_rack(self, arg):
        """rack <bay>          Show which wells of a bay hold vials (simulator only)."""
        info = self.admin(cmd="rack_map", slot=int(arg))
        wells = sorted(info["occupied"], key=lambda w: (int(w[1:]), w[0]))     # column by column, like the arm walks
        self.say(f"bay {arg}: {info['barcode'] or 'no rack'}, {len(wells)} vials"
                 + (f" ({', '.join(wells[:12])}{' ...' if len(wells) > 12 else ''})" if wells else ""))

    def do_load(self, arg):
        """load <bay> [full|empty|A1,B2,...]   Put a rack in a bay (simulator only).   e.g. load 4 A1,A2,A3"""
        args = shlex.split(arg)
        bay = int(args[0])
        what = args[1] if len(args) > 1 else "full"
        fill = {"full": 1.0, "empty": 0.0}.get(what.lower(), what.split(","))
        self.admin(cmd="load_rack", slot=bay, barcode=f"RACK{bay:03d}", fill=fill)
        self.do_rack(str(bay))

    def do_unload(self, arg):
        """unload <bay>        Take the rack out of a bay (simulator only)."""
        self.admin(cmd="remove_rack", slot=int(arg))
        self.say(f"bay {arg} empty")

    def do_fault(self, arg):
        """fault <name> [count]  Make the next operation(s) fail (simulator only). `fault` alone lists names.
e.g.  fault scanner_noread     fault heater_timeout     fault heater_overshoot     fault gripper_miss 2"""
        args = shlex.split(arg)
        if not args:
            for name, what in self.admin(cmd="faults")["faults"].items():
                print(f"   {name:<18} {what}")
            return
        self.admin(cmd="inject", fault=args[0], count=int(args[1]) if len(args) > 1 else 1)
        self.say(f"armed fault {args[0]}")

    # ------------------------------------------------------------------- misc
    def do_wait(self, arg):
        """wait <seconds>      Pause (useful in scripts)."""
        time.sleep(float(arg))

    def do_run(self, arg):
        """run <file>          Run commands from a file, one per line (# for comments)."""
        with open(arg.strip()) as f:
            for line in f:
                line = line.split("#", 1)[0].strip()
                if line:
                    print(self.prompt + line)
                    self.onecmd(line)

    def do_quit(self, _):
        """quit                Leave the CLI."""
        return True

    do_exit = do_quit
    do_EOF = do_quit


def main() -> None:
    ap = argparse.ArgumentParser(description="Command the workcell from the terminal.")
    ap.add_argument("--host", default="127.0.0.1", help="simulator address, e.g. the ROS2 VM")
    ap.add_argument("--deck", help="deck file (default: src/vialctl/decks/workcell.json)")
    ap.add_argument("-c", dest="commands", help="semicolon-separated commands to run, then exit")
    ap.add_argument("script", nargs="*", help="`run <file>` or any single command, then exit")
    args = ap.parse_args()

    loop = asyncio.new_event_loop()
    cell = loop.run_until_complete(Workcell.connect(args.host, deck=Deck.load(args.deck), trace=echo))
    shell = WorkcellShell(cell, loop)
    try:
        if args.commands:
            for line in args.commands.split(";"):
                if line.strip():
                    print(shell.prompt + line.strip())
                    shell.onecmd(line.strip())
        elif args.script:
            shell.onecmd(" ".join(args.script))
        else:
            shell.cmdloop()
    except KeyboardInterrupt:
        print()
    finally:
        loop.run_until_complete(cell.close())
        loop.close()


if __name__ == "__main__":
    main()
