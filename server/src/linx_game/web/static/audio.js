/*
 * 音频：角色语音播放（AudioOut）与麦克风采集（MicCapture）。
 *
 * 二进制帧格式（服务器 → 客户端）：
 *   [2 字节小端 JSON 头长度][UTF-8 JSON 头 {"cid","gain"}][16 kHz 单声道 s16le PCM]
 * 麦克风上行：16 kHz 单声道 s16le PCM，按约 60 ms 一帧发送。
 */
"use strict";

const PCM_RATE = 16000;
const MIC_FRAME_SAMPLES = 960; // 60 ms @ 16 kHz
const PLAYBACK_LEAD_S = 0.08; // 断流后重新起播的缓冲时长
const GAIN_SMOOTH_S = 0.06; // 音量平滑过渡的时间常数
const AUDIO_SCRIPT_URL = document.currentScript ? document.currentScript.src : location.href;

/** 解析一帧服务器下发的音频，返回 {header, samples}（samples 为 Float32Array），格式错误返回 null。 */
function parseAudioFrame(buffer) {
  if (!(buffer instanceof ArrayBuffer) || buffer.byteLength < 2) return null;
  const view = new DataView(buffer);
  const headerLen = view.getUint16(0, true);
  if (2 + headerLen > buffer.byteLength) return null;
  let header;
  try {
    header = JSON.parse(new TextDecoder().decode(new Uint8Array(buffer, 2, headerLen)));
  } catch (err) {
    return null;
  }
  const offset = 2 + headerLen;
  const count = (buffer.byteLength - offset) >> 1;
  const samples = new Float32Array(count);
  // 头长度可能为奇数，PCM 不一定按 2 字节对齐，因此逐个用 DataView 读取
  for (let i = 0; i < count; i++) samples[i] = view.getInt16(offset + i * 2, true) / 32768;
  return { header, samples };
}

/** 按角色分别排队、无缝播放语音；每个角色一个 GainNode。 */
class AudioOut {
  constructor(ctx) {
    this.ctx = ctx;
    this.master = ctx.createGain();
    this.master.connect(ctx.destination);
    this.streams = new Map(); // cid -> {gain, next, lastGain}
  }

  stream(cid) {
    let s = this.streams.get(cid);
    if (!s) {
      const gain = this.ctx.createGain();
      gain.gain.value = 0;
      gain.connect(this.master);
      s = { gain, next: 0, lastGain: 0 };
      this.streams.set(cid, s);
    }
    return s;
  }

  /** 处理一帧二进制音频，返回其 JSON 头（解析失败返回 null）。 */
  handleFrame(buffer) {
    const frame = parseAudioFrame(buffer);
    if (!frame) return null;
    const { header, samples } = frame;
    const cid = String(header.cid ?? "");
    const s = this.stream(cid);
    const now = this.ctx.currentTime;
    const target = clampGain(header.gain);
    if (samples.length === 0) {
      s.gain.gain.setTargetAtTime(target, now, GAIN_SMOOTH_S);
      s.lastGain = target;
      return header;
    }
    const buf = this.ctx.createBuffer(1, samples.length, PCM_RATE);
    buf.copyToChannel(samples, 0);
    const src = this.ctx.createBufferSource();
    src.buffer = buf;
    src.connect(s.gain);
    let startAt = s.next;
    if (startAt < now + 0.01) {
      // 队列已播完（或首次）：留出少量缓冲后起播，并让音量从当前值直接到位
      startAt = now + PLAYBACK_LEAD_S;
      s.gain.gain.cancelScheduledValues(now);
      s.gain.gain.setValueAtTime(s.lastGain, now);
    }
    s.gain.gain.setTargetAtTime(target, startAt, GAIN_SMOOTH_S);
    s.lastGain = target;
    src.start(startAt);
    s.next = startAt + buf.duration;
    return header;
  }

  /** 该角色当前是否有声音在播放，返回其音量（0 表示没有）。 */
  playingGain(cid) {
    const s = this.streams.get(cid);
    if (!s || s.next <= this.ctx.currentTime) return 0;
    return Math.max(s.lastGain, 0.05);
  }

