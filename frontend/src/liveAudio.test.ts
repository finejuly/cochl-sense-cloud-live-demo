import { describe, expect, it } from 'vitest';
import {
  LiveWindowBuffer,
  appendCompactedSpectrogramFrame,
  encodePcm16Wav,
  melFrequencyPositionPercent,
  melMagnitudesFromDecibels,
  type LiveSpectrogramFrame,
} from './liveAudio';

describe('LiveWindowBuffer', () => {
  it('emits overlapping windows only after enough samples arrive', () => {
    const buffer = new LiveWindowBuffer({ sampleRate: 4, windowSec: 2, hopSec: 1 });

    expect(buffer.push(Float32Array.from([0, 1, 2, 3, 4, 5, 6]))).toEqual([]);
    const first = buffer.push(Float32Array.from([7]));

    expect(first).toHaveLength(1);
    expect(first[0].windowStartSec).toBe(0);
    expect(first[0].windowEndSec).toBe(2);
    expect(Array.from(first[0].samples)).toEqual([0, 1, 2, 3, 4, 5, 6, 7]);

    const second = buffer.push(Float32Array.from([8, 9, 10, 11]));

    expect(second).toHaveLength(1);
    expect(second[0].windowStartSec).toBe(1);
    expect(second[0].windowEndSec).toBe(3);
    expect(Array.from(second[0].samples)).toEqual([4, 5, 6, 7, 8, 9, 10, 11]);
  });

  it('emits every complete window from one large audio batch', () => {
    const buffer = new LiveWindowBuffer({ sampleRate: 4, windowSec: 2, hopSec: 1 });

    const windows = buffer.push(Float32Array.from(Array.from({ length: 16 }, (_, index) => index)));

    expect(windows.map((window) => [window.windowStartSec, window.windowEndSec])).toEqual([
      [0, 2],
      [1, 3],
      [2, 4],
    ]);
    expect(windows.map((window) => Array.from(window.samples))).toEqual([
      [0, 1, 2, 3, 4, 5, 6, 7],
      [4, 5, 6, 7, 8, 9, 10, 11],
      [8, 9, 10, 11, 12, 13, 14, 15],
    ]);
  });
});

describe('encodePcm16Wav', () => {
  it('encodes mono PCM samples into a WAV blob', async () => {
    const blob = encodePcm16Wav(Float32Array.from([-1, 0, 1]), 16000);
    const bytes = new Uint8Array(await readBlob(blob));
    const view = new DataView(bytes.buffer);
    const text = (start: number, end: number) => String.fromCharCode(...bytes.slice(start, end));

    expect(blob.type).toBe('audio/wav');
    expect(text(0, 4)).toBe('RIFF');
    expect(text(8, 12)).toBe('WAVE');
    expect(text(12, 16)).toBe('fmt ');
    expect(view.getUint16(20, true)).toBe(1);
    expect(view.getUint16(22, true)).toBe(1);
    expect(view.getUint32(24, true)).toBe(16000);
    expect(view.getUint16(34, true)).toBe(16);
    expect(text(36, 40)).toBe('data');
    expect(view.getUint32(40, true)).toBe(6);
    expect(view.getInt16(44, true)).toBe(-32768);
    expect(view.getInt16(46, true)).toBe(0);
    expect(view.getInt16(48, true)).toBe(32767);
  });
});

describe('melMagnitudesFromDecibels', () => {
  const options = {
    sampleRate: 48_000,
    binCount: 64,
    minFrequencyHz: 50,
    maxFrequencyHz: 16_000,
    minDecibels: -95,
    maxDecibels: -25,
  };

  it('maps low and high tones to distinct ordered mel bands', () => {
    const lowTone = spectrumWithTone(500, -25);
    const highTone = spectrumWithTone(8_000, -25);

    const lowMagnitudes = melMagnitudesFromDecibels(lowTone, options);
    const highMagnitudes = melMagnitudesFromDecibels(highTone, options);
    const lowPeakIndex = indexOfMaximum(lowMagnitudes);
    const highPeakIndex = indexOfMaximum(highMagnitudes);

    expect(lowMagnitudes).toHaveLength(64);
    expect(highMagnitudes).toHaveLength(64);
    expect(lowPeakIndex).toBeLessThan(highPeakIndex);
    expect(lowMagnitudes[lowPeakIndex]).toBeGreaterThan(0.7);
    expect(highMagnitudes[highPeakIndex]).toBeGreaterThan(0.6);
  });

  it('normalizes the configured decibel range and clamps silence safely', () => {
    const floor = new Float32Array(1024).fill(-95);
    const ceiling = new Float32Array(1024).fill(-25);
    const silence = new Float32Array(1024).fill(Number.NEGATIVE_INFINITY);

    expect(melMagnitudesFromDecibels(floor, options)).toEqual(new Array(64).fill(0));
    expect(melMagnitudesFromDecibels(ceiling, options)).toEqual(new Array(64).fill(1));
    expect(melMagnitudesFromDecibels(silence, options)).toEqual(new Array(64).fill(0));
  });

  it('returns an empty array for empty input or non-positive bin counts', () => {
    expect(melMagnitudesFromDecibels(new Float32Array(), options)).toEqual([]);
    expect(melMagnitudesFromDecibels(new Float32Array([1, 2, 3]), {
      ...options,
      binCount: 0,
    })).toEqual([]);
    expect(melMagnitudesFromDecibels(new Float32Array([1, 2, 3]), {
      ...options,
      sampleRate: 0,
    })).toEqual([]);
  });

  it('places frequency guides from the top high band to the bottom low band', () => {
    expect(melFrequencyPositionPercent(16_000)).toBeCloseTo(0);
    expect(melFrequencyPositionPercent(50)).toBeCloseTo(100);
    expect(melFrequencyPositionPercent(8_000)).toBeLessThan(
      melFrequencyPositionPercent(1_000),
    );
  });
});

describe('appendCompactedSpectrogramFrame', () => {
  it('bounds memory while preserving the full time range and recent resolution', () => {
    let frames: LiveSpectrogramFrame[] = [];

    for (let timestampSec = 0; timestampSec <= 20; timestampSec += 1) {
      frames = appendCompactedSpectrogramFrame(
        frames,
        { timestampSec, magnitudes: [timestampSec / 20] },
        8,
      );
    }

    expect(frames.length).toBeLessThanOrEqual(8);
    expect(frames[0].timestampSec).toBe(0);
    expect(frames.at(-1)?.timestampSec).toBe(20);
    expect(frames.slice(-3).map((frame) => frame.timestampSec)).toEqual([18, 19, 20]);
  });
});

function readBlob(blob: Blob): Promise<ArrayBuffer> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(reader.error);
    reader.onload = () => resolve(reader.result as ArrayBuffer);
    reader.readAsArrayBuffer(blob);
  });
}

function spectrumWithTone(frequencyHz: number, decibels: number): Float32Array {
  const sampleRate = 48_000;
  const spectrum = new Float32Array(1024).fill(-95);
  const frequencyPerBin = sampleRate / (spectrum.length * 2);
  spectrum[Math.round(frequencyHz / frequencyPerBin)] = decibels;
  return spectrum;
}

function indexOfMaximum(values: number[]): number {
  return values.reduce(
    (maximumIndex, value, index) => value > values[maximumIndex] ? index : maximumIndex,
    0,
  );
}
