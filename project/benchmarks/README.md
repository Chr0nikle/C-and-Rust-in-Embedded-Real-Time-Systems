# Benchmark definitions

- `suite.toml` and `specifications.md` are the live benchmark contract.
- `campaign-schema.json` defines the schema-v2 campaign record written by
  `project/scripts/benchmarks/campaign.py`.
- `archive/` contains retired source snapshots of benchmark definitions. It is
  not run evidence; experimental evidence lives under `results/`.

The suite version is identified by hashing `suite.toml` and
`specifications.md` into every new campaign record.
