# audiopub

Turn EPUB books into audiobooks where **each character has their own voice**.

```
book.epub ─▶ chapters & paragraphs ─▶ narration / dialogue split ─▶ LLM: who says each line?
          ─▶ cast: one voice per character ─▶ text-to-speech ─▶ book.m4b (with chapters & cover)
```

- Dialogue is found deterministically (“curly”, "straight", «guillemets», „German“ quotes and
  `— dash` dialogue as used in Italian/Spanish/French books). The LLM only *labels* who speaks each
  quote, so it can never change or skip the book's text.
- The LLM keeps a running list of characters with aliases ("Lizzy" = "Elizabeth Bennet"), gender
  and age, which drive voice choice: women get female voices, men male ones, children are pitched
  up, the elderly slowed down. Voices are in the book's language; when there are more characters
  than voices, voices are reused with small pitch/speed variations.
- **Any model, any provider** for the attribution step, and three TTS backends (one fully offline).
- Everything is cached: interrupt and re-run to resume; edit the cast and re-render only what changed.

## Install

Requires [uv](https://docs.astral.sh/uv/) and [ffmpeg](https://ffmpeg.org). uv fetches a suitable
Python by itself.

Install the `audiopub` command globally (run from this folder):

```bash
uv tool install .                     # Anthropic + Edge TTS
uv tool install '.[openai]'           # + OpenAI-compatible LLMs (OpenAI, Ollama, OpenRouter, Gemini, ...) and OpenAI TTS
uv tool install '.[openai,kokoro]'    # + Kokoro offline TTS
```

Re-run with `--force` after pulling changes. Or skip installing and run it from the project:

```bash
uv run audiopub convert my-book.epub
uv run --extra kokoro audiopub convert my-book.epub --tts kokoro
```

## API keys

Put your keys in a `.env` file (see [`.env.example`](.env.example)):

```bash
mkdir -p ~/.config/audiopub
cp .env.example ~/.config/audiopub/.env    # works from any folder
$EDITOR ~/.config/audiopub/.env
```

audiopub reads, in order: the file given with `--env-file`, `./.env` in the current folder, then
`~/.config/audiopub/.env`. If a key is defined in several, the first wins, and variables exported in
your shell override all files. `--llm-api-key` / `--tts-api-key` override everything. `audiopub -v ...`
logs which files were loaded.

## Quick start

```bash
audiopub convert my-book.epub              # -> ./My_Book.m4b
audiopub convert ~/books/*.epub -o ~/audiobooks
```

Try a couple of chapters first:

```bash
audiopub chapters my-book.epub             # list chapters with word counts
audiopub convert my-book.epub --chapters 1-2
```

## Working in steps, checkpoints and resuming

`convert` runs three steps in one go: `analyze`, `cast` and `render`. Run them separately to review
the casting in between:

```bash
audiopub analyze my-book.epub              # find characters and who says what
audiopub cast my-book.epub --tts edge      # pick voices; prints the cast table and the file path
$EDITOR <project folder>/cast.edge.json    # path printed by 'audiopub cast' / 'audiopub status'
audiopub render my-book.epub --tts edge    # synthesize and build the .m4b
```

`cast.<backend>.json` maps each speaker (and `NARRATOR`) to a voice:

```json
"Elizabeth Bennet": {"voice": "en-GB-SoniaNeural", "pitch": 0.0, "speed": 1.0, ...}
```

`pitch` is in semitones, `speed` is a multiplier. Your edits are kept on later runs; delete an
entry to have it re-assigned, or run `audiopub cast --reset` to re-assign every voice (this
discards your edits).

Voices are taken from the book's language only. Some languages have few voices (Edge has two male
and two female Italian ones), so characters then share a voice with small pitch/speed variations;
the main characters get the most distinct ones, and the narrator's voice is used only as a last
resort. `--multilingual-voices` adds other languages' multilingual voices for more variety, but
they read the book with a foreign accent. `audiopub voices --tts edge --lang it` lists available voices.
`characters.json` and `script/chNNN.json` in the same folder show what the LLM decided.

