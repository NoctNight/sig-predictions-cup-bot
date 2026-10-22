
# Prediction-market tournaments / competitions research (written 2026-10-22)
Budget at stop: 5-hour limit 1% used, weekly all-models 0% used (checked at start, after ~5, ~10, ~15, ~17 calls). ~19 searches + ~12 fetches.
Labels: [V]=verified from fetched primary/near-primary source; [S]=self-reported/secondary/news; [I]=my inference.

## 0. Headline
No published winner write-ups exist for any Kalshi/Polymarket-style play-money tournament (searched Kalshi, Polymarket, Manifold,
Susquehanna, IMC Prosperity, Jane Street/CMU, Illinois PredictionLabs, Binghamton). The Cup itself (Oct 1 - Nov 4 2026, 100k SUSQies,
$30k to 1st, final balance) is the inaugural season: https://secure.businesswire.com/news/home/20260922468550/en/Susquehanna-Predictions-Launches-Predictions-Cup-for-University-Students (search snippet only; fetch 403).
Evidence below is therefore (a) 2024 real-money whale cases, (b) academic microstructure papers, (c) tournament-design theory, (d) format comparables.

## 1. Polymarket 2024 whales (real money)
- Theo/Fredi9999 network: 4 accounts ~ $47-48M profit on ~$45M wagered (Bloomberg/Decrypt, Nov 2024); Chainalysis-based later estimate up to 11 accounts, ~$85M. Theo4 alone ~$21M on $19.2M. [S, journalism]
  https://decrypt.co/290419/mystery-prediction-market-whale-47-million-trump
  https://news.bloomberglaw.com/crypto/new-analysis-boosts-trump-whales-polymarket-haul-to-85-million
  https://cointelegraph.com/news/top-polymarket-whale-profits-20-m-trump-victory
- How: ONE highly concentrated directional view (Trump win), ~$30-52M deployed (majority of his liquid assets, self-reported to WSJ), spread over the winner market ($22M) AND correlated side markets (popular vote, PA etc, $26M). Reported edge = privately commissioned "neighbor polls" (who do you think your neighbors vote for) - self-reported, pollster results never shared. Added size progressively (5 accounts $6.4M -> $52M by end Oct; $20M of YES on Oct 18). [S]
  https://fortune.com/2024/11/02/french-whale-polymarket-30-million-donald-trump-election-bet-kamala-harris
- Other: zxgngl +$11M on an $18M Trump position; largest Harris holder (leier) -$5M. Outcome was one binary: concentration won and lost on a single realisation. [S]
- Takeaway: the top-1 outcomes in real-money election markets were single-factor concentrated bets, not arbitrage/MM. Same shape as top of Cup leaderboard (8-10x).

## 2. Arbitrage on Polymarket (peer-ish, unreviewed preprint, AFT 2025)
- Saguillo, Ghafouri, Kiffer, Suarez-Tangil (IMDEA), arXiv 2508.03474, Apr 2024-Apr 2025, 86M bets: ~$40M realised arbitrage profit; top 3 wallets $4.2M on >10,200 bets; typical arb trade 1-5% [V via DL News + arXiv]
  https://arxiv.org/pdf/2508.03474  https://www.dlnews.com/articles/markets/polymarket-users-lost-millions-of-dollars-to-bot-like-bettors-over-the-past-year/
- Two types: within-market rebalancing (sum of outcomes != $1) and combinatorial/cross-market (logically dependent markets). Politics/2024 presidential was the most profitable category; sports had more opportunities but smaller profits. One trade: bought YES+NO for <$0.02 each, ~$59k. [V]
- Implication [I]: riskless set arbs are real but small per trade (1-5%) and capital/size-limited; at 8-10x leaders they won't win alone. Combinatorial (dependent-market, e.g. chamber control vs. individual seats, state vs. national) is the under-exploited extension.

## 3. Kalshi academic evidence (maker/taker, favourite-longshot)
- Burgi, Deng, Whelan "Makers and Takers" (CESifo/ifo/UCD WP; VoxEU summary), >300k Kalshi contracts: avg pre-fee return ~ -20% (~ -22% post-fee); takers ~ -32%, makers ~ -10%; contracts <10c lose >60% of money (5c contract winning 2% loses 60%); 95c contract wins ~98%. Bias holds across politics, volume, trade size; a true 20% is perceived ~23%; accuracy improves near expiry; some evidence bias diminishing over time. [V via VoxEU; working paper not peer reviewed]
  https://cepr.org/voxeu/columns/economics-kalshi-prediction-market  https://www.ifo.de/en/cesifo/publications/2026/working-paper/makers-and-takers-economics-kalshi-prediction-market
