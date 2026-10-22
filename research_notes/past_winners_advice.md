# Past winners / top finishers advice - research notes (as of 2026-10-22)

## Headline finding
There are NO past winners to learn from for the Susquehanna Predictions Cup: the 2026 Midterm Elections
competition (Oct 1 - Nov 4 2026 noon ET) is the inaugural season, still running, final standings only after all
markets settle. No SIG-run earlier student trading/forecasting competition with published winner write-ups was found
(SIG poker events exist - CardPlayer, 34st.com - but no strategy write-ups). thesuper.market ran an earlier "$5K Contest"
(a market on the site mentions a contest/leaderboard by 2026-07-01) but no results or winner posts found.
Everything below is therefore (a) hard facts about the live contest, (b) live leaderboard evidence, (c) self-reported,
UNVERIFIED in-progress competitor repos, (d) empirical microstructure research, (e) weak comparables.

## A. Verified contest facts (primary sources)
- Rules: https://predictionscup.com/rules/  (fetched)
  - Score = verified total SUSQies balance after ALL markets resolve (not mark-to-market at Nov 4). Leaderboard provisional.
  - Prizes: 1st up to $30,000, 2nd $5,000, 3rd $2,500 -> massively top-heavy (~80% to 1st). Ties split pooled prizes, no tiebreak.
  - Unlimited bots allowed via API, one account per person; all bot activity is the participant's own. Sponsor not
    obliged to fix bot order errors; server records control. Sponsor "may act as market maker", no obligation.
  - Sponsor may change a market's rules/outcome/resolution at any time incl. after resolution date.
  - No mention markets. Insider/MNPI trading prohibited; staff/advisers of a candidate can't trade that market.
  - Winners must submit eligibility form in 72h, docs in 5 days, else next-ranked replaces.
- Docs: https://sig.thesuper.market/docs/faq , /docs/markets-and-trading , /docs/settlement-and-payouts ,
  /docs/portfolio-and-leaderboards , /docs/changelog (fetched)
  - Cash accounts only, no margin/leverage. Balance can't go negative. Limit and market orders. Prices 0-1, tick 0.005.
  - Buying opposite side nets against held shares and pays 1 SUSQie per cancelled pair immediately.
  - Self-trade prevention: crossing remainder of own order is cancelled.
  - API limits: 100 reads + 30 writes /min per account (shared across keys per one repo's reading).
  - Fees: none stated (FAQ says free); position limits not stated.
  - Void/cancel/N-A markets: positions refunded at refundable cost (so a void erases projected gain).
  - Automatic markets (elections/politics) checked every 4h; the 4 chamber-control markets are settled MANUALLY by an
    admin once control is established and stay closed to trading until then; runoffs settled by admins when official.
    => final standings can lag well after Nov 4; settlement-timing/tail risk is real.
  - Changelog Oct 14: org admins can correct a settled market's YES/NO result for a tournament after it ends (payout
    corrections exist, API `correction` entries Oct 17). So post-settlement score changes are possible.
  - Changelog Oct 17: multi-leg order endpoint can return 502 ORDER_STATUS_UNKNOWN; retry with same idempotency key.
  - Oct 18 trader-metric change: gains/losses from shares sold before settlement now count in trader stats.

## B. Live leaderboard evidence (primary, fetched 2026-10-22; https://sig.thesuper.market/leaderboards)
1,748 traders. Account value change vs 100k start / trades:
 1 kind-brook-6861 +974.2k / 4,706 trades | 2 bright-finch-0400 +886.3k / 16,838 | 3 steady-finch-4087 +861.2k / 24,390
 4 nimble-beacon-4953 +806.7k / 4,720 | 5 bright-badger-3145 +572.3k / 2,511 | 6 noble-birch-8618 +550.5k / 37,132
 7 merry-coral-9766 +490.2k / 5,532 | 8 calm-badger-5206 +449.9k / 920 | 9 +319.6k / 3,188 | 10 +264.4k / 2,073
 11 +239.4k | 12 +236.0k / 11,884 | 13 +232.1k / 299 trades | 14 +220.3k | 15 +192.7k | 20 +108.9k / 698 trades
