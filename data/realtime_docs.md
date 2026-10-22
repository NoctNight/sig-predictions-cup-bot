# Realtime feed documentation (extracted from /api/v1/openapi.json)

## Realtime (WebSocket) feeds

Polling `GET /orders` and `GET /portfolio/*` in a tight loop is slow and the fastest way to hit the rate limit. Use Realtime WebSocket feeds for low-latency updates (versioned order books on tournament markets, invalidation hints elsewhere), then reconcile decisions against the authoritative REST reads.

Realtime runs on Supabase Realtime **private broadcast** channels. Payloads are server-signed. Market and account changes are grouped into one-second batches that retain the actual trade, fill, and order payloads while sharply reducing message count. Tournament market batches also carry `books`: the full order book of each exchange they name, versioned by `asOf`, so a bot can keep a book current without refetching it. Each batch carries `revision` and `previousRevision` and covers the topic's revisions after `previousRevision` through `revision`, normally exactly one. Each batch is sent once: a stale, oversized, or undeliverable batch is dropped, not delayed. A batch whose `revision` is at or below the last accepted one is a duplicate. A batch was missed only when `previousRevision` is greater than the last accepted revision: refetch the corresponding REST resources before acting. A batch flagged `resyncRequired` could not carry all its content, because it was too large or its order books could not be read, and also requires a refetch; such batches can arrive for many markets at once, so coalesce and rate-limit those refetches. Engine sequence bounds are provenance, not the topic gap counter. Delivery remains best-effort, so reconnects, token refreshes, and socket errors also require a refetch. The final batch on a quiet topic can be lost with no later gap to reveal it, so also refetch periodically (for example every one to two minutes). There are three things a bot typically wants:

- **Live market batches** — `market:{exchange_id}` (or `market:{market_id}:combined`) emits `market_batch`
  with `trades[]`, `bookDirty[]`, and `marketSettled[]`. Each market-settlement item is `{ marketId, tournamentId, settledWith, at }`, where `settledWith` is the market's stamped settlement value and is `REFUND` for refunds. Every trade carries `id` and `sequence`, the same id `GET /exchanges/{id}/trades` gives it. Any minted token can read it. Tournament markets publish the same batch on the tournament-gated `tournament:{tournament_id}:market:{market_id}` instead, adding the versioned `books` of the exchanges each batch names. The tournament's markets topic, `tournament:{tournament_id}:markets`, carries those same batches for every market of the tournament that changed, as `markets_batch` `{ markets: [{ marketId, batch }] }`, so a bot following many markets joins one channel instead of one per market.
- **Your own account batches** — `user:{profile_id}` emits `account_batch` with `fills[]`, `orderUpdates[]`, `settlements[]`, and `refunds[]`, plus a standalone `notification` event `{ notificationId, type }` whenever an in-app notification is created for you. Private: the token carries `realtime_profile_id`, and the user channel is authorized against that claim.
- **Leaderboard invalidation** — `leaderboard:all` emits a `{ at }` `tick` after the global standings are recomputed, with no revision tracking; debounce it, then refetch `GET /leaderboards`. Any minted token can read it, like the market channels.

The full channel matrix and every event payload live under the **Realtime** tag (`POST /realtime/token`).

### Setup

One authenticated call returns everything needed to connect — no separate Supabase credentials to configure:

```bash
curl -s -X POST "$BASE/realtime/token" -H "Authorization: Bearer $KEY"
# → { "token": "<token>", "expiresAt": "<timestamp>", "supabaseUrl": "<url>", "anonKey": "<anon-key>", "channels": { "user": "user:<profile_id>" } }
```

- `supabaseUrl` + `anonKey` → the connection (`createClient(supabaseUrl, anonKey)`).
- `token` → the realtime access token. Every channel requires the minted token: authorize **once** with `setAuth(token)` before subscribing to any channel. The `anonKey` only opens the connection; a join made without the token is refused.
- `channels.user` → your ready-made `user:{profile_id}` channel name.

`supabaseUrl` is the Realtime server for your account and may not be a `*.supabase.co` address. Always connect with the `supabaseUrl` and `anonKey` that `POST /realtime/token` returns, never with hard-coded values, and read them again whenever your bot restarts.

Tokens expire after **3 hours** — call `POST /realtime/token` again before expiry and `setAuth` the new token to refresh. A channel the server closed (after a token expired, say) stays closed: `setAuth` alone never brings it back, so re-create it, and any channel whose join was refused, after applying the fresh token. Which tournament channels a token can join is decided when the token is minted, so a change to your access (joining or leaving a tournament, a ban, an archived tournament, or a new organization default) takes effect at your next token refresh.

### Connect and subscribe

```ts
import { createClient } from "@supabase/supabase-js";

const response = await fetch("/api/v1/realtime/token", {
  method: "POST",
  headers: { Authorization: `Bearer ${API_KEY}` },
});
if (!response.ok) throw new Error(`Realtime token request failed: ${response.status}`);
const { token, anonKey, supabaseUrl, channels } = await response.json();
if (!token) throw new Error("Realtime token response carried no token");

const supabase = createClient(supabaseUrl, anonKey); // one long-lived client per process
await supabase.realtime.setAuth(token); // every channel requires the minted token

// A refused join is not transient: remove the channel instead of letting the client retry it.
const isRefusedJoin = (err) => /Unauthorized|Too many channels/.test(err?.message ?? "");
// Channels the server closed or refused (an expired token, say) stay gone: re-create them after the next setAuth.
const channelsToRecreate = [];
const subscribe = (makeChannel, onSubscribed) => {
  const channel = makeChannel();
  let refused = false;
  channel.subscribe((status, err) => {
    if (status === "SUBSCRIBED") return onSubscribed(); // initial state and every rejoin: resync over REST
    if (status === "CHANNEL_ERROR" && isRefusedJoin(err)) {
      refused = true;
      supabase.removeChannel(channel); // stop retrying until the token or channel count is fixed
      channelsToRecreate.push(() => subscribe(makeChannel, onSubscribed)); // a fresh token may fix it
      return onRefusedJoin(channel, err);
    }
    if (status === "CLOSED" && !refused) channelsToRecreate.push(() => subscribe(makeChannel, onSubscribed));
    // A CHANNEL_ERROR or TIMED_OUT from the transport is transient: the client rejoins on its own.
  });
};

// Before the 3 hour expiry: mint a fresh token (checking response.ok and token as above), apply it,
// then re-create every channel the server closed or refused meanwhile.
const refreshRealtimeToken = async () => {
  const freshToken = await mintRealtimeToken();
  await supabase.realtime.setAuth(freshToken);
  for (const recreate of channelsToRecreate.splice(0)) recreate();
};

// React to every account change in the batch.
const accountChannel = () => supabase
  .channel(channels.user, { config: { private: true } })
  .on("broadcast", { event: "account_batch" }, ({ payload }) => {
    payload.fills.forEach(onFill);
    payload.orderUpdates.forEach(onOrderUpdate);
    payload.settlements.forEach(onSettled);
  });
subscribe(accountChannel, resyncAccountFromRest);

// Keep a tournament market's books and trade tape current.
let lastRevision = null;
const marketChannel = () => supabase
  .channel(`tournament:${tournamentId}:market:${marketId}`, { config: { private: true } })
  .on("broadcast", { event: "market_batch" }, ({ payload }) => {
    (payload.books ?? []).forEach(applyBookIfNewer); // by asOf, even on a duplicate revision
    const { revision, previousRevision } = payload.delivery;
    // Check resyncRequired before anything else: a compact one carries no arrays.
    if (payload.resyncRequired) {
      if (lastRevision === null || revision > lastRevision) lastRevision = revision;
      return resyncFromRest();
    }
    if (lastRevision !== null && revision <= lastRevision) return; // duplicate
    const missed = lastRevision !== null && previousRevision > lastRevision;
    lastRevision = revision;
    if (missed || payload.trades.some((trade) => trade.sequence === null)) return resyncFromRest();
    payload.trades.forEach(onTrade);
  });
subscribe(marketChannel, resyncFromRest);
```

### Refused joins and connection limits

