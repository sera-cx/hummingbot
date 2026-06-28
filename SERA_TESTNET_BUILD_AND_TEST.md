# Sera Testnet Build and Test Guide

This guide builds Hummingbot from source, runs the Sera connector tests, deploys the MQTT broker, and starts the Sera pure market making strategy on testnet.

Sera is currently pointed at testnet in:

```text
hummingbot/connector/exchange/sera/sera_constants.py
```

```python
REST_URL = "https://api.testnet.sera.cx/api/v1"
```

## Current Sera Changes

This branch includes the Sera testnet work needed to market make on an initially empty book:

- Sera reads the mid price from the configured Hummingbot rate oracle when the exchange order book is empty.
- Wise is available as a rate oracle source, using unauthenticated Wise quotes.
- Pure market making can continue when the Sera order book is empty, so the first market maker can place both sides from the oracle mid price.
- The Sera PMM config uses `take_if_crossed: true`, which avoids filtering all orders when there is no opposing side in the book.
- Sera balances use `vault_available` as the Hummingbot available balance. Funds must be deposited into the Sera vault before PMM can create orders.

## 1. Set the Conda Command

On this machine, `conda` may not be on `PATH`, so use the full path:

```bash
export CONDA=/opt/homebrew/anaconda3/bin/conda
```

If your shell already has `conda`, this also works:

```bash
export CONDA=conda
```

## 2. Install or Update the Environment

For an existing `hummingbot` environment:

```bash
$CONDA env update -n hummingbot -f setup/environment.yml
$CONDA run -n hummingbot conda develop .
$CONDA run -n hummingbot python -m pip install --no-deps -r setup/pip_packages.txt
```

For a first-time setup:

```bash
$CONDA env create -n hummingbot -f setup/environment.yml
$CONDA run -n hummingbot conda develop .
$CONDA run -n hummingbot python -m pip install --no-deps -r setup/pip_packages.txt
```

If `conda` is on `PATH`, the repo target can do this:

```bash
make install
```

## 3. Build Hummingbot

Build the Cython extensions in place:

```bash
$CONDA run -n hummingbot --no-capture-output python setup.py build_ext --inplace
```

This is the build command verified for the Sera test work.

## 4. Run the Sera Unit Tests

These tests mock network calls; they do not hit Sera testnet.

```bash
$CONDA run -n hummingbot pytest test/hummingbot/connector/exchange/sera
```

Run only the exchange behavior tests:

```bash
$CONDA run -n hummingbot pytest test/hummingbot/connector/exchange/sera/test_sera_exchange.py
```

Run the focused Sera PMM and oracle tests:

```bash
$CONDA run -n hummingbot pytest \
  test/hummingbot/connector/exchange/sera/test_sera_oracle_mid_price.py \
  test/hummingbot/connector/exchange/sera/test_sera_pure_market_making.py \
  test/hummingbot/core/rate_oracle/sources/test_wise_rate_source.py
```

## 5. Run Lint Hooks for Sera Files

```bash
$CONDA run -n hummingbot flake8 hummingbot/connector/exchange/sera test/hummingbot/connector/exchange/sera
$CONDA run -n hummingbot pre-commit run isort --files \
  hummingbot/connector/exchange/sera/sera_api_order_book_data_source.py \
  hummingbot/connector/exchange/sera/sera_api_user_stream_data_source.py \
  hummingbot/connector/exchange/sera/sera_auth.py \
  hummingbot/connector/exchange/sera/sera_constants.py \
  hummingbot/connector/exchange/sera/sera_exchange.py \
  hummingbot/connector/exchange/sera/sera_utils.py \
  hummingbot/connector/exchange/sera/sera_web_utils.py \
  test/hummingbot/connector/exchange/sera/test_sera_auth.py \
  test/hummingbot/connector/exchange/sera/test_sera_exchange.py \
  test/hummingbot/connector/exchange/sera/test_sera_pure_market_making.py \
  test/hummingbot/connector/exchange/sera/test_sera_utils.py
```

## 6. Configure Live Sera Testnet Credentials

Use a testnet wallet only. Do not use a production private key.

Set credentials either by exporting them directly:

