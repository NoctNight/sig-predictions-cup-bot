"""Shared loader for the FEC independent-expenditure bulk file: general-election House and Senate rows, one side per row, race name in SIG's form."""
import os
import numpy as np, pandas as pd
STATES = {"AL": "Alabama", "AK": "Alaska", "AZ": "Arizona", "AR": "Arkansas", "CA": "California", "CO": "Colorado", "CT": "Connecticut", "DE": "Delaware", "FL": "Florida", "GA": "Georgia", "HI": "Hawaii", "ID": "Idaho", "IL": "Illinois", "IN": "Indiana", "IA": "Iowa", "KS": "Kansas", "KY": "Kentucky", "LA": "Louisiana", "ME": "Maine", "MD": "Maryland", "MA": "Massachusetts", "MI": "Michigan", "MN": "Minnesota", "MS": "Mississippi", "MO": "Missouri", "MT": "Montana", "NE": "Nebraska", "NV": "Nevada", "NH": "New Hampshire", "NJ": "New Jersey", "NM": "New Mexico", "NY": "New York", "NC": "North Carolina", "ND": "North Dakota", "OH": "Ohio", "OK": "Oklahoma", "OR": "Oregon", "PA": "Pennsylvania", "RI": "Rhode Island", "SC": "South Carolina", "SD": "South Dakota", "TN": "Tennessee", "TX": "Texas", "UT": "Utah", "VT": "Vermont", "VA": "Virginia", "WA": "Washington", "WV": "West Virginia", "WI": "Wisconsin", "WY": "Wyoming"}


def load(out, races):
    F = pd.read_csv(os.path.join(out, "fec_ie.csv"), low_memory=False, dtype=str)
    F = F[F.ele_type.fillna("").str.startswith("G") & F.can_office.isin(["H", "S"])].copy()
    F["amt"] = pd.to_numeric(F.exp_amo, errors="coerce").fillna(0)
    F = F.drop_duplicates(["spe_id", "tran_id"], keep="last")
    F["rc"] = pd.to_datetime(F.receipt_dat, format="%d-%b-%y", errors="coerce")
    pty = F.cand_pty_aff.fillna("").str.upper()
    dem, rep = pty.str.contains("DEMOCRAT"), pty.str.contains("REPUBLICAN")
    sup, opp = F.sup_opp == "S", F.sup_opp == "O"
    F["side"] = np.where((dem & sup) | (rep & opp), "D", np.where((rep & sup) | (dem & opp), "R", ""))
    F = F[(F.side != "") & (F.amt <= 5e6)]       # the file holds junk filings (a "TANKING" filer at $12bn)
    dis = pd.to_numeric(F.can_office_dis, errors="coerce").fillna(0).astype(int)
    F["race"] = np.where(F.can_office == "H", F.can_office_state + "-" + dis.map("{:02d}".format) + " House race", F.can_office_state.map(STATES) + " Senate")
    return F[F.race.isin(races)]
