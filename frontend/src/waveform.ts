import type { SoundEvent } from './types';

export interface OverlayStyle {
  left: string;
  width: string;
}

export function peaksFromSamples(samples: Float32Array, bucketCount: number): number[] {
  if (!samples.length || bucketCount <= 0) {
    return [];
  }

  const bucketSize = Math.max(1, Math.ceil(samples.length / bucketCount));
  const peaks: number[] = [];

  for (let start = 0; start < samples.length; start += bucketSize) {
    const end = Math.min(samples.length, start + bucketSize);
    let peak = 0;
    for (let index = start; index < end; index += 1) {
      peak = Math.max(peak, Math.abs(samples[index]));
    }
    peaks.push(Number(Math.min(1, peak).toFixed(3)));
  }

  while (peaks.length < bucketCount) {
    peaks.push(0);
  }

  return peaks.slice(0, bucketCount);
}

export function clampEventToDuration(event: SoundEvent, durationSec: number): SoundEvent {
  const duration = Math.max(0, durationSec);
  const start = clamp(event.start_time_sec, 0, duration);
  const end = clamp(event.end_time_sec, start, duration);
  return {
    ...event,
    start_time_sec: start,
    end_time_sec: end,
  };
}

export function eventOverlayStyle(event: SoundEvent, durationSec: number): OverlayStyle {
  if (durationSec <= 0) {
    return { left: '0%', width: '0%' };
  }

  const clamped = clampEventToDuration(event, durationSec);
  const left = (clamped.start_time_sec / durationSec) * 100;
  const width = ((clamped.end_time_sec - clamped.start_time_sec) / durationSec) * 100;
  return {
    left: `${roundPercentage(left)}%`,
    width: `${roundPercentage(width)}%`,
  };
}

export function formatTime(totalSeconds: number): string {
  const safeSeconds = Math.max(0, Math.floor(totalSeconds));
  const minutes = Math.floor(safeSeconds / 60);
  const seconds = safeSeconds % 60;
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function roundPercentage(value: number): number {
  return Number(value.toFixed(3));
}
