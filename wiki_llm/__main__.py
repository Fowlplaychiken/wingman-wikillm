"""Read the same local wiki from a shell, RAG client or another assistant."""
import argparse
import json
from pathlib import Path

from .dependencies.wingman_wiki.library import lookup, note_history


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Use another local wiki database")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("lookup", help="Find cited local evidence")
    search.add_argument("query")
    search.add_argument("--build", help="Exact game build")
    history = commands.add_parser("history", help="Read revisions of one local note")
    history.add_argument("topic")
    history.add_argument("--before", type=int)
    args = parser.parse_args()
    result = (lookup(Path(__file__).with_name("guides"), args.query, args.build, args.db)
              if args.command == "lookup" else note_history(args.topic, args.db, args.before))
    print(json.dumps(result, ensure_ascii=True))


if __name__ == "__main__":
    main()
