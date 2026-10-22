# Binary-option / prediction-market market making: arXiv sweep notes

Scope: how to quote binary contracts bounded in [0,1]. Sources read via WebFetch (HTML for 2607.17991, 2610.26789, 2602.19520; ar5iv for 1009.1446 and 1105.3115; abs page for the latter two for authors). Long HTML pages were read in chunks; the first 100k chars of 2607.17991, 2610.26789 and 2602.19520 were summarised, and the tail of 2607.17991 and 2610.26789 was the appendix/experiments section. Numbers below are as reported by the fetch summaries, not independently re-verified against PDFs.

## 2607.17991 - Optimal Market Making in Prediction Markets

- Data: none real. Pure model plus simulation, 10,000 paths, p0 = 0.5. Parameters explicitly "illustrative, not calibrated".
- Model: price p_t = f(L_t), L_t latent belief diffusion (logistic f => L = log-odds). Drift chosen so p is a martingale: dp = p(1-p) sigma_tilde(t,p) dW. Settlement pays 1 w.p. p_T. Fills are Poisson with state-dependent intensities, fixed size Delta, inventory in [-Q, Q]. Objective: cash + settlement value of inventory - gamma * integral q^2 sigma^2 ds, terminal penalty Phi = -gamma_T q^2 p(1-p). HJB reduces from (t,p,q,x) to (t,p,q) because cash is additive. Quotes withdrawn on the constrained side at q = +/-Q.
- Optimal quotes: maximisers of Lambda(G - z), where G^b = p - bid, G^a = ask - p, z = discrete inventory value difference (V(q)-V(q+/-Delta))/Delta. Existence and uniqueness proven under regularity assumptions.
- Table 1 params: sigma0 0.6, sigma1 0.3, sigma2 0.1, eta 3; A0 100, A1 150, xi 2; nu 1, k0 35, k1 50, kappa 1.5; gamma 4e-3, gamma_T 1e-3; Q 100, Delta 10, T 1; price grid [1e-3, 1-1e-3].
- Results (optimal vs myopic, mean PnL 12.39 vs 12.47, i.e. ~99.4% retained):
  - PnL std 10.34 vs 28.11 (-63%)
  - avg |terminal inventory| 15.23 vs 49.37 (-69%)
  - 5% VaR 4.20 vs 32.41 (-87%)
  - 5% ES 9.68 vs 40.50 (-76%)
- Qualitative: spread narrows over time but widens near settlement for p near 0.5 (settlement risk peaks at p(1-p) max). At zero inventory skew is positive for p<0.5 and negative for p>0.5, growing near 0/1. Inventory skew: long pushes quotes down, short pushes up, zero when flat. Higher gamma acts early; higher gamma_T acts near settlement.
- Actionable rule: add a settlement-risk term q^2 p(1-p) that bites as T-t shrinks; widen near p=0.5 into expiry; skew quotes by inventory and by distance of p from 0.5; keep a hard inventory cap and pull the capped side.
- Caveats: illustrative parameters, one scenario, intensity functional form assumed (authors say empirical form unstudied), myopic benchmark simulated without inventory bound, no fees/collateral yield/adverse selection beyond intensities.

## 2610.26789 - Cooperative MARL for Market Making on Prediction-Market Order Books

- Data: Polymarket L2 book updates (PMXT) re-encoded into synthetic order-level LOBSTER-style messages. Paper cites 417 real markets. Experiment section (as read): Polymarket BTC up/down 5-minute binary markets, final 5 minutes; 4 training days (22 and 28 Apr, 14 and 28 May 2026), held-out day 11 Jun 2026; the paper itself reports inconsistent held-out window counts (64, 48, 1,152). Note the fetch summary's experiment detail (4 training days, BTC 5-min markets) is narrower than the "417 markets" framing - check the PDF before citing the 417 as the test set.
- Setup: JaxMARL-HFT / JAX-LOB, PPO with 256-unit GRU, GAE, auxiliary mid-move forecast head at 3 horizons. Quoter (18 actions incl. no-trade, reach 13 ticks, one-sided quotes) plus risk manager (21 actions) as independent learners. Episode: 250 steps x 400 messages, 64 envs, Adam lr 2.5e-4, 3.2M env steps. Inventory cap 4 units (20 for PAPER_BASE). One seed per model.
- Held-out portfolio value (ticks): PAPER_BASE -46.1; WIDE_SPOONER -9.6; D_SPLIT -72.3; D_BLEND -41.0; D_MARK -5.9; D_BL_MARK_CURR -4.3. No configuration positive on average. D_MARK lost in 6 of 48 windows (+133 ticks aggregate); D_BL_MARK_CURR lost in 8 (+67 ticks aggregate); D_SPLIT lost in 45, D_BLEND in 39.
- Best configs have tiny volume (0.022-0.032 units/step) and ~20% no-trade, i.e. they win by trading less. Baseline JaxMARL-HFT maker chose no-trade 61.1%-97.7%.
- Reward lessons: bare split pays quoter for fills whose losses land on the risk manager (worst). Markout penalty (0.3 at 5-step horizon, ramped) helps most; blend 0.3 helps partly; annealed loss stop (240 -> 15 ticks) adds stability not level. Removing no-trade row just moves mass to widest quotes where nothing fills.
- Actionable rule: do not expect RL to beat a hand-built quote model out of the box; if you use any learner, charge it for post-fill markout, cap inventory hard, and measure adverse selection on fills.
- Caveats: single seed, one instrument family on one venue, synthetic queue positions/order IDs, trained against a learned execution agent, inconsistent evaluation-set descriptions.

