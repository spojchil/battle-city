/*
 * 麦克风采集 AudioWorklet 处理器。
 *
 * 将输入（通常为 44.1/48 kHz）按区间平均法降采样为 16 kHz 单声道，
 * 转换为 s16le 后分批发回主线程。区间平均同时起到简单低通滤波的作用，
 * 可抑制降采样带来的混叠，对语音足够。
 *
 * 主线程消息：
 *   {type: "record", on: true}   开始录音（清空残留状态）
 *   {type: "record", on: false}  停止录音：先发出剩余样本，再回复 {type: "stopped"}
 * 发往主线程：Int16Array（样本）或 {type: "stopped"}。
 */

const BATCH_SAMPLES = 320; // 20 ms @ 16 kHz

class MicCaptureProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    const opts = (options && options.processorOptions) || {};
    this.targetRate = opts.targetRate || 16000;
    this.recording = false;
    this.reset();
    this.port.onmessage = (event) => {
      const msg = event.data || {};
      if (msg.type !== "record") return;
      if (msg.on) {
        this.reset();
        this.recording = true;
      } else {
        this.flush();
        this.recording = false;
        this.port.postMessage({ type: "stopped" });
      }
    };
  }

  reset() {
    this.acc = 0;
    this.count = 0;
    this.phase = 0;
    this.out = new Int16Array(BATCH_SAMPLES);
    this.outLen = 0;
  }

  flush() {
    if (this.outLen === 0) return;
    const chunk = this.out.slice(0, this.outLen);
    this.port.postMessage(chunk, [chunk.buffer]);
    this.outLen = 0;
  }

  process(inputs) {
    const input = inputs[0];
    if (!this.recording || !input || input.length === 0) return true;
    const channels = input.length;
    const frames = input[0].length;
    for (let i = 0; i < frames; i++) {
      let s = input[0][i];
      if (channels > 1) {
        for (let c = 1; c < channels; c++) s += input[c][i];
        s /= channels;
      }
      this.acc += s;
      this.count += 1;
      this.phase += this.targetRate;
      if (this.phase >= sampleRate) {
        this.phase -= sampleRate;
        let v = this.acc / this.count;
        this.acc = 0;
        this.count = 0;
        if (v > 1) v = 1;
        else if (v < -1) v = -1;
        this.out[this.outLen++] = v < 0 ? v * 0x8000 : v * 0x7fff;
        if (this.outLen >= BATCH_SAMPLES) this.flush();
      }
    }
    return true;
  }
}

registerProcessor("mic-capture", MicCaptureProcessor);
