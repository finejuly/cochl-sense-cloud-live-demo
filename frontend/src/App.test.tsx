import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { LiveAudioCaptureOptions, LiveAudioWindow, LiveSpectrogramFrame } from './liveAudio';

const appMocks = vi.hoisted(() => ({
  analyzeRecording: vi.fn(),
  analyzeLiveChunk: vi.fn(),
  createLiveAudioCapture: vi.fn(),
  liveWindowCallbacks: [] as Array<(window: LiveAudioWindow) => void>,
  liveSpectrogramCallbacks: [] as Array<(frame: LiveSpectrogramFrame) => void>,
  liveCleanups: [] as Array<() => void>,
}));

vi.mock('./api', () => ({
  analyzeRecording: appMocks.analyzeRecording,
  analyzeLiveChunk: appMocks.analyzeLiveChunk,
}));

vi.mock('./liveAudio', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./liveAudio')>();
  return {
    ...actual,
    createLiveAudioCapture: appMocks.createLiveAudioCapture,
  };
});

import App, { AnalysisPanel } from './App';
import type { AnalysisResponse, LiveChunkAnalysisResponse, SoundEvent } from './types';

beforeEach(() => {
  appMocks.analyzeRecording.mockReset();
  appMocks.analyzeLiveChunk.mockReset();
  appMocks.createLiveAudioCapture.mockReset();
  appMocks.liveWindowCallbacks = [];
  appMocks.liveSpectrogramCallbacks = [];
  appMocks.liveCleanups = [];
  appMocks.analyzeLiveChunk.mockResolvedValue({
    sequence_id: 1,
    window_start_sec: 0,
    window_end_sec: 2,
    sound_events: [],
    processing_time_ms: 1,
  });
  appMocks.createLiveAudioCapture.mockImplementation((_stream, onWindow, options?: LiveAudioCaptureOptions) => {
    appMocks.liveWindowCallbacks.push(onWindow);
    if (options?.onSpectrogramFrame) {
      appMocks.liveSpectrogramCallbacks.push(options.onSpectrogramFrame);
    }
    const cleanup = vi.fn();
    appMocks.liveCleanups.push(cleanup);
    return cleanup;
  });
});

function installRecordingEnvironment() {
  const originalMediaDevices = navigator.mediaDevices;
  const originalMediaRecorder = window.MediaRecorder;
  const stoppedTrack = { stop: vi.fn() };
  const stream = { getTracks: () => [stoppedTrack] } as unknown as MediaStream;
  let recorder: FakeMediaRecorder | null = null;

  class FakeMediaRecorder {
    static isTypeSupported = vi.fn(() => true);
    mimeType = 'audio/webm';
    state: RecordingState = 'inactive';
    ondataavailable: ((event: BlobEvent) => void) | null = null;
    onstop: (() => void) | null = null;

    constructor() {
      recorder = this;
    }

    start() {
      this.state = 'recording';
    }

    stop() {
      this.state = 'inactive';
    }
  }

  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    value: {
      getUserMedia: vi.fn(async () => stream),
    },
  });
  Object.defineProperty(window, 'MediaRecorder', {
    configurable: true,
    value: FakeMediaRecorder,
  });

  return {
    stoppedTrack,
    get recorder() {
      return recorder;
    },
    async finishRecording(blob: Blob | null = new Blob(['audio'], { type: 'audio/webm' })) {
      await act(async () => {
        if (blob) {
          recorder?.ondataavailable?.({ data: blob } as BlobEvent);
        }
        recorder?.onstop?.();
      });
    },
    restore() {
      Object.defineProperty(navigator, 'mediaDevices', {
        configurable: true,
        value: originalMediaDevices,
      });
      Object.defineProperty(window, 'MediaRecorder', {
        configurable: true,
        value: originalMediaRecorder,
      });
    },
  };
}

function liveWindow(windowStartSec: number, windowEndSec: number): LiveAudioWindow {
  return {
    samples: Float32Array.from([0, 0, 0, 0]),
    sampleRate: 2,
    windowStartSec,
    windowEndSec,
  };
}

function liveResponse(
  sequenceId: number,
  events: Array<Pick<SoundEvent, 'label' | 'confidence'> & Partial<Pick<SoundEvent, 'start_time_sec' | 'end_time_sec'>>>,
): LiveChunkAnalysisResponse {
  return {
    sequence_id: sequenceId,
    window_start_sec: 0,
    window_end_sec: 2,
    sound_events: events.map((event, index) => ({
      start_time_sec: event.start_time_sec ?? index,
      end_time_sec: event.end_time_sec ?? index + 0.5,
      label: event.label,
      confidence: event.confidence,
    })),
    processing_time_ms: 1,
  };
}

