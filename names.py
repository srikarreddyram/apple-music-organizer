"""Playlist names with some personality, picked from what's actually in the playlist.

A playlist is profiled (vibe bucket, style family, language, dominant artist,
era) and names are drawn from hand-written banks for that combination, plus
templates built around a dominant artist or an older-leaning era. Several
options come back so you can choose; the draw is seeded by the playlist so
re-running gives the same options unless you ask for a reroll.
"""
import json
import random
import statistics
from collections import Counter
from pathlib import Path

import labels
from suggest import credit_names

BANK = {
    ("rap", "Gym"): [
        "Pre-Workout Felony", "808s & Deadlifts", "Squat Rack Sermons", "Leg Day Is a Hate Crime",
        "Spotter Not Required", "Adrenaline Is Legal", "Mosh Pit Cardio", "Rage Against the Leg Press",
        "PR or ER", "Villain Origin Story (Gym Edition)",
    ],
    ("rap", "Party"): [
        "Function Starter Pack", "Aux Cord Privileges", "Neighbours Called Twice", "Bad Decisions Pregame",
        "Turn Up Tax Bracket", "Security Is Watching", "The Function Got Shut Down", "Speaker Blown, No Refunds",
    ],
    ("rap", "Cruise"): [
        "Windows Down, Volume Illegal", "Bass in the Trunk", "Scenic Route Through the Hood",
        "Long Way Home on Purpose", "Hand Out the Window", "Cruise Control, Ego Uncontrolled",
        "Passenger Princess Rejects", "Speed Limit Is a Suggestion",
    ],
    ("rap", "Feels"): [
        "Therapy Session (Explicit)", "Big Steppers Only", "Bars I Felt in My Chest", "Overthinking in 4/4",
        "Rap Songs That Read My Diary", "Hood Philosophy 101", "Real Talk at Red Lights",
    ],
    ("rap", "Late Night"): [
        "Headlights Off", "3AM Parked Outside", "Toronto at 4AM", "Lowkey Up Too Late",
        "Phone on 1%, Feelings on 100", "Night Shift for My Thoughts", "Dim the Dashboard",
    ],
    ("rnb", "Late Night"): [
        "Slow Burn Sunday", "Texts I Shouldn't Send", "Candlelit & Conflicted", "After-Hours Feelings",
        "Velvet Ceiling", "Situationship Soundtrack",
    ],
    ("rnb", "Feels"): ["Soft Life, Hard Feelings", "Love Language: Bass", "Heart on Mute", "Toxic but Make It Smooth"],
    ("pop", "Party"): [
        "Kitchen Disco", "Scream It in the Car", "Main Pop Girl Energy", "Hairbrush Microphone",
        "Glitter on the Aux", "Karaoke Felony", "Dance Like the Group Chat's Watching",
    ],
    ("pop", "Cruise"): [
        "Golden Hour Highway", "Sunroof Season", "Romanticizing My Commute", "Road Trip, No Destination",
        "Windows Down, Heart Up",
    ],
    ("pop", "Feels"): [
        "Main Character Montage", "Falling in Love in the Cereal Aisle", "Butterflies on Shuffle",
        "Sing It Like You Mean It", "Rom-Com I'm Not In", "Delulu Is the Solulu",
    ],
    ("pop", "Late Night"): [
        "Ceiling Staring Club", "Texts I Didn't Send", "Heartbreak Hotel, Room 2B", "Crying in 4K",
        "Soft Boy Hours", "Sad but Make It Pretty", "Overthinking Under Fairy Lights",
    ],
    ("pop", "Gym"): ["Cardio Is Just Dancing Angry", "Treadmill Main Character", "Sweat & Sparkle"],
    ("rock", "Late Night"): ["Indie Kid Insomnia", "Vinyl Crackle at Midnight", "Sad Guitar Hours"],
    ("rock", "Cruise"): ["Air Guitar on the Highway", "Garage Band Dreams", "Leather Jacket Weather"],
    ("rock", "Gym"): ["Headbang Your PR", "Riffs > Reps", "Distortion Pedal Cardio"],
    ("rock", "Party"): ["Air Guitar Olympics", "Stadium in My Bedroom", "Mosh Pit for One"],
    ("electronic", "Party"): ["Drop It Like It's Rent", "Strobe Light Therapy", "BPM Over Feelings", "Bass Face Mandatory"],
    ("electronic", "Gym"): ["Rave Cardio", "Sweat Like the Drop's Coming", "Subwoofer Damage"],
    ("latin", "Party"): ["Perreo Emergency", "Hips Don't Lie, Neither Do I", "Reggaeton Is Cardio", "Fiesta Sin Permiso"],
    ("latin", "Late Night"): ["Noche Lenta", "Besos a las 3AM", "Corazón en Modo Avión"],
    ("indian-film", "Party"): [
        "First Day First Show", "Whistle Podu Mode", "Hero Entry BGM", "Mass Ah Machaan",
        "Theatre Floor Dance Break", "Baraat Has Entered the Chat",
    ],
    ("indian-film", "Gym"): ["Interval Bang", "Villain Got Slapped", "Slow-Mo Punch Scene", "Hero Walk-In, Goons Fly Out"],
    ("indian-film", "Late Night"): [
        "Baarish & Chai", "Window Seat Monsoon", "Train Journey Thoughts", "Terrace at Midnight",
        "Second Half Heartbreak",
    ],
    ("indian-film", "Feels"): ["Dil Ki Baatein", "Song Sequence in Switzerland", "Love Track Before the Interval"],
    ("indian-film", "Cruise"): ["Highway Dhaba Drive", "Long Drive, Old Songs", "Ghat Road Curves"],
    ("roots", "Late Night"): ["Porch Light Philosophy", "Campfire After Midnight", "Station Wagon Radio, 1974",
                              "Grandpa's Records, My Feelings"],
    ("roots", "Feels"): ["Country Roads, Take Me Anywhere", "Dirt Road Diary", "Banjo-Shaped Nostalgia"],
    ("roots", "Cruise"): ["Dirt Road Detour", "Pickup Truck Daydream", "Highway Through Nowhere, Montana"],
    ("roots", "Party"): ["Barn Dance Emergency", "Boots on the Coffee Table"],
    ("rock", "Feels"): ["Indie Sleaze Diary", "Garage Band Heartbreak"],
    ("indian-other", "Late Night"): ["Indie Chai Sessions", "Rooftop Unplugged", "Acoustic Heartache, Desi Edition"],
}
FALLBACK = {
    "Gym": ["Rep Count: Infinite", "No Days Off (Lies)", "Sweat Equity"],
    "Party": ["Volume Knob Snapped Off", "Aux Hostage Situation", "The Neighbours Know My Taste Now"],
    "Cruise": ["Windows Down Weather", "Nowhere in Particular", "Long Way Round"],
    "Feels": ["Emotional Damage, Curated", "Heart on Shuffle", "Feelings, Alphabetized"],
    "Late Night": ["After Midnight Edition", "Lights Off, Brain On", "Can't Sleep, Won't Sleep"],
}
LANGUAGE_FLAVOUR = {
    "telugu": ["Tollywood Mass Jathara", "Telugu Tape, Rewind Again", "Gully to Godavari"],
    "tamil": ["Kollywood Vibe Check", "Vibe-u Machaan", "Anirudh Said Dance"],
    "hindi": ["Bollywood Breakdown", "Filmi Feels Only", "Dil Se, Full Volume"],
    "spanish": ["Modo Fiesta", "Latino Heat Index"],
}
ARTIST_TEMPLATES = {
    "Gym": ["{a} Made Me Skip Rest Day", "Lifting With {a}", "{a} Is My Personal Trainer"],
    "Party": ["{a} Hosted This Party", "Certified {a} Function", "{a} on the Aux, No Arguments"],
    "Cruise": ["Riding Shotgun With {a}", "{a} Scenic Route", "Driving Like a {a} Video"],
    "Feels": ["{a} Read My Diary", "{a} Ruined My Evening", "Emotionally Sponsored by {a}"],
    "Late Night": ["{a} at 3AM", "{a} Ruined My Sleep Schedule", "Late Night With {a}"],
    "For You": ["Certified {a} Moment", "{a} Starter Pack", "Deep in {a} Territory", "{a} But Only the Bangers",
                "What Would {a} Do", "{a} Supremacy"],
}
NICKNAMES = {  # for the artists you play most; anything else uses the plain name
    "Kendrick Lamar": ["Kung Fu Kenny", "K.Dot"], "Drake": ["Drizzy", "Champagne Papi"], "The Weeknd": ["Abel"],
    "Anirudh Ravichander": ["Rockstar Anirudh", "Ani"], "Eminem": ["Slim Shady"], "Travis Scott": ["La Flame", "Cactus Jack"],
    "Kanye West": ["Ye"], "A$AP Rocky": ["Pretty Flacko"], "Future": ["Pluto"], "Metro Boomin": ["Metro"],
    "Arijit Singh": ["Arijit"], "Devi Sri Prasad": ["Rockstar DSP", "DSP"], "One Direction": ["1D"],
    "Justin Bieber": ["Biebs"], "Ed Sheeran": ["Ed"], "Gunna": ["Wunna"], "JID": ["J.I.D"], "S.S. Thaman": ["Thaman"],
    "Sid Sriram": ["Sid"], "Bad Bunny": ["Benito"], "A.R. Rahman": ["the Mozart of Madras", "ARR"],
    "Michael Jackson": ["the King of Pop", "MJ"], "Don Toliver": ["Don"], "Daniel Caesar": ["Caesar"],
    "Charlie Puth": ["Charlie"], "Shawn Mendes": ["Shawn"], "Baby Keem": ["Keem"], "Clipse": ["Push & Malice"],
}
NICK_TEMPLATES = ["{n} Hall of Fame", "{n} Said Run It Back", "Certified {n} Classics", "{n} Season",
                  "Only {n} Understands Me", "In {n} We Trust"]
