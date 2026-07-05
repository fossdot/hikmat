# Roshni AI — local tutor MVP (setup & run)

A local-Ollama tutor wired into the game's "Roshni, mujhe doubt hai" sheet. A logged-in
girl types a doubt in Hindi → it goes to a **local** Ollama model on this MacBook → her
Hindi answer is shown **and spoken**. Every turn is logged for facilitator review.

**This MVP = typed doubt only.** No microphone/voice-in, no guard model, no real-time
crisis alerting, no analytics clustering yet (those are the next phases). The AI is purely
**additive** — the four scripted buttons (hear again / slow / Hindi / tell-teacher) always
work, so if Ollama is down or offline, nothing breaks.

## 1. Pull the model (on this Mac)

```bash
ollama pull gemma4:12b-mlx        # ~6.8GB, lighter+faster MLX build for the 16GB M4
```

Optional — bake the persona into a named model instead of sending the prompt per request:

```bash
cd apps/hikmat && ollama create roshni -f Modelfile.roshni
# then in Hikmat Settings set "Ollama model" = roshni and leave the system-prompt blank
```

## 2. Create the doctypes + settings (on the live site)

```bash
cd /Users/fossdot/code/hikmat-bench
bench --site hikmat.local execute hikmat.setup_data.create_doctypes   # adds AI Conversation + AI Conversation Turn (skips existing)
bench --site hikmat.local execute hikmat.setup_data.add_ai_fields     # adds AI fields to the existing Hikmat Settings single
bench --site hikmat.local execute hikmat.setup_data.setup_analytics   # rebuilds reports+workspace incl. "AI Review Queue"
bench --site hikmat.local migrate
```

> In **developer mode**, creating the doctypes auto-writes their JSON/.py into
> `hikmat/hikmat/doctype/ai_conversation*/` — commit those generated files (same way
> `lesson_doubt/` was created).

## 3. Turn it on (Desk → Hikmat Settings)

- ✅ **Enable Roshni AI**
- **Ollama model** = `gemma4:12b-mlx` (or `roshni` if you baked it)
- **Ollama endpoint** = `http://localhost:11434`
- **System prompt / crisis reply** — leave blank to use the built-in Hindi defaults, or edit to tune Roshni's voice.

The public `get_settings` payload exposes only `aiEnabled` — the model, endpoint and prompt
never leave the server.

## 4. Sync the game & test

`sync-game.sh` has been run (index.html → game.html). Reload the served game, **log in as a
student** (AI requires a logged-in profile — guests don't get it), start a lesson, tap
**🙋 doubt है → ✨ रोशनी से पूछो**, and type a Hindi question.

## Before piloting with real children (preconditions, not optional)

- **Harden login PIN.** `_pin_ok`/`_token_ok` currently pass for PIN-less/token-less
  profiles → on a shared laptop another child could open a profile. Require a PIN for any
  profile that gets logged. (AI transcripts are Desk-facilitator-only, never read back to
  the student side.)
- **Name the Safeguarding Lead** + contact channel. Crisis-flagged doubts get a safe canned
  reply and are flagged in the **AI Review Queue**, but real-time escalation to a named adult
  is the next step.
- **Consent + counsel** on storing minors' free text (DPDP-children); 90-day retention
  auto-purge is designed but not yet built.
- **RAM check.** 16GB is tight: run `gemma4:12b-mlx` and watch `memory_pressure`/Activity
  Monitor with Frappe + browser up. If it swaps, drop to a smaller model. Ollama serializes
  requests, so the classroom model is **take-turns**, not concurrent.

## How it fails safe

`ai_ask` is fail-closed (rate-limit denies if Redis is down), requires student+token,
redacts structured PII before persisting, short-circuits a crisis lexicon to a safe reply
without calling the model, and on any Ollama error returns `ai_unavailable` → the game keeps
the scripted buttons and **speaks** a Hindi fallback so a non-reader always hears the outcome.
