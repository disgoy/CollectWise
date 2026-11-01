"""The Flux voice socket (`/v2/speak`, M7.6 B2) behind the same speaker as
Aura: every Aura speaker test passes against a fake that answers like a Flux
voice (Speak / Interrupt / SpeechInterrupted), and the differences are
tested here: which socket opens, an interruption the socket never confirms,
and an error message."""

import asyncio
import logging
from types import SimpleNamespace

import pytest
from deepgram.core.unchecked_base_model import construct_type
from deepgram.speak.v2.socket_client import V2SocketClientResponse

from backend.app.voice import deepgram_live
from backend.app.voice.deepgram_live import AURA, FLUX_VOICE, DeepgramLiveTTS, speak_protocol
from backend.app.voice.live_speech import SegmentDone, SpokenAudio
from backend.app.voice.speech import SpeechUnavailableError
from backend.tests import test_deepgram_live as aura_tests
from backend.tests.test_deepgram_live import END, FakeAuraSocket, FakeConnector, run, take, take_until_done


class FakeFluxVoiceSocket(FakeAuraSocket):
    """Answers like a Flux voice. Records sentences as "text" and
    interruptions as "clear", like the Aura fake, so the Aura tests read the
    same."""

    def __init__(self, *, stale_audio_after_clear: bool = False, confirms_interrupt: bool = True) -> None:
        super().__init__(stale_audio_after_clear=stale_audio_after_clear)
        self.confirms_interrupt = confirms_interrupt

    async def __aiter__(self):
        while True:
            item = await self.incoming.get()
            if item is END:
                return
            if isinstance(item, BaseException):
                raise item
            yield item if isinstance(item, bytes) else construct_type(type_=V2SocketClientResponse, object_=item)

    async def send_text(self, message) -> None:
        raise AssertionError("a Flux voice has no Text message")

    async def send_clear(self) -> None:
        raise AssertionError("a Flux voice has no Clear message")

    async def send_speak(self, message) -> None:
        self.sent.append(("text", message.text))
        self.pending += message.text

    async def send_flush(self) -> None:
        self.sent.append(("flush", ""))
        audio = self.pending.encode()
        self.pending = ""
        self.push(audio[: len(audio) // 2])
        self.push(audio[len(audio) // 2:])
        self.push({"type": "Flushed", "speech_id": f"s{self.flushes}"})
        self.flushes += 1

    async def send_interrupt(self) -> None:
        self.sent.append(("clear", ""))
        self.pending = ""
        if self.stale_audio_after_clear:
            self.push(b"stale audio")
            self.push({"type": "Flushed", "speech_id": "s99"})
        if self.confirms_interrupt:
            self.push({"type": "SpeechInterrupted", "audio_played_ms": 0, "text_spoken": "",
                       "text_remaining": "", "metadata": {}})


def flux_client(speak) -> SimpleNamespace:
    return SimpleNamespace(speak=SimpleNamespace(v1=None, v2=SimpleNamespace(connect=speak)))


def flux_tts(connector: FakeConnector) -> DeepgramLiveTTS:
    return DeepgramLiveTTS(flux_client(connector), voice="flux-rahul-hi")


# Every Aura test of the speaker itself, not of Aura's own settings.
SPEAKER_TESTS = [
    aura_tests.test_a_one_sentence_reply_is_flushed_at_once,
    aura_tests.test_a_longer_reply_uses_two_flushes_first_sentence_then_the_rest,
    aura_tests.test_audio_for_the_rest_arriving_before_its_flush_belongs_to_the_rest,
    aura_tests.test_a_warning_never_completes_a_segment,
    aura_tests.test_clear_discards_everything_until_cleared_then_the_next_reply_works,
    aura_tests.test_clear_drops_the_open_reply_so_end_reply_sends_no_flush,
    aura_tests.test_an_idle_close_is_quiet_and_the_next_sentence_reconnects,
    aura_tests.test_a_drop_mid_reply_is_an_outage,
    aura_tests.test_a_sentence_over_the_limit_is_refused_before_sending,
    aura_tests.test_sentence_numbers_keep_counting_across_replies,
    aura_tests.test_closing_the_speaker_closes_the_socket_and_ends_the_output,
]


@pytest.mark.parametrize("aura_test", SPEAKER_TESTS, ids=[t.__name__[5:] for t in SPEAKER_TESTS])
def test_a_flux_voice_passes_every_aura_speaker_test(aura_test, monkeypatch) -> None:
    monkeypatch.setattr(aura_tests, "FakeAuraSocket", FakeFluxVoiceSocket)
    monkeypatch.setattr(aura_tests, "tts", flux_tts)
    aura_test()


def test_flux_voices_use_v2_speak_and_everything_else_aura() -> None:
    assert speak_protocol("flux-rahul-hi") is FLUX_VOICE
    assert speak_protocol("flux-meena-en") is FLUX_VOICE
    assert speak_protocol("aura-2-thalia-en") is AURA


def test_rahuls_socket_opens_at_the_start_of_the_call_with_our_settings() -> None:
    connector = FakeConnector([FakeFluxVoiceSocket()])

    async def go():
        async with flux_tts(connector).open() as speaker:
            assert speaker.sample_rate == 24_000

    run(go())

    assert connector.calls == [
        {"model": "flux-rahul-hi", "encoding": "linear16", "sample_rate": 24_000, "mip_opt_out": "true"}
    ]


def test_an_interruption_never_confirmed_stops_discarding_after_a_wait(monkeypatch, caplog) -> None:
    monkeypatch.setattr(deepgram_live, "INTERRUPT_WAIT_S", 0.05)
    socket = FakeFluxVoiceSocket(confirms_interrupt=False)

    async def go():
        async with flux_tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("A long first reply.")
            await speaker.clear()
            await asyncio.sleep(0.1)
            await speaker.say("Next.")
            await speaker.end_reply()
            return await take_until_done(speaker, 1)

    with caplog.at_level(logging.WARNING):
        items = run(go())

    assert items[-3:] == [SpokenAudio(1, b"Ne"), SpokenAudio(1, b"xt."), SegmentDone(1, (1,))]
    assert "never confirmed an interruption" in caplog.text


def test_a_confirmed_interruption_needs_no_wait(monkeypatch, caplog) -> None:
    monkeypatch.setattr(deepgram_live, "INTERRUPT_WAIT_S", 0.05)
    socket = FakeFluxVoiceSocket()

    async def go():
        async with flux_tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("A long first reply.")
            await speaker.clear()
            await speaker.say("Next.")
            await speaker.end_reply()
            items = await take_until_done(speaker, 1)
            await asyncio.sleep(0.1)  # past the wait: nothing more happens
            return items

    with caplog.at_level(logging.WARNING):
        items = run(go())

    assert items[-1] == SegmentDone(1, (1,))
    assert "never confirmed" not in caplog.text


def test_audio_before_an_unconfirmed_interruption_times_out_is_still_dropped(monkeypatch) -> None:
    monkeypatch.setattr(deepgram_live, "INTERRUPT_WAIT_S", 5)
    socket = FakeFluxVoiceSocket(confirms_interrupt=False)

    async def go():
        async with flux_tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("A long first reply.")
            await speaker.clear()
            socket.push(b"stale audio")  # still the interrupted reply's
            socket.push({"type": "SpeechInterrupted", "audio_played_ms": 0, "metadata": {}})
            await speaker.say("Next.")
            await speaker.end_reply()
            return await take_until_done(speaker, 1)

    items = run(go())

    assert SpokenAudio(0, b"stale audio") not in items and SpokenAudio(1, b"stale audio") not in items


def test_an_error_mid_reply_is_an_outage_and_only_its_code_is_logged(caplog) -> None:
    socket = FakeFluxVoiceSocket()

    async def go():
        async with flux_tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("One.")
            await speaker.say("Two.")
            await take(speaker, 3)
            socket.push({"type": "Error", "code": "DATA-0000", "description": "bad text: Two."})
            await take(speaker, 1)

    with caplog.at_level(logging.WARNING), pytest.raises(SpeechUnavailableError, match="tts_DATA-0000"):
        run(go())
    assert "DATA-0000" in caplog.text and "bad text" not in caplog.text


def test_an_error_between_replies_only_logs(caplog) -> None:
    socket = FakeFluxVoiceSocket()

    async def go():
        async with flux_tts(FakeConnector([socket])).open() as speaker:
            socket.push({"type": "Error", "code": "NET-0001", "description": "idle"})
            await asyncio.sleep(0)
            await speaker.say("Hello.")
            await speaker.end_reply()
            return await take(speaker, 3)

    with caplog.at_level(logging.WARNING):
        items = run(go())

    assert items[-1] == SegmentDone(0, (0,))
    assert "NET-0001" in caplog.text


def test_a_second_interruption_gets_its_own_full_wait(monkeypatch) -> None:
    # The first interruption's wait must not end the second one's early, or
    # the second reply's late audio could reach the next reply.
    monkeypatch.setattr(deepgram_live, "INTERRUPT_WAIT_S", 0.2)
    socket = FakeFluxVoiceSocket(confirms_interrupt=False)

    async def go():
        async with flux_tts(FakeConnector([socket])).open() as speaker:
            await speaker.say("First.")
            await speaker.clear()
            await asyncio.sleep(0.15)
            await speaker.say("Second.")
            await speaker.clear()  # 0.15 s into the first wait
            await asyncio.sleep(0.1)  # past the first wait, inside the second
            still_waiting = speaker._discarding
            await asyncio.sleep(0.2)  # the second wait ends
            return still_waiting, speaker._discarding

    assert run(go()) == (True, False)
