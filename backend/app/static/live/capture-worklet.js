// Runs in the browser's audio thread: passes the microphone's samples to the
// page in blocks of about 20 ms (M5.6). Resampling happens on the page.
class CaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.block = new Float32Array(1024);
    this.filled = 0;
  }

  process(inputs) {
    const channel = inputs[0] && inputs[0][0];
    if (channel) {
      for (let i = 0; i < channel.length; i++) {
        this.block[this.filled++] = channel[i];
        if (this.filled === this.block.length) {
          this.port.postMessage(this.block.slice());
          this.filled = 0;
        }
      }
    }
    return true; // keep running while the call lasts
  }
}

registerProcessor("capture", CaptureProcessor);
