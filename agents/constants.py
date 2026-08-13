"""Shared reference data. `state`/`district` on Application are free-text (filled in by the
applicant), so this list is used only for dashboard filter dropdowns and the SVG map — it
doesn't constrain what can be stored.
"""

MALAYSIA_STATES = [
    'Johor', 'Kedah', 'Kelantan', 'Melaka', 'Negeri Sembilan', 'Pahang', 'Perak', 'Perlis',
    'Pulau Pinang', 'Sabah', 'Sarawak', 'Selangor', 'Terengganu',
    'W.P. Kuala Lumpur', 'W.P. Labuan', 'W.P. Putrajaya',
]
