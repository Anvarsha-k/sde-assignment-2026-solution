"""Entry point: python -m controller --url http://127.0.0.1:8700   (reports: python -m controller.report)"""
import argparse
import logging
import sys

from .loop import run
from .settings import Settings


def main() -> int:
    ap = argparse.ArgumentParser(description="Outcome-first controller")
    ap.add_argument("--url", required=True)
    ap.add_argument("--log-level", default="INFO")
    a = ap.parse_args()
    logging.basicConfig(level=a.log_level, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return run(a.url, Settings.from_env())


if __name__ == "__main__":
    sys.exit(main())
