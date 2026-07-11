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

export type LiveAudioContextState = AudioContextState | 'interrupted';

export function appendCompactedSpectrogramFrame(
  frames: LiveSpectrogramFrame[],
  frame: LiveSpectrogramFrame,
  maxFrames: number,
): LiveSpectrogramFrame[] {
  const boundedMax = Math.max(2, Math.floor(maxFrames));
  frames.push(frame);
  if (frames.length <= boundedMax) {
    return frames;
  }

  // Preserve the full time range while progressively reducing resolution in
  // the oldest half. Recent frames remain at full capture resolution.
  const compactUntil = Math.floor(frames.length / 2);
  return frames.filter((_, index) => index >= compactUntil || index % 2 === 0);
}

export interface LiveAudioCaptureOptions {
  sampleRate?: number;
  windowSec?: number;
  hopSec?: number;
  onSpectrogramFrame?: (frame: LiveSpectrogramFrame) => void;
  spectrogramFps?: number;
  spectrogramBins?: number;
  onAudioTimeUpdate?: (elapsedSec: number) => void;
  onStateChange?: (state: LiveAudioContextState) => void;
}

export type LiveAudioCaptureController = (() => void) & {
  resume: () => Promise<void>;
  state: () => LiveAudioContextState;
};

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

export async function createLiveAudioCapture(
  stream: MediaStream,
  onWindow: (window: LiveAudioWindow) => void,
  options: LiveAudioCaptureOptions = {},
): Promise<LiveAudioCaptureController> {
  const AudioContextCtor = getAudioContextConstructor();
  if (!AudioContextCtor) {
    throw new Error('Web Audio API is not supported.');
  }

  const requestedSampleRate = positiveIntegerOrNull(options.sampleRate);
  const context = requestedSampleRate === null
    ? new AudioContextCtor()
    : new AudioContextCtor({ sampleRate: requestedSampleRate });
  if (requestedSampleRate !== null && context.sampleRate !== requestedSampleRate) {
    await context.close().catch(() => undefined);
    throw new Error(`${requestedSampleRate} Hz 오디오 처리를 지원하지 않는 환경입니다.`);
  }

  const initialState = context.state as LiveAudioContextState | undefined;
  if (initialState && initialState !== 'running' && initialState !== 'closed') {
    await context.resume().catch(async (error) => {
      await context.close().catch(() => undefined);
      throw error;
    });
  }
  if (context.state && context.state !== 'running') {
    await context.close().catch(() => undefined);
    throw new Error(`오디오 처리를 시작하지 못했습니다 (${context.state}).`);
  }
  const source = context.createMediaStreamSource(stream);
  const analyser = context.createAnalyser();
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

  const processSamples = (input: AudioSamples) => {
    totalCapturedSamples += input.length;
    options.onAudioTimeUpdate?.(totalCapturedSamples / context.sampleRate);
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

  let captureNode: AudioNode;
  let detachCapture: () => void;
  if (context.audioWorklet && typeof AudioWorkletNode !== 'undefined') {
    const workletUrl = URL.createObjectURL(
      new Blob([LIVE_CAPTURE_WORKLET_SOURCE], { type: 'text/javascript' }),
    );
    try {
      await context.audioWorklet.addModule(workletUrl);
    } catch (error) {
      await context.close().catch(() => undefined);
      throw error;
    } finally {
      URL.revokeObjectURL(workletUrl);
    }
    let worklet: AudioWorkletNode;
    try {
      worklet = new AudioWorkletNode(context, LIVE_CAPTURE_WORKLET_NAME, {
        numberOfInputs: 1,
        numberOfOutputs: 1,
        outputChannelCount: [1],
      });
    } catch (error) {
      await context.close().catch(() => undefined);
      throw error;
    }
    worklet.port.onmessage = (event: MessageEvent<Float32Array | ArrayBuffer>) => {
      const samples = event.data instanceof Float32Array
        ? event.data
        : new Float32Array(event.data);
      processSamples(samples);
    };
    captureNode = worklet;
    detachCapture = () => {
      worklet.port.onmessage = null;
      worklet.port.close();
    };
  } else {
    // Compatibility fallback for older WebViews. Supported browsers use the
    // AudioWorklet path above so capture no longer runs on the render thread.
    const processor = context.createScriptProcessor(4096, 1, 1);
    processor.onaudioprocess = (event) => {
      processSamples(event.inputBuffer.getChannelData(0));
    };
    captureNode = processor;
    detachCapture = () => {
      processor.onaudioprocess = null;
    };
  }

  source.connect(analyser);
  analyser.connect(captureNode);
  captureNode.connect(context.destination);

  const handleStateChange = () => options.onStateChange?.(
    context.state as LiveAudioContextState,
  );
  context.addEventListener?.('statechange', handleStateChange);

  let cleanedUp = false;
  const cleanup = (() => {
    if (cleanedUp) {
      return;
    }
    cleanedUp = true;
    context.removeEventListener?.('statechange', handleStateChange);
    detachCapture();
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
      captureNode.disconnect();
    } catch {
      // Ignore cleanup errors from already-disconnected nodes.
    }
    void context.close().catch(() => undefined);
  }) as LiveAudioCaptureController;
  cleanup.resume = async () => {
    if (cleanedUp) {
      throw new Error('이미 종료된 오디오 캡처는 재개할 수 없습니다.');
    }
    if (context.state !== 'running') {
      await context.resume();
    }
    if (context.state !== 'running') {
      throw new Error(`오디오 처리를 재개하지 못했습니다 (${context.state}).`);
    }
    options.onStateChange?.(context.state as LiveAudioContextState);
  };
  cleanup.state = () => context.state as LiveAudioContextState;
  if ((context.state as string) === 'closed') {
    cleanup();
    throw new Error('오디오 처리 연결이 시작 중 종료되었습니다.');
  }
  options.onStateChange?.(context.state as LiveAudioContextState);
  return cleanup;
}

const LIVE_CAPTURE_WORKLET_NAME = 'cochl-live-audio-capture';
const LIVE_CAPTURE_WORKLET_SOURCE = `
class CochlLiveAudioCaptureProcessor extends AudioWorkletProcessor {
  constructor() {
    super();
    this.buffer = new Float32Array(4096);
    this.offset = 0;
  }

  process(inputs) {
    const input = inputs[0] && inputs[0][0];
    if (!input || input.length === 0) {
      return true;
    }

    let sourceOffset = 0;
    while (sourceOffset < input.length) {
      const copyLength = Math.min(input.length - sourceOffset, this.buffer.length - this.offset);
      this.buffer.set(input.subarray(sourceOffset, sourceOffset + copyLength), this.offset);
      this.offset += copyLength;
      sourceOffset += copyLength;
      if (this.offset === this.buffer.length) {
        const completed = this.buffer;
        this.port.postMessage(completed, [completed.buffer]);
        this.buffer = new Float32Array(4096);
        this.offset = 0;
      }
    }
    return true;
  }
}

registerProcessor('${LIVE_CAPTURE_WORKLET_NAME}', CochlLiveAudioCaptureProcessor);
`;

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

function positiveIntegerOrNull(value: number | undefined): number | null {
  if (typeof value !== 'number' || !Number.isFinite(value) || value <= 0) {
    return null;
  }
  return Math.max(1, Math.round(value));
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
