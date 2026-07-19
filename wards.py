# wards.py — RoadSeva Ward & Division Master Data
# ================================================
# Source: GVMC Engineering Civil Works Division — OFFICIAL DOCUMENT
# (As shown in the scanned division table, Jan 2026)
#
# IMPORTANT NOTES:
#   - Division 2 is spelled "Madhurawada" (single d) per official document
#   - Ward numbers match exactly the official GVMC engineering division table
#   - WARD_NAMES uses real area names for citizen dropdown (not division names)
#   - Chodavaram / Sabbavaram / Yelamanchili are NOT GVMC wards — removed
#   - Ward 98 belongs to Madhurawada Division (geographically Kommadi/Kapuluppada)

# ── Official Division → Ward number mapping ───────────────────────────────────
# Source: GVMC Engineering Civil Works Division table (official scanned document)

DIVISION_WARD_MAP = {
    "Bheemunipatnam": [1, 2, 3, 4],
    "Madhurawada":     [5, 6, 7, 8, 98],
    "East":           [9, 10, 11, 12, 13, 15, 16, 17, 18, 19, 20, 21, 22, 23, 28],
    "South":          [27, 29, 30, 31, 32, 33, 34, 35, 36, 37, 38, 39, 41],
    "West":           [40, 52, 56, 57, 58, 59, 60, 61, 62, 63, 89, 90, 91, 92],
    "North":          [14, 24, 25, 26, 42, 43, 44, 45, 46, 47, 48, 49, 50, 51, 53, 54, 55],
    "Gajuwaka":       [64, 65, 66, 67, 68, 69, 70, 71, 72, 73, 74, 75, 76, 86, 87],
    "Aganampudi":     [77, 78, 79, 85],
    "Anakapalli":     [80, 81, 82, 83, 84],
    "Pendurthi":      [88, 93, 94, 95, 96, 97],
}

# ── Reverse lookup: ward number → division name ───────────────────────────────
# Built automatically — do not edit manually.
# Usage: WARD_DIVISION_MAP[22] → "East"
WARD_DIVISION_MAP = {
    ward: division
    for division, wards in DIVISION_WARD_MAP.items()
    for ward in wards
}

# ── Division metadata ─────────────────────────────────────────────────────────
DIVISION_INFO = {
    "Bheemunipatnam": {"ee_code": "Div-I",    "display": "Bheemunipatnam Division", "ward_count": 4},
    "Madhurawada":     {"ee_code": "Div-II",   "display": "Madhurawada Division",     "ward_count": 5},
    "East":           {"ee_code": "Div-III",  "display": "East Division",           "ward_count": 15},
    "South":          {"ee_code": "Div-IV",   "display": "South Division",          "ward_count": 13},
    "West":           {"ee_code": "Div-V",    "display": "West Division",           "ward_count": 14},
    "North":          {"ee_code": "Div-VI",   "display": "North Division",          "ward_count": 17},
    "Gajuwaka":       {"ee_code": "Div-VII",  "display": "Gajuwaka Division",       "ward_count": 15},
    "Aganampudi":     {"ee_code": "Div-VIII", "display": "Aganampudi Division",     "ward_count": 4},
    "Anakapalli":     {"ee_code": "Div-IX",   "display": "Anakapalli Division",     "ward_count": 5},
    "Pendurthi":      {"ee_code": "Div-X",    "display": "Pendurthi Division",      "ward_count": 6},
}

DIVISION_NAMES = list(DIVISION_WARD_MAP.keys())

# ── Zone → Division mapping (GVMC administrative zones, ABOVE divisions) ──────
# ⚠️  PLACEHOLDER — NOT YET FILLED. A zonal_commissioner's staff.zone value
# must match a key here, or they see ZERO wards (fail-closed, by design).
# Get the real zone→division breakdown from GVMC and fill this in before
# onboarding any real zonal_commissioner. Example shape:
#   ZONE_DIVISION_MAP = {
#       "Zone 1": ["Bheemunipatnam", "Madhurawada"],
#       "Zone 2": ["East", "North"],
#       ...
#   }
ZONE_DIVISION_MAP = {}


def get_wards_for_zone(zone_name: str) -> list:
    """
    Returns ward NAME strings (e.g. 'Ward 22 - Seethammadhara') for a zone,
    via ZONE_DIVISION_MAP → DIVISION_WARD_MAP → WARD_NAMES.
    Mirrors get_wards_for_division() exactly, one level up.
    Returns an EMPTY list if the zone isn't configured — intentional.
    An empty result means "show nothing" to the caller, not "everything."
    """
    divisions = ZONE_DIVISION_MAP.get(zone_name, [])
    ward_nums = set()
    for div in divisions:
        ward_nums.update(DIVISION_WARD_MAP.get(div, []))
    return [w for w in WARD_NAMES if _ward_num_from_name(w) in ward_nums]


def get_division_for_ward_name(ward_name: str) -> str:
    """
    Extract ward number from a ward name string and return its division.

    'Ward 22 - Seethammadhara'           → 'East'
    'Ward 64 - Pedagantyada / Yarada'    → 'Gajuwaka'
    'Ward 98 - Kommadi / Kapuluppada 2'  → 'Madhurawada'
    Returns 'Unknown' if ward number cannot be parsed.
    """
    try:
        parts = ward_name.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "ward":
            ward_num = int(parts[1].rstrip("-.,"))
            return WARD_DIVISION_MAP.get(ward_num, "Unknown")
    except (ValueError, IndexError):
        pass
    return "Unknown"


