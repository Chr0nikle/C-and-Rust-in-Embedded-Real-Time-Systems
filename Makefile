# Offline targets never touch hardware. Hardware targets are explicit.

PYTHON ?= python3
ANALYSIS_PYTHON ?= $(if $(wildcard thesis/.venv/bin/python),$(CURDIR)/thesis/.venv/bin/python,python3)

.PHONY: help check test test-repro check-firmware check-firmware-lint check-hardware benchmark analyze safety thesis archive-evidence verify-evidence report-processing

help:
	@printf '%s\n' \
		'make check             offline Rust + project Python checks' \
		'make test              offline unit/integration suites' \
		'make test-repro        full scientific reproducibility checks' \
		'make check-firmware    offline firmware formatting/tests' \
		'make check-firmware-lint  firmware Clippy (known warnings)' \
		'make check-hardware    board discovery and build plans (no flashing)' \
		'make benchmark         run the benchmark campaign (hardware)' \
		'make analyze           regenerate thesis analysis from the locked campaign' \
		'make safety            run the full safety pipeline' \
		'make thesis            compile the thesis PDF' \
		'make archive-evidence  write a deterministic evidence archive' \
		'make verify-evidence ARCHIVE=...  verify an evidence archive' \
		'make report-processing  report duplicate processing provenance records'

check:
	cd project && $(PYTHON) -m unittest discover -s scripts/maintenance/tests
	cd project && cargo fmt --all -- --check
	cd project && cargo check --workspace --offline --locked
	cd project && cargo clippy --workspace --all-targets --offline --locked -- -D warnings
	cd project && cargo test --workspace --offline --locked

test: check check-firmware
	cd thesis/analysis && $(PYTHON) analyze.py test
	cd project/safety && $(PYTHON) safety.py test

test-repro:
	cd project/safety && $(PYTHON) safety.py test --tier repro
	cd thesis/analysis && $(PYTHON) analyze.py test

check-firmware:
	cd project && cargo fmt --manifest-path firmware/Cargo.toml --all -- --check
	cd project && cargo test --manifest-path firmware/Cargo.toml --workspace --offline --locked

check-firmware-lint:
	cd project && cargo clippy --manifest-path firmware/Cargo.toml --workspace --all-targets --offline --locked -- -D warnings

check-hardware:
	cd project && ./target/debug/rtos list
	cd project && ./target/debug/rtos freertos --board stm32f4discovery --language c --plan
	cd project && ./target/debug/rtos freertos --board stm32h723 --language c --plan
	cd project && ./target/debug/rtos freertos --board nrf52840dk --language c --plan
	cd project && ./target/debug/rtos freertos --board esp32c6 --language c --plan

benchmark:
	cd project && $(PYTHON) scripts/benchmarks/campaign.py run --fresh

analyze:
	cd thesis/analysis && $(ANALYSIS_PYTHON) analyze.py all

safety:
	cd project/safety && $(PYTHON) safety.py all

thesis:
	cd thesis && typst compile --root .. thesis.typ exports/thesis_nenning.pdf

archive-evidence:
	$(PYTHON) project/scripts/maintenance/archive_evidence.py $(if $(OUTPUT),--output $(OUTPUT),)

verify-evidence:
	@test -n "$(ARCHIVE)" || (printf '%s\n' 'ARCHIVE=path is required' >&2; exit 2)
	$(PYTHON) project/scripts/maintenance/archive_evidence.py --verify "$(ARCHIVE)"

report-processing:
	$(PYTHON) project/scripts/maintenance/report_processing_duplicates.py
