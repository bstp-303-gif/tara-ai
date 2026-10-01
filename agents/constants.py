"""Shared reference data. MALAYSIA_STATES is the canonical list of state names: the application
form's State dropdown, State Officer profiles, dashboard filters and the SVG map all use these
exact spellings, so an application's state always matches exactly one State Officer.
"""

MALAYSIA_STATES = [
    'Johor', 'Kedah', 'Kelantan', 'Melaka', 'Negeri Sembilan', 'Pahang', 'Perak', 'Perlis',
    'Pulau Pinang', 'Sabah', 'Sarawak', 'Selangor', 'Terengganu',
    'W.P. Kuala Lumpur', 'W.P. Labuan', 'W.P. Putrajaya',
]

STATE_CHOICES = [(s, s) for s in MALAYSIA_STATES]

# Common alternative spellings, keyed by lowercase with dots/spaces removed.
_STATE_ALIASES = {
    'wpkl': 'W.P. Kuala Lumpur',
    'kualalumpur': 'W.P. Kuala Lumpur',
    'wpkualalumpur': 'W.P. Kuala Lumpur',
    'wilayahpersekutuankualalumpur': 'W.P. Kuala Lumpur',
    'wpputrajaya': 'W.P. Putrajaya',
    'putrajaya': 'W.P. Putrajaya',
    'wilayahpersekutuanputrajaya': 'W.P. Putrajaya',
    'wplabuan': 'W.P. Labuan',
    'labuan': 'W.P. Labuan',
    'wilayahpersekutuanlabuan': 'W.P. Labuan',
    'penang': 'Pulau Pinang',
    'malacca': 'Melaka',
}


def _key(value):
    return ''.join(ch for ch in str(value or '').lower() if ch.isalnum())


_CANONICAL_BY_KEY = {_key(s): s for s in MALAYSIA_STATES}


def canonical_state(value):
    """Returns the canonical state name for `value`, or '' if it isn't recognised."""
    key = _key(value)
    return _CANONICAL_BY_KEY.get(key) or _STATE_ALIASES.get(key, '')
