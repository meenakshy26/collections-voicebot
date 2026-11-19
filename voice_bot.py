"""Mumbai Bank collections voicebot — Pipecat voice pipeline.

Run:  uv run voice_bot.py            (then open http://localhost:7860/client)
Env:  PROMPT_VERSION=v1|v2|v3  CUSTOMER_ID=MB-CC-10042  LLM_MODEL=...  STT_MODE=translit|codemix

What Pipecat owns: audio transport, turn-taking, STT/LLM/TTS plumbing.
What this file owns: wiring the bot package (prompt, tools, guardrails, outcome,
webhook) into that pipeline. All product logic lives in bot/.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from pathlib import Path

from loguru import logger
from pipecat.adapters.llm.schemas.function_schema import FunctionSchema
from pipecat.adapters.llm.schemas.tools_schema import ToolsSchema
from pipecat.frames.frames import (
    BotStoppedSpeakingFrame,
    EndFrame,
    Frame,
    InterimTranscriptionFrame,
    LLMFullResponseEndFrame,
    LLMMessagesAppendFrame,
    MetricsFrame,
    TranscriptionFrame,
    TTSAudioRawFrame,
    TTSSpeakFrame,
    TTSStartedFrame,
    TTSStoppedFrame,
    TTSTextFrame,
)
from pipecat.metrics.metrics import TTFBMetricsData
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineParams, PipelineWorker, ProcessorUnusablePolicy
from pipecat.processors.aggregators.llm_context import LLMContext
from pipecat.processors.aggregators.llm_response_universal import LLMContextAggregatorPair, LLMUserAggregatorParams
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.runner.types import RunnerArguments
from pipecat.runner.utils import create_transport
from pipecat.services.anthropic.llm import AnthropicLLMService
from pipecat.services.llm_service import FunctionCallParams
from pipecat.services.sarvam.stt import SarvamRealtimeSTTService
from pipecat.services.sarvam.tts import SarvamTTSService
from pipecat.transports.base_transport import BaseTransport, TransportParams
from pipecat.turns.user_start import ExternalUserTurnStartStrategy
from pipecat.turns.user_stop import ExternalUserTurnStopStrategy
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from bot.config import get_settings

_ROOT = Path(__file__).resolve().parent
from bot.prompts import KICKOFF_INSTRUCTION, TURN_BUDGET_NUDGE
from bot.session import CallSession

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")


# --------------------------------------------------------------------------- #
# Processors that connect the pipeline to the CallSession
# --------------------------------------------------------------------------- #


class CustomerTurnLogger(FrameProcessor):
    """Sits right after STT. Logs every final transcript and runs the input triggers."""

    def __init__(self, session: CallSession):
        super().__init__()
        self._s = session

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)
        if isinstance(frame, TranscriptionFrame) and not isinstance(frame, InterimTranscriptionFrame):
            text = (frame.text or "").strip()
            if text and len(text) < 2:
                logger.info(f"ignored stray transcript: {text!r}")
                return  # a stray syllable from background noise; don't hand it to the model
            if text:
                trig = self._s.log_customer(text)
                logger.info(f"CUSTOMER: {text}" + (f"  [trigger: {trig.reason.value}]" if trig else ""))
                if self._s.closing and len(text.split()) >= 4:
                    # The customer said something substantive after the outcome was recorded
                    # (not just "okay bye"): let the model respond, and possibly replace an
                    # unresolved outcome, before we hang up.
                    self._s.closing_response_done = False
                    self._s.closing_audio_reset = True
        await self.push_frame(frame, direction)


_GOODBYE_RE = re.compile(r"\b(goodbye|good bye|bye)\b", re.I)


class BotTurnMonitor(FrameProcessor):
    """Sits after transport output. Logs spoken text, counts bot turns, and applies the
    turn-level guardrails: forced transfer, turn budget, and ending the call once the
    outcome is recorded and the goodbye has been spoken."""

    def __init__(self, session: CallSession):
        super().__init__()
        self._s = session
        self._worker: PipelineWorker | None = None
        self._ending = False
        self._nudged = False
        self._spoke_this_response = False
        self._closing_audio_done = False
        self._ttfb: dict[str, list[float]] = {}
        self._utterance_text = ""

    def attach(self, worker: PipelineWorker):
        self._worker = worker

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, TTSTextFrame):
            self._s.log_bot(frame.text)
            self._spoke_this_response = True
            self._utterance_text += " " + frame.text
            logger.info(f"BOT: {frame.text}")

        elif isinstance(frame, TTSStartedFrame):
            self._tts_t0 = time.time()
            self._audio_frames = 0
            self._utterance_text = ""
            if getattr(self._s, "closing_audio_reset", False):
                self._closing_audio_done = False
                self._s.closing_audio_reset = False
            logger.info("TTS started")
        elif isinstance(frame, TTSAudioRawFrame):
            self._audio_frames += 1
            if self._audio_frames == 1:
                logger.info(f"TTS first audio after {time.time() - getattr(self, '_tts_t0', time.time()):.2f}s")
        elif isinstance(frame, TTSStoppedFrame):
            logger.info(f"TTS stopped after {time.time() - getattr(self, '_tts_t0', time.time()):.2f}s, {getattr(self, '_audio_frames', 0)} audio frames")
            if self._s.closing and self._s.closing_response_done:
                self._closing_audio_done = True  # the goodbye has fully played
            # The outcome is recorded and the sentence that just finished playing said goodbye:
            # the call is over. Ending here (rather than waiting for BotStoppedSpeaking, which the
            # transport emits *before* this frame) also cuts off any second goodbye the model
            # generates after the record_outcome tool result.
            if self._s.closing and (_GOODBYE_RE.search(self._utterance_text) or getattr(self._s, "end_after_speech", False)):
                self._s.closing_response_done = True
                await asyncio.sleep(0.6)  # let the last audio packets drain to the browser
                await self._end_call("outcome recorded and goodbye spoken")

        elif isinstance(frame, MetricsFrame):
            for d in frame.data:
                if isinstance(d, TTFBMetricsData) and d.value:
                    self._ttfb.setdefault(d.processor, []).append(round(d.value, 3))

        elif isinstance(frame, LLMFullResponseEndFrame):
            # Only responses with spoken text count as a turn; tool-only responses don't.
            if self._spoke_this_response:
                self._s.note_bot_turn_end()
                if self._s.closing:
                    self._s.closing_response_done = True
            self._spoke_this_response = False
            await self._after_bot_turn()

        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._s.closing and self._s.closing_response_done and self._closing_audio_done:
                await self._end_call("outcome recorded and goodbye spoken")

        await self.push_frame(frame, direction)

    async def _after_bot_turn(self):
        if self._worker is None or self._ending:
            return
        if self._s.needs_forced_transfer():
            line = await self._s.force_transfer()
            logger.warning("GUARDRAIL: model missed a transfer trigger; forcing hand-off")
            self._s.closing_response_done = True
            await self._worker.queue_frames([TTSSpeakFrame(line), EndFrame()])
            self._ending = True
        elif self._s.over_budget() and not self._s.closing and not self._nudged:
            self._nudged = True
            logger.warning(f"GUARDRAIL: call budget reached ({self._s.bot_turns} spoken turns, {self._s.elapsed():.0f}s); nudging to close")
            await self._worker.queue_frames(
                [LLMMessagesAppendFrame(messages=[{"role": "user", "content": f"[Call controller] {TURN_BUDGET_NUDGE}"}], run_llm=True)]
            )

    async def _end_call(self, why: str):
        if self._ending or self._worker is None:
            return
        self._ending = True
        logger.info(f"Ending call: {why}")
        await self._worker.queue_frames([EndFrame()])

    @property
    def ttfb(self) -> dict[str, list[float]]:
        return self._ttfb


# --------------------------------------------------------------------------- #
# Pipeline
# --------------------------------------------------------------------------- #


def _tools_schema(session: CallSession) -> ToolsSchema:
    return ToolsSchema(
        standard_tools=[
            FunctionSchema(name=t["name"], description=t["description"], properties=t["parameters"]["properties"], required=t["parameters"]["required"])
            for t in session.tool_schemas()
        ]
    )


def _next_call_overrides() -> dict:
    """The console writes logs/next_call.json to pick the version/customer for the next call."""
    import json

    p = _ROOT / "logs" / "next_call.json"
    try:
        data = json.loads(p.read_text())
        return {k: v for k, v in data.items() if k in ("prompt_version", "customer_id") and v}
    except Exception:  # noqa: BLE001
        return {}


async def run_bot(transport: BaseTransport):
    settings = get_settings(**_next_call_overrides())
    session = CallSession(settings)
    logger.info(f"Call {session.call_id} | voice {os.getenv('VOICE_PROVIDER', 'sarvam')} | prompt {settings.prompt_version} | customer {session.account.customer_id} ({session.account.full_name}) | llm {settings.llm_model}")

    # --- Speech to text. Default: Sarvam Saaras realtime (server-side VAD decides turns).
    # VOICE_PROVIDER=deepgram swaps STT+TTS to Deepgram (Nova-3 multilingual + Aura-2) — used when
    # Sarvam returns HTTP 429 (rate limit / credits) so a demo never dies on one vendor.
    provider = os.getenv("VOICE_PROVIDER", "sarvam").lower()
    if provider == "deepgram":
        from pipecat.services.deepgram.stt import DeepgramSTTService

        stt = DeepgramSTTService(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            sample_rate=16000,
            settings=DeepgramSTTService.Settings(
                model="nova-3",
                language="multi",  # Hindi/English code-switching
                language_hint=["hi", "en"],
                smart_format=True,
                interim_results=True,
                endpointing=int(os.getenv("STT_SILENCE_MS", "700")),
                keyterm=["UPI", "NEFT", "EMI", "OTP", "Mumbai Bank"],
            ),
        )
    else:
      stt = SarvamRealtimeSTTService(
          api_key=settings.sarvam_api_key,
          endpointing="vad",
          sample_rate=16000,
          keyterms=["UPI", "NEFT", "EMI", "OTP", "Mumbai Bank", "net banking", "auto debit", "cheque"],
          settings=SarvamRealtimeSTTService.Settings(
              model="saaras:v4",
              language=settings.stt_language,
              mode=settings.stt_mode,
              stream_type="fast",
              # Noise handling: a higher threshold needs more confident speech before a turn starts,
              # min_speech_duration ignores short bursts (clicks, a word from someone nearby), and
              # silence_duration decides how long a pause ends the customer's turn.
              threshold=float(os.getenv("STT_VAD_THRESHOLD", "0.75")),
              min_speech_duration_ms=int(os.getenv("STT_MIN_SPEECH_MS", "500" if os.getenv("ALLOW_BARGE_IN", "0") == "1" else "350")),
              silence_duration_ms=int(os.getenv("STT_SILENCE_MS", "700")),
          ),
      )

    # --- Language model
    llm = AnthropicLLMService(
        api_key=settings.anthropic_api_key,
        model=settings.llm_model,
        settings=AnthropicLLMService.Settings(temperature=0.3, max_tokens=350),
    )
    for t in session.tool_schemas():

        async def _handler(params: FunctionCallParams, _name=t["name"]):
            result = await session.handle_tool(_name, params.arguments or {})
            await params.result_callback(result)

        llm.register_function(t["name"], _handler)

    # --- Text to speech (Sarvam Bulbul v4 flash, Indian-English customer-service voice)
    async def output_guardrail(text: str, _text_type) -> str:
        return session.filter_bot_text(text)

    if provider == "deepgram":
        from pipecat.services.deepgram.tts import DeepgramTTSService

        tts = DeepgramTTSService(
            api_key=os.environ["DEEPGRAM_API_KEY"],
            sample_rate=24000,
            settings=DeepgramTTSService.Settings(voice=os.getenv("DEEPGRAM_VOICE", "aura-2-thalia-en")),
            text_transforms=[("*", output_guardrail)],
        )
    else:
      tts = SarvamTTSService(
        api_key=settings.sarvam_api_key,
        settings=SarvamTTSService.Settings(model=settings.tts_model, voice=settings.tts_voice, language="en-IN", pace=1.0, min_buffer_size=30),
        text_transforms=[("*", output_guardrail)],
    )

    # --- Context: system prompt + tools. The account balance is NOT in here (except v1).
    context = LLMContext(messages=[{"role": "system", "content": session.system_prompt()}], tools=_tools_schema(session))
    if os.getenv("USE_SILERO_VAD") == "1" or provider == "deepgram":
        # Fallback: local Silero VAD decides turns instead of Sarvam's server-side VAD.
        from pipecat.audio.vad.silero import SileroVADAnalyzer

        from pipecat.audio.vad.vad_analyzer import VADParams

        # Local Silero VAD with a conservative profile for noisy rooms.
        vad = SileroVADAnalyzer(params=VADParams(confidence=float(os.getenv("VAD_CONFIDENCE", "0.6")), start_secs=0.2, stop_secs=0.6, min_volume=float(os.getenv("VAD_MIN_VOLUME", "0.3"))))
        user_params = LLMUserAggregatorParams(vad_analyzer=vad, user_idle_timeout=12.0)
    else:
        # Sarvam's production guide: pair its server-side VAD with external turn strategies
        # and do not stack a local VAD on top (it adds ~0.8 s per turn).
        # The customer's turn only starts once at least two words have been transcribed, so a
        # cough, a background voice or a one-word "hmm" does not interrupt the bot mid-sentence.
        from pipecat.turns.user_start import MinWordsUserTurnStartStrategy

        # ALLOW_BARGE_IN=0 (default): the bot finishes its sentence even if the room is noisy;
        # what the customer says meanwhile is still transcribed and answered next. Set 1 to
        # let the customer cut the bot off mid-sentence (nicer in a quiet room, fragile in a noisy one).
        barge_in = os.getenv("ALLOW_BARGE_IN", "0") == "1"
        if barge_in:
            # Sarvam only sends a transcript once the customer pauses, so a word-count gate can
            # never interrupt the bot mid-sentence. For barge-in we trust Sarvam's server-side
            # VAD speech-start event instead, made noise-tolerant by the higher VAD threshold,
            # the minimum speech duration and RNNoise on the microphone input.
            start_strategies = [ExternalUserTurnStartStrategy(enable_interruptions=True)]
        else:
            start_strategies = [MinWordsUserTurnStartStrategy(min_words=int(os.getenv("TURN_MIN_WORDS", "3")), enable_interruptions=False)]
        user_params = LLMUserAggregatorParams(
            user_turn_strategies=UserTurnStrategies(
                start=start_strategies,
                stop=[ExternalUserTurnStopStrategy(timeout=0.25, wait_for_transcript=True)],
            ),
            user_idle_timeout=12.0,
            user_turn_stop_timeout=3.0,
        )
    user_agg, assistant_agg = LLMContextAggregatorPair(context, user_params=user_params)

    customer_logger = CustomerTurnLogger(session)
    monitor = BotTurnMonitor(session)

    pipeline = Pipeline(
        [
            transport.input(),
            stt,
            customer_logger,
            user_agg,
            llm,
            tts,
            transport.output(),
            monitor,
            assistant_agg,
        ]
    )

    worker = PipelineWorker(
        pipeline,
        params=PipelineParams(enable_metrics=True, enable_usage_metrics=True),
        processor_unusable_policy=ProcessorUnusablePolicy.END,
    )
    monitor.attach(worker)

    @transport.event_handler("on_client_connected")
    async def on_client_connected(transport, client):
        logger.info("Client connected — starting the call")
        session.started_at = time.time()
        await worker.queue_frames([LLMMessagesAppendFrame(messages=[{"role": "user", "content": KICKOFF_INSTRUCTION}], run_llm=True)])

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        logger.info("Client disconnected")
        await worker.cancel()

    async def hard_stop():
        await asyncio.sleep(settings.max_call_seconds)
        logger.warning("Hard stop: max call length reached")
        await worker.queue_frames([TTSSpeakFrame("We're out of time for today. Mumbai Bank will call you back. Thank you."), EndFrame()])

    stopper = asyncio.create_task(hard_stop())
    runner = WorkerRunner()
    try:
        await runner.add_workers(worker)
        await runner.run()
    finally:
        stopper.cancel()
        outcome = await session.finalize()
        if monitor.ttfb:
            session.guardrail_events.append({"kind": "ttfb_summary", **{k: v for k, v in monitor.ttfb.items()}})
            session.save()
        logger.info(f"Call {session.call_id} ended: {outcome.outcome.value} | webhook {session.webhook_result} | transcript saved to logs/calls/")


async def bot(runner_args: RunnerArguments):
    def _webrtc_params() -> TransportParams:
        # RNNoise (open-source) suppresses steady background noise on the incoming audio
        # before the speech detector or STT hear it. Optional: skipped if not installed.
        audio_filter = None
        if os.getenv("NOISE_SUPPRESSION", "1") == "1":
            try:
                from pipecat.audio.filters.rnnoise_filter import RNNoiseFilter

                audio_filter = RNNoiseFilter()
                logger.info("RNNoise noise suppression enabled on the microphone input")
            except Exception as e:  # noqa: BLE001
                logger.warning(f"RNNoise not available ({e}); running without noise suppression")
        return TransportParams(audio_in_enabled=True, audio_out_enabled=True, audio_in_filter=audio_filter)

    transport_params = {"webrtc": _webrtc_params}
    transport = await create_transport(runner_args, transport_params)
    await run_bot(transport)


if __name__ == "__main__":
    from pipecat.runner.run import main

    main()
