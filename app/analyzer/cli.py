"""Run the analyzer from the command line.

    python -m app.analyzer.cli "message text" [--lang hi] [--image screenshot.png]
"""

import argparse
import asyncio
import sys
from pathlib import Path

from app.analyzer.combine import analyze_message
from app.analyzer.prompts import LANGUAGES


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.analyzer.cli", description="Check a message for scam signs.")
    parser.add_argument("text", nargs="?", help="message text to check")
    parser.add_argument("--lang", default="en", choices=sorted(LANGUAGES), help="reply language")
    parser.add_argument("--image", type=Path, help="path to a screenshot to check")
    args = parser.parse_args(argv)

    if not args.text and not args.image:
        parser.error("give message text, --image, or both")

    image_bytes = args.image.read_bytes() if args.image else None
    result = asyncio.run(analyze_message(text=args.text, image_bytes=image_bytes, language=args.lang))
    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
