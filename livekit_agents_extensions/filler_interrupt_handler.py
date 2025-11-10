# livekit_agents_extensions/filler_interrupt_handler.py
"""
Filler-aware interruption handler for LiveKit Agents.

Usage:
    handler = FillerInterruptHandler(...)
    await handler.attach(agent, transcriber, tts)  # or supply event callbacks/registrations

This module does NOT modify LiveKit SDK internals — it listens to events and emits
"interruption" or "speech_registered" callbacks you hook into your agent loop.
"""

import asyncio
import logging
import os
import re
from typing import Callable, Iterable, Optional, Set, Dict

logger = logging.getLogger("filler_interrupt_handler")
logger.setLevel(os.getenv("FILLER_HANDLER_LOG_LEVEL", "INFO"))

# Default filler set (can be overridden via env or runtime)
DEFAULT_IGNORED_WORDS = {"uh", "umm", "hmm", "haan", "uhh", "uhm", "erm", "ah", "mm", "mmh", "mhmm"}

# Words that should always be treated as forcing a stop if present (could be extended)
DEFAULT_FORCE_STOP_WORDS = {"stop", "wait", "hold", "pause", "waita", "no", "waitone", "waitone", "one moment"}  # add variants as needed

# A simple normalizer to strip punctuation and lowercase
_TOKEN_RE = re.compile(r"[^\w']+", re.UNICODE)

def normalize_text(text: str) -> str:
    return _TOKEN_RE.sub(" ", text or "").strip().lower()

def tokenize(text: str) -> Iterable[str]:
    return [t for t in normalize_text(text).split() if t]

