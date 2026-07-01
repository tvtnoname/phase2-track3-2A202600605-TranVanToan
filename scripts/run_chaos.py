from __future__ import annotations

import argparse
import random

from reliability_lab.chaos import load_queries, run_simulation
from reliability_lab.config import load_config


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="configs/default.yaml")
    parser.add_argument("--out", default="reports/metrics.json")
    parser.add_argument("--csv-out", default=None, help="Optional path to also write a CSV export")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for reproducible runs")
    args = parser.parse_args()
    random.seed(args.seed)
    config = load_config(args.config)
    metrics = run_simulation(config, load_queries())
    metrics.write_json(args.out)
    print(f"wrote {args.out}")
    if args.csv_out:
        metrics.write_csv(args.csv_out)
        print(f"wrote {args.csv_out}")


if __name__ == "__main__":
    main()
