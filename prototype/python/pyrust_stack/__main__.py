from __future__ import annotations

import argparse
import json

from .unwinder import read_python_stacks


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read Python stacks from a CPython 3.14 process"
    )
    parser.add_argument("pid", type=int)
    args = parser.parse_args()
    stacks = [stack.to_dict() for stack in read_python_stacks(args.pid)]
    print(json.dumps({"threads": stacks}, separators=(",", ":")))


if __name__ == "__main__":
    main()