  stopAll() {
    for (const s of this.streams.values()) {
      s.gain.gain.cancelScheduledValues(this.ctx.currentTime);
      s.gain.gain.setTargetAtTime(0, this.ctx.currentTime, 0.05);
      s.next = 0;
    }
  }
}

function clampGain(value) {
  const g = Number(value);
  if (!Number.isFinite(g)) return 1;
  return Math.min(2, Math.max(0, g));
}

/**
 * 麦克风采集：AudioWorklet 降采样为 16 kHz s16le，攒满 60 ms 回调一次 onFrame(Int16Array)。
 * onLevel(rms) 用于界面音量指示。
 */
class MicCapture {
  constructor(ctx, { onFrame, onLevel }) {
    this.ctx = ctx;
    this.onFrame = onFrame;
    this.onLevel = onLevel || (() => {});
    this.node = null;
    this.mediaStream = null;
    this.opening = null;
    this.recording = false;
    this.frame = new Int16Array(MIC_FRAME_SAMPLES);
    this.frameLen = 0;
    this.stopResolve = null;
  }

  get ready() {
    return this.node !== null;
  }

  /** 申请麦克风权限并建立采集链路；重复调用返回同一个 Promise。 */
  open() {
    if (!this.opening) this.opening = this._open();
    return this.opening;
  }

  async _open() {
    if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
      throw new Error("浏览器不支持麦克风采集（需要 https 或 localhost）");
    }
    if (!this.ctx.audioWorklet) throw new Error("浏览器不支持 AudioWorklet");
    this.mediaStream = await navigator.mediaDevices.getUserMedia({
      audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true, autoGainControl: true },
    });
    await this.ctx.audioWorklet.addModule(new URL("mic-worklet.js", AUDIO_SCRIPT_URL).href);
    const source = this.ctx.createMediaStreamSource(this.mediaStream);
    const node = new AudioWorkletNode(this.ctx, "mic-capture", {
      numberOfInputs: 1,
      numberOfOutputs: 1,
      outputChannelCount: [1],
      processorOptions: { targetRate: PCM_RATE },
    });
    node.port.onmessage = (event) => this._onMessage(event.data);
    // 处理器需接入渲染图才会被调度；经静音节点连到输出端
    const mute = this.ctx.createGain();
    mute.gain.value = 0;
    source.connect(node);
    node.connect(mute);
    mute.connect(this.ctx.destination);
    this.node = node;
  }

  start() {
    if (!this.node) return false;
    this.frameLen = 0;
    this.recording = true;
    this.node.port.postMessage({ type: "record", on: true });
    return true;
  }

  /** 停止录音；等处理器交回剩余样本、发出最后一个不足 60 ms 的帧后 resolve。 */
  stop() {
    if (!this.node || !this.recording) return Promise.resolve();
    return new Promise((resolve) => {
      const finish = () => {
        if (!this.recording) return;
        this.recording = false;
        this.stopResolve = null;
        if (this.frameLen > 0) {
          this.onFrame(this.frame.slice(0, this.frameLen));
          this.frameLen = 0;
        }
        this.onLevel(0);
        resolve();
      };
      this.stopResolve = finish;
      this.node.port.postMessage({ type: "record", on: false });
      setTimeout(finish, 400); // 处理器无响应时的兜底
    });
  }

  _onMessage(data) {
    if (data && data.type === "stopped") {
      if (this.stopResolve) this.stopResolve();
      return;
    }
    if (!(data instanceof Int16Array) || !this.recording) return;
    let sum = 0;
    for (let i = 0; i < data.length; i++) {
      const v = data[i] / 32768;
      sum += v * v;
      this.frame[this.frameLen++] = data[i];
      if (this.frameLen === MIC_FRAME_SAMPLES) {
        this.onFrame(this.frame.slice());
        this.frameLen = 0;
      }
    }
    this.onLevel(Math.sqrt(sum / Math.max(1, data.length)));
  }
}
