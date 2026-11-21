"""Orca deployment package for the workcell as separate hardware: arm (transporter), code reader, balance and
heater (devices), rack wells (positions), vials (labware, one Orca thread each). See cell.py.

    python -m orca_vialstation.deployment.run --package cell scan_weigh_rows heat_rows   # both at once
"""
