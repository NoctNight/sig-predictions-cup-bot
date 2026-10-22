# Market Making on Prediction Markets and Binary Contracts (order-book and AMM venues)

Scope note: binary contracts bounded in [0,1], paying 1 or 0 at a known resolution date, traded on Kalshi, Polymarket CLOB, PredictIt, Betfair Exchange, or via LMSR-style AMMs. Primary sources read in full or near-full: Bürgi–Deng–Whelan (Kalshi), Whelan (Betfair), Page–Clemen (Intrade, EJ 2013), Aktuğ–Torul (nine-venue 2024 election), Saguillo et al. (Polymarket arbitrage, AFT 2025), Cheng–Yang–Zou (Polymarket NBA order books), Feil–Nendel (optimal MM in prediction markets), Xi–Moallemi–Pai–Wang (Kalshi volatility), Moallemi–Robinson–Zhu (uniform-loss AMM), Dalen (logit jump-diffusion "handbook"), Taleb (election martingales), Le (Kalshi/Polymarket calibration), Archak–Ipeirotis (volatility model), plus Othman–Sandholm LS-LMSR and Polymarket/Kalshi venue documentation. All numbers below are from the cited source unless labelled as my own derivation.

---

## Key Question 1: What is special/risky about market making binary contracts (bounded prices, jump-to-resolution, p(1-p) and time-dependent volatility, correlated/complementary contracts)?

### Takeaway
A binary contract's price is a probability, so its instantaneous volatility is structurally proportional to p(1-p) and rises toward resolution (a "deadline clock" of order p(1-p)/τ in the best-fitting Kalshi model), with discrete jumps dominating sports/news-driven markets; inventory held into settlement carries a lumpy variance of q²·p(1-p) that cannot be hedged in the contract itself, and YES/NO (and multi-outcome) complements are the natural hedge but are frequently mispriced by cents on Polymarket.

### Cited Findings

