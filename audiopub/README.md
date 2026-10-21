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
  up, the elderly slowed down. When there are more characters than voices, voices are reused with
  a pitch shift.
- **Any model, any provider** for the attribution step, and three TTS backends (one fully offline).
- Everything is cached: interrupt and re-run to resume; edit the cast and re-render only what changed.

## Install

Requires Python 3.10+ and [ffmpeg](https://ffmpeg.org).

```bash
cd audiopub
pip install -e .                 # Anthropic + Edge TTS
pip install -e '.[openai]'       # + OpenAI-compatible LLMs (OpenAI, Ollama, OpenRouter, Gemini, ...) and OpenAI TTS
pip install -e '.[kokoro]'       # + Kokoro offline TTS
```

## Quick start

```bash
export ANTHROPIC_API_KEY=...
audiopub convert my-book.epub              # -> ./My_Book.m4b
audiopub convert ~/books/*.epub -o ~/audiobooks
```

Try a couple of chapters first:

```bash
audiopub chapters my-book.epub             # list chapters with word counts
audiopub convert my-book.epub --chapters 1-2
```

## Step by step (to review the casting)

```bash
audiopub analyze my-book.epub              # find characters and who says what
audiopub cast my-book.epub --tts edge      # pick voices; prints the cast table
$EDITOR audiopub-work/my-book/cast.edge.json
audiopub render my-book.epub --tts edge    # synthesize and build the .m4b
```

`cast.<backend>.json` maps each speaker (and `NARRATOR`) to a voice:

```json
"Elizabeth Bennet": {"voice": "en-GB-SoniaNeural", "pitch": 0.0, "speed": 1.0, ...}
```

`pitch` is in semitones, `speed` is a multiplier. Your edits are kept on later runs; delete an
entry to have it re-assigned. `audiopub voices --tts edge --lang it` lists available voices.
`characters.json` and `script/chNNN.json` in the same folder show what the LLM decided.

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

`--effort low|medium|high|...` sets reasoning effort on models that support it. If a request
fails or is declined, that part of the chapter falls back to the heuristics and the run continues.
Use `--reanalyze` to redo attribution with a different model.

Cost guide: the whole book passes through the model once (≈1.3 tokens per word plus small
overhead), so a 100k-word novel is roughly 150k input tokens.

## Choosing the voices (`--tts`)

| `--tts` | |
|---|---|
| `edge` (default) | Microsoft Edge neural voices: free, needs internet, ~400 voices in ~80 languages, plus multilingual voices that are used when a language has few native ones. |
| `kokoro` | [Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M), runs on your machine. ~50 voices (English, Spanish, French, Italian, Portuguese, Hindi, Japanese, Chinese). |
| `openai` | OpenAI speech (`--tts-model gpt-4o-mini-tts` by default), or any server with the same API via `--tts-base-url` (e.g. Kokoro-FastAPI). For servers with other voices set `AUDIOPUB_TTS_VOICES="id:female,id2:male,..."`. |

The book's language comes from the EPUB metadata.

## Output

- `--format m4b` (default): a single audiobook file with chapter markers, title/author and cover,
  playable in Apple Books, BookPlayer, Smart AudioBook Player, VLC, etc.
- `--format mp3`: a folder with one MP3 per chapter.
- `--no-titles` skips announcing chapter titles.

## Development

```bash
pip install -e '.[dev,openai]'
pytest
```

Tests run the full pipeline offline with a fake LLM and a fake TTS backend.
