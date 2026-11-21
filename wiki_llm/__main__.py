"""Read and maintain the same offline game library from another assistant."""
import argparse
from dataclasses import asdict
import json
from pathlib import Path

from .dependencies.wingman_wiki.library import check_inventory, library_status, lookup, note_history, read_page, review_queue
from .dependencies.wingman_wiki.json_feeds import parse_feeds
from .dependencies.wingman_wiki.reference import community_db_path
from .dependencies.wingman_wiki.refresh import refresh_once, snapshot_limits
from .dependencies.wingman_wiki.snapshots import import_snapshot
from .dependencies.wingman_wiki.web_sources import parse_roots


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, help="Use another local wiki database")
    parser.add_argument("--wiki", action="append", default=[], help="Enable a wiki home URL (repeatable)")
    parser.add_argument("--api", action="append", default=[], help="Enable a public JSON collection URL or a JSON field mapping (repeatable)")
    parser.add_argument("--no-sc", action="store_true", help="Turn off the Star Citizen preset for this command")
    commands = parser.add_subparsers(dest="command", required=True)
    search = commands.add_parser("lookup", help="Find cited local evidence")
    search.add_argument("query")
    search.add_argument("--build", help="Exact game build")
    history = commands.add_parser("history", help="Read revisions of one local note")
    history.add_argument("topic")
    history.add_argument("--before", type=int)
    review = commands.add_parser("review", help="Find drafts and notes with changed sources")
    review.add_argument("--after", help="Continue from a previous next_after topic ID")
    article = commands.add_parser("read", help="Read a saved page without connecting")
    article.add_argument("url")
    article.add_argument("--offset", type=int, default=0)
    commands.add_parser("status", help="Show local coverage of enabled sources")
    refresh = commands.add_parser("refresh", help="Update enabled sources once")
    refresh.add_argument("--batch-size", type=int, default=5)
    refresh.add_argument("--no-galactapedia", action="store_true", help="Skip the community Galactapedia API")
    snapshot = commands.add_parser("import", help="Import a local MediaWiki XML, article JSONL or the September 2023 Star Citizen archive")
    snapshot.add_argument("path", type=Path)
    inventory = commands.add_parser("verify", help="Compare saved article text against a publisher URL/revision inventory")
    inventory.add_argument("manifest", type=Path)
    inventory.add_argument("--queue-missing", action="store_true",
                           help="Queue undiscovered articles of enabled custom wikis for the next refresh")
    args = parser.parse_args()
    db = community_db_path(args.db)
    try:
        roots = parse_roots("\n".join(args.wiki))
        feeds = parse_feeds("\n".join(args.api))
        options = {"include_star_citizen": not args.no_sc, "roots": roots, "feeds": feeds}
        if args.command == "lookup":
            result = lookup(Path(__file__).with_name("guides"), args.query, args.build, db, **options)
        elif args.command == "history":
            result = note_history(args.topic, db, args.before)
        elif args.command == "review":
            result = review_queue(db, after=args.after, feeds=feeds)
        elif args.command == "read":
            result = read_page(db, args.url, offset=args.offset, **options)
        elif args.command == "status":
            result = library_status(db, **options)
        elif args.command == "verify":
            result = check_inventory(db, args.manifest, queue_missing=args.queue_missing, **options)
        elif args.command == "import":
            result = import_snapshot(db, args.path, **snapshot_limits())
        else:
            result = asdict(refresh_once(db, batch_size=args.batch_size, enabled_catalogs=() if args.no_sc else None,
                                         roots=roots, feeds=feeds, import_snapshots=True,
                                         include_sc_edition=not args.no_sc,
                                         sync_galactapedia=not (args.no_sc or args.no_galactapedia)))
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    print(json.dumps(result, ensure_ascii=True))
    if args.command == "verify" and result["status"] == "incomplete":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
