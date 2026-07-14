import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type {
  LiveAudioCaptureController,
  LiveAudioCaptureOptions,
  LiveAudioWindow,
  LiveSpectrogramFrame,
} from './liveAudio';

const appMocks = vi.hoisted(() => ({
  analyzeLiveChunk: vi.fn(),
  endLiveSession: vi.fn(),
  fetchRuntimeConfig: vi.fn(),
  fetchCollectedSessions: vi.fn(),
  deleteCollectedSession: vi.fn(),
  deleteCollectedSegment: vi.fn(),
  createLiveAudioCapture: vi.fn(),
  liveWindowCallbacks: [] as Array<(window: LiveAudioWindow) => void>,
  liveSpectrogramCallbacks: [] as Array<(frame: LiveSpectrogramFrame) => void>,
  liveAudioTimeCallbacks: [] as Array<(elapsedSec: number) => void>,
  liveAudioStateCallbacks: [] as Array<(state: AudioContextState) => void>,
  liveCleanups: [] as LiveAudioCaptureController[],
}));

vi.mock('./api', () => ({
  analyzeLiveChunk: appMocks.analyzeLiveChunk,
  endLiveSession: appMocks.endLiveSession,
  fetchRuntimeConfig: appMocks.fetchRuntimeConfig,
  fetchCollectedSessions: appMocks.fetchCollectedSessions,
  deleteCollectedSession: appMocks.deleteCollectedSession,
  deleteCollectedSegment: appMocks.deleteCollectedSegment,
  collectedFileUrl: (sessionId: string, filename: string) =>
    `/api/collected-sessions/${sessionId}/files/${filename}`,
}));

vi.mock('./liveAudio', async (importOriginal) => {
  const actual = await importOriginal<typeof import('./liveAudio')>();
  return {
    ...actual,
    createLiveAudioCapture: appMocks.createLiveAudioCapture,
  };
});

import App from './App';
import type { LiveChunkAnalysisResponse, SoundEvent } from './types';

const emptyCurationAggregates = {
  candidate_segment_count: 0,
  policy_selected_segment_count: 0,
  policy_selected_duration_sec: 0,
  policy_selected_audio_bytes: 0,
  rejected_repetitive_count: 0,
  rejected_class_balance_count: 0,
  rejected_session_budget_count: 0,
  invalid_audio_count: 0,
  write_error_count: 0,
  selected_label_segment_counts: {},
  selected_quota_duration_sec: {},
  policy_version: 1,
};

function mockLiveCaptureController(
  onWindow: (window: LiveAudioWindow) => void,
  options?: LiveAudioCaptureOptions,
  captureStartedAtMs = Date.now(),
): LiveAudioCaptureController {
  appMocks.liveWindowCallbacks.push(onWindow);
  if (options?.onSpectrogramFrame) {
    appMocks.liveSpectrogramCallbacks.push(options.onSpectrogramFrame);
  }
  if (options?.onAudioTimeUpdate) {
    appMocks.liveAudioTimeCallbacks.push(options.onAudioTimeUpdate);
  }
  if (options?.onStateChange) {
    appMocks.liveAudioStateCallbacks.push(options.onStateChange);
  }
  const cleanup = Object.assign(vi.fn(), {
    captureStartedAtMs,
    resume: vi.fn(async () => undefined),
    state: vi.fn(() => 'running' as AudioContextState),
  }) as LiveAudioCaptureController;
  appMocks.liveCleanups.push(cleanup);
  return cleanup;
}

