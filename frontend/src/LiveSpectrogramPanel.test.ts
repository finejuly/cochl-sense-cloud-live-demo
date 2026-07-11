import { createElement } from 'react';
import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { LiveSpectrogramPanel, framesInViewport } from './LiveSpectrogramPanel';
import type { LiveSpectrogramFrame } from './liveAudio';
import { emptyLiveDiagnostics } from './liveTimeline';

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

describe('LiveSpectrogramPanel', () => {
  it('labels the mel scale, energy legend, and frequency guides', () => {
    const frame: LiveSpectrogramFrame = {
      timestampSec: 1,
      magnitudes: new Array(64).fill(0.5),
      scale: 'mel',
      minFrequencyHz: 50,
      maxFrequencyHz: 16_000,
    };
    const { container } = render(createElement(LiveSpectrogramPanel, {
      frames: [frame],
      frameVersion: 1,
      events: [],
      diagnostics: emptyLiveDiagnostics(),
      viewport: { startSec: 0, endSec: 20 },
      totalDurationSec: 20,
      maxViewportStartSec: 0,
      autoFollow: true,
      historyLimit: 6,
      chunkRecords: [],
      chunkSnapshotMs: 0,
      canDownloadCsv: false,
      onDownloadCsv: vi.fn(),
      onOpenCsv: vi.fn(),
      onViewportStartChange: vi.fn(),
      onJumpToLatest: vi.fn(),
    }));

    expect(screen.getByRole('heading', { name: '실시간 멜 스펙트로그램' })).toBeInTheDocument();
    expect(screen.getByText('50 Hz–16 kHz · 64 멜 대역')).toBeInTheDocument();
    expect(screen.getByLabelText('에너지 색상 범례: 약함에서 강함')).toBeInTheDocument();
    expect(container.querySelector('canvas')).toHaveAccessibleName(
      '실시간 스펙트로그램 (멜), 50 Hz–16 kHz',
    );
    expect(container.querySelectorAll('.live-spectrogram-frequency-line')).toHaveLength(7);
  });
});
