import { getAudioContextConstructor } from './audioContext';

type AudioSamples = Float32Array<ArrayBufferLike>;

export interface LiveAudioWindow {
  samples: AudioSamples;
  sampleRate: number;
  windowStartSec: number;
  windowEndSec: number;
}

export interface LiveSpectrogramFrame {
  timestampSec: number;
  magnitudes: number[];
}

export interface LiveAudioCaptureOptions {
  windowSec?: number;
  hopSec?: number;
  onSpectrogramFrame?: (frame: LiveSpectrogramFrame) => void;
  spectrogramFps?: number;
  spectrogramBins?: number;
}

export class LiveWindowBuffer {
  private readonly sampleRate: number;
  private readonly windowSamples: number;
  private readonly hopSamples: number;
  private nextWindowEndSample: number;
  private buffer: AudioSamples = new Float32Array(0);
  private bufferStartSample = 0;
  private totalSamples = 0;

  constructor(options: { sampleRate: number; windowSec: number; hopSec: number }) {
    this.sampleRate = options.sampleRate;
    this.windowSamples = Math.max(1, Math.round(options.sampleRate * options.windowSec));
    this.hopSamples = Math.max(1, Math.round(options.sampleRate * options.hopSec));
    this.nextWindowEndSample = this.windowSamples;
  }

  push(samples: AudioSamples): LiveAudioWindow[] {
    if (samples.length) {
      this.buffer = concatSamples(this.buffer, samples);
      this.totalSamples += samples.length;
    }

    const windows: LiveAudioWindow[] = [];
    while (this.totalSamples >= this.nextWindowEndSample) {
      const windowStartSample = this.nextWindowEndSample - this.windowSamples;
      const bufferOffset = windowStartSample - this.bufferStartSample;
      const windowSamples = this.buffer.slice(bufferOffset, bufferOffset + this.windowSamples);

      windows.push({
        samples: windowSamples,
        sampleRate: this.sampleRate,
        windowStartSec: windowStartSample / this.sampleRate,
        windowEndSec: this.nextWindowEndSample / this.sampleRate,
      });
      this.nextWindowEndSample += this.hopSamples;
    }

    this.prune();
    return windows;
  }

  private prune() {
    const nextWindowStartSample = this.nextWindowEndSample - this.windowSamples;
    const pruneCount = nextWindowStartSample - this.bufferStartSample;
    if (pruneCount <= 0) {
      return;
    }

    this.buffer = this.buffer.slice(pruneCount);
    this.bufferStartSample += pruneCount;
  }
}

export function encodePcm16Wav(samples: AudioSamples, sampleRate: number): Blob {
  const bytesPerSample = 2;
  const channelCount = 1;
  const dataBytes = samples.length * bytesPerSample;
  const buffer = new ArrayBuffer(44 + dataBytes);
  const view = new DataView(buffer);

  writeAscii(view, 0, 'RIFF');
  view.setUint32(4, 36 + dataBytes, true);
  writeAscii(view, 8, 'WAVE');
  writeAscii(view, 12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, channelCount, true);
  view.setUint32(24, sampleRate, true);
  view.setUint32(28, sampleRate * channelCount * bytesPerSample, true);
  view.setUint16(32, channelCount * bytesPerSample, true);
  view.setUint16(34, 16, true);
  writeAscii(view, 36, 'data');
  view.setUint32(40, dataBytes, true);

  samples.forEach((sample, index) => {
    const clamped = Math.max(-1, Math.min(1, sample));
    const value = clamped < 0 ? clamped * 0x8000 : clamped * 0x7fff;
    view.setInt16(44 + index * bytesPerSample, value, true);
  });

  return new Blob([buffer], { type: 'audio/wav' });
}

