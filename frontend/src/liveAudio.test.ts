import { describe, expect, it } from 'vitest';
import { LiveWindowBuffer, encodePcm16Wav, magnitudesFromFrequencyData } from './liveAudio';

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

describe('magnitudesFromFrequencyData', () => {
  it('compresses analyzer bytes into normalized rounded buckets', () => {
    const magnitudes = magnitudesFromFrequencyData(Uint8Array.from([0, 64, 128, 255]), 2);

    expect(magnitudes).toEqual([0.125, 0.751]);
  });

  it('returns an empty array for empty input or non-positive bin counts', () => {
    expect(magnitudesFromFrequencyData(Uint8Array.from([]), 2)).toEqual([]);
    expect(magnitudesFromFrequencyData(Uint8Array.from([1, 2, 3]), 0)).toEqual([]);
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