- Check `response.ok` and a non-empty `token` before calling `setAuth`: an error response carries no token, and without one every join is refused.
- The server refuses a join with `CHANNEL_ERROR` when the token is missing or does not authorize the topic (`Unauthorized`), or when the connection already holds 100 channels (`Too many channels`). A refusal does not fix itself, but by default supabase-js re-sends a refused join after 1, 2 and 5 seconds, then every 10 seconds, for as long as the socket stays open. On a refused join, call `removeChannel` and do not subscribe to that channel again until the token or the channel count is fixed. A `CHANNEL_ERROR` from a dropped socket or network error is transient: let the client reconnect on its own, then resynchronize over REST.
- Use one long-lived client per process and refresh its token with `setAuth`. One connection carries at most 100 channels, so subscribing to every market of a large tournament on one socket overflows it: to follow every market, subscribe to `tournament:{tournament_id}:markets` instead; otherwise subscribe only to the markets you need, or spread the topics over more connections.

`price` in every `trades[]` item, and in every `fills[]` item where it is not null, is the **YES-denominated** execution price in `[0, 1]` — one consistent series regardless of which side traded. For a NO position the effective per-share cost is `1 - price`. The per-trade and per-order events (`trade`, `fill`, `order_update`) are retired; the batches carry them. Some admin and settlement actions still send `book_dirty`, `market_settled`, `position_settled`, and `refund` individually, with the same payload as the matching batch item; see the Realtime token endpoint. A fill against your resting order appears in `fills[]` with no `orderUpdates[]` entry; `orderUpdates[]` reports orders placed or cancelled in the window.

### Keeping a book in sync

1. Subscribe to `tournament:{tournament_id}:market:{market_id}`. Once subscribed, read `GET /markets/{id}/orderbook?tournamentId=...&depth=200`; each exchange's book carries its own `asOf` version `{ sequence, at }`.
2. For each pushed book, replace the book you hold for that exchange when the pushed `asOf` is newer: compare `sequence`, then `at`, and keep the higher. A held book with a null `asOf` was served without an engine version, so any pushed book replaces it. Books apply even when their batch's revision is a duplicate.
3. Read the REST orderbook again after a revision gap, a batch flagged `resyncRequired`, a trade whose `sequence` is null, or a reconnect, token refresh, or socket error, and periodically while subscribed.
4. Each pushed book carries `nextExpiryAt`, the soonest expiry among its resting orders. On engine-routed scopes the expiry sweep pushes the changed book a few seconds after it; refetch over REST if you want it sooner. Other scopes announce no expiry, so refetch that exchange's book after `nextExpiryAt`.

`market:*` topics carry no books: refetch the books their `bookDirty[]` entries name.

---



# POST /realtime/token

Mints a short-lived (3 hour) JWT that authorizes the caller for Realtime private channels. The token carries a `realtime_profile_id` claim, and the `user:{profile_id}` channel is authorized against that claim. Tokens minted for organization-bound API keys also carry `realtime_org_id` and `realtime_key_org_id`; the latter limits tournament-channel authorization to tournaments in the key's organization. It does not change access to the caller's user channel or to the market and leaderboard topics, which any minted token can read. Which tournament channels a token can join is decided when the token is minted, so a change to your access (joining or leaving a tournament, a ban, an archived tournament, or a new organization default) takes effect at your next token refresh.

Realtime is a **best-effort low-latency feed**, not the financial source of truth and not a replayable event log. Engine market and account changes are coalesced into one pending batch per topic, published once its first change's one-second window closes, so normal flow creates at most one batch per topic per second. Each batch is sent once and never retried. A batch that is stale, oversized, or undeliverable is dropped, not delayed, except that on a tournament market topic an oversized batch, or one whose order books could not be read, is sent flagged `resyncRequired` instead. The arrays still contain every trade, fill, and order change the batch covers, except in a batch flagged `resyncRequired` (see below).

Every `market_batch` and `account_batch` has `delivery: { model, correlationId, revision, previousRevision, sourceSequenceFrom, sourceSequenceThrough }`, where `model` is always `"best-effort-authoritative-resync"`, and covers its topic's revisions after `previousRevision` through `revision`, normally exactly one. Keep the last accepted revision per topic. A batch whose `revision` is at or below it is a duplicate: ignore its arrays (its `books` still apply, see below). A message was missed only when `previousRevision` is greater than the last accepted revision: perform authoritative REST resynchronization before acting. Otherwise accept the batch and advance the last accepted revision to its `revision`. `delivery.correlationId` identifies the published batch for support diagnostics. `sourceSequenceFrom` and `sourceSequenceThrough` identify the engine-event range that produced the batch, but they are provenance, not a topic-local gap counter. A dropped batch shows up only as a gap in the next batch on its topic, so the final batch on a quiet topic can be lost with no gap to reveal it. Clients MUST therefore resynchronize on initial subscription, reconnect, token refresh, socket error, a revision gap, or a batch flagged `resyncRequired`, and periodically while subscribed (for example every one to two minutes). A missed broadcast never changes durable engine state or reporting history.

Every channel requires the minted token. Apply it as the Realtime access token before subscribing to any channel — `supabase.realtime.setAuth(token)` in supabase-js or `await supabase.realtime.set_auth(token)` with the asynchronous supabase-py client. The `anonKey` only opens the connection; a join made without the token is refused.

`supabaseUrl` is the Realtime server for your account and may not be a `*.supabase.co` address. Always connect with the `supabaseUrl` and `anonKey` that `POST /realtime/token` returns, never with hard-coded values, and read them again whenever your bot restarts.

#### Channels

All channels are Realtime **private** channels: subscribe with `{ config: { private: true } }` after `setAuth(token)`. The market and leaderboard channels admit any token minted by this endpoint. Tournament and user channels add the rules in each channel's **Auth**. No channel accepts client broadcasts — payloads are server-signed via the service role, so a subscriber cannot forge a batch or tick.

**`market:{exchange_id}`**

- **Auth:** Requires the minted token — any token from this endpoint, applied with `setAuth`.
- **Events:** `market_batch`; also `book_dirty`, `market_settled` (see below)
- **Payload:** `{ trades[], bookDirty[], marketSettled[], delivery }` for the changes the batch covers.

**`market:{market_id}:combined`**

- **Auth:** Requires the minted token — any token from this endpoint, applied with `setAuth`.
- **Events:** `market_batch`; also `book_dirty`, `market_settled`
- **Payload:** Same as above. Lets a multi-outcome market page subscribe once for every contract; demux on `exchangeId`.

**`tournament:{tournament_id}:market:{market_id}`**

- **Auth:** Private — authorized by the tournament in the topic, and denied when that tournament is archived. The membership path, which also denies an actively banned profile, admits any profile token for an open tournament, enrolled members of a private tournament, and owners or admins of the tournament's organization (every tournament of that organization, before they join; service-account profiles do not get this owner/admin shortcut). When the tournament belongs to an organization, each of them must also be a member of that organization. An organization's **default** tournament is additionally readable by any token carrying that organization's `realtime_org_id` — see below. When `realtime_key_org_id` is present, every tournament channel is further restricted to that organization. These rules are applied when the token is minted, so a change reaches the token at its next refresh.
- **Events:** `market_batch`; also `book_dirty`, `market_settled`
- **Payload:** Same arrays for one market in that tournament, with `tournamentId` on each item, plus `books`: the versioned order book of every exchange the batch names (see below). Every exchange of a multi-outcome market shares the topic; demux on `exchangeId`. Not mirrored to global `market:*` channels. The bare `tournament:{tournament_id}` topic no longer carries market updates; to follow every market of a tournament, use `tournament:{tournament_id}:markets` below.

**`tournament:{tournament_id}:markets`**

- **Auth:** Private — exactly the rules of `tournament:{tournament_id}:market:{market_id}` for the tournament in the topic, the organization-default `realtime_org_id` path and the `realtime_key_org_id` restriction included.
- **Events:** `markets_batch`; also `book_dirty`, `market_settled`
- **Payload:** `{ markets: [{ marketId, batch }] }`: the batches of the tournament's markets that changed, each `batch` exactly the `market_batch` payload its market topic carries, with its own `delivery`. See below.

**`leaderboard:all`**

- **Auth:** Requires the minted token — any token from this endpoint, applied with `setAuth`.
- **Events:** `tick`
- **Payload:** `{ at }` dirty signal, sent directly rather than in a revisioned batch, so it has no revision or gap tracking — emitted when the global standings have been recomputed, not when the trade that dirtied them executed, so a refetch on `tick` reads the new standings rather than the ones it replaced. It therefore lags the matching fill by a few seconds, and consecutive moves coalesce into one tick. Subscribe regardless of which period you display and call `GET /leaderboards?period=1d|7d|30d|quarter|all` to refetch.