```bash
export SERA_API_KEY="your-sera-api-key"
export SERA_API_SECRET="your-sera-api-secret"
export SERA_WALLET_ADDRESS="0xyourtestnetwallet"
export SERA_PRIVATE_KEY="your-testnet-wallet-private-key"
export SERA_TRADING_PAIR="XSGD-USDC"
export CONFIG_PASSWORD="1234"
```

Or load them from `.env`:

```bash
set -a
source .env
set +a
```

If you need to create a Sera API key for the wallet, set `SERA_PRIVATE_KEY` first and run:

```bash
$CONDA run -n hummingbot python sera_setup.py
```

Then copy the printed `api_key` and `api_secret` into `SERA_API_KEY` and `SERA_API_SECRET`.

If Hummingbot is already connected to Sera, the encrypted connector config is stored in:

```text
conf/connectors/sera.yml
```

## 7. Check Rate Oracle and PMM Config

The Sera PMM config is:

```text
conf/strategies/conf_pmm_sera.yml
```

Important settings:

```yaml
exchange: sera
market: XSGD-USDC
order_amount: 20
price_source: current_market
price_type: mid_price
take_if_crossed: true
```

### Add a PMM Price Ladder

Use a price ladder when you want PMM to place multiple orders on each side of the mid price instead of one bid and one ask.

For a simple evenly-spaced ladder, edit:

```text
conf/strategies/conf_pmm_sera.yml
```

Example:

```yaml
bid_spread: 1
ask_spread: 1
order_amount: 20

order_levels: 3
order_level_spread: 0.5
order_level_amount: 5
```

PMM config spreads are percentages, so `1` means `1%` and `0.5` means `0.5%`.

With the example above, PMM places:

```text
Bid levels: 1.0%, 1.5%, 2.0% below mid
Ask levels: 1.0%, 1.5%, 2.0% above mid
Amounts:    20, 25, 30 base units per side
```

For a custom asymmetric ladder, enable split order levels. This overrides `order_amount`, `order_level_spread`, and `order_level_amount`:

```yaml
split_order_levels_enabled: true

bid_order_level_spreads: 0.5,1,2
ask_order_level_spreads: 0.75,1.5,3
bid_order_level_amounts: 10,15,25
ask_order_level_amounts: 8,12,20
```

With split levels enabled, PMM uses the comma-separated spread and amount at each index:

```text
Bid 1: 10 XSGD at 0.5% below mid
Bid 2: 15 XSGD at 1.0% below mid
Bid 3: 25 XSGD at 2.0% below mid

Ask 1: 8 XSGD at 0.75% above mid
Ask 2: 12 XSGD at 1.5% above mid
Ask 3: 20 XSGD at 3.0% above mid
```

Keep enough Sera vault balance for the full ladder, not just the top order. For the simple ladder example, PMM may reserve `20 + 25 + 30 = 75` base units for asks, plus enough quote balance for all bid levels.

The client rate oracle must be Wise so the connector can price Sera's synthetic FX
pairs (e.g. `XSGD-MYRT`) when the order book is empty. **This is now the baked-in
default** (`ClientConfigMap.rate_oracle_source` in
`hummingbot/client/config/client_config_map.py`), so a freshly generated
`conf/conf_client.yml` already contains:

```yaml
rate_oracle_source:
  name: wise
  trading_pairs: XSGD-MYRT,XSGD-USDT,XSGD-JPYC,MYRT-JPYC,XSGD-EGBP,EGBP-MYRT
  currency_map: XSGD:SGD,MYRT:MYR,JPYC:JPY,EGBP:GBP
  source_amount: 100
```

> **Existing deploy hosts:** the default only applies when `conf/conf_client.yml`
> is generated fresh. A host that already has a `conf/conf_client.yml` (e.g. one
> defaulting to `name: binance`) keeps its old value, and exchange oracles do not
> list `XSGD-MYRT`, so the connector logs `oracle mid price is not available yet`
> forever and never places orders. Verify and fix in place:
>
> ```bash
> docker compose -f docker-compose.sera.yml exec hummingbot-sera \
>   sh -c "grep -A4 rate_oracle_source conf/conf_client.yml"
> ```
>
> If `name:` is not `wise`, replace the block with the YAML above (or rsync the
> configured `conf/` from a working host) and restart.