**Bounded price / probability-space dynamics**
- Feil & Nendel (July 2026) model the price as p_t = f(L_t) with L a latent belief diffusion and f logistic; the martingale requirement fixes the drift, and the price SDE becomes dp = p(1−p)·σ(t, logit p)·dW — i.e. price volatility "scales with p(1−p), so it vanishes as p approaches 0 or 1" — [arXiv:2607.17991](https://arxiv.org/abs/2607.17991)
- Taleb (Quantitative Finance 2018 / arXiv 2017): a binary forecast must be a martingale bounded in [0,1]; the bounded dual process has dY = s(Y)dW with s(Y) = (σ/√π)·exp(−erf⁻¹(2Y−1)²), which the paper notes is approximated by a constant times Y(1−Y). Binary value B(Y₀,σ,t₀,T) = ½ erfc[(l − erf⁻¹(2Y₀−1)e^{σ²(T−t₀)}) / √(e^{2σ²(T−t₀)}−1)]. The paper argues that as uncertainty about the final outcome rises "the arbitrage value of the binary gets closer to 50%" and the forecast "should not undergo large changes even if polls … show significant variations" — [arXiv:1703.06351](https://arxiv.org/abs/1703.06351) (full text via [ar5iv](https://ar5iv.labs.arxiv.org/html/1703.06351))
- Archak & Ipeirotis (2008, NYU Stern): modelling each side's "ability" as an Itô diffusion gives contract price π(t) = N[(S(t)+μ(T−t))/(σ√(T−t))] and instantaneous price volatility V(t) = σ/√(T−t) · φ(N⁻¹(π(t))), which "depends only on the current price of the contract and time to expiration"; volatility is highest near 0.5, →0 at 0/1, and rises as expiration approaches; the authors report that InTrade realised volatility "fits well the current model" (no numeric fit statistics in the accessible summary) — [NYU archive 2451/27716](https://archive.nyu.edu/handle/2451/27716); formulas via [Ipeirotis blog](https://www.behind-the-enemy-lines.com/2008/10/modeling-volatility-in-prediction_20.html)
- Moallemi, Robinson & Zhu (July 2026) formalise "win-martingales" (processes converging to 0 or 1 at a fixed resolution time T) as dP = G(P)/h(t) dW with a volatility profile G and an information clock h. Canonical profiles: G(p)=1 (absorbed BM), G(p)=√(p(1−p)) (Wright–Fisher/Jacobi), G(p)=p(1−p) (log-odds/logistic: "evidence adds up in log-odds space"), G(p)=φ(Φ⁻¹(p)) with h(t)=√(T−t) (Gaussian score dynamics). Clocks: h(t)=√(T−t); Brody–Hughston–Macrina h(t)=(T−t)/(σT) which gives dP = (σT/(T−t))·P(1−P)dW, i.e. volatility ∝ 1/(T−t); the power-law family h=c(T−t)^α needs α ≥ 1/2 so that the information clock diverges at resolution — [arXiv:2607.17428](https://arxiv.org/abs/2607.17428)

**Empirical volatility: level- and deadline-dependence on Kalshi**
- Xi, Moallemi, Pai & Wang (July 2026, v3 Oct 2026), hourly data on 41,348 Kalshi contracts Aug 2021–Apr 2026 (~6.75M contract-hours out of sample; Sports = 39.8% of contract-hours and 76.8% of volume, Politics 14.2%/9.7%): one-step conditional variance h² = [p(1−p)/τ + K·ν(V)·s²/4]·Δ, where the first term is a Wright–Fisher "deadline-resolution" clock (p = mid, τ = time to resolution) and the second a Glosten–Milgrom adverse-selection term (s = bid–ask spread, V = hourly volume, exact one-event posterior variance (s²/4)[1−α²(2p−1)²]). Realised volatility "peaks near 50 cents and falls toward the boundaries, and rises sharply in the final hours before resolution"; beyond about one day from resolution volatility is roughly flat, so a pure 1/τ clock over-predicts decline far from expiry. Structural model beats GARCH(1,1) by ~15% on volume-weighted interval score (0.736→0.623) and ~2.1× per-hour likelihood; GARCH×structural is ~20% better than plain GARCH with 95.1% coverage. Sports is jump-like: ~47.8% of Sports volume occurs in "large-move hours"; Economics contracts follow smoother deadline-resolution dynamics — [arXiv:2607.08199](https://arxiv.org/abs/2607.08199)
- The same paper finds spreads and volume are informative drivers of volatility: volume's max/min bin ratio of conditional variance is 7.5×, spread's is 2.2× — [arXiv:2607.08199](https://arxiv.org/abs/2607.08199)

**Jump risk and inventory at resolution**
- Feil & Nendel penalise open inventory at settlement with Φ(p,q) = −γ_T·q²·p(1−p) — the conditional variance of the settlement payoff qY — in addition to the running mark-to-market penalty γq²ς²; near settlement "spreads widen for prices near 1/2, where settlement risk is highest" — [arXiv:2607.17991](https://arxiv.org/abs/2607.17991)
- Aktuğ & Torul (2026): Polymarket's 2024 Trump-victory price changed in only 1.1% of 5-minute bins (Betfair 5.4%, sportsbooks 0.1–0.4%), and every series shows "very high excess kurtosis" because price changes concentrate "in short bursts around news". The July 13 assassination attempt moved Trump's probability ~10 points within 48 hours (about 11.5 pp over the weekend CME closure alone); the June 27 debate started a slide from ~60%. — [Aktuğ & Torul, pridis.pdf](https://web.bogazici.edu.tr/torul/pridis.pdf)
- Moshrefi (July 2026), 23M Kalshi sports moneyline trades: calibration is near-perfect mid-life but "departs sharply near expiry"; in the final ten minutes the calibration curve becomes step-like (Prelec curvature "well above one"), attributed to insurance demand from traders holding losing positions — [arXiv:2607.14430](https://arxiv.org/abs/2607.14430)

**Complementary and correlated contracts**
- Saguillo, Ghafouri, Kiffer & Suarez-Tangil (AFT 2025), Polymarket Apr 2024–Apr 2025 (8,659 single-condition markets + 1,578 NegRisk multi-outcome markets = 17,218 conditions; ~86M on-chain bids): 7,051 of 17.2K conditions had at least one YES+NO < $1 opportunity (threshold ≥ $0.05 profit per dollar, markets below 95% probability); all single-condition opportunities were on the long side (sum below $1). Within NegRisk markets, 662 of 1,578 had opportunities, averaging ~100 per market, with average maximum profit ≈ 40 cents per dollar (long and short). Cross-market (combinatorial) arbitrage: 13 logically dependent pairs identified in the 2024 US election, pair 4 alone had 6,630 opportunities, mostly in low-liquidity periods, average max profit ≈ $100. Realised extraction ≈ $39.6M over the year: $5.9M buying full sets below $1, $4.7M selling above $1, $17.3M "buy NO" and $11.1M "buy YES" within-market rebalancing; top account ≈ $2.01M across 4,049 transactions — [arXiv:2508.03474](https://arxiv.org/abs/2508.03474)
- Cheng, Yang & Zou (Apr 2026), 75M+ Polymarket order-book snapshots over 173 NBA games: only 7 executable single-market (YES+NO) arbitrage episodes, median duration 3.6 s; 290 combinatorial episodes (e.g. moneyline vs spread/total), concentrated in the final minutes of live play, median return 101 bp, 76.9% constrained by order-book depth with average executable size 14.8 shares — [arXiv:2605.00864](https://arxiv.org/abs/2605.00864)
- Polymarket's NegRisk design groups mutually exclusive markets so exactly one resolves YES, and YES prices "should equal approximately $1.00" in aggregate — [Polymarket docs](https://docs.polymarket.com/advanced/neg-risk.md)
- Dalen (Oct 2025) gives a cross-event hedge ratio β ≈ (S′_i/S′_j)·ρ_ij (S′ = p(1−p) delta of each contract), shrunk by α ∈ [0.5,1), and an inventory cap |q| ≤ q_max ∝ 1/max{S′(x), ε} that tightens near the boundaries — [arXiv:2510.15205](https://arxiv.org/abs/2510.15205)

**Fees scale with p(1-p) too**
- Kalshi taker fee = 0.07·C·P·(1−P) (rounded up to the cent), maker fee = 0.0175·C·P·(1−P) on series that charge it; max at 50c = 1.75c/contract taker, 0.63c at 10c or 90c — [Kalshi fee schedule via search summary](https://kalshi.com/docs/kalshi-fee-schedule.pdf); corroborated by Bürgi–Deng–Whelan who note the round-up makes the effective taker fee at 50c 1.77% of price and that pre-April-2025 Kalshi charged takers only — [Kalshi paper](https://www.karlwhelan.com/Papers/Kalshi.pdf)
- Betfair charges commission on net winnings, "typically 2%" in recent years — [Whelan, Betfair](https://www.karlwhelan.com/Papers/Betfair.pdf)

### Inferences
- Versus equity/crypto MM, the maker's variance budget is not stationary: it has a deterministic component that *grows* as resolution nears (p(1−p)/τ) and collapses to a one-shot Bernoulli variance q²p(1−p) at settlement. A maker who sizes on trailing realised vol will be systematically under-hedged in the final day/hours.
- Because YES and NO are the same contract, the only true hedge for inventory is the complement (or a logically linked market); the Saguillo data show those complements are frequently off by ≥5c, so "hedging" through the complement is itself a source of edge or slippage.
- Sports/news markets (jump-like, 47.8% of Sports volume in large-move hours) call for quoting regimes closer to an options dealer around earnings than to a continuous diffusion model.

### Gaps
- No source gives a measured jump-size distribution (e.g., 95th percentile hourly move) by price bucket for Kalshi or Polymarket; Xi et al. report model scores, not raw tail statistics.
- I found no paper that empirically estimates the correlation matrix across logically linked election markets (e.g. state vs national) for hedging purposes; the combinatorial-MM literature (below) is theoretical.

---

## Key Question 2: Empirical studies of liquidity, spreads, depth, overround, arbitrage persistence on Polymarket / Kalshi / Betfair / PredictIt

### Takeaway
Empirical microstructure work is thin but growing: Betfair's liquid soccer markets run at ~101% back / ~99% lay overround (≈1–2% round-trip), Polymarket shows thousands of cent-scale complementary mispricings persisting hours in illiquid periods but only seconds in liquid in-play NBA books, and cross-venue gaps on the identical 2024 Trump contract averaged 6.2 percentage points; in all venues makers earn structurally higher returns than takers (Kalshi: −9.6% vs −31.5% average post-fee).

### Cited Findings

**Betfair (order-book exchange, full book at 1-second resolution)**
- Whelan (Jan 2026; CEPR DP 20633): >200,000 soccer matches 2022–2024 with "the full order book at one-second intervals" and every matched trade, maker/taker side identified. Typical back overround ≈ 100.7–101% and lay overround ≈ 99% on high-profile games, implying takers of back offers lose ≈0.7–1% and makers of lay offers earn ≈1% pre-commission; commission ≈ 2% of net winnings. Pre-match and early in-play, makers are near break-even on most bets while takers' losses rise nonlinearly as win probability falls; late in-play "longshot bets generate large, systematic losses even for liquidity providers". In the calibrated model, spreads "rise faster than 1/q as q falls" (the proportional disadvantage for takers ≈ q/(q+δ) with a constant belief gap δ) — [Whelan, Agreeing to Disagree](https://www.karlwhelan.com/Papers/Betfair.pdf)
- Whelan reports no favourite–longshot bias in Betfair average odds across 200k+ matches, consistent with Smith, Paton & Vaughan Williams (2006, Economica) who found no FLB in Betfair odds for 799 races, unlike bookmaker odds — [Whelan, Betfair](https://www.karlwhelan.com/Papers/Betfair.pdf)
- Aktuğ & Torul: Betfair's Trump price changed in 5.4% of 5-minute bins; Betfair led price discovery at the 1-hour horizon while Polymarket led at 5 minutes; Betfair + Polymarket jointly ≈ 85% of Hasbrouck information share, no sportsbook >11% — [Aktuğ & Torul](https://web.bogazici.edu.tr/torul/pridis.pdf)

**Kalshi**
- Bürgi, Deng & Whelan (Jan 2026; CEPR DP 20631): 313,972 Yes/No contract-prices from 46,282 Yes contracts in 12,403 events (open ≥24h, final spread ≤20c filter), 156,986 daily price observations up to 10 days before close. Mean absolute error falls smoothly each day toward close with a steep drop on the last day. Average post-fee return: makers −9.64%, takers −31.46% (F-test rejects equality). Contracts ≤10c lose >60% of invested money; a 5c contract winning 3% has −40% pre-fee return; a 95c contract winning ~98% returns +3.1% pre-fee; contracts >70c show small, statistically significant positive post-fee returns. Average pre-fee return across all contracts −20% (volume-weighted zero by construction). Makers buy 43.5% of 1c–10c contracts (46,185 of 106,209) but are more likely to buy higher-priced ones. Calibrated model: disagreement width δ = 0.107, matching probability θ = 0.60, probability-misperception parameter in 0.06–0.12 (chosen 0.09, e.g. true 20% perceived ≈23%); "bid–ask spreads are larger as a fraction of price for cheap contracts" — [Kalshi paper](https://www.karlwhelan.com/Papers/Kalshi.pdf); summary at [VoxEU](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)
- Le (Feb 2026): Kalshi 64.7M trades on 210,608 binary contracts; Polymarket 227.6M trades on ~116k resolved contracts (cutoff 31 Dec 2025). Politics has the highest median trades per market on Kalshi (127); Polymarket has ≈8× higher median per-market volume overall and ≈11× in Politics; on Kalshi, trades >10,000 contracts are 0.15% of trades but ≈15% of volume — [arXiv:2602.19520](https://arxiv.org/abs/2602.19520)
- Xi et al.: Kalshi hourly spread is a significant volatility driver (2.2× max/min variance ratio across spread bins) — [arXiv:2607.08199](https://arxiv.org/abs/2607.08199)

**Polymarket**
- Saguillo et al.: see KQ1 — 7,051/17,218 conditions with YES+NO<$1 at ≥5c edge; ~40c/$ average max profit in NegRisk sets; $39.6M extracted in a year; cross-market opportunities "mostly occurred during low-liquidity periods"; prices carried forward up to ~2.5 h, matched bids grouped in ~1-h windows (75% of bids fell within) — [arXiv:2508.03474](https://arxiv.org/abs/2508.03474)
- Cheng, Yang & Zou: in liquid in-game NBA books, single-market mispricing is "rare" (7 episodes/173 games, median life 3.6 s); combinatorial edges median 101 bp but 76.9% depth-constrained at ~14.8 shares — [arXiv:2605.00864](https://arxiv.org/abs/2605.00864)
- Aktuğ & Torul: same Trump contract traded at "an average cross-venue spread of 6.2 percentage points" across nine venues Jan–Nov 2024 (limits to arbitrage: jurisdiction, locked capital, settlement lag); Polymarket election volume $3.6B — [Aktuğ & Torul](https://web.bogazici.edu.tr/torul/pridis.pdf)
- Matt Levine (Bloomberg, 9 Oct 2024) reported a Trump contract buyable ≈$0.49 on Kalshi and sellable ≈$0.53 on Polymarket, a ~4c cross-venue gap — [Bloomberg](https://www.bloomberg.com/opinion/articles/2024-10-09/a-trump-trade-arbitrage)
- Ng, Peng, Tao & Zhou (SSRN, rev. Apr 2026): across Polymarket, Kalshi, PredictIt, Robinhood before the 2024 election, Polymarket leads Kalshi in 30-minute return VARs, the lead concentrates in high-liquidity periods, and large-trade order imbalance predicts subsequent returns; "significant price disparities across platforms" — [SSRN 5331995](https://papers.ssrn.com/abstract=5331995)
- Clinton & Huang (Vanderbilt WP 2025, "Prediction Markets? The Accuracy and Efficiency of $2.4 Billion in the 2024 Presidential Election"): every political contract on PredictIt, Polymarket, Kalshi, IEM in the final five weeks; daily moves on Polymarket were largely unrelated to those on other platforms and often reversed after spikes; cross-platform inefficiency "increased in the final two weeks"; press-reported accuracy PredictIt 93%, Kalshi 78%, Polymarket 67% (secondary reporting only; Kalshi disputes methodology) — [DL News](https://www.dlnews.com/articles/markets/polymarket-kalshi-prediction-markets-not-so-reliable-says-study), [PredictStreet summary](https://investor.wedbush.com/wedbush/article/predictstreet-2026-1-14-the-accuracy-paradox-new-vanderbilt-study-shines-a-light-on-the-reliability-of-prediction-markets)

**Intrade / PredictIt**
- Page & Clemen (EJ 2013): 1,787 Intrade markets, 512,612 transactions (markets lasting >1 month). Calibration is close to the diagonal for prices ≤100 days from expiry and strongly longshot-biased >100 days; political markets "show a very strong longshot bias" vs slight miscalibration elsewhere; parametric Lattimore-type fit p̂ = d·p^c/(d·p^c+(1−p)^c) has c significantly ≠1 (curvature) and c decreasing in time-to-expiration (negative, significant coefficient) — [Page & Clemen PDF](https://people.duke.edu/~clemen/bio/Published%20Papers/45.PredictionMarkets-Page&Clemen-EJ-2013.pdf)
- No peer-reviewed PredictIt order-book/spread study surfaced (PredictIt's $850 position cap is often cited as the reason its prices stayed better aligned in 2024, but only in commentary) — [PredictStreet](https://investor.wedbush.com/wedbush/article/predictstreet-2026-1-16-the-liquidity-paradox-why-predictits-850-limit-beat-polymarkets-24-billion-in-2024)

### Inferences
- "Typical spread" numbers are venue- and regime-specific: Betfair's liquid soccer markets price a ~1% overround per side; Kalshi's own sample filter (drop final spread >20c) implies spreads of 10–20c are common enough to need filtering; Polymarket complementary gaps ≥5c are pervasive in long-dated/illiquid markets but close in seconds in liquid live-sports books.
- Dutch-book gaps are overwhelmingly on the *long* side (full set < $1), i.e. the book is usually too wide/cheap rather than overround on Polymarket — the opposite of a bookmaker's overround and consistent with makers refusing to quote tightly in thin markets.
- The persistent 4–6 pp cross-venue wedge in 2024 shows that "fair value" for an MM on one venue is not pinned by the other venue; the wedge is a limits-to-arbitrage rent, not an inefficiency a single-venue maker can collect.

### Gaps
- No published distribution of Kalshi or Polymarket bid–ask spreads by price bucket and time-to-resolution (only filters, proxies and driver ratios). Allium documents a Kalshi order-book snapshot table that could support one — [Allium docs](https://docs.allium.so/historical-data/predictions/kalshi/market-orderbook)
- Depth statistics (shares within 1c/5c of mid) exist only in the NBA study (14.8-share executable size on arbitrage legs).
- The Clinton–Huang working paper itself could not be retrieved; accuracy percentages are press-reported.

---

## Key Question 3: Practical strategies / bot designs: sizing, laddering, skewing, hedging across complementary markets; what is known about mispricing between complementary contracts

### Takeaway
Rigorous guidance comes from three places: Feil–Nendel's stochastic-control solution (quote wider near p=0.5 and near settlement, skew against inventory with skew shrinking as p→0/1), Dalen's Avellaneda–Stoikov-in-logit-space handbook (spread in probability ≈ p(1−p)·logit spread, inventory cap ∝ 1/(p(1−p)), cross-event hedge β ≈ ratio of deltas × ρ), and venue reward programs (Polymarket pays quadratic-in-distance rewards and requires two-sided quotes when mid <0.10 or >0.90); the complementary-contract literature shows large, exploitable but liquidity-constrained gaps.

### Cited Findings

**Optimal quoting (stochastic control)**
- Feil & Nendel: HJB in belief space with running penalty γq²ς² and terminal penalty γ_T q² p(1−p); quotes have no closed form (π* = (Λ)⁻¹(−∂_z H/Δ)). Numerics (T=1, σ(t,x)=σ₀+σ₁(t/T)^η+σ₂/(1+x²) with σ₀=0.6, σ₁=0.3, σ₂=0.1, η=3; arrival A(t,p)=(A₀+A₁(e^{ξt/T}−1)/(e^ξ−1))·√(p(1−p)) with A₀=100, A₁=150, ξ=2; fill decay k(t) rising from 35 to 50; γ=4·10⁻³, γ_T=10⁻³, Q=100, Δ=10): (i) at fixed time, spreads are larger near p=1/2 than near the boundaries; (ii) spreads shrink over time as k(t) rises, but widen near settlement for p near 1/2; (iii) skew is zero at q=0 at p=1/2, positive for p<1/2 and negative for p>1/2 even with no inventory (asymmetric fill intensities), decreasing in inventory (short → quote up, long → quote down), converging to zero near 0/1 because p(1−p)→0; (iv) vs a myopic maximiser over 10,000 paths from p₀=0.5: mean PnL 12.39 vs 12.47, PnL std 10.34 vs 28.11 (−63%), mean |q_T| 15.23 vs 49.37 (−69%), 5% ES 9.68 vs 40.50 (−76%) — [arXiv:2607.17991](https://arxiv.org/html/2607.17991v1)

**Handbook-style rules (logit-space Avellaneda–Stoikov)**
- Dalen: reservation logit r_x = x_t − q_t·γ·σ_b²·(T−t); total logit spread 2δ_x ≈ γσ_b²(T−t) + (2/k)·ln(1+γ/k); displayed probability half-spread δ_p ≈ p(1−p)·δ_x "so quoted spreads compress near 0 and 1" with a tick-based floor recommended; inventory cap |q| ≤ q_max ∝ 1/max{S′(x),ε}; cross-event hedge β ≈ (S′_i/S′_j)ρ_ij shrunk by α∈[0.5,1); no quote-size rule given — [arXiv:2510.15205](https://arxiv.org/html/2510.15205v2) (single-author preprint; calibration results are on synthetic paths — treat as a framework, not evidence)

**Venue incentive structures that shape ladder design**
- Polymarket liquidity rewards: order score S(v,s) = ((v−s)/v)²·b where v = max spread from mid (cents, set per market, e.g. 3c), s = order's distance from the size-cutoff-adjusted midpoint; Q_one/Q_two sum S×size over bids on m plus asks on complement m′ (and vice versa); for mid ∈ [0.10,0.90], Q_min = max(min(Q_one,Q_two), max(Q_one,Q_two)/c) with c = 3.0, so one-sided liquidity earns at 1/3 rate; for mid <0.10 or >0.90, Q_min = min(Q_one,Q_two) — must be two-sided; book sampled once per minute at a random offset, epoch = 1 UTC day (≤1,440 samples), each market sets a min qualifying size (API example rewards_min_size: 100), min payout $1 — [Polymarket docs](https://docs.polymarket.com/developers/market-makers/liquidity-rewards)
- Kalshi: taker fee 0.07·P(1−P) per contract (1.75c at 50c, 0.63c at 10c/90c); maker fee 0.0175·P(1−P) where charged; before July 2025 maker fee was a flat 0.25c/contract on some series — [search summary of fee schedule](https://kalshi.com/docs/kalshi-fee-schedule.pdf), [Whirligigbear](https://whirligigbear.substack.com/p/makertaker-math-on-kalshi), [InGame](https://www.ingame.com/kalshis-change-may-increase-fee-sports-traders/)

**Maker vs taker economics (why quoting pays, where it doesn't)**
- Kalshi makers −9.64% vs takers −31.46% average post-fee; the gap is widest on cheap contracts; makers' losses on ≤10c contracts are still significantly negative on 5 of 6 pre-close days — [Kalshi paper](https://www.karlwhelan.com/Papers/Kalshi.pdf)
- Betfair: makers near break-even pre-match; "profits emerge for those who accept offers on favorites" late in-play and longshot lays lose even for makers — [Whelan, Betfair](https://www.karlwhelan.com/Papers/Betfair.pdf)
- Whelan's model: makers "earn their highest returns when betting on longshots, where even a modest mispricing on the other side gives them a large proportional edge", because spreads rise faster than 1/q as q falls — [Whelan, Betfair](https://www.karlwhelan.com/Papers/Betfair.pdf)

**Complementary / linked-market arbitrage magnitudes**
- Polymarket (Saguillo et al.): 7,051 conditions with YES+NO<$1 at ≥5c; NegRisk sets with avg max profit ≈40c/$; 13 dependent election pairs, 6,630 opportunities in one pair, avg max profit ≈$100 (low liquidity); $39.6M realised; one account captured $58,983 in a single trade buying YES and NO each under $0.02 — [arXiv:2508.03474](https://arxiv.org/abs/2508.03474)
- Polymarket NBA (Cheng et al.): single-market 7 episodes / 3.6 s median; combinatorial 290 episodes, median 101 bp, 14.8-share executable size — [arXiv:2605.00864](https://arxiv.org/abs/2605.00864)
- Kalshi parlays priced above the product of leg prices, overpricing growing with leg count and persisting even where legs are perfectly calibrated — [arXiv:2607.14430](https://arxiv.org/abs/2607.14430)
- Fees can absorb small gaps: a practitioner guide notes a 3c apparent arbitrage "fully consumed by fees (~2.2c)" (secondary) — [NexusFi](https://nexusfi.com/a/prediction-markets/arbitrage-prediction-markets)

**Combinatorial / linked-market automated makers**
- Dudík, Lahaie & Pennock (EC 2012) build a tractable market maker for logically related securities via convex optimisation and constraint generation, "passing some information among related securities" without full combinatorial pricing; deployed in the 2012 PredictWise/Microsoft election market — [Princeton CS summary](https://www.cs.princeton.edu/node/24815), [Microsoft Research](https://www.microsoft.com/en-us/research/?p=190217)
- Kroer, Dudík, Lahaie & Balakrishnan (2016): arbitrage-free pricing of integer-program-specified combinatorial markets bounds the maker's subsidy but is #P-hard in the worst case; they use Frank–Wolfe with an IP oracle to compute the Bregman projection — [arXiv:1606.02825](https://www.arxiv.org/pdf/1606.02825)

**Practitioner material (secondary only)**
- Open-source Polymarket bots list Avellaneda–Stoikov with inventory management and Kelly sizing (README claims, no performance evidence); one Substack live-bot log shows 104 trades at 32.69% win rate for a directional (not MM) strategy — [polypigeon README mirror](https://backiee.wasmer.app/https_github_com/CMLKevin/polypigeon), [falsifylab](https://falsifylab.substack.com/p/strategy-deep-dive-polymarket)

### Inferences
- A ladder design consistent with both theory and incentives: (a) widest quotes and smallest size at p≈0.5 and in the final hours (variance ∝ p(1−p)/τ); (b) always two-sided when mid <0.10 or >0.90 (Polymarket rule) but with inventory caps scaling like 1/(p(1−p)) because per-contract P&L on a wrong-way resolution is 1−p or p; (c) express skew in logit space so it automatically fades at the extremes; (d) treat Kalshi's fee as a p(1−p)-shaped floor on the half-spread (≈0.9c at 50c for a taker, ≈0.2c for a maker).
- The Kalshi maker-loss evidence on ≤10c contracts implies that posting bids on longshots is where makers themselves get adversely selected (takers' overoptimism does not fully protect makers); quoting bids below ~10c should be sparing or only as a hedge leg.
- Dutch-book gaps are an inventory-management channel rather than a steady income: they are large only when the book is thin (which is when a maker's own fills are least informative), and they close in seconds in liquid books.

### Gaps
- No peer-reviewed back-test of a live prediction-market MM strategy with reported Sharpe/turnover; the only PnL numbers are Feil–Nendel's simulation.
- No source quantifies how often PredictIt or Kalshi YES/NO complements violate the sum-to-1 bound (Polymarket only).
- Polymarket's per-market max spread/min size parameters are not published in aggregate; only the 3c example and c=3.0 appear in docs.

---

## Key Question 4: LMSR / liquidity-sensitive AMMs (Othman–Sandholm) and implications for depth allocation on an order book

### Takeaway
LMSR has fixed, price-independent depth set by b (worst-case loss b·ln n), which Othman–Sandholm showed cannot coexist with path independence and translation invariance once liquidity is made volume-sensitive (LS-LMSR: b(q)=α·Σq_i, spread ≈ α-controlled); the 2026 "uniform-loss" theory shows the right depth profile depends on the assumed volatility profile G(p): constant-product (V=2√(p(1−p))) is loss-uniform only under log-odds dynamics, LMSR (V=entropy) under G=√(p(1−p)H(p)), and liquidity should be withdrawn over time roughly like √((T−t)/T) to hold a constant loss rate.

### Cited Findings
- Othman, Sandholm, Pennock & Reeves (EC 2010; ACM TEAC 2013): LS-LMSR replaces LMSR's constant b with b(q) = α·Σ_i q_i, giving cost C(q) = b(q)·ln Σ e^{q_i/b(q)}; loss is bounded for any initial liquidity and worst-case loss → 0 as initial liquidity → 0, and the maker "can run at a profit"; no market maker can simultaneously satisfy path independence, translation invariance and liquidity sensitivity; LMSR satisfies the first two but not liquidity sensitivity — [CMU theory-lunch abstract](https://www.cs.cmu.edu/~./theorylunch/pastSemesterIndices/20100922.html), [Othman–Sandholm WINE 2011 homogeneous risk measures](https://kilthub.cmu.edu/articles/journal_contribution/Liquidity-Sensitive_Automated_Market_Makers_via_Homogeneous_Risk_Measures/6606998)
- Li & Wortman Vaughan (EC 2013): axiomatic characterisation of adaptive-liquidity makers combining Abernethy et al. and Othman–Sandholm; "the curvature of the price space can be used to manage a trade-off between information loss and profit" — [paper PDF](https://www.jennwv.com/papers/liquidity.pdf), [Microsoft Research](https://www.microsoft.com/en-us/research/?p=165874)
- Moallemi, Robinson & Zhu: define uniform AMMs ("instantaneous LVR proportional to pool value and independent of current price"); for a win-martingale dP=G(P)/h(t)dW the uniform pricing function solves G(p)²V″+βV=0 with V(0)=V(1)=0. Pairs: absorbed BM ↔ V=sin(πp), β=π²; Wright–Fisher ↔ V=p(1−p), β=2; logistic/log-odds ↔ V=2√(p(1−p)) (the constant-product maker), β=1/4; Gaussian score ↔ V=φ(Φ⁻¹(p)), β=1; LMSR (V=L·H(p)) is uniform only under dP=√(P(1−P)H(P))/h dW. Using CPMM under Gaussian-score dynamics makes relative LVR blow up near 0 and 1, while the matched "pm-AMM" keeps it at 1/(2h(t)²) everywhere. Dynamic result (Thm 14): to hit a target expected cumulative loss D(t), set L_t = (2w₀/(βV(p₀)))·h(t)²·exp(∫₀ᵗ β/(2h²))·D′(t); on the √(T−t) clock a constant loss rate gives L_t ∝ √((T−t)/T) and a linear target gives L_t ∝ (T−t)^{1−β/2}; liquidity is withdrawal-only when that expression is non-increasing — [arXiv:2607.17428](https://arxiv.org/html/2607.17428v1)
- Feil & Nendel link log-odds to LMSR: under Hanson's LMSR, log-odds are proportional to net outstanding shares, which motivates the logistic map f in their model — [arXiv:2607.17991](https://arxiv.org/abs/2607.17991)
- Polymarket itself launched in 2020 on an LMSR-style constant-function AMM and migrated to a hybrid CLOB over 2022–2023; 2024 election prices were "set by human limit orders" — [Aktuğ & Torul](https://web.bogazici.edu.tr/torul/pridis.pdf)

### Inferences
- An order-book maker can read the uniform-loss result as a depth-allocation rule: if you believe news arrives additively in log-odds (G=p(1−p)), quote depth across price levels proportional to the CPMM profile 2√(p(1−p)) — i.e. shallower (fewer shares per cent) near 0.5 and relatively deeper near the tails in *share* terms, which is the opposite of what a naive "cents are cents" ladder does. If you believe in Wright–Fisher dynamics, depth ∝ p(1−p) instead.
- The time schedule L_t ∝ √((T−t)/T) (constant loss rate) is a clean default for shrinking total resting size toward resolution; combined with the Kalshi finding that volatility is flat beyond ~1 day, a maker can hold size roughly constant until the final day and then taper on a √(T−t) schedule.
- LS-LMSR's α is the AMM analogue of a maker's spread/size-vs-volume rule: spread widens when little has traded and tightens with volume, consistent with Whelan's thick/thin equilibria where spreads rise as match rates fall.

### Gaps
- I could not open the full Othman et al. PDF to transcribe the LS-LMSR bid–ask spread bound in terms of α and n (the summary states only that the spread is controlled by α); the EC/TEAC paper should be consulted for the exact expression.
- No empirical comparison of LMSR/CPMM loss vs order-book maker loss on the same prediction-market data.

---

## Key Question 5: Favorite–longshot bias (direction and magnitude) and what it implies for quoting

### Takeaway
Across Kalshi, Polymarket (politics), Intrade and racetracks, longshots are overpriced and favourites underpriced, worsening with time to resolution: on Kalshi a 5c contract wins ≈2–3% (−40% to −60% return), >70c contracts earn small positive returns, and logistic recalibration slopes rise from 0.99 within an hour of resolution to 1.32 beyond a month (1.73–1.74 for politics/sports); Betfair soccer odds, by contrast, show no bias on average.

### Cited Findings
- Kalshi: contracts ≤10c lose >60% of money; 5c contracts win ≈2–3% (−40% pre-fee); 95c wins ≈98% (+3.1%); >70c significant positive post-fee returns; average pre-fee return −20%; bias stronger for takers than makers — [Kalshi paper](https://www.karlwhelan.com/Papers/Kalshi.pdf), [VoxEU](https://cepr.org/voxeu/columns/economics-kalshi-prediction-market)
- Le (2026): universal recalibration slope 0.99 (<1h to resolution) → 1.32 (>1 month); Politics 0.93–1.83 by horizon (1.73 at >1 month), Sports 0.90–1.10 within 48h then 1.74 beyond a month, Crypto ≈1 rising to 1.36, Weather overconfident short-term (0.69–0.97); Polymarket politics mean slope 1.31 vs Kalshi 1.64; illustrative: a 70c contract one month out ≈75% true, a 70c political contract one week out ≈83% — [arXiv:2602.19520](https://arxiv.org/html/2602.19520v1)
- Page & Clemen (2013): bias toward 0.5 grows with time to expiration, explained by traders discounting delayed payouts (b = 1.1–1.2 in simulations); strong in political markets, slight elsewhere; well calibrated ≤100 days — [Page & Clemen](https://people.duke.edu/~clemen/bio/Published%20Papers/45.PredictionMarkets-Page&Clemen-EJ-2013.pdf)
- Snowberg & Wolfers (JPE 2010): racetrack favourite–longshot bias is better explained by probability misperception (prospect-theory weighting) than risk-love, using win/exacta/quinella/trifecta comparisons — [NBER w15923](https://www.nber.org/system/files/working_papers/w15923/w15923.pdf)
- Betfair soccer: no favourite–longshot bias in average odds (200k+ matches), consistent with Smith, Paton & Vaughan Williams (2006) on 799 races; but takers' losses rise nonlinearly as win probability falls, and late in-play longshots lose even for makers — [Whelan, Betfair](https://www.karlwhelan.com/Papers/Betfair.pdf)
- Moshrefi: Kalshi sports calibration near-perfect mid-life, step-like in the last ten minutes — [arXiv:2607.14430](https://arxiv.org/abs/2607.14430)

### Inferences
- For a maker, the bias means the "fair" mid for a long-dated low-priced contract sits *below* the traded price (and above it for high-priced contracts); a maker should centre quotes on a debiased value (e.g., invert the Lattimore/Page–Clemen curve or Le's slope) and expect to be lifted on the longshot-ask more often than hit on the longshot-bid.
- Since the bias vanishes within ~a day of resolution, the debiasing adjustment should decay with time-to-resolution, like the vol clock.

### Gaps
- No bucket-by-bucket win-rate table for Polymarket (only slopes); no PredictIt calibration study found.
- Rothschild (2009, POQ) debiasing transform (Φ(1.64·Φ⁻¹(price)) in the Leigh–Wolfers tradition) was not confirmed in any retrieved source; cite only after checking the original.

---

## Key Question 6: Fair variance of a binary at horizon T and the resulting spread/size scaling with p and T

### Takeaway
Under log-odds-Brownian beliefs the local price variance is σ_p² ≈ p²(1−p)²·σ_x²·dt (delta = p(1−p)); under Gaussian-score/Archak–Ipeirotis it is σ²φ(Φ⁻¹(p))²/(T−t)·dt; under the Kalshi-fitted deadline clock it is p(1−p)/τ·dt — in every case half-spreads and inventory limits should scale with p(1−p) (or its square root) and the per-contract risk budget should rise as τ shrinks, with total resting size tapered roughly as √((T−t)/T).

### Cited Findings
- Logit diffusion: dp = p(1−p)·σ(t, logit p)·dW (Feil–Nendel) — [arXiv:2607.17991](https://arxiv.org/abs/2607.17991); Dalen: delta S′ = p(1−p), gamma = p(1−p)(1−2p), short-horizon probability variance ≈ (p(1−p))²·∫σ_b² + jump term — [arXiv:2510.15205](https://arxiv.org/abs/2510.15205)
- Gaussian score: V(t) = σ/√(T−t) · φ(N⁻¹(π)) — [Archak–Ipeirotis via Ipeirotis blog](https://www.behind-the-enemy-lines.com/2008/10/modeling-volatility-in-prediction_20.html); the same G=φ(Φ⁻¹(p)), h=√(T−t) pairing appears in Moallemi–Robinson–Zhu — [arXiv:2607.17428](https://arxiv.org/abs/2607.17428)
- Taleb: s(Y) = (σ/√π)·e^{−erf⁻¹(2Y−1)²} ≈ const·Y(1−Y); forecast std over the remaining horizon s ≈ √(e^{−2erf⁻¹(2Y₀−1)²}(e^{2σ²(T−t₀)}−1)/(2π)) — [ar5iv 1703.06351](https://ar5iv.labs.arxiv.org/html/1703.06351)
- Empirical deadline clock: h² = [p(1−p)/τ + K·ν(V)·s²/4]·Δ fits Kalshi better than GARCH; volatility flat beyond ~1 day, sharply rising in final hours — [arXiv:2607.08199](https://arxiv.org/abs/2607.08199)
- Recommended spread scaling: Dalen δ_p ≈ p(1−p)·δ_x with δ_x containing γσ_b²(T−t); inventory cap ∝ 1/p(1−p) — [arXiv:2510.15205](https://arxiv.org/abs/2510.15205); Feil–Nendel numerics: spreads largest at p=1/2 at any fixed time, widening near settlement at p≈1/2, skew fading at extremes — [arXiv:2607.17991](https://arxiv.org/html/2607.17991v1)
- Liquidity-over-time schedule: L_t ∝ √((T−t)/T) for constant expected loss rate on the √(T−t) clock — [arXiv:2607.17428](https://arxiv.org/html/2607.17428v1)
- Total variance to resolution is fixed: Var(Y_T | p) = p(1−p) regardless of path, which is exactly Feil–Nendel's terminal penalty q²p(1−p) — [arXiv:2607.17991](https://arxiv.org/abs/2607.17991)

### Inferences (my derivations from the cited models)
- Closed form for a maker's quoting horizon Δt (log-odds model): σ_p(Δt) ≈ p(1−p)·σ_x·√Δt. Example: σ_x = 0.6/yr^{1/2} (Feil–Nendel's σ₀) and Δt = 1 hour (1/8760 yr) gives σ_p ≈ 0.25·0.6·0.0107 ≈ 0.16c at p=0.5 and ≈0.06c at p=0.1 — tiny relative to a 1c tick, so tick size and fees, not diffusion, bind far from resolution; adverse selection and jumps dominate.
- Deadline-clock form: σ_p(Δt) ≈ √(p(1−p)·Δt/τ). With one day to go and a 1-hour horizon, σ_p ≈ √(0.25/24) ≈ 10c at p=0.5 and ≈ √(0.09/24) ≈ 6c at p=0.1 — orders of magnitude larger than the diffusion estimate, which is why spreads must widen and sizes shrink in the last day.
- Half-spread rule consistent with all three models: δ_p = max(tick floor, fee term ≈ 0.07·p(1−p) on Kalshi for taker-equivalent, γ·σ_p(Δt)²-type inventory term, adverse-selection term ∝ spread²·volume) — and because every term except the tick floor contains p(1−p), the ladder is naturally narrowest in cents at the extremes but widest in *relative* (percentage-of-price) terms there, matching the Whelan/Bürgi observation that spreads are "larger as a fraction of price for cheap contracts".
- Size rule: with per-contract settlement loss bounded by max(p,1−p) and variance p(1−p), a fixed dollar risk budget R implies max inventory q_max ≈ R/√(p(1−p)) for a diffusion view or R/max(p,1−p) for a worst-case view; both shrink the allowable *share* count fastest near 0.5 for worst-case but allow more shares near the extremes — consistent with Dalen's 1/p(1−p) cap only if one also caps worst-case loss.

### Gaps
- No empirical paper reports a fitted σ_x (log-odds volatility) by market category for Polymarket/Kalshi; Feil–Nendel's parameters are illustrative, Dalen's are synthetic.
- No source reconciles the 1/τ clock with the observed flat volatility beyond one day — Xi et al. only note the discrepancy.

---

## Key Question 7: Election prediction markets 2016–2024 — final-weeks behaviour, volatility, large jumps

### Takeaway
Election contracts move in bursts: in 2024 the Trump contract jumped ~10–11.5 pp over a single weekend (assassination attempt), ran from ~50% to 61% in October on three large accounts' buying (~$30–38M), and in 2016 Clinton's PredictIt price fell >50 pp within hours on election night; cross-venue gaps widened rather than closed in the final two weeks of 2024 (Clinton–Huang), so late-cycle inventory risk is dominated by jumps and venue fragmentation, not diffusion.

### Cited Findings
- 2024, nine venues: June 27 debate started Trump's slide from ~60%; July 13 assassination attempt +~10 pp within 48h (≈11.5 pp during the CME weekend closure); July 21 Biden withdrawal reversed part of the move; price-discovery regimes: May–Jul Betfair/William Hill 30–45% each, Jul–Aug fragmentation, Sep–Nov Polymarket dominance; prediction-market consensus led S&P futures on election night "by minutes early in the resolution and by hours late in it"; average cross-venue spread 6.2 pp — [Aktuğ & Torul](https://web.bogazici.edu.tr/torul/pridis.pdf)
- 2024 Polymarket whales: a French trader ("Théo") placed roughly $30–38M on Trump across four accounts (figures vary by outlet), with up to 71 orders/minute from one account; coverage attributed the October rise from ~50% to 61% partly to these buys in thin liquidity; eventual profit ≈$48.5–49.5M — [Fortune](https://fortune.com/2024/11/02/french-whale-polymarket-30-million-donald-trump-election-bet-kamala-harris), [Business Insider](https://businessinsider.nl/the-polymarket-whale-is-placing-as-many-as-71-bets-a-minute-in-favor-of-donald-trump/), [DL News](https://www.dlnews.com/articles/markets/polymarket-whale-is-poised-to-collect-49m-jackpot-on-trumps-election-victory)
- 2024 cross-platform: 9 Oct Kalshi ≈49c vs Polymarket ≈53c on Trump (Levine); mid-Oct Harris 45c Kalshi vs 43.1c Polymarket (Manifold snapshot) — [Bloomberg](https://www.bloomberg.com/opinion/articles/2024-10-09/a-trump-trade-arbitrage), [Manifold](https://manifold.markets/PlainBG/will-kalshis-presidential-election)
- 2024 final five weeks (Clinton & Huang): ~2,500 political markets, ~$2.4–2.5B volume; daily moves across platforms largely unrelated, spikes often reversed; inefficiency increased in the final two weeks — [DL News](https://www.dlnews.com/articles/markets/polymarket-kalshi-prediction-markets-not-so-reliable-says-study)
- Ng et al.: Polymarket leads Kalshi, lead strongest in high-liquidity periods, whale order imbalance predicts returns — [SSRN 5331995](https://papers.ssrn.com/abstract=5331995)
- 2016 election night: betting markets began shifting toward Trump ~110 minutes before major media acknowledged Clinton was in trouble; PredictIt Clinton price fell by "more than 50 percentage points" (one version says ~75 pp to 5%) from Tuesday morning; Ladbrokes moved from 76% Clinton to ~85% Trump in one night; a pre-election aggregator showed Clinton at 91.2% on Nov 8 — [Grand Forks Herald/Reuters](https://www.grandforksherald.com/news/trumps-chance-of-victory-skyrockets-on-betting-exchanges-online-market), [IOL](https://iol.co.za/news/world/2016-11-09-betting-is-on-trump-to-win/), [casino.org](https://www.casino.org/news/election-betting-polling-miss-presidential-results-2016-for-equal-epic-fail)
- Taleb on 2016: forecasters gave Trump "between 0.1% and 3%" at points and revised from 48% to 15% while citing uncertainty; a 98% vs 2% binary can arise from a 53/47 vote-share estimate — [ar5iv 1703.06351](https://ar5iv.labs.arxiv.org/html/1703.06351)
- Kalshi MAE declines smoothly toward close with a steep drop on the last day (all categories) — [Kalshi paper](https://www.karlwhelan.com/Papers/Kalshi.pdf)

### Inferences
- The relevant tail for a maker in election markets is a multi-point gap over a closed-market weekend or a 50-pp move in a few hours on election night; diffusion-based inventory limits are irrelevant there — size should be set by worst-case loss (1−p or p per share) times the probability of a regime-changing event in the holding window.
- Single-account flow (71 orders/min, $30M+) can move a thin book 10 pp; a maker's skew logic should treat persistent one-sided aggressive flow as informed (or at least price-moving) and lean quotes rather than absorb.

### Gaps
- No academic paper quantifies the election-night 2016 or 2024 minute-level price path on Betfair/PredictIt; numbers above are press reports that conflict (50 vs 75 pp).
- Clinton–Huang's working paper and its exact efficiency metrics could not be retrieved; Kalshi disputes it.
- IEM (Berg–Rietz) spread/depth statistics were not retrieved in this pass and are not cited above.