SONG_TEMPLATES = ["{s} and Other Bangers", "Everything Sounds Like {s}", "It Started With {s}",
                  "{s} on Repeat (Send Help)"]
ERA_TEMPLATES = ["Aux Cord Circa {y}", "{y} Called, It Wants Its Bangers Back", "Tumblr Era Survivor", "Throwback Thursday Forever"]


# Hand-picked names, keyed by the plan ref of the playlist they're for ("Calling-Late Night").
# They're offered first whenever that playlist is planned again.
PREFERRED = Path(__file__).parent / "data" / "playlist_names.json"


def preferred(ref):
    return json.loads(PREFERRED.read_text()).get(ref, []) if PREFERRED.exists() else []


def remember(ref, names):
    data = json.loads(PREFERRED.read_text()) if PREFERRED.exists() else {}
    data[ref] = list(dict.fromkeys(names + data.get(ref, [])))
    PREFERRED.parent.mkdir(parents=True, exist_ok=True)
    PREFERRED.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def profile(ids, tracks, eff):
    labs = [eff[i] for i in ids if i in eff]
    styles = Counter(labels.FAMILY.get(l["style"], "other") for l in labs)
    langs = Counter(l["language"] for l in labs)
    artists = Counter(credit_names(tracks[i])[0] for i in ids if i in tracks and credit_names(tracks[i]))
    years = [tracks[i]["year"] for i in ids if i in tracks and tracks[i].get("year")]
    top_artist, top_n = artists.most_common(1)[0] if artists else (None, 0)
    return {
        "family": styles.most_common(1)[0][0] if styles else "other",
        "language": langs.most_common(1)[0][0] if langs else "english",
        "artist": top_artist if top_n / max(len(ids), 1) >= 0.3 else None,
        "year": int(statistics.median(years)) if years else None,
        "size": len(ids),
    }