When the Sera order book is empty, the connector should log a message like:

```text
Order book is empty for XSGD-MYRT; using oracle mid price ...
```

## 8. Start the MQTT Broker

Hummingbot uses MQTT for the headless broker bridge. The local client config should point to the local broker:

```yaml
mqtt_bridge:
  mqtt_host: localhost
  mqtt_port: 1883
  mqtt_username: ''
  mqtt_password: ''
  mqtt_namespace: hbot
  mqtt_ssl: false
```

Install Docker Desktop if Docker is not already running. Then clone the Hummingbot broker repo if it is not already present:

```bash
git clone https://github.com/hummingbot/brokers /private/tmp/hummingbot-brokers
```

Start EMQX:

```bash
docker compose -f /private/tmp/hummingbot-brokers/compose/emqx_v5/emqx.compose.yml up -d --remove-orphans
```

Check broker status:

```bash
docker compose -f /private/tmp/hummingbot-brokers/compose/emqx_v5/emqx.compose.yml ps
```

The broker should expose MQTT on:

```text
localhost:1883
```

If Hummingbot logs MQTT connection errors, restart the broker and verify Docker is running before starting Hummingbot again.

## 9. Run Hummingbot API for Condor Integration

Condor talks to running bots through Hummingbot API. The Hummingbot API stack includes:

```text
API: http://localhost:8000
Swagger UI: http://localhost:8000/docs
PostgreSQL: localhost:5432
EMQX MQTT broker: localhost:1883
EMQX Dashboard: http://localhost:18083
```

If you use Hummingbot API's bundled EMQX broker, do not also run the standalone broker from section 8 on the same machine, because both try to bind `localhost:1883`. Either stop the standalone broker first or configure one of the stacks to use a different MQTT port.

From an empty directory outside this repo, install and start Hummingbot API with Docker:

```bash
mkdir -p /private/tmp/hummingbot-api-deploy
cd /private/tmp/hummingbot-api-deploy
curl -fsSL https://raw.githubusercontent.com/hummingbot/deploy/main/setup.sh | bash -s -- --hummingbot-api
```

The installer clones `hummingbot-api`, creates its `.env`, pulls Docker images, and starts the API, PostgreSQL, and EMQX containers. After it finishes, verify the API:

```bash
curl http://localhost:8000/health
open http://localhost:8000/docs
```

If you cloned `hummingbot-api` manually, the equivalent workflow is:

```bash
git clone https://github.com/hummingbot/hummingbot-api /private/tmp/hummingbot-api
cd /private/tmp/hummingbot-api
make setup
make deploy
```

Important `.env` values in the `hummingbot-api` repo:

```text
USERNAME=admin
PASSWORD=admin
CONFIG_PASSWORD=1234
```

Use the same `CONFIG_PASSWORD` that decrypts this Hummingbot repo's connector config when the API launches or manages bot containers that need Sera credentials.

### Make Sure Hummingbot API Uses a Bot Image with Sera

Hummingbot API does not automatically use the connector code from this local checkout. It launches Hummingbot bot instances from the bot image configured in the Hummingbot API deployment.

That means:

```text
This local repo: has the Sera connector.
Default official Hummingbot bot image: may not have the Sera connector.
Custom bot image built from this branch: has the Sera connector.
```

Build a custom bot image from this repo after the Sera connector and PMM changes are present:

```bash
cd /Users/shikai/Code/hummingbot
docker build -t hummingbot-sera:local .
```

`docker build` produces an image matching the build host's CPU architecture. On an Apple Silicon Mac that is `arm64`, which will not run on an `amd64` (x86_64) deployment host. If the Hummingbot API host is `amd64`, build the image for `amd64` instead. See section 12, [Build the Docker Image for amd64 Deployment](#12-build-the-docker-image-for-amd64-deployment).

Then configure the Hummingbot API deployment to launch bot containers from that image. The exact environment variable name can vary by `hummingbot-api` version, but look in the `hummingbot-api` `.env` or `docker-compose.yml` for the bot image setting and point it to:

```text
hummingbot-sera:local
```

Common names to look for are:

```text
HUMMINGBOT_IMAGE
BOT_IMAGE
HUMMINGBOT_DOCKER_IMAGE
```

