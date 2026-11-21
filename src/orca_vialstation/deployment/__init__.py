"""An Orca deployment package for the workcell: `topology.py` plus `workflows/`.

    python -m orca_vialstation.deployment.run --mode sim     # Orca alone, in-memory workcell
    python -m orca_vialstation.deployment.run --mode live    # through vialctl to the simulator

Addresses come from the environment (see `StationConfig.from_env`).
"""
