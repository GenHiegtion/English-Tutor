"""Build src/english_tutor/data/cefr_wordlist.yaml from the Open Language Profiles word lists.

Sources (https://github.com/openlanguageprofiles/olp-en-cefrj):
  - CEFR-J Vocabulary Profile 1.5 (A1-B2), Tono Laboratory, TUFS: free for research and
    commercial use with citation.
  - Octanove Vocabulary Profile C1/C2 1.0, Octanove Labs: CC BY-SA 4.0.

The lists hold base forms only. The guard already strips regular endings (-s, -ed, -ing...), so
this script adds what it cannot guess: irregular verb forms, irregular plurals and comparatives,
contractions and a few everyday words missing from the lists. Each added form takes the level of
its base word.

Run:  uv run python scripts/build_wordlist.py [--source DIR]   (DIR: already downloaded CSVs)
"""

from __future__ import annotations

import argparse
import csv
import io
import re
import unicodedata
import urllib.request
from pathlib import Path

import yaml

BASE_URL = "https://raw.githubusercontent.com/openlanguageprofiles/olp-en-cefrj/master"
FILES = ("cefrj-vocabulary-profile-1.5.csv", "octanove-vocabulary-profile-c1c2-1.0.csv")
OUT = (
    Path(__file__).resolve().parent.parent / "src" / "english_tutor" / "data" / "cefr_wordlist.yaml"
)
LEVELS = ("A1", "A2", "B1", "B2", "C1", "C2")
TOKEN = re.compile(r"^[a-z]+(?:'[a-z]+)?$")

# base form, then its irregular forms (past, past participle, other spellings)
IRREGULAR_VERBS = """
arise arose arisen; awake awoke awoken; bear bore borne born; beat beaten; become became;
begin began begun; bend bent; bet bet; bind bound; bite bit bitten; bleed bled; blow blew blown;
break broke broken; breed bred; bring brought; broadcast broadcast; build built; burn burnt;
burst burst; buy bought; catch caught; choose chose chosen; cling clung; come came; cost cost;
creep crept; cut cut; deal dealt; dig dug; do does did done doing; draw drew drawn;
dream dreamt dreamed; drink drank drunk; drive drove driven; eat ate eaten; fall fell fallen;
feed fed; feel felt; fight fought; find found; flee fled; fly flew flown;
forbid forbade forbidden; forget forgot forgotten; forgive forgave forgiven;
freeze froze frozen; get got gotten; give gave given; go goes went gone; grind ground;
grow grew grown; hang hung; have has had having; hear heard; hide hid hidden; hit hit;
hold held; hurt hurt; keep kept; kneel knelt; know knew known; lay laid; lead led; lean leant;
leap leapt; learn learnt; leave left; lend lent; let let; lie lay lain lying; light lit;
lose lost; make made; mean meant; meet met; mistake mistook mistaken; overcome overcame;
pay paid; prove proven; put put; quit quit; read read; ride rode ridden; ring rang rung;
rise rose risen; run ran; say said; see saw seen; seek sought; sell sold; send sent; set set;
sew sewn; shake shook shaken; shine shone; shoot shot; show shown; shrink shrank shrunk;
shut shut; sing sang sung; sink sank sunk; sit sat; sleep slept; slide slid; smell smelt;
speak spoke spoken; speed sped; spell spelt; spend spent; spill spilt; spin spun; spit spat;
split split; spoil spoilt; spread spread; spring sprang sprung; stand stood; steal stole stolen;
stick stuck; sting stung; strike struck; swear swore sworn; sweep swept; swim swam swum;
swing swung; take took taken; teach taught; tear tore torn; tell told; think thought;
throw threw thrown; understand understood; upset upset; wake woke woken; wear wore worn;
weep wept; win won; wind wound; withdraw withdrew withdrawn; write wrote written;
"""
# base form, then its irregular plurals, comparatives or modal forms
IRREGULAR_OTHER = """
child children; man men; woman women; person people; foot feet; tooth teeth; mouse mice;
goose geese; knife knives; wife wives; life lives; leaf leaves; half halves; shelf shelves;
wolf wolves; thief thieves; potato potatoes; tomato tomatoes; hero heroes; good better best;
well better best; bad worse worst; badly worse worst; far farther farthest further furthest;
many more most; much more most; little less least; can could; will would; shall should;
may might;
"""
# Always allowed (A1): contractions, short forms and everyday words the lists leave out.
EVERYDAY = """
i'm i've i'd i'll you're you've you'd you'll he's he'd he'll she's she'd she'll it's it'd it'll
we're we've we'd we'll they're they've they'd they'll that's there's here's what's where's
who's how's let's isn't aren't wasn't weren't don't doesn't didn't haven't hasn't hadn't can't
cannot couldn't won't wouldn't shouldn't mustn't mr mrs ms dr ok oh ah um uh hmm wow yeah yep
nope bye goodbye
"""