After changing the image setting, restart Hummingbot API:

```bash
cd /private/tmp/hummingbot-api
make deploy
```

If the API was installed by the deploy helper into a different folder, run `make deploy` from that `hummingbot-api` folder instead.

The bot container also needs the Sera strategy and connector credentials. Make sure the API-managed bot has access to:

```text
conf/connectors/sera.yml
conf/strategies/conf_pmm_sera.yml
CONFIG_PASSWORD
```

If Condor can reach Hummingbot API but Sera is missing from connector lists or bot startup fails with an unknown connector error, the API is probably still using a Hummingbot image that does not include this Sera branch.

Install Condor

```sh
curl -fsSL https://raw.githubusercontent.com/hummingbot/deploy/main/setup.sh | bash
```

In Condor, add the Hummingbot API server from Telegram:

```text
/servers
```

For a local Condor and local Hummingbot API setup, use:

```text
API URL: http://localhost:8000
Username: admin
Password: admin
```

For Condor running on another machine, expose the API over a private network such as Tailscale and use the private URL, for example:

```text
API URL: http://hummingbot-api:8000
```

Once Condor can reach Hummingbot API, use:

```text
/servers   check API connectivity
/keys      add or verify Sera credentials
/bots      monitor running bots
/new_bot   create bot configs through the API
```

Reference docs:

```text
Hummingbot API: https://github.com/hummingbot/hummingbot-api
Condor: https://hummingbot.org/condor/
```

## 10. Ensure Sera Vault Balances Are Available

The Sera connector reports Hummingbot available balances from Sera `vault_available`, not from wallet totals. If PMM logs proposals with `buys=0` and `sells=0`, check whether vault balances are zero or frozen.

Use Sera testnet UI or the Sera account API to deposit testnet tokens into the vault. For `conf_pmm_sera.yml`, keep at least enough vault balance for one bid and one ask:

```text
XSGD vault_available >= 20
USDC vault_available >= 20 * oracle_mid_price
```

After funds are in the vault, Hummingbot can reserve part of the balance as `vault_frozen` for live orders.

## 11. Start Hummingbot Headless with PMM

Activate the environment and start the client:

```bash
$CONDA run -n hummingbot --no-capture-output ./bin/hummingbot_quickstart.py
```

Inside the client:

```text
connect sera
```

Hummingbot prompts for:

```text
Sera API key
Sera API secret
Sera wallet address
Sera wallet private key
```

The credentials are saved encrypted in:

```text
conf/connectors/sera.yml
```

For the headless PMM run, load `.env`, make sure the broker is already up, and start the strategy config:

```bash
set -a
source .env
set +a

$CONDA run -n hummingbot --no-capture-output ./bin/hummingbot_quickstart.py --config-file-name conf_pmm_sera.yml --headless
```

On this machine, the fully expanded command is:

```bash
/opt/homebrew/anaconda3/bin/conda run -n hummingbot --no-capture-output ./bin/hummingbot_quickstart.py --config-file-name conf_pmm_sera.yml --headless
```

Expected startup signals:

```text
MQTT connected to localhost:1883
Order book is empty for XSGD-USDC; using oracle mid price ...
Creating bid orders
Creating ask orders
```

If the strategy starts but creates no orders, check in this order:

1. EMQX broker is running on `localhost:1883`.
2. `CONFIG_PASSWORD` is exported and decrypts `conf/connectors/sera.yml`.
3. `rate_oracle_source.name` is `wise` and includes `XSGD-USDC`.
4. Sera vault balances show nonzero `vault_available`.
5. The project was rebuilt after any Cython changes.

## 12. Build the Docker Image for amd64 Deployment

Use this when you deploy the Sera bot image to an `amd64` (x86_64) host, such as a cloud server or the Hummingbot API machine in section 9. Building from this branch produces a self-contained image with the Sera connector and PMM changes already compiled in.

`docker build` produces an image whose CPU architecture matches the build host. This repo's dev machine is an Apple Silicon Mac (`arm64`), so a plain `docker build` makes an `arm64` image that cannot run on an `amd64` host. To target `amd64` from the Mac, cross-build with `docker buildx`, which compiles the `amd64` layers under QEMU emulation.

