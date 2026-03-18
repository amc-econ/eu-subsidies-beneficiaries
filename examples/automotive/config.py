"""
Automotive-specific matcher configuration for the EU Subsidies pipeline.

Contains Python-only patterns that cannot be serialized to JSON: regex patterns,
frozensets for exact-only names, contextual blocklists, and known false-positive pairs.

Parent groups, nationality, and sector tags are defined in config/ as JSON files
and loaded directly by consolidation.py.

Used by run_pipeline.py (stage_automotive) only.
The generic matcher (src/matching/) has no dependency on this file.
"""

import re

# Automotive acronyms that should only match via exact lookup (too ambiguous for fuzzy)
AUTOMOTIVE_EXACT_ONLY = frozenset({
    'bmw', 'vw', 'gm', 'mg', 'byd', 'jac', 'gac', 'faw', 'man',
    'baic', 'nio', 'aro', 'tvr', 'daf', 'uaz', 'acc', 'varta',
})

# Automotive-specific contextual blocklist additions
AUTOMOTIVE_CONTEXTUAL_BLOCKLIST = frozenset({
    'smart', 'seat', 'mini', 'alpine', 'radar', 'tank', 'mine',
    'geometry', 'magna', 'champion', 'ideal', 'noble', 'karma',
    'genesis', 'triumph', 'eagle', 'rover', 'archer', 'herald',
    'lotus', 'elaris', 'silva', 'mia', 'think', 'reva', 'ora',
    'leap', 'lynk', 'zero', 'fiat', 'ford',
    'arrival', 'rimac', 'togg', 'ineos', 'fisker', 'canoo',
    'continental', 'refine', 'proton', 'galaxy', 'mobilize', 'brilliance',
    'hitech', 'evolute', 'lincoln', 'firefly', 'blue bird',
    'mg', 'gm', 'ds', 'ev', 'acc',
    'basf', 'varta',
    # Automotive-specific industry words
    'automotive', 'motor', 'motors', 'auto', 'vehicle', 'vehicles',
    'battery',
})

# Automotive-specific false positive pairs
AUTOMOTIVE_FP_PAIRS = frozenset({
    ('seg automotive', 'automotive rdi'),
    ('hanon systems', 'systems'),
    ('continental', 'continental biofuel'),
    ('li auto', 'auto'),
})

# Automotive-specific beneficiary FP patterns
AUTOMOTIVE_FP_PATTERNS = {
    'tesla': re.compile(r'nikola\s+tesla|ericsson.*tesla', re.I),
    'bosch': re.compile(r'fundacio\s+bosch\s+gimpera|bosch\s+college\s+uwc|'
                        r'bosch\s+stiftung|den\s+bosch\s+innovatie|'
                        r'bosch\s+power\s+tool', re.I),
    'aptiv': re.compile(r'delphi\s+film|delphi\s+bdu\s+sc|'
                        r'delfi.*association|delphi[\-\s]filmtheater', re.I),
    'bmw':   re.compile(r'bmw\s+stiftung|frank\s+keane\s+bmw', re.I),
}
