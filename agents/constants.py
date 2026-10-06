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


# Annual national-level recognitions asked about on the application form. Unlike awards (one-time,
# they stay), these are given for a particular year, so a teacher may hold one this year and not
# the next. Teachers who have never held any get a reserved Recommended place (agents/ranking.py).
ANNUAL_RECOGNITIONS = ['Edufluencer KPM', 'Pakar Jauhari Digital', 'Cikgu Juara Digital', 'GPGD']


# The 143 official district education offices (PPD), per state. District on the application form is a
# dropdown of these, and district quotas and maximums are keyed by these exact names. Perlis,
# W.P. Labuan and W.P. Putrajaya have no PPD, so their quotas are state-wide (agents/ranking.py).
PPD_BY_STATE = {
    'Johor': ['PPD Batu Pahat', 'PPD Johor Bahru', 'PPD Kluang', 'PPD Kota Tinggi', 'PPD Kulai', 'PPD Mersing', 'PPD Muar',
              'PPD Pasir Gudang', 'PPD Pontian', 'PPD Segamat', 'PPD Tangkak'],
    'Kedah': ['PPD Baling', 'PPD Kota Setar', 'PPD Kuala Muda', 'PPD Kubang Pasu', 'PPD Kulim Bandar Baharu', 'PPD Langkawi',
              'PPD Padang Terap', 'PPD Pendang', 'PPD Sik', 'PPD Yan'],
    'Kelantan': ['PPD Bachok', 'PPD Gua Musang', 'PPD Jeli', 'PPD Kota Bharu', 'PPD Kuala Krai', 'PPD Machang', 'PPD Pasir Mas',
                 'PPD Pasir Puteh', 'PPD Tanah Merah', 'PPD Tumpat'],
    'Melaka': ['PPD Alor Gajah', 'PPD Jasin', 'PPD Melaka Tengah'],
    'Negeri Sembilan': ['PPD Jempol dan Jelebu', 'PPD Kuala Pilah', 'PPD Port Dickson', 'PPD Rembau', 'PPD Seremban', 'PPD Tampin'],
    'Pahang': ['PPD Bentong', 'PPD Bera', 'PPD Cameron Highlands', 'PPD Jerantut', 'PPD Kuantan', 'PPD Lipis', 'PPD Maran',
               'PPD Pekan', 'PPD Raub', 'PPD Rompin', 'PPD Temerloh'],
    'Perak': ['PPD Bagan Datuk', 'PPD Batang Padang', 'PPD Hilir Perak', 'PPD Hulu Perak', 'PPD Kerian', 'PPD Kinta Selatan',
              'PPD Kinta Utara', 'PPD Kuala Kangsar', 'PPD Larut Matang & Selama', 'PPD Manjung', 'PPD Muallim', 'PPD Perak Tengah'],
    'Perlis': [],
    'Pulau Pinang': ['PPD Barat Daya', 'PPD Seberang Perai Selatan', 'PPD Seberang Perai Tengah', 'PPD Seberang Perai Utara',
                     'PPD Timur Laut'],
    'Sabah': ['PPD Beaufort', 'PPD Beluran', 'PPD Keningau', 'PPD Kinabatangan', 'PPD Kota Belud', 'PPD Kota Kinabalu',
              'PPD Kota Marudu', 'PPD Kuala Penyu', 'PPD Kudat', 'PPD Kunak', 'PPD Lahad Datu', 'PPD Papar', 'PPD Penampang',
              'PPD Pensiangan Nabawan', 'PPD Pitas', 'PPD Ranau', 'PPD Sandakan', 'PPD Semporna', 'PPD Sipitang', 'PPD Tambunan',
              'PPD Tawau', 'PPD Telupid', 'PPD Tenom', 'PPD Tuaran'],
    'Sarawak': ['PPD Baram', 'PPD Bau', 'PPD Belaga', 'PPD Betong', 'PPD Bintulu', 'PPD Dalat', 'PPD Daro', 'PPD Julau',
                'PPD Kanowit', 'PPD Kapit', 'PPD Kuching', 'PPD Lawas', 'PPD Limbang', 'PPD Lubok Antu', 'PPD Lundu',
                'PPD Meradong', 'PPD Miri', 'PPD Mukah', 'PPD Padawan', 'PPD Samarahan', 'PPD Saratok', 'PPD Sarikei',
                'PPD Selangau', 'PPD Serian', 'PPD Sibu', 'PPD Simunjan', 'PPD Song', 'PPD Sri Aman', 'PPD Subis',
                'PPD Tatau/Sebauh'],
    'Selangor': ['PPD Gombak', 'PPD Hulu Langat', 'PPD Hulu Selangor', 'PPD Klang', 'PPD Kuala Langat', 'PPD Kuala Selangor',
                 'PPD Petaling Perdana', 'PPD Petaling Utama', 'PPD Sabak Bernam', 'PPD Sepang'],
    'Terengganu': ['PPD Besut', 'PPD Dungun', 'PPD Hulu Terengganu', 'PPD Kemaman', 'PPD Kuala Nerus', 'PPD Kuala Terengganu',
                   'PPD Marang', 'PPD Setiu'],
    'W.P. Kuala Lumpur': ['PPD Bangsar / Pudu', 'PPD Keramat', 'PPD Sentul'],
    'W.P. Labuan': [],
    'W.P. Putrajaya': [],
}


def districts_for(state):
    """The district choices for `state`: its PPDs, or the state itself when it has none (Perlis, Labuan, Putrajaya)."""
    return PPD_BY_STATE.get(state) or ([state] if state in PPD_BY_STATE else [])