**`user:{profile_id}`**

- **Auth:** Private — `realtime_profile_id` must match the channel profile id.
- **Events:** `account_batch`; also `position_settled`, `refund` (see below), and `notification`
- **Payload:** `account_batch`: `{ fills, orderUpdates, settlements, refunds, delivery }` for the changes the batch covers. `notification`: `{ notificationId, type }`, sent on its own (outside `account_batch`, with no revision) each time an in-app notification is created for the profile.

`price` on every `trades` item, and on every `fills` item where it is not null, is the **YES-denominated** execution price in `[0, 1]` — a single consistent series regardless of which side traded. For a NO position the effective per-share cost is `1 - price`.

#### `market_batch`

- `trades` contains the actual executions in the window. Each item is `{ id, sequence, exchangeId, marketId, price, quantity, executedAt, tournamentId? }`.
- `id` (a string) and `sequence` (a number) identify the match: its engine event sequence, which is also the same id `GET /exchanges/{id}/trades` gives the trade, so one trade seen on both can be deduplicated and ordered. Rarely, the engine records a fill without a match event of its own; then `id` and `sequence` are null, the trade cannot be ordered against other reads, and the client must resynchronize that exchange's price and trade tape over REST.
- `bookDirty` contains `{ exchangeId, marketId, tournamentId, at }` signals for books whose resting depth changed. On `market:*` topics, refetch those books with `GET /markets/{id}/orderbook`; on tournament market topics the batch carries them in `books`.
- `marketSettled` contains `{ marketId, tournamentId, settledWith, at }` signals when a market scope settles or refunds.
- `settledWith` is the market's stamped settlement value: the winning outcome label for a settlement, or `REFUND` for a refund.

A `marketSettled` signal comes from the root engine settlement/refund event, so it fires even when the exchange has zero holders and therefore produces no per-user entries.
Treat it as an invalidation hint and refetch the authoritative market state.

An order that rests unfilled or is cancelled changes `bookDirty` without adding a trade. Book signals carry no private order details.

`bookDirty` and `marketSettled` entries can repeat for the same scope within a batch: settlement signals are deduplicated per exchange but carry no `exchangeId`, so a multi-outcome market can report one `marketSettled` per settling exchange. Treat both as invalidation hints and coalesce them before refetching.
A `bookDirty` entry may arrive in the same batch as a trade for the same order; refetch the book once.

**Order expiry.** On engine-routed scopes the engine closes expired orders in a sweep that runs every few seconds, and each expiry emits the same signals as a cancel: `bookDirty` (with `books` on tournament market topics) and an `orderUpdates` entry with `open: false` on `user:{profile_id}`. Expect them a few seconds after `expirationDate`, not exactly at it. Other orders are filtered at read time and emit nothing, so on `market:*` topics for those scopes a client that has stopped polling should set a timer to the earliest `expirationDate` it knows of and refetch then.

#### Versioned books on tournament market topics

On `tournament:{tournament_id}:market:{market_id}`, a `market_batch` whose `trades` or `bookDirty` name an exchange also carries `books`: one complete order book per named exchange, ascending by `exchangeId`. Each is `{ exchangeId, asOf: { sequence, at }, nextExpiryAt, bids: [{ price, quantity }], asks: [{ price, quantity }] }`, with `exchangeId` as a number. Levels use the same YES-normalized `{ price, quantity }` objects as the REST orderbooks: total resting depth from every participant, your own orders included, bids best (highest) first and asks best (lowest) first, with no depth cap. Exchanges the batch does not name carry no book and have not changed through it; a batch that names none (settlement only) has no `books`. `market:*` topics never carry books.

- `asOf` is the engine version the book was read at: the last engine event applied (`sequence`) and the engine clock (`at`). Books are read when the batch is published, so a book can be newer than the batch's own trades, and the books in one batch can have different versions.
- Keep one version per exchange and compare `asOf` by `sequence`, then `at` as an instant. Apply a pushed book only when it is newer than the book you hold, and keep the higher. Apply a batch's `books` even when its revision is a duplicate: a version never goes backwards, so a repeated book is harmless.
- `nextExpiryAt` is the soonest expiry among the book's resting orders, or null when none expires. The expiry sweep pushes the changed book a few seconds after `nextExpiryAt`; a client that wants it sooner can refetch over REST.

