#!/usr/bin/env python3
"""
cqscout — callsign scout for ham radio operators.

Generate callsigns from a regex pattern and find those that are simultaneously
free (not in callbook.json), short in NATO phonetics, and short in Morse code.

Each flag is a filter/sort criterion — combining them narrows to the intersection:
  --free   keep only callsigns not registered in callbook.json
  --nato   sort by total NATO phonetic character count (shortest first)
  --morse  sort by total Morse transmission time in timing units (shortest first)

When both --nato and --morse are active the sort key is their combined score.
Free/taken status is always shown in the output so the Venn picture is clear.

Default pattern: 9A[2-9][A-Z]{1,3}

Supported regex: literals, [...] classes, (a|b) alternation, groups,
  {n}/{m,n}/? quantifiers, ^ $ anchors. No .  *  +  [^...]
"""

import argparse
import argcomplete
import csv
import itertools
import json
import re
import signal
import sys
import warnings

signal.signal(signal.SIGPIPE, signal.SIG_DFL)

with warnings.catch_warnings():
    warnings.simplefilter("ignore", DeprecationWarning)
    import sre_parse

CALLBOOK = "callbook.json"
DEFAULT_PATTERN = r"9A[1-9][A-Z]{1,3}"

# ---------------------------------------------------------------------------
# NATO phonetic alphabet (ITU standard)
# ---------------------------------------------------------------------------

NATO = {
    "A": "Alfa",      "B": "Bravo",    "C": "Charlie",  "D": "Delta",
    "E": "Echo",      "F": "Foxtrot",  "G": "Golf",     "H": "Hotel",
    "I": "India",     "J": "Juliet",   "K": "Kilo",     "L": "Lima",
    "M": "Mike",      "N": "November", "O": "Oscar",    "P": "Papa",
    "Q": "Quebec",    "R": "Romeo",    "S": "Sierra",   "T": "Tango",
    "U": "Uniform",   "V": "Victor",   "W": "Whiskey",  "X": "X-ray",
    "Y": "Yankee",    "Z": "Zulu",
    "0": "Zero",      "1": "One",      "2": "Two",      "3": "Three",
    "4": "Four",      "5": "Five",     "6": "Six",      "7": "Seven",
    "8": "Eight",     "9": "Nine",
}

# ---------------------------------------------------------------------------
# Morse code
# ---------------------------------------------------------------------------

MORSE = {
    "A": ".-",    "B": "-...",  "C": "-.-.",  "D": "-..",
    "E": ".",     "F": "..-.",  "G": "--.",   "H": "....",
    "I": "..",    "J": ".---",  "K": "-.-",   "L": ".-..",
    "M": "--",    "N": "-.",    "O": "---",   "P": ".--.",
    "Q": "--.-",  "R": ".-.",   "S": "...",   "T": "-",
    "U": "..-",   "V": "...-",  "W": ".--",   "X": "-..-",
    "Y": "-.--",  "Z": "--..",
    "0": "-----", "1": ".----", "2": "..---", "3": "...--",
    "4": "....-", "5": ".....", "6": "-....", "7": "--...",
    "8": "---..", "9": "----.",
}


def nato_score(callsign: str) -> int:
    """Total character count of all NATO phonetic words."""
    return sum(len(NATO[c]) for c in callsign if c in NATO)


def morse_score(callsign: str) -> int:
    """
    Total Morse transmission time in standard timing units:
      dot = 1, dash = 3, inter-element gap = 1, inter-letter gap = 3.
    A callsign is one word so inter-word gaps (7) don't apply.
    """
    chars = [MORSE[c] for c in callsign if c in MORSE]
    total = 0
    for i, code in enumerate(chars):
        total += code.count(".") + code.count("-") * 3  # key-down
        total += (len(code) - 1)                         # inter-element gaps
        if i < len(chars) - 1:
            total += 3                                   # inter-letter gap
    return total


def nato_spelling(callsign: str) -> str:
    return " ".join(NATO.get(c, c) for c in callsign)


def morse_spelling(callsign: str) -> str:
    return " ".join(MORSE.get(c, c) for c in callsign)


