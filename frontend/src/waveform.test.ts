import { describe, expect, it } from 'vitest';
import {
  clampEventToDuration,
  eventOverlayStyle,
  formatTime,
  peaksFromSamples,
} from './waveform';

describe('waveform utilities', () => {
  it('builds normalized peak buckets from samples', () => {
    const peaks = peaksFromSamples(Float32Array.from([0, 0.25, -0.5, 1, -1, 0.1]), 3);

    expect(peaks).toEqual([0.25, 1, 1]);
  });

  it('clamps events to the available recording duration', () => {
    const event = clampEventToDuration(
      { start_time_sec: -2, end_time_sec: 12, label: 'Speech', confidence: 0.9 },
      10,
    );

    expect(event.start_time_sec).toBe(0);
    expect(event.end_time_sec).toBe(10);
  });

  it('creates percentage overlay style from event time range', () => {
    const style = eventOverlayStyle(
      { start_time_sec: 2, end_time_sec: 5, label: 'Speech', confidence: 0.9 },
      10,
    );

    expect(style.left).toBe('20%');
    expect(style.width).toBe('30%');
  });

  it('formats seconds as minute timestamp', () => {
    expect(formatTime(65.4)).toBe('01:05');
  });
});
