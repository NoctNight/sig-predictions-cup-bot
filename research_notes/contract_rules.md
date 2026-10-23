# Do SIG, Polymarket and Kalshi contracts resolve the same way? (2026-10-23)

Data: `data/research_contract_rules/tc_mapping.json` (237 SIG exchanges -> race, category, PM/Kalshi legs, terms, codes).
Sources: SIG `GET /markets/{id}/nodes` on 16 markets (contract_details; the template is uniform), the SIG docs
(settlement-and-payouts) and predictionscup.com/rules; PM gamma event descriptions (117 events); Kalshi event and market
rules (114 events) plus the SENATEPARTY and GOVPARTY contract-terms PDFs.

**Verdict: no. The terms do not explain the price gaps.** All three venues pay on the same thing: the party of the
November general-election winner, runoffs included. 220 of 237 legs have the same terms. Among those same-terms legs, SIG
(21 Oct snapshot) minus PM (23 Oct) averages 4.2c in absolute size: underdogs are +4.0c rich on SIG and favourites
-2.8c cheap. That is the favourite-longshot tilt. The 17 "different" legs show the same 5c gap with no extra sign. Treat
SIG gaps as mispricing, not as a difference in what the contract pays.

**SIG terms.** Each leg is an "Election Outcome" contract: `resolutionType: Party Winner`, `raceStage: General`,
`winnerName` = party label, fed by an automatic election-data feed (raceId/stageId) and checked every 4 hours. A leg stays
*pending* until the feed has a valid result. Runoff races and the 4 chamber markets are settled manually by an admin, an
N/A outcome refunds the position, and an admin can correct a result after settlement. The 2026-11-04 17:00 UTC
`settlementDate` closes trading; it is not a deadline for the result. A race that is uncalled by then simply waits. PM
does the same, and Kalshi waits until the swearing-in. The payoff is the same, but SIG cash stays locked with no exit.

**Differences that change the payoff (17 legs, all small):**
1. **No SIG independent leg** (`no_sig_I_leg`): Idaho Sen (Achilles 2.8% on PM), SD Sen (Bengs 5%), Maine Gov (Bennett),
   Tennessee Gov (Hatley), Michigan Gov (Duggan, under 1%), Alaska Gov (Walker). If the independent wins, SIG D and R both
   settle NO. Fair SIG D + R = 1 - P(I). Price each leg against the same-party outside leg, never as 1 - the other party.
   Applying that complement to SIG R overstates it by P(I), up to 5c in South Dakota.
2. **Independent legs** (NE, MT Sen; RI Gov): SIG pays on any winner with the label, PM's leg is a generic
   "Independent", and Kalshi's leg names one person. Nebraska's SIG `winnerName` is **"Nonpartisan"**, not "Independent".
   If the feed labels Osborn differently, an admin has to override, which is a small extra risk on SIG NE-I.
   Direction: SIG I >= Kalshi named leg.
3. **Candidate-based outside markets** (Alaska Gov, California Gov; PM's Alaska Sen): PM and Kalshi KXGOV* pay per
   candidate, and the bot maps candidates to parties itself (Murkowski was mapped to R). PM resolves "Other" if there is no
   result by 31 Jul 2027. PM's Alaska Sen R side sums two Sullivan markets ("Daniel J. Sullivan Jr." is under 1%); check
   for double counting.

**Same payoff, different timing or source (tail only):** the source is a SIG feed vs PM's AP+Fox+NBC unanimity (else
certification) vs Kalshi's 4-of-8 media consensus (else the person sworn in or inaugurated; a vacancy goes to the party of
the replacement). A wrong early call on SIG can be corrected afterwards. Fusion (NY) and DFL (MN) map to D and R on all
three venues. Kalshi says DFL explicitly; SIG is assumed to.
**Chamber control:** SIG's text is close to a copy of PM's: a majority, or 50 seats plus the VP (VP is R, so 50-50 goes
R), independents who caucus count, with Speaker / pro tem as the fallback. The one SIG quirk: an independent whose caucus
is undetermined on election day counts for nobody (PM waits until 4 Jan). Kalshi falls back to the party of the Speaker /
pro tem on 1 Feb 2027. The bot derives Kalshi from the KXBALANCEPOWERCOMBO legs, but direct CONTROLS-2026 / CONTROLH-2026
markets exist with the same terms and are tighter.
**Mapping notes:** ME-02 has no PM match and Kentucky Sen has no Kalshi match. Kalshi GOVPARTYKS-27 and GOVPARTYNH-28 have
odd suffixes, but their rules say "pursuant to the 2026 election", so they are the same contract.

| Group | Races | Terms | Gap direction it could justify |
|---|---|---|---|
| No SIG I leg | ID Sen, SD Sen, ME Gov, TN Gov, MI Gov, AK Gov | different (payoff) | SIG D+R < 1 by P(I) (up to ~5c); not a same-leg gap |
| SIG I legs | NE Sen, MT Sen, RI Gov | same (NE label risk) | SIG I >= Kalshi named; NE-I small discount for label risk |
| Candidate-mapped outside | AK Gov, CA Gov, AK Sen | different (mapping) | none by rule; check bot party attribution |
| Runoff possible | GA Sen, GA Gov | same, late (1 Dec) | none (SIG scored after all resolve); cash locked |
| RCV / slow count | AK x2, ME Sen, ME-02, AZ Gov/01/06, CA-22/48, CA Gov, NV Gov, OR Gov/Sen, WA-03 | same, late | none on payoff; cash locked past 4 Nov |
| Chamber control | U.S. House, U.S. Senate | same (tail fallbacks) | none |
| Everything else | all other Senate, Governor and House legs (~190 legs) | **same** | safe to compare prices directly |

Safe to compare (terms the same, normally called on election night): every leg in tc_mapping.json with
`terms: "same"` and no `*_late_settle` code. That is 194 legs: all Senate and Governor races not listed above, plus all
House races outside AZ, CA, ME and WA.
