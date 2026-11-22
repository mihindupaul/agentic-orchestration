"""The Orca deployment package for the workcell: `topology.py` (the arm, the instruments and the rack wells,
see cell.py) and `workflows/`. Run with `run.py`:

    python -m orca_workcell.deployment.run scan_weigh_rows heat_rows      # both at once, live

The controller's address comes from the environment (see `ControllerConfig.from_env`).
"""
