import { describe, expect, it } from 'vitest';
import { framesInViewport } from './LiveSpectrogramPanel';
import type { LiveSpectrogramFrame } from './liveAudio';

describe('framesInViewport', () => {
  it('selects only frames inside the requested viewport', () => {
    const frames: LiveSpectrogramFrame[] = Array.from({ length: 1000 }, (_, index) => ({
      timestampSec: index / 10,
      magnitudes: [0.5],
    }));

    const visible = framesInViewport(frames, { startSec: 20, endSec: 22 });

    expect(visible[0].timestampSec).toBe(20);
    expect(visible.at(-1)?.timestampSec).toBe(22);
    expect(visible).toHaveLength(21);
  });
});
