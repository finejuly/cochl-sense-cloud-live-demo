import { describe, expect, it } from 'vitest';
import { formatTime } from './time';

describe('formatTime', () => {
  it('formats seconds as a minute timestamp', () => {
    expect(formatTime(65.4)).toBe('01:05');
  });

  it('clamps negative timestamps to zero', () => {
    expect(formatTime(-1)).toBe('00:00');
  });
});