beforeEach(() => {
  appMocks.analyzeLiveChunk.mockReset();
  appMocks.endLiveSession.mockReset();
  appMocks.fetchRuntimeConfig.mockReset();
  appMocks.createLiveAudioCapture.mockReset();
  appMocks.liveWindowCallbacks = [];
  appMocks.liveSpectrogramCallbacks = [];
  appMocks.liveAudioTimeCallbacks = [];
  appMocks.liveAudioStateCallbacks = [];
  appMocks.liveCleanups = [];
  appMocks.analyzeLiveChunk.mockResolvedValue({
    sequence_id: 1,
    window_start_sec: 0,
    window_end_sec: 2,
    sound_events: [],
    processing_time_ms: 1,
  });
  appMocks.endLiveSession.mockImplementation(async (sessionId: string) => ({
    ...emptyCurationAggregates,
    session_id: sessionId,
    session_name: null,
    started_at: null,
    ended_at: null,
    segment_count: 0,
    total_collected_duration_sec: 0,
    kept_chunk_count: 0,
    discarded_silent_chunk_count: 0,
    discarded_speech_chunk_count: 0,
    segments: [],
  }));
  appMocks.fetchRuntimeConfig.mockResolvedValue({
    collection_confidence_threshold: 0.5,
    api_token: 'test-token',
    capabilities: { gcs: true },
  });
  appMocks.fetchCollectedSessions.mockReset();
  appMocks.deleteCollectedSession.mockReset();
  appMocks.deleteCollectedSegment.mockReset();
  appMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [] });
  appMocks.deleteCollectedSession.mockResolvedValue(undefined);
  appMocks.deleteCollectedSegment.mockResolvedValue(undefined);
  vi.spyOn(window, 'confirm').mockReturnValue(true);
  appMocks.createLiveAudioCapture.mockImplementation(
    (_stream, onWindow, options?: LiveAudioCaptureOptions) => (
      mockLiveCaptureController(onWindow, options)
    ),
  );
});

