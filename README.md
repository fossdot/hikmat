# Hikmat 🪔

A game-style platform to teach functional **English** (and then **computer/IT skills**) to teenage girls (14–18) in Champaran, Bihar. Built to be **offline-first** for centres with intermittent internet and shared laptops.

## What's inside

- **The game** — a self-contained, zero-dependency single-file web game (`index.html`, served as `hikmat/public/game.html`). A mascot ("Roshni"), a winding lesson trail, sounds, and an EN/HI interface. It fetches content from the backend and caches it for offline play; opened as a plain file it runs fully offline on bundled data.
- **The Frappe app** (`hikmat/`) — the backoffice. Teachers author Tracks → Lessons → Words/Dialogues/exercises as DocTypes, manage Students & Cohorts, and view analytics. Students never see Frappe; they only use the game.

## Activity types

Vocabulary & conversation for every lesson: **Learn → Listen → Spell → Build a Sentence → Talk**. The IT/Work track adds three more: **Complete the Code** 💻, **Find the Bug** 🐞, and **Write an Email** 📧. Each activity appears only when the lesson has that content.

## Architecture

```
Game (student PWA)  ──fetch──>  hikmat.api.get_courses / get_settings   (offline-first, cached)
                    ──post───>  hikmat.api.submit_attempt               (progress sync)
Teachers ──> Frappe Desk: Track/Lesson/Dialogue, Students/Cohorts, "Hikmat" workspace + analytics
```

Curriculum lives in `hikmat/data/curriculum.json` and is loaded by `hikmat/setup_data.py` into DocTypes; `hikmat/api.py` returns it to the game 1:1.

## Run it

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app <URL_OF_THIS_REPO>
bench install-app hikmat
bench start
# game:  http://localhost:8000/play      admin: http://localhost:8000/app/hikmat
```

Seed / re-seed content after editing `data/curriculum.json`:

```bash
bench --site <your-site> console
>>> import hikmat.setup_data as m; m.seed_content()
```

`setup_data.create_doctypes()` (in developer mode) + `bench migrate` sets up the DocTypes; `setup_data.setup_analytics()` builds the dashboard, workspace, and the "Student Progress" report.

## License

MIT
