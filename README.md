# Bodhya Learn 🪔

A game-style learning platform for girls in Champaran, Bihar — built **offline-first**, for
centres with intermittent internet, shared laptops and phones that are handed round.

It began as an English-and-computer-skills course for teenagers ("Hikmat"). It now covers
**Class 1–10 across six subjects**, alongside the life-skills tracks it started with. On the
Play Store it ships as **Bodhya Learn** (`net.bodhya.learn`).

> **A note on the name.** The product is *Bodhya Learn*. The Frappe app, its Python package,
> its DocType module and its asset path are all still `hikmat` — that is a technical
> identifier, and renaming it would move `/assets/hikmat/…`, break every installed PWA's
> service-worker cache and orphan the packaged Android app. Leave it alone.

## What's inside

- **The game** — a self-contained, zero-dependency single-file web app (`index.html`, served
  as `hikmat/public/game.html`). A mascot, a winding lesson trail, synthesised sounds, and a
  fully bilingual EN/HI interface. It fetches content from the backend and caches it for
  offline play; opened as a plain file it runs entirely on bundled data.
- **The Frappe app** (`hikmat/`) — the backoffice. Facilitators author Tracks → Lessons →
  Words/Dialogues/exercises as DocTypes, manage Students and Cohorts, and read the analytics.
  Learners never see Frappe; they only ever see the game.

## The curriculum

**26 tracks, 283 lessons, 1,779 activity screens.** Three ways in, each remembering where you
left off:

| Door | What's behind it |
|---|---|
| **By class** | Grades 1–4, 5–8 and 9–10 — English, Hindi, Maths, Science, EVS, Social Science, Computer |
| **By subject** | The same lessons regrouped, at beginner / intermediate / advanced |
| **Life skills** | Bazaar, Home, School, Work, Money, Health, Travel, Coding, AI |

## Activity types

**29 of them**, and each lesson carries its own rotation rather than the same ladder every
time — hear → recognise → produce → converse → read → apply → play. Vocabulary work (Learn,
Listen, Spell, Build a Sentence, Talk) sits alongside recognition games (Match the Pairs, Odd
One Out, Sort the Baskets), reading and comprehension (Story, Cloze, Notice), numeracy (Count,
Money, Clock), and the Work track's own (Complete the Code 💻, Find the Bug 🐞, Write an Email
📧). An activity appears only when the lesson has content for it, and a per-lesson `skip` list
can leave one out. The first time a learner meets a new type, the game explains it once, out
loud, in her language.

## Architecture

```
Game (learner PWA)  ──fetch──>  hikmat.api.get_courses / get_settings   (offline-first, cached)
                    ──post───>  hikmat.api.submit_attempt               (progress sync, queued when offline)
Facilitators ──> Frappe Desk: Track/Lesson/Dialogue, Students/Cohorts, workspace + analytics
```

Content layers in this order, each overwriting the last only if it returns something:
bundled defaults → `localStorage` → the precached `curriculum.json` → the live API. Going
offline therefore never loses content, and finished activities queue locally until there is a
network to flush them to.

Curriculum lives in `hikmat/data/curriculum.json`, is loaded into DocTypes by
`hikmat/setup_data.py`, and `hikmat/api.py` returns it to the game 1:1.

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

`setup_data.create_doctypes()` (in developer mode) + `bench migrate` sets up the DocTypes;
`setup_data.setup_analytics()` builds the dashboard, workspace and reports.

Lint the curriculum before committing content changes:

```bash
python3 lint-curriculum.py
```

## Branches

`nightly` is where work lands — **always push there**. `main` is the release branch: it is what
production and the Play Store build come from, and it moves only by promoting `nightly` on a
deliberate say-so. Both are protected.

## License

MIT
