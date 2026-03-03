#!/usr/bin/env python3
# scripts/first_run.py
"""Process the last N meeting folders. Run this once on first setup."""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from dotenv import load_dotenv
load_dotenv()

from agent.monitor import run_monitor


def main():
    parser = argparse.ArgumentParser(description="First run: process last N meeting folders")
    parser.add_argument("--last-n", type=int, default=5, help="Number of recent folders to process")
    args = parser.parse_args()

    print(f"[first_run] Processing last {args.last_n} folders...")
    run_monitor(last_n=args.last_n)


if __name__ == "__main__":
    main()
