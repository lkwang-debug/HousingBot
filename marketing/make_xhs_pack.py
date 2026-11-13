"""Export an already submitted report; never collects data or sends messages."""
import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from app.housing.share_pack import ChromeRenderer, export_share_pack, read_frozen_report


def main():
    parser = argparse.ArgumentParser(description="生成小红书群完整分享包（默认最近已发早报）")
    parser.add_argument("--kind", choices=["digest", "evening"], default="digest")
    parser.add_argument("--date", help="原报表日期，如2026-10-23")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--chrome", type=Path)
    args = parser.parse_args()
    report = read_frozen_report(args.kind, args.date)
    result = export_share_pack(report, args.output, renderer=ChromeRenderer(args.chrome))
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"): sys.stdout.reconfigure(encoding="utf-8")
    main()
