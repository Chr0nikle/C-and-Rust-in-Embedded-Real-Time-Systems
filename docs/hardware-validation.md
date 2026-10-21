# Hardware validation plan

Status: **not executed**. Physical campaign execution requires explicit
authorization. No board was flashed during this audit.

## Boards and paths

| Board | Flash/debug | Recommended port handling |
|---|---|---|
| STM32F4 Discovery | probe-rs | auto-detected by probe-rs |
| STM32H723 | probe-rs | auto-detected by probe-rs |
| nRF52840 DK | probe-rs | J-Link VCOM |
| ESP32-C6 | esptool/ESP-IDF | explicit `--port /dev/ttyACM*` |

## Preconditions

- Boards connected and not used by another flashing/debug session.
- Toolchains available from `project/.local/` or PATH.
- ESP-IDF export environment functional.
- `project/target/debug/rtos` built.
- Existing `results/` evidence backed up or archived.
- No running campaign driver.

## Safe validation sequence

```sh
cd project
cargo build -p rtos-cli --offline --locked
./target/debug/rtos list
./target/debug/rtos check freertos --board stm32f4discovery --language c
./target/debug/rtos freertos --board stm32f4discovery --language c --plan
```

Repeat `check`/`--plan` for `stm32h723`, `nrf52840dk`, and `esp32c6`.
Only after those pass, run one smoke configuration at a time.

## Minimal schema-v2 validation

Use one board, one RTOS, one language, one run and a tiny sample budget:

```sh
cd project
cargo build -p rtos-cli --offline --locked
./target/debug/rtos list
./target/debug/rtos check freertos --board stm32f4discovery --language c
./target/debug/rtos freertos --board stm32f4discovery --language c --plan
python3 scripts/benchmarks/campaign.py run \
  --combination freertos/stm32f4discovery/c \
  --fresh --runs 1 --repetitions 3 --warmup 1
```

This must create a new `results/campaigns/campaign-replication-<UTC>/` with
`schema_version: 2`, one unit, `VALIDATION.json`, `provenance/`, and `COMPLETE`.
It is a validation artifact, not scientific evidence, and must not be added to
the thesis campaign lock.

## Full campaign

```sh
cd project
cargo build -p rtos-cli --offline --locked
python3 scripts/benchmarks/campaign.py run \
  --fresh --runs 10 --repetitions 1000 --warmup 100
```

The current readiness matrix, console routing and outstanding ESP32-C6
physical checks are in [benchmark-readiness.md](benchmark-readiness.md).

Expected output is a new schema-v2 bundle under
`results/campaigns/campaign-replication-<UTC>/` containing:

- `campaign.json` and `VALIDATION.json`;
- `provenance/` with Git state, toolchains, devices, preflight and input manifest;
- `benchmarks/<build_id>/` raw and processed records;
- `COMPLETE` only when every unit is recorded.

## Evaluation

1. Validate the record structurally:

   ```sh
   cd project
   python3 -c "import json; from scripts.benchmarks.campaign import validate_campaign_payload; validate_campaign_payload(json.load(open('.../campaign.json')))"
   ```

2. Confirm the input manifest hashes every benchmark file and no analysis output.
3. Update `thesis/analysis/campaign.lock.json` deliberately only after review.
4. Run `make analyze` and inspect `results/processing/<campaign>/<analysis>/provenance.json`.
5. Run `make test-repro`; resolve the known profile metadata drift against the new
   campaign before publishing updated profiles.
6. Build with `make thesis`.
7. Archive with `make archive-evidence` and verify with `make verify-evidence`.

## Safety precautions

- Do not flash multiple boards concurrently without confirming independent
  power and probe ownership.
- Do not disconnect a board during flashing.
- Preserve existing campaign bundles; never overwrite them.
- Stop on validation failures; do not publish partial bundles as complete.
- Record board serials and probe identifiers in `provenance/devices.txt`.

## Validation record: 2026-10-21

Authorized minimal run:

```sh
python3 scripts/benchmarks/campaign.py run \
  --combination freertos/stm32f4discovery/c \
  --fresh --runs 1 --repetitions 3 --warmup 1
```

Result: **PASS**.

- Campaign: `results/campaigns/campaign-replication-20261021T104952Z`
- Board: STM32F4 Discovery, probe `0483:3748:52C3BF6D`
- RTOS/language: FreeRTOS / C
- Build: `freertos-stm32f4-bench-d4cf8483`
- Cases: 29 protocol cases, 3 samples each, 1 warmup, 168 MHz DWT
- Samples: 87
- Validation: `complete`, 1 unit recorded, 0 missing, 0 failed
- Manifest: 5 input files, all hashes match
- Schema: version 2, validation **PASS**
- Thesis lock: **unchanged**

Anomalies and limitations:

- Case 11 samples at load variants 100/150 contain the documented
  `UINT64_MAX` deadline-miss sentinel; processing reports them as misses, not
  overflow.
- This campaign predates the preflight/toolchain-provenance fix. Its
  `preflight-checks.json` says `not_collected`, and its toolchain metadata lacks
  the project-local ARM GCC even though the build used it. Future campaigns
  collect `rtos check` output and resolve project-local ARM/RISC-V tools.
- Three samples per case are a pipeline smoke test, not publication evidence.
  The campaign is deliberately absent from `thesis/analysis/campaign.lock.json`.

## Corrected provenance validation: 2026-10-21

A second authorized minimal run used the committed corrected writer:

```sh
python3 scripts/benchmarks/campaign.py run \
  --combination freertos/stm32f4discovery/c \
  --fresh --runs 1 --repetitions 3 --warmup 1
```

Result: **PASS**.

- Campaign: `results/campaigns/campaign-replication-20261021T112527Z`
- Build: `freertos-stm32f4-bench-833826e3`
- Schema: version 2, validation **PASS**
- Cases/samples: 29 cases, 3 samples each, 1 warmup, 87 samples
- Preflight: `status: collected`, `rtos check` return code 0
- Embedded ARM GCC recorded: `arm-none-eabi-gcc 14.2.1`
- RISC-V GCC and West also recorded
- Manifest: 5 files, all hashes valid
- `COMPLETE`: present
- Thesis lock: unchanged (`campaign-replication-20261019T214853Z`)
- Case 11: 2 documented `UINT64_MAX` deadline-miss sentinels

This campaign remains a hardware validation artifact, not publication evidence.