def options(prof, bucket, n=6, avoid=(), seed="", reroll=0, preferred=()):
    """Name options for a playlist; the first is the default. `preferred` names come first (not on reroll)."""
    rnd = random.Random(f"{seed}|{reroll}")
    picks = list(BANK.get((prof["family"], bucket), []))
    rnd.shuffle(picks)
    extra = []
    if prof["artist"]:
        extra += [t.format(a=prof["artist"]) for t in ARTIST_TEMPLATES.get(bucket, [])]
        for nick in NICKNAMES.get(prof["artist"], []):
            extra += [t.format(n=nick) for t in NICK_TEMPLATES]
    if prof.get("song"):
        extra += [t.format(s=prof["song"]) for t in SONG_TEMPLATES]
    if prof["language"] in LANGUAGE_FLAVOUR and prof["family"] != "indian-film":
        extra += LANGUAGE_FLAVOUR[prof["language"]]
    if prof["year"] and prof["year"] <= 2016:
        extra += [t.format(y=prof["year"]) for t in ERA_TEMPLATES]
    rnd.shuffle(extra)
    fallback = list(FALLBACK.get(bucket, []))
    rnd.shuffle(fallback)
    # Mostly the bank for this sound, with an artist/era/language twist mixed in.
    ordered = (list(preferred) if not reroll else []) + picks[:3] + extra[:2] + picks[3:] + extra[2:] + fallback
    out, seen = [], set(a.lower() for a in avoid)
    for name in ordered:
        if name.lower() not in seen:
            seen.add(name.lower())
            out.append(name)
    return out[:n]
