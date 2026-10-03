"""Mood, energy, context, language and style labels, kept apart by where they came from.

  data/labels/inferred.json   AI-inferred (Claude, from title/artist/album/genre and
                              its knowledge of the song). Each label has a confidence:
                              high = knows the song, medium = knows the artist/film,
                              low = guessed from metadata alone.
  data/labels/manual.json     Your overrides. Always win over inferred labels.

Labels use the closed vocabularies below so they can be grouped and compared.
"""
import json
from datetime import datetime
from pathlib import Path

LABELS = Path(__file__).parent / "data" / "labels"

MOODS = ["hype", "aggressive", "dark", "confident", "chill", "romantic", "heartbreak",
         "melancholic", "feel-good", "euphoric", "dreamy", "nostalgic", "introspective"]
CONTEXTS = ["workout", "drive", "party", "focus", "late-night", "wind-down", "pregame", "sing-along"]
LANGUAGES = ["english", "telugu", "tamil", "hindi", "punjabi", "spanish", "korean", "instrumental", "other"]
STYLES = [
    # rap
    "trap", "melodic-rap", "drill", "rage", "boom-bap", "conscious-rap", "pop-rap", "west-coast", "uk-rap",
    "experimental-rap",
    # r&b / pop
    "rnb", "alt-rnb", "funk", "dance-pop", "synth-pop", "teen-pop", "acoustic-pop", "pop-ballad", "indie-pop",
    # rock / alternative
    "indie-rock", "alt-rock", "dream-pop", "pop-rock", "hard-rock",
    # electronic / latin
    "edm", "house", "uk-garage", "electronic", "phonk", "reggaeton", "latin-pop", "latin-trap",
    # indian
    "film-melody", "film-dance", "film-bgm", "indian-indie", "punjabi-pop", "sufi", "devotional",
    # roots
    "folk", "country", "jazz",
    "other",
]
FAMILIES = {
    "rap": ["trap", "melodic-rap", "drill", "rage", "boom-bap", "conscious-rap", "pop-rap", "west-coast", "uk-rap",
            "experimental-rap"],
    "rnb": ["rnb", "alt-rnb", "funk"],
    "pop": ["dance-pop", "synth-pop", "teen-pop", "acoustic-pop", "pop-ballad", "indie-pop", "dream-pop"],
    "rock": ["indie-rock", "alt-rock", "pop-rock", "hard-rock"],
    "electronic": ["edm", "house", "uk-garage", "electronic", "phonk"],
    "latin": ["reggaeton", "latin-pop", "latin-trap"],
    "indian-film": ["film-melody", "film-dance", "film-bgm"],
    "indian-other": ["indian-indie", "punjabi-pop", "sufi", "devotional"],
    "roots": ["folk", "country", "jazz"],
    "other": ["other"],
}
FAMILY = {style: fam for fam, styles in FAMILIES.items() for style in styles}
FIELDS = ("mood", "energy", "contexts", "language", "style", "confidence")


def load(kind):
    p = LABELS / f"{kind}.json"
    return json.loads(p.read_text()) if p.exists() else {}


def save(kind, data):
    LABELS.mkdir(parents=True, exist_ok=True)
    (LABELS / f"{kind}.json").write_text(json.dumps(data, indent=0, ensure_ascii=False))


def validate(label):
    errors = []
    if label.get("mood") not in MOODS:
        errors.append(f"mood {label.get('mood')!r}")
    if label.get("energy") not in (1, 2, 3, 4, 5):
        errors.append(f"energy {label.get('energy')!r}")
    bad = [c for c in label.get("contexts", []) if c not in CONTEXTS]
    if bad:
        errors.append(f"contexts {bad}")
    if label.get("language") not in LANGUAGES:
        errors.append(f"language {label.get('language')!r}")
    if label.get("style") not in STYLES:
        errors.append(f"style {label.get('style')!r}")
    if label.get("confidence", "high") not in ("high", "medium", "low"):
        errors.append(f"confidence {label.get('confidence')!r}")
    return errors


def import_lines(text, known_ids, source):
    """Parse 'ID|mood|energy|ctx,ctx|language|style|confidence' lines into inferred labels."""
    data, problems = load("inferred"), []
    stamp = datetime.now().isoformat(timespec="seconds")
    for line in text.strip().splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) != 7 or parts[0] not in known_ids:
            problems.append(f"bad line: {line}")
            continue
        pid, mood, energy, ctx, lang, style, conf = parts
        label = {"mood": mood, "energy": int(energy) if energy.isdigit() else energy,
                 "contexts": [c for c in ctx.split(",") if c], "language": lang, "style": style,
                 "confidence": conf}
        errs = validate(label)
        if errs:
            problems.append(f"{pid}: {', '.join(errs)}")
            continue
        data[pid] = {**label, "source": source, "at": stamp}
    save("inferred", data)
    return data, problems


def merged():
    """Effective labels per track: manual fields override inferred ones."""
    inferred, manual = load("inferred"), load("manual")
    out = {}
    for pid in set(inferred) | set(manual):
        lab = dict(inferred.get(pid, {}))
        lab.update(manual.get(pid, {}))
        if pid in manual:
            lab["source"] = "manual" if pid not in inferred else "manual+" + inferred[pid]["source"]
        out[pid] = lab
    return out