def _forms(headword: str) -> set[str]:
    """Lower-case single-word tokens of a headword ("a.m./A.M./am/AM" -> {"am"})."""
    out: set[str] = set()
    for variant in headword.split("/"):
        text = unicodedata.normalize("NFKD", variant.strip().lower())
        text = "".join(c for c in text if not unicodedata.combining(c))  # café -> cafe
        for token in re.split(r"[\s\-.]+", text):
            if TOKEN.match(token):
                out.add(token)
    return out


def _read(source: Path | None, name: str) -> str:
    if source is not None:
        return (source / name).read_text(encoding="utf-8")
    with urllib.request.urlopen(f"{BASE_URL}/{name}", timeout=30) as resp:
        return resp.read().decode("utf-8")


def build(source: Path | None) -> dict[str, list[str]]:
    level_of: dict[str, int] = {}

    def add(word: str, level: int) -> None:
        if word not in level_of or level < level_of[word]:
            level_of[word] = level

    for name in FILES:
        for row in csv.DictReader(io.StringIO(_read(source, name))):
            level = (row.get("CEFR") or "").strip()
            if level in LEVELS:
                for word in _forms(row.get("headword") or ""):
                    add(word, LEVELS.index(level))
    for table in (IRREGULAR_VERBS, IRREGULAR_OTHER):
        for entry in filter(str.strip, table.split(";")):
            base, *forms = entry.split()
            for form in forms if base in level_of else ():
                add(form, level_of[base])
    for word in EVERYDAY.split():
        add(word, 0)
    by_level: dict[str, list[str]] = {level: [] for level in LEVELS}
    for word, level in level_of.items():
        by_level[LEVELS[level]].append(word)
    return {level: sorted(words) for level, words in by_level.items()}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--source", type=Path, help="directory with the downloaded CSV files")
    args = parser.parse_args()
    levels = build(args.source)
    header = (
        "# Generated by scripts/build_wordlist.py; do not edit by hand, change the script.\n"
        "# CEFR-J Vocabulary Profile 1.5 (A1-B2): Yukio Tono, Tokyo University of Foreign\n"
        "#   Studies, http://www.cefr-j.org/ - free for research and commercial use, cited.\n"
        "# Octanove Vocabulary Profile C1/C2 1.0 (C1-C2): Octanove Labs, CC BY-SA 4.0,\n"
        "#   https://creativecommons.org/licenses/by-sa/4.0/ - this derived list is shared under\n"
        "#   the same licence. Both via https://github.com/openlanguageprofiles/olp-en-cefrj\n"
        "# Plus irregular forms, contractions and everyday words added by the script.\n"
    )
    body = {
        "source": "CEFR-J Vocabulary Profile 1.5 + Octanove Vocabulary Profile C1/C2 1.0",
        "reviewed_by": "developers",
        "levels": levels,
    }
    text = yaml.safe_dump(
        body, allow_unicode=True, sort_keys=False, width=100, default_flow_style=None
    )
    OUT.write_text(header + text, encoding="utf-8")
    counts = ", ".join(f"{lv} {len(ws)}" for lv, ws in levels.items())
    print(f"wrote {OUT}: {counts}")


if __name__ == "__main__":
    main()