Observations (my inference): the top 4 are already at 8-10x the starting bankroll while published bot-builders sit at
+5% to +15% (rank ~31 to ~365). Trade counts range 299 to 37k, so both very-low-frequency concentrated bettors and
high-frequency bots appear in the top 20. Top profiles are private (kind-brook-6861 "not publicly visible";
bright-finch-0400 shows only P&L 886,257). Unknown whether these gains are mark-to-market on longshots/favourites
that have not settled (grissou notes leaderboard marks carry the tilt) - so the leaders may be holding large
unsettled directional positions. The gap between rank 1 and rank 31 is far larger than any edge a riskless/MM strategy
earns, which is consistent with a heavy-tailed, top-prize-takes-all contest.

## C. In-progress competitor repos (SELF-REPORTED, UNVERIFIED, not winners)
1. github.com/grissou/prediction-market-maker (already known; re-read 2026-10-22). Snapshot 20 Oct 21:58 UTC: account
   104,786 (+4.8%), EV at settlement 114,487 (+14.5%), rank 187/1,727. Said: ladder harvest ~15% and allocator ~11%
   of EV per unit cash; market making earned "a few hundred" because many competing bots left little spread; momentum
   sleeve was a mistake (sold 12.9k of good positions, turned off in 2 days); mark-driven selling on 4 Oct gave up
   ~3.7k EV in 4h; leaderboard marks carry the favourite-longshot tilt so can show losses settlement won't; Rhode Island
   shorts hit 27k in one night -> per-state cap 15k; reduce-only above 40% of account in one state; 80 req/min,
   28 writes/min budget; WebSocket book-change channel lost 12 Oct.  Newer than earlier notes: the 20 Oct numbers.
2. github.com/JosephWong333/prediction-market-bots - self-reported #31 of 1,403 on Oct 10 (final after races resolve).
   Real-money liquidity-reward bots on Kalshi/Gemini/Polymarket US plus a Cup bot; strategy for the Cup is private.
   Quote: "fills on their own lose money; the liquidity rewards carry the bot" (Kalshi) - i.e. pure spread capture is
   negative without a subsidy. Reconcile modelled vs actual payouts.
3. github.com/benjamingao93/SIG-prediction-bot - no results. Design: (i) multi-outcome arb: buy NO on every party in a
   race when YES bids sum >1, as one atomic multi-leg order, exit when >=0.0025/set locked; (ii) directional vs
   Kalshi-plus-ratings fair value (log-odds blend 70% Kalshi), quarter-Kelly, min edge 0.02 + uncertainty, stop when fair
   value drops 0.03, max 500/race live, 2,000 cash reserve; Kalshi books filtered to spread<=0.04. Own caveats: paper fills
   ignore queue position; fundamentals model "knows nothing about specific candidates" so biggest gaps vs market are
   candidate effects already priced; order books are YES-normalized; every order must carry tournamentId else it hits the
   public book; WS can drop last update on quiet markets -> REST reload every 90s.
4. github.com/IamYanis/Susquehanna-Prediction-Cup-Strategy - riskless YES/NO pair scanner. No results. Findings: in a 7 Oct
   audit 117 title pairs produced zero approvable pairs; an 8 Oct paired test left first leg UNKNOWN. Resting orders do not
   reserve cash on the platform; cancelling doesn't undo filled shares; fusion-ticket rule (Rhode Island) breaks naive
   party pairs; cancellation/N-A refunds can erase projected gains. Principle: "Projected profit is not realized cash".
5. github.com/georgeilici05/prediction-cup-bot - R/D parity scanner + arb executor + early-exit recycler; caps 40k total,
   2k per contest. No results.
6. github.com/ZVogel1/SIG-PM-Challenge-F2026 - goal "maximize chance of finishing #1"; edge_hunter/constraint_arb/
   risk_manager; "tournament Kelly" (slightly above classic Kelly, capped per race and per basket), lottery tickets avoided
   (FLB), 20% drawdown from peak halves buy size. No results. Notes honestly that it doesn't discuss #1 vs top-3 variance.
7. github.com/Lnvqntx/Susquehanna-Predictions-Cup - self-reported rank 365/1,403 (+0.8%) on Oct 12; target top 3; 15-pt min edge
   from one external source; limit orders only; fully deployed bankroll -> building "portfolio rotation". Illustrates
   the failure mode of a single un-calibrated external forecast and capital being fully tied up.
   (Others: Pranallar/Midterm-Predicitons read-only; DanielCWeng/predictions-cup research only; vincal848, andrewgaur WIP;
    sam-bateman/sig-bot and SamuelRegan-dev/Prediction-Cup returned 404.)