**Project folder.** Results are saved per book in `~/.local/share/audiopub/books/<title>-<id>/`.
Change the root with `$AUDIOPUB_HOME`, or the folder for one command with `--workdir`. The folder
is keyed by the book's metadata, so commands find it from any directory and after you rename the
`.epub`. Speaker detection is done once per chapter and reused by later commands: the LLM is not
called again unless you pass `--reanalyze`.

**Checkpoints.** Progress is saved as it happens:

- analysis is saved after every LLM request, even mid-chapter;
- each synthesized line is saved;
- each encoded chapter is saved.

Interrupt with Ctrl-C at any time and re-run the same command to resume. To work through a long
book in batches, limit any command with `--chapters` (`audiopub chapters` lists the numbers):

```bash
audiopub analyze my-book.epub --chapters 1-10
audiopub analyze my-book.epub --chapters 11-20
```

**Progress output.** While analyzing, audiopub reports each passage it sends (paragraph range,
number of quotes, size, model), how long the model took, who speaks in it, new characters
(`+ new character: …`) and new details about known ones (`~ …: also "…"`), retries with their
reason (`⚠ … retrying (2/3)`) and passages that fell back to heuristics (`✗ …`), then a summary
per chapter and for the whole run:

```
[4/5] I pugnalatori — 21,467 words, 11 passages with dialogue to send (resuming at ¶106)
  passage 10/11 · ¶106–112 of 115 · 14 quotes · 11,820 chars → deepseek:deepseek-flash
    ⚠ model returned invalid JSON (…) — retrying (2/3)
    ✓ 14 quotes labelled in 6.2s
      speakers: Longo ×5, Mangano ×3, narrator ×1
      + new character: Cavaliere Longo (male, adult) — a knight
```

`-q` reduces this to one line per chapter (plus warnings); `-v` adds debug details; `-vv` also
shows every HTTP request. Options like these go before the command: `audiopub -q analyze …`.

**Status.** See what is done:

```bash
audiopub status my-book.epub               # per chapter: analysis, audio, encoded
audiopub status my-book.epub --speakers    # also speakers, line counts, gender, aliases
audiopub status my-book.epub --tts openai  # audio progress for another backend
```

For each chapter, `status` shows whether it is analyzed (and by which model), partially analyzed,
how many lines have audio, and whether it is encoded. `--tts` selects which backend's audio
progress to show.

**Fixing attribution** without re-running the model:

```bash
audiopub speaker my-book.epub "Lizzy" "Elizabeth Bennet"   # rename or merge across the book
audiopub speaker my-book.epub "The letter" NARRATOR         # quoted text that isn't speech
audiopub render my-book.epub --tts edge                     # re-synthesizes only affected lines
```

The `script/chNNN.json` files can also be edited by hand.

**Files in the project folder:**

| File | Contents |
|---|---|
| `book.json` | the book this project is (title, author, language, EPUB path) |
| `characters.json` | the character roster: aliases, gender, age, description |
| `script/chNNN.json` | who says what, per chapter (hand-editable) |
| `script/chNNN.partial.json` | checkpoint of a chapter being analyzed |
| `cast.<backend>.json` | voice per speaker for one TTS backend (hand-editable) |
| `audio/` | one file per synthesized line, keyed by content |
| `chapters/` | encoded chapters, keyed by content |

## Choosing the LLM (`--llm provider:model`)

| `--llm` | Notes |
|---|---|
| `anthropic:claude-opus-5-5` | **default**. `ANTHROPIC_API_KEY` |
| `anthropic:claude-sonnet-5-5`, `anthropic:claude-haiku-5-5` | cheaper / faster |
| `openai:<model>` | `OPENAI_API_KEY` |
| `ollama:<model>` | local, e.g. `ollama:qwen3:14b` (needs `ollama serve`) |
| `lmstudio:<model>` | local LM Studio server |
| `openrouter:<vendor/model>` | `OPENROUTER_API_KEY`, hundreds of models |
| `gemini:<model>` | `GEMINI_API_KEY` |
| `groq:`, `mistral:`, `deepseek:` | `GROQ_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY` |
| `openai:<model> --llm-base-url URL` | any other OpenAI-compatible server (vLLM, llama.cpp, ...) |
| `--no-llm` | built-in heuristics ("…," said Anna): free and offline, but much less accurate |