## 2602.19520 - Decomposing Crowd Wisdom: Domain-Specific Calibration Dynamics

- Data: ~353M trades, ~429k binary contracts. Kalshi: 64.7M trades, 210,608 markets, cutoff 31 Dec 2025. Polymarket: 288.7M trades, ~218k resolved contracts (Sports, Crypto, Politics only). After 5-95c price filter: 58.7M Kalshi, 135.6M Polymarket trades.
- Method: logit P(yes) = a + b logit(p); b>1 underconfident (prices compressed toward 50%), b<1 overconfident. 6 domains x 9 horizon bins x 4 size bins = 216 cells (min 200 trades/cell). ANOVA-style decomposition.
- Variance: Kalshi in-sample 87.3% (adj R2 0.810); out-of-sample 71.5% leave-one-cell-out, 70.4% 10-fold CV. Leave-one-domain/size out performs poorly. Permutation null mean R2 0.329 (95th pct 0.406). Shares: horizon 30.2%, domain 14.6%, size 3.2%, domain x horizon 26.0%, domain x size 13.3%, residual 12.7%. Measurement-error model: structural components ~45.6% of observed variance (CrI 31-62%), so about half of raw slope variation is noise.
- Slopes: universal horizon effect 0.99 (0-1h) -> 1.32 (>1 month). Politics 1.34 (0-1h), 1.32-1.55 (3-24h), 1.83 (1wk-1mo), 1.73 (>1mo); Polymarket politics mean 1.45. Sports 0.90-1.10 within 48h, 1.24 at 1-4 weeks, 1.74 beyond a month. Crypto ~0.99-1.07 short, 1.36 >1 month. Weather 0.69-0.97 within 48h (overconfident), 1.20-1.37 longer. Entertainment 0.81-1.11. Politics domain intercept +0.107 [0.062, 0.152]; Weather -0.072.
- Politics size effect (Kalshi): Single 1.19 vs Large (>100) 1.74, delta +0.53 (trade-level CI [0.29, 0.75]; event-clustered [0.12, 1.80]). Sports delta +0.07 (n.s.). Politics ECE 0.117, all other domains <= 0.022. Isotonic at raw 0.75: Politics 0.886, Weather 0.691.
- Rule: fair value p* = sigmoid(a + b logit(p)) with b by domain x horizon bucket; example 70c politics a week out, b ~ 1.83, a ~ 0 gives ~0.83. Use only for fair-value skew, shrink toward b=1 for noisy cells.
- Caveats: descriptive not causal; event-clustered errors ~50x naive; Polymarket has ~3h timestamp noise and 3 domains; Politics 1-3h dip is composition (Trump Administration markets 63% of trades there); fees ~1.2-1.3c/contract; coarse domain labels; the fitted slope is on traded prices, so applying it to mid quotes is an extrapolation. Also note b>1 on trade prices says takers' prices are conservative, which is exposure for a maker only if you can identify which side is informed.

## 1009.1446 - Comparing Prediction Market Structures, With an Application to Market Making

- Authors: Brahma, Das, Magdon-Ismail (Sep 2010).
- Data: 6 live sessions, 11-17 participants each (mostly grad students), 10 min each; two markets on independent gambler's-ruin random walks (horizontal, vertical); MM type swapped across markets between experiments. Plus 1,000-run simulations of 200 steps (jump prob 0.01, mean trade size 20).
- Setup: LMSR b = 125 (loss bound b ln2 ~ 8,664). BMM: Gaussian belief, initial mu 50, sigma 12, sigma_eps 5, window W = 5 (first two sessions) / 10 (rest); doubles variance when recent trades look inconsistent.
- Simulation (BMM / LMSR): Gaussian shocks profit 2081 / -2457, RMSD 2.92 / 5.38, spread 1.42 / 1.35; uniform shocks profit 603 / -1898, RMSD 8.78 / 10.79, max loss 50,184 / 8,384.
- Live: BMM profit positive in 5 of 6 sessions (e.g. +47,232 Equilibrium(1), +20,226 IndivInfoShock; -10,589 Equilibrium(4)); LMSR lost in all six (-92 to -3,169). RMSD after convergence BMM 0.93-1.00 in LimitedInfo/Equilibrium(5) vs LMSR 14.56/8.15; BMM worse in Equilibrium(4) (17.05 vs 14.82).
- Rule: an information-based MM that widens on surprise and narrows on confirmation gets better price stability and lower loss than an inventory-only MM, but you need your own loss cap. For a [0,1] book: widen spread when recent fills are inconsistent with current belief (variance-doubling), and keep a hard stop since loss is unbounded.
- Caveats: 2010, non-binary (0-100 random-walk value) experimental markets, student participants, small sample (6 sessions), BMM spread sometimes widens in stable periods (instability), window size is a tradeoff (larger = stabler but slower).

