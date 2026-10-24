"""
Mechanical limits of the rig, in a module with no imports so that both the scanner (hardware/scanner.py) and the request
schemas (schemas/scanner.py) can use the same numbers.
"""

# Mechanical travel of the Z axis (the usable length of the rod, set per rig; to be confirmed by measuring it) and the
# extra distance homing / move-to-top may travel past it before giving up. Their sum is the step budget of those two
# moves. The longest Z move a request may ask for is the travel itself (schemas.scanner.Z_DISTANCE_MAX_MM).
Z_TRAVEL_MAX_MM = 475
Z_HOMING_MARGIN_MM = 15