`--effort low|medium|high|...` sets reasoning effort on models that support it. A reply that
isn't usable (e.g. malformed JSON, common with providers that lack strict JSON schemas) is retried
up to 3 times. If the model still fails, or declines, that passage falls back to the heuristics, the
run continues, and the passage is recorded: `analyze` and `status` list it, and
`audiopub analyze --retry-failed` sends just those passages to the model again (you can also switch
`--llm` for the retry). Use `--reanalyze` to redo attribution with a different model.

Cost guide: the whole book passes through the model once (≈1.3 tokens per word plus small
overhead), so a 100k-word novel is roughly 150k input tokens.

## Choosing the voices (`--tts`)

| `--tts` | |
|---|---|
| `edge` (default) | Microsoft Edge neural voices: free, needs internet, ~400 voices in ~80 languages, plus multilingual voices from other languages (opt-in with `--multilingual-voices`; they have a foreign accent). |
| `kokoro` | [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), runs on your machine. ~50 voices (English, Spanish, French, Italian, Portuguese, Hindi, Japanese, Chinese). |
| `openai` | OpenAI speech (`--tts-model gpt-4o-mini-tts` by default), or any server with the same API via `--tts-base-url` (e.g. Kokoro-FastAPI). For servers with other voices set `AUDIOPUB_TTS_VOICES="id:gender[:locale],..."` (see below). |
| `openrouter` | Speech models on OpenRouter, default `mistralai/voxtral-mini-tts-3-0` (Mistral Voxtral TTS: English, French, Spanish, Portuguese, Italian, Dutch, German, Hindi, Arabic; about $14 per million characters, roughly $1–3 for a novel). Uses `OPENROUTER_API_KEY`. Pick another model with `--tts-model`, e.g. `mistralai/voxtral-mini-tts-2603`. OpenRouter's API has no speed option, so speed changes are applied with ffmpeg afterwards. |

The book's language comes from the EPUB metadata.

### Finding and labelling voices

`audiopub voices --tts openrouter --lang it` lists the voices a backend offers for a language.
Voxtral's named voices are English and French (`en_paul_neutral`, `gb_oliver_neutral`,
`gb_jane_neutral`, `fr_marie_neutral`); the numbered `voice_NNNN` ones are undocumented, so their
language and gender are unknown. Record a sample with every voice and listen to them:

```bash
audiopub voices --tts openrouter --lang it --sample "Buongiorno, mi chiamo Giacosa e questa è la mia voce."
```

Samples are saved to `./voice-samples/openrouter/` (use `--sample-dir` to change this). Existing
files are not re-recorded. It costs a few cents.

Then label the voices you like in `.env` (the ids below are only examples):

```bash
AUDIOPUB_TTS_VOICES=voice_3150:male:it-IT,voice_478:female:it-IT,en_paul_neutral:male:en-US
```

Each entry is `id:gender[:locale]`: gender is `male` or `female`, and the locale is like `it-IT`.
Voices labelled with the book's language are preferred for casting. When `AUDIOPUB_TTS_VOICES` is
set, this list replaces the backend's built-in voice list. This works for `--tts openai` too, e.g.
with self-hosted servers.

## Output

- `--format m4b` (default): a single audiobook file with chapter markers, title/author and cover,
  playable in Apple Books, BookPlayer, Smart AudioBook Player, VLC, etc.
- `--format mp3`: a folder with one MP3 per chapter.
- `--no-titles` skips announcing chapter titles.

## Development

```bash
uv sync          # creates .venv with the dev group (pytest, openai)
uv run pytest
```

Tests run the full pipeline offline with a fake LLM and a fake TTS backend.
