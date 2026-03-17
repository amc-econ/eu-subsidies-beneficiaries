"""
Automotive-specific configuration for the EU Subsidies matching pipeline.

Contains all sector-specific constants that were extracted from:
- consolidation.py: PARENT_GROUPS, COMPANY_NATIONALITY, SECTOR_TAGS
- automotive_matcher.py: sector buckets, FP patterns, blocklists

These are used by the automotive example analysis only.
The generic matcher (src/matching/) has no dependency on this file.
"""

import re

# ============================================================================
# PARENT GROUP DEFINITIONS (from consolidation.py)
# ============================================================================

PARENT_GROUPS = {
    # Stellantis family (merged PSA + FCA in 2021)
    'Stellantis': [
        'stellantis', 'fiat', 'fiat chrysler', 'fca', 'fca italy', 'fiat industrial',
        'fiat auto', 'peugeot', 'citroen', 'psa', 'opel', 'vauxhall', 'ds automobiles',
        'alfa romeo', 'lancia', 'maserati', 'abarth', 'jeep', 'dodge', 'ram',
        'chrysler', 'comau', 'magneti marelli', 'teksid', 'sevel',
        'pca slovakia', 'groupe psa', 'fpt industrial',
    ],
    'Volkswagen Group': [
        'volkswagen', 'vw', 'audi', 'porsche', 'seat', 'cupra', 'skoda',
        'bentley', 'lamborghini', 'bugatti', 'man', 'man truck', 'traton',
        'scania', 'navistar', 'powerco', 'cariad', 'autoeuropa',
        'volkswagen de mexico', 'volkswagen india',
    ],
    'Mercedes-Benz Group': [
        'mercedes', 'mercedes-benz', 'daimler', 'daimler truck', 'daimler ag',
        'smart', 'amg', 'mercedes benz',
    ],
    'BMW Group': [
        'bmw', 'mini', 'rolls-royce motor cars', 'bmw brilliance',
    ],
    'Renault Group': [
        'renault', 'dacia', 'alpine', 'renault nissan', 'mobilize',
        'renault trucks', 'ampere',
    ],
    'Toyota Group': [
        'toyota', 'lexus', 'daihatsu', 'hino',
    ],
    'Volvo Cars (Geely)': [
        'volvo car', 'volvo cars', 'polestar',
        'volvo car corp', 'volvo car ab', 'volvo car belg', 'volvo car nederland',
        'volvo car sverige',
    ],
    'AB Volvo (Trucks)': [
        'ab volvo', 'volvo truck', 'volvo trucks', 'volvo group',
        'volvo trucks clean', 'volvo rdi',
        'renault trucks',
    ],
    'Hyundai-Kia': [
        'hyundai', 'kia', 'hyundai motor', 'genesis',
    ],
    'Ford Motor': [
        'ford', 'ford otosan', 'ford otomotiv', 'ford craiova', 'ford uk',
    ],
    'Robert Bosch': [
        'bosch', 'robert bosch',
    ],
    'Continental AG': [
        'continental', 'continental ag',
    ],
    'ZF Friedrichshafen': [
        'zf', 'zf friedrichshafen',
    ],
    'Valeo': [
        'valeo',
    ],
    'Forvia (Faurecia)': [
        'faurecia', 'forvia', 'faurecia forvia',
    ],
    'Schaeffler': [
        'schaeffler',
    ],
    'STMicroelectronics': [
        'stmicroelectronics', 'stm',
    ],
    'Infineon': [
        'infineon',
    ],
    'NXP Semiconductors': [
        'nxp',
    ],
    'Michelin': [
        'michelin',
    ],
    'Pirelli': [
        'pirelli',
    ],
    'Bridgestone': [
        'bridgestone',
    ],
    'Northvolt': [
        'northvolt', 'novo energy',
    ],
    'ACC (Automotive Cells Company)': [
        'acc', 'automotive cells company',
    ],
    'Iveco Group': [
        'iveco', 'iveco group', 'fincantieri',
    ],
    'Nissan': [
        'nissan',
    ],
    'Honda': [
        'honda',
    ],
    'verkor': [
        'verkor', 'giga verkor',
    ],
    'FREYR': [
        'freyr',
    ],
    'Leclanché': [
        'leclanche',
    ],
    'VARTA': [
        'varta',
    ],
    'Samsung SDI': [
        'samsung sdi',
    ],
    'SK Innovation': [
        'sk innovation', 'sk on', 'sk battery',
    ],
    'LG Energy Solution': [
        'lg energy', 'lg chem',
    ],
    'Umicore': [
        'umicore',
    ],
    'Johnson Matthey': [
        'johnson matthey',
    ],
    'thyssenkrupp': [
        'thyssenkrupp', 'thyssen krupp', 'thyssen',
    ],
    'BASF': [
        'basf',
    ],
    'Magna International': [
        'magna',
    ],
    'Denso': [
        'denso',
    ],
    'Aptiv': [
        'aptiv', 'delphi',
    ],
    'CIE Automotive': [
        'cie automotive',
    ],
    'Gestamp': [
        'gestamp', 'gestamp automocion',
    ],
    'Adient': [
        'adient',
    ],
    'ZKW Group': [
        'zkw',
    ],
    'Benteler': [
        'benteler',
    ],
    'CLN Group': [
        'cln', 'cln coils lamiere nastri',
    ],
    'Horse Powertrain': [
        'horse powertrain',
    ],
    'AGC Automotive': [
        'agc automotive', 'agc flat glass',
    ],
    'Rheinmetall': [
        'rheinmetall',
    ],
    'Hella': [
        'hella',
    ],
    'AVL': [
        'avl',
    ],
    'Vitesco Technologies': [
        'vitesco',
    ],
    'Symbio (Faurecia/Michelin)': [
        'symbio',
    ],
    'OPmobility (Plastic Omnium)': [
        'opmobility', 'plastic omnium',
    ],
    'Solvay': [
        'solvay',
    ],
    'InoBat Energy': [
        'inobat', 'inobat energy',
    ],
    'Skeleton Technologies': [
        'skeleton', 'skeleton technologies',
    ],
    'ElringKlinger': [
        'elringklinger',
    ],
    'EKPO Fuel Cell Technologies': [
        'ekpo',
    ],
}

