# Running sigbot on an always-on server

The bot is stdlib Python (3.12 or newer), so any small Linux VM runs it. Once it is on the server, the laptop can be
closed, asleep or off.

## 1. Rent a small server (you do this; it needs an account and payment)

* **Where:** US West. The venue's API is served from Vercel's San Francisco region (`sfo1`), so a West Coast machine
  saves roughly 150 ms per request compared with calling from London. Good picks: DigitalOcean SFO3, Hetzner Hillsboro
  (Oregon), AWS Lightsail `us-west-2`, Vultr Silicon Valley.
* **Size:** the smallest plan is enough (1 vCPU, 1 GB RAM, about $4-6 a month).
* **Image:** Ubuntu 24.04 LTS (ships Python 3.12, which the bot needs).
* **Access:** paste your public key during creation so you can log in without a password:

```bash
cat ~/.ssh/id_ed25519.pub
```

Then add a short alias on the laptop (replace the IP and the user: `root` on DigitalOcean/Vultr/Hetzner, `ubuntu` on
Lightsail):

```bash
printf 'Host sigbot\n  HostName 203.0.113.10\n  User root\n  IdentityFile ~/.ssh/id_ed25519\n' >> ~/.ssh/config
```

Check it works:

```bash
ssh sigbot 'uname -a && python3 -V'
```

## 2. Prepare the server (one time)

```bash
ssh sigbot 'sudo bash -s' < deploy/server_setup.sh
```

This installs Python, rsync, sqlite3 and tzdata, creates an unprivileged `sigbot` user and `/opt/sigbot`, and turns on
clock sync.

## 3. Cut over from the laptop

Stop the laptop bot first (Ctrl-C in the "sigbot live" terminal tab, wait for the prompt), then:

```bash
deploy/cutover.sh sigbot
```

It refuses to run while a laptop bot is live. It pushes the code and runs one dry cycle on the server. It cancels every
resting order and checks the book is empty, copies a snapshot of the SQLite log, then starts the service and shows the
first cycle. After this, the laptop refuses to trade live: cutover writes `deploy/SERVER_HOST`, and `sigbot.run --live` exits on any
machine without the systemd service while that file exists. Two bots on one account would double the orders. To move
trading back to the laptop, stop the server bot and delete `deploy/SERVER_HOST`.

The first start on the server may spend a few minutes rebuilding the liquidity cache if the copied cache is older than
6 hours (237 reads at the main loop's budget).

## 4. Day to day

```bash
deploy/remote.sh sigbot status    # service state, last HEALTH/cycle lines, fills and markouts
deploy/remote.sh sigbot logs      # follow the live log (Ctrl-C stops following, not the bot)
deploy/remote.sh sigbot stop      # stop trading; resting orders are cancelled on the way out
deploy/remote.sh sigbot start
deploy/remote.sh sigbot latency   # round trip from the server to the venue
deploy/remote.sh sigbot pull-db   # copy the server's log DB to data/sigbot.server.sqlite for analysis
```

To ship a code or config change: edit locally, then

```bash
deploy/push.sh sigbot && deploy/remote.sh sigbot restart
```

## What the service does for you

* Starts at boot and restarts 30 s after a crash. It gives up after 5 crashes in 10 minutes, so a repeating bug stops
  instead of churning orders.
* On stop, and after a crash, it cancels every resting order (the bot does it on SIGTERM, and `cancel_all.py` runs
  again after the process exits).
* Runs as an unprivileged user that can write only `/opt/sigbot`.
* Logs go to the journal and to `/opt/sigbot/data/sigbot.log`.

## Keys

`.sig_api_key` (and `.anthropic_api_key` for the news sweeper) are copied with mode 600 and owned by the `sigbot` user.
They are never printed. To rotate one, replace the local file and run `deploy/push.sh sigbot`, then
`deploy/remote.sh sigbot restart`.