export function createLiveAudioCapture(
  stream: MediaStream,
  onWindow: (window: LiveAudioWindow) => void,
  options: LiveAudioCaptureOptions = {},
): () => void {
  const AudioContextCtor = getAudioContextConstructor();
  if (!AudioContextCtor) {
    throw new Error('Web Audio API is not supported.');
  }

  const context = new AudioContextCtor();
  const source = context.createMediaStreamSource(stream);
  const analyser = context.createAnalyser();
  const processor = context.createScriptProcessor(4096, 1, 1);
  const buffer = new LiveWindowBuffer({
    sampleRate: context.sampleRate,
    windowSec: options.windowSec ?? 2,
    hopSec: options.hopSec ?? 1,
  });
  const spectrogramFps = sanitizePositiveNumber(options.spectrogramFps, 12);
  const spectrogramBins = sanitizePositiveInteger(options.spectrogramBins, 64);
  const spectrogramHopSamples = Math.max(1, Math.round(context.sampleRate / spectrogramFps));
  let nextSpectrogramSample = spectrogramHopSamples;
  let totalCapturedSamples = 0;

  analyser.fftSize = clampAnalyserFftSize(nextPowerOfTwo(spectrogramBins * 2));
  const frequencyData = new Uint8Array(analyser.frequencyBinCount);

  processor.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);
    totalCapturedSamples += input.length;
    buffer.push(input.slice(0)).forEach(onWindow);

    if (options.onSpectrogramFrame && totalCapturedSamples >= nextSpectrogramSample) {
      analyser.getByteFrequencyData(frequencyData);
      options.onSpectrogramFrame({
        timestampSec: totalCapturedSamples / context.sampleRate,
        magnitudes: magnitudesFromFrequencyData(frequencyData, spectrogramBins),
      });
      while (nextSpectrogramSample <= totalCapturedSamples) {
        nextSpectrogramSample += spectrogramHopSamples;
      }
    }
  };

  source.connect(analyser);
  analyser.connect(processor);
  processor.connect(context.destination);

  return () => {
    processor.onaudioprocess = null;
    try {
      source.disconnect();
    } catch {
      // Ignore cleanup errors from already-disconnected nodes.
    }
    try {
      analyser.disconnect();
    } catch {
      // Ignore cleanup errors from already-disconnected nodes.
    }
    try {
      processor.disconnect();
    } catch {
      // Ignore cleanup errors from already-disconnected nodes.
    }
    void context.close().catch(() => undefined);
  };
}

export function magnitudesFromFrequencyData(frequencyData: Uint8Array, binCount: number): number[] {
  if (!frequencyData.length || binCount <= 0) {
    return [];
  }

  const magnitudes: number[] = [];
  for (let bin = 0; bin < binCount; bin += 1) {
    const start = Math.floor((bin * frequencyData.length) / binCount);
    const end = Math.max(start + 1, Math.floor(((bin + 1) * frequencyData.length) / binCount));
    let sum = 0;
    for (let index = start; index < Math.min(end, frequencyData.length); index += 1) {
      sum += frequencyData[index];
    }
    const bucketSize = Math.max(1, Math.min(end, frequencyData.length) - start);
    magnitudes.push(Number(Math.min(1, Math.max(0, sum / bucketSize / 255)).toFixed(3)));
  }
  return magnitudes;
}

function concatSamples(left: AudioSamples, right: AudioSamples): AudioSamples {
  const result = new Float32Array(left.length + right.length);
  result.set(left, 0);
  result.set(right, left.length);
  return result;
}

function writeAscii(view: DataView, offset: number, value: string) {
  for (let index = 0; index < value.length; index += 1) {
    view.setUint8(offset + index, value.charCodeAt(index));
  }
}

function sanitizePositiveNumber(value: number | undefined, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 ? value : fallback;
}

function sanitizePositiveInteger(value: number | undefined, fallback: number): number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0
    ? Math.max(1, Math.floor(value))
    : fallback;
}

function nextPowerOfTwo(value: number): number {
  let power = 1;
  while (power < value) {
    power *= 2;
  }
  return power;
}

function clampAnalyserFftSize(value: number): number {
  return Math.min(32768, Math.max(128, value));
}
