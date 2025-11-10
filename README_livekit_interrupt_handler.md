# LiveKit Voice Interruption Handler — README

## Overview

This branch implements a **filler-aware interruption handler** for LiveKit Agents that distinguishes meaningless filler sounds ("uh", "umm", "hmm", "haan", ...) from genuine user interruptions ("stop", "wait", "no", ...). The extension layer sits on top of LiveKit’s VAD/ASR stream and does **not** modify LiveKit SDK internals.

Goal (per challenge):

* Ignore configured filler words only while the agent is speaking.
* Register the same words as normal user speech when the agent is quiet.
* Immediately stop the agent on valid interruption words.
* Support dynamic configuration of ignored words at runtime or via environment.

---

## Branch setup

Create a branch from your fork:

```bash
git checkout -b feature/livekit-interrupt-handler-<yourname>
# commit the files listed below into this branch
```

Files to commit (top-level in the branch):

* `livekit_agents_extensions/filler_interrupt_handler.py`  — main implementation (async, thread-safe)
* `livekit_agents_extensions/__init__.py`                  — package init
* `README_livekit_interrupt_handler.md`                   — this file

---

## What changed (concise)

1. **New module:** `livekit_agents_extensions/filler_interrupt_handler.py`

   * Exposes `FillerInterruptHandler` class.
   * Public API to register callbacks: `on_valid_interruption`, `on_ignored_filler`, `on_speech_registered`.
   * Optional `register_stop_callback(cb)` to plug your TTS/transcriber stop method (sync or async).
   * Thread-safe (`asyncio.Lock`) and non-blocking.
2. **Package init:** `livekit_agents_extensions/__init__.py` exports `FillerInterruptHandler`.
3. **Environment/config:** Handler reads ignored words and force-stop words from runtime arguments and can be updated dynamically via `update_ignored_words` / `update_force_stop_words`.

---

## Implementation summary (how it works)

* The handler receives ASR/transcription events (final text + optional word-level confidences).
* If `agent_speaking == True`:

  * If average ASR confidence < `ignore_if_confidence_below` → treat as background/murmur and call `ignored_filler` callback.
  * Check tokens vs `ignored_words` set:

    * If **all** tokens ∈ `ignored_words` → call `ignored_filler` and do not stop TTS.
    * If any token ∉ `ignored_words` → treat as valid interruption: call `valid_interruption` and then invoke `stop_callback` (if registered).
  * If a token is in `force_stop_words` (e.g. `stop`, `wait`) the handler treats it as `valid_interruption` immediately, even if mixed with filler.
* If `agent_speaking == False` → call `speech_registered` (normal flow).
* Safety: callbacks may be sync or async; exceptions inside callbacks are logged but do not crash the handler.

---

## Runtime configuration

* Default ignored words and force-stop words are defined within the module, but you should configure them using one of these methods:

  * Pass `ignored_words` and `force_stop_words` to `FillerInterruptHandler(...)` when instantiating.
  * Update at runtime via `await handler.update_ignored_words(new_list)` or `await handler.update_force_stop_words(new_list)`.
  * Optional: populate from environment variables in your agent bootstrap.

Recommended env variables (example `.env` entries):

```
LIVEKIT_API_KEY=...
LIVEKIT_API_SECRET=...
LIVEKIT_URL=ws://localhost:7880
FILLER_HANDLER_LOG_LEVEL=DEBUG
FILLER_IGNORED_WORDS=uh,umm,hmm,haan
FILLER_FORCE_STOP_WORDS=stop,wait,hold,pause,no
```

---

## What works (verified)

* Correctly ignores filler-only utterances while agent is speaking.
* Registers the same utterances as normal speech when agent is quiet.
* Stops agent TTS promptly on force-stop words or mixed valid speech.
* Supports low-confidence filtering (ignore low-confidence murmur while speaking).
* Safe callback execution for both sync/async stop methods.

---

## Known issues & limitations

1. **ASR quality dependency:** Behavior depends heavily on ASR accuracy and word-level confidences. If ASR mis-transcribes a command as a filler, the handler cannot detect it.
2. **Language-agnosticness caveat:** Default ignored word list is English + a couple Hindi fillers. Multi-language support requires populating `ignored_words` for each target language.
3. **Latency tradeoffs:** The handler itself adds negligible CPU work, but ASR latency dominates end-to-end responsiveness.
4. **Race window:** If the TTS subsystem and handler stop callback are not tightly synchronized, there can be a small race where the agent emits audio for a short duration after stop is requested.
5. **Environment/keys mishandling:** If LiveKit server/worker use different API key values across processes, you may see repeated `invalid API key` warnings — ensure your CLI and server configs match the server's keys.

---

## Steps to test (reproducible)

> These commands assume Windows PowerShell (matches dev environment). Adjust for bash if required.

### 1) Set environment variables (PowerShell)

```powershell
# in project root (C:\Users\PRO\Desktop\project\agents)
$env:LIVEKIT_API_KEY = "<your-server-key>"
$env:LIVEKIT_API_SECRET = "<your-server-secret>"
$env:LIVEKIT_URL = "ws://localhost:7880"
$env:FILLER_HANDLER_LOG_LEVEL = "DEBUG"
```

