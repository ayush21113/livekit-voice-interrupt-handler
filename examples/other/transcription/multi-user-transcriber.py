import asyncio
import logging

from dotenv import load_dotenv

from livekit import rtc
from livekit.agents import (
    Agent,
    AgentSession,
    AutoSubscribe,
    JobContext,
    JobProcess,
    RoomInputOptions,
    RoomIO,
    RoomOutputOptions,
    StopResponse,
    WorkerOptions,
    cli,
    llm,
    utils,
)
from livekit.plugins import deepgram, silero
from livekit_agents_extensions.filler_interrupt_handler import FillerInterruptHandler
# from livekit_agents_extensions.filler_interrupt_handler import FillerInterruptHandler

load_dotenv()

logger = logging.getLogger("transcriber")


# This example demonstrates how to transcribe audio from multiple remote participants.
# It creates agent sessions for each participant and transcribes their audio.


class Transcriber(Agent):
    def __init__(self, *, participant_identity: str):
        super().__init__(
            instructions="not-needed",
            stt=deepgram.STT(),
        )
        self.participant_identity = participant_identity

    async def on_user_turn_completed(self, chat_ctx: llm.ChatContext, new_message: llm.ChatMessage):
        user_transcript = new_message.text_content
        logger.info(f"{self.participant_identity} -> {user_transcript}")

        raise StopResponse()


# from livekit_agents_extensions.filler_interrupt_handler import FillerInterruptHandler