function installRecordingEnvironment(
  trackSettings: MediaTrackSettings & { voiceIsolation?: boolean } = {},
) {
  const originalMediaDevices = navigator.mediaDevices;
  const originalMediaRecorder = window.MediaRecorder;
  const trackEvents = new EventTarget();
  const stoppedTrack = {
    stop: vi.fn(),
    getSettings: vi.fn(() => trackSettings),
    addEventListener: trackEvents.addEventListener.bind(trackEvents),
    removeEventListener: trackEvents.removeEventListener.bind(trackEvents),
  };
  const stream = {
    getTracks: () => [stoppedTrack],
    getAudioTracks: () => [stoppedTrack],
  } as unknown as MediaStream;
  const getUserMedia = vi.fn(async () => stream);
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
      getUserMedia,
    },
  });
  Object.defineProperty(window, 'MediaRecorder', {
    configurable: true,
    value: FakeMediaRecorder,
  });

  return {
    stoppedTrack,
    triggerTrackEnded() {
      trackEvents.dispatchEvent(new Event('ended'));
    },
    getUserMedia,
    get recorder() {
      return recorder;
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

  it('does not create a full MediaRecorder recording or expose whole-file analysis', async () => {
    const recording = installRecordingEnvironment();
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      expect(recording.recorder).toBeNull();
      expect(screen.queryByRole('button', { name: /^분석$/i })).not.toBeInTheDocument();
      expect(screen.getByText(/원본 저장 안 함 · 화면 이력 최근 60분/i)).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('requests mono 48 kHz input with automatic voice processing disabled', async () => {
    const recording = installRecordingEnvironment();
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      expect(recording.getUserMedia).toHaveBeenCalledWith({
        audio: {
          channelCount: { ideal: 1 },
          sampleRate: { ideal: 48_000 },
          echoCancellation: { exact: false },
          noiseSuppression: { exact: false },
          autoGainControl: { exact: false },
          voiceIsolation: { exact: false },
        },
      });
      expect(appMocks.createLiveAudioCapture).toHaveBeenCalledWith(
        expect.anything(),
        expect.any(Function),
        expect.objectContaining({
          sampleRate: 48_000,
          windowSec: 2,
          hopSec: 1,
        }),
      );
      expect(screen.getByText(/입력 48 kHz · 모노 · 음성 보정 끔/i)).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('uses the backend runtime confidence threshold for live display decisions', async () => {
    const recording = installRecordingEnvironment();
    appMocks.fetchRuntimeConfig.mockResolvedValue({
      collection_confidence_threshold: 0.73,
      api_token: 'test-token',
      capabilities: { gcs: true },
    });
    appMocks.analyzeLiveChunk.mockResolvedValue(
      liveResponse(1, [{ label: 'Quiet knock', confidence: 0.7 }]),
    );

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);
      expect(screen.getByText(/수집 기준 신뢰도 73%/i)).toBeInTheDocument();

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });

      expect(await screen.findByText('EMPTY 1')).toBeInTheDocument();
      expect(screen.queryByText(/Quiet knock 70%/i)).not.toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('ignores a stale initial runtime config that resolves after recording starts', async () => {
    const recording = installRecordingEnvironment();
    let resolveInitialConfig: (value: {
      collection_confidence_threshold: number;
      api_token: string;
      capabilities: { gcs: boolean };
    }) => void = () => undefined;
    const initialConfig = new Promise<{
      collection_confidence_threshold: number;
      api_token: string;
      capabilities: { gcs: boolean };
    }>((resolve) => {
      resolveInitialConfig = resolve;
    });
    appMocks.fetchRuntimeConfig
      .mockReturnValueOnce(initialConfig)
      .mockResolvedValueOnce({
        collection_confidence_threshold: 0.8,
        api_token: 'new-token',
        capabilities: { gcs: true },
      });

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/수집 기준 신뢰도 80%/i);

      await act(async () => {
        resolveInitialConfig({
          collection_confidence_threshold: 0.2,
          api_token: 'stale-token',
          capabilities: { gcs: false },
        });
        await initialConfig;
      });

      expect(screen.getByText(/수집 기준 신뢰도 80%/i)).toBeInTheDocument();
      expect(screen.queryByText(/수집 기준 신뢰도 20%/i)).not.toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('drives the visible recording clock from captured audio time', async () => {
    const recording = installRecordingEnvironment();
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveAudioTimeCallbacks[0](65.8);
      });

      expect(document.querySelector('.timer')).toHaveTextContent('01:05');
    } finally {
      recording.restore();
    }
  });

  it('offers an explicit resume action when the audio context is suspended', async () => {
    const recording = installRecordingEnvironment();
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      act(() => appMocks.liveAudioStateCallbacks[0]('suspended'));
      expect(await screen.findByText(/오디오 처리가 일시 중지되었습니다/i)).toBeInTheDocument();

      await userEvent.click(screen.getByRole('button', { name: /오디오 재개/i }));
      expect(appMocks.liveCleanups[0].resume).toHaveBeenCalledTimes(1);
      expect(screen.queryByText(/오디오 처리가 일시 중지되었습니다/i)).not.toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('finalizes confirmed data when the audio context closes unexpectedly', async () => {
    const recording = installRecordingEnvironment();
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      act(() => appMocks.liveAudioStateCallbacks[0]('closed'));

      expect(await screen.findByText(/오디오 처리 연결이 예기치 않게 종료되어/i)).toBeInTheDocument();
      await waitFor(() => expect(appMocks.endLiveSession).toHaveBeenCalledTimes(1));
      expect(await screen.findByText(/^완료$/, { selector: 'span.status-badge' })).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('does not enter recording when the audio context closes during capture startup', async () => {
    const recording = installRecordingEnvironment();
    const cleanup = Object.assign(vi.fn(), {
      captureStartedAtMs: Date.now(),
      resume: vi.fn(async () => undefined),
      state: vi.fn(() => 'closed' as AudioContextState),
    }) as LiveAudioCaptureController;
    appMocks.createLiveAudioCapture.mockImplementationOnce(async (
      _stream,
      _onWindow,
      options?: LiveAudioCaptureOptions,
    ) => {
      options?.onStateChange?.('closed');
      return cleanup;
    });

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));

      expect(await screen.findByText(/오디오 처리 연결이 예기치 않게 종료되어/i)).toBeInTheDocument();
      await waitFor(() => expect(appMocks.endLiveSession).toHaveBeenCalledTimes(1));
      expect(await screen.findByText(/^완료$/, { selector: 'span.status-badge' })).toBeInTheDocument();
      expect(cleanup).toHaveBeenCalledTimes(1);
    } finally {
      recording.restore();
    }
  });

  it('finalizes confirmed data when the microphone track ends unexpectedly', async () => {
    const recording = installRecordingEnvironment();
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      act(() => recording.triggerTrackEnded());

      expect(await screen.findByText(/마이크 연결이 끊겨 녹음을 중단했습니다/i)).toBeInTheDocument();
      await waitFor(() => expect(appMocks.endLiveSession).toHaveBeenCalledTimes(1));
      expect(await screen.findByText(/^완료$/, { selector: 'span.status-badge' })).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('detects a microphone track that ended before the watchdog was attached', async () => {
    const recording = installRecordingEnvironment();
    Object.defineProperty(recording.stoppedTrack, 'readyState', {
      configurable: true,
      value: 'ended',
    });
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));

      expect(await screen.findByText(/마이크 연결이 끊겨 녹음을 중단했습니다/i)).toBeInTheDocument();
      await waitFor(() => expect(appMocks.endLiveSession).toHaveBeenCalledTimes(1));
      expect(await screen.findByText(/^완료$/, { selector: 'span.status-badge' })).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('sends only one finalization when microphone and audio context end together', async () => {
    const recording = installRecordingEnvironment();
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      act(() => {
        // Simulate callbacks that were already queued before either cleanup
        // could detach the other listener.
        recording.triggerTrackEnded();
        appMocks.liveAudioStateCallbacks[0]('closed');
      });

      await waitFor(() => expect(appMocks.endLiveSession).toHaveBeenCalledTimes(1));
      expect(await screen.findByText(/^완료$/, { selector: 'span.status-badge' })).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('stops the stream when the browser reports automatic voice processing still enabled', async () => {
    const recording = installRecordingEnvironment({ noiseSuppression: true });
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));

      expect(await screen.findByText(/자동 음성 보정을 끌 수 없습니다: 노이즈 억제/i)).toBeInTheDocument();
      expect(recording.stoppedTrack.stop).toHaveBeenCalledTimes(1);
      expect(appMocks.createLiveAudioCapture).not.toHaveBeenCalled();
    } finally {
      recording.restore();
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

  it('notifies the native wrapper after the dashboard mounts', () => {
    const postMessage = vi.fn();
    const originalWebkit = (window as Window & { webkit?: unknown }).webkit;
    Object.defineProperty(window, 'webkit', {
      configurable: true,
      value: { messageHandlers: { appReady: { postMessage } } },
    });

    try {
      render(<App />);

      expect(postMessage).toHaveBeenCalledWith({ ready: true });
    } finally {
      Object.defineProperty(window, 'webkit', {
        configurable: true,
        value: originalWebkit,
      });
    }
  });

  it('shows a compact recording status and restores the native window after completion', async () => {
    const recording = installRecordingEnvironment();
    const postMessage = vi.fn();
    appMocks.analyzeLiveChunk.mockResolvedValueOnce(
      liveResponse(1, [{ label: 'Cough', confidence: 0.93 }]),
    );
    const originalWebkit = (window as Window & { webkit?: unknown }).webkit;
    Object.defineProperty(window, 'webkit', {
      configurable: true,
      value: { messageHandlers: { windowMode: { postMessage } } },
    });

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);
      postMessage.mockClear();

      await userEvent.click(screen.getByRole('button', { name: /작게 보기/i }));

      const compactRecorder = screen.getByLabelText(/작게 보기 녹음 상태/i);
      expect(compactRecorder).toBeVisible();
      expect(within(compactRecorder).getByLabelText(/녹음 시간 00:00/i)).toBeInTheDocument();
      expect(within(compactRecorder).getByText(/새 감지를 기다리는 중/i)).toBeInTheDocument();
      expect(within(compactRecorder).queryByText(/실시간 분석|수집/)).not.toBeInTheDocument();
      expect(screen.getByLabelText(/실시간 스펙트로그램/i)).not.toBeVisible();
      expect(postMessage).toHaveBeenCalledWith({ compact: true });

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });
      expect(await within(compactRecorder).findByText('Cough')).toBeInTheDocument();
      expect(within(compactRecorder).getByText('93%')).toBeInTheDocument();

      await userEvent.click(within(compactRecorder).getByRole('button', { name: /녹음 중지/i }));

      await waitFor(() => {
        expect(screen.queryByLabelText(/작게 보기 녹음 상태/i)).not.toBeInTheDocument();
      });
      expect(await screen.findByText(/^완료$/, { selector: 'span.status-badge' })).toBeInTheDocument();
      expect(postMessage).toHaveBeenCalledWith({ compact: false });
    } finally {
      Object.defineProperty(window, 'webkit', {
        configurable: true,
        value: originalWebkit,
      });
      recording.restore();
    }
  });

  it('moves focus into compact mode and restores it when expanded', async () => {
    const recording = installRecordingEnvironment();
    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      const compactButton = screen.getByRole('button', { name: /작게 보기/i });
      await userEvent.click(compactButton);
      const compactRecorder = screen.getByLabelText(/작게 보기 녹음 상태/i);
      await waitFor(() => expect(compactRecorder).toHaveFocus());

      await userEvent.click(within(compactRecorder).getByRole('button', { name: /전체 화면으로 보기/i }));
      await waitFor(() => expect(compactButton).toHaveFocus());
    } finally {
      recording.restore();
    }
  });

  it('keeps the newest sequence in compact recent-sound status when responses arrive out of order', async () => {
    const recording = installRecordingEnvironment();
    const older = deferredLiveResponse(1);
    const newer = deferredLiveResponse(2);
    appMocks.analyzeLiveChunk.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);
      await userEvent.click(screen.getByRole('button', { name: /작게 보기/i }));
      const compactRecorder = screen.getByLabelText(/작게 보기 녹음 상태/i);

      act(() => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
        appMocks.liveWindowCallbacks[0](liveWindow(1, 3));
      });
      await act(async () => {
        newer.resolve(liveResponse(2, [{ label: 'New sound', confidence: 0.92 }]));
      });
      expect(await within(compactRecorder).findByText('New sound')).toBeInTheDocument();

      await act(async () => {
        older.resolve(liveResponse(1, [{ label: 'Old sound', confidence: 0.99 }]));
      });
      expect(within(compactRecorder).getByText('New sound')).toBeInTheDocument();
      expect(within(compactRecorder).queryByText('Old sound')).not.toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('shows live collection counts from chunk collection statuses', async () => {
    const recording = installRecordingEnvironment();
    appMocks.analyzeLiveChunk
      .mockResolvedValueOnce({
        ...liveResponse(1, [{ label: 'Knock', confidence: 0.9 }]),
        collection_status: 'collected',
      })
      .mockResolvedValueOnce({
        ...liveResponse(2, []),
        collection_status: 'discarded_silent',
      })
      .mockResolvedValueOnce({
        ...liveResponse(3, [{ label: 'Speech', confidence: 0.9 }]),
        collection_status: 'discarded_speech',
      })
      .mockResolvedValueOnce({
        ...liveResponse(4, []),
        collection_status: 'discarded_late',
      });

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
        appMocks.liveWindowCallbacks[0](liveWindow(1, 3));
        appMocks.liveWindowCallbacks[0](liveWindow(2, 4));
        appMocks.liveWindowCallbacks[0](liveWindow(3, 5));
      });

      expect(
        await screen.findByText(/수집 후보 청크 1 · 무음 제외 1 · 음성 제외 1 · 종료 후 제외 1/),
      ).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('shows monotonic segment decisions while recording when responses arrive out of order', async () => {
    const recording = installRecordingEnvironment();
    const older = deferredLiveResponse(1);
    const newer = deferredLiveResponse(2);
    appMocks.analyzeLiveChunk.mockReturnValueOnce(older.promise).mockReturnValueOnce(newer.promise);

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      act(() => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
        appMocks.liveWindowCallbacks[0](liveWindow(1, 3));
      });
      await act(async () => {
        newer.resolve({
          ...liveResponse(2, []),
          curation_progress: {
            candidate_segment_count: 2,
            selected_segment_count: 1,
            rejected_repetitive_count: 1,
            rejected_class_balance_count: 0,
            rejected_session_budget_count: 0,
            invalid_audio_count: 0,
            write_error_count: 0,
          },
        });
      });
      expect(await screen.findByText(/세그먼트 판정 후보 2 · 선택 1 · 무시 1/)).toBeInTheDocument();

      await act(async () => {
        older.resolve({
          ...liveResponse(1, []),
          curation_progress: {
            candidate_segment_count: 1,
            selected_segment_count: 1,
            rejected_repetitive_count: 0,
            rejected_class_balance_count: 0,
            rejected_session_budget_count: 0,
            invalid_audio_count: 0,
            write_error_count: 0,
          },
        });
      });
      expect(screen.getByText(/세그먼트 판정 후보 2 · 선택 1 · 무시 1/)).toBeInTheDocument();
      expect(screen.getByText(/반복 1 · 균형 0 · 상한 0/)).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('ends the live session and shows the collection summary after 완료', async () => {
    const recording = installRecordingEnvironment();
    appMocks.endLiveSession.mockImplementation(async (sessionId: string) => ({
      ...emptyCurationAggregates,
      session_id: sessionId,
      candidate_segment_count: 5,
      policy_selected_segment_count: 2,
      rejected_repetitive_count: 2,
      rejected_session_budget_count: 1,
      segment_count: 2,
      total_collected_duration_sec: 17.5,
      kept_chunk_count: 12,
      discarded_silent_chunk_count: 4,
      discarded_speech_chunk_count: 3,
      segments: [
        {
          segment_index: 1,
          start_sec: 0,
          end_sec: 12,
          duration_sec: 12,
          event_count: 5,
          labels: ['Knock', 'Keyboard'],
          audio_filename: 'segment-001-0.000-12.000.wav',
          metadata_filename: 'segment-001-0.000-12.000.json',
        },
        {
          segment_index: 2,
          start_sec: 20,
          end_sec: 25.5,
          duration_sec: 5.5,
          event_count: 2,
          labels: ['Glass_break'],
          audio_filename: 'segment-002-20.000-25.500.wav',
          metadata_filename: 'segment-002-20.000-25.500.json',
        },
      ],
    }));

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await userEvent.click(screen.getByRole('button', { name: /완료/i }));

      expect(appMocks.endLiveSession).toHaveBeenCalledTimes(1);
      expect(String(appMocks.endLiveSession.mock.calls[0][0])).toMatch(/^session-/);
      expect(await screen.findByText(/데이터 수집 결과/)).toBeInTheDocument();
      expect(screen.getByText(/후보 5개 · 선택 2개 · 총 17.5초 저장/)).toBeInTheDocument();
      expect(screen.getByText(/반복 제외 2개/)).toBeInTheDocument();
      expect(screen.getByText(/Knock, Keyboard/)).toBeInTheDocument();
      expect(screen.getByText(/Glass_break/)).toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('shows an actionable error and retries when session finalization fails', async () => {
    const recording = installRecordingEnvironment();
    appMocks.endLiveSession.mockRejectedValueOnce(new Error('backend unavailable'));

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);
      await userEvent.click(screen.getByRole('button', { name: /^완료$/i }));

      expect(await screen.findByRole('alert')).toHaveTextContent('backend unavailable');
      expect(screen.getByRole('alert')).toHaveTextContent('수집된 데이터는 유지됩니다');
      expect(screen.getByRole('button', { name: /녹음 시작/i })).toBeDisabled();

      await userEvent.click(screen.getByRole('button', { name: /종료 다시 시도/i }));

      await waitFor(() => expect(appMocks.endLiveSession).toHaveBeenCalledTimes(2));
      expect(await screen.findByText(/^완료$/, { selector: 'span.status-badge' })).toBeInTheDocument();
      expect(screen.queryByRole('button', { name: /종료 다시 시도/i })).not.toBeInTheDocument();
    } finally {
      recording.restore();
    }
  });

  it('times out a stalled session finalization and keeps retry available', async () => {
    const recording = installRecordingEnvironment();
    appMocks.endLiveSession.mockImplementation(
      (_sessionId: string, _sessionName: string | undefined, signal: AbortSignal) => new Promise(
        (_resolve, reject) => signal.addEventListener('abort', () => reject(new DOMException(
          'aborted',
          'AbortError',
        )), { once: true }),
      ),
    );

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);
      vi.useFakeTimers();
      fireEvent.click(screen.getByRole('button', { name: /^완료$/i }));
      expect(screen.getByText(/수집 정리 중/, { selector: 'span.status-badge' })).toBeInTheDocument();

      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      expect(screen.getByRole('alert')).toHaveTextContent('30초 안에 완료되지 않았습니다');
      expect(screen.getByRole('button', { name: /종료 다시 시도/i })).toBeEnabled();
    } finally {
      vi.useRealTimers();
      recording.restore();
    }
  });

  it('applies the finalization deadline while waiting for a stalled live chunk', async () => {
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

      vi.useFakeTimers();
      fireEvent.click(screen.getByRole('button', { name: /^완료$/i }));
      await act(async () => {
        await vi.advanceTimersByTimeAsync(30_000);
      });

      expect(screen.getByRole('alert')).toHaveTextContent('30초 안에 완료되지 않았습니다');
      expect(appMocks.endLiveSession).not.toHaveBeenCalled();
      expect(appMocks.analyzeLiveChunk.mock.calls[0][0].signal).toHaveProperty('aborted', true);
    } finally {
      vi.useRealTimers();
      recording.restore();
    }
  });

  it('stops recording while keeping collected data and shows the final summary', async () => {
    const recording = installRecordingEnvironment();

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await userEvent.click(screen.getByRole('button', { name: /중단 \(수집분 유지\)/i }));

      await waitFor(() => expect(appMocks.endLiveSession).toHaveBeenCalledTimes(1));
      expect(await screen.findByText(/데이터 수집 결과/)).toBeInTheDocument();
      expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining('이미 수집된 데이터는 유지'));
    } finally {
      recording.restore();
    }
  });

  it('sends the session name with live chunks and the session end request', async () => {
    const recording = installRecordingEnvironment();

    try {
      render(<App />);
      await userEvent.type(screen.getByLabelText(/세션 이름/), '사무실 소음');
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });

      expect(appMocks.analyzeLiveChunk).toHaveBeenCalledWith(
        expect.objectContaining({ sessionName: '사무실 소음' }),
      );

      await userEvent.click(screen.getByRole('button', { name: /완료/i }));

      await waitFor(() => expect(appMocks.endLiveSession).toHaveBeenCalledTimes(1));
      expect(appMocks.endLiveSession).toHaveBeenCalledWith(
        expect.stringMatching(/^session-/),
        '사무실 소음',
        expect.any(AbortSignal),
      );
    } finally {
      recording.restore();
    }
  });

  it('waits for in-flight live chunks to drain before ending the session', async () => {
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

      await userEvent.click(screen.getByRole('button', { name: /완료/i }));

      expect(screen.getByText(/수집 데이터를 정리하고 있습니다/)).toBeInTheDocument();
      expect(appMocks.endLiveSession).not.toHaveBeenCalled();
      expect(screen.getByRole('button', { name: /녹음 시작/i })).toBeDisabled();
      expect(screen.getByRole('button', { name: /중단 \(수집분 유지\)/i })).toBeDisabled();

      await act(async () => {
        request.resolve(liveResponse(1, []));
      });

      await waitFor(() => expect(appMocks.endLiveSession).toHaveBeenCalledTimes(1));
      await waitFor(() =>
        expect(screen.queryByText(/수집 데이터를 정리하고 있습니다/)).not.toBeInTheDocument(),
      );
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

  it('promotes consecutive delivery failures to a top-level degraded warning and clears on recovery', async () => {
    const recording = installRecordingEnvironment();
    appMocks.analyzeLiveChunk.mockRejectedValue(new Error('temporary failure'));

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
        appMocks.liveWindowCallbacks[0](liveWindow(1, 3));
        appMocks.liveWindowCallbacks[0](liveWindow(2, 4));
      });

      expect(await screen.findByText(/최근 3개 청크가 연속으로/i)).toBeInTheDocument();
      expect(screen.getByText('전송 불안정', { selector: 'span.status-badge' })).toBeInTheDocument();

      appMocks.analyzeLiveChunk.mockResolvedValue(liveResponse(4, []));
      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(3, 5));
      });
      await waitFor(() => {
        expect(screen.queryByText(/최근 3개 청크가 연속으로/i)).not.toBeInTheDocument();
      });
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
    // Simulate one second of AudioContext/AudioWorklet setup. The visible
    // window-end latency must start at actual capture, not the button click.
    appMocks.createLiveAudioCapture.mockImplementationOnce(
      (_stream, onWindow, options?: LiveAudioCaptureOptions) => (
        mockLiveCaptureController(onWindow, options, 101_000)
      ),
    );

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));
      await screen.findByText(/녹음 중/i);

      nowMs = 103_050;
      await act(async () => {
        appMocks.liveWindowCallbacks[0](liveWindow(0, 2));
      });

      nowMs = 104_400;
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

      const marker = await screen.findByRole('button', { name: /Cough.*00:00.*93%/i });
      expect(marker).toBeInTheDocument();
      await userEvent.click(marker);
      expect(marker).toHaveAttribute('aria-pressed', 'true');
      expect(screen.getByText('Cough 00:00-00:00 93%', { selector: '.live-marker-detail' })).toBeInTheDocument();
      expect(await screen.findByRole('img', { name: /청크 #1.*DETECTED.*요청 1\.35초.*서버 0\.90초.*윈도우 종료 후 1\.40초/i })).toBeInTheDocument();
      expect(await screen.findAllByText('Cough 93%')).not.toHaveLength(0);
      const latency = await screen.findByLabelText(/최근 실시간 감지 지연/i);
      expect(latency).toHaveTextContent('최근 API 요청: 1.35초 · Cough 감지까지 2.90초');
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
      const oldSignal = appMocks.analyzeLiveChunk.mock.calls[0][0].signal as AbortSignal;
      await userEvent.click(screen.getByRole('button', { name: /중단 \(수집분 유지\)/i }));
      expect(oldSignal.aborted).toBe(true);
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

      await userEvent.click(screen.getByRole('button', { name: /중단 \(수집분 유지\)/i }));
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

  it('shows finalizing until late live responses drain and then completes', async () => {
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
      expect(screen.getByText(/수집 정리 중/, { selector: 'span.status-badge' })).toBeInTheDocument();

      await act(async () => {
        request.resolve(liveResponse(1, [{ label: 'Speech', confidence: 0.9 }]));
      });

      expect(await screen.findByRole('img', { name: /청크 #1.*DETECTED.*Speech 90%/i })).toBeInTheDocument();
      expect(await screen.findByText(/^완료$/, { selector: 'span.status-badge' })).toBeInTheDocument();
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

  it('fails the recording start when the only live capture path cannot be created', async () => {
    const recording = installRecordingEnvironment();
    appMocks.createLiveAudioCapture.mockImplementation(() => {
      throw new Error('Web Audio API is not supported.');
    });

    try {
      render(<App />);
      await userEvent.click(screen.getByRole('button', { name: /녹음 시작/i }));

      expect(await screen.findByText(/확인 필요/i)).toBeInTheDocument();
      expect(await screen.findByText('FAIL 0: Web Audio API is not supported.')).toBeInTheDocument();
      expect(recording.stoppedTrack.stop).toHaveBeenCalled();
      expect(screen.queryByRole('button', { name: /작게 보기/i })).not.toBeInTheDocument();
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

      await userEvent.click(screen.getByRole('button', { name: /중단 \(수집분 유지\)/i }));
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
});
