from __future__ import annotations

import argparse
import json
from pathlib import Path


def main() -> None:
	parser = argparse.ArgumentParser(description="Remove private mail examples from a diagnostic result.")
	parser.add_argument("input", type=Path)
	parser.add_argument("output", type=Path)
	args = parser.parse_args()

	result = json.loads(args.input.read_text(encoding="utf-8"))
	result.pop("private_examples", None)
	args.output.write_text(
		json.dumps(result, ensure_ascii=False, indent=2) + "\n",
		encoding="utf-8",
	)


if __name__ == "__main__":
	main()