def get_wards_for_division(division_name: str) -> list:
    """Returns list of ward name strings belonging to a given division."""
    ward_nums = set(DIVISION_WARD_MAP.get(division_name, []))
    return [w for w in WARD_NAMES if _ward_num_from_name(w) in ward_nums]


def _ward_num_from_name(ward_name: str) -> int:
    """Extract ward number from 'Ward 22 - ...' → 22. Returns 0 on failure."""
    try:
        parts = ward_name.strip().split()
        if len(parts) >= 2 and parts[0].lower() == "ward":
            return int(parts[1].rstrip("-.,"))
    except (ValueError, IndexError):
        pass
    return 0


# ── 98 GVMC Ward Names — citizen dropdown ────────────────────────────────────
# These are the real area names citizens recognise.
# Ordered by ward number 1→98.
# All 98 wards map to one of the 10 official GVMC Engineering Divisions above.
# NOTE: Wards 31-37 appear as "Gajuwaka/Steel Plant" in some old lists —
#       those are incorrect unofficial names. Official GVMC document lists
#       these under South/West/North divisions as shown in the division table.

WARD_NAMES = [
    # ── Bheemunipatnam Division (Wards 1-4) ──────────────────────────────────
    "Ward 1 - Kondapeta / Wilsonpeta",
    "Ward 2 - Bheemili / Chinnabazar",
    "Ward 3 - Yeguvapeta / Bheemili",
    "Ward 4 - Pedda Uppada / Chepaluppada",

    # ── Madhurawada Division (Wards 5-8, 98) ──────────────────────────────────
    "Ward 5 - Marikavalasa / Bottavanipalem",
    "Ward 6 - Madhurawada / Bakkannapalem",
    "Ward 7 - Madhurawada / Vambay Colony",
    "Ward 8 - Yendada / Sagarnagar",

    # ── East Division (Wards 9-13, 15-23, 28) ────────────────────────────────
    "Ward 9 - Visalakshinagar",
    "Ward 10 - Vivekananda Nagar / Arilova",
    "Ward 11 - Arilova / Pedagadili",
    "Ward 12 - Arilova / Sangivalasa",
    "Ward 13 - Arilova Colony",
    "Ward 14 - Seethammadhara / BS Layout",
    "Ward 15 - MVP Colony Sector 1 & 2",
    "Ward 16 - MVP Colony Sector 3 & 4",
    "Ward 17 - MVP Colony Sector 5 & 6",
    "Ward 18 - MVP Colony Sector 7 & 8",
    "Ward 19 - MVP Colony Sector 9 & 10",
    "Ward 20 - Waltair / Pedawaltair",
    "Ward 21 - Chinawaltair / Lawsons Bay",
    "Ward 22 - Sivajipalem / AU Campus",
    "Ward 23 - Seethammadhara / KRM Colony",

    # ── North Division (Wards 14, 24-26, 42-51, 53-55) ───────────────────────
    # Ward 14 is North Division (Seethammadhara BS Layout area)
    "Ward 24 - Nakkavanipalem / Old Resapuvanipalem",
    "Ward 25 - Madhura Nagar / Rajendra Nagar",
    "Ward 26 - Akkayapalem / NGGO Colony",

    # ── South Division (Wards 27, 29-39, 41) ─────────────────────────────────
    "Ward 27 - Srinagar / Dondaparthi",
    "Ward 28 - Ram Nagar / Daspalla Hills",
    "Ward 29 - Maharanipeta / Kannayyapeta",
    "Ward 30 - Maharanipeta / Jalaripeta / KGH",
    "Ward 31 - Dabagardens / Suryabagh",
    "Ward 32 - Allipuram / South Jail Road",
    "Ward 33 - Allipuram / Bangaramma Metta",
    "Ward 34 - Dabagardens / Ambedkar Colony",
    "Ward 35 - Chinawaltair / Stadium Road",
    "Ward 36 - Agraharam / Chengalaraopeta",
    "Ward 37 - Paindorapeta / Relli Veedhi",
    "Ward 38 - Agraharam / SKML Street",
    "Ward 39 - Thomson Street / Kurpam Market",

    # ── West Division (Wards 40, 52, 56-63, 89-92) ───────────────────────────
    "Ward 40 - Malkapuram / Dolphin Hills",
    "Ward 41 - Gnanapuram / Railway Quarters",

    # North Division continued (42-51, 53-55)
    "Ward 42 - Thatichetlapalem / Jaganadhapuram",
    "Ward 43 - Nandagiri Nagar / Akkayyapalem",
    "Ward 44 - Nandagiri Nagar / Thatichetlapalem",
    "Ward 45 - Port Quarters / Simhagiri Colony",
    "Ward 46 - Seethammadhara / Kailasapuram Road",
    "Ward 47 - Kancharapalem / Kapparada",
    "Ward 48 - Kancharapalem / Indira Nagar",
    "Ward 49 - Kancharapalem / PR Gardens",
    "Ward 50 - Madhavadhara / Murali Nagar",
    "Ward 51 - Marripalem / Madhavadhara",

    # West Division continued
    "Ward 52 - NAD / Karasa / Marripalem VUDA",
    "Ward 53 - Marripalem / Rana Pratap Nagar",
    "Ward 54 - Kancharapalem / 104 Area",
    "Ward 55 - Thatichetlapalem / Kancharapalem",
    "Ward 56 - Gavara Kancharapalem / NDA Area",
    "Ward 57 - Thummadapalem / NAD Colony",
    "Ward 58 - Sriharipuram / Malkapuram",
    "Ward 59 - Malkapuram / Nakkavanipalem",
    "Ward 60 - Malkapuram / Ambedkar Colony",
    "Ward 61 - Malkapuram / Rama Krishna Puram",
    "Ward 62 - Malkapuram / Trinadhapuram",
    "Ward 63 - Gandhigram / Yarada",

    # ── Gajuwaka Division (Wards 64-76, 86-87) ───────────────────────────────
    "Ward 64 - Pedagantyada / Yarada / Gangavaram",
    "Ward 65 - Pedagantyada / Dayal Nagar",
    "Ward 66 - New Gajuwaka / BC Road",
    "Ward 67 - Old Gajuwaka / Auto Nagar",
    "Ward 68 - Mindi / BHPV / Akkireddy Palem",
    "Ward 69 - Tunglam / BHPV Township",
    "Ward 70 - Old Gajuwaka / MMTC Colony",
    "Ward 71 - Auto Nagar / Chinagantyada",
    "Ward 72 - Chinagantyada / Chaitanya Nagar",
    "Ward 73 - Gajuwaka / Sanath Nagar",
    "Ward 74 - Pedagantyada / Siddeswaram",
    "Ward 75 - Pedagantyada / Nellimukku",
    "Ward 76 - Gajuwaka / Korada / Peda Nadupuru",

    # ── Aganampudi Division (Wards 77-79, 85) ────────────────────────────────
    "Ward 77 - Pittavanipalem / Steel Plant Sectors 1 & 2",
    "Ward 78 - Steel Plant Sectors 3 to 11",
    "Ward 79 - Lankelapalem / Aganampudi / Sanivada",

    # ── Anakapalli Division (Wards 80-84) ────────────────────────────────────
    "Ward 80 - Anakapalli / Gavarapalem",
    "Ward 81 - Anakapalli / GNT Road",
    "Ward 82 - Anakapalli / Narasinga Rao Peta",
    "Ward 83 - Anakapalli / Miriyala Colony",
    "Ward 84 - Anakapalli / Parawada / Koppaka",

    # ── Aganampudi Division (Ward 85) ────────────────────────────────────────
    "Ward 85 - Aganampudi / Lankelapalem / E-Marripalem",

    # ── Gajuwaka Division (Wards 86-87) ──────────────────────────────────────
    "Ward 86 - Kurmannapalem / Vadlapudi / Duvvada",
    "Ward 87 - Vadlapudi / RH Colony / Kanithi Colony",

    # ── Pendurthi Division (Wards 88, 93-97) ─────────────────────────────────
    "Ward 88 - Narava / Duvvada / Vedulla Narava",

    # ── West Division (Wards 89-92) ──────────────────────────────────────────
    "Ward 89 - Gopalapatnam / Kothapalem",
    "Ward 90 - Butchirajupalem / NSTL Quarters",
    "Ward 91 - Gopalapatnam / Old Gopalapatnam",
    "Ward 92 - Venkatapuram / Kamparapalem",

    # ── Pendurthi Division continued (93-97) ─────────────────────────────────
    "Ward 93 - Vepagunta / Pendurthi / Prahaladhapuram",
    "Ward 94 - Vepagunta / Purushothapuram",
    "Ward 95 - Pendurthi / Cheemalapalli",
    "Ward 96 - Pendurthi / Old Village",
    "Ward 97 - Pendurthi / Chinamushidiwada",

    # ── Madhurawada Division (Ward 98) ────────────────────────────────────────
    "Ward 98 - Simhachalam / Kommadi / Kapuluppada",
]

