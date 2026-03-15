# cqscout

A callsign scout for Croatian amateur radio operators. Generates every possible callsign matching a regex pattern, checks which are free against a local callbook, and ranks results by NATO phonetic length and Morse transmission time — helping you find a callsign that is short on air and easy to say.

## Tools

| File | Description |
|------|-------------|
| `cqscout.py` | Main tool — generate, filter, and score callsigns |
| `install_autocomplete.py` | One-shot shell autocomplete installer (bash, zsh, fish, tcsh, pwsh) |
| `callbook.example.json` | Example callbook schema |
| `sync.py` | *(encrypted)* Builds and maintains `callbook.json` |

## Requirements

```bash
pip install requests beautifulsoup4 argcomplete
```

## Callbook

`cqscout.py` reads `callbook.json` to know which callsigns are taken. The file is not included in this repo (it contains personal data). Either run `sync.py` to generate it or provide your own file matching the schema in `callbook.example.json`.

```json
[
  {
    "callsign": "9A0AA",
    "name": "Radioklub Example",
    "address": {
      "street": "Primjer 1",
      "postal_code": "10000",
      "city": "Zagreb"
    },
    "duplicate": false
  }
]
```

## Usage

```
cqscout.py [PATTERN] [options]
```

Default pattern: `9A[1-9][A-Z]{1,3}`

The pattern can be any finite regex — character classes, alternation, bounded repetition. Unbounded quantifiers (`*`, `+`) are rejected since they would produce infinite sets.

### Filtering

| Flag | Effect |
|------|--------|
| `--free` | Only callsigns not in callbook.json |
| `--taken` | Only callsigns already registered |
| `--prefix STR` | Only callsigns starting with prefix, e.g. `9A3` |
| `--city CITY` | Only taken callsigns whose owner is in this city |
| `--postal CODE` | Only taken callsigns with this postal code |
| `--others` | Append taken callsigns *outside* the city/postal filter as a second section |
| `--top N` | Limit to top N results |

### Sorting

| Flag | Sort key |
|------|----------|
| `--sort call` | Alphabetical (default) |
| `--sort nato` | NATO phonetic character count (shortest first) |
| `--sort morse` | Morse transmission time in timing units (shortest first) |
| `--sort overall` | NATO score + Morse score combined |
| `--nato` | Shorthand for `--sort nato` |
| `--morse` | Shorthand for `--sort morse` |

**Morse scoring** uses standard timing units: dot = 1, dash = 3, inter-element gap = 1, inter-letter gap = 3. Only key-down time plus gaps within the callsign are counted (no inter-word gap since a callsign is one word).

### Output

| Flag | Effect |
|------|--------|
| `--owner` | Add owner name and address column for taken callsigns |
| `--stats` | Print summary counts and exit |
| `--out-md FILE` | Write Markdown table to file |
| `--out-csv FILE` | Write CSV to file |

Both `--out-md` and `--out-csv` can be used together. Without either flag the Markdown table is printed to stdout.

## Examples

```bash
# Top 10 free callsigns ranked by combined NATO + Morse score
python3 cqscout.py --free --sort overall --top 10

# All callsigns sorted by Morse time, showing free and taken
python3 cqscout.py --morse

# Free callsigns in the 9A3 district, sorted by NATO phonetic length
python3 cqscout.py --free --nato --prefix 9A3

# Who holds the shortest callsigns in Zagreb?
python3 cqscout.py --taken --city Zagreb --sort morse --owner --top 10

# Zagreb callsigns + everything else taken outside Zagreb
python3 cqscout.py --taken --city Zagreb --sort overall --owner --others

# Custom pattern — two-letter suffix only
python3 cqscout.py --free --sort overall "9A[2-9][A-Z]{2}"

# Export to both Markdown and CSV
python3 cqscout.py --free --sort overall --top 50 --out-md results.md --out-csv results.csv

# Stats
python3 cqscout.py --stats
```

### Sample output

```
python3 cqscout.py --free --sort overall --top 5
```

| Callsign | Status | NATO | Morse | Score NATO | Score Morse | Overall |
| --- | --- | --- | --- | --- | --- | --- |
| 9A5E | free | Nine Alfa Five Echo | ----. .- ..... . | 16 | 41 | 57 |
| 9A6E | free | Nine Alfa Six Echo | ----. .- -.... . | 15 | 43 | 58 |
| 9A4E | free | Nine Alfa Four Echo | ----. .- ....- . | 16 | 43 | 59 |
| 9A5I | free | Nine Alfa Five India | ----. .- ..... .. | 17 | 43 | 60 |
| 9A5T | free | Nine Alfa Five Tango | ----. .- ..... - | 17 | 43 | 60 |

## Autocomplete

Run the installer once to enable tab-completion for all detected shells:

```bash
python3 install_autocomplete.py
source ~/.zshrc   # or ~/.bashrc
```

After that, `--city` and `--postal` complete from the values actually present in `callbook.json`.

## Repository setup

`sync.py` is stored encrypted with [git-crypt](https://github.com/AGWA/git-crypt). `callbook.json` is excluded entirely via `.gitignore`. To unlock on a new machine:

```bash
git-crypt unlock /path/to/callsign-git-crypt.key
```
