CASES = ["minimum", "normal", "maximum", "design", "single", "other"]
CASE_TITLES = {"minimum": "Minimum", "normal": "Normal", "maximum": "Maximum",
               "design": "Design", "single": "Single", "other": "Other"}
KINDS = ["condition", "design", "calculated"]

# Canonical parameter names. Printed labels vary per vendor; these make sheets comparable.
CANON = [
    "fluid_name", "fluid_state", "flow_rate_mass", "flow_rate_volume", "flow_rate_gas_std",
    "inlet_pressure", "outlet_pressure", "pressure_drop", "shutoff_pressure", "design_pressure",
    "vapor_pressure", "critical_pressure", "inlet_temperature", "outlet_temperature",
    "design_temperature", "critical_temperature", "specific_gravity", "density",
    "molecular_weight", "viscosity", "compressibility_factor", "specific_heat_ratio",
    "specific_volume", "cavitation_factor", "percent_vapour", "percent_solids",
    "corrosion_allowance", "other",
]
PRESSURE = {"inlet_pressure", "outlet_pressure", "pressure_drop", "shutoff_pressure",
            "design_pressure", "vapor_pressure", "critical_pressure"}
TEMP = {"inlet_temperature", "outlet_temperature", "design_temperature", "critical_temperature"}
FLOW = {"flow_rate_mass", "flow_rate_volume", "flow_rate_gas_std"}
NON_NUMERIC = {"fluid_name", "fluid_state", "other", "corrosion_allowance"}
P_BASES = {"bar", "mbar", "kpa", "mpa", "pa", "psi", "kg/cm2", "kgf/cm2", "atm"}
T_UNITS = {"c", "f", "k", "r"}