## 1105.3115 - Dealing with the Inventory Risk (Gueant, Lehalle, Fernandez Tapia)

- Data: none; model plus illustrative simulations/backtest remark (gamma chosen so inventory stayed within -10..+10 trade-size units on the studied day).
- Model: dS = sigma dW; intensity lambda(delta) = A exp(-k delta); inventory q in [-Q, Q] unit trades; CARA utility -exp(-gamma(X_T + q S_T)), horizon T. HJB becomes linear ODE system in v_q(t), v_q(T) = 1; alpha = (k/2) gamma sigma^2, eta = A(1+gamma/k)^-(1+k/gamma).
- Exact quote: delta^b*(t,q) = (1/k) ln(v_q/v_{q+1}) + (1/gamma) ln(1+gamma/k); ask symmetric.
- Asymptotic closed form: delta^b(q) ~ (1/gamma) ln(1+gamma/k) + ((2q+1)/2) * sqrt( sigma^2 gamma / (2kA) * (1+gamma/k)^(1+k/gamma) ); delta^a(q) ~ (1/gamma) ln(1+gamma/k) - ((2q-1)/2) * same sqrt. Spread ~ (2/gamma) ln(1+gamma/k) + sqrt(...), independent of q in the approximation.
- Example parameters: sigma 0.3-1.0, A 0.2-0.9, k 0.3-0.9, gamma 0.01, T 600 s.
- Comparative statics: higher sigma^2 widens spread and increases inventory skew; higher A narrows spread; higher k narrows spread; gamma effect ambiguous. Market impact xi per fill adds xi/2 to each quote and scales skew by e^(k xi/4). Drift mu shifts both quotes by -/+ mu/(gamma sigma^2) * sqrt(...).
- Rule: quote half-spread + linear-in-q skew with calibrated A, k from fill data; set gamma by the inventory band you tolerate rather than by theory.
- Caveats: arithmetic Brownian price on unbounded line, not bounded [0,1]; approximation weakest at large |q|; fixed unit trade size; no settlement. On a [0,1] contract replace sigma by sigma_p = p(1-p)*sigma_L (from 2607.17991) and add settlement term.

## What this means for the bot

1. Reference quote structure: Gueant-Lehalle-Fernandez Tapia gives a calibratable template (A, k from your own fills; sigma from price changes; gamma set by an inventory band). Port it to probability space by using local price volatility sigma_p = p(1-p) sigma_L, which shrinks toward 0 and 1 (2607.17991 martingale diffusion).
2. Add what the GLFT model lacks for binaries: the settlement penalty gamma_T q^2 p(1-p), which grows near expiry and at p near 0.5; widen and cut size into expiry near 0.5; and clamp quotes to [tick, 1-tick] with the capped side withdrawn. In the paper this keeps ~99% of mean PnL while cutting VaR(5%) ~87% and terminal inventory ~69% versus myopic.
3. Fair-value skew: apply a domain x horizon calibration map p* = sigmoid(a + b logit p) from 2602.19520 (politics b ~ 1.3-1.8, short-horizon sports/crypto ~1, weather near expiry < 1) but shrink toward b = 1 because half of slope variance is noise and event-clustered CIs are very wide. Use as a skew on the reservation price, not as a standalone alpha.
4. Do not train a model-free RL maker as the main strategy: the MARL study found no positive held-out portfolio value (best -4.3 ticks, single seed); its good results come from trading less. If you use learning, penalise markout, cap inventory, and benchmark against a no-trade baseline.
5. Adaptive spread (1009.1446): widen after fills inconsistent with the current belief and tighten when consistent; and enforce a hard per-market loss limit since BMM-style quoting has no bounded-loss guarantee (worst case ~50k vs LMSR bound ~8.7k in simulation).
6. Validation order: backtest on recorded L2 with adverse-selection (markout) accounting; fit A, k per market class; check spread/skew behaviour near 0, 1 and expiry; paper-trade small before size.
