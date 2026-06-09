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

The client rate oracle should be set to Wise for `XSGD-USDC`:

```yaml
rate_oracle_source:
  name: wise
  trading_pairs: XSGD-USDC
  currency_map: XSGD:SGD,USDC:USD,USDT:USD,DAI:USD
  source_amount: 100
```

When the Sera order book is empty, the connector should log a message like:

```text
Order book is empty for XSGD-USDC; using oracle mid price ...
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

## 9. Ensure Sera Vault Balances Are Available

The Sera connector reports Hummingbot available balances from Sera `vault_available`, not from wallet totals. If PMM logs proposals with `buys=0` and `sells=0`, check whether vault balances are zero or frozen.

Use Sera testnet UI or the Sera account API to deposit testnet tokens into the vault. For `conf_pmm_sera.yml`, keep at least enough vault balance for one bid and one ask:

```text
XSGD vault_available >= 20
USDC vault_available >= 20 * oracle_mid_price
```

After funds are in the vault, Hummingbot can reserve part of the balance as `vault_frozen` for live orders.

## 10. Start Hummingbot Headless with PMM

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
