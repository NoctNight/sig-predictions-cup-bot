"""US state names, postal codes and URL slugs used to match SIG races to outside markets."""
STATES = {
    "AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut",
    "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa",
    "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan",
    "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire",
    "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma",
    "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee",
    "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming",
}
NAME_TO_ABBR = {v: k for k, v in STATES.items()}


def slug(name: str) -> str:
    return name.lower().replace(" ", "-")


def parse_race(name: str) -> dict:
    """SIG race name -> {office, state, abbr, district}. office in chamber|senate|governor|house|other."""
    if name == "U.S. Senate":
        return {"office": "chamber", "chamber": "senate", "state": None, "abbr": None, "district": None}
    if name == "U.S. House":
        return {"office": "chamber", "chamber": "house", "state": None, "abbr": None, "district": None}
    if name.endswith(" House race"):
        code = name[: -len(" House race")]          # e.g. AZ-01
        abbr, dist = code.split("-")
        return {"office": "house", "state": STATES.get(abbr), "abbr": abbr, "district": int(dist)}
    for office in ("Senate", "Governor"):
        if name.endswith(" " + office):
            st = name[: -len(office) - 1]
            return {"office": office.lower(), "state": st, "abbr": NAME_TO_ABBR.get(st), "district": None}
    return {"office": "other", "state": None, "abbr": None, "district": None}
