"""python -m backend.investigation TRACE_ID [--strategy baseline|agent] [--output path.json]"""

import argparse
from pathlib import Path

from pydantic import ValidationError

from .baseline import investigate
from .agent import investigate_agent
from .models import TraceQuery
from .model import ChatCompletionsModel, ModelConfig, ModelConfigError


def main():
    parser = argparse.ArgumentParser(description="只读故障调查：固定流程或模型执行器")
    parser.add_argument("trace_id")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strategy", choices=("baseline", "agent"), default="baseline")
    parser.add_argument("--max-tool-calls", type=int, default=6)
    args = parser.parse_args()
    try:
        TraceQuery(trace_id=args.trace_id)
    except ValidationError:
        parser.error("Trace ID 必须为 32 位小写十六进制字符")
    if not 0 <= args.max_tool_calls <= 100:
        parser.error("--max-tool-calls 必须为 0 到 100 的整数")
    if args.strategy == "agent":
        try:
            config = ModelConfig.from_env()
        except ModelConfigError as exc:
            parser.error(str(exc))
        with ChatCompletionsModel(config) as model:
            report = investigate_agent(args.trace_id, model, max_tool_calls=args.max_tool_calls)
    else:
        report = investigate(args.trace_id)
    text = report.model_dump_json(indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