**Keeping a book in sync.** Subscribe, and once subscribed read the REST orderbook (`GET /markets/{id}/orderbook` or `GET /exchanges/{id}/orderbook` with the topic's `tournamentId`, and `depth=200` for the full depth the stream pushes); each exchange's book there carries its own `asOf`. Then apply every pushed book whose `asOf` is newer than the one you hold; a held book with a null `asOf` was served without an engine version, so any pushed book replaces it. Read the REST orderbook again after a revision gap, a batch flagged `resyncRequired`, a trade whose `sequence` is null, a reconnect or token refresh, after an exchange's `nextExpiryAt`, and periodically while subscribed.

#### `resyncRequired`

On a tournament market topic, a batch that cannot carry all its content is sent with `resyncRequired: true` and its usual `delivery`. A batch too large for the 768 KiB realtime message size limit keeps only what fits: its `books`, then as many of its `trades` as fit, in order, and no `bookDirty` or `marketSettled`. When even the books do not fit, or the batch's order books could not be read, it is the compact form `{ resyncRequired: true, delivery }`, with no arrays at all. This is the one exception to "the arrays contain every trade". Check `resyncRequired` before reading any array: apply the `books` it carries by version, advance the last accepted revision as usual, then resynchronize that market over REST. A failed book read flags every market of the tournament that changed in that second, so coalesce these recoveries and rate-limit them, for example to one REST resynchronization per market every 30 seconds.

#### `markets_batch` on a tournament's markets topic

`tournament:{tournament_id}:markets` gathers, into one feed, the batches of every market in the tournament that changed. Following many markets of a tournament, subscribe to it instead of the per-market topics, and do not hold both for the same markets: you would receive every batch twice. Following only a few markets, stay on their per-market topics: the markets topic delivers every changed market's batch, including the ones you do not follow.

- `markets` lists `{ marketId, batch }` entries, one per market whose batch was published, in publication order. `marketId` is the market id as a string. `batch` is exactly the `market_batch` payload published on `tournament:{tournament_id}:market:{market_id}`, with the same `trades`, `bookDirty`, `marketSettled`, `books` and `delivery`.
- Revisions stay per market: keep the last accepted revision **per `marketId`** and apply each entry's `delivery` by the rules above, and its `books` by `asOf`. The topic has no revision of its own.
- A message is not a snapshot of the tournament. It carries only markets that changed, and one round of changes can span several messages, each within the 768 KiB realtime message size limit. A market with no changes sends nothing.
- An entry's `batch` can be flagged `resyncRequired` like any batch, including the compact `{ resyncRequired: true, delivery }` when the entry alone would not fit in a message or its market's order books could not be read. Handle it as above, for that market.
- The `book_dirty` and `market_settled` events that admin and settlement actions send individually on a tournament market topic arrive here too, unbatched and with the same payload. Treat them as invalidation hints for the market they name.

Resynchronize every market you follow over REST on the occasions listed above: initial subscription, reconnect, token refresh, socket error, a revision gap or `resyncRequired` in that market's entry, and periodically.

#### Organization default tournaments are surface-scoped

An organization's configured default tournament follows the same host-relative rule as the REST surfaces: it is **public on that organization's own subdomain** and **members-only on the main platform**. When no default is configured, the legacy global tournament is the fallback. Tokens carry which surface applies.

- A token minted from a request to the organization's subdomain carries a `realtime_org_id` claim, and that claim alone authorizes `tournament:{tournament_id}:market:{market_id}` for every market of that organization's default tournament. No identity is required, mirroring the fact that a logged-out visitor to that subdomain can already read the same market, prices, book and candles over REST.
- A web token minted anywhere else carries no `realtime_org_id`, so the same channel falls back to the membership gate: the caller must be a member of the organization.

This `realtime_org_id` entitlement applies **only** to the current default tournament, including a finite tournament when configured. Every other tournament, including a legacy global tournament that is no longer the default, stays membership-gated on every surface.

Organization-bound API keys receive both `realtime_org_id` and `realtime_key_org_id` set to the key's organization. The key-org claim is a restriction across all tournament channels: even when the profile could personally read another organization's tournament, this token cannot. Organization-unbound API keys receive neither claim and retain the profile-based behavior. The restriction does not affect `user:{profile_id}`, `market:*`, or `leaderboard:all` topics.

**Load-bearing invariant:** *an organization's default tournament is readable by anyone who can reach that organization's subdomain.* For web tokens, the org claim is minted from the host the caller arrived on and lives for the token's full 3 hours. The default tournament the token grants is the one configured when it was minted, so a change of default reaches the client at its next token refresh. The token is proof of arrival, arrival is unrestricted, and the same data is readable over REST by reloading the subdomain.

If org subdomains ever become login- or membership-gated, this invariant breaks and **both** the RLS policy and the token minting must change together.

#### `account_batch` payload arrays

- `fills` — one item per executed trade row for the user. `{ orderId, exchangeId, marketId, price, quantity, executedAt, tournamentId }` `orderId` is null when the fill cannot be attributed to an order, and `price` is null when the fill carried no price; resynchronize that order or position over REST.
- `orderUpdates` — the post-execution state of each order placed or cancelled in the window. `{ orderId, exchangeId, marketId, open, quantityTraded, totalCost, latestTradePrice, tournamentId, at }` A resting order that only fills produces a `fills` entry and no `orderUpdates` entry; track its remaining size from your fills, or read it with `GET /orders/{id}`. `quantityTraded` and `totalCost` count only fills in the same window, so the closing update for an order that filled earlier and then expired or was cancelled reports 0; reconcile lifetime totals with `GET /orders/{id}/fills`. Entries carry no terminal reason: an expiry and a cancel look the same, and the order's `terminalReasonCode` from `GET /orders/{id}` tells them apart.
- `settlements` — per (user, exchange), including losses with `payout: 0`. `{ marketId, exchangeId, outcomeSide, shares, costBasis, payout, realizedPnl, settlementOutcome, tournamentId, at }` `costBasis`, `realizedPnl`, and `settlementOutcome` are null for engine-recorded settlements; `outcomeSide` is always set.
- `refunds` — per (user, exchange) when a market resolves to `REFUND`. `{ marketId, exchangeId, shares, refundAmount, tournamentId, at }`

The per-trade and per-order events `trade`, `fill`, and `order_update` are retired; the batches carry them. Some admin and settlement actions still send `book_dirty` and `market_settled` individually on the market channels, and `position_settled` and `refund` individually on `user:{profile_id}`. Each of those payloads has the same shape as the matching `bookDirty`, `marketSettled`, `settlements`, or `refunds` batch item, so handle it the same way. A settlement outcome correction sends a further `position_settled` for each affected member, with the corrected outcome and payout; it replaces the earlier settlement rather than adding to it.

#### Refused joins and connection limits

- Check `response.ok` and a non-empty `token` before calling `setAuth`. An error response carries no token, and without one every join is refused.
- The server refuses a join with `CHANNEL_ERROR` when the token is missing or does not authorize the topic (`Unauthorized`), or when the connection already holds 100 channels (`Too many channels`). A refusal does not fix itself, but by default supabase-js re-sends a refused join after 1, 2 and 5 seconds, then every 10 seconds, for as long as the socket stays open. Subscribe with a status callback; on a refused join, call `removeChannel` and do not subscribe to that channel again until the token or the channel count is fixed. A `CHANNEL_ERROR` from a dropped socket or network error is transient: let the client reconnect on its own, then resynchronize over REST.
- A channel the server closed (after your token expired, say) stays closed: supabase-js drops it, and `setAuth` alone never brings it back. After applying a fresh token, re-create every channel the server closed, and every channel whose join it refused.
- Use one long-lived client per process and refresh its token with `setAuth`; do not open a new client per subscription. One connection carries at most 100 channels, so subscribing to every market of a large tournament on one socket overflows it. To follow every market of a tournament, subscribe to `tournament:{tournament_id}:markets` instead; otherwise subscribe only to the markets you need, or spread the topics over more connections.

Subscribe pattern (example in supabase-js):

```ts
// One call returns everything needed to connect: supabaseUrl, anonKey (connection apikey), and token (realtime access token).
const response = await fetch('/api/v1/realtime/token', { method: 'POST', headers: { Authorization: `Bearer ${API_KEY}` } })
if (!response.ok) throw new Error(`Realtime token request failed: ${response.status}`)
const { token, anonKey, supabaseUrl } = await response.json()
if (!token) throw new Error('Realtime token response carried no token')

// One long-lived client per process.
const supabase = createClient(supabaseUrl, anonKey)

// Every channel requires the minted token: apply it before the first subscribe.
await supabase.realtime.setAuth(token)

// A refused join is not transient; supabase-js would otherwise re-send it every 10 seconds.
const isRefusedJoin = (err) => /Unauthorized|Too many channels/.test(err?.message ?? '')
// Channels the server closed or refused (an expired token, say) stay gone: re-create them after the next setAuth.
const channelsToRecreate = []
function subscribe(makeChannel, onSubscribed) {
  const channel = makeChannel()
  let refused = false
  channel.subscribe((status, err) => {
    if (status === 'SUBSCRIBED') return onSubscribed() // initial state and every rejoin: resync over REST
    if (status === 'CHANNEL_ERROR' && isRefusedJoin(err)) {
      refused = true
      supabase.removeChannel(channel) // stop retrying until the token or channel count is fixed
      channelsToRecreate.push(() => subscribe(makeChannel, onSubscribed)) // a fresh token may fix it
      return onRefusedJoin(channel, err)
    }
    if (status === 'CLOSED' && !refused) channelsToRecreate.push(() => subscribe(makeChannel, onSubscribed))
    // A CHANNEL_ERROR or TIMED_OUT from the transport is transient: the client rejoins on its own.
  })
}

// Before the 3 hour expiry: mint a fresh token (checking response.ok and token as above), apply it,
// then re-create every channel the server closed or refused meanwhile.
async function refreshRealtimeToken() {
  const freshToken = await mintRealtimeToken()
  await supabase.realtime.setAuth(freshToken)
  for (const recreate of channelsToRecreate.splice(0)) recreate()
}

// Tournament market batches — tournament gate; carry versioned books. One last accepted revision per market.
const lastRevision = new Map()
function onMarketBatch(marketId, payload) {
  // Books apply by version (asOf.sequence, then asOf.at), even on a duplicate revision.
  for (const book of payload.books ?? []) applyBookIfNewer(book)
  const { revision, previousRevision } = payload.delivery
  const last = lastRevision.get(marketId) ?? null
  // Check resyncRequired before anything else: a compact one carries no arrays.
  if (payload.resyncRequired) {
    if (last === null || revision > last) lastRevision.set(marketId, revision)
    return resyncMarketFromRest(marketId)
  }
  if (last !== null && revision <= last) return // duplicate
  lastRevision.set(marketId, revision)
  if (last !== null && previousRevision > last) return resyncMarketFromRest(marketId)
  for (const trade of payload.trades) {
    if (trade.sequence === null) return resyncMarketFromRest(marketId)
    onTrade(trade)
  }
}

// Following a few markets: one tournament market topic each.
const marketChannel = (marketId) => () => supabase
  .channel(`tournament:${tournamentId}:market:${marketId}`, { config: { private: true } })
  .on('broadcast', { event: 'market_batch' }, ({ payload }) => onMarketBatch(String(marketId), payload))
subscribe(marketChannel(marketId), () => resyncMarketFromRest(String(marketId)))

// Following many markets: the tournament's markets topic instead, never alongside their market topics.
const marketsChannel = () => supabase
  .channel(`tournament:${tournamentId}:markets`, { config: { private: true } })
  .on('broadcast', { event: 'markets_batch' }, ({ payload }) => {
    for (const { marketId, batch } of payload.markets) onMarketBatch(marketId, batch)
  })
  // Individually sent hints arrive unbatched on the same topic.
  .on('broadcast', { event: 'book_dirty' }, ({ payload }) => refetchBook(payload.exchangeId))
  .on('broadcast', { event: 'market_settled' }, ({ payload }) => resyncMarketFromRest(String(payload.marketId)))
subscribe(marketsChannel, resyncFollowedMarketsFromRest)

// User feed — realtime_profile_id must match the channel profile id
const userChannel = () => supabase
  .channel(`user:${profileId}`, { config: { private: true } })
  .on('broadcast', { event: 'account_batch' }, ({ payload }) => {
    for (const fill of payload.fills) onFill(fill)
    for (const order of payload.orderUpdates) onOrderUpdate(order)
  })
subscribe(userChannel, resyncAccountFromRest)
```

Subscribe pattern (async supabase-py):

```python
import asyncio

import httpx
from realtime import RealtimeSubscribeStates
from supabase import acreate_client

async with httpx.AsyncClient() as http:
    response = await http.post(f"{base_url}/realtime/token", headers={"Authorization": f"Bearer {api_key}"})
response.raise_for_status()
body = response.json()
token = body.get("token")
if not token:
    raise RuntimeError("Realtime token response carried no token")

supabase = await acreate_client(body["supabaseUrl"], body["anonKey"])
await supabase.realtime.set_auth(token)  # every channel requires the minted token

channel = supabase.channel("market:42", {"config": {"private": True}})

def on_status(status, err):
    refused = err is not None and ("Unauthorized" in str(err) or "Too many channels" in str(err))
    if status == RealtimeSubscribeStates.CHANNEL_ERROR and refused:
        # Stop retrying until the token or the channel count is fixed.
        asyncio.create_task(supabase.remove_channel(channel))

await channel.subscribe(on_status)
```

Following every market of a tournament (async supabase-py):

```python
last_revision = {}  # last accepted revision per market id

def on_market_batch(market_id, batch):
    for book in batch.get("books", []):
        apply_book_if_newer(book)  # by asOf, even on a duplicate revision
    revision, previous = batch["delivery"]["revision"], batch["delivery"]["previousRevision"]
    last = last_revision.get(market_id)
    if batch.get("resyncRequired"):  # check first: a compact one carries no arrays
        if last is None or revision > last:
            last_revision[market_id] = revision
        return resync_market_from_rest(market_id)
    if last is not None and revision <= last:
        return  # duplicate
    last_revision[market_id] = revision
    if last is not None and previous > last:
        return resync_market_from_rest(market_id)
    for trade in batch["trades"]:
        if trade["sequence"] is None:
            return resync_market_from_rest(market_id)
        on_trade(trade)

def on_markets_batch(message):
    for entry in message["payload"]["markets"]:
        on_market_batch(entry["marketId"], entry["batch"])

markets = supabase.channel(f"tournament:{tournament_id}:markets", {"config": {"private": True}})
markets.on_broadcast("markets_batch", on_markets_batch)
markets.on_broadcast("book_dirty", lambda message: refetch_book(message["payload"]["exchangeId"]))
markets.on_broadcast("market_settled", lambda message: resync_market_from_rest(str(message["payload"]["marketId"])))

def on_markets_status(status, err):
    refused = err is not None and ("Unauthorized" in str(err) or "Too many channels" in str(err))
    if status == RealtimeSubscribeStates.SUBSCRIBED:
        asyncio.create_task(resync_followed_markets_from_rest())  # initial state and every rejoin
    elif status == RealtimeSubscribeStates.CHANNEL_ERROR and refused:
        asyncio.create_task(supabase.remove_channel(markets))

await markets.subscribe(on_markets_status)
```

Tokens expire after 3 hours. Call this endpoint again before expiry to refresh, apply the new token with `setAuth`, then re-create any channel the server closed or refused meanwhile. A refresh also picks up any change to your tournament access. Each call counts as one write against the per-account rate budget.


## Response schema

{
 "200": {
  "description": "Token minted.",
  "content": {
   "application/json": {
    "schema": {
     "type": "object",
     "properties": {
      "token": {
       "type": "string",
       "example": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
      },
      "expiresAt": {
       "type": "string",
       "example": "2026-06-14T14:00:00.000Z"
      },
      "supabaseUrl": {
       "type": "string",
       "example": "https://example.supermarket.com",
       "description": "This account's Realtime server, not always a *.supabase.co host \u2014 createClient(supabaseUrl, anonKey). Use the returned value, not a stored one."
      },
      "anonKey": {
       "type": "string",
       "example": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJyb2xlIjoiYW5vbiJ9.example-signature",
       "description": "Anon key (a JWT) \u2014 pass as the connection apikey (createClient second arg). Use the returned value, not a stored one."
      },
      "channels": {
       "type": "object",
       "properties": {
        "user": {
         "type": "string",
         "example": "user:00000000-0000-0000-0000-000000000001"
        }
       },
       "required": [
        "user"
       ]
      }
     },
     "required": [
      "token",
      "expiresAt",
      "supabaseUrl",
      "anonKey",
      "channels"
     ]
    }
   }
  }
 },
 "500": {
  "description": "Server misconfigured (missing Supabase URL, anon key, or JWT secret), or the token could not be minted.",
  "content": {
   "application/json": {
    "schema": {
     "$ref": "#/components/schemas/RealtimeError"
    }
   }
  }
 }
}


## Related component schemas
{
 "CombinedOrderbookExchange": {
  "type": "object",
  "properties": {
   "exchangeId": {
    "type": "string",
    "example": "36"
   },
   "option": {
    "type": [
     "string",
     "null"
    ],
    "example": "YES"
   },
   "asOf": {
    "$ref": "#/components/schemas/BookVersion"
   },
   "impliedProbability": {
    "type": [
     "number",
     "null"
    ],
    "example": 0.55,
    "description": "Best ask price, or latestPrice if no asks exist. Null when no tournament price or ask exists."
   },
   "bids": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/OrderbookLevel"
    }
   },
   "asks": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/OrderbookLevel"
    }
   }
  },
  "required": [
   "exchangeId",
   "option",
   "asOf",
   "impliedProbability",
   "bids",
   "asks"
  ]
 },
 "BookVersion": {
  "type": [
   "object",
   "null"
  ],
  "properties": {
   "sequence": {
    "type": "integer",
    "example": 4817,
    "description": "The last engine event applied to the book."
   },
   "at": {
    "type": "string",
    "example": "2026-09-28T09:15:02.125+00:00",
    "description": "The engine clock when the book was read."
   }
  },
  "required": [
   "sequence",
   "at"
  ],
  "description": "The engine version an exchange's book was captured at, the same `asOf` the realtime book push carries. Versions compare by `sequence`, then `at`. Null when the book was read from the database, which has no version."
 },
 "OrderbookLevel": {
  "type": "object",
  "properties": {
   "price": {
    "type": "number",
    "example": 0.55
   },
   "quantity": {
    "type": "number",
    "example": 600
   }
  },
  "required": [
   "price",
   "quantity"
  ]
 },
 "MarketOrderbookContext": {
  "allOf": [
   {
    "$ref": "#/components/schemas/MarketReadContextDescriptor"
   },
   {
    "type": "object",
    "properties": {
     "orderbook": {
      "$ref": "#/components/schemas/CombinedMarketOrderbook"
     }
    },
    "required": [
     "orderbook"
    ]
   }
  ]
 },
 "CombinedMarketOrderbook": {
  "type": "object",
  "properties": {
   "exchanges": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/CombinedOrderbookExchange"
    }
   },
   "overround": {
    "type": [
     "number",
     "null"
    ],
    "example": 1.05,
    "description": "Sum of implied probabilities across all outcomes. Null when any outcome has no implied probability."
   },
   "hasArbitrageOpportunity": {
    "type": "boolean",
    "example": false,
    "description": "True when this is a mutually exclusive multi-outcome market whose best prices sum below 1 \u2014 a potential arbitrage, not a guaranteed one; false otherwise."
   }
  },
  "required": [
   "exchanges",
   "overround",
   "hasArbitrageOpportunity"
  ]
 },
 "ExchangeOrderbookLevel": {
  "type": "object",
  "properties": {
   "price": {
    "type": "number",
    "example": 0.55
   },
   "quantity": {
    "type": "number",
    "example": 300
   }
  },
  "required": [
   "price",
   "quantity"
  ]
 },
 "ExchangeTrade": {
  "type": "object",
  "properties": {
   "id": {
    "type": "string",
    "example": "98342",
    "description": "Trade identifier: the trading-engine trade sequence for engine-recorded trades, or the legacy trade ID for pre-cutover trades. The two are separate ID spaces."
   },
   "createdAt": {
    "type": "string",
    "example": "2026-04-04T18:23:11.000Z"
   },
   "price": {
    "type": [
     "number",
     "null"
    ],
    "example": 0.42,
    "description": "YES-normalized fill price."
   },
   "size": {
    "type": "integer",
    "example": 100,
    "description": "Shares traded (absolute value of the underlying signed quantity)."
   },
   "side": {
    "type": "string",
    "enum": [
     "YES",
     "NO"
    ],
    "example": "YES",
    "description": "Whether the fill happened in YES or NO space."
   },
   "volume": {
    "type": "number",
    "example": 42,
    "description": "Notional volume. Engine-recorded trades report `size` \u00d7 the executed side's price, unrounded; pre-cutover trades report the stored trade volume."
   }
  },
  "required": [
   "id",
   "createdAt",
   "price",
   "size",
   "side",
   "volume"
  ]
 },
 "BatchConsentErrorResult": {
  "type": "object",
  "properties": {
   "index": {
    "type": "integer"
   },
   "ok": {
    "type": "boolean",
    "enum": [
     false
    ]
   },
   "status": {
    "type": "number",
    "enum": [
     403
    ]
   },
   "data": {
    "$ref": "#/components/schemas/TermsNotAcknowledgedError"
   }
  },
  "required": [
   "index",
   "ok",
   "status",
   "data"
  ],
  "example": {
   "index": 0,
   "ok": false,
   "status": 403,
   "data": {
    "error": {
     "code": "TERMS_NOT_ACKNOWLEDGED",
     "message": "Terms & Conditions acknowledgment required.",
     "details": {
      "missingDocuments": [
       {
        "documentId": "org:550e8400-e29b-41d4-a716-446655440000:rules",
        "kind": "org_rules",
        "version": "2.0.0",
        "organizationId": "550e8400-e29b-41d4-a716-446655440000",
        "organizationSlug": "sig",
        "organizationName": "SIG",
        "supportEmail": null,
        "currencyName": "SUSQies",
        "url": null,
        "host": "https://sig.thesuper.market"
       }
      ]
     }
    }
   }
  }
 },
 "BatchOrderInput": {
  "type": "object",
  "properties": {
   "idempotencyKey": {
    "type": "string",
    "minLength": 1,
    "maxLength": 255,
    "example": "order-placement-2026-07-23-001",
    "description": "Required client-supplied idempotency key. Reuse this key only when retrying the same logical request. An identical resolved payload replays the exact stored response, which is kept and replayed for at least 48 hours after the request completes; reuse with a different resolved payload returns 409. Once the stored response is compacted, any reuse of the key returns 409 `IDEMPOTENCY_KEY_EXPIRED`."
   },
   "orders": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/OrderInput"
    },
    "minItems": 1,
    "maxItems": 50
   }
  },
  "required": [
   "idempotencyKey",
   "orders"
  ],
  "example": {
   "idempotencyKey": "batch-placement-2026-07-23-001",
   "orders": [
    {
     "exchangeId": "36",
     "side": "yes",
     "action": "buy",
     "quantity": 100,
     "price": 0.42
    },
    {
     "exchangeId": "37",
     "side": "no",
     "action": "buy",
     "quantity": 50,
     "price": 0.3,
     "tournamentId": "550e8400-e29b-41d4-a716-446655440000"
    }
   ]
  }
 },
 "Fill": {
  "type": "object",
  "properties": {
   "id": {
    "type": "integer",
    "example": 5001,
    "description": "Trade identifier, shared by both counterparties' fills of the same trade. Engine-recorded fills use the trading-engine trade sequence; pre-cutover fills use the legacy trade ID \u2014 the two are separate ID spaces. Not an order ID \u2014 use orderId to look up the associated order."
   },
   "orderId": {
    "type": [
     "integer",
     "null"
    ],
    "example": 1001,
    "description": "The order that was filled. Use this ID with GET /orders/{id}."
   },
   "exchangeId": {
    "type": "string",
    "example": "36"
   },
   "marketId": {
    "type": "string",
    "example": "26"
   },
   "price": {
    "type": [
     "number",
     "null"
    ],
    "example": 0.42,
    "description": "Per-share price of the outcome that traded: a NO fill reports the NO price. Realtime `fills[].price` and `trades[].price` are YES-denominated instead, so a NO fill at 0.30 here is 0.70 there."
   },
   "quantity": {
    "type": "number",
    "example": 100,
    "description": "Outcome-signed fill quantity; negative values indicate the NO side."
   },
   "side": {
    "type": "string",
    "enum": [
     "yes",
     "no"
    ],
    "example": "yes"
   },
   "filledAt": {
    "type": "string",
    "example": "2025-06-14T12:05:00.000Z"
   }
  },
  "required": [
   "id",
   "orderId",
   "exchangeId",
   "marketId",
   "price",
   "quantity",
   "side",
   "filledAt"
  ]
 },
 "TournamentExchangeWithOrderbook": {
  "type": "object",
  "properties": {
   "id": {
    "type": "string",
    "example": "36"
   },
   "option": {
    "type": [
     "string",
     "null"
    ],
    "example": "YES"
   },
   "latestPrice": {
    "type": [
     "number",
     "null"
    ],
    "example": 0.61,
    "description": "Tournament-specific latest traded price. Null when no tournament trade has established a price; the tournament orderbook may still contain bids and asks. It never falls back to the global price."
   },
   "initialPrice": {
    "type": [
     "number",
     "null"
    ],
    "example": 0.5
   },
   "asOf": {
    "$ref": "#/components/schemas/BookVersion"
   },
   "bids": {
    "type": "array",
    "items": {
     "type": "object",
     "properties": {
      "price": {
       "type": "number"
      },
      "quantity": {
       "type": "number"
      }
     },
     "required": [
      "price",
      "quantity"
     ]
    }
   },
   "asks": {
    "type": "array",
    "items": {
     "type": "object",
     "properties": {
      "price": {
       "type": "number"
      },
      "quantity": {
       "type": "number"
      }
     },
     "required": [
      "price",
      "quantity"
     ]
    }
   },
   "bestBid": {
    "type": [
     "number",
     "null"
    ]
   },
   "bestAsk": {
    "type": [
     "number",
     "null"
    ]
   }
  },
  "required": [
   "id",
   "option",
   "latestPrice",
   "initialPrice",
   "asOf",
   "bids",
   "asks",
   "bestBid",
   "bestAsk"
  ]
 },
 "DesignatedMarketMakerTradeList": {
  "type": "object",
  "properties": {
   "data": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/DesignatedMarketMakerTrade"
    }
   },
   "nextCursor": {
    "type": [
     "string",
     "null"
    ],
    "example": null
   }
  },
  "required": [
   "data",
   "nextCursor"
  ]
 },
 "DesignatedMarketMakerTrade": {
  "type": "object",
  "properties": {
   "id": {
    "type": "integer",
    "example": 1024
   },
   "createdAt": {
    "type": "string",
    "example": "2026-06-15T10:00:00.000Z"
   },
   "marketId": {
    "type": "string",
    "example": "42"
   },
   "marketTitle": {
    "type": "string",
    "example": "Will it rain tomorrow?"
   },
   "settlementOption": {
    "type": "string",
    "example": "YES"
   },
   "price": {
    "type": "number",
    "example": 0.5
   },
   "quantity": {
    "type": "integer",
    "example": 10
   },
   "volume": {
    "type": "number",
    "example": 5
   },
   "bidOrderId": {
    "type": [
     "integer",
     "null"
    ],
    "example": 2001
   },
   "askOrderId": {
    "type": [
     "integer",
     "null"
    ],
    "example": 2002
   },
   "bid": {
    "$ref": "#/components/schemas/DesignatedMarketMakerTradeParticipant"
   },
   "ask": {
    "$ref": "#/components/schemas/DesignatedMarketMakerTradeParticipant"
   }
  },
  "required": [
   "id",
   "createdAt",
   "marketId",
   "marketTitle",
   "settlementOption",
   "price",
   "quantity",
   "volume",
   "bidOrderId",
   "askOrderId",
   "bid",
   "ask"
  ]
 },
 "DesignatedMarketMakerTradeParticipant": {
  "type": [
   "object",
   "null"
  ],
  "properties": {
   "profileId": {
    "type": "string",
    "example": "550e8400-e29b-41d4-a716-446655440000"
   },
   "username": {
    "type": [
     "string",
     "null"
    ],
    "example": "trader1"
   },
   "firstName": {
    "type": [
     "string",
     "null"
    ],
    "example": "Grace"
   },
   "lastName": {
    "type": [
     "string",
     "null"
    ],
    "example": "Hopper"
   },
   "email": {
    "type": [
     "string",
     "null"
    ],
    "example": "trader1@example.com"
   }
  },
  "required": [
   "profileId",
   "username",
   "firstName",
   "lastName",
   "email"
  ]
 },
 "DesignatedMarketMakerLiquidityBook": {
  "type": "object",
  "properties": {
   "bids": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/DesignatedMarketMakerLiquidityLevel"
    }
   },
   "asks": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/DesignatedMarketMakerLiquidityLevel"
    }
   }
  },
  "required": [
   "bids",
   "asks"
  ]
 },
 "DmmTraderAnalyticsUsersListResponse": {
  "type": "object",
  "properties": {
   "users": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/DmmTraderAnalyticsUser"
    }
   },
   "computedAsOf": {
    "type": "string",
    "example": "2026-05-21T00:00:00.000Z"
   },
   "sort": {
    "type": "string",
    "enum": [
     "smart_score_desc",
     "profit_desc",
     "roi_desc",
     "sharpe_desc",
     "win_rate_desc",
     "trades_desc"
    ],
    "example": "smart_score_desc"
   },
   "limit": {
    "type": "integer",
    "example": 50
   },
   "total": {
    "type": "integer",
    "example": 1247
   },
   "nextCursor": {
    "type": [
     "string",
     "null"
    ],
    "example": null
   },
   "scope": {
    "$ref": "#/components/schemas/DmmTraderAnalyticsScope"
   }
  },
  "required": [
   "users",
   "computedAsOf",
   "sort",
   "limit",
   "total",
   "nextCursor",
   "scope"
  ]
 },
 "DmmTraderAnalyticsUser": {
  "type": "object",
  "properties": {
   "profileId": {
    "type": "string",
    "format": "uuid"
   },
   "username": {
    "type": [
     "string",
     "null"
    ]
   },
   "email": {
    "type": [
     "string",
     "null"
    ]
   },
   "smartScore": {
    "type": "number"
   },
   "smartScoreDecayed": {
    "type": "number"
   },
   "isElite": {
    "type": "boolean"
   },
   "sharpeScaled": {
    "type": [
     "number",
     "null"
    ],
    "description": "Sharpe scaled to 0\u2013100 by the trader-scoring pipeline. Null when the trader has fewer than two returns, so no volatility is measured; the Smart Score counts it as 0. 0 when the member has no metrics."
   },
   "roi": {
    "type": "number"
   },
   "winRate": {
    "type": "number",
    "description": "Win rate as a percentage (0\u2013100): markets in profit (net realized + unrealized PnL > 0) out of markets traded, settled or still open."
   },
   "realizedPnl": {
    "type": "number"
   },
   "unrealizedPnl": {
    "type": "number"
   },
   "totalPnl": {
    "type": "number",
    "description": "realizedPnl + unrealizedPnl."
   },
   "totalTrades": {
    "type": "integer"
   },
   "totalStake": {
    "type": "number"
   },
   "marketsTraded": {
    "type": "integer"
   },
   "normalizedPnl": {
    "type": "number"
   },
   "lastTradeAt": {
    "type": [
     "string",
     "null"
    ]
   },
   "scope": {
    "$ref": "#/components/schemas/DmmTraderAnalyticsScope"
   }
  },
  "required": [
   "profileId",
   "username",
   "email",
   "smartScore",
   "smartScoreDecayed",
   "isElite",
   "sharpeScaled",
   "roi",
   "winRate",
   "realizedPnl",
   "unrealizedPnl",
   "totalPnl",
   "totalTrades",
   "totalStake",
   "marketsTraded",
   "normalizedPnl",
   "lastTradeAt",
   "scope"
  ]
 },
 "DmmTraderAnalyticsScope": {
  "type": "object",
  "properties": {
   "marketType": {
    "type": "string",
    "example": "global"
   },
   "organizationId": {
    "type": "string",
    "format": "uuid",
    "example": "550e8400-e29b-41d4-a716-446655440000"
   },
   "organizationSlug": {
    "type": [
     "string",
     "null"
    ],
    "example": "acme-corp"
   },
   "tournamentId": {
    "type": [
     "string",
     "null"
    ],
    "format": "uuid",
    "example": "550e8400-e29b-41d4-a716-446655440000",
    "description": "Tournament id when the request was scoped to a tournament; null otherwise."
   },
   "tournamentSlug": {
    "type": [
     "string",
     "null"
    ],
    "example": "alpha-tournament"
   },
   "userGroupId": {
    "type": [
     "string",
     "null"
    ],
    "format": "uuid"
   },
   "marketGroupId": {
    "type": [
     "string",
     "null"
    ],
    "format": "uuid"
   },
   "period": {
    "type": "string",
    "enum": [
     "day",
     "week",
     "month",
     "quarter",
     "all"
    ],
    "example": "month",
    "description": "Calendar-aligned period (UTC). `day` = today, `week` = since the most recent Sunday, `month` = since the 1st of the current month, `quarter` = since the 1st of the current calendar quarter, `all` = no time filter."
   },
   "periodStart": {
    "type": [
     "string",
     "null"
    ],
    "example": "2026-05-01T00:00:00.000Z",
    "description": "Resolved window start used to recompute metrics: the custom `periodStart` when one was sent (including with `period=all`), otherwise the start of the calendar `period`; null when no start applies."
   },
   "periodEnd": {
    "type": [
     "string",
     "null"
    ],
    "example": null,
    "description": "Echo of the custom `periodEnd` the metrics were computed as of; null when none was sent and metrics run up to the time of the request."
   }
  },
  "required": [
   "marketType",
   "organizationId",
   "organizationSlug",
   "tournamentId",
   "tournamentSlug",
   "period",
   "periodStart",
   "periodEnd"
  ]
 },
 "DmmTraderAnalyticsUserDetailResponse": {
  "type": "object",
  "properties": {
   "user": {
    "type": "object",
    "properties": {
     "profileId": {
      "type": "string",
      "format": "uuid"
     },
     "username": {
      "type": [
       "string",
       "null"
      ]
     },
     "email": {
      "type": [
       "string",
       "null"
      ]
     },
     "hasSetUsername": {
      "type": "boolean",
      "description": "the handle is finalized and displayable \u2014 chosen by the participant or generated by the organization's automatic-username policy"
     },
     "avatarUrl": {
      "type": [
       "string",
       "null"
      ]
     },
     "joinedAt": {
      "type": "string"
     }
    },
    "required": [
     "profileId",
     "username",
     "email",
     "hasSetUsername",
     "avatarUrl",
     "joinedAt"
    ]
   },
   "metrics": {
    "type": "object",
    "properties": {
     "marketType": {
      "type": [
       "string",
       "null"
      ]
     },
     "calculationAsOf": {
      "type": [
       "string",
       "null"
      ]
     },
     "totalTrades": {
      "type": "integer"
     },
     "marketsTraded": {
      "type": "integer"
     },
     "profitMarketCount": {
      "type": "integer",
      "description": "Markets the trader is in profit on (net realized + unrealized PnL > 0), settled or still open."
     },
     "realizedPnl": {
      "type": "number"
     },
     "unrealizedPnl": {
      "type": "number"
     },
     "totalPnl": {
      "type": "number"
     },
     "totalStake": {
      "type": "number"
     },
     "winRate": {
      "type": "number",
      "description": "profitMarketCount out of marketsTraded as a percentage (0\u2013100); 0 when no market was traded."
     },
     "roi": {
      "type": "number"
     },
     "sharpeScaled": {
      "type": [
       "number",
       "null"
      ],
      "description": "Sharpe scaled to 0\u2013100. Null when the trader has fewer than two returns, so no volatility is measured; the Smart Score counts it as 0. 0 when the member has no metrics."
     },
     "smartScore": {
      "type": "number"
     },
     "smartScoreDecayed": {
      "type": "number"
     },
     "normalizedPnl": {
      "type": "number"
     },
     "lastTradeAt": {
      "type": [
       "string",
       "null"
      ]
     },
     "isElite": {
      "type": "boolean"
     }
    },
    "required": [
     "marketType",
     "calculationAsOf",
     "totalTrades",
     "marketsTraded",
     "profitMarketCount",
     "realizedPnl",
     "unrealizedPnl",
     "totalPnl",
     "totalStake",
     "winRate",
     "roi",
     "sharpeScaled",
     "smartScore",
     "smartScoreDecayed",
     "normalizedPnl",
     "lastTradeAt",
     "isElite"
    ]
   },
   "categories": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/DmmTraderAnalyticsCategoryRow"
    },
    "description": "Per-market-type breakdown drawn from the latest snapshot row of each category in `organization_trader_metrics`. Always organization-wide regardless of `tournamentId` because per-tournament category history is not persisted."
   },
   "history": {
    "type": [
     "array",
     "null"
    ],
    "items": {
     "$ref": "#/components/schemas/DmmTraderAnalyticsHistoryPoint"
    },
    "description": "Up to 30 most recent daily smart-score snapshots from `organization_trader_metrics` for the requested `marketType`. Returned only when `includeHistory=true`. Always organization-wide regardless of `tournamentId`."
   },
   "computedAsOf": {
    "type": "string"
   },
   "scope": {
    "$ref": "#/components/schemas/DmmTraderAnalyticsScope"
   }
  },
  "required": [
   "user",
   "metrics",
   "categories",
   "history",
   "computedAsOf",
   "scope"
  ]
 },
 "DmmTraderAnalyticsCategoryRow": {
  "type": "object",
  "properties": {
   "marketType": {
    "type": "string",
    "example": "sports"
   },
   "calculationAsOf": {
    "type": "string"
   },
   "totalTrades": {
    "type": "integer"
   },
   "marketsTraded": {
    "type": "integer"
   },
   "realizedPnl": {
    "type": "number"
   },
   "unrealizedPnl": {
    "type": "number"
   },
   "totalPnl": {
    "type": "number"
   },
   "totalStake": {
    "type": "number"
   },
   "winRate": {
    "type": [
     "number",
     "null"
    ],
    "description": "Markets in profit out of markets traded (0\u2013100). Null when the category snapshot was computed before profitable-market counts were stored, or no market was traded."
   },
   "roi": {
    "type": "number"
   },
   "sharpeScaled": {
    "type": [
     "number",
     "null"
    ],
    "description": "Sharpe scaled to 0\u2013100. Null when the trader has fewer than two returns, so no volatility is measured; the Smart Score counts it as 0."
   },
   "smartScore": {
    "type": "number"
   },
   "smartScoreDecayed": {
    "type": "number"
   }
  },
  "required": [
   "marketType",
   "calculationAsOf",
   "totalTrades",
   "marketsTraded",
   "realizedPnl",
   "unrealizedPnl",
   "totalPnl",
   "totalStake",
   "winRate",
   "roi",
   "sharpeScaled",
   "smartScore",
   "smartScoreDecayed"
  ]
 },
 "DmmTraderAnalyticsHistoryPoint": {
  "type": "object",
  "properties": {
   "asOf": {
    "type": "string",
    "example": "2026-05-20T00:00:00.000Z"
   },
   "smartScore": {
    "type": "number"
   },
   "smartScoreDecayed": {
    "type": "number"
   },
   "totalTrades": {
    "type": "integer"
   }
  },
  "required": [
   "asOf",
   "smartScore",
   "smartScoreDecayed",
   "totalTrades"
  ]
 },
 "DmmTraderAnalyticsEliteSummaryResponse": {
  "type": "object",
  "properties": {
   "scope": {
    "type": "object",
    "properties": {
     "marketType": {
      "type": "string"
     },
     "organizationId": {
      "type": "string",
      "format": "uuid"
     },
     "organizationSlug": {
      "type": [
       "string",
       "null"
      ]
     },
     "tournamentId": {
      "type": [
       "string",
       "null"
      ],
      "format": "uuid"
     },
     "tournamentSlug": {
      "type": [
       "string",
       "null"
      ]
     }
    },
    "required": [
     "marketType",
     "organizationId",
     "organizationSlug",
     "tournamentId",
     "tournamentSlug"
    ]
   },
   "latestEliteCutAsOf": {
    "type": [
     "string",
     "null"
    ],
    "example": "2026-05-21T00:00:00.000Z",
    "description": "Cut of the latest metrics run for this scope, which the cohort is read at. Set even when that run selected no elites of this market type; `null` only when no run exists for this scope."
   },
   "thresholds": {
    "type": "object",
    "properties": {
     "minimumEliteScore": {
      "type": "number",
      "example": 80
     },
     "elitePercentage": {
      "type": "number",
      "example": 0.05
     },
     "minimumTradeCount": {
      "type": "number",
      "example": 20
     },
     "minimumStake": {
      "type": "number",
      "example": 50
     },
     "componentWeights": {
      "type": "object",
      "properties": {
       "pnl": {
        "type": "number"
       },
       "winRate": {
        "type": "number"
       },
       "roi": {
        "type": "number"
       },
       "sharpe": {
        "type": "number"
       }
      },
      "required": [
       "pnl",
       "winRate",
       "roi",
       "sharpe"
      ]
     },
     "timeDecayFactor": {
      "type": "number",
      "example": 0.9
     }
    },
    "required": [
     "minimumEliteScore",
     "elitePercentage",
     "minimumTradeCount",
     "minimumStake",
     "componentWeights",
     "timeDecayFactor"
    ]
   },
   "cohort": {
    "type": "object",
    "properties": {
     "size": {
      "type": "integer"
     },
     "meanSmartScoreDecayed": {
      "type": [
       "number",
       "null"
      ]
     },
     "minSmartScoreDecayed": {
      "type": [
       "number",
       "null"
      ]
     },
     "maxSmartScoreDecayed": {
      "type": [
       "number",
       "null"
      ]
     }
    },
    "required": [
     "size",
     "meanSmartScoreDecayed",
     "minSmartScoreDecayed",
     "maxSmartScoreDecayed"
    ]
   },
   "topPerformers": {
    "type": "array",
    "items": {
     "$ref": "#/components/schemas/DmmTraderAnalyticsTopPerformer"
    }
   }
  },
  "required": [
   "scope",
   "latestEliteCutAsOf",
   "thresholds",
   "cohort",
   "topPerformers"
  ]
 },
 "DmmTraderAnalyticsTopPerformer": {
  "type": "object",
  "properties": {
   "profileId": {
    "type": "string",
    "format": "uuid"
   },
   "username": {
    "type": [
     "string",
     "null"
    ]
   },
   "email": {
    "type": [
     "string",
     "null"
    ]
   },
   "hasSetUsername": {
    "type": "boolean",
    "description": "the handle is finalized and displayable \u2014 chosen by the participant or generated by the organization's automatic-username policy"
   },
   "avatarUrl": {
    "type": [
     "string",
     "null"
    ]
   },
   "smartScoreDecayed": {
    "type": "number"
   },
   "smartScore": {
    "type": [
     "number",
     "null"
    ]
   },
   "selectedVia": {
    "type": "string",
    "example": "percentile",
    "description": "Reason this trader was selected into the elite cohort (e.g. `percentile`, `threshold`, `global_backfill`)."
   },
   "totalTrades": {
    "type": "integer"
   },
   "roi": {
    "type": [
     "number",
     "null"
    ]
   },
   "sharpeScaled": {
    "type": [
     "number",
     "null"
    ]
   },
   "winRate": {
    "type": [
     "number",
     "null"
    ]
   }
  },
  "required": [
   "profileId",
   "username",
   "email",
   "hasSetUsername",
   "avatarUrl",
   "smartScoreDecayed",
   "smartScore",
   "selectedVia",
   "totalTrades",
   "roi",
   "sharpeScaled",
   "winRate"
  ]
 },
 "RealtimeError": {
  "type": "object",
  "properties": {
   "error": {
    "type": "object",
    "properties": {
     "code": {
      "type": "string",
      "example": "INTERNAL_ERROR"
     },
     "message": {
      "type": "string",
      "example": "Internal server error."
     },
     "details": {
      "type": "object",
      "additionalProperties": {}
     }
    },
    "required": [
     "code",
     "message"
    ]
   }
  },
  "required": [
   "error"
  ]
 }
}