class MultiUserTranscriber:
    
    def __init__(self, ctx: JobContext):
        self.ctx = ctx
        self._sessions: dict[str, AgentSession] = {}
        self._tasks: set[asyncio.Task] = set()

        # === Initialize the filler interruption handler ===
        self.handler = FillerInterruptHandler(
            ignored_words=["uh", "umm", "hmm", "haan"],
            force_stop_words=["stop", "wait", "pause", "end", "terminate"],
            ignore_if_confidence_below=0.35
        )

        # === Register handler callbacks for debugging ===
        self.handler.on_valid_interruption(
            lambda text, meta: print(f"[INTERRUPT] {text} | {meta}")
        )
        self.handler.on_ignored_filler(
            lambda text, meta: print(f"[IGNORED FILLER] {text} | {meta}")
        )
        self.handler.on_speech_registered(
            lambda text, meta: print(f"[SPEECH REGISTERED] {text} | {meta}")
        )

        # Simulate agent speaking/quiet flag
        self.handler.agent_speaking = True
        asyncio.create_task(self._set_agent_quiet())

        # Register behavior for stop phrases
        self._register_stop_behavior()

    async def _set_agent_quiet(self):
        await asyncio.sleep(5)
        self.handler.agent_speaking = False
        print("[DEBUG] Agent speaking flag set to False — now agent is quiet.")

    # === Handle stop events from FillerInterruptHandler ===
        # === Handle stop events from FillerInterruptHandler ===
    def _register_stop_behavior(self):
        async def stop_all():
            print("🛑 Stop command detected — shutting down all sessions.")
            try:
                await self.aclose()
            except Exception as e:
                print(f"[Stop Handler] Error closing sessions: {e}")
            finally:
                # Instead of sys.exit, just cancel asyncio loop gracefully
                loop = asyncio.get_running_loop()
                for task in asyncio.all_tasks(loop):
                    if task is not asyncio.current_task():
                        task.cancel()
                print("[Stop Handler] Graceful shutdown triggered.")

        # Register the callback non-blocking
        def handle_interrupt(text, meta):
            clean_text = text.lower().strip()
            if clean_text in ["stop", "please stop", "end", "terminate"]:
                print(f"[Stop Handler] Trigger phrase detected: {clean_text}")
                asyncio.create_task(stop_all())
            else:
                print(f"[Stop Handler] Ignored phrase: {clean_text}")

        # attach handler safely
        self.handler.on_valid_interruption(handle_interrupt)


    async def _set_agent_quiet(self):
        await asyncio.sleep(5)
        self.handler.agent_speaking = False
        print("[DEBUG] Agent speaking flag set to False — now agent is quiet.")


    def start(self):
        self.ctx.room.on("participant_connected", self.on_participant_connected)
        self.ctx.room.on("participant_disconnected", self.on_participant_disconnected)

    async def aclose(self):
        await utils.aio.cancel_and_wait(*self._tasks)

        await asyncio.gather(*[self._close_session(session) for session in self._sessions.values()])

        self.ctx.room.off("participant_connected", self.on_participant_connected)
        self.ctx.room.off("participant_disconnected", self.on_participant_disconnected)

    def on_participant_connected(self, participant: rtc.RemoteParticipant):
        if participant.identity in self._sessions:
            return

        logger.info(f"starting session for {participant.identity}")
        task = asyncio.create_task(self._start_session(participant))
        self._tasks.add(task)

        def on_task_done(task: asyncio.Task):
            try:
                self._sessions[participant.identity] = task.result()
            finally:
                self._tasks.discard(task)

        task.add_done_callback(on_task_done)

    def on_participant_disconnected(self, participant: rtc.RemoteParticipant):
        if (session := self._sessions.pop(participant.identity)) is None:
            return

        logger.info(f"closing session for {participant.identity}")
        task = asyncio.create_task(self._close_session(session))
        self._tasks.add(task)
        task.add_done_callback(lambda _: self._tasks.discard(task))
    
    async def _start_session(self, participant: rtc.RemoteParticipant) -> AgentSession:
        if participant.identity in self._sessions:
            return self._sessions[participant.identity]

        # === Create new Agent session ===
        session = AgentSession(
            vad=self.ctx.proc.userdata["vad"],
        )

        # === Set up room IO ===
        room_io = RoomIO(
            agent_session=session,
            room=self.ctx.room,
            participant=participant,
            input_options=RoomInputOptions(
                text_enabled=False,  # only audio
            ),
            output_options=RoomOutputOptions(
                transcription_enabled=True,
                audio_enabled=False,
            ),
        )
        await room_io.start()

        # === Start the transcriber agent ===
        await session.start(
            agent=Transcriber(
                participant_identity=participant.identity,
            )
        )

        # === Attach the FillerInterruptHandler to STT output ===
        try:
            # Deepgram STT instance is inside the agent
            stt_engine = session.agent.stt

            # For each transcription result, send it to our handler
            # Deepgram may emit different event types; ensure text extraction
            def on_transcript_event(result):
                # Some results may not have .text, adjust if needed
                text = getattr(result, "text", None) or getattr(result, "transcript", "")
                confidence = getattr(result, "confidence", 1.0)
                asyncio.create_task(self.handler._on_transcript_event({
                    "text": text,
                    "confidence": confidence,
                }))

            # Attach if supported
            if hasattr(stt_engine, "on_transcript"):
                stt_engine.on_transcript(on_transcript_event)
                print(f"[Handler] Attached to STT for participant: {participant.identity}")
            else:
                print(f"[Handler] STT engine does not support 'on_transcript' callback — manual integration needed")

        except Exception as e:
            print(f"[Handler] Error attaching handler to STT: {e}")

        return session

    # async def _start_session(self, participant: rtc.RemoteParticipant) -> AgentSession:
    #     if participant.identity in self._sessions:
    #         return self._sessions[participant.identity]

    # # === Create new Agent session ===
    #     session = AgentSession(
    #         vad=self.ctx.proc.userdata["vad"],
    #     )

    # # === Set up room IO ===
    #     room_io = RoomIO(
    #         agent_session=session,
    #         room=self.ctx.room,
    #         participant=participant,
    #         input_options=RoomInputOptions(
    #             text_enabled=False,  # only audio
    #         ),
    #         output_options=RoomOutputOptions(
    #             transcription_enabled=True,
    #             audio_enabled=False,
    #         ),
    #     )
    #     await room_io.start()

    #     # === Start the transcriber agent ===
    #     await session.start(
    #         agent=Transcriber(
    #             participant_identity=participant.identity,
    #         )
    #     )
    #     async def stop_tts_async():
    #         try:
    #             # Most LiveKit agents expose TTS via session.agent.tts
    #             if hasattr(session.agent, "tts") and hasattr(session.agent.tts, "stop"):
    #                 await session.agent.tts.stop()
    #                 print("[TTS STOP] Stopped TTS due to user interruption")
    #             else:
    #                 print("[WARN] TTS stop not supported on this session")
    #         except Exception as e:
    #             print(f"[ERROR stopping TTS] {e}")

    #     # Register it with the handler
    #     self.handler.register_stop_callback(stop_tts_async)
    #     # === Attach the FillerInterruptHandler to STT output ===
    #     try:
    #         # Deepgram STT instance is inside the agent
    #         stt_engine = session.agent.stt

    #         # For each transcription result, send it to our handler
    #         # Deepgram may emit different event types; ensure text extraction
    #         def on_transcript_event(result):
    #             # Some results may not have .text, adjust if needed
    #             text = getattr(result, "text", None) or getattr(result, "transcript", "")
    #             confidence = getattr(result, "confidence", 1.0)
    #             asyncio.create_task(self.handler._on_transcript_event({
    #                 "text": text,
    #                 "confidence": confidence,
    #             }))

    #         # Attach if supported
    #         if hasattr(stt_engine, "on_transcript"):
    #             stt_engine.on_transcript(on_transcript_event)
    #             print(f"[Handler] Attached to STT for participant: {participant.identity}")
    #         else:
    #             print(f"[Handler] STT engine does not support 'on_transcript' callback — manual integration needed")

    #     except Exception as e:
    #         print(f"[Handler] Error attaching handler to STT: {e}")

    #     return session



    # async def _start_session(self, participant: rtc.RemoteParticipant) -> AgentSession:
    #     if participant.identity in self._sessions:
    #         return self._sessions[participant.identity]

    #     session = AgentSession(
    #         vad=self.ctx.proc.userdata["vad"],
    #     )
    #     room_io = RoomIO(
    #         agent_session=session,
    #         room=self.ctx.room,
    #         participant=participant,
    #         input_options=RoomInputOptions(
    #             # text input is not supported for multiple room participants
    #             # if needed, register the text stream handler by yourself
    #             # and route the text to different sessions based on the participant identity
    #             text_enabled=False,
    #         ),
    #         output_options=RoomOutputOptions(
    #             transcription_enabled=True,
    #             audio_enabled=False,
    #         ),
    #     )
    #     await room_io.start()
    #     await session.start(
    #         agent=Transcriber(
    #             participant_identity=participant.identity,
    #         )
    #     )
    #     return session

    async def _close_session(self, sess: AgentSession) -> None:
        await sess.drain()
        await sess.aclose()


async def entrypoint(ctx: JobContext):
    transcriber = MultiUserTranscriber(ctx)
    transcriber.start()

    await ctx.connect(auto_subscribe=AutoSubscribe.AUDIO_ONLY)
    for participant in ctx.room.remote_participants.values():
        # handle all existing participants
        transcriber.on_participant_connected(participant)

    async def cleanup():
        await transcriber.aclose()

    ctx.add_shutdown_callback(cleanup)


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


if __name__ == "__main__":
    cli.run_app(WorkerOptions(entrypoint_fnc=entrypoint, prewarm_fnc=prewarm))