Do not use `make build`. The `build` target runs `git clean -xdf`, which deletes untracked and ignored files including your local `conf/`, `logs/`, and `data/`.

### Prerequisites

```text
Docker Desktop running
docker buildx available (bundled with Docker Desktop)
```

Confirm the `desktop-linux` builder advertises `linux/amd64`:

```bash
docker buildx ls
```

Because the `amd64` Cython `build_ext` step runs under emulation on Apple Silicon, this build is much slower than a native build and can take many minutes. Run it in a terminal you can leave open.

### Build

```bash
cd /Users/shikai/Code/hummingbot

docker buildx build \
  --platform linux/amd64 \
  --build-arg BRANCH="$(git rev-parse --abbrev-ref HEAD)" \
  --build-arg COMMIT="$(git rev-parse --short HEAD)" \
  --build-arg BUILD_DATE="$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  -t hummingbot/hummingbot:sera-amd64 \
  -f Dockerfile \
  --load \
  .
```

`--load` imports the finished image into the local Docker image store so you can run, retag, or push it. `--load` only supports a single platform, which is why `--platform` lists `linux/amd64` alone.

### Verify the Image Architecture

```bash
docker image inspect hummingbot/hummingbot:sera-amd64 --format '{{.Architecture}}'
```

This must print:

```text
amd64
```

### Use the Image with docker-compose

`docker-compose.yml` in this repo references `hummingbot/hummingbot:latest`. To run the amd64 build through compose, retag it:

```bash
docker tag hummingbot/hummingbot:sera-amd64 hummingbot/hummingbot:latest
docker compose up -d
```

On the Apple Silicon Mac the amd64 image still runs locally through emulation, which is fine for a smoke test but slower than a native image. Run it natively on an amd64 host for real deployment.

### Use the Image with Hummingbot API

Section 9 launches bot containers from a configured bot image, referenced there as `hummingbot-sera:local`. Tag the amd64 build with that name on the amd64 host, or push it to a registry the host can pull from:

```bash
docker tag hummingbot/hummingbot:sera-amd64 hummingbot-sera:local
```

### Push to a Registry for Remote Deployment

To deploy on a separate amd64 server, push the image to a registry it can reach, then pull it there:

```bash
docker tag hummingbot/hummingbot:sera-amd64 <registry>/hummingbot-sera:amd64
docker push <registry>/hummingbot-sera:amd64
```

On the amd64 host:

```bash
docker pull <registry>/hummingbot-sera:amd64
```

Replace `<registry>` with your registry path, for example `docker.io/<dockerhub-user>` or a private registry host.

### Smoke Test the Image

Confirm the container starts and the Sera connector imports. Call the conda env's Python directly so the check does not depend on shell activation, which the Dockerfile notes does not apply to a manual `docker run image COMMAND`:

```bash
docker run --rm --platform linux/amd64 -w /home/hummingbot hummingbot/hummingbot:sera-amd64 \
  /opt/conda/envs/hummingbot/bin/python -c "import hummingbot.connector.exchange.sera.sera_exchange; print('sera connector OK')"
```

Expected output:

```text
sera connector OK
```

## 13. Run the V1 Sera Strategy Headless in Docker

Use this to run the full `sera_market_making` strategy (including VL and the
triangular model) inside the amd64 container, auto-started from
`conf/strategies/conf_serapmm_sera.yml`. This path does not involve Condor,
Hummingbot API, or a V2 controller, so all VL functionality runs unchanged.

This is the recommended way to trade the Sera VL strategy today. The Condor
`/new_bot` flow only accepts V2 controller configs, and `sera_market_making` is a
V1 strategy — see section 9 and the V2 controller scaffold notes
(`controllers/generic/SERA_VL_CONTROLLER.md`) for that path.

### How It Works

The default image command runs `bin/hummingbot_quickstart.py`, which reads these
environment variables (`bin/hummingbot_quickstart.py:256-266`) when the matching
CLI arguments are not passed:

```text
CONFIG_FILE_NAME  ->  --config-file-name   strategy config to load and start
CONFIG_PASSWORD   ->  --config-password    decrypts conf/connectors/sera.yml
HEADLESS_MODE     ->  --headless           run without the CLI; auto-enables MQTT
SCRIPT_CONFIG     ->  --v2                 (V2 script config; not used here)
```

