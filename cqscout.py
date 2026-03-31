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
DEFAULT_PATTERN = r"9A[0-9][A-Z]{1,3}"

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


# ---------------------------------------------------------------------------
# Leet-speak digit → letter mapping
# ---------------------------------------------------------------------------

LEET: dict[str, list[str]] = {
    # digits → letters they visually resemble
    '0': ['O'],
    '1': ['I', 'L'],
    '2': ['R', 'Z'],
    '3': ['E', 'B'],   # 3 looks like E; reversed 3 looks like B
    '4': ['A'],
    '5': ['S'],
    '6': ['G', 'B'],
    '7': ['T', 'L', 'Y'],  # 7 → T (crossbar), L and Y also map to 7 in leet
    '8': ['B'],
    '9': ['G', 'Q', 'P'],  # 9 resembles g, q, and p
    # letters → diacritic variants (language-specific)
    'C': ['Ć', 'Č'],
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
# Word matching via leet-speak
# ---------------------------------------------------------------------------

# Reverse leet: letter → set of callsign chars (itself + digits that expand to it)
_REVERSE_LEET: dict[str, set[str]] = {c: {c} for c in 'ABCDEFGHIJKLMNOPQRSTUVWXYZ'}
for _digit, _letters in LEET.items():
    for _letter in _letters:
        _REVERSE_LEET.setdefault(_letter, {_letter}).add(_digit)


def expand_leet(segment: str) -> list[str]:
    """All word strings obtainable from a callsign segment by leet substitution."""
    if not segment:
        return ['']
    char, rest = segment[0], segment[1:]
    suffixes = expand_leet(rest)
    letters = LEET[char] if char in LEET else [char]
    return [l + s for l in letters for s in suffixes]


def load_wordlist(path: str, min_len: int = 3, max_len: int = 6) -> set[str]:
    """Load a word list file; return uppercase words within the given length range.

    Accepts any word whose every character has a reverse-leet mapping (i.e. can be
    represented by a callsign character), so Croatian diacritics like Č/Ć are included
    as long as they appear in LEET.
    """
    words: set[str] = set()
    with open(path, encoding='utf-8', errors='ignore') as f:
        for line in f:
            w = line.strip().upper()
            if w and w.isalpha() and min_len <= len(w) <= max_len and all(c in _REVERSE_LEET for c in w):
                words.add(w)
    return words


def build_word_index(wordset: set[str]) -> dict[str, list[str]]:
    """
    Build a reverse index: callsign_segment → list of words it decodes to.

    For each word, generates all callsign segment strings (via reverse leet) that
    would expand back to that word. Looking up a callsign window in the index is
    O(1) rather than requiring per-callsign leet expansion.
    """
    index: dict[str, list[str]] = {}
    for word in wordset:
        # Generate all callsign-character representations of this word
        segs = ['']
        for ch in word:
            cs_chars = sorted(_REVERSE_LEET.get(ch, {ch}))
            segs = [s + c for s in segs for c in cs_chars]
        for seg in segs:
            index.setdefault(seg, []).append(word)
    return index


def callsign_words(callsign: str, word_index: dict[str, list[str]],
                   min_len: int = 3, max_len: int = 6,
                   anchor_left: bool = False, anchor_right: bool = False) -> list[str]:
    """Return sorted list of unique words from sliding windows of the callsign.

    anchor_left:  word must start at position 0.
    anchor_right: word must end at the last character.
    """
    found: set[str] = set()
    n = len(callsign)
    starts = range(0, 1) if anchor_left else range(n)
    for start in starts:
        for length in range(min_len, min(max_len + 1, n - start + 1)):
            if anchor_right and start + length != n:
                continue
            segment = callsign[start:start + length]
            for word in word_index.get(segment, []):
                found.add(word)
    return sorted(found)


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
        choices=["call", "nato", "morse", "overall", "word"],
        metavar="{call,nato,morse,overall,word}",
        help="Sort by: call (alphabetical), nato (phonetic length), morse (tx time), overall (nato+morse), word (first matched word)",
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
    parser.add_argument(
        "--words",
        nargs="+",
        metavar="FILE",
        help="Word list files (one word per line). Show only callsigns that spell a word.",
    )
    parser.add_argument(
        "--word-len",
        nargs="+",
        type=int,
        metavar="N",
        default=[3, 6],
        help="Word length filter: one value for exact length, two for N–M range (default: 3 6).",
    )
    parser.add_argument(
        "--word-left",
        action="store_true",
        help="Word must start at the first character of the callsign.",
    )
    parser.add_argument(
        "--word-right",
        action="store_true",
        help="Word must end at the last character of the callsign.",
    )
    argcomplete.autocomplete(parser)
    args = parser.parse_args()

    wlen = args.word_len
    if len(wlen) == 1:
        min_wlen, max_wlen = wlen[0], wlen[0]
    elif len(wlen) == 2:
        min_wlen, max_wlen = wlen[0], wlen[1]
    else:
        print("Error: --word-len accepts 1 or 2 values", file=sys.stderr)
        sys.exit(1)
    if not (1 <= min_wlen <= max_wlen <= 20):
        print("Error: --word-len values must satisfy 1 ≤ N ≤ M ≤ 20", file=sys.stderr)
        sys.exit(1)

    wordset: set[str] = set()
    if args.words:
        for path in args.words:
            try:
                wordset |= load_wordlist(path, min_wlen, max_wlen)
            except OSError as e:
                print(f"Error loading word list {path}: {e}", file=sys.stderr)
                sys.exit(1)
        word_index = build_word_index(wordset)
    else:
        word_index: dict[str, list[str]] = {}

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

    def word_sort_key(cs: str) -> tuple:
        words = callsign_words(cs, word_index, min_wlen, max_wlen, args.word_left, args.word_right)
        return (words[0] if words else "", cs)

    sort_keys = {
        "call":    lambda cs: cs,
        "nato":    lambda cs: (nato_score(cs), cs),
        "morse":   lambda cs: (morse_score(cs), cs),
        "overall": lambda cs: (nato_score(cs) + morse_score(cs), cs),
        "word":    word_sort_key,
    }
    results = sorted(results, key=sort_keys[sort_by])

    if word_index:
        results = [cs for cs in results if callsign_words(cs, word_index, min_wlen, max_wlen, args.word_left, args.word_right)]

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

    header_cols = ["Callsign"]
    if word_index:
        header_cols.append("Words")
    header_cols += ["Status", "NATO", "Morse", "Score NATO", "Score Morse", "Overall"]
    if args.owner:
        header_cols.append("Owner")
    HEADER = tuple(header_cols)
    SEP    = tuple("---" for _ in HEADER)

    def row(cs: str, status: str | None = None) -> tuple[str, ...]:
        ns = nato_score(cs)
        ms = morse_score(cs)
        if status is None:
            status = "free" if cs not in taken else "taken"
        cells = [cs]
        if word_index:
            cells.append(", ".join(callsign_words(cs, word_index, min_wlen, max_wlen, args.word_left, args.word_right)) or "—")
        cells += [
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
        def aligned_fmt(all_rows):
            widths = [max(len(r[i]) for r in all_rows) for i in range(len(all_rows[0]))]
            def fmt(cells):
                padded = [c.ljust(widths[i]) for i, c in enumerate(cells)]
                return "| " + " | ".join(padded) + " |"
            return fmt

        main_rows = [HEADER, SEP] + [row(cs) for cs in results]
        fmt = aligned_fmt(main_rows)
        for r in main_rows:
            f.write(fmt(r) + "\n")
        if others:
            f.write(f"\n**Others ({len(others)} taken outside filter)**\n\n")
            other_rows = [HEADER, SEP] + [row(cs, status="other") for cs in others]
            fmt2 = aligned_fmt(other_rows)
            for r in other_rows:
                f.write(fmt2(r) + "\n")

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
