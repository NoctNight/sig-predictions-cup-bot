# Empirical fill probability, fill speed, adverse selection and depth profiles vs. distance from mid / queue position

Scope note: every number below is labelled with asset class and era. "Primary" = peer-reviewed paper or arXiv preprint whose methodology/results sections I read (full text or HTML). Practitioner material (Databento, Amberdata) is used only to confirm, never alone. Sources are ordered roughly by how directly they answer each question.

Master source list (arXiv ID / DOI):
- Hollifield, Miller, Sandås, "Empirical Analysis of Limit Order Markets", Rev. Econ. Stud. 71(4) 2004, 1027–1063 (working-paper PDF: https://www.cis.upenn.edu/~mkearns/finread/hollifield-liquid2.pdf). Ericsson, Stockholm SE, Dec 1991–Mar 1992.
- Arroyo, Cartea, Moreno-Pino, Zohren, "Deep Attentive Survival Analysis in LOBs", arXiv:2306.05479, Quant. Finance 24(1) 2024. Nasdaq LOBSTER, Sep–Dec 2022.
- Lokin & Yu, "Fill Probabilities in a LOB with State-Dependent Stochastic Order Flows", arXiv:2403.02572 (v2, 2026). LMAX EUR/USD spot, Nov 2020–Oct 2021.
- "Optimal Execution with Passive Market Impact", arXiv:2607.28323 (2026). Nasdaq LOBSTER 2016 (6 stocks) + LSEG FX L2 2026 (5 pairs). Reports fitted k per tick.
- Eisler, Kertész, Lillo, Mantegna, "Diffusive behavior and the modeling of characteristic times in limit order executions", arXiv:physics/0701335, Quant. Finance 2009. LSE SETS 2002.
- Lo, MacKinlay, Zhang, "Econometric models of limit-order executions", J. Fin. Econ. 65 (2002) 31–71 (https://web.mit.edu/~alo/www/Papers/limit10.html). US equities, ITG order data, 1990s.
- Huang, Lehalle, Rosenbaum, "Simulating and analyzing order book data: the queue-reactive model", arXiv:1312.0563, JASA 110(509) 2015, DOI 10.1080/01621459.2014.982278. Euronext Paris, Jan 2010–Mar 2012.
- Abergel & Jedidi, "A mathematical approach to order book modeling", arXiv:1010.5136, IJTAF 16(5) 2013. CAC 40, March 2011.
- Cont, Stoikov, Talreja, "A stochastic model for order book dynamics", Oper. Res. 58(3) 2010, DOI 10.1287/opre.1090.0780. Sky Perfect Communications, Tokyo SE (per Gould et al. review table).
- Bacry, Jaisson, Muzy, "Estimation of slowly decreasing Hawkes kernels: application to high-frequency order book dynamics", arXiv:1412.7096, Quant. Finance 2016. DAX and Bund futures, Jun 2013–Jun 2014.
- "A Deterministic LOB Simulator with Hawkes-Driven Order Flow", arXiv:2510.08085 (2025). Binance BTCUSDT perp, Jan 2024; LOBSTER AAPL 2012.
- Albers, Cucuringu, Howison, Shestopaloff, "The Market Maker's Dilemma: Navigating the Fill Probability vs. Post-Fill Returns Trade-Off", arXiv:2502.18625 (2025). Binance USDT-margined BTC perpetual, Feb & Aug 2024, live minimum-size maker orders.
- Moallemi & Yuan, "A Model for Queue Position Valuation in a Limit Order Book" (2016/2017), https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf. Nasdaq ITCH, Aug 2013, 9 large-tick US names.
- Lehalle & Mounjid, "Limit Order Strategic Placement with Adverse Selection Risk and the Role of Latency", arXiv:1610.00261 (2016). AstraZeneca, Nasdaq OMX Nordic, 2013, broker-labelled trades.
- "Model-Free Passive Execution via Order-Level Shadowing", arXiv:2609.18019 (2026). CME ES futures MBO, calendar 2025.
- Bouchaud, Mézard, Potters, "Statistical properties of stock order books: empirical results and models", arXiv:cond-mat/0203511, Quant. Finance 2 (2002). Paris Bourse, Feb 2001.
- Zovko & Farmer, "The power of patience: a behavioral regularity in limit order placement", arXiv:cond-mat/0206280, Quant. Finance 2 (2002). LSE, 50 stocks.
- Potters & Bouchaud, "More statistical properties of order books and price impact", arXiv:cond-mat/0210710, Physica A 2003. Island ECN (QQQ, SPY, MSFT), Jun–Jul 2002.
- Gould, Porter, Williams, McDonald, Fenn, Howison, "Limit order books" (review), arXiv:1012.0349, Quant. Finance 2013.
- Jain, Muzy, Kochems, Bacry, "No Tick-Size Too Small", arXiv:2410.08744 (v3 2025). LOBSTER 2019, 15 S&P 500 names.
- Mike & Farmer, "An empirical behavioral model of liquidity and volatility", arXiv:physics/0509194, JEDC 2008. LSE.
- Dubach, "The Anatomy of a Decentralized Prediction Market: Microstructure Evidence from the Polymarket Order Book", arXiv:2604.24366 (2026). Polymarket, 600 markets, 2026.
- "An Empirical Study on Arrival Rates of Limit Orders and Order Cancellation Rates in Borsa Istanbul", arXiv:1909.08308 (2019). Garanti Bank, Aug–Sep 2017.
- Guéant, Lehalle, Fernandez-Tapia, "Dealing with the inventory risk", arXiv:1105.3115, Math. Fin. Econ. 2013.
- Falces Marín, Díaz Pardo, Lopez Lopez, "A reinforcement learning approach to improve the performance of the Avellaneda-Stoikov market-making algorithm", PLOS ONE 2022, https://pmc.ncbi.nlm.nih.gov/articles/PMC9767337/. BTC-USD L2, Dec 2020–Jan 2021.
- Schnaubelt, Rende, Krauss, "Testing Stylized Facts of Bitcoin Limit Order Books", J. Risk Fin. Mgmt 12(1) 2019, DOI 10.3390/jrfm12010025.

---

## Key Question 1: Execution probability (and fill time) of a limit order as a function of distance from the best quote — fitted curves and tables, with horizons

### Takeaway
Across equities (1992 Stockholm, 2002 LSE, 2013–2022 Nasdaq), FX (2020–2021 LMAX) and crypto (2024 Binance), the empirical fill probability falls steeply with distance: roughly 65% → 30% → 12% at 1/2/3 ticks from the quote for a lifetime-horizon measure (Stockholm), and essentially negligible beyond one tick in a tight FX book (fills at the best ≈2%, one tick deeper ≈0.2% per event). The decay of fill intensity in ticks is well approximated by exp(−k·δ) with fitted k between 0.5/tick (small-tick, wide-spread names and FX) and 1.6–3.7/tick (large-tick names); median time-to-fill grows sub-diffusively as distance^1.4.

### Cited Findings

**Stockholm Stock Exchange, Ericsson, 59 trading days Dec 3 1991–Mar 2 1992, 20,760 realized order submissions (Hollifield, Miller & Sandås 2004).** Execution probability is defined as expected fraction of the order quantity executed over the order's life (eq. 9); picking-off risk as the expected change in common value weighted by executed quantity (eq. 10). Table 9 sample averages (std errors in parentheses):
- Buy limit orders: 1 tick from quote: execution probability 0.68 (0.01), picking-off risk 0.01 (0.03); 2 ticks: 0.33 (0.02), −0.09 (0.05); 3 ticks: 0.12 (0.02), −0.15 (0.10).
- Sell limit orders: 1 tick: 0.63 (0.01), 0.01 (0.02); 2 ticks: 0.27 (0.02), 0.14 (0.07); 3 ticks: 0.13 (0.02), 0.11 (0.08).
- Only 2.8% of orders remain in the system more than two trading days, and 62.3% of those are eventually cancelled; tick size 0.5–1 SKr. — [Hollifield et al. working paper](https://www.cis.upenn.edu/~mkearns/finread/hollifield-liquid2.pdf)

**Nasdaq (LOBSTER), 9 stocks (AAPL, AMZN, BIDU, COST, CSCO, DELL, GOOG, INTC, MSFT), fill statistics 1 Oct–27 Dec 2022 (Arroyo, Cartea, Moreno-Pino, Zohren 2023, arXiv:2306.05479).** Fills of hypothetical orders are defined under price-time priority: filled when a market order executes against tracked orders at its price, or when orders ahead in queue are filled, or a market order arrives at a better price. Large-tick stock = average spread < 1.3 ticks (CSCO 1.15, INTC 1.13; others 1.44–30.89 ticks). Table 3 fill probability by placement (best level vs. 1, 2, 5 ticks inside the spread):
- AAPL 5.39% / 13.17% / 36.01% / 44.31%; AMZN 9.23 / 13.12 / 33.60 / 44.85; BIDU 9.48 / 5.28 / 15.33 / 21.40; COST 4.58 / 1.22 / 8.26 / 11.35; CSCO 5.73 / 17.53 / 35.49 / 45.58; DELL 3.92 / 6.20 / 22.63 / 29.95; GOOG 6.18 / 10.88 / 31.99 / 50.12; INTC 5.70 / 29.62 / 32.15 / 11.65; MSFT 6.79 / 7.34 / 26.32 / 44.66.
- Average fill times at the best level range from 0.99 s (MSFT) to 7.33 s (INTC); in-spread fill times are sub-second to a few seconds.
- Orders inside the spread of large-tick stocks are "approximately 3 to 6 times more likely" to fill than best-level orders; for large-tick stocks in-spread fill probability rises sharply at short horizons, then decays toward the best-level value.
- Orders pegged outside the best level are mostly censored (rarely fill within the tracking window), so the analysis focuses on the best level. Shapley values: microprice is the most important feature, then volatility; time-of-day matters little. — [arXiv:2306.05479](https://arxiv.org/abs/2306.05479)

**LMAX EUR/USD spot, 2 Nov 2020–29 Oct 2021, four representative weeks 7 Jun–2 Jul 2021 (Lokin & Yu, arXiv:2403.02572).**
- 99.87–99.93% of limit orders are cancelled; only 0.07–0.13% are (partially) filled, per week. Market-order size / limit-order size ≈ 0.38–0.47; cancellation size / limit size = 1.00.
- More than 90% of executions occur at the best quotes; about 85% of executed limit orders were submitted within one tick of the best quote; "fill probabilities beyond one tick from the best quote are typically negligible."
- Spread is 3–4 ticks about 80% of the time (range 1–5 ticks).
- Table 6 (5 Jul 2021, spread S=1, ask queue qA=1, event-level fill probability for a new best-bid order, empirical / model): qB=1: 2.0% / 3.0%; qB=2: 0.6% / 1.9%; qB=3: 0.3% / 1.5%; qB=4: 0.0% / 1.2%; qB=5: 0.0% / 0.9%. Other spreads, qA=qB=1: S=2 1.3%/1.5%; S=3 0.5%/1.2%; S=4 1.5%/1.1%; S=5 0.0%/1.1%.
- Table 8 (one tick deeper than the best bid, S=1 at submission): qB⁻=1: 0.19% empirical / 0.75% model; qB⁻=2: 0.06% / 0.63%; qB⁻=3: 0.28% / 0.42%. The model overestimates execution when queues are large. — [arXiv:2403.02572](https://arxiv.org/abs/2403.02572)

**Nasdaq LOBSTER, 6 stocks, 252 days 4 Jan–30 Dec 2016, 10:00–15:30 (arXiv:2607.28323, "Optimal Execution with Passive Market Impact").** Fill intensity per distance bin δ (ticks from mid) is Λ̂(δ) = N_fill(δ)/T_exp(δ) (executed shares / tracked exposure seconds, summed over days), fitted as log Λ̂(δ) = log λ − kδ. Table 4 (k̂ per tick; ℓ̂, m̂ are MLOFI impact-decay parameters):
- AMZN (spread 36.7 ticks) k̂ = 0.4807; TSLA (19.2) 0.5657; NFLX (4.0) 0.8584; ORCL (1.2) 1.5781; CSCO (1.1) 3.7247; MU (1.1) 1.6858.
- FX (LSEG L2, 100 business days from start of 2026, 06:00–18:00 UTC, synthetic fills: a passive quote counts as filled when next-bucket aggressor trades reach or cross it), Table 5: USDMXN (spread 4.4 ticks) k̂ = 0.52; GBPUSD (2.7) 0.49; AUDUSD (2.3) 0.54; USDTHB (2.1) 0.62; USDSGD (1.5) 0.61.
- Confidence intervals are stated to be "roughly one to two orders of magnitude smaller" than the estimates; intercepts log λ̂ are fitted but not tabulated. The paper describes the "approximately exponential decay of limit-order fill probabilities with distance from the midprice" as an empirical building block. — [arXiv:2607.28323](https://arxiv.org/abs/2607.28323)

**LSE SETS, 2002, five liquid stocks (AZN, GSK, LLOY, SHEL, VOD) (Eisler, Kertész, Lillo, Mantegna, arXiv:physics/0701335).**
- Median time-to-fill (TTF) scales with distance from the best as Δ^1.4 (vs Δ² for Brownian first-passage); median first-passage time scales as Δ^1.5–1.8.
- Tail exponents (Δ = 1–4 ticks): first-passage time λ_FPT ≈ 1.49–1.57 (close to the Brownian 3/2); TTF λ_TTF ≈ 1.8–2.2; time-to-cancel λ_TTC ≈ 1.9–2.4; so λ_FPT < λ_TTF ≈ λ_TTC, and the TTF/FPT gap is explained by cancellations with power-law lifetimes, exponent λ_LT ≈ 1.6.
- Orders at Δ ≤ 0 (at or inside the quote) are less than half of all limit orders. — [arXiv:physics/0701335](https://arxiv.org/abs/physics/0701335)

**US equities, broker (ITG) limit-order records, 1990s (Lo, MacKinlay & Zhang, JFE 2002).** Survival-analysis (accelerated-failure-time) models of time-to-first-fill and time-to-completion; "execution times are very sensitive to limit price and several other explanatory variables, but not sensitive to limit size." Hypothetical executions built from first-passage times or from the transactions tape "are very poor proxies for actual limit-order executions." — [MIT abstract page](https://web.mit.edu/~alo/www/Papers/limit10.html)

**Island ECN, QQQ/SPY/MSFT, 1 Jun–15 Jul 2002 (Potters & Bouchaud 2003).** Cancel rate at the bid/ask ≈ 10 per minute; the execution rate is about 22% of the cancel rate for QQQ and about 40% for MSFT; order lifetime increases with distance from the best price. — [arXiv:cond-mat/0210710](https://arxiv.org/abs/cond-mat/0210710)

**FX (2013, Gould et al. 2013b, cited in the review):** more than 99.99% of active orders ended in cancellation rather than matching. — [arXiv:1012.0349](https://arxiv.org/abs/1012.0349)

**Binance USDT-margined BTC perpetual, 12–19 Feb 2024, 232,897 live minimum-size maker orders at the best (Albers et al., arXiv:2502.18625).** 127,051 filled, 105,846 cancelled unfilled (54.5% filled). Empirical fill probability on a 20×20 grid of near-side × opposite-side queue size at submission ranges from about 30% (large near-side, small opposite queue) to above 90% (small near-side, very large opposite queue); OLS surface R² = 0.946 with coefficients β0 = 0.5649, β1 = 0.0159, β2 = 0.1013, β3 = −0.3166 (imbalance). Tick 0.1 USD ≈ 0.03 bp. — [arXiv:2502.18625](https://arxiv.org/abs/2502.18625)

**Nasdaq ITCH, August 2013, 9 large-tick names (spread ≈ 1.00–1.02 ticks) (Moallemi & Yuan).** Backtested fill probability of an order placed at the back of the best queue: BAC 0.60, CSCO 0.59, GE 0.60, F 0.64, INTC 0.61, PFE 0.58, PBR 0.53, EEM 0.63, EFA 0.53 (model: 0.57–0.65). — [Moallemi & Yuan PDF](https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf)

### Inferences
- Measured "fill probability" is horizon-dependent and the literature uses three different horizons: order lifetime until fill/cancel (Hollifield; Moallemi–Yuan backtest), event-level (next mid move / next event; Lokin–Yu), and a fixed clock horizon (Arroyo survival curves; 2607.28323 intensity per second). A practitioner must pick one and label it; the Stockholm 0.68/0.33/0.12 and the LMAX 2%/0.2% numbers are not comparable without that.
- Converting the fitted k̂ to ratios: with k ≈ 0.5/tick (AMZN, FX), moving one tick deeper cuts fill intensity by ~40%; with k ≈ 1.6/tick (ORCL/MU) by ~80%; with k ≈ 3.7/tick (CSCO) by ~98%. Large-tick books therefore have almost no fill intensity beyond the touch, which is consistent with the LMAX finding that >90% of executions occur at the best.
- The relative tick size (spread in ticks) is the main driver of k: in Table 4 of arXiv:2607.28323, k̂ decreases monotonically as spread-in-ticks rises (36.7 ticks → 0.48; 1.1 ticks → 3.7). Expressing δ in units of the spread rather than ticks should collapse much of this cross-sectional variation (my inference; not tested by the paper).
- The TTF ∝ Δ^1.4 scaling means expected fill time grows sub-quadratically in distance, i.e., the hazard per unit time does not fall as fast as Brownian first passage would imply, because price-level crossings (not only diffusion) consume deeper levels.

### Gaps
- I could not retrieve the coefficient tables or distance-bucket execution rates from Lo, MacKinlay & Zhang (2002); only the abstract was accessible (NBER PDF is a scanned image).
- arXiv:2607.28323 does not tabulate the intercepts λ̂, so absolute fill intensities per tick are not available, only the decay rates k̂.
- Arroyo et al. Table 3 does not state the clock horizon over which "fill probability" is computed (presumably over the tracked lifetime within the dataset); level-2+ survival curves are shown only in figures.
- No source reports a fill-probability-vs-distance table for crypto beyond the best level; the Binance study (arXiv:2502.18625) quotes only at the touch.

---

## Key Question 2: Queue-reactive / CST / Abergel–Jedidi / Hawkes models — per-level arrival and cancel intensities and implied fill rates

### Takeaway
Empirical per-level estimates show that limit-order arrival intensity is not monotone in distance: it peaks at the first or second level and is essentially zero beyond 10–14 ticks (CAC 40, 2011), that per-share cancellation rates fall by three orders of magnitude from level 1 to level 13, and that at the best queue insertion is flat in queue size while cancellation is concave and market-order intensity decays roughly exponentially in queue size (Euronext large-tick, 2010–2012); Poisson (state-independent) models overestimate execution probability.

### Cited Findings

**Euronext Paris, France Telecom and Alcatel-Lucent (large-tick), Jan 2010–Mar 2012, Cheuvreux database, 5 limits per side, first and last hour removed (Huang, Lehalle & Rosenbaum, arXiv:1312.0563 / JASA 2015).**
- ~159,250 (FTE) / 129,400 (ALU) orders per day; 7,282 / 8,626 trades per day; average spread 1.43 / 1.99 ticks.
- Queue sizes are normalised by the average event size (AES) at each limit: FTE AES1 = 836, AES2 = 1,068, AES3 = 1,069 shares (average trade size 637); ALU 3,033 / 3,451 / 3,528 (ATS 2,340).
- Model I intensities (functions of own queue size, FTE): insertion at Q1 roughly constant in queue size (much smaller at zero); insertion at Q2 and Q3 decreasing in queue size; cancellation at Q1 increasing and concave up to about 25 AES then flat; cancellation at Q2 reaches its asymptote faster and lower; cancellation at Q3 increases almost linearly beyond 3 AES; market orders at Q1, Q2, Q3 decrease roughly exponentially with queue size, Q2 much smaller than Q1, Q3 rare. K = 3 was chosen because dynamics at Q±4, Q±5 resemble Q±3.
- Model II: at Q2 insertion decreases with queue size and reaches its asymptote faster when Q1 is empty; cancellation at Q2 is higher when Q1 is empty; no market orders arrive at Q2 while Q1 has orders; at Q1 insertion is significantly higher when the opposite queue is empty and cancellation falls as opposite-side liquidity rises.
- Invariant distribution in closed form π(n) = π(0)·Π ρ(j−1), ρ(n) = λ^L(n)/(λ^C(n+1)+λ^M(n+1)); the model fits empirical queue-size distributions at Q±1..Q±3 "very well".
- Model III calibrated θ = 0.7, θ_reinit = 0.85; with θ = 1, θ_reinit = 0 the mechanical volatility is ≈5 bp vs 14 bp empirical.
- Execution-probability check (Fig. 9): the three queue-reactive models give similar results while the Poisson model "clearly overestimates" execution probabilities. Pegging-to-best tactic gets higher passive execution rate but more impact. — [arXiv:1312.0563](https://arxiv.org/abs/1312.0563)

**CAC 40 constituents, March 2011 (23 days), 9:30–14:00 CET, 10 levels, Thomson Reuters Tick History; plots for Schneider Electric SCHN.PA (Abergel & Jedidi, arXiv:1010.5136).** Market order rate λ^M± = 0.1237; lognormal size parameters (v, s): market (4.00, 1.19), limit (4.47, 0.83), cancel (4.48, 0.82). Table 5, per level i (ticks from best): average depth ⟨X_i⟩ shares, limit arrival rate λ^L±, and per-share cancellation rate 10³·λ^C±:
- i=1: 276 shares, 0.2842, 0.8636
- i=2: 1,129, 0.5255, 0.4635
- i=3: 1,896, 0.2971, 0.1487
- i=4: 1,924, 0.2307, 0.1096
- i=5: 1,951, 0.0826, 0.0402
- i=6: 1,966, 0.0682, 0.0341
- i=10: 1,691, 0.0321, 0.0178
- i=12: 1,435, 0.0015, 0.0012
- i=13: 1,338, 0.0001, 0.0001
- i=14: 1,238, 0; i=30: 249, 0.
Limit-order arrival peaks at level 2 and is essentially zero by levels 12–14; per-share cancellation rate falls from 8.6×10⁻⁴ at level 1 to 10⁻⁷ at level 13. Simulated spread and long-run variance are systematically lower than empirical (cross-sectional R² 0.94–0.99). — [arXiv:1010.5136](https://arxiv.org/abs/1010.5136)

**Cont, Stoikov & Talreja (Oper. Res. 2010).** Independent Poisson arrivals per level with limit-order rate decaying in distance and cancellations proportional to queue size; estimated on Tokyo Stock Exchange data (Sky Perfect Communications, five best levels per side, per the data table in the Gould et al. review). The queue-reactive paper notes CST's "linearly increasing cancellation rate" assumption and finds instead a concave, flattening cancellation rate at the best. The maximum arrival rate of incoming orders on the TSE was found inside the spread (Cont et al. 2010), versus at relative price 0 on the LSE, Shenzhen, Paris, and NASDAQ. — [DOI 10.1287/opre.1090.0780](https://doi.org/10.1287/opre.1090.0780); [arXiv:1012.0349](https://arxiv.org/abs/1012.0349); [arXiv:1312.0563](https://arxiv.org/abs/1312.0563)

**Borsa Istanbul, Garanti Bank, 1 Aug–29 Sep 2017, 228 instances (arXiv:1909.08308).** Normalised quantity share by tick (first 15 ticks) is best fit by a discrete Weibull (lowest L1 error in 210/228 instances); power-law fit is statistically indistinguishable (Welch p = 0.24–0.97); the exponential fit is 2–3× worse. Cancellation ratios across the first 10 ticks are not rejected as uniform (chi-square) in all but one week. — [arXiv:1909.08308](https://arxiv.org/abs/1909.08308)

**LMAX EUR/USD (Lokin & Yu).** Limit, market and cancellation rates are "much higher when the spread is small"; estimators conditional on spread S: λ̂^S(δ) = N_l^S(δ)/T^S, μ̂^S = (N_m^S/T^S)·(S_m/S_l), θ̂^S(δ) = N_c^S(δ)/(T^S·Q̄_δ^S)·(S_c/S_l). Model-implied vs empirical probability of an upward mid move has MAPE ≈ 10% (rolling 5-day calibration, spread-dependent). — [arXiv:2403.02572](https://arxiv.org/abs/2403.02572)

**DAX and Bund front-month futures, Level-I, Jun 2013–Jun 2014, 8-dimensional Hawkes (P±, T±, L±, C±) (Bacry, Jaisson & Muzy, arXiv:1412.7096).**
- Event counts (millions, DAX / Bund): price moves 9.36 / 1.72 per side; non-price-moving trades 2.66 / 3.40; limit orders at best 19.7 / 29.8; cancels at best 23.3 / 26.6.
- Baseline (exogenous) intensities μ in s⁻¹ and exogeneity ratio μ/Λ: DAX L(a) 2.07e-2 (1.1%), C(a) 1.53e-2 (0.7%), T(a) 1.06e-2 (4.3%), P(a) 2.37e-2 (2.7%); Bund L(a) 3.83e-2 (1.4%), C(a) 4.00e-2 (1.6%), T(a) 1.41e-2 (4.5%), P(a) 7.13e-3 (4.4%). So 95–99% of limit/cancel events at the best are endogenous.
- Kernels are roughly power-law with exponents slightly above 1, estimated from ~0.1 ms to 140 s; trades have a very fast positive effect on opposite-side limit orders that reverses (inhibition) at longer lags; upward price moves excite bid limit orders and ask cancels (localised ~0.3 ms) and inhibit ask limit orders and bid cancels long-term; for the large-tick Bund the authors link price moves to adverse selection with cross-excitation of trades. — [arXiv:1412.7096](https://arxiv.org/abs/1412.7096)

**Binance BTCUSDT perpetual, January 2024, ~12.5M trades (arXiv:2510.08085).** Univariate Hawkes on trade flow: exponential kernel μ = 4.127, α = 1.854, β = 2.321, branching ratio 0.799, mean intensity 20.53 events/s; power-law kernel γ = 1.234, branching 0.812. (Internal inconsistencies between Tables 2 and 3 noted; no per-level rates.) — [arXiv:2510.08085](https://arxiv.org/abs/2510.08085)

### Inferences
- The Abergel–Jedidi table gives a directly usable "fill-relevant" quantity per level: λ^L(i)/⟨X_i⟩ (new-order share per existing share) is 1.0e-3 at level 1, 4.7e-4 at level 2, 1.6e-4 at level 3, 1.2e-4 at level 4, 4.2e-5 at level 5 — a decay of roughly a factor 2–3 per tick near the touch, which is in the same range as the k ≈ 0.9–1.6 per tick fitted for medium-tick Nasdaq names in arXiv:2607.28323.
- Since 95–99% of best-level limit and cancel events are endogenous (Hawkes exogeneity ratios), any "fill intensity" measured unconditionally is a time average over strongly clustered flow; fills arrive in bursts, so sizing per level from average rates understates variance of fills per unit time.
- The queue-reactive finding that market-order intensity at Q1 decays exponentially in queue size, while Q2 receives no market orders while Q1 is non-empty, is the mechanism behind the "negligible beyond one tick" fill results in large-tick/FX data: a second-level order only fills after the first level is depleted.

### Gaps
- I could not access the full text of Cont, Stoikov & Talreja (2010) (INFORMS 403; mirrors unreachable), so their fitted λ(i) = k/i^α, μ and θ(i) values are not reproduced. (I recall k ≈ 1.92, α ≈ 0.71, μ ≈ 0.94, θ(1..5) ≈ 0.71, 0.81, 0.68, 0.56, 0.47 from training data but could not verify them; treat as unconfirmed.)
- The queue-reactive paper reports intensity shapes only as figures; no numeric λ^L/λ^C/λ^M values per AES level are given in the text.
- The Hawkes studies are Level-I only; no Hawkes paper I found reports kernels per depth level.

---

## Key Question 3: Realized adverse selection (markout) by level and queue position vs. the half-spread captured

### Takeaway
Empirically, adverse selection at the touch is of the same order as the half-spread plus rebate in large-tick US equities (0.53–0.89 ticks vs 0.8 ticks, 2013), and grows with queue position: in Binance BTC perp (2024) 1-s markouts are −0.06 bp at the front of a large queue but −0.78 to −1.16 bp at the back, versus a 0.5 bp maker rebate; in ES futures (2025) passive fills give up ≈0.08–0.10 ticks of a 0.52-tick half-spread. The only multi-level dataset I found (Stockholm 1992) shows picking-off risk increasing, not decreasing, with distance for the orders that actually fill.

### Cited Findings

**Nasdaq ITCH, 9 large-tick names, 21 trading days Aug 2013, backtest with artificial orders (Moallemi & Yuan).** Liquidity premium δ0 = half-spread + 0.3-tick Nasdaq rebate = 0.8 ticks. Table 3 (simulation values, ticks, average over 30 days; model values in parentheses): adverse selection cost for an order at the back of the best queue: BAC 0.57 (0.57), CSCO 0.68 (0.68), GE 0.65 (0.67), F 0.53 (0.60), INTC 0.56 (0.63), PFE 0.61 (0.62), PBR 0.89 (0.85), EEM 0.64 (0.69), EFA 0.73 (0.74). Resulting order value (back of queue): BAC 0.14, CSCO 0.07, GE 0.09, F 0.15, INTC 0.09, PFE 0.11, PBR −0.04, EEM 0.08, EFA 0.04 ticks; value at the very front of the queue ("touch value"): BAC 0.31, CSCO 0.21, GE 0.23, F 0.23, INTC 0.23, PFE 0.21, PBR 0.03, EEM 0.15, EFA 0.09 ticks. Order value = fill probability × (liquidity premium − adverse selection cost). Theorem 2: execution probability is non-increasing in queue position; adverse selection is positive and increasing with queue position; for BAC and CSCO the front/back gap "can be very large and comparable to the bid ask spread". BAC August 2013 daily estimates: average jump size ≈ 1 tick; price impact λ in bp per 1% of daily volume 0.44–1.01. — [Moallemi & Yuan PDF](https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf)

**Binance BTC perpetual, 12–19 Feb 2024, live minimum-size maker orders, markout = mid move 1 s after fill in bp (Albers et al., arXiv:2502.18625).** Queue position QP = liquidity ahead/(ahead+behind). Average 1-s post-fill return by near/opposite queue size, front (QP 0–0.1) vs back (QP 0.75–1):
- large near / small opposite: −0.058 bp (front) vs −0.775 bp (back, std 1.6);
- large / large: −0.296 vs −1.157 (std 1.1);
- small / small: −0.562 vs −0.677 (std 2.2);
- small / large: −0.539 vs −0.763 (std 0.9).
Only front-of-queue fills in a large near-side queue with a small opposite queue have a near-zero mean markout (distribution mostly positive). Average 5-s post-fill return across all orders ≈ −0.8 bp, ≈ −0.3 bp net of the 0.5 bp maker rebate. Orders followed by negative 5-s returns fill at high rates; those followed by positive returns rarely fill. Naive two-sided market making: ≈ −60% cumulative over ~3 days 7 hours including rebates, annualised Sharpe −109, average holding 11 s; naive maker −0.43 bp per round trip. A logistic reversal filter raises 5-s return to +0.71 bp at 654 fills/day (Sharpe 11.97) and +1.21 bp at 426 fills/day. — [arXiv:2502.18625](https://arxiv.org/abs/2502.18625)

**CME ES futures, market-by-order, 245 sessions in 2025, ~1.63M passive fills (arXiv:2609.18019).** Markout = mid move after fill, signed so positive = against the fill: passive +0.0825 ± 0.0076 ticks at 1 s, +0.0743 ± 0.0110 at 5 s, +0.0993 ± 0.0200 at 30 s; aggressive +0.0826 / +0.0874 / +0.0913. Crossing immediately costs +0.519 ticks vs mid; passive execution's relative slippage is +0.0953 ticks vs mid, i.e., −0.4236 ticks relative to immediate execution. Post-fill profile is flat: the price move "is complete within a second of the fill and does not revert" over 29 s. ES book is one tick wide 96.3% of the time (mean spread 1.038 ticks). Markouts by queue position or level are not reported. — [arXiv:2609.18019](https://arxiv.org/abs/2609.18019)

**Stockholm, Ericsson 1991–92 (Hollifield et al. 2004), Table 9 picking-off risk (expected common-value change weighted by executed quantity):** buy orders 1/2/3 ticks from the quote: 0.01 / −0.09 / −0.15; sell orders: 0.01 / 0.14 / 0.11; the authors state that "buy orders away from best bid quote and sell orders away from the best ask quote face a larger picking off risk than orders closer to the quotes" (exception: 3-tick sells). Estimated payoff at threshold valuation falls from 1.94 (1-tick buy) to 0.39 (2-tick) to −0.02 (3-tick). — [Hollifield et al.](https://www.cis.upenn.edu/~mkearns/finread/hollifield-liquid2.pdf)

**AstraZeneca, Nasdaq OMX Nordic, 2 Jan–30 Sep 2013, 376,672 trades with member labels (Lehalle & Mounjid, arXiv:1610.00261).** Average best-level imbalance (Q_bid−Q_ask)/(Q_bid+Q_ask) at the moment of a passive fill: global banks sell 0.35 / buy −0.38; high-frequency participants sell 0.32 / buy −0.33; institutional brokers sell 0.57 / buy −0.52 — i.e., institutional brokers' limit orders fill when the book is most tilted against them; price profiles ±5 min around fills (normalised by spread) show institutional brokers buying while the price falls, while HFPs insert orders shortly before a fill after the price has already moved in their favour. Proprietary traders fill at smaller |imbalance| and suffer less adverse selection. The model parameters (λ+ = 0.06, λ− = 0.5, α = 4, etc.) are illustrative, not fitted. — [arXiv:1610.00261](https://arxiv.org/abs/1610.00261)

**Practitioner confirmation (not a primary source): Databento, NVDA, XNAS.ITCH MBP-1, 21 May 2024.** Passive-fill markouts vs mid start positive (spread capture) and turn negative after roughly 1 second for all size buckets (<100, ≥100, ≥200 shares). — [Databento example](https://databento.com/docs/examples/algo-trading/execution-slippage)

### Inferences
- In large-tick equities and tight crypto perps, the gross spread capture at the touch is almost fully consumed by adverse selection for back-of-queue fills: Moallemi–Yuan's 0.53–0.89 ticks vs 0.8 ticks premium leaves 0.03–0.15 ticks per order; Albers et al.'s −0.8 bp 5-s markout vs 0.5 bp rebate (tick ≈ 0.03 bp) is net negative. Queue position (front vs back) is worth 0.1–0.2 ticks in equities and ~0.7–0.9 bp in BTC perps — larger than the residual edge itself.
- The hypothesis "deeper quotes are less adversely selected per fill" is not supported by the one multi-level dataset with realized fills (Stockholm 1992): conditional on execution, deeper orders experienced larger adverse common-value moves, because they only fill when the price has moved through the touch. The deeper level does capture a larger price concession (1 extra tick per level), so net payoff still fell from 1.94 to −0.02 across 1→3 ticks in that sample.
- Markout horizon matters: ES and BTC results show the adverse move completes within ~1 s and does not revert, so markouts at 1 s, 5 s, 30 s are roughly equal; a practitioner can use a 1–5 s horizon for calibration without biasing the level comparison.

### Gaps
- No published study I found tabulates realized markouts as a function of distance from mid beyond the best level for modern electronic markets (equities, FX or crypto). The best available are Hollifield's 1992 three-tick table and Albers et al.'s touch-only grid.
- Hollifield's picking-off-risk units (SKr vs ticks) were not recoverable from the extracted text; tick size was 0.5–1 SKr, so the values are of order 0.1–0.3 ticks.
- Cheridito & Weiss (arXiv:2608.18195) report markouts ≈ 0.5 ticks for 2- vs 20-lot touch orders, but in a simulated agent-based environment, not historical data; excluded from findings.

---

## Key Question 4: Empirical shape of the depth profile vs. distance from mid (the "hump") and what it implies about how competing makers size

### Takeaway
Time-averaged depth is hump-shaped in most markets: small at the best quote, rising to a maximum a few ticks away (2–6 ticks on Paris/CAC 40 and LMAX EUR/USD; at the touch for large-tick US names; 30–80 ticks from mid for small-tick names like AMZN/GOOG/BKNG), then decaying as a power law with the same exponent as the placement distribution (μ ≈ 0.6 Paris; cumulative exponent 1.5 LSE); Polymarket depth is nearly uniform across the top 10 levels (L1 share median 0.136).

### Cited Findings

**Paris Bourse, France Telecom, Vivendi, Total, February 2001 (Bouchaud, Mézard & Potters 2002).** Tick 0.05 EUR (FTE, VIV) / 0.10 EUR (TOT); FTE 270,000 orders, 28,600 market orders, avg spread 2.0 ticks, avg volume at best 2,700 shares; Total 94,350 orders, spread 1.4 ticks.
- Incoming limit-order placement distance Δ from the best (same side) follows P(Δ) ∝ Δ0^μ/(Δ1+Δ)^(1+μ) with μ ≈ 0.6 for all three stocks (Δ1 = 1, 0.5, 0), valid from 1 tick to over 100 ticks (crossover to faster decay beyond ~100 ticks); P(−1) ~ P(0) ~ P(1); orders at or inside the spread are roughly half of all orders; mean Δ formally infinite since μ < 1.
- Average book is symmetric with a maximum away from the best; ρ_st(Δ) ∝ Δ^(1−μ) for small Δ and ∝ Δ^(−1−μ) for large Δ (data collapse across the three stocks after rescaling).
- Volume at the best is Gamma distributed, R(V) ∝ V^(γ−1)e^(−V/V0), γ ≈ 0.7–0.8, V0 ≈ 2,700 (FTE); ~10% of limit orders are modified or cancelled; cancellation modelled as constant rate Γ ≈ 10⁻³ per unit time; characteristic price scale during an order's lifetime ≈ 10 ticks.
- Mean order volume at distance Δ is roughly constant for Δ = 1–20 ticks, then decays as Δ^(−1.5). — [arXiv:cond-mat/0203511](https://arxiv.org/abs/cond-mat/0203511)

**LSE, 50 stocks, ~7 million limit orders (Zovko & Farmer 2002).** Cumulative distribution of relative limit price (limit price minus best) decays as a power law with exponent ≈ 1.5 over more than two decades (a few ticks to ~2,000 ticks), for both buys and sells; relative-price series autocorrelation decays as τ^(−0.4); relative prices correlate positively with volatility, volatility leading. — [arXiv:cond-mat/0206280](https://arxiv.org/abs/cond-mat/0206280)

**Island ECN, Jun 1–Jul 15 2002 (Potters & Bouchaud 2003).** QQQ time-averaged queue is symmetric and peaks away from the best (hump like French stocks); SPY and MSFT queues are largest at the best (Island is not dominant for these: ~40% of QQQ volume vs ~20% of SPY/MSFT). Nasdaq placement tail indices are "in the same ballpark" as the French (μ ≈ 0.6) and British (μ ≈ 1.5) values, ordered MSFT < QQQ < SPY. Price response is logarithmic in volume, R(V,τ) ≈ R(τ) ln V. — [arXiv:cond-mat/0210710](https://arxiv.org/abs/cond-mat/0210710)

**Review (Gould et al. 2013):** mean relative depth profiles are hump-shaped on the Paris Bourse, NASDAQ, the Stockholm SE (Hollifield et al. 2004) and the Shenzhen SE (Gu et al. 2008c); for SPY the maximum is at the best, "a hump with its maximum at a relative price of 0"; the location varies across markets and tick size prevents direct comparison; the relative-price distribution of incoming orders is power-law in all markets studied (Paris, Shenzhen, Tokyo, LSE, NASDAQ); maximum arrival rate is at relative price 0 on LSE, Shenzhen, Paris, NASDAQ but inside the spread on the TSE; the far tail of the depth profile is disputed (power law vs exponential (Gu et al. 2008) vs lognormal (Preis et al. 2006)). — [arXiv:1012.0349](https://arxiv.org/abs/1012.0349)

**LSE (Mike & Farmer 2008):** placement relative to the best is well approximated by a Student-t with fewer than two degrees of freedom, symmetric, independent of spread, same for buys and sells. — [arXiv:physics/0509194](https://arxiv.org/abs/physics/0509194)

**CAC 40, Schneider Electric, March 2011 (Abergel & Jedidi):** average depth by level: 276 (1), 1,129 (2), 1,896 (3), 1,924 (4), 1,951 (5), 1,966 (6), 1,691 (10), 1,435 (12), 249 (30) shares — a hump peaking at levels 5–6, i.e., level-1 depth is ~14% of the peak. — [arXiv:1010.5136](https://arxiv.org/abs/1010.5136)

**LMAX EUR/USD 2020–21 (Lokin & Yu):** average queue sizes are smallest at the best quote, peak around 4 to 6 ticks, then level off. — [arXiv:2403.02572](https://arxiv.org/abs/2403.02572)

**Nasdaq LOBSTER, 15 S&P 500 names, 2 Jan–30 Dec 2019 (Jain, Muzy, Kochems, Bacry, arXiv:2410.08744).** Relative tick (bp): SIRI 16.47, BAC 3.40, INTC 2.19, CSCO 1.94, ORCL 1.85 (large, ≥1.5 bp); ABBV 1.30, PM 1.22, MSFT 0.77, IBM 0.72, AAPL 0.48 (medium); TSLA 0.36, CHTR 0.25, GOOG 0.08, AMZN 0.05, BKNG 0.05 (small). The volume-by-distance peak is at half a tick from mid (the best) for large-tick assets and shifts deeper as relative tick ε falls, following a power law with slope ≈ −1.513 in ε. Quartiles of liquidity distance from mid (ticks): large-tick ≈ 1 / 2.5–2.7 (so half of liquidity within ~2.5 levels); AAPL 1.8 / 3.8 / 6.5; AMZN 32.1 / 49.2 / 67.0; GOOG 31.2 / 47.6 / 66.9; BKNG 59.3 / 78.6 / 98.8. For small-tick assets "the top of the book is not necessarily the best place to post a passive limit order" because maximum liquidity lies much deeper; for large-tick assets liquidity is concentrated at the top. Mean trade-induced mid move is half a tick for large-tick names and scales ≈ ε^(−0.955) for the others. — [arXiv:2410.08744](https://arxiv.org/abs/2410.08744)

**Polymarket, 600-market stratified panel, 28-day window 28 Feb–27 Mar 2026, 547 markets with depth (Dubach, arXiv:2604.24366).** Median share of cumulative top-10 depth at each level: L1 0.1364 (p25 0.0800, p75 0.2055), L2 0.1034, L3 0.0945, L4 0.0806, L5 0.0824, L6 0.0789, L7 0.0818, L8 0.0821, L9 0.0767, L10 0.0830; median KL divergence from uniform 0.087 (a pure top-of-book book would be ≈2.30); 9% of markets have L1 > 0.5, 57% have L1 between 0.05 and 0.20. Quoted spreads by mid-price decile (bp, median): 0.0–0.1: 1,818; 0.1–0.2: 1,339; 0.4–0.6: 400; 0.8–0.9: 222; 0.9–1.0: 53 (paper is inconsistent on half- vs full-spread). Maker concentration: median HHI 0.031 (≈32 effective makers), p90 0.119 (≈8). — [arXiv:2604.24366](https://arxiv.org/abs/2604.24366)

**Bitcoin (Schnaubelt, Rende & Krauss 2019, BTC/USD):** limit order books are "relatively shallow and liquidity costs relatively high" compared with established markets; sides roughly symmetric. — [DOI 10.3390/jrfm12010025](https://doi.org/10.3390/jrfm12010025)

**Practitioner confirmation only: Amberdata, Binance BTC/FDUSD, 1 Jul–12 Aug 2025:** cumulative depth ≈ $3.50M within 10 bp of mid vs ≈ $16.53M within 100 bp, i.e., depth grows sub-linearly with distance (about 4.7× for 10× the distance). — [Amberdata blog](https://blog.amberdata.io/how-liquidity-really-works-in-crypto-markets)

### Inferences
- The hump arises mechanically from power-law placement plus a diffusing price that "eats" nearby orders (BMP 2002 model); its location in ticks scales inversely with relative tick size (Jain et al.), so in spread units the peak sits roughly at 1–2 spreads from mid for medium/small-tick assets and at the touch for large-tick assets. A size-per-level schedule expressed in spread units transfers across assets better than one in ticks.
- Competing makers collectively post little at the touch relative to a few ticks deeper (level-1 depth ≈ 14% of peak on CAC 40 2011; Gamma-distributed best-queue with shape γ < 1 means the best queue is frequently near-empty). The cross-section of resting size therefore rises with distance over the first few levels — the opposite of the exponential fill-intensity decay — which is consistent with makers sizing in proportion to the inverse of adverse-selection-adjusted fill risk rather than to fill probability.
- Polymarket's near-uniform profile (L1 ≈ 14%, L4–L10 ≈ 8% each) and 400 bp median spread indicate makers there spread size evenly across a coarse 1-cent grid rather than concentrating at the touch; with ~30 effective makers per market, it resembles a dealer-style ladder more than an equity LOB.

### Gaps
- No peer-reviewed study reports an empirical depth-vs-distance table for Binance/Bitmex crypto books; only vendor cumulative-depth figures were found (Amberdata), plus a qualitative "shallow" characterisation (Schnaubelt et al.).
- BMP 2002 does not state the hump's peak location in ticks (only the scaling forms); Potters & Bouchaud give shapes only in figures.
- The far-tail form of the depth profile remains unresolved in the literature (power law vs exponential vs lognormal) and is poorly measured because deep orders are often undisclosed.

---

## Key Question 5: Papers that calibrate A and k (or fill probabilities) directly from historical data and report values, incl. crypto and prediction markets

### Takeaway
The only peer-style source reporting fitted exponential-decay constants from real fills is arXiv:2607.28323 (Nasdaq 2016: k ≈ 0.48–3.7 per tick, decreasing with spread-in-ticks; FX 2026: k ≈ 0.49–0.62 per tick); Guéant–Lehalle–Fernandez-Tapia use A = 0.9 s⁻¹, k = 0.3 tick⁻¹ as illustrative values and note A, k must depend on the spread; crypto and prediction-market papers either tune γ with k estimated per interval (BTC-USD 2020–21, values not reported) or report fill-probability grids rather than (A, k).

### Cited Findings
- **Nasdaq 2016 / FX 2026 fitted k̂ per tick** (full table in Key Question 1): AMZN 0.48, TSLA 0.57, NFLX 0.86, ORCL 1.58, CSCO 3.72, MU 1.69; USDMXN 0.52, GBPUSD 0.49, AUDUSD 0.54, USDTHB 0.62, USDSGD 0.61; fitted by log Λ̂(δ) = log λ − kδ on fills / exposure-time per tick bin; the simulation uses k = 48 per dollar (= 0.48 per cent tick). — [arXiv:2607.28323](https://arxiv.org/abs/2607.28323)
- **Guéant, Lehalle & Fernandez-Tapia (2013):** "σ, A and k can be calibrated on trade-by-trade limit order book data while γ has to be chosen. However, it is well known by practitioners that A and k have to depend at least on the actual market bid-ask spread." Numerical illustrations use σ = 0.3 tick·s^(−1/2), A = 0.9 s⁻¹, k = 0.3 tick⁻¹, γ = 0.01 tick⁻¹, T = 600 s. — [arXiv:1105.3115](https://arxiv.org/abs/1105.3115)
- **Avellaneda–Stoikov form Λ(δ) = A e^(−kδ)** is adopted in GLFT and later work; a 2026 paper on robust market making rescales the fitted A by the simulator's empirical two-sided fill rate at the zero-inventory depth divided by the model's prediction, to correct for queue-priority effects (seen only in a search summary, arXiv:2609.11614; not verified in full text).
- **Large-tick Euronext (queue-reactive):** Poisson/state-independent intensities "clearly overestimate" execution probabilities relative to queue-dependent ones. — [arXiv:1312.0563](https://arxiv.org/abs/1312.0563)
- **Borsa Istanbul 2017:** exponential fits of quantity-by-tick are 2–3× worse (L1 error) than discrete Weibull or power law. — [arXiv:1909.08308](https://arxiv.org/abs/1909.08308)
- **BTC-USD L2, 7 Dec 2020–8 Jan 2021, 12 h/day (Falces Marín et al., PLOS ONE 2022):** AS liquidity parameter k is estimated from order-arrival counts in each interval (Aldridge's method; "no separate A parameter is needed"); σ from a window of w ticks; a genetic algorithm (450 individuals, Sharpe fitness) tuned γ = 0.624, w = 25, π = 1 min on 8 Dec 2020; no k values, fill rates or adverse selection are reported; mean Sharpe of the tuned AS baseline was −0.24. — [PMC9767337](https://pmc.ncbi.nlm.nih.gov/articles/PMC9767337/)
- **Binance BTC perp 2024 (Albers et al.):** fill probability of touch orders is modelled empirically as a function of near- and opposite-side queue sizes (OLS R² 0.946), not as A·e^(−kδ). — [arXiv:2502.18625](https://arxiv.org/abs/2502.18625)
- **Binance BTCUSDT Jan 2024 (Hawkes simulator):** trade-flow Hawkes with μ ≈ 4.1/s, branching ≈ 0.8; no (A, k). — [arXiv:2510.08085](https://arxiv.org/abs/2510.08085)
- **LMAX EUR/USD (Lokin & Yu):** rates are estimated nonparametrically per tick distance conditional on spread (rolling 5-day window); model-implied fill probabilities overshoot empirical ones by 1–2 percentage points at the touch and ~0.3–0.6 pp one tick deeper. — [arXiv:2403.02572](https://arxiv.org/abs/2403.02572)
- **Prediction markets:** a 2026 University of Tartu thesis applies a logit-transformed Avellaneda–Stoikov framework to Polymarket and estimates volatility and fill-intensity decay from data (search summary; the repository was unreachable, see Gaps). The Polymarket microstructure paper reports 255M OrderFilled events over 28 days and depth/spread profiles but no fill-intensity-by-distance fit. — [arXiv:2604.24366](https://arxiv.org/abs/2604.24366)

### Inferences
- Using the Nasdaq/FX k̂ values, 1/k (the "depth of the book" in ticks, as some authors read it) ranges from ~0.3 ticks for CSCO-type large-tick names to ~2 ticks for AMZN/TSLA/FX; in spread units 1/k ≈ 0.06–0.1 spreads for the small-tick names (AMZN 2.1 ticks / 36.7-tick spread) vs ≈0.3 spreads for large-tick (CSCO 0.27/1.1), so the exponential decay is much steeper relative to the spread in small-tick books.
- For crypto the practical state of the art in the literature is to estimate the touch fill probability as a function of queue state (Albers et al.) rather than a distance-decay constant; a practitioner should expect per-level data beyond the touch to be sparse because fills there are rare (LMAX: 0.2% one tick deeper).

### Gaps
- No paper found reports fitted (A, k) for Binance/Bitmex books; the PLOS ONE BTC paper estimates k but does not publish the values.
- The Tartu Polymarket AS thesis (dspace.ut.ee) could not be fetched (connection refused), so its fitted decay parameters are unverified.
- The Cartea–Jaimungal–Penalva NFLX fill-probability example with an exponential fit (textbook/notes) could not be fetched (certificate error); it is secondary material in any case.
- arXiv:2607.28323 reports no goodness-of-fit statistics for the exponential beyond stating narrow confidence intervals; whether the exponential breaks down at the touch (where queue position dominates) is not tested there.

---

## Key Question 6: Practical estimation of fill intensity per level from own fills and from the trade tape

### Takeaway
The literature converges on an exposure-time estimator — fills (shares or events) at distance δ divided by the time the quote was exposed at δ, binned by tick and conditioned on spread and queue state — with hypothetical-order replay against order-by-order data (tracking volume ahead in queue) as the standard way to generate fills when own fills are scarce; trade-tape proxies (counting trades that traverse a level) are used in FX but are warned to be poor proxies for actual executions.

### Cited Findings
- **Exposure-time estimator and log-linear fit:** Λ̂(δ) = N_fill(δ)/T_exp(δ) with δ = |mid − p|/tick, N_fill = executed shares, T_exp = tracked exposure seconds, both summed across days before dividing; then regress log Λ̂(δ) on δ. For FX with quote-only data, fills are synthetic: a hypothetical passive quote is "filled" when the next 10-s bucket's aggressor trades reach or cross it (books conflated at ≥55 ms). — [arXiv:2607.28323](https://arxiv.org/abs/2607.28323)
- **Spread-conditioned sample-average rates (CST-style):** λ̂^S(δ) = N_l^S(δ)/T^S (limit arrivals at distance δ while spread = S), μ̂^S = (N_m^S/T^S)·(S_m/S_l), θ̂^S(δ) = N_c^S(δ)/(T^S·Q̄_δ^S)·(S_c/S_l), where Q̄ is the average outstanding queue; rolling 5-day calibration gave MAPE ≈ 10% for mid-move probabilities. — [arXiv:2403.02572](https://arxiv.org/abs/2403.02572)
- **Queue-reactive estimation:** record for each queue event the waiting time since the previous event at that queue, the event type and the queue size before the event; normalise queue size by the average event size at that limit; estimate the total event rate per queue-size bucket and multiply by the empirical share of each event type; pool bid and ask at the same distance; K = 3 levels suffices for large-tick stocks. — [arXiv:1312.0563](https://arxiv.org/abs/1312.0563)
- **Hypothetical-order fill rule (Maglaras et al. 2021, used by Arroyo et al.):** it suffices to track the messages associated with orders in front of the hypothetical order in the queue and market orders arriving at or beyond the limit price; orders pegged outside the best are mostly censored. — [arXiv:2306.05479](https://arxiv.org/abs/2306.05479)
- **Artificial-order backtest on ITCH (Moallemi & Yuan):** insert artificial orders at random times, both "regular" (end of queue) and "touch" (front of queue), track them under FIFO through the historical message stream, and average fill probability, adverse selection (mid at a later time minus fill price) and order value over 30 trading days; the execution price is treated as a noisy unbiased estimate of fundamental value, so many observations are needed. — [Moallemi & Yuan PDF](https://drupalgsb-test.paas.cc.columbia.edu/sites/default/files-efs/pubfiles/25461/queue-value-2016.pdf)
- **Live-order grid (Albers et al.):** send minimum-size orders continuously (or periodically) at the touch, record near- and opposite-side queue sizes at submission, bin into a 20×20 grid to estimate fill probability, and compute 1-s and 5-s markouts per bin; continuous vs periodic quoting gave "essentially identical" results. — [arXiv:2502.18625](https://arxiv.org/abs/2502.18625)
- **Realized-fill definitions (Hollifield et al.):** execution probability = expected executed fraction of quantity over the order's life; discard orders in the last two sample days to limit censoring; exclude the first three minutes of the day. — [Hollifield et al.](https://www.cis.upenn.edu/~mkearns/finread/hollifield-liquid2.pdf)
- **Caveat on trade-tape proxies:** hypothetical executions constructed from first-passage times or from transactions data "are very poor proxies for actual limit-order executions" (Lo, MacKinlay & Zhang 2002). — [MIT page](https://web.mit.edu/~alo/www/Papers/limit10.html); Eisler et al. quantify the gap: the first-passage-time tail exponent ≈1.5 vs time-to-fill ≈1.8–2.2 and median TTF ∝ Δ^1.4 vs FPT ∝ Δ^1.5–1.8, with cancellations explaining the difference. — [arXiv:physics/0701335](https://arxiv.org/abs/physics/0701335)
- **Censoring/cancellation base rates to expect:** 99.9% of LMAX EUR/USD limit orders are cancelled; >99.99% of FX orders in 2013 data; QQQ execution rate ≈ 22% of cancel rate (2002). — [arXiv:2403.02572](https://arxiv.org/abs/2403.02572); [arXiv:1012.0349](https://arxiv.org/abs/1012.0349); [arXiv:cond-mat/0210710](https://arxiv.org/abs/cond-mat/0210710)
- **Markout horizon:** post-fill mid moves complete within ~1 s and do not revert over 30 s in ES (2025); Databento's NVDA example shows passive markouts turning negative after ~1 s. — [arXiv:2609.18019](https://arxiv.org/abs/2609.18019); [Databento](https://databento.com/docs/examples/algo-trading/execution-slippage)

### Inferences
- A practitioner's per-level calibration pipeline consistent with these sources: (1) bin own resting quotes by distance in ticks (and separately in spread units) and by spread state; (2) accumulate exposure seconds and executed quantity per bin to get Λ̂(δ); (3) fit log Λ̂ = log A − kδ but check the touch bin separately, where queue position, not distance, drives fills; (4) for levels with too few own fills, replay hypothetical orders through order-by-order data using the Maglaras/Arroyo rule, not the trade tape; (5) attach 1–5 s markouts per bin and compute order value = P(fill) × (price concession + rebate − markout) per level, following Moallemi–Yuan's decomposition.
- Trade-tape traversal (counting trades that cross a level) overstates fills for two reasons documented above: it ignores queue priority (volume ahead) and ignores that orders would have been cancelled before the price arrived (Eisler et al.'s TTF vs FPT gap). It is acceptable as an upper bound for deep levels where the queue-ahead volume is small relative to typical sweep sizes.

### Gaps
- No source provides a validated recipe for correcting trade-tape-based fill proxies to actual fill rates (a scalar "haircut"); arXiv:2609.11614's rescaling of A by the empirical two-sided fill rate is the closest, but I could only see it in a search summary.
- None of the sources discuss estimating k from partial fills or from fills of orders larger than the typical event size (all use unit or minimum-size orders).