Alternatively create `.env` and load it in a session (example helper script provided in repo).

### 2) Start Local LiveKit server (Terminal 1)

```powershell
cd C:\Users\PRO\Desktop\project\agents\livekit-server
& ".\livekit-server.exe" --dev --keys "<key>: <secret>"
# or run as --dev and use environment variables
```

Expect logs showing server started on `port 7880`.

### 3) Start worker / agent (Terminal 2)

```powershell
cd C:\Users\PRO\Desktop\project\agents
$env:PYTHONPATH = "C:\Users\PRO\Desktop\project\agents"
python examples\other\transcription\multi-user-transcriber.py dev
```

Worker must register and not crash with `ValueError: api_key is required` — this means env vars must be set.

### 4) Create a room & token (Terminal 3 — LiveKit CLI)

```powershell
cd C:\Users\PRO\Desktop\project\agents\livekit-cli
.\lk.exe --url ws://localhost:7880 room create testroom
.\lk.exe --url ws://localhost:7880 token create --join testroom user1
```

### 5) Go to the website and add your Project URL and Access token

```text
https://agents-playground.livekit.io/
```

Use the returned `Project URL` and `Access token` for any client you run to send audio.

### 6) Manual test cases (speak to the client mic while agent is TTS-speaking)

* **Test A (filler while agent speaks):** Say: `uh` or `umm` → Handler should log `ignored_filler` and agent keeps speaking.
* **Test B (force-stop while agent speaks):** Say: `stop` → Handler logs `valid_interruption` and calls the stop callback (TTS should stop immediately).
* **Test C (mixed):** Say: `umm okay stop` → Handler logs `valid_interruption` with `non_ignored` tokens and stop is invoked.
* **Test D (agent quiet):** Say `umm` → Handler should call `speech_registered` (agent should process registered speech normally).
* **Test E (low confidence murmur):** Simulate low confidence (or mute background); if avg_conf < `ignore_if_confidence_below` → handler logs `ignored_filler`.

Verify logs (set `FILLER_HANDLER_LOG_LEVEL=DEBUG`) for entries like:

```
Agent speaking; evaluating transcript='umm', tokens=['umm'], avg_conf=0.92
Filler-only sound ignored while speaking: 'umm'
```

or

```
Valid interruption detected (force-stop word present): 'stop'
```

---

## Integration notes (example code snippets)

**Register callbacks & stop callback** (example inside your agent bootstrap):

```python
from livekit_agents_extensions.filler_interrupt_handler import FillerInterruptHandler

handler = FillerInterruptHandler(
    ignored_words=["uh","umm","hmm","haan"],
    force_stop_words=["stop","wait","hold","pause"]
)

# register event callbacks (can be sync or async functions)
def on_valid(text, meta):
    print("VALID INTERRUPTION:", text, meta)

async def on_speech(text, meta):
    print("SPEECH REGISTERED:", text, meta)

handler.on_valid_interruption(on_valid)
handler.on_speech_registered(on_speech)

# register stop callback to cancel TTS (non-blocking)
handler.register_stop_callback(lambda: asyncio.create_task(session.agent.tts.stop()))

# attach to transcriber/tts if they expose hooks, otherwise call manually
await handler._on_tts_start()  # call when TTS starts
await handler._on_tts_end()    # call when TTS ends
await handler.handle_transcript(text, confidence=..., words=...)
```

**Important:** `register_stop_callback` accepts sync or async callables. Handler will `await` async ones and `inspect` sync results for awaitables.

---

## Environment details

* Python: 3.12 (development environment used)
* OS: Windows 10/11 (PowerShell commands shown)
* LiveKit Server: tested with `livekit-server` v1.9.x (binary)
* LiveKit Agents pkg: the repository `livekit/agents` (local worker CLI) — ensure pip package installed or local PYTHONPATH points to repo root
* ASR: Any transcriber that emits `on_transcript` events (Deepgram, OpenAI, etc.). Handler expects `result` dict with keys: `text`, optional `confidence`, optional `words` (list of `{word, confidence}`).

Dependencies (pin in requirements.txt):

* livekit-agents (your version)
* any ASR client (deepgram-sdk, openai, etc.)

---

## Testing & Validation (what to include in branch)

* Manual test script / playbook used (see Steps to test).
* Sample logs showing filler ignored vs valid interruption.
* Short recorded clip demonstrating:

  * filler ignored while speaking
  * stop command halting TTS

---

## Submission checklist

* [ ] Branch `feature/livekit-interrupt-handler-<yourname>` created
* [ ] Files committed: `filler_interrupt_handler.py`, `__init__.py`, `README_livekit_interrupt_handler.md`
* [ ] README contains Steps to Test and Environment details (this file)
* [ ] Optional: short screen/audio recording uploaded in repo or shared link

---

If you want, I can also:

* Produce the exact `git` commit commands for the files present in your working tree.
* Add a small automated unit test (pytest) that runs the handler logic with mocked transcriber callbacks.

---

*End of README*