# ---------------------------------------------------------------------------
# Regex → string enumeration
# ---------------------------------------------------------------------------

def _expand_class(av: list) -> list[str]:
    chars = []
    negate = False
    for op, val in av:
        if op == sre_parse.NEGATE:
            negate = True
        elif op == sre_parse.LITERAL:
            chars.append(chr(val))
        elif op == sre_parse.RANGE:
            lo, hi = val
            chars.extend(chr(c) for c in range(lo, hi + 1))
        elif op == sre_parse.CATEGORY:
            chars.extend(_expand_category(val))
        else:
            raise ValueError(f"Unsupported character class element: op={op}")
    if negate:
        raise ValueError("Negated character classes [^...] are not supported")
    return chars


def _expand_category(category) -> list[str]:
    import string
    mapping = {
        sre_parse.CATEGORY_DIGIT: string.digits,
        sre_parse.CATEGORY_NOT_DIGIT: "".join(chr(c) for c in range(128) if chr(c) not in string.digits),
        sre_parse.CATEGORY_WORD: string.ascii_letters + string.digits + "_",
        sre_parse.CATEGORY_SPACE: string.whitespace,
    }
    if category not in mapping:
        raise ValueError(f"Unsupported category: {category}")
    return list(mapping[category])


def _enumerate(parsed) -> list[str]:
    segments: list[list[str]] = []
    for op, av in parsed:
        if op == sre_parse.LITERAL:
            segments.append([chr(av)])
        elif op == sre_parse.NOT_LITERAL:
            raise ValueError("Negated literals are not supported")
        elif op == sre_parse.IN:
            segments.append(_expand_class(av))
        elif op == sre_parse.ANY:
            raise ValueError("Wildcard '.' is not supported — use a character class instead")
        elif op in (sre_parse.MAX_REPEAT, sre_parse.MIN_REPEAT):
            min_c, max_c, subpat = av
            if max_c == sre_parse.MAXREPEAT:
                raise ValueError("Unbounded quantifiers (* and +) are not supported — use {min,max}")
            sub = _enumerate(subpat)
            expansions: list[str] = []
            for count in range(min_c, max_c + 1):
                if count == 0:
                    expansions.append("")
                else:
                    for combo in itertools.product(sub, repeat=count):
                        expansions.append("".join(combo))
            segments.append(expansions)
        elif op == sre_parse.SUBPATTERN:
            segments.append(_enumerate(av[-1]))
        elif op == sre_parse.BRANCH:
            _, branches = av
            branch_strings: list[str] = []
            for branch in branches:
                branch_strings.extend(_enumerate(branch))
            segments.append(branch_strings)
        elif op == sre_parse.AT:
            pass
        elif op == sre_parse.GROUPREF:
            raise ValueError("Back-references are not supported")
        else:
            raise ValueError(f"Unsupported regex feature: op={op} av={av}")
    if not segments:
        return [""]
    return ["".join(combo) for combo in itertools.product(*segments)]


def generate_from_pattern(pattern: str) -> list[str]:
    try:
        parsed = sre_parse.parse(pattern)
    except re.error as e:
        raise ValueError(f"Invalid regex: {e}") from e
    return sorted(_enumerate(parsed))


# ---------------------------------------------------------------------------
# Callbook
# ---------------------------------------------------------------------------

def load_callbook() -> tuple[set[str], dict[str, dict]]:
    """
    Returns (taken_set, owners) where owners maps callsign → {name, address}.
    """
    try:
        with open(CALLBOOK, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"Warning: {CALLBOOK} not found — assuming no callsigns are taken", file=sys.stderr)
        return set(), {}
    taken = {entry["callsign"] for entry in data}
    owners = {entry["callsign"]: entry for entry in data}
    return taken, owners


def _callbook_values(field: str) -> list[str]:
    """Read unique values of address[field] from callbook.json for use as completers."""
    try:
        with open(CALLBOOK, encoding="utf-8") as f:
            data = json.load(f)
        return sorted({e["address"][field] for e in data if field in e.get("address", {})})
    except Exception:
        return []


