name: Traficar Warszawa

on:
  schedule:
    # GitHub may start these late or skip some. Each run reads for ~5h45m and the
    # concurrency group queues the next one, so it takes over as soon as this one ends.
    - cron: "*/20 * * * *"
  workflow_dispatch:

concurrency:
  group: traficar-collector
  cancel-in-progress: false

permissions:
  contents: write

jobs:
  collect:
    runs-on: ubuntu-latest
    timeout-minutes: 355
    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Configure git
        run: |
          git config user.name "traficar-bot"
          git config user.email "41898282+github-actions[bot]@users.noreply.github.com"

      - name: Test detection
        run: python -m unittest test_collector test_report

      - name: Read the map every minute
        run: python traficar_collector.py --minutes 345 --git