# ============================================================================
# NATIONALITY CLASSIFICATION
# ============================================================================

COMPANY_NATIONALITY = {
    'Stellantis': ('EU', 'NL', 'Franco-Italian-American'),
    'Volkswagen Group': ('EU', 'DE', 'German'),
    'Mercedes-Benz Group': ('EU', 'DE', 'German'),
    'BMW Group': ('EU', 'DE', 'German'),
    'Renault Group': ('EU', 'FR', 'French'),
    'Toyota Group': ('JP', 'JP', 'Japanese'),
    'Volvo Cars (Geely)': ('CN-owned', 'SE', 'Chinese-owned (Geely)'),
    'AB Volvo (Trucks)': ('EU', 'SE', 'Swedish'),
    'Hyundai-Kia': ('KR', 'KR', 'Korean'),
    'Ford Motor': ('US', 'US', 'American'),
    'Robert Bosch': ('EU', 'DE', 'German'),
    'Continental AG': ('EU', 'DE', 'German'),
    'ZF Friedrichshafen': ('EU', 'DE', 'German'),
    'Valeo': ('EU', 'FR', 'French'),
    'Forvia (Faurecia)': ('EU', 'FR', 'French'),
    'Schaeffler': ('EU', 'DE', 'German'),
    'STMicroelectronics': ('EU', 'NL', 'Franco-Italian'),
    'Infineon': ('EU', 'DE', 'German'),
    'NXP Semiconductors': ('EU', 'NL', 'Dutch'),
    'Michelin': ('EU', 'FR', 'French'),
    'Pirelli': ('EU', 'IT', 'Italian (CN minority)'),
    'Bridgestone': ('JP', 'JP', 'Japanese'),
    'Northvolt': ('EU', 'SE', 'Swedish'),
    'ACC (Automotive Cells Company)': ('EU', 'FR', 'Franco-German-Italian'),
    'Iveco Group': ('EU', 'IT', 'Italian'),
    'Nissan': ('JP', 'JP', 'Japanese'),
    'Honda': ('JP', 'JP', 'Japanese'),
    'verkor': ('EU', 'FR', 'French'),
    'FREYR': ('EU', 'NO', 'Norwegian'),
    'Leclanché': ('EU', 'CH', 'Swiss'),
    'VARTA': ('EU', 'DE', 'German'),
    'Samsung SDI': ('KR', 'KR', 'Korean'),
    'SK Innovation': ('KR', 'KR', 'Korean'),
    'LG Energy Solution': ('KR', 'KR', 'Korean'),
    'Umicore': ('EU', 'BE', 'Belgian'),
    'Johnson Matthey': ('EU', 'GB', 'British'),
    'thyssenkrupp': ('EU', 'DE', 'German'),
    'BASF': ('EU', 'DE', 'German'),
    'Magna International': ('Other', 'CA', 'Canadian'),
    'Denso': ('JP', 'JP', 'Japanese'),
    'Aptiv': ('US', 'US', 'American'),
    'CIE Automotive': ('EU', 'ES', 'Spanish'),
    'Gestamp': ('EU', 'ES', 'Spanish'),
    'Adient': ('US', 'US', 'American'),
    'ZKW Group': ('KR', 'AT', 'Korean (LG subsidiary)'),
    'Benteler': ('EU', 'DE', 'German'),
    'CLN Group': ('EU', 'IT', 'Italian'),
    'Horse Powertrain': ('EU', 'ES', 'Franco-Chinese (Renault-Geely JV)'),
    'AGC Automotive': ('JP', 'BE', 'Japanese'),
    'Rheinmetall': ('EU', 'DE', 'German'),
    'Hella': ('EU', 'DE', 'German (Faurecia subsidiary)'),
    'AVL': ('EU', 'AT', 'Austrian'),
    'Vitesco Technologies': ('EU', 'DE', 'German'),
    'Symbio (Faurecia/Michelin)': ('EU', 'FR', 'Franco-German (Faurecia/Michelin JV)'),
    'OPmobility (Plastic Omnium)': ('EU', 'FR', 'French'),
    'Solvay': ('EU', 'BE', 'Belgian'),
    'InoBat Energy': ('EU', 'SK', 'Slovak'),
    'Skeleton Technologies': ('EU', 'EE', 'Estonian'),
    'ElringKlinger': ('EU', 'DE', 'German'),
    'EKPO Fuel Cell Technologies': ('EU', 'DE', 'German (EKPO = ElringKlinger + Plastic Omnium)'),
}