function deferredLiveResponse(sequenceId: number) {
  let resolve: (value: LiveChunkAnalysisResponse) => void = () => undefined;
  const promise = new Promise<LiveChunkAnalysisResponse>((nextResolve) => {
    resolve = nextResolve;
  });
  return {
    promise,
    resolve,
    sequenceId,
  };
}

async function blobText(blob: Blob): Promise<string> {
  const textMethod = (blob as Blob & { text?: () => Promise<string> }).text;
  if (typeof textMethod === 'function') {
    return textMethod.call(blob);
  }

  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result ?? ''));
    reader.onerror = () => reject(reader.error ?? new Error('Could not read blob'));
    reader.readAsText(blob);
  });
}

afterEach(() => {
  vi.restoreAllMocks();
});

describe('App', () => {
  it('renders the recording workspace', () => {
    render(<App />);

    expect(screen.getByRole('button', { name: /녹음 시작/i })).toBeInTheDocument();
    expect(screen.getByText(/Cochl\.Sense Cloud API/i)).toBeInTheDocument();
  });

  it('shows an unsupported message when media APIs are missing', async () => {
    const originalMediaDevices = navigator.mediaDevices;
    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: undefined,
    });

    render(<App />);
    await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));

    expect(screen.getByText(/마이크 녹음을 지원하지 않는 브라우저/i)).toBeInTheDocument();

    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: originalMediaDevices,
    });
  });

  it('keeps a discarded in-progress recording idle when the stop event resolves later', async () => {
    const originalMediaDevices = navigator.mediaDevices;
    const originalMediaRecorder = window.MediaRecorder;
    const stoppedTrack = { stop: vi.fn() };
    let recorder: FakeMediaRecorder | null = null;

    class FakeMediaRecorder {
      static isTypeSupported = vi.fn(() => true);
      mimeType = 'audio/webm';
      state: RecordingState = 'inactive';
      ondataavailable: ((event: BlobEvent) => void) | null = null;
      onstop: (() => void) | null = null;

      constructor() {
        recorder = this;
      }

      start() {
        this.state = 'recording';
      }

      stop() {
        this.state = 'inactive';
      }
    }

    Object.defineProperty(navigator, 'mediaDevices', {
      configurable: true,
      value: {
        getUserMedia: vi.fn(async () => ({
          getTracks: () => [stoppedTrack],
        })),
      },
    });
    Object.defineProperty(window, 'MediaRecorder', {
      configurable: true,
      value: FakeMediaRecorder,
    });

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await userEvent.click(screen.getByRole('button', { name: /폐기/i }));
      await act(async () => {
        recorder?.ondataavailable?.({
          data: new Blob(['audio'], { type: 'audio/webm' }),
        } as BlobEvent);
        recorder?.onstop?.();
      });

      expect(stoppedTrack.stop).toHaveBeenCalled();
      expect(appMocks.liveCleanups[0]).toHaveBeenCalled();
      expect(screen.getByRole('button', { name: /분석/i })).toBeDisabled();
      expect(screen.queryByText(/업로드 준비/i)).not.toBeInTheDocument();
    } finally {
      Object.defineProperty(navigator, 'mediaDevices', {
        configurable: true,
        value: originalMediaDevices,
      });
      Object.defineProperty(window, 'MediaRecorder', {
        configurable: true,
        value: originalMediaRecorder,
      });
    }
  });

  it('starts live capture and cleans it up when recording is completed', async () => {
    const recording = installRecordingEnvironment();

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      expect(appMocks.createLiveAudioCapture).toHaveBeenCalledTimes(1);
      expect(screen.getByLabelText(/실시간 스펙트로그램/i)).toBeInTheDocument();

      await userEvent.click(screen.getByRole('button', { name: /완료/i }));

      expect(appMocks.liveCleanups[0]).toHaveBeenCalled();
      expect(recording.stoppedTrack.stop).toHaveBeenCalled();
    } finally {
      recording.restore();
    }
  });

  it('records a skipped live window as diagnostics when ten requests are already in flight', async () => {
    const recording = installRecordingEnvironment();
    appMocks.analyzeLiveChunk.mockImplementation(() => new Promise(() => undefined));

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        for (let index = 0; index < 11; index += 1) {
          appMocks.liveWindowCallbacks[0](liveWindow(index, index + 2));
        }
      });

      expect(appMocks.analyzeLiveChunk).toHaveBeenCalledTimes(10);
      expect(await screen.findByText('SKIP 11')).toBeInTheDocument();
      expect(await screen.findByRole('img', { name: /청크 #11.*SKIP/i })).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('shows a pending live chunk row before a deferred response resolves', async () => {
    const recording = installRecordingEnvironment();
    const request = deferredLiveResponse(1);
    appMocks.analyzeLiveChunk.mockReturnValue(request.promise);

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });

      expect(await screen.findByRole('img', { name: /청크 #1.*PENDING/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /CSV 다운로드/i })).not.toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('records a failure diagnostic with the rejection reason when a live request rejects', async () => {
    const recording = installRecordingEnvironment();
    appMocks.analyzeLiveChunk.mockRejectedValue(new Error('Cochl live chunk analysis failed.'));

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });

      expect(await screen.findByText('FAIL 1: Cochl live chunk analysis failed.')).toBeInTheDocument();
      expect(await screen.findByRole('img', { name: /청크 #1.*FAIL.*Cochl live chunk analysis failed/i })).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('shows measured latency for detected live events', async () => {
    const recording = installRecordingEnvironment();
    const request = deferredLiveResponse(1);
    let nowMs = 100_000;
    vi.spyOn(Date, 'now').mockImplementation(() => nowMs);
    const infoSpy = vi.spyOn(console, 'info').mockImplementation(() => undefined);
    appMocks.analyzeLiveChunk.mockReturnValue(request.promise);

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      nowMs = 102_050;
      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });

      nowMs = 103_400;
      await act(async () => {
        request.resolve({
          sequence_id: 1,
          window_start_sec: 0,
          window_end_sec: 2,
          sound_events: [
            {
              start_time_sec: 0.5,
              end_time_sec: 0.8,
              label: 'Cough',
              confidence: 0.93,
            },
          ],
          processing_time_ms: 900,
        });
      });

      expect(await screen.findByRole('img', { name: /Cough.*00:00.*93%/i })).toBeInTheDocument();
      expect(await screen.findByRole('img', { name: /청크 #1.*DETECTED.*요청 1\.35초.*서버 0\.90초.*윈도우 종료 후 1\.40초/i })).toBeInTheDocument();
      expect(await screen.findAllByText('Cough 93%')).not.toHaveLength(0);
      const latency = await screen.findByLabelText(/최근 실시간 감지 지연/i);
      expect(latency).toHaveTextContent('최근 지연: Cough 2.90초 · 요청 1.35초');
      expect(latency).toHaveAttribute(
        'title',
        expect.stringContaining('윈도우 종료->마커 1.40초'),
      );
      expect(infoSpy).toHaveBeenCalledWith(
        '[Cochl.Sense Cloud Live Demo latency]',
        expect.objectContaining({
          backendMs: 900,
          eventDelayMs: 2900,
          requestMs: 1350,
          windowCallbackDelayMs: 1350,
          windowEndDelayMs: 1400,
        }),
      );
    } finally {
      recording.restore();
    }
  });

  it('renders second labels for a resolved slow live chunk delay while recording', async () => {
    const recording = installRecordingEnvironment();
    const request = deferredLiveResponse(1);
    let nowMs = 100_000;
    vi.spyOn(Date, 'now').mockImplementation(() => nowMs);
    appMocks.analyzeLiveChunk.mockReturnValue(request.promise);

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      nowMs = 102_050;
      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });

      nowMs = 106_300;
      await act(async () => {
        request.resolve(liveResponse(1, []));
      });

      expect(await screen.findByRole('img', { name: /청크 #1.*EMPTY.*윈도우 종료 후 4\.30초/i })).toBeInTheDocument();
      const lane = screen.getByLabelText(/실시간 청크 요청 상태/i);
      expect(within(lane).getByText('1s')).toBeInTheDocument();
      expect(within(lane).getByText('2s')).toBeInTheDocument();
      expect(within(lane).getByText('3s')).toBeInTheDocument();
      expect(within(lane).getByText('4s')).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /CSV 다운로드/i })).not.toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('ignores late live responses after a recording is discarded', async () => {
    const recording = installRecordingEnvironment();
    const oldRequest = deferredLiveResponse(1);
    const newRequests = Array.from({ length: 10 }, (_value, index) => deferredLiveResponse(index + 1));
    appMocks.analyzeLiveChunk.mockReturnValueOnce(oldRequest.promise);
    newRequests.forEach((request) => {
      appMocks.analyzeLiveChunk.mockReturnValueOnce(request.promise);
    });

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });
      await userEvent.click(screen.getByRole('button', { name: /폐기/i }));
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        for (let index = 0; index < 10; index += 1) {
          appMocks.liveWindowCallbacks[1](liveWindow(index, index + 2));
        }
      });
      await act(async () => {
        oldRequest.resolve(liveResponse(1, [{ label: 'Speech', confidence: 0.9 }]));
      });
      await act(async () => {
        appMocks.liveWindowCallbacks[1](liveWindow(10, 12));
      });

      expect(appMocks.analyzeLiveChunk).toHaveBeenCalledTimes(11);
      expect(screen.queryByText('Speech 90%')).not.toBeInTheDocument();
      expect(screen.queryByRole('img', { name: /청크 #1.*DETECTED.*Speech/i })).not.toBeInTheDocument();
      expect(await screen.findByText('SKIP 11')).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('ignores stale live window callbacks after a new recording starts', async () => {
    const recording = installRecordingEnvironment();

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);
      const firstSessionWindow = appMocks.liveWindowCallbacks[0];

      await userEvent.click(screen.getByRole('button', { name: /폐기/i }));
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);
      appMocks.analyzeLiveChunk.mockClear();

      await act(async () => {
        firstSessionWindow(liveWindow(0, 2));
      });

      expect(appMocks.analyzeLiveChunk).not.toHaveBeenCalled();
      expect(screen.queryByText('Speech 90%')).not.toBeInTheDocument();
      expect(screen.queryByText(/^SKIP \d+/)).not.toBeInTheDocument();
      expect(screen.queryByText(/^FAIL \d+/)).not.toBeInTheDocument();
      expect(screen.queryByText(/^EMPTY \d+/)).not.toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('records an empty live response as diagnostics instead of an animated bubble', async () => {
    const recording = installRecordingEnvironment();
    appMocks.analyzeLiveChunk.mockResolvedValue(liveResponse(1, []));

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });
      expect(await screen.findByText('EMPTY 1')).toBeInTheDocument();
      expect(await screen.findByRole('img', { name: /청크 #1.*EMPTY/i })).toBeInTheDocument();
      expect(screen.getByText(/EMPTY 1/)).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('keeps the completed session open for late live responses after 완료', async () => {
    const recording = installRecordingEnvironment();
    const request = deferredLiveResponse(1);
    appMocks.analyzeLiveChunk.mockReturnValue(request.promise);

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });
      expect(await screen.findByRole('img', { name: /청크 #1.*PENDING/i })).toBeInTheDocument();

      await userEvent.click(screen.getByRole('button', { name: /완료/i }));
      await recording.finishRecording();
      expect(await screen.findByText(/업로드 준비/i)).toBeInTheDocument();

      await act(async () => {
        request.resolve(liveResponse(1, [{ label: 'Speech', confidence: 0.9 }]));
      });

      expect(await screen.findByRole('img', { name: /청크 #1.*DETECTED.*Speech 90%/i })).toBeInTheDocument();
      expect(screen.getByRole('button', { name: /CSV 다운로드/i })).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('shows CSV download only after completion and exports one row per chunk', async () => {
    const recording = installRecordingEnvironment();
    let capturedBlob: Blob | null = null;
    let clickedAnchor: HTMLAnchorElement | null = null;
    vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => {
      if (blob instanceof Blob) {
        capturedBlob = blob;
      }
      return 'blob:live-chunks';
    });
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const clickSpy = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clickedAnchor = this;
    });
    appMocks.analyzeLiveChunk.mockResolvedValue(liveResponse(1, []));

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });
      expect(await screen.findByRole('img', { name: /청크 #1.*EMPTY/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /CSV 다운로드/i })).not.toBeInTheDocument();

      await userEvent.click(screen.getByRole('button', { name: /완료/i }));
      await recording.finishRecording();
      const button = await screen.findByRole('button', { name: /CSV 다운로드/i });
      vi.useFakeTimers();
      fireEvent.click(button);

      expect(clickSpy).toHaveBeenCalledTimes(1);
      expect(clickedAnchor).toHaveAttribute('href', 'blob:live-chunks');
      expect(clickedAnchor).toHaveAttribute('target', '_blank');
      expect(clickedAnchor).toHaveAttribute('rel', 'noopener');
      expect(revokeSpy).not.toHaveBeenCalled();
      vi.advanceTimersByTime(59_999);
      expect(revokeSpy).not.toHaveBeenCalled();
      vi.advanceTimersByTime(1);
      expect(revokeSpy).toHaveBeenCalledWith('blob:live-chunks');
      vi.useRealTimers();
      if (!capturedBlob) {
        throw new Error('Expected CSV blob');
      }
      expect(capturedBlob.type).toBe('text/csv;charset=utf-8');
      const csv = await blobText(capturedBlob);
      expect(csv.split('\n')).toHaveLength(2);
      expect(csv).toContain('session_id,sequence_id,status');
      expect(csv).toContain(',1,EMPTY,0,2,');
    } finally {
      vi.useRealTimers();
      recording.restore();
    }
  });

  it('opens completed chunk CSV in a new tab when direct downloads are unavailable', async () => {
    const recording = installRecordingEnvironment();
    let capturedBlob: Blob | null = null;
    vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => {
      if (blob instanceof Blob) {
        capturedBlob = blob;
      }
      return 'blob:open-live-chunks';
    });
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const openSpy = vi.spyOn(window, 'open').mockImplementation(() => null);
    appMocks.analyzeLiveChunk.mockResolvedValue(liveResponse(1, []));

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });
      expect(await screen.findByRole('img', { name: /청크 #1.*EMPTY/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /CSV 열기/i })).not.toBeInTheDocument();

      await userEvent.click(screen.getByRole('button', { name: /완료/i }));
      await recording.finishRecording();
      const button = await screen.findByRole('button', { name: /CSV 열기/i });
      vi.useFakeTimers();
      fireEvent.click(button);

      expect(openSpy).toHaveBeenCalledWith('blob:open-live-chunks', '_blank', 'noopener');
      expect(revokeSpy).not.toHaveBeenCalled();
      vi.advanceTimersByTime(60_000);
      expect(revokeSpy).toHaveBeenCalledWith('blob:open-live-chunks');
      vi.useRealTimers();
      if (!capturedBlob) {
        throw new Error('Expected CSV blob');
      }
      const csv = await blobText(capturedBlob);
      expect(csv).toContain(',1,EMPTY,0,2,');
    } finally {
      vi.useRealTimers();
      recording.restore();
    }
  });

  it('keeps out-of-order live chunk responses in request rows and CSV', async () => {
    const recording = installRecordingEnvironment();
    const firstRequest = deferredLiveResponse(1);
    const secondRequest = deferredLiveResponse(2);
    let capturedBlob: Blob | null = null;
    vi.spyOn(URL, 'createObjectURL').mockImplementation((blob) => {
      if (blob instanceof Blob) {
        capturedBlob = blob;
      }
      return 'blob:out-of-order-live-chunks';
    });
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    appMocks.analyzeLiveChunk.mockReturnValueOnce(firstRequest.promise);
    appMocks.analyzeLiveChunk.mockReturnValueOnce(secondRequest.promise);

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
        appMocks.liveWindowCallbacks[0](liveWindow(1, 3));
      });
      await act(async () => {
        secondRequest.resolve(liveResponse(2, []));
      });
      await act(async () => {
        firstRequest.resolve(liveResponse(1, [{ label: 'Cough', confidence: 0.88 }]));
      });

      expect(await screen.findByRole('img', { name: /청크 #1.*DETECTED.*Cough 88%/i })).toBeInTheDocument();
      expect(await screen.findByRole('img', { name: /청크 #2.*EMPTY/i })).toBeInTheDocument();

      await userEvent.click(screen.getByRole('button', { name: /완료/i }));
      await recording.finishRecording();
      await userEvent.click(await screen.findByRole('button', { name: /CSV 다운로드/i }));

      if (!capturedBlob) {
        throw new Error('Expected CSV blob');
      }
      const lines = (await blobText(capturedBlob)).split('\n');
      expect(lines).toHaveLength(3);
      expect(lines[1]).toContain(',1,DETECTED,0,2,');
      expect(lines[2]).toContain(',2,EMPTY,1,3,');
    } finally {
      recording.restore();
    }
  });

  it('keeps chunk rows visible after an empty recording error without offering CSV', async () => {
    const recording = installRecordingEnvironment();
    const createObjectUrlSpy = vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:should-not-happen');
    appMocks.analyzeLiveChunk.mockResolvedValue(liveResponse(1, []));

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });
      expect(await screen.findByRole('img', { name: /청크 #1.*EMPTY/i })).toBeInTheDocument();

      await userEvent.click(screen.getByRole('button', { name: /완료/i }));
      await recording.finishRecording(new Blob([], { type: 'audio/webm' }));

      expect(await screen.findByText(/녹음된 오디오가 비어 있습니다/i)).toBeInTheDocument();
      expect(screen.getByRole('img', { name: /청크 #1.*EMPTY/i })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /CSV 다운로드/i })).not.toBeInTheDocument();
      expect(createObjectUrlSpy).not.toHaveBeenCalled();
    } finally {
      recording.restore();
    }
  });

  it('keeps recording active and shows FAIL 0 diagnostics when live capture setup fails', async () => {
    const recording = installRecordingEnvironment();
    appMocks.createLiveAudioCapture.mockImplementation(() => {
      throw new Error('Web Audio API is not supported.');
    });

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));

      expect(await screen.findByText(/녹음 중/i)).toBeInTheDocument();
      expect(await screen.findByText('FAIL 0: Web Audio API is not supported.')).toBeInTheDocument();
      expect(screen.queryByText(/확인 필요/i)).not.toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('ignores stale spectrogram frame callbacks after a new recording starts', async () => {
    const recording = installRecordingEnvironment();

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);
      const firstSessionSpectrogram = appMocks.liveSpectrogramCallbacks[0];

      await userEvent.click(screen.getByRole('button', { name: /폐기/i }));
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        firstSessionSpectrogram({ timestampSec: 45, magnitudes: [0.1, 0.7] });
      });

      expect(screen.getByRole('slider', { name: /실시간 타임라인 위치/i })).toHaveAttribute('max', '0');
    } finally {
      recording.restore();
    }
  });

  it('renders waveform controls for detected sound events', () => {
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/ogg' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.ogg', {
      type: 'audio/ogg',
    });

    render(<AnalysisPanel analysis={analysis} recordingFile={file} />);

    expect(screen.getByLabelText(/녹음 파형/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Speech 구간 재생/i })).toBeInTheDocument();
  });

  it('shows a playback error when the browser rejects segment playback', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(new Error('unsupported'));
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });

    render(<AnalysisPanel analysis={analysis} recordingFile={file} />);
    await userEvent.click(await screen.findByRole('button', { name: /Speech 구간 재생/i }));

    expect(await screen.findByText(/녹음 파일을 재생하지 못했습니다/i)).toBeInTheDocument();
  });

  it('does not show a playback error when rejected play has already started media', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(new Error('interrupted'));
    vi.spyOn(HTMLMediaElement.prototype, 'paused', 'get').mockReturnValue(false);
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });

    render(<AnalysisPanel analysis={analysis} recordingFile={file} />);
    await userEvent.click(await screen.findByRole('button', { name: /Speech 구간 재생/i }));

    await waitFor(() => {
      expect(screen.queryByText(/녹음 파일을 재생하지 못했습니다/i)).not.toBeInTheDocument();
    });
  });

  it('clears a playback error when the native audio control starts playing', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, 'play').mockRejectedValue(new Error('unsupported'));
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });

    render(<AnalysisPanel analysis={analysis} recordingFile={file} />);
    await userEvent.click(await screen.findByRole('button', { name: /Speech 구간 재생/i }));
    expect(await screen.findByText(/녹음 파일을 재생하지 못했습니다/i)).toBeInTheDocument();

    fireEvent.play(screen.getByLabelText(/브라우저 오디오 컨트롤/i));

    await waitFor(() => {
      expect(screen.queryByText(/녹음 파일을 재생하지 못했습니다/i)).not.toBeInTheDocument();
    });
  });

  it('plays a detected segment through the decoded audio buffer', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    vi.spyOn(HTMLMediaElement.prototype, 'pause').mockImplementation(() => undefined);
    const starts: Array<{ when: number; offset: number; duration: number | undefined }> = [];
    const decodedBuffer = {
      duration: 10,
      length: 4,
      numberOfChannels: 1,
      sampleRate: 16000,
      getChannelData: () => Float32Array.from([0, 0.4, -0.6, 0.1]),
    } as AudioBuffer;
    const source = {
      buffer: null as AudioBuffer | null,
      connect: vi.fn(),
      disconnect: vi.fn(),
      start: vi.fn((when: number, offset: number, duration?: number) => {
        starts.push({ when, offset, duration });
      }),
      stop: vi.fn(),
      onended: null as (() => void) | null,
    };
    const decodeAudioData = vi.fn(async () => decodedBuffer);
    const createBufferSource = vi.fn(() => source);
    class FakeAudioContext {
      destination = {};
      state = 'running';
      close = vi.fn(async () => undefined);
      createBufferSource = createBufferSource;
      decodeAudioData = decodeAudioData;
      resume = vi.fn(async () => undefined);
    }
    const originalAudioContext = window.AudioContext;
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(async () => new ArrayBuffer(3)),
    });

    try {
      render(<AnalysisPanel analysis={analysis} recordingFile={file} />);
      await waitFor(() => expect(decodeAudioData).toHaveBeenCalled());
      await userEvent.click(await screen.findByRole('button', { name: /Speech 구간 재생/i }));

      expect(starts).toEqual([{ when: 0, offset: 1, duration: 2 }]);
    } finally {
      Object.defineProperty(window, 'AudioContext', {
        configurable: true,
        value: originalAudioContext,
      });
    }
  });

  it('plays the full recording through decoded audio controls', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const starts: Array<{ when: number; offset: number; duration: number | undefined }> = [];
    const decodedBuffer = {
      duration: 10,
      length: 4,
      numberOfChannels: 1,
      sampleRate: 16000,
      getChannelData: () => Float32Array.from([0, 0.4, -0.6, 0.1]),
    } as AudioBuffer;
    const source = {
      buffer: null as AudioBuffer | null,
      connect: vi.fn(),
      disconnect: vi.fn(),
      start: vi.fn((when: number, offset: number, duration?: number) => {
        starts.push({ when, offset, duration });
      }),
      stop: vi.fn(),
      onended: null as (() => void) | null,
    };
    class FakeAudioContext {
      currentTime = 0;
      destination = {};
      state = 'running';
      close = vi.fn(async () => undefined);
      createBufferSource = vi.fn(() => source);
      decodeAudioData = vi.fn(async () => decodedBuffer);
      resume = vi.fn(async () => undefined);
    }
    const originalAudioContext = window.AudioContext;
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(async () => new ArrayBuffer(3)),
    });

    try {
      render(<AnalysisPanel analysis={analysis} recordingFile={file} />);
      await userEvent.click(await screen.findByRole('button', { name: /녹음 재생/i }));

      expect(starts).toEqual([{ when: 0, offset: 0, duration: undefined }]);
      expect(screen.getByRole('button', { name: /녹음 일시정지/i })).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, 'AudioContext', {
        configurable: true,
        value: originalAudioContext,
      });
    }
  });

  it('starts full recording playback from the selected position', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const starts: Array<{ when: number; offset: number; duration: number | undefined }> = [];
    const decodedBuffer = {
      duration: 10,
      length: 4,
      numberOfChannels: 1,
      sampleRate: 16000,
      getChannelData: () => Float32Array.from([0, 0.4, -0.6, 0.1]),
    } as AudioBuffer;
    const source = {
      buffer: null as AudioBuffer | null,
      connect: vi.fn(),
      disconnect: vi.fn(),
      start: vi.fn((when: number, offset: number, duration?: number) => {
        starts.push({ when, offset, duration });
      }),
      stop: vi.fn(),
      onended: null as (() => void) | null,
    };
    class FakeAudioContext {
      currentTime = 0;
      destination = {};
      state = 'running';
      close = vi.fn(async () => undefined);
      createBufferSource = vi.fn(() => source);
      decodeAudioData = vi.fn(async () => decodedBuffer);
      resume = vi.fn(async () => undefined);
    }
    const originalAudioContext = window.AudioContext;
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(async () => new ArrayBuffer(3)),
    });

    try {
      render(<AnalysisPanel analysis={analysis} recordingFile={file} />);
      const slider = await screen.findByRole('slider', { name: /녹음 재생 위치/i });
      fireEvent.change(slider, { target: { value: '2.5' } });
      await userEvent.click(screen.getByRole('button', { name: /녹음 재생/i }));

      expect(starts).toEqual([{ when: 0, offset: 2.5, duration: undefined }]);
    } finally {
      Object.defineProperty(window, 'AudioContext', {
        configurable: true,
        value: originalAudioContext,
      });
    }
  });

  it('pauses full recording playback from the decoded audio controls', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const decodedBuffer = {
      duration: 10,
      length: 4,
      numberOfChannels: 1,
      sampleRate: 16000,
      getChannelData: () => Float32Array.from([0, 0.4, -0.6, 0.1]),
    } as AudioBuffer;
    const source = {
      buffer: null as AudioBuffer | null,
      connect: vi.fn(),
      disconnect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
      onended: null as (() => void) | null,
    };
    class FakeAudioContext {
      currentTime = 0;
      destination = {};
      state = 'running';
      close = vi.fn(async () => undefined);
      createBufferSource = vi.fn(() => source);
      decodeAudioData = vi.fn(async () => decodedBuffer);
      resume = vi.fn(async () => undefined);
    }
    const originalAudioContext = window.AudioContext;
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(async () => new ArrayBuffer(3)),
    });

    try {
      render(<AnalysisPanel analysis={analysis} recordingFile={file} />);
      await userEvent.click(await screen.findByRole('button', { name: /녹음 재생/i }));
      await userEvent.click(screen.getByRole('button', { name: /녹음 일시정지/i }));

      expect(source.stop).toHaveBeenCalled();
    } finally {
      Object.defineProperty(window, 'AudioContext', {
        configurable: true,
        value: originalAudioContext,
      });
    }
  });

  it('advances the full recording timer while playback is active', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    let animationFrame: FrameRequestCallback | null = null;
    vi.spyOn(window, 'requestAnimationFrame').mockImplementation((callback) => {
      animationFrame = callback;
      return 1;
    });
    vi.spyOn(window, 'cancelAnimationFrame').mockImplementation(() => undefined);
    const contexts: FakeAudioContext[] = [];
    const decodedBuffer = {
      duration: 10,
      length: 4,
      numberOfChannels: 1,
      sampleRate: 16000,
      getChannelData: () => Float32Array.from([0, 0.4, -0.6, 0.1]),
    } as AudioBuffer;
    const source = {
      buffer: null as AudioBuffer | null,
      connect: vi.fn(),
      disconnect: vi.fn(),
      start: vi.fn(),
      stop: vi.fn(),
      onended: null as (() => void) | null,
    };
    class FakeAudioContext {
      currentTime = 0;
      destination = {};
      state = 'running';
      close = vi.fn(async () => undefined);
      createBufferSource = vi.fn(() => source);
      decodeAudioData = vi.fn(async () => decodedBuffer);
      resume = vi.fn(async () => undefined);

      constructor() {
        contexts.push(this);
      }
    }
    const originalAudioContext = window.AudioContext;
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(async () => new ArrayBuffer(3)),
    });

    try {
      render(<AnalysisPanel analysis={analysis} recordingFile={file} />);
      await userEvent.click(await screen.findByRole('button', { name: /녹음 재생/i }));
      contexts[1].currentTime = 1.25;

      act(() => {
        animationFrame?.(1000);
      });

      expect(screen.getByText('00:01')).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, 'AudioContext', {
        configurable: true,
        value: originalAudioContext,
      });
    }
  });

  it('does not expose native audio controls while the decoded player is loading', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const decodedBuffer = {
      duration: 10,
      length: 4,
      numberOfChannels: 1,
      sampleRate: 16000,
      getChannelData: () => Float32Array.from([0, 0.4, -0.6, 0.1]),
    } as AudioBuffer;
    let resolveDecode: (value: AudioBuffer) => void = () => undefined;
    const decodeAudioData = vi.fn(
      () => new Promise<AudioBuffer>((resolve) => {
        resolveDecode = resolve;
      }),
    );
    class FakeAudioContext {
      destination = {};
      state = 'running';
      close = vi.fn(async () => undefined);
      createBufferSource = vi.fn();
      decodeAudioData = decodeAudioData;
      resume = vi.fn(async () => undefined);
    }
    const originalAudioContext = window.AudioContext;
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(async () => new ArrayBuffer(3)),
    });

    try {
      render(<AnalysisPanel analysis={analysis} recordingFile={file} />);

      await waitFor(() => expect(decodeAudioData).toHaveBeenCalled());
      expect(screen.queryByRole('button', { name: /녹음 재생/i })).not.toBeInTheDocument();
      expect(screen.queryByLabelText(/브라우저 오디오 컨트롤/i)).not.toBeInTheDocument();

      resolveDecode(decodedBuffer);

      expect(await screen.findByRole('button', { name: /녹음 재생/i })).toBeInTheDocument();
    } finally {
      Object.defineProperty(window, 'AudioContext', {
        configurable: true,
        value: originalAudioContext,
      });
    }
  });

  it('keeps segment buttons disabled until the decoded playback buffer is ready', async () => {
    vi.spyOn(URL, 'createObjectURL').mockReturnValue('blob:recording');
    vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => undefined);
    const decodedBuffer = {
      duration: 10,
      length: 4,
      numberOfChannels: 1,
      sampleRate: 16000,
      getChannelData: () => Float32Array.from([0, 0.4, -0.6, 0.1]),
    } as AudioBuffer;
    let resolveDecode: (value: AudioBuffer) => void = () => undefined;
    const decodeAudioData = vi.fn(
      () => new Promise<AudioBuffer>((resolve) => {
        resolveDecode = resolve;
      }),
    );
    class FakeAudioContext {
      destination = {};
      state = 'running';
      close = vi.fn(async () => undefined);
      createBufferSource = vi.fn();
      decodeAudioData = decodeAudioData;
      resume = vi.fn(async () => undefined);
    }
    const originalAudioContext = window.AudioContext;
    Object.defineProperty(window, 'AudioContext', {
      configurable: true,
      value: FakeAudioContext,
    });
    const analysis: AnalysisResponse = {
      recording: { duration_sec: 10, content_type: 'audio/mp4' },
      sound_events: [
        { start_time_sec: 1, end_time_sec: 3, label: 'Speech', confidence: 0.91 },
      ],
      speech_segments: [],
      audio_insights: null,
      usage: {
        audio_duration_sec: 10,
        services_used: ['sound_event_detection'],
        processing_time_ms: 20,
      },
    };
    const file = new File([new Uint8Array([1, 2, 3])], 'recording.m4a', {
      type: 'audio/mp4',
    });
    Object.defineProperty(file, 'arrayBuffer', {
      configurable: true,
      value: vi.fn(async () => new ArrayBuffer(3)),
    });

    try {
      render(<AnalysisPanel analysis={analysis} recordingFile={file} />);
      const segmentButton = await screen.findByRole('button', { name: /Speech 구간 재생/i });
      expect(segmentButton).toBeDisabled();

      resolveDecode(decodedBuffer);

      await waitFor(() => expect(segmentButton).not.toBeDisabled());
    } finally {
      Object.defineProperty(window, 'AudioContext', {
        configurable: true,
        value: originalAudioContext,
      });
    }
  });
});
