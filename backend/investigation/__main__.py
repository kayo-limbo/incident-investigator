"""python -m backend.investigation TRACE_ID [--output path.json]"""

import argparse
from pathlib import Path

from pydantic import ValidationError

from .baseline import investigate


def main():
    parser = argparse.ArgumentParser(description="只读固定流程调查（不使用模型）")
    parser.add_argument("trace_id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    try:
        report = investigate(args.trace_id)
    except ValidationError:
        parser.error("Trace ID 必须为 32 位小写十六进制字符")
    text = report.model_dump_json(indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