- Implication [I]: supports the longshot/favourite tilt sleeve and passive (maker) posture; but in a PLAY-MONEY contest taker "optimism" may be stronger (no real loss aversion), and the leaderboard marks the tilt (see existing grissou notes).

## 4. Kalshi leaderboard / competitions
- Kalshi's own leaderboard is opt-in performance tracking (https://help.kalshi.com/trading/leaderboard); bracket-style contests exist with consolation prizes. No published trading-competition winner strategies found. Kalshi blog post (Tarek Mansour) has no data. [V - nothing there]

## 5. Manifold leagues
- Monthly seasons; score = profit on ranked markets only (+ creator unique-trader bonus); divisions Bronze-Masters + bot-only "Silicon"; Bronze top 10 promote (top 2 skip), Masters ~60% demote monthly; one prize per user per season; prizes withheld/recalled for market manipulation, refunding counterparties, coordinated trading across accounts. [V] https://manifold.markets/community-guidelines/leagues
- Takeaway [I]: rank-based, short-season, profit-only structure => no published "winner playbook"; anti-collusion/alt rules are strict; Cup rules similarly say one account per person.

## 6. Illinois / university comparables
- Illinois PredictionLabs (SIGecom/SIGPolicy), spring 2026: 100 SigCoin start, AMM-priced binary contracts at 50c start, ~3 weeks, $800 prize pool + $200 at each of 3 live events; ranking rule undefined; no results. [V] https://banks.acm.illinois.edu/articles/predictionlabs
- CMU MSCF Jane Street-sponsored Market Making Game 2026 (teams of 6, estimation/probability contracts) - page 404 on fetch, no winner info. IMC Prosperity (72h rounds, algo + manual, separate manual-winner prize) - only a solo Medium write-up and IMC interview found; no manual-round variance analysis. https://medium.com/@amjadsaidama/i-competed-in-the-hardest-trading-challenge-on-the-planet-imc-prosperity-4-5cfbf161f203

## 7. Tournament-design theory (relevant to top-3-prize, final-balance scoring)
- Winner-take-all contests: theory predicts contestants choose maximal risk; leaders tend to "play safe", trailers gamble (field evidence from mutual funds mixed; some find leaders take more risk). Higher reward inequality raises performance dispersion. [S - search snippets of academic papers, e.g. arXiv 2409.06248]
- EA Forum: top-X prize objective differs from maximising accuracy; extreme/contrarian outcomes favoured; distortion stronger with few questions; luck dominates small pools (top-3 of 200 over 10 questions). https://forum.effectivealtruism.org/posts/ztmBA8v6KvGChxw92 [V, opinion post]
- arXiv 1903.02131 "A Prediction Tournament Paradox" (Aldous): weaker contestant often wins by luck via mean-variance trade-off, also in top 10. [S via snippet, PDF unreadable]
- Dota-2 Polymarket concentration: top 10% of accounts supplied 97.8% of deposits [S, financemagnates] - volume concentrates in few wallets.

## 8. 2026 midterm market structure (directly relevant)
- ACDC (Aug 17 2026): top 1% of Polymarket wallets = 68% of congressional-market volume; 10 wallets = 17% of volume, active in 426/470 seats; 87% of markets thin (<$10k or very few traders); 4 in 5 Polymarket markets <100 wallets; platforms opened 16x as many midterm markets as 2024. Authors conclude markets are manipulable/untested. [V, fetched] https://acdatacollective.org/?p=1662
- 2022 midterms precedent: PredictIt/Polymarket priced a Republican sweep (Polymarket Senate GOP ~65%); outcome much narrower, Dems held Senate. Markets overpriced consensus "wave" narrative. [S, news] https://theconversation.com/a-republican-bubble-how-pollsters-and-pundits-got-the-us-midterms-so-wrong-194684
- 2024 election night: reports of displayed liquidity vanishing before fills, up to ~$0.60/share slippage (low-quality source) [S]; Polymarket at 95% Trump by 11:43pm ET, ~6h before AP call [S]. https://www.onebullex.com/news/articles/polymarket-kalshi-traders-overpaid-0-60-per-share-as-election-odds-vanished-before-fills

## Not found
Winner interviews/strategy for Kalshi/Polymarket-run trading contests; Polymarket/Kalshi crypto tournaments with published results; Robbins World Cup data; Chainalysis profitable-trader share; election-night MM profit figures; Manifold league winner playbooks.
