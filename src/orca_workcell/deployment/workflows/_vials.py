"""Helpers shared by the workflows: one Orca thread per vial, named after its well."""

from orca.sdk.labware import PlateTemplate

# Orca needs a catalog labware type with a working factory. None of its tube entries builds in this
# release, so each vial is declared with the deep-well plate type already used elsewhere; the type only
# matters to Orca's catalog, the arm moves whatever stands in the well.
VIAL_TYPE = "Thermo_Nunc_96_well_plate_1300uL_Rb"


def named(func, name: str):
    func.__name__ = func.__qualname__ = name
    return func


def vial(well: str) -> PlateTemplate:
    return PlateTemplate(f"vial_{well}", labware_type=VIAL_TYPE)