def city_completer(**_):
    return _callbook_values("city")


def postal_completer(**_):
    return _callbook_values("postal_code")


def format_owner(entry: dict) -> str:
    addr = entry.get("address", {})
    city = addr.get("city", "")
    postal = addr.get("postal_code", "")
    street = addr.get("street", "") or addr.get("raw", "")
    city_part = f"{city} ({postal})" if postal else city
    parts = [p for p in [entry.get("name", ""), city_part, street] if p]
    return ", ".join(parts)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Find callsigns at the intersection of: free, short NATO, short Morse.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"Default pattern: {DEFAULT_PATTERN}\n\nExamples:\n"
               f"  %(prog)s --free --nato --morse          # best free callsigns on all metrics\n"
               f"  %(prog)s --morse                        # all callsigns sorted by Morse time\n"
               f"  %(prog)s --free --nato '9A3[A-Z]{{2}}'  # free 9A3xx sorted by NATO\n",
    )
    parser.add_argument(
        "pattern",
        nargs="?",
        default=DEFAULT_PATTERN,
        metavar="PATTERN",
        help=f"Regex pattern (default: {DEFAULT_PATTERN})",
    )
    filter_group = parser.add_mutually_exclusive_group()
    filter_group.add_argument(
        "--free",
        action="store_true",
        help="Show only callsigns not registered in callbook.json",
    )
    filter_group.add_argument(
        "--taken",
        action="store_true",
        help="Show only callsigns already registered in callbook.json",
    )
    parser.add_argument(
        "--nato",
        action="store_true",
        help="Shorthand for --sort nato",
    )
    parser.add_argument(
        "--morse",
        action="store_true",
        help="Shorthand for --sort morse",
    )
    parser.add_argument(
        "--sort",
        choices=["call", "nato", "morse", "overall"],
        metavar="{call,nato,morse,overall}",
        help="Sort by: call (alphabetical), nato (phonetic length), morse (tx time), overall (nato+morse)",
    )
    parser.add_argument(
        "--prefix",
        metavar="STR",
        help="Only show callsigns starting with this prefix (e.g. 9A3)",
    )
    parser.add_argument(
        "--city",
        metavar="CITY",
        help="Only show callsigns whose owner is in this city (taken callsigns only)",
    ).completer = city_completer
    parser.add_argument(
        "--postal",
        metavar="CODE",
        help="Only show callsigns whose owner has this postal code (taken callsigns only)",
    ).completer = postal_completer
    parser.add_argument(
        "--top",
        metavar="N",
        type=int,
        help="Show only the top N results",
    )
    parser.add_argument(
        "--stats",
        action="store_true",
        help="Print summary statistics and exit",
    )
    parser.add_argument(
        "--owner",
        action="store_true",
        help="Add owner column (name, city, street) for taken callsigns",
    )
    parser.add_argument(
        "--others",
        action="store_true",
        help="Append taken callsigns outside the --city/--postal filter as a separate section",
    )
    parser.add_argument(
        "--out-md",
        metavar="FILE",
        help="Write results as a Markdown table to FILE",
    )
    parser.add_argument(
        "--out-csv",
        metavar="FILE",
        help="Write results as CSV to FILE",
    )
    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    try:
        all_callsigns = generate_from_pattern(args.pattern)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    taken, owners = load_callbook()
    all_set = set(all_callsigns)
    in_pattern = taken & all_set
    outside_pattern = taken - all_set
    free = [cs for cs in all_callsigns if cs not in taken]

    if args.stats:
        print(f"Pattern:            {args.pattern}")
        print(f"Total possible:     {len(all_callsigns):,}")
        print(f"Taken (in pattern): {len(in_pattern):,}")
        print(f"Taken (outside):    {len(outside_pattern):,}  (other prefixes, clubs, etc.)")
        print(f"Free:               {len(free):,}")
        return

    # Filter to free, taken, or all
    if args.free:
        results = free
    elif args.taken:
        results = [cs for cs in all_callsigns if cs in taken]
    else:
        results = all_callsigns

    # Prefix filter
    if args.prefix:
        prefix = args.prefix.upper()
        results = [cs for cs in results if cs.startswith(prefix)]

    # City / postal filters — match against owner address (free callsigns have no owner)
    # Snapshot taken callsigns before filtering so --others can compute the remainder.
    taken_before_filter = [cs for cs in results if cs in taken]
    if args.city:
        city = args.city.strip()
        results = [
            cs for cs in results
            if owners.get(cs, {}).get("address", {}).get("city", "").lower() == city.lower()
        ]
    if args.postal:
        results = [
            cs for cs in results
            if owners.get(cs, {}).get("address", {}).get("postal_code", "") == args.postal.strip()
        ]

    # Resolve sort key: --sort takes precedence; --nato/--morse are shorthands
    sort_by = args.sort
    if sort_by is None:
        if args.nato and args.morse:
            sort_by = "overall"
        elif args.nato:
            sort_by = "nato"
        elif args.morse:
            sort_by = "morse"
        else:
            sort_by = "call"

    sort_keys = {
        "call":    lambda cs: cs,
        "nato":    lambda cs: (nato_score(cs), cs),
        "morse":   lambda cs: (morse_score(cs), cs),
        "overall": lambda cs: (nato_score(cs) + morse_score(cs), cs),
    }
    results = sorted(results, key=sort_keys[sort_by])

    if args.top:
        results = results[:args.top]

    # Callsigns taken but outside the city/postal filter
    results_set = set(results)
    others = (
        sorted(
            [cs for cs in taken_before_filter if cs not in results_set],
            key=sort_keys[sort_by],
        )
        if args.others and (args.city or args.postal)
        else []
    )

    header_cols = ["Callsign", "Status", "NATO", "Morse", "Score NATO", "Score Morse", "Overall"]
    if args.owner:
        header_cols.append("Owner")
    HEADER = tuple(header_cols)
    SEP    = tuple("---" for _ in HEADER)

    def row(cs: str, status: str | None = None) -> tuple[str, ...]:
        ns = nato_score(cs)
        ms = morse_score(cs)
        if status is None:
            status = "free" if cs not in taken else "taken"
        cells = [
            cs,
            status,
            nato_spelling(cs),
            morse_spelling(cs),
            str(ns),
            str(ms),
            str(ns + ms),
        ]
        if args.owner:
            entry = owners.get(cs)
            cells.append(format_owner(entry) if entry else "—")
        return tuple(cells)

    def write_md(f):
        def fmt(cells):
            return "| " + " | ".join(cells) + " |"
        f.write(fmt(HEADER) + "\n")
        f.write(fmt(SEP)    + "\n")
        for cs in results:
            f.write(fmt(row(cs)) + "\n")
        if others:
            f.write(f"\n**Others ({len(others)} taken outside filter)**\n\n")
            f.write(fmt(HEADER) + "\n")
            f.write(fmt(SEP)    + "\n")
            for cs in others:
                f.write(fmt(row(cs, status="other")) + "\n")

    def write_csv(f):
        writer = csv.writer(f)
        writer.writerow(HEADER)
        for cs in results:
            writer.writerow(row(cs))
        if others:
            writer.writerow([])  # blank separator row
            writer.writerow([f"# others ({len(others)} taken outside filter)"])
            for cs in others:
                writer.writerow(row(cs, status="other"))

    # Always write Markdown to stdout unless a file output is requested
    if args.out_md:
        with open(args.out_md, "w", encoding="utf-8") as f:
            write_md(f)
        print(f"Wrote {len(results):,} callsigns to {args.out_md}", file=sys.stderr)
    elif not args.out_csv:
        write_md(sys.stdout)

    if args.out_csv:
        with open(args.out_csv, "w", encoding="utf-8", newline="") as f:
            write_csv(f)
        print(f"Wrote {len(results):,} callsigns to {args.out_csv}", file=sys.stderr)


if __name__ == "__main__":
    try:
        main()
    except BrokenPipeError:
        sys.exit(0)