# ── Locality → Ward lookup (from detailed WARD_DATA) ─────────────────────────
WARD_DATA = {
    "Bheemunipatnam Division": {
        "Ward 1 - Kondapeta / Wilsonpeta": ["Bunglow Metta","Vempadavariveedhi","Kapuveedhi","Kondapeta","Kondapeta Colony","Wilsonpeta","Wilson Peta Company Quarters","Jutemill Quarters","Reddika Veedhi","Velam Peta","Chakalipeta","Harizanapeta","Rajaveedhi","Ramaveedhi","Nagarapuveedhi","Balaji Nagar","Venkateswara Metta"],
        "Ward 2 - Bheemili / Chinnabazar": ["Sabbivanipeta","VUDA Complex","Satyanarayana Peta Market Road","Bheemili Road","Santha Road","NTR Kalyana Mandapam","Kummari Veedhi","Gollaveedhi","Adharsh Nagar","Jeerupeta","Pallikummaripalem","Rayipalem","Rajalingampeta","Mamidipalem","Nammivanipeta Colony","Nammivanipeta","Sangivalasa Colony","Sangivalasa"],
        "Ward 3 - Yeguvapeta / Bheemili": ["Telephone Exchange Area","Cinema Hall Area","Dutch Road","Old ITI","Gaduveedhi","Burma Colony","Smith Street","Nalli Veedhi","Sai Baba Temple","Subhash Road","St Anns Grounds","Bank Road","Appikonda Veedhi","Old Bus Stand Area","Nehru Street","Reddikaveedhi","Municipal High School Area","Yeguvapeta","Police Quarters Area","Nerella Valasa Colony","Beach Road"],
        "Ward 4 - Pedda Uppada / Chepaluppada": ["Nidigattu Panchayat","Chepaluppada Panchayat","Kapuluppada Panchayat","K Nagarpalem Panchayat","JV Agraharam Panchayat"],
    },
    "Madhurawada Division": {
        "Ward 5 - Marikavalasa / Bottavanipalem": ["Boyyipalem Junction","Paradesipalem","EWS Colony","JK Layout","Ambedkar Colony","Marikavalasa NGOs Drivers Colony","Kommadi Village","Kommadi","Kommadi SC Colony","Sai Ram Colony","Bottavanipalem","Sadguru Sai Colony","Sri Laxmi Nagar","Marikavalasa Hill"],
        "Ward 6 - Madhurawada / Bakkannapalem": ["Car Shed Junction","Laxmivanipalem","RH Colony","HB Colony","Ashok Nagar","Bakkannapalem","NTR Colony","FCI Layout","Gayatri Nagar","Chandrampalem","Madhurawada Junction","Srinivasa Nagar","Devimetta"],
        "Ward 7 - Madhurawada / Vambay Colony": ["Vambay Colony","Krishna Nagar","Madhurawada Junction","Port Colony","Kalanagar","Mallayyapalem","Mallayyapalem SC Colony","Priyadarsini Colony","Bapujinagar","Shyam Nagar","Old Madhurawada","RTC Colony"],
        "Ward 8 - Yendada / Sagarnagar": ["Law College","HB Colony","PK Palem","Old PM Palem","Sagarnagar HIG-1","Sagarnagar HIG-II","Sagarnagar MIG-A","Sagarnagar MIG-B","Sagarnagar LIG-A","Sagarnagar LIG-B","Yendada Main Village","Vivekananda Nagar","Gollala Yendada","Chinna Rushikonda","Peda Rushikonda","Chandrampalem"],
        "Ward 98 - Simhachalam / Kommadi / Kapuluppada": ["Appannapalem","Simhapuri Layout","Adivivaram","SC Colony","MMTC Colony","Gandhi Nagar","Simhachalam Hill","Srinivasa Kalyanamandapam","Sai Baba Temple Street","Balaji Nagar Up","Sai Madhava Nagar Sector-2 & 3"],
    },
    "East Division": {
        "Ward 9 - Visalakshinagar": ["Ramalayam Street","Sanjay Nagar","Sanjeev Nagar Colony","SC & ST Colony","Visalakshi Nagar","Jodugullapalem","Dayal Nagar","Police Quarters","Dairy Farm"],
        "Ward 10 - Vivekananda Nagar / Arilova": ["Rajeev Nagar","Indira Nagar","Sundar Nagar","Vivekananda Nagar","SIG Nagar","BC Colony","Ravindra Nagar"],
        "Ward 11 - Arilova / Pedagadili": ["Raveendra Nagar II","Srihari Nagar","Gandhi Nagar","Vivekananda Nagar","Balaji Nagar","Anna Nagar","Sri Ram Nagar","Krishna Nagar","Arilova Sector-I","Kranthi Nagar I & II","Mayuri Nagar","Durga Nagar"],
        "Ward 12 - Arilova / Sangivalasa": ["Rajiv Nagar & Central Prison","Surya Teja Nagar","Parvathi Nagar","BTR Nagar","Nehru Nagar","Ambedkar New Colony","Durga Nagar & Parvathi Nagar"],
        "Ward 13 - Arilova Colony": ["Sundarayya Nagar","Nehru Nagar","Vishnupuri Colony","Siva Sankar Nagar","Sivaji Nagar I","NMR Colony","Durga Bazar","Sai Nagar","Pandurangapuram","Central Prison Area"],
        "Ward 15 - MVP Colony Sector 1 & 2": ["Kotha Venkojipalem","Housing Board Office Area","MVP Colony Sector-1","MVP Colony Sector-2","Satyasai School Area","TTD Kalyanamandapam Opposite Area","Lakshmi Nagar","Pedawaltair","Pedawaltair Village"],
        "Ward 16 - MVP Colony Sector 3 & 4": ["MVP Colony Sector-3","MVP Colony Sector-4","Near Veera Brhammam Temple","Girijana Bhavan Area","SDAH High School Area"],
        "Ward 17 - MVP Colony Sector 5 & 6": ["MVP Colony Sector-5","MVP Colony Sector-6","Indira Nagar"],
        "Ward 18 - MVP Colony Sector 7 & 8": ["State Bank Colony","MVP Colony Sector-7","MVP Colony Sector-8"],
        "Ward 19 - MVP Colony Sector 9 & 10": ["Samatha College Area","MVP Colony Sector-9 & 10","Kailashagiri Konda Route Lane","AS Raja Ground Area","Cancer Hospital Area"],
        "Ward 20 - Waltair / Pedawaltair": ["Pedawaltair Junction","Vijayanagar Palace Layout Area","Nethaji Veedhi Chinawaltair","Kanakammavari Gudi Area","East Point Colony","Fish Bazar Veedhi","Nauka Nagar","Six Tap Junction"],
        "Ward 21 - Chinawaltair / Lawsons Bay": ["MVP Colony Sector-12","MVP Colony Sector-11","Opp KKR Gowtham School Lawsonsbay Colony","Beach Road","Vasuvanipalem","Jalaripeta"],
        "Ward 22 - Sivajipalem / AU Campus": ["Harbour Park Road","HPCL Quarters","Kirlampudi Layout","Peethalavariveedhi","Defence Flats","Nethaji Veedhi","Kona Veedhi","Tamil Street","Relli Veedhi","Church Street","AU Campus","Sivajipalem"],
        "Ward 23 - Seethammadhara / KRM Colony": ["EENADU Main Road","Priyadarshini Colony","Vijaya Residency Area","Chaitanya Nagar","Maddilapalem Sivalayam Backside","KRM Colony Park Area","Vivekananda Park","SFS School Area"],
        "Ward 28 - Ram Nagar / Daspalla Hills": ["Circuit House","Daspalla Hills","Harbour Quarters Park","Ram Nagar","Kailasametta","Bhanoji Nagar","Nehru Nagar","Nowroji Road","JSM Colony"],
    },
    "North Division": {
        "Ward 14 - Seethammadhara / BS Layout": ["Near NRI Hospital","TPT Colony","BS Layout","Raghavendra Nursing Home Area","Presidential School Area","Bilal Colony","ASR Nagar"],
        "Ward 24 - Nakkavanipalem / Old Resapuvanipalem": ["Kranthi Nagar","Nakkavanipalem","Shivalayam Street 60 Feet Road","Old Resapuvanipalem","Tulasipeta","Ramatalkies Road","Gandhi Nagar","P&T Colony"],
        "Ward 25 - Madhura Nagar / Rajendra Nagar": ["Madhura Nagar","Chakali Gedda","Seethammpeta Vinayaka Temple Area","Shantipuram","Sai Baba Temple Area","Rajendra Nagar","80 Feet Road"],
        "Ward 26 - Akkayapalem / NGGO Colony": ["Lalitha Nagar","Krishna Hospital Road","80 Feet Road","NGGO's Colony","St Mary School Akkayapalem","Ramakrishna Nagar","NH 5"],
        "Ward 42 - Thatichetlapalem / Jaganadhapuram": ["Jaganadhapuram","Chakalipeta","Gas Godown Area","Railway New Colony","Ramalayam Street","Sai Baba Temple"],
        "Ward 43 - Nandagiri Nagar / Akkayyapalem": ["Akkayyapalem 80 Feet Road","HP Gas Godown","KV Quarters","Nandagiri Nagar","Narendra Nagar","Post Office Street","Railway New Colony","Srinivasa Nagar","Vivekananda Hospital Road"],
        "Ward 44 - Nandagiri Nagar / Thatichetlapalem": ["Santoshi Matha Temple","Abid Nagar","Akkayapalem Highway","Gavara Thatichetlapalem","KV Quarters","Muslim Thatichetlapalem","Srinivasa Nagar"],
        "Ward 45 - Port Quarters / Simhagiri Colony": ["Port Quarters","Simhagiri Colony","Varahagiri Colony","Ganesh Nagar","Narasimha Nagar","Prasanthi Nagar"],
        "Ward 46 - Seethammadhara / Kailasapuram Road": ["Madhusudhan Nagar","Kasturi Nagar","Santhi Nagar","Kailasapuram Road","Laxminarayanapuram"],
        "Ward 47 - Kancharapalem / Kapparada": ["Ambedkar Nagar","Ramji Estate","Indira Nagar","Kapparada","Madhusudhan Nagar"],
        "Ward 48 - Kancharapalem / Indira Nagar": ["ASR Nagar","Bapuji Nagar","Indira Nagar I","Indira Nagar II","Jai Bharat Nagar","Palnati Colony","Pedda Kotturu","Srinivasa Nagar"],
        "Ward 49 - Kancharapalem / PR Gardens": ["Kamakshi Nagar","Burma Camp","Lal Bahadur Nagar","Burma Colony","Girija Nagar","PR Gardens","Nehru Nagar","NGGO's Colony"],
        "Ward 50 - Madhavadhara / Murali Nagar": ["Murali Nagar","Singaraya Metta","Sai Ram Nagar","Madhavadhara","Vidya Nagar","Kalinga Nagar","Seethanna Gardens"],
        "Ward 51 - Marripalem / Madhavadhara": ["Madhavadhara","Seethanna Gardens","Gold Smith Colony","Bheem Nagar","Gandhi Nagar","R & B Colony","Ambedkar Colony"],
        "Ward 53 - Marripalem / Rana Pratap Nagar": ["Bhaskar Gardens","Rana Pratap Nagar","Andhra Kesari Nagar","Harsha Nagar","Parvathi Nagar","Marripalem","Marripalem Main Road"],
        "Ward 54 - Kancharapalem / 104 Area": ["104 Area Main Road","Bapuji Nagar","Gajapathinagar","Jyothi Nagar","Lakshmi Nagar","Kancharapalem Main Road","Nalanda Nagar","Sai Nagar"],
        "Ward 55 - Thatichetlapalem / Kancharapalem": ["Jashuvanagar","Ashok Nagar","Reddy Kancharapalem","Bapuji Nagar","Thatichetlapalem Bus Stop","Kapparada","Kancharapalem","Tikkavanipalem","Dharmanagar"],
    },
    "South Division": {
        "Ward 27 - Srinagar / Dondaparthi": ["VIP Road","Rama Talkies Road","Amar Nagar","Timpany School Area","Srinagar","Ashok Nagar","Bhagatsingh Colony","Dondaparthi","Akkayapalem Road"],
        "Ward 29 - Maharanipeta / Kannayyapeta": ["Kannayyapeta","Apsara Main Road","Prakasha Rao Peta","Anthony Nagar","Nowroji Road","Ramajogi Peta","Beach Road","Dandu Bazar","Venkatapathiraju Nagar"],
        "Ward 30 - Maharanipeta / Jalaripeta / KGH": ["Old Employment Office","Kotta Jalaripeta","KGH Opposite","Jalaripeta","Kummara Veedhi","Salipeta","Super Bazar Area","ZP Road","Surya Hospital Road","Beach Road","Official Colony"],
        "Ward 31 - Dabagardens / Suryabagh": ["Bheem Nagar","South Jail Road","Assam Gardens","Krishna Gardens","Dabagardens","Yellammathota","Suryabagh","APSRTC Complex","Lalitha Colony","SBI Colony","Spring Road","75 Feet Road"],
        "Ward 32 - Allipuram / South Jail Road": ["South Jail Road","Nerella Koneru Road","Allipuram Road","Karanala Veedhi","Chakrathota","Railway Quarters","Bheem Nagar","Krishnanagar"],
        "Ward 33 - Allipuram / Bangaramma Metta": ["South Jail Road","Kummari Veedhi","Bangaramma Metta","Harijana Veedhi","Venkateswara Metta","Dabagardens","Leela Mahal Road"],
        "Ward 34 - Dabagardens / Ambedkar Colony": ["SRMT Road","Ambedkar Colony","Netaji Nagar","Atchiyamma Peta","Bhupesh Nagar","Taraka Rama Colony"],
        "Ward 35 - Chinawaltair / Stadium Road": ["Stadium Road","Prasad Gardens","Periki Veedhi","Velampeta","75 Feet Road","Gajula Veedhi"],
        "Ward 36 - Agraharam / Chengalaraopeta": ["Agraharam Veedhi","Sunnapu Veedhi","Chengalaraopeta","SKML Street","Relli Veedhi","Town Main Road"],
        "Ward 37 - Paindorapeta / Relli Veedhi": ["Relli Veedhi","Paindorapeta","Asipapa Veedhi","Kotta Relli Veedhi","Agraharam Veedhi","Chengalaraopeta","Gollaveedhi"],
        "Ward 38 - Agraharam / SKML Street": ["Agraharam Veedhi","Chengalaraopeta","Town Hall Road","SKML Street","Thomson Street","Beach Road","Soldier Peta","Town Main Road","St Alloys High School Road"],
        "Ward 39 - Thomson Street / Kurpam Market": ["Thomson Street","Kota Veedhi","Potti Sriramulu Park","Kurpam Market","Vada Veedhi","Chilakapeta","Padma Nagar","Ferry Road"],
        "Ward 41 - Gnanapuram / Railway Quarters": ["Jaganadhapuram","Babu Colony","Gnanapuram Scheme Houses","Post Office Street","RC Aided School Area","Railway New Colony","Railway Quarters","Seva Nagar","TSN Colony","Venkata Raju Nagar"],
    },
    "West Division": {
        "Ward 40 - Malkapuram / Dolphin Hills": ["AKC Colony","Amzeri Park","Dolphin Hills","Naval Park","Shipyard Colony"],
        "Ward 52 - NAD / Karasa / Marripalem VUDA": ["Airport & New Police Quarters","Durga Puram","Ganesh Nagar","Gowri Nagar","Marripalem VUDA Layout","New Karasa","Old Karasa","Old Police Quarters","Netaji Colony"],
        "Ward 56 - Gavara Kancharapalem / NDA Area": ["Dayanand Nagar","Durga Nagar","Gavara Kancharapalem","Golla Kancharapalem","Kancharapalem Main Road","NDA Area","Railway Quarters"],
        "Ward 57 - Thummadapalem / NAD Colony": ["Bharat Nagar","Bhavani Gardens","Marripalem","MES Quarters","NAD Civilians Residential Colony","NAD Colony","Nirman Park","Ramu Naidu Colony","Thummadapalem"],
        "Ward 58 - Sriharipuram / Malkapuram": ["Ajantha Colony","Annamma Colony","Annapurna Nagar","Gollapalem","Mulagada","Old Ramalayam"],
        "Ward 59 - Malkapuram / Nakkavanipalem": ["Ashok Nagar Colony","Checkpost","Ex Serviceman Colony","Ganapathi Nagar","Hanuman Nagar","Himachal Nagar","Mulagada Housing Colony","Nakkavani Palem","Nehru Nagar"],
        "Ward 60 - Malkapuram / Ambedkar Colony": ["Ashok Nagar","Burma Colony","Ganesh Mandir Street","Indira Colony","Jawahar Nagar","Main Road Ramalayam Street","Nehru Nagar"],
        "Ward 61 - Malkapuram / Rama Krishna Puram": ["Gandhiji Street","Golla Street","Industrial Colony","Jalari Street","Malkapuram","Prakash Nagar","Priyadarshini Colony","Rama Krishna Puram"],
        "Ward 62 - Malkapuram / Trinadhapuram": ["Achi Naidu Thota","Ambedkar Colony","ASR Colony","CISF Quarters","Coast Guard","Durga Nagar Colony","Gandhiji Street","Harijana Veedhi","Kranthi Nagar","Nauseena Bagh","Port Quarters"],
        "Ward 63 - Gandhigram / Yarada": ["Chinthala Lova","Double Tank","Kakarlova","Kranthi Nagar","Naval Quarters Yarada"],
        "Ward 89 - Gopalapatnam / Kothapalem": ["Chandra Nagar","Adarsha Nagar","Surya Nagar","Kothapalem","Nagendra Nagar","Santhosh Nagar","Ganesh Nagar","Yellapuvanipalem","Bhagatsingh Nagar"],
        "Ward 90 - Butchirajupalem / NSTL Quarters": ["Vidyuth Nagar","Seetharamarajunagar","Butchirajupalem Main Road","APSEB Colony","Gandhi Nagar","Narasimha Nagar","NSTL Quarters","Viman Nagar","Srinivasa Nagar"],
        "Ward 91 - Gopalapatnam / Old Gopalapatnam": ["Nethaji Veedhi","Station Road","SBI Main Road","Prasanthi Nagar","Ramakrishna Nagar","Lakshmi Nagar","Harijana Colony","Old Gopalapatnam"],
        "Ward 92 - Venkatapuram / Kamparapalem": ["Venkatapuram","SC Colony","Padmanabha Nagar","Kamparapalem","Sri Ram Nagar","Bapuji Nagar","Ajantha Park","Railway Quarters","Narasimha Nagar","Indira Nagar"],
    },
    "Gajuwaka Division": {
        "Ward 64 - Pedagantyada / Yarada / Gangavaram": ["Yarada","Gangavaram","Jalaripeta","Pallivedhi","Yathapalem","Kongapalem","Venkannapalem","RH Colony","Satyanarayanapuram"],
        "Ward 65 - Pedagantyada / Dayal Nagar": ["Sanjeevagiri Colony","Bhanojithota","Netaji Colony","Sanjeeva Colony","Vikas Nagar","KL Rao Nagar","Bhavani Nagar","Kotta Dibbapalem","Girija Colony","Vambay Colony","Ashok Nagar","Bapuji Colony"],
        "Ward 66 - New Gajuwaka / BC Road": ["Azeemabad","BC Road","Gajuwaka Main Road Part","Indira Colony","Kailash Nagar"],
        "Ward 67 - Old Gajuwaka / Auto Nagar": ["Ganesh Nagar","New Gajuwaka","Gajuwaka Main Road Part","High School Road","Pentayya Nagar","Krishna Nagar","Old Gajuwaka","Neelkamal Road"],
        "Ward 68 - Mindi / BHPV / Akkireddy Palem": ["Akkireddy Palem","Panchavati","Ram Nagar","Mindi","Kalika Nagar","Giriprasad Nagar","Rajeev Nagar"],
        "Ward 69 - Tunglam / BHPV Township": ["Chukkavaani Palem","Nathayyapalem","Tunglam","Tunglam SC Colony","BHPV Township BHEL","Venkateswara Colony","Sheela Nagar"],
        "Ward 70 - Old Gajuwaka / MMTC Colony": ["Chattivanipalem","Old Gajuwaka","MMTC Colony","Chittinaidu Colony","Dasamikonda Colony","Drivers Colony","TVN Colony","LV Nagar","Srinivas Nagar"],
        "Ward 71 - Auto Nagar / Chinagantyada": ["Auto Nagar","Durga Nagar","Sundarayya Colony","Kunchamamba Colony","Dattasai Nagar","Visweswarayya Colony","Railway Quarters Vadlapudi","Sri Nagar","Sriram Nagar"],
        "Ward 72 - Chinagantyada / Chaitanya Nagar": ["Chinagantyada SC Colony","Chaitanya Nagar","Prasanthi Nagar","China Nadupuru","Old Karnavani Palem","VUDA Colony","Sri Nagar","BC Social Welfare Colony","Kanithi Road"],
        "Ward 73 - Gajuwaka / Sanath Nagar": ["China Nadupuru Part","Gonthinavanipalem","Kailash Nagar","Kotha Karnavani Palem","Pydimamba Colony","Sanath Nagar","Simhagiri Colony"],
        "Ward 74 - Pedagantyada / Siddeswaram": ["Nehru Nagar","BC Road","Ambedkar Colony","Siddeswaram","TGR Nagar","Vempal Nagar","Dayal Nagar","Simhagiri Colony","Peda Korada"],
        "Ward 75 - Pedagantyada / Nellimukku": ["Pedagantyada","Patha Ayyannapalem","Neelapuvedhi","Nellimukku","Ayyannapalem","Durgavanipalem","Seekuvanipalem","Seetharam Nagaram","Pedakorada"],
        "Ward 76 - Gajuwaka / Korada / Peda Nadupuru": ["Bala Cheruvu Sivalayam Veedhi","Korada","APHB Colony","Ramachandra Nagar","Rickshaw Colony","Bharat Nagar","Peda Nadupuru","Seeravani Palem","Dairy Colony","Burma Colony"],
        "Ward 86 - Kurmannapalem / Vadlapudi / Duvvada": ["Uppara Colony","Duvvada Station Road","Kurmannapalem","ITI Road","Rajeev Nagar","Rasalamma Colony","VUDA Phase-I","Matru Sree Nagar","Duvvada Sector-II","Simon Nagar"],
        "Ward 87 - Vadlapudi / RH Colony / Kanithi Colony": ["Kurmannapalem Part","Vadlapudi RH Colony","Vadlapudi Kanithi Colony Main Road","Old Vadlapudi","Ganesh Nagar","Lakshmipuram Colony","Gavara Veedhi","Tirumala Nagar","Siddarda Nagar","Kanithi High School Road","TGR Nagar"],
    },
    "Aganampudi Division": {
        "Ward 77 - Pittavanipalem / Steel Plant Sectors 1 & 2": ["Goruduvanipalem","New Ginnipalem","Old Ginnivanipalem","Maddivanipalem","Palavalasa","Peda Devada","Peddapalem","Pittavanipalem","Butchayyapeta","Madina Bagh","Sri Ram Nagar","Steel Plant Sector I","Steel Plant Sector II","CISF Quarters"],
        "Ward 78 - Steel Plant Sectors 3 to 11": ["Steel Plant Sector 3","Steel Plant Sector 4","Steel Plant Sector 5","Steel Plant Sector 6","Steel Plant Sector 7","Steel Plant Sector 8","Steel Plant Sector 9","Steel Plant Sector 10","Steel Plant Sector 11"],
        "Ward 79 - Lankelapalem / Aganampudi / Sanivada": ["Sivaji Nagar","Gallavani Palem","Old Aganampudi","Santhi Nagar Satram","Srinivasa Nagar","KSN Reddy Nagar","Sanivada","Lankelapalem Karanam Vari Veedi","BC Colony","Konda Veedi","Kotturu Colony","Ganji Peta","Lankelapalem Industries","Ashok Nagar","Desapatruni Palem"],
        "Ward 85 - Aganampudi / Lankelapalem / E-Marripalem": ["Talariavanipalem","Dibbapalem","Mantripalem","Kotturu","Sri Ram Colony","Lankelapalem","E-Marripalem","Kotta Palem","E-Bonangi","Aganampudi","Pedamadaka Part","Sai Nagar","Aganampudi Kondayya Valasa"],
    },
    "Anakapalli Division": {
        "Ward 80 - Anakapalli / Gavarapalem": ["Tadi","Rajupalem","Valluru","Gavarapalem Pedda Veedhi","Santhoshimatha Gudi Veedhi","Polimera Veedhi","Gavarapalem Area","Branch Road","Dibba Veedhi","Harijana Wada","Vizayanagaram Road","Visakhapatnam Road Upto Koppaka Gate"],
        "Ward 81 - Anakapalli / GNT Road": ["GNT Road","Masjid Road","Kaspa Veedhi","Maternity Hospital Road","Municipal Dispensary Road","Dr Appa Rao Road","Kamakshamma Gudi Area","Market Area","Bandigadi Veedhi"],
        "Ward 82 - Anakapalli / Narasinga Rao Peta": ["Gandhi Nagar","Anjayya Colony","Income Tax Street","Post Office Street","GNT Road","Relli Veedhi","Sarada Colony","Teachers Colony","Burma Colony","Telephone Exchange Road","Chodavaram Road","Community Hall Area","Sriramnagar"],
        "Ward 83 - Anakapalli / Miriyala Colony": ["Miriyala Colony","ARC Quarters","Lakshmideepeta","Wood Peta","Sarvakamadamba Park Street","Chakala Peta","Town Girls High School Area","Goods Shed Road","Railway Station Road"],
        "Ward 84 - Anakapalli / Parawada / Koppaka": ["Mallimadugula Vari Veedhi","Pedda Veedhi Part","Malla Veedhi","GNT Road","Pedabhaskarayya Peta","Thendra Peta","Railway Station Road","Perugubazar","Fish Market Area","Vizayanagaram Road","Koppaka","KNR Peta","Sirasapalli"],
    },
    "Pendurthi Division": {
        "Ward 88 - Narava / Duvvada / Vedulla Narava": ["Narava","Kota Narava","Sattivanipalem","Gavara Jaggayyapalem","Vedulla Narava","BC Colony","Kotturu Colony","Sandhya Nagar","E-Gangavaram","Duvvada Sector-1","New Kottavanipalem","Mangalapalem","Uppara Colony Duvvada","Dockyard Colony"],
        "Ward 93 - Vepagunta / Pendurthi / Prahaladhapuram": ["Sai Durga Nagar","Durga Nagar","Krishna Nagar","Balaji Gardens","Prahaladhapuram","Simhadri Nagar","Dattasai Nagar","Virat Nagar","Maruthi Nagar","Lanka Nagar","Prahaladhapuram Colony","Port Quarters","Srinivasa Nagar","Balaji Nagar","Ganesh Nagar"],
        "Ward 94 - Vepagunta / Purushothapuram": ["Gangiredla Colony","Anjaneyulu Nagar","Venkata Sai Nagar","Banta Colony","Ravi Nagar","Prasanthi Nagar","Vidya Nagar","Gowtham Nagar","Purushothapuram","Gokuldham Colony","Simhapuri Colony Phase-II"],
        "Ward 95 - Pendurthi / Cheemalapalli": ["MES Layout","Cheemalapalli","Porlupalem","Gavara Palem","Seshadri Nagar","Varalakshmi Nagar","Santhosh Nagar","HB Colony","Gokuldham Colony","Krishnarayapuram","LIC Colony","Papayyarajupalem","Surya Nagar","NAD Layout","Ratnagiri Nagar","Satabdhi Nagar"],
        "Ward 96 - Pendurthi / Old Village": ["Pendurthi Old Village","ZP High School Area","Prasanthi Nagar","Yethapeta","Sai Nagar","Adityanagar","Port Colony","Mondibanda","Teachers Colony","BC Colony","Uppara Colony","Prakash Nagar","Relli Colony","Doggavanipalem","Sai Niranjan Colony"],
        "Ward 97 - Pendurthi / Chinamushidiwada": ["Kranthi Nagar","VUDA Colony","Railway Colony","Prasanthi Nagar","Savithri Nagar","Ambedkar Nagar","Srinivasa Nagar","Ganesh Nagar","Saradha Nagar","BRTS Road","Sapthagiri Nagar","Hamsi Nagar","Sujatha Nagar A-Zone","Sujatha Nagar B-Zone","Anjaneya Nagar","Gopala Krishna Nagar","Karmika Nagar","Chinamushidiwada","Chakalaveedhi","Durga Nagar","Gayathri Nagar","Ayyappa Nagar","Pulagalipalem"],
    },
}

# ── Derived lookups ───────────────────────────────────────────────────────────
ZONE_NAMES = list(WARD_DATA.keys())

_LOCALITY_TO_WARD: dict[str, str] = {}
for _zone_wards in WARD_DATA.values():
    for _ward_label, _localities in _zone_wards.items():
        for _loc in _localities:
            _LOCALITY_TO_WARD[_loc.strip().lower()] = _ward_label


def find_ward(locality_name: str):
    """Find ward label from a locality name."""
    return _LOCALITY_TO_WARD.get(locality_name.strip().lower())


def get_localities(ward_label: str):
    """Get all localities for a ward label."""
    for zone_wards in WARD_DATA.values():
        if ward_label in zone_wards:
            return zone_wards[ward_label]
    return []