class FillerInterruptHandler:
    """
    Handles filler-only interruptions while the agent speaks. Exposes the following events:
      - on_valid_interruption(text, metadata)
      - on_ignored_filler(text, metadata)
      - on_speech_registered(text, metadata)  # when agent quiet or when valid non-filler speech arrives
    """

    def __init__(
        self,
        ignored_words: Optional[Iterable[str]] = None,
        force_stop_words: Optional[Iterable[str]] = None,
        min_confidence_to_consider: float = 0.5,
        ignore_if_confidence_below: float = 0.4,
        logger_name: str = "filler_interrupt_handler",
    ):
        self.ignored_words: Set[str] = set(w.lower() for w in (ignored_words or DEFAULT_IGNORED_WORDS))
        self.force_stop_words: Set[str] = set(w.lower() for w in (force_stop_words or DEFAULT_FORCE_STOP_WORDS))
        self.min_confidence_to_consider = float(min_confidence_to_consider)
        self.ignore_if_confidence_below = float(ignore_if_confidence_below)
        self.agent_speaking = False
        self.lock = asyncio.Lock()
        self._callbacks: Dict[str, Callable] = {}
        self.logger = logging.getLogger(logger_name)
        self.logger.setLevel(os.getenv("FILLER_HANDLER_LOG_LEVEL", "INFO"))

    # ---------- Public API for attaching callbacks ----------
    def on_valid_interruption(self, cb: Callable[[str, dict], None]):
        """Callback invoked when a valid interruption is detected. cb(text, metadata)"""
        self._callbacks["valid_interruption"] = cb

    def on_ignored_filler(self, cb: Callable[[str, dict], None]):
        """Callback invoked when filler-only input is ignored while agent was speaking."""
        self._callbacks["ignored_filler"] = cb

    def on_speech_registered(self, cb: Callable[[str, dict], None]):
        """Callback invoked when speech should be registered/handled (agent quiet or valid speech)."""
        self._callbacks["speech_registered"] = cb

    # ---------- Tools to update config dynamically (bonus) ----------
    async def update_ignored_words(self, new_list: Iterable[str]):
        async with self.lock:
            self.ignored_words = set(w.lower() for w in new_list)
            self.logger.info(f"Ignored words updated: {sorted(self.ignored_words)}")

    async def update_force_stop_words(self, new_list: Iterable[str]):
        async with self.lock:
            self.force_stop_words = set(w.lower() for w in new_list)
            self.logger.info(f"Force-stop words updated: {sorted(self.force_stop_words)}")

    # ---------- Event hooks to integrate with LiveKit Agent loop ----------
    async def attach_to_events(self, *, agent, transcriber=None, tts=None):
        """
        Attach to events. This function is intentionally generic — adapt to your project's event names.

        - agent: LiveKit agent or event emitter object (we only need it to optionally subscribe)
        - transcriber: object that emits 'transcript' events; it's expected to call handler.handle_transcript(...)
        - tts: object that emits 'tts_started' and 'tts_ended' events
        """

        # Example subscriptions; adapt to your agent/transcriber event emitter signatures:
        if hasattr(tts, "on_tts_start"):
            tts.on_tts_start(self._on_tts_start)
            tts.on_tts_end(self._on_tts_end)
        else:
            # If TTS emits different events, the caller should call `handler._on_tts_start()` etc directly.
            self.logger.debug("TTS object missing 'on_tts_start'/'on_tts_end'. You must call handler._on_tts_start/_on_tts_end manually.")

        if transcriber is not None and hasattr(transcriber, "on_transcript"):
            # transcriber should call callbacks like cb(result_dict)
            transcriber.on_transcript(self._on_transcript_event)
        else:
            self.logger.debug("Transcriber missing 'on_transcript'. You should call handler.handle_transcript() yourself.")

    # If your SDK doesn't provide event attach methods, call these from your event loop:
    async def _on_tts_start(self, *args, **kwargs):
        async with self.lock:
            self.agent_speaking = True
            self.logger.debug("Agent speaking: START")

    async def _on_tts_end(self, *args, **kwargs):
        async with self.lock:
            self.agent_speaking = False
            self.logger.debug("Agent speaking: END")

    # Generic adapter that accepts transcription results (from your transcriber)
    async def _on_transcript_event(self, result: dict):
        """
        Expected 'result' keys (flexible):
          - 'text': final transcription text (string)
          - 'confidence': float between 0 and 1 (optional)
          - 'words': optional list of dicts with word-level confidence: [{'word': 'uh', 'confidence':0.9}, ...]
          - any other metadata forwarded to callbacks
        """
        await self.handle_transcript(
            text=result.get("text", ""),
            confidence=result.get("confidence", None),
            words=result.get("words", None),
            metadata=result,
        )

    async def handle_transcript(self, text: str, confidence: Optional[float] = None, words: Optional[list] = None, metadata: Optional[dict] = None):
        """
        Core decision logic lives here.
        metadata is forwarded to callbacks for debugging/logging.
        """
        metadata = metadata or {}
        text = (text or "").strip()
        if not text:
            self.logger.debug("Empty transcript received; ignoring.")
            return

        tokens = tokenize(text)
        # compute overall confidence heuristically if word-level confidences are provided
        if words:
            # words: list of {'word': 'uh', 'confidence': 0.95}
            confidences = [w.get("confidence", 1.0) for w in words if isinstance(w, dict)]
            if confidences:
                avg_conf = sum(confidences) / len(confidences)
            else:
                avg_conf = confidence if confidence is not None else 1.0
        else:
            avg_conf = confidence if confidence is not None else 1.0

        async with self.lock:
            agent_speaking = self.agent_speaking

        # If agent is speaking, filter using filler-word policy
        if agent_speaking:
            self.logger.debug(f"Agent speaking; evaluating transcript='{text}', tokens={tokens}, avg_conf={avg_conf:.3f}")
            # Very low confidence => treat as background/murmur -> ignore
            if avg_conf < self.ignore_if_confidence_below:
                self.logger.info("Ignoring low-confidence background/murmur while agent speaks.")
                cb = self._callbacks.get("ignored_filler")
                if cb:
                    cb(text, {"reason": "low_confidence", "avg_conf": avg_conf, **(metadata or {})})
                return

            # token-level check: if any token is not in ignored words -> valid interruption
            non_ignored_tokens = [t for t in tokens if t not in self.ignored_words]
            # check for forced stop words (even if mixed)
            has_force_stop = any(t in self.force_stop_words for t in tokens)

            if has_force_stop:
                self.logger.info(f"Valid interruption detected (force-stop word present): '{text}'")
                cb = self._callbacks.get("valid_interruption")
                if cb:
                    cb(text, {"reason": "force_stop_word", "avg_conf": avg_conf, **(metadata or {})})
                return

            if not non_ignored_tokens:
                # Only filler words (all tokens are ignored); ignore interruption
                self.logger.info(f"Filler-only sound ignored while speaking: '{text}'")
                cb = self._callbacks.get("ignored_filler")
                if cb:
                    cb(text, {"reason": "filler_only", "avg_conf": avg_conf, **(metadata or {})})
                return
            else:
                # Mixed filler + real speech => valid interrupt
                self.logger.info(f"Valid interruption (contains non-filler token): '{text}'")
                cb = self._callbacks.get("valid_interruption")
                if cb:
                    cb(text, {"reason": "mixed_tokens", "non_ignored": non_ignored_tokens, "avg_conf": avg_conf, **(metadata or {})})
                return
        else:
            # Agent is quiet -> register speech normally
            self.logger.debug(f"Agent quiet; registering speech: '{text}'")
            cb = self._callbacks.get("speech_registered")
            if cb:
                cb(text, {"reason": "agent_quiet", "avg_conf": avg_conf, **(metadata or {})})
            return
