"""Background noise for test audio, added locally (no extra API calls)."""

import math
import random
import sys
from array import array

from backend.app.voice.audio import AudioClip


def add_noise(clip: AudioClip, snr_db: float | None, rng: random.Random) -> AudioClip:
    """Mix in Gaussian noise at `snr_db` (signal-to-noise ratio in decibels;
    lower is noisier). None returns the clip unchanged.

    The signal level is measured over the whole clip, pauses included, so the
    speech itself sits a little above the stated ratio.
    """
    if snr_db is None:
        return clip
    samples = array("h")
    samples.frombytes(clip.data)
    if sys.byteorder == "big":  # WAV samples are little-endian
        samples.byteswap()
    if not samples:
        return clip
    signal_rms = math.sqrt(sum(s * s for s in samples) / len(samples))
    if signal_rms == 0:
        return clip
    noise_rms = signal_rms / (10 ** (snr_db / 20))
    noisy = array("h", (max(-32768, min(32767, round(s + rng.gauss(0, noise_rms)))) for s in samples))
    if sys.byteorder == "big":
        noisy.byteswap()
    return AudioClip(data=noisy.tobytes(), sample_rate=clip.sample_rate)