## D. Empirical microstructure evidence (peer-quality-ish; relevant to sleeves)
- Becker, "Microstructure of Wealth Transfer in Prediction Markets" https://www.jbecker.dev/research/prediction-market-microstructure
  (Kalshi, 72.1M trades, $18.26B): 5c contracts won 4.18% (-16% mispricing); all <20c underperform, all >80c outperform;
  takers -1.12%/trade, makers +1.12%; NO beats YES at 69/99 price levels; at 1c YES EV -41% vs NO +23%. Maker-taker gap by
  category: Politics only ~1.0pp (Finance 0.17, Sports 2.2, Entertainment 4.8, World Events 7.3). Crucial regime note: before
  2024 Q2 the sign was REVERSED (takers +2%, makers -2%) and flipped as pro market makers arrived; author notes caveats
  (maker/taker is a proxy; US venue).  => longshot tilt exists on real-money Kalshi, but politics has the smallest maker edge.
- arXiv 2606.04217 (Polymarket v1, 1.2B trades) https://arxiv.org/html/2606.04217v1 : favourite-longshot REVERSAL on
  Polymarket - deciles 0-0.3 mean return -0.2 to -0.3c, 0.7-0.9 about +0.8 to +1.0c. Same sign as classic FLB but tiny
  (<1c per share). Preprint, not vetted.  => the tilt edge in real venues is ~1c/share; the Cup's own book may differ a lot
  (many play-money bots; grissou measured tilt s ~ 0.05-0.13).
- Burgi & Whelan, "Makers and Takers: Economics of the Kalshi Prediction Market" (karlwhelan.com/Papers/Kalshi.pdf): PDF
  unreadable via fetch; abstract per search: makers earn more than takers, both show FLB pattern. Not verified here.

## E. Comparable forecasting competitions (weak/indirect)
- Astral Codex Ten "AI superforecasters are here" (https://www.astralcodexten.com/p/the-ai-superforecasters-are-here):
  Metaculus Cup top places mixed humans (benshindel, MarcosO) and bots (Preseen, manticAI); "competitions carry heavy luck, a
  single tournament result is not decisive"; best humans/bots "too close to tell"; a human can rarely deploy more than a few
  thousand dollars before an inefficiency closes (relevant: capacity). Not a final-balance scoring game.
- ACX "Who Predicted 2023?" - Blind Mode winners largely unknown/lucky; aggregation beats nearly all individuals. Indirect.
- Good Judgment Project: supers are better calibrated (79% closer to perfect calibration), update in small increments,
  GJ Open crowd overconfident at 0-20% and >50% (company analysis, geopolitics not elections).  Indirect.
- Manifold: Joel Becker interview (news.manifold.markets/p/interview-with-1-leaderboard-bettor): topped leaderboard by
  manipulating a market he controlled; irrelevant to us except: expect copycats and hedge distant outcomes; he held too much
  too long (late bets). Manifold league (profit-in-ranked-markets, monthly seasons) - no winner write-ups found.
  Salem tournament thread (manifold.markets/BionicD0LPH1N/...): tips - rank bets by (own prob - price) gaps via script,
  layered staggered limit orders, spread over many markets to reduce impact; a moderator said bots probably not allowed there.
- Kalshi news "Competition breeds legends" (2026-09-13): marketing only, no winners/strategy.
- IMC Prosperity (algo trading comp, final-PnL scoring): only LinkedIn snippets - "understanding market mechanics mattered
  more than black-box models"; no write-ups retrieved.
- Cross-venue (blogs, low quality): the hedge is only a hedge if resolution wording/source/timing is identical; Polymarket UMA
  disputes, different settlement times, fees. No 2024-election primary write-up found.

## Not found (searched, nothing)
SIG/Susquehanna past student-competition winner interviews; any Predictions Cup winner/top-10 LinkedIn/blog/Reddit/YouTube post;
Kalshi/Polymarket-run trading competitions with published winner advice; GJ Open top forecaster posts on election-market
calibration (only generic GJ material); Manifold league champion strategy posts.