So running the container with `CONFIG_FILE_NAME=conf_serapmm_sera.yml`,
`HEADLESS_MODE=true`, and `CONFIG_PASSWORD` set is enough to auto-start the V1
strategy. No code or image changes are required.

### Prerequisites

```text
conf/strategies/conf_serapmm_sera.yml present
conf/connectors/sera.yml present, encrypted with CONFIG_PASSWORD
amd64 image built or pulled as hummingbot/hummingbot:sera-amd64 (section 12)
```

If `conf/connectors/sera.yml` does not exist yet, create it once via
`connect sera` (section 11) before running headless.

### Provisioning Credentials on a Deploy Host

A fresh deploy host has no credentials. Hummingbot validates the master password
against `conf/.password_verification` at login, so if that file is missing the
bot exits immediately with:

```text
FileNotFoundError: '/home/hummingbot/conf/.password_verification'
```

`conf/.password_verification` and `conf/connectors/sera.yml` are a matched set
encrypted with the same master password. Provision them one of two ways:

- Copy the encrypted config from a machine that already has it (keeps the same
  password and credentials). Use `rsync -a` so the hidden `.password_verification`
  dotfile is included — a plain `cp conf/*` or `scp conf/*` silently skips it:

  ```bash
  rsync -av /path/to/hummingbot/conf/ user@deploy-host:/path/to/conf/
  ```

  Then start with the same `CONFIG_PASSWORD` used on the source machine.

- Set up fresh on the deploy host by running the image interactively (no
  `HEADLESS_MODE`, no `CONFIG_FILE_NAME`) so the CLI prompts you to create a
  password and connect:

  ```bash
  docker run -it --rm \
    -v "$(pwd)/conf:/home/hummingbot/conf" \
    -v "$(pwd)/logs:/home/hummingbot/logs" \
    -v "$(pwd)/data:/home/hummingbot/data" \
    -v "$(pwd)/certs:/home/hummingbot/certs" \
    hummingbot/hummingbot:sera-amd64
  ```

  In the CLI: set a password (creates `.password_verification`), `connect sera`,
  enter the API key/secret/wallet/private key, then `exit`. Run headless
  afterwards with that password.

The strategy config `conf_serapmm_sera.yml` is plain YAML (not encrypted) but
must also be present in `conf/strategies/`.

### Run with docker compose

A ready-made compose file is included: `docker-compose.sera.yml`. It uses the
`hummingbot/hummingbot:sera-amd64` image, mounts `conf/`, `logs/`, `data/`, and
sets the env vars above. `CONFIG_PASSWORD` is taken from the host env, not
hardcoded.

```bash
CONFIG_PASSWORD=yourpass docker compose -f docker-compose.sera.yml up -d
docker compose -f docker-compose.sera.yml logs -f
```

Stop it with:

```bash
docker compose -f docker-compose.sera.yml down
```

### Run with docker run

The same thing without compose:

```bash
docker run -d --name hummingbot-sera --network host \
  -e CONFIG_PASSWORD="yourpass" \
  -e CONFIG_FILE_NAME=conf_serapmm_sera.yml \
  -e HEADLESS_MODE=true \
  -v "$(pwd)/conf:/home/hummingbot/conf" \
  -v "$(pwd)/logs:/home/hummingbot/logs" \
  -v "$(pwd)/data:/home/hummingbot/data" \
  -v "$(pwd)/certs:/home/hummingbot/certs" \
  hummingbot/hummingbot:sera-amd64

docker logs -f hummingbot-sera
```

### Expected startup signals

```text
Order book is empty for XSGD-MYRT; using oracle mid price ...
Creating bid orders
Creating ask orders
Placed Sera VL batch ...
```

If the strategy starts but creates no orders, work through the same checklist as
section 11 (broker, `CONFIG_PASSWORD`, Wise rate oracle, vault balances, rebuild
after Cython changes). Headless mode auto-enables MQTT; if no broker is running
on `localhost:1883`, the bot logs MQTT connection errors but still trades —
start the broker from section 8 if you want remote control/monitoring.