# ============================================================================
# SECTOR CLASSIFICATION FOR GROUPS
# ============================================================================

SECTOR_TAGS = {
    'Stellantis': 'oem', 'Volkswagen Group': 'oem', 'Mercedes-Benz Group': 'oem',
    'BMW Group': 'oem', 'Renault Group': 'oem', 'Toyota Group': 'oem',
    'Volvo Cars (Geely)': 'oem', 'Hyundai-Kia': 'oem', 'Ford Motor': 'oem',
    'Iveco Group': 'oem', 'Nissan': 'oem', 'Honda': 'oem',
    'AB Volvo (Trucks)': 'truck_oem',
    'Robert Bosch': 'supplier', 'Continental AG': 'supplier',
    'ZF Friedrichshafen': 'supplier', 'Valeo': 'supplier',
    'Forvia (Faurecia)': 'supplier', 'Schaeffler': 'supplier',
    'Magna International': 'supplier', 'Denso': 'supplier', 'Aptiv': 'supplier',
    'thyssenkrupp': 'supplier',
    'STMicroelectronics': 'semiconductor', 'Infineon': 'semiconductor',
    'NXP Semiconductors': 'semiconductor',
    'Northvolt': 'battery', 'ACC (Automotive Cells Company)': 'battery',
    'verkor': 'battery', 'VARTA': 'battery', 'FREYR': 'battery', 'Leclanché': 'battery',
    'Samsung SDI': 'battery', 'SK Innovation': 'battery',
    'LG Energy Solution': 'battery',
    'Umicore': 'battery_materials', 'Johnson Matthey': 'battery_materials',
    'BASF': 'battery_materials',
    'Michelin': 'tire', 'Pirelli': 'tire', 'Bridgestone': 'tire',
    'CIE Automotive': 'supplier', 'Gestamp': 'supplier',
    'Adient': 'supplier', 'ZKW Group': 'supplier',
    'Benteler': 'supplier', 'CLN Group': 'supplier',
    'Horse Powertrain': 'supplier', 'AGC Automotive': 'supplier',
    'Rheinmetall': 'supplier', 'Hella': 'supplier',
    'AVL': 'supplier', 'Vitesco Technologies': 'supplier',
    'Symbio (Faurecia/Michelin)': 'hydrogen_fc',
    'OPmobility (Plastic Omnium)': 'supplier',
    'Solvay': 'battery_materials',
    'InoBat Energy': 'battery',
    'Skeleton Technologies': 'battery',
    'ElringKlinger': 'supplier',
    'EKPO Fuel Cell Technologies': 'hydrogen_fc',
}


# ============================================================================
# MATCHER CONFIG OVERRIDES (automotive-specific)
# ============================================================================

# Companies classified by sector (for the original automotive_matcher.py sector tagging)
BATTERY_COMPANIES = frozenset({
    'northvolt', 'samsung sdi', 'lg energy solution', 'sk innovation',
    'catl', 'svolt', 'envision aesc', 'acc', 'verkor', 'freyr',
    'italvolt', 'britishvolt', 'powervolt', 'varta', 'panasonic energy',
    'umicore', 'johnson matthey',
})

EV_CHARGING_COMPANIES = frozenset({
    'ionity', 'chargepoint', 'allego', 'fastned',
})

CHINESE_COMPANIES = frozenset({
    'li auto', 'nio', 'xpeng', 'byd', 'wm motors', 'yema automobile',
    'great wall motor', 'great wall', 'geely', 'chery', 'saic', 'dongfeng',
    'beijing electric vehicle', 'gac', 'catl', 'envision aesc',
    'changan', 'leapmotor', 'svolt',
})

OTHER_NON_EU = frozenset({
    'toyota motor', 'toyota', 'honda motor', 'honda',
    'nissan motor', 'nissan', 'subaru', 'mazda motor', 'mazda',
    'suzuki motor', 'suzuki', 'mitsubishi motors', 'mitsubishi',
    'samsung sdi', 'lg energy solution', 'sk innovation',
    'panasonic energy', 'rivian', 'lucid',
})

NON_EU_COMPANIES = CHINESE_COMPANIES | OTHER_NON_EU

AMBIGUOUS_COMPANIES = {
    'rolls royce': 'aerospace/energy — Rolls-Royce Motor Cars is BMW subsidiary; subsidy matches are likely Rolls-Royce Holdings (aerospace)',
}

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
