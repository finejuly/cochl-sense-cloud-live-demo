import { type ChangeEvent, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  AudioLines,
  Database,
  Maximize2,
  Mic,
  Minimize2,
  Pause,
  RefreshCw,
  Square,
} from 'lucide-react';
import { analyzeLiveChunk, endLiveSession, fetchRuntimeConfig } from './api';
import { CollectedSessionsPanel } from './CollectedSessionsPanel';
import { LiveSpectrogramPanel } from './LiveSpectrogramPanel';
import {
  createLiveAudioCapture,
  encodePcm16Wav,
  appendCompactedSpectrogramFrame,
  type LiveAudioCaptureController,
  type LiveAudioContextState,
  type LiveAudioWindow,
  type LiveSpectrogramFrame,
} from './liveAudio';
import {
  emptyLiveDiagnostics,
  liveTimelineEventsFromResponse,
  mergeLiveTimelineEvents,
  recordLiveDiagnostic,
  retainRecentLiveTimelineEvents,
  resolveLiveViewport,
  type LiveDiagnostics,
  type LiveTimelineEvent,
} from './liveTimeline';
import {
  completeLiveChunkRecord,
  createPendingLiveChunkRecord,
  createSkippedLiveChunkRecord,
  failLiveChunkRecord,
  hasPendingLiveChunkRecords,
  latestLiveChunkTailEndSec,
  liveChunkCsvFilename,
  liveChunkRecordsToCsv,
  markLiveChunkRequestStarted,
  retainRecentLiveChunkRecords,
  upsertLiveChunkRecord,
  type LiveChunkRecord,
} from './liveChunkRecords';
import type {
  LiveChunkCollectionStatus,
  LiveCurationProgress,
  LiveSessionEndResponse,
  RuntimeCapabilities,
  SoundEvent,
} from './types';
import { formatTime } from './time';
import './App.css';

type RecorderStatus = 'idle' | 'starting' | 'recording' | 'finalizing' | 'complete' | 'error';
type ExtendedMediaTrackConstraints = MediaTrackConstraints & {
  voiceIsolation?: ConstrainBoolean;
};
type ExtendedMediaTrackSettings = MediaTrackSettings & {
  voiceIsolation?: boolean;
};

const LIVE_WINDOW_SEC = 2;
const LIVE_HOP_SEC = 1;
const LIVE_AUDIO_SAMPLE_RATE_HZ = 48_000;
const LIVE_MICROPHONE_AUDIO_CONSTRAINTS: ExtendedMediaTrackConstraints = {
  channelCount: { ideal: 1 },
  sampleRate: { ideal: LIVE_AUDIO_SAMPLE_RATE_HZ },
  echoCancellation: { exact: false },
  noiseSuppression: { exact: false },
  autoGainControl: { exact: false },
  voiceIsolation: { exact: false },
};
const LIVE_MICROPHONE_CONSTRAINTS: MediaStreamConstraints = {
  audio: LIVE_MICROPHONE_AUDIO_CONSTRAINTS,
};
const LIVE_MAX_IN_FLIGHT = 10;
const DEFAULT_LIVE_CONFIDENCE_THRESHOLD = 0.5;
const LIVE_VIEWPORT_SEC = 20;
const LIVE_SPECTROGRAM_FPS = 12;
const LIVE_SPECTROGRAM_BINS = 64;
const LIVE_HISTORY_LIMIT = 6;
const LIVE_REQUEST_TIMEOUT_MS = 60_000;
const LIVE_SESSION_END_TIMEOUT_MS = 30_000;
const MAX_LIVE_SPECTROGRAM_FRAMES = 12_000;
const LIVE_UI_HISTORY_RETENTION_SEC = 60 * 60;
const COMPACT_SOUND_RECENCY_MS = 5_000;
const DEGRADED_CONSECUTIVE_ISSUE_THRESHOLD = 3;
const COLLECTION_SUMMARY_DISPLAY_LIMIT = 100;
const DEFAULT_RUNTIME_CAPABILITIES: RuntimeCapabilities = {
  gcs: true,
};

interface LiveCollectionCounts {
  collected: number;
  discardedSilent: number;
  discardedSpeech: number;
  discardedLate: number;
}

function emptyLiveCollectionCounts(): LiveCollectionCounts {
  return { collected: 0, discardedSilent: 0, discardedSpeech: 0, discardedLate: 0 };
}

function applyCollectionStatus(
  counts: LiveCollectionCounts,
  status: LiveChunkCollectionStatus,
): LiveCollectionCounts {
  if (status === 'collected') {
    return { ...counts, collected: counts.collected + 1 };
  }
  if (status === 'discarded_speech') {
    return { ...counts, discardedSpeech: counts.discardedSpeech + 1 };
  }
  if (status === 'discarded_late') {
    return { ...counts, discardedLate: counts.discardedLate + 1 };
  }
  return { ...counts, discardedSilent: counts.discardedSilent + 1 };
}

function mergeLiveCurationProgress(
  current: LiveCurationProgress | null,
  next: LiveCurationProgress,
): LiveCurationProgress {
  if (!current) {
    return next;
  }
  return {
    candidate_segment_count: Math.max(current.candidate_segment_count, next.candidate_segment_count),
    selected_segment_count: Math.max(current.selected_segment_count, next.selected_segment_count),
    rejected_repetitive_count: Math.max(
      current.rejected_repetitive_count,
      next.rejected_repetitive_count,
    ),
    rejected_class_balance_count: Math.max(
      current.rejected_class_balance_count,
      next.rejected_class_balance_count,
    ),
    rejected_session_budget_count: Math.max(
      current.rejected_session_budget_count,
      next.rejected_session_budget_count,
    ),
    invalid_audio_count: Math.max(current.invalid_audio_count, next.invalid_audio_count),
    write_error_count: Math.max(current.write_error_count, next.write_error_count),
  };
}

interface LiveLatencySample {
  label: string;
  sequenceId: number;
  confidence: number | null;
  eventStartSec: number;
  eventEndSec: number;
  eventDelayMs: number;
  requestMs: number;
  backendMs: number;
  backendTotalMs: number;
  windowEndDelayMs: number;
  windowCallbackDelayMs: number;
  captureClockDriftMs: number;
  encodeMs: number;
}

export default function App() {
  const [status, setStatus] = useState<RecorderStatus>('idle');
  const [elapsedSec, setElapsedSec] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const liveSpectrogramFramesRef = useRef<LiveSpectrogramFrame[]>([]);
  const [liveSpectrogramVersion, setLiveSpectrogramVersion] = useState(0);
  const [liveTimelineEvents, setLiveTimelineEvents] = useState<LiveTimelineEvent[]>([]);
  const [liveDiagnostics, setLiveDiagnostics] = useState<LiveDiagnostics>(() => emptyLiveDiagnostics());
  const [liveViewportStartSec, setLiveViewportStartSec] = useState(0);
  const [liveAutoFollow, setLiveAutoFollow] = useState(true);
  const [liveLatencySample, setLiveLatencySample] = useState<LiveLatencySample | null>(null);
  const [liveChunkRecords, setLiveChunkRecords] = useState<LiveChunkRecord[]>([]);
  const [liveChunkSnapshotMs, setLiveChunkSnapshotMs] = useState(() => Date.now());
  const [liveCollectionCounts, setLiveCollectionCounts] = useState<LiveCollectionCounts>(
    () => emptyLiveCollectionCounts(),
  );
  const [liveCurationProgress, setLiveCurationProgress] = useState<LiveCurationProgress | null>(null);
  const [collectionSummary, setCollectionSummary] = useState<LiveSessionEndResponse | null>(null);
  const [collectionFinalizing, setCollectionFinalizing] = useState(false);
  const [finalizationError, setFinalizationError] = useState<string | null>(null);
  const [captureIssue, setCaptureIssue] = useState<string | null>(null);
  const [liveAudioState, setLiveAudioState] = useState<LiveAudioContextState | 'unknown'>('unknown');
  const [liveConfidenceThreshold, setLiveConfidenceThreshold] = useState(
    DEFAULT_LIVE_CONFIDENCE_THRESHOLD,
  );
  const [runtimeCapabilities, setRuntimeCapabilities] = useState<RuntimeCapabilities>(
    DEFAULT_RUNTIME_CAPABILITIES,
  );
  const [collectedRefreshToken, setCollectedRefreshToken] = useState(0);
  const [sessionName, setSessionName] = useState('');
  const [compactMode, setCompactMode] = useState(false);
  const compactRecorderRef = useRef<HTMLElement | null>(null);
  const workspaceTitleRef = useRef<HTMLHeadingElement | null>(null);
  const compactModeButtonRef = useRef<HTMLButtonElement | null>(null);
  const focusBeforeCompactRef = useRef<HTMLElement | null>(null);
  const wasCompactRef = useRef(false);
  const sessionNameRef = useRef('');
  const streamRef = useRef<MediaStream | null>(null);
  const streamWatchdogCleanupRef = useRef<(() => void) | null>(null);
  const startedAtRef = useRef<number | null>(null);
  const recordingSessionRef = useRef(0);
  const liveCleanupRef = useRef<LiveAudioCaptureController | null>(null);
  const liveInFlightRef = useRef(0);
  const liveSequenceRef = useRef(0);
  // UI rows stay bounded to one hour, but this O(1)-updated map preserves one
  // compact final row per sequence for the post-session diagnostic CSV.
  const liveChunkLogRef = useRef<Map<number, LiveChunkRecord>>(new Map());
  const liveSessionTokenRef = useRef('');
  const livePendingRequestsRef = useRef<Map<string, Set<Promise<void>>>>(new Map());
  const liveAbortControllersRef = useRef<Map<string, Set<AbortController>>>(new Map());
  const finalizationAbortControllerRef = useRef<AbortController | null>(null);
  const finalizationInFlightTokenRef = useRef<string | null>(null);
  const liveConfidenceThresholdRef = useRef(DEFAULT_LIVE_CONFIDENCE_THRESHOLD);
  const runtimeConfigRequestSequenceRef = useRef(0);
  const hasPendingLiveChunks = useMemo(
    () => hasPendingLiveChunkRecords(liveChunkRecords),
    [liveChunkRecords],
  );

  useEffect(() => {
    let active = true;
    const requestSequence = runtimeConfigRequestSequenceRef.current + 1;
    runtimeConfigRequestSequenceRef.current = requestSequence;
    void fetchRuntimeConfig()
      .then((config) => {
        if (!active || requestSequence !== runtimeConfigRequestSequenceRef.current) {
          return;
        }
        liveConfidenceThresholdRef.current = config.collection_confidence_threshold;
        setLiveConfidenceThreshold(config.collection_confidence_threshold);
        setRuntimeCapabilities(config.capabilities);
      })
      .catch(() => {
        // Read-only rendering can use the documented default. A state-changing
        // action retries configuration loading before it touches local data.
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    notifyNativeAppReady();
  }, []);

  useEffect(() => {
    if (status !== 'recording' && !hasPendingLiveChunks) {
      return;
    }

    const interval = window.setInterval(() => {
      setLiveChunkSnapshotMs(Date.now());
    }, 1000);

    return () => window.clearInterval(interval);
  }, [hasPendingLiveChunks, status]);

  useEffect(() => {
    return () => {
      const liveSessionToken = liveSessionTokenRef.current;
      finalizationAbortControllerRef.current?.abort();
      stopLiveCapture({ clearTimeline: false, invalidateSession: true });
      stopTracks();
      if (finalizationInFlightTokenRef.current !== liveSessionToken) {
        void finalizeLiveSession(liveSessionToken, { showSummary: false });
      }
    };
  }, []);

  useEffect(() => {
    if (status !== 'recording' && compactMode) {
      setCompactMode(false);
    }
  }, [compactMode, status]);

  useEffect(() => {
    requestNativeWindowMode(compactMode);
    if (compactMode) {
      wasCompactRef.current = true;
      window.requestAnimationFrame(() => compactRecorderRef.current?.focus());
      return;
    }
    if (wasCompactRef.current) {
      wasCompactRef.current = false;
      window.requestAnimationFrame(() => {
        const previous = focusBeforeCompactRef.current;
        if (previous?.isConnected) {
          previous.focus();
        } else if (compactModeButtonRef.current) {
          compactModeButtonRef.current.focus();
        } else {
          workspaceTitleRef.current?.focus();
        }
      });
    }
  }, [compactMode]);

  useEffect(() => () => requestNativeWindowMode(false), []);

  const durationLabel = useMemo(() => formatTime(elapsedSec), [elapsedSec]);
  const compactDetectedSound = liveLatencySample
    && Math.max(0, elapsedSec * 1000 - liveLatencySample.eventEndSec * 1000) <= COMPACT_SOUND_RECENCY_MS
    ? liveLatencySample
    : null;
  const liveDurationSec = useMemo(() => {
    const latestFrameTime = liveSpectrogramFramesRef.current.at(-1)?.timestampSec ?? 0;
    const eventTimes = liveTimelineEvents.map((event) => event.endTimeSec);
    const chunkTailEndSec = latestLiveChunkTailEndSec(liveChunkRecords, liveChunkSnapshotMs);
    return Math.max(0, elapsedSec, chunkTailEndSec, latestFrameTime, ...eventTimes);
  }, [elapsedSec, liveChunkRecords, liveChunkSnapshotMs, liveSpectrogramVersion, liveTimelineEvents]);
  const consecutiveDeliveryIssues = useMemo(
    () => countConsecutiveDeliveryIssues(liveChunkRecords),
    [liveChunkRecords],
  );
  const liveDegraded = status === 'recording'
    && consecutiveDeliveryIssues >= DEGRADED_CONSECUTIVE_ISSUE_THRESHOLD;
  const liveViewportState = useMemo(
    () => resolveLiveViewport(liveDurationSec, liveViewportStartSec, liveAutoFollow, LIVE_VIEWPORT_SEC),
    [liveAutoFollow, liveDurationSec, liveViewportStartSec],
  );

  async function startRecording() {
    setError(null);
    setFinalizationError(null);
    setCaptureIssue(null);

    if (!navigator.mediaDevices?.getUserMedia) {
      setStatus('error');
      setError('마이크 녹음을 지원하지 않는 브라우저입니다.');
      return;
    }

    setStatus('starting');
    try {
      const requestSequence = runtimeConfigRequestSequenceRef.current + 1;
      runtimeConfigRequestSequenceRef.current = requestSequence;
      const runtimeConfig = await fetchRuntimeConfig({ force: true });
      if (requestSequence === runtimeConfigRequestSequenceRef.current) {
        liveConfidenceThresholdRef.current = runtimeConfig.collection_confidence_threshold;
        setLiveConfidenceThreshold(runtimeConfig.collection_confidence_threshold);
        setRuntimeCapabilities(runtimeConfig.capabilities);
      }
      const stream = await navigator.mediaDevices.getUserMedia(LIVE_MICROPHONE_CONSTRAINTS);
      streamRef.current = stream;
      assertAutomaticVoiceProcessingDisabled(stream);
      const sessionId = recordingSessionRef.current + 1;

      recordingSessionRef.current = sessionId;
      setElapsedSec(0);
      const captureStarted = await startLiveCapture(stream, sessionId);
      if (!captureStarted) {
        return;
      }
      const streamAlive = installStreamWatchdog(stream, liveSessionTokenRef.current);
      if (!streamAlive) {
        return;
      }
      setStatus('recording');
    } catch (err) {
      setStatus('error');
      setError(err instanceof Error ? err.message : '마이크 권한을 가져오지 못했습니다.');
      stopLiveCapture({ clearTimeline: false, invalidateSession: true });
      stopTracks();
      startedAtRef.current = null;
    }
  }

  function completeRecording() {
    if (status !== 'recording') {
      return;
    }
    const liveSessionToken = liveSessionTokenRef.current;
    stopLiveCapture({ clearTimeline: false, invalidateSession: false });
    stopTracks();
    setStatus('finalizing');
    void finalizeLiveSession(liveSessionToken, { showSummary: true });
  }

  function stopRecordingAndKeepCollected() {
    if (status !== 'recording') {
      return;
    }
    if (!window.confirm(
      '녹음을 지금 중단할까요? 이미 수집된 데이터는 유지되며, 아직 분석 중인 청크는 취소됩니다.',
    )) {
      return;
    }
    const liveSessionToken = liveSessionTokenRef.current;
    abortLiveRequests(liveSessionToken);
    stopLiveCapture({ clearTimeline: false, invalidateSession: false });
    stopTracks();
    setStatus('finalizing');
    void finalizeLiveSession(liveSessionToken, { showSummary: true, waitForDrain: false });
  }

  function retryFinalization() {
    const liveSessionToken = liveSessionTokenRef.current;
    if (!finalizationError || !liveSessionToken.startsWith('session-')) {
      return;
    }
    setStatus('finalizing');
    void finalizeLiveSession(liveSessionToken, { showSummary: true });
  }

  async function startLiveCapture(
    stream: MediaStream,
    recordingSessionId: number,
  ): Promise<boolean> {
    const liveSessionToken = `session-${Date.now()}-${recordingSessionId}`;
    liveSessionTokenRef.current = liveSessionToken;
    liveInFlightRef.current = 0;
    liveSequenceRef.current = 0;
    clearLiveTimelineState();
    setLiveLatencySample(null);

    try {
      const cleanup = await createLiveAudioCapture(
        stream,
        (window) => handleLiveWindow(window, liveSessionToken),
        {
          sampleRate: LIVE_AUDIO_SAMPLE_RATE_HZ,
          windowSec: LIVE_WINDOW_SEC,
          hopSec: LIVE_HOP_SEC,
          onSpectrogramFrame: (frame) => handleLiveSpectrogramFrame(frame, liveSessionToken),
          spectrogramFps: LIVE_SPECTROGRAM_FPS,
          spectrogramBins: LIVE_SPECTROGRAM_BINS,
          onAudioTimeUpdate: (audioTimeSec) => {
            if (liveSessionToken === liveSessionTokenRef.current) {
              updateAudioTimeline(audioTimeSec);
            }
          },
          onStateChange: (audioState) => {
            if (liveSessionToken === liveSessionTokenRef.current) {
              handleLiveAudioStateChange(audioState, liveSessionToken);
            }
          },
        },
      );
      if (
        liveSessionToken !== liveSessionTokenRef.current
        || streamRef.current !== stream
      ) {
        cleanup();
        return false;
      }
      startedAtRef.current = cleanup.captureStartedAtMs;
      liveCleanupRef.current = cleanup;
      return true;
    } catch (err) {
      liveCleanupRef.current = null;
      setLiveDiagnostics((current) => recordLiveDiagnostic(current, 'failure', 0, errorMessage(err)));
      throw err;
    }
  }

  function stopLiveCapture({
    clearTimeline,
    invalidateSession,
  }: {
    clearTimeline: boolean;
    invalidateSession: boolean;
  }) {
    if (invalidateSession) {
      const activeToken = liveSessionTokenRef.current;
      abortLiveRequests(activeToken);
      liveSessionTokenRef.current = `inactive-${Date.now()}-${recordingSessionRef.current}`;
      liveInFlightRef.current = 0;
    }
    liveCleanupRef.current?.();
    liveCleanupRef.current = null;
    setLiveAudioState('unknown');
    if (clearTimeline) {
      liveSequenceRef.current = 0;
      clearLiveTimelineState();
      setLiveLatencySample(null);
    }
  }

  function clearLiveTimelineState() {
    liveSpectrogramFramesRef.current = [];
    setLiveSpectrogramVersion((version) => version + 1);
    setLiveTimelineEvents([]);
    setLiveDiagnostics(emptyLiveDiagnostics());
    setLiveViewportStartSec(0);
    setLiveAutoFollow(true);
    setLiveChunkRecords([]);
    liveChunkLogRef.current.clear();
    setLiveChunkSnapshotMs(Date.now());
    setLiveCollectionCounts(emptyLiveCollectionCounts());
    setLiveCurationProgress(null);
    setCollectionSummary(null);
  }

  function updateAudioTimeline(audioTimeSec: number) {
    const nextElapsedSec = Math.max(0, Math.floor(audioTimeSec));
    setElapsedSec((current) => Math.max(current, nextElapsedSec));
  }

  function handleLiveAudioStateChange(
    audioState: LiveAudioContextState,
    liveSessionToken: string,
  ) {
    setLiveAudioState(audioState);
    if (audioState === 'running') {
      setCaptureIssue(null);
      return;
    }
    if (audioState === 'suspended') {
      setCaptureIssue('오디오 처리가 일시 중지되었습니다. 녹음을 계속하려면 오디오 재개를 눌러 주세요.');
      return;
    }
    if (audioState === 'closed') {
      setCaptureIssue('오디오 처리 연결이 예기치 않게 종료되어 녹음을 중단했습니다. 수집 데이터를 정리합니다.');
      stopLiveCapture({ clearTimeline: false, invalidateSession: false });
      stopTracks();
      setStatus('finalizing');
      void finalizeLiveSession(liveSessionToken, { showSummary: true });
      return;
    }
    setCaptureIssue(`오디오 처리가 중단되었습니다 (${audioState}). 오디오 재개를 눌러 주세요.`);
  }

  async function resumeLiveAudio() {
    try {
      await liveCleanupRef.current?.resume();
      setCaptureIssue(null);
    } catch (err) {
      setCaptureIssue(errorMessage(err) ?? '오디오 처리를 재개하지 못했습니다.');
    }
  }

  function installStreamWatchdog(stream: MediaStream, liveSessionToken: string): boolean {
    streamWatchdogCleanupRef.current?.();
    const track = stream.getAudioTracks?.()[0];
    if (!track?.addEventListener) {
      streamWatchdogCleanupRef.current = null;
      return true;
    }

    const handleTrackEnded = () => {
      if (
        liveSessionToken !== liveSessionTokenRef.current
        || streamRef.current !== stream
      ) {
        return;
      }
      streamWatchdogCleanupRef.current?.();
      streamWatchdogCleanupRef.current = null;
      setCaptureIssue('마이크 연결이 끊겨 녹음을 중단했습니다. 지금까지 수집된 데이터를 정리합니다.');
      stopLiveCapture({ clearTimeline: false, invalidateSession: false });
      stopTracks();
      setStatus('finalizing');
      void finalizeLiveSession(liveSessionToken, { showSummary: true });
    };
    track.addEventListener('ended', handleTrackEnded, { once: true });
    streamWatchdogCleanupRef.current = () => {
      track.removeEventListener('ended', handleTrackEnded);
    };
    // The track may have ended while the AudioContext graph was starting,
    // before this listener could be attached. Re-check after attachment to
    // close that race without missing an event between check and listen.
    if (track.readyState === 'ended') {
      handleTrackEnded();
    }
    return streamRef.current === stream;
  }

  async function waitForLiveDrain(liveSessionToken: string) {
    while (true) {
      const pending = [...(livePendingRequestsRef.current.get(liveSessionToken) ?? [])];
      if (!pending.length) {
        return;
      }
      await Promise.allSettled(pending);
    }
  }

  function abortLiveRequests(liveSessionToken: string) {
    const controllers = liveAbortControllersRef.current.get(liveSessionToken);
    controllers?.forEach((controller) => controller.abort());
    liveAbortControllersRef.current.delete(liveSessionToken);
  }

  async function finalizeLiveSession(
    liveSessionToken: string,
    {
      showSummary,
      waitForDrain = showSummary,
    }: { showSummary: boolean; waitForDrain?: boolean },
  ) {
    if (!liveSessionToken.startsWith('session-')) {
      return;
    }
    if (finalizationInFlightTokenRef.current === liveSessionToken) {
      return;
    }
    finalizationInFlightTokenRef.current = liveSessionToken;

    const controller = new AbortController();
    finalizationAbortControllerRef.current = controller;
    let timedOut = false;
    let timeout = 0;
    const timeoutPromise = new Promise<never>((_resolve, reject) => {
      timeout = window.setTimeout(() => {
        timedOut = true;
        controller.abort();
        abortLiveRequests(liveSessionToken);
        reject(new DOMException('Session finalization timed out.', 'TimeoutError'));
      }, LIVE_SESSION_END_TIMEOUT_MS);
    });

    if (showSummary) {
      setCollectionFinalizing(true);
      setFinalizationError(null);
      setStatus('finalizing');
    }

    try {
      if (showSummary && waitForDrain) {
        // One user-visible deadline covers both draining the final chunks and
        // persisting the session summary. This prevents a stalled chunk from
        // extending finalization by its own request timeout.
        await Promise.race([
          waitForLiveDrain(liveSessionToken),
          timeoutPromise,
        ]);
      }
      const summary = await Promise.race([
        endLiveSession(
          liveSessionToken,
          sessionNameRef.current.trim() || undefined,
          controller.signal,
        ),
        timeoutPromise,
      ]);
      if (showSummary && liveSessionToken === liveSessionTokenRef.current) {
        setCollectionSummary(summary);
        liveSessionTokenRef.current = `inactive-${Date.now()}-${recordingSessionRef.current}`;
        liveInFlightRef.current = 0;
        startedAtRef.current = null;
        setStatus('complete');
        setFinalizationError(null);
      }
      setCollectedRefreshToken((token) => token + 1);
    } catch (err) {
      const detail = timedOut
        ? `세션 종료 확인이 ${Math.round(LIVE_SESSION_END_TIMEOUT_MS / 1000)}초 안에 완료되지 않았습니다.`
        : (errorMessage(err) ?? '세션 종료 요청에 실패했습니다.');
      if (showSummary && liveSessionToken === liveSessionTokenRef.current) {
        setFinalizationError(`${detail} 수집된 데이터는 유지됩니다. 다시 시도해 주세요.`);
        setStatus('error');
      } else {
        console.warn('[Cochl.Sense Cloud Live Demo] 수집 세션 종료 실패:', detail);
      }
    } finally {
      window.clearTimeout(timeout);
      if (finalizationAbortControllerRef.current === controller) {
        finalizationAbortControllerRef.current = null;
      }
      if (finalizationInFlightTokenRef.current === liveSessionToken) {
        finalizationInFlightTokenRef.current = null;
      }
      if (showSummary) {
        setCollectionFinalizing(false);
      }
    }
  }

  function upsertLiveChunkState(next: LiveChunkRecord) {
    liveChunkLogRef.current.set(next.sequenceId, next);
    setLiveChunkRecords((current) => retainRecentLiveChunkRecords(
      upsertLiveChunkRecord(current, next),
      LIVE_UI_HISTORY_RETENTION_SEC,
    ));
  }

  function handleLiveWindow(window: LiveAudioWindow, liveSessionToken: string) {
    if (liveSessionToken !== liveSessionTokenRef.current) {
      return;
    }

    const recordingStartedAtMs = startedAtRef.current;
    if (recordingStartedAtMs === null) {
      return;
    }

    const windowEmittedAtMs = Date.now();
    const sequenceId = liveSequenceRef.current + 1;
    liveSequenceRef.current = sequenceId;
    const recordInput = {
      sessionId: liveSessionToken,
      sequenceId,
      recordingStartedAtMs,
      windowStartSec: window.windowStartSec,
      windowEndSec: window.windowEndSec,
      windowEmittedAtMs,
      inFlightAtDispatch: liveInFlightRef.current,
    };

    if (liveInFlightRef.current >= LIVE_MAX_IN_FLIGHT) {
      upsertLiveChunkState(createSkippedLiveChunkRecord(recordInput));
      setLiveDiagnostics((current) => recordLiveDiagnostic(current, 'skipped', sequenceId));
      return;
    }

    const pendingRecord = createPendingLiveChunkRecord(recordInput);
    upsertLiveChunkState(pendingRecord);
    liveInFlightRef.current += 1;
    const pendingRequest = submitLiveWindow(
      window,
      liveSessionToken,
      pendingRecord,
      windowEmittedAtMs,
    );
    const sessionRequests = livePendingRequestsRef.current.get(liveSessionToken) ?? new Set();
    sessionRequests.add(pendingRequest);
    livePendingRequestsRef.current.set(liveSessionToken, sessionRequests);
    void pendingRequest.finally(() => {
      sessionRequests.delete(pendingRequest);
      if (!sessionRequests.size) {
        livePendingRequestsRef.current.delete(liveSessionToken);
      }
    });
  }

  function handleLiveSpectrogramFrame(frame: LiveSpectrogramFrame, liveSessionToken: string) {
    if (liveSessionToken !== liveSessionTokenRef.current) {
      return;
    }
    appendLiveSpectrogramFrame(frame);
    updateAudioTimeline(frame.timestampSec);
  }

  function appendLiveSpectrogramFrame(frame: LiveSpectrogramFrame) {
    liveSpectrogramFramesRef.current = appendCompactedSpectrogramFrame(
      liveSpectrogramFramesRef.current,
      frame,
      MAX_LIVE_SPECTROGRAM_FRAMES,
    );
  }

  async function submitLiveWindow(
    window: LiveAudioWindow,
    liveSessionToken: string,
    initialRecord: LiveChunkRecord,
    windowEmittedAtMs: number,
  ) {
    let currentRecord = initialRecord;
    const controller = new AbortController();
    const sessionControllers = liveAbortControllersRef.current.get(liveSessionToken) ?? new Set();
    sessionControllers.add(controller);
    liveAbortControllersRef.current.set(liveSessionToken, sessionControllers);
    const timeout = globalThis.setTimeout(() => controller.abort(), LIVE_REQUEST_TIMEOUT_MS);
    try {
      const file = encodePcm16Wav(window.samples, window.sampleRate);
      if (liveSessionToken !== liveSessionTokenRef.current) {
        return;
      }
      const requestStartedAtMs = Date.now();
      currentRecord = markLiveChunkRequestStarted(currentRecord, requestStartedAtMs);
      upsertLiveChunkState(currentRecord);
      const response = await analyzeLiveChunk({
        file,
        sessionId: liveSessionToken,
        sequenceId: currentRecord.sequenceId,
        windowStartSec: window.windowStartSec,
        windowEndSec: window.windowEndSec,
        sessionName: sessionNameRef.current.trim() || undefined,
        signal: controller.signal,
      });
      const responseReceivedAtMs = Date.now();
      if (liveSessionToken !== liveSessionTokenRef.current) {
        return;
      }
      currentRecord = completeLiveChunkRecord(
        currentRecord,
        response,
        liveConfidenceThresholdRef.current,
        responseReceivedAtMs,
      );
      upsertLiveChunkState(currentRecord);
      const collectionStatus = response.collection_status;
      if (collectionStatus) {
        setLiveCollectionCounts((current) => applyCollectionStatus(current, collectionStatus));
      }
      if (response.curation_progress) {
        const nextProgress = response.curation_progress;
        setLiveCurationProgress((current) => mergeLiveCurationProgress(
          current,
          nextProgress,
        ));
      }

      const timelineEvents = liveTimelineEventsFromResponse(
        response,
        liveConfidenceThresholdRef.current,
      );
      setLiveTimelineEvents((current) => retainRecentLiveTimelineEvents(
        timelineEvents.length ? mergeLiveTimelineEvents(current, timelineEvents) : current,
        currentRecord.windowEndSec,
        LIVE_UI_HISTORY_RETENTION_SEC,
      ));
      if (timelineEvents.length) {
        const detectedEvents = response.sound_events.filter(
          (event) => typeof event.confidence === 'number'
            && event.confidence >= liveConfidenceThresholdRef.current,
        );
        detectedEvents.forEach((event) => {
          recordLiveLatency({
            event,
            response,
            requestStartedAtMs,
            responseReceivedAtMs,
            windowEmittedAtMs,
            windowEndSec: currentRecord.windowEndSec,
          });
        });
      } else {
        setLiveDiagnostics((current) => recordLiveDiagnostic(current, 'empty', currentRecord.sequenceId));
      }
    } catch (err) {
      if (liveSessionToken === liveSessionTokenRef.current) {
        currentRecord = failLiveChunkRecord(currentRecord, err, Date.now());
        upsertLiveChunkState(currentRecord);
        setLiveDiagnostics((current) => recordLiveDiagnostic(
          current,
          'failure',
          currentRecord.sequenceId,
          errorMessage(err),
        ));
      }
    } finally {
      globalThis.clearTimeout(timeout);
      sessionControllers.delete(controller);
      if (!sessionControllers.size) {
        liveAbortControllersRef.current.delete(liveSessionToken);
      }
      if (liveSessionToken === liveSessionTokenRef.current) {
        liveInFlightRef.current = Math.max(0, liveInFlightRef.current - 1);
      }
    }
  }

  function recordLiveLatency({
    event,
    response,
    requestStartedAtMs,
    responseReceivedAtMs,
    windowEmittedAtMs,
    windowEndSec,
  }: {
    event: SoundEvent;
    response: Awaited<ReturnType<typeof analyzeLiveChunk>>;
    requestStartedAtMs: number;
    responseReceivedAtMs: number;
    windowEmittedAtMs: number;
    windowEndSec: number;
  }) {
    const recordingStartedAtMs = startedAtRef.current;
    if (recordingStartedAtMs === null) {
      return;
    }

    const markerCreatedAtMs = Date.now();
    const captureClockDriftMs = Math.round(
      windowEmittedAtMs - (recordingStartedAtMs + windowEndSec * 1000),
    );
    const windowCallbackDelayMs = positiveRoundedMs(markerCreatedAtMs - windowEmittedAtMs);
    const sample: LiveLatencySample = {
      label: event.label,
      sequenceId: response.sequence_id,
      confidence: event.confidence,
      eventStartSec: event.start_time_sec,
      eventEndSec: event.end_time_sec,
      eventDelayMs: positiveRoundedMs(
        windowCallbackDelayMs + (windowEndSec - event.start_time_sec) * 1000,
      ),
      requestMs: positiveRoundedMs(responseReceivedAtMs - requestStartedAtMs),
      backendMs: positiveRoundedMs(response.processing_time_ms),
      backendTotalMs: positiveRoundedMs(response.timings?.total_ms ?? response.processing_time_ms),
      windowEndDelayMs: windowCallbackDelayMs,
      windowCallbackDelayMs,
      captureClockDriftMs,
      encodeMs: positiveRoundedMs(requestStartedAtMs - windowEmittedAtMs),
    };
    setLiveLatencySample((current) => {
      if (!current) {
        return sample;
      }
      if (sample.sequenceId !== current.sequenceId) {
        return sample.sequenceId > current.sequenceId ? sample : current;
      }
      return sample.eventStartSec >= current.eventStartSec ? sample : current;
    });
    console.info('[Cochl.Sense Cloud Live Demo latency]', sample);
  }

  function stopTracks() {
    streamWatchdogCleanupRef.current?.();
    streamWatchdogCleanupRef.current = null;
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
  }

  function handleSessionNameChange(event: ChangeEvent<HTMLInputElement>) {
    setSessionName(event.target.value);
    sessionNameRef.current = event.target.value;
  }

  function handleLiveViewportStartChange(startSec: number) {
    setLiveAutoFollow(false);
    setLiveViewportStartSec(startSec);
  }

  function handleLiveJumpToLatest() {
    setLiveAutoFollow(true);
    setLiveViewportStartSec(liveViewportState.maxStartSec);
  }

  function handleDownloadLiveChunkCsv() {
    if (!canDownloadCsv) {
      return;
    }

    const objectUrl = createLiveChunkCsvObjectUrl();
    const anchor = document.createElement('a');

    try {
      anchor.href = objectUrl;
      anchor.download = liveChunkCsvFilename(fullLiveChunkLog()[0]?.sessionId ?? 'session');
      anchor.target = '_blank';
      anchor.rel = 'noopener';
      document.body.append(anchor);
      anchor.click();
    } finally {
      anchor.remove();
      scheduleObjectUrlRevoke(objectUrl);
    }
  }

  function handleOpenLiveChunkCsv() {
    if (!canDownloadCsv) {
      return;
    }

    const objectUrl = createLiveChunkCsvObjectUrl();
    window.open(objectUrl, '_blank', 'noopener');
    scheduleObjectUrlRevoke(objectUrl);
  }

  function createLiveChunkCsvObjectUrl(): string {
    const csv = liveChunkRecordsToCsv(fullLiveChunkLog(), Date.now());
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    return URL.createObjectURL(blob);
  }

  function fullLiveChunkLog(): LiveChunkRecord[] {
    return [...liveChunkLogRef.current.values()];
  }

  function scheduleObjectUrlRevoke(objectUrl: string) {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  }

  const canStart =
    !collectionFinalizing
    && !finalizationError
    && (status === 'idle' || status === 'error' || status === 'complete');
  const canComplete = status === 'recording';
  const canStopAndKeep = status === 'recording';
  const canDownloadCsv = status === 'complete' && liveChunkLogRef.current.size > 0;
  const hasCollectionActivity =
    liveCollectionCounts.collected > 0 ||
    liveCollectionCounts.discardedSilent > 0 ||
    liveCollectionCounts.discardedSpeech > 0 ||
    liveCollectionCounts.discardedLate > 0;
  const ignoredSegmentCount = liveCurationProgress
    ? Math.max(
      0,
      liveCurationProgress.candidate_segment_count - liveCurationProgress.selected_segment_count,
    )
    : 0;
  const otherIgnoredSegmentCount = liveCurationProgress
    ? liveCurationProgress.invalid_audio_count + liveCurationProgress.write_error_count
    : 0;

  return (
    <main className={`app-shell${compactMode ? ' app-shell-compact' : ''}`}>
      <section
        className="workspace"
        aria-labelledby={compactMode ? 'compact-recorder-title' : 'app-title'}
      >
        {compactMode && (
          <section
            ref={compactRecorderRef}
            className="compact-recorder"
            aria-label="작게 보기 녹음 상태"
            tabIndex={-1}
          >
            <header className="compact-recorder-header">
              <h1 id="compact-recorder-title" className="compact-recording-indicator">
                <span aria-hidden="true" />
                녹음 중
              </h1>
              <button
                type="button"
                className="compact-expand-button"
                onClick={() => setCompactMode(false)}
                aria-label="전체 화면으로 보기"
                title="전체 화면으로 보기"
              >
                <Maximize2 size={16} aria-hidden="true" />
              </button>
            </header>

            <div className="compact-recorder-time">
              <span>녹음 시간</span>
              <time dateTime={`PT${elapsedSec}S`} aria-label={`녹음 시간 ${durationLabel}`}>
                {durationLabel}
              </time>
            </div>

            <div
              className={`compact-sound-status${compactDetectedSound ? ' is-detected' : ''}`}
              role="status"
              aria-live="polite"
              aria-atomic="true"
            >
              <span className="compact-sound-icon" aria-hidden="true">
                <AudioLines size={21} strokeWidth={2.2} />
              </span>
              <span className="compact-sound-copy">
                <span>최근 감지 소리</span>
                <strong>{compactDetectedSound?.label ?? '새 감지를 기다리는 중…'}</strong>
              </span>
              {typeof compactDetectedSound?.confidence === 'number' && (
                <span className="compact-sound-confidence">
                  {Math.round(compactDetectedSound.confidence * 100)}%
                </span>
              )}
            </div>

            {liveDegraded && (
              <p className="compact-degraded-warning" role="status">
                전송이 불안정합니다 · 연속 {consecutiveDeliveryIssues}개 누락/실패
              </p>
            )}

            <button
              className="compact-stop-button"
              type="button"
              onClick={completeRecording}
              disabled={!canComplete}
            >
              <Square size={15} fill="currentColor" aria-hidden="true" />
              녹음 중지
            </button>
          </section>
        )}

        <div
          className="full-workspace"
          aria-hidden={compactMode || undefined}
          hidden={compactMode}
        >
        <header className="workspace-header">
          <div>
            <p className="eyebrow">Cochl.Sense Cloud API</p>
            <h1 ref={workspaceTitleRef} id="app-title" tabIndex={-1}>실시간 스트리밍 현황판</h1>
          </div>
          <div className="workspace-header-actions">
            <StatusBadge status={status} degraded={liveDegraded} />
            {status === 'recording' && (
              <button
                ref={compactModeButtonRef}
                type="button"
                className="compact-mode-button"
                onClick={() => {
                  focusBeforeCompactRef.current = document.activeElement as HTMLElement | null;
                  setCompactMode(true);
                }}
              >
                <Minimize2 size={16} aria-hidden="true" />
                작게 보기
              </button>
            )}
          </div>
        </header>

        <div className="recording-surface">
          <div className="timer">
            {durationLabel}
          </div>
          <div className="meta-row">
            <span>중요 구간만 자동 저장</span>
            <span>원본 저장 안 함 · 화면 이력 최근 60분 · 진단 CSV 전체 세션</span>
            <span>입력 48 kHz · 모노 · 음성 보정 끔</span>
            <span>수집 기준 신뢰도 {Math.round(liveConfidenceThreshold * 100)}%</span>
            {(status === 'recording' || hasCollectionActivity) && (
              <span
                aria-label="실시간 데이터 수집 현황"
                title="의미 있는 소리 구간만 저장합니다. 무음과 음성(프라이버시) 구간은 제외됩니다."
              >
                수집 후보 청크 {liveCollectionCounts.collected} · 무음 제외 {liveCollectionCounts.discardedSilent} · 음성 제외{' '}
                {liveCollectionCounts.discardedSpeech} · 종료 후 제외 {liveCollectionCounts.discardedLate}
              </span>
            )}
            {status === 'recording' && liveCurationProgress
              && liveCurationProgress.candidate_segment_count > 0 && (
              <span
                aria-label="실시간 세그먼트 최종 판정"
                aria-live="polite"
                title={`손상 오디오 ${liveCurationProgress.invalid_audio_count} · 저장 오류 ${liveCurationProgress.write_error_count}`}
              >
                세그먼트 판정 후보 {liveCurationProgress.candidate_segment_count} · 선택{' '}
                {liveCurationProgress.selected_segment_count} · 무시 {ignoredSegmentCount} (반복{' '}
                {liveCurationProgress.rejected_repetitive_count} · 균형{' '}
                {liveCurationProgress.rejected_class_balance_count} · 상한{' '}
                {liveCurationProgress.rejected_session_budget_count}
                {otherIgnoredSegmentCount > 0 && ` · 기타 ${otherIgnoredSegmentCount}`})
              </span>
            )}
            {liveLatencySample && (
              <span
                aria-label="최근 실시간 감지 지연"
                title={formatLiveLatencyTitle(liveLatencySample)}
              >
                {formatLiveLatencyLabel(liveLatencySample)}
              </span>
            )}
          </div>
          <LiveSpectrogramPanel
            frames={liveSpectrogramFramesRef.current}
            framesRef={liveSpectrogramFramesRef}
            frameVersion={liveSpectrogramVersion}
            active={status === 'recording'}
            events={liveTimelineEvents}
            diagnostics={liveDiagnostics}
            viewport={liveViewportState.viewport}
            totalDurationSec={liveDurationSec}
            maxViewportStartSec={liveViewportState.maxStartSec}
            autoFollow={liveAutoFollow}
            historyLimit={LIVE_HISTORY_LIMIT}
            chunkRecords={liveChunkRecords}
            chunkSnapshotMs={liveChunkSnapshotMs}
            canDownloadCsv={canDownloadCsv}
            onDownloadCsv={handleDownloadLiveChunkCsv}
            onOpenCsv={handleOpenLiveChunkCsv}
            onViewportStartChange={handleLiveViewportStartChange}
            onJumpToLatest={handleLiveJumpToLatest}
          />
          <div className="session-name-row">
            <label htmlFor="session-name-input">세션 이름</label>
            <input
              id="session-name-input"
              type="text"
              value={sessionName}
              maxLength={100}
              placeholder="예: 사무실 오후 소음 (선택)"
              onChange={handleSessionNameChange}
              disabled={status === 'starting' || status === 'recording'}
            />
          </div>
          <div className="controls" aria-label="녹음 컨트롤">
            <button className="primary" type="button" onClick={startRecording} disabled={!canStart}>
              <Mic size={18} aria-hidden="true" />
              녹음 시작
            </button>
            <button type="button" onClick={completeRecording} disabled={!canComplete}>
              <Square size={18} aria-hidden="true" />
              완료
            </button>
            <button
              type="button"
              onClick={stopRecordingAndKeepCollected}
              disabled={!canStopAndKeep}
              title="이미 수집된 데이터는 유지하고, 분석 중인 청크를 취소한 뒤 녹음을 중단합니다."
            >
              <Pause size={18} aria-hidden="true" />
              중단 (수집분 유지)
            </button>
          </div>
        </div>

        {liveDegraded && (
          <div className="notice warning" role="status">
            <AlertTriangle size={18} aria-hidden="true" />
            <span>
              실시간 전송이 불안정합니다. 최근 {consecutiveDeliveryIssues}개 청크가 연속으로
              누락되거나 실패했습니다. 네트워크와 API 상태를 확인해 주세요.
            </span>
          </div>
        )}

        {captureIssue && (
          <div className="notice warning" role="status">
            <AlertTriangle size={18} aria-hidden="true" />
            <span>{captureIssue}</span>
            {status === 'recording'
              && liveAudioState !== 'running'
              && liveAudioState !== 'closed'
              && liveAudioState !== 'unknown' && (
              <button type="button" onClick={() => void resumeLiveAudio()}>
                오디오 재개
              </button>
            )}
          </div>
        )}

        {collectionFinalizing && (
          <div className="notice progress" role="status">
            <RefreshCw size={18} aria-hidden="true" />
            마지막 청크와 수집 데이터를 정리하고 있습니다. 잠시만 기다려 주세요.
          </div>
        )}

        {finalizationError && (
          <div className="notice error notice-with-action" role="alert">
            <AlertTriangle size={18} aria-hidden="true" />
            <span>{finalizationError}</span>
            <button type="button" onClick={retryFinalization} disabled={collectionFinalizing}>
              종료 다시 시도
            </button>
          </div>
        )}

        {error && (
          <div className="notice error" role="alert">
            <AlertTriangle size={18} aria-hidden="true" />
            {error}
          </div>
        )}

        {collectionSummary && <CollectionSummaryPanel summary={collectionSummary} />}

        <CollectedSessionsPanel
          refreshToken={collectedRefreshToken}
          capabilities={runtimeCapabilities}
        />
        </div>
      </section>
    </main>
  );
}

interface NativeWindowModeHandler {
  postMessage(message: { compact: boolean }): void;
}

interface NativeAppReadyHandler {
  postMessage(message: { ready: true }): void;
}

function notifyNativeAppReady() {
  const webkitWindow = window as Window & {
    webkit?: {
      messageHandlers?: {
        appReady?: NativeAppReadyHandler;
      };
    };
  };
  try {
    webkitWindow.webkit?.messageHandlers?.appReady?.postMessage({ ready: true });
  } catch {
    // Regular browsers do not expose the native macOS readiness bridge.
  }
}

function requestNativeWindowMode(compact: boolean) {
  const webkitWindow = window as Window & {
    webkit?: {
      messageHandlers?: {
        windowMode?: NativeWindowModeHandler;
      };
    };
  };
  try {
    webkitWindow.webkit?.messageHandlers?.windowMode?.postMessage({ compact });
  } catch {
    // The browser layout still provides compact mode when no native bridge is available.
  }
}

function errorMessage(err: unknown): string | undefined {
  return err instanceof Error ? err.message : undefined;
}

function assertAutomaticVoiceProcessingDisabled(stream: MediaStream) {
  const audioTrack = typeof stream.getAudioTracks === 'function'
    ? stream.getAudioTracks()[0]
    : undefined;
  if (!audioTrack || typeof audioTrack.getSettings !== 'function') {
    return;
  }

  const settings = audioTrack.getSettings() as ExtendedMediaTrackSettings;
  const enabled = [
    settings.echoCancellation === true ? '에코 제거' : null,
    settings.noiseSuppression === true ? '노이즈 억제' : null,
    settings.autoGainControl === true ? '자동 게인' : null,
    settings.voiceIsolation === true ? 'Voice Isolation' : null,
  ].filter((label): label is string => label !== null);
  if (enabled.length) {
    throw new Error(`자동 음성 보정을 끌 수 없습니다: ${enabled.join(', ')}`);
  }
}

function positiveRoundedMs(value: number): number {
  return Math.max(0, Math.round(value));
}

function formatLiveLatencyLabel(sample: LiveLatencySample): string {
  return `최근 API 요청: ${formatLatency(sample.requestMs)} · ${sample.label} 감지까지 ${formatLatency(sample.eventDelayMs)}`;
}

function formatLiveLatencyTitle(sample: LiveLatencySample): string {
  return [
    `이벤트 시작->마커 ${formatLatency(sample.eventDelayMs)}`,
    `윈도우 종료(콜백 기준)->마커 ${formatLatency(sample.windowEndDelayMs)}`,
    `요청 왕복 ${formatLatency(sample.requestMs)}`,
    `서버 분석 ${formatLatency(sample.backendMs)}`,
    `서버 전체 ${formatLatency(sample.backendTotalMs)}`,
    `WAV 인코딩 ${formatLatency(sample.encodeMs)}`,
    `윈도우 콜백->마커 ${formatLatency(sample.windowCallbackDelayMs)}`,
    `캡처 시계 차이 ${formatSignedLatency(sample.captureClockDriftMs)}`,
    `sequence ${sample.sequenceId}`,
  ].join(', ');
}

function formatLatency(milliseconds: number): string {
  return `${(milliseconds / 1000).toFixed(2)}초`;
}

function formatSignedLatency(milliseconds: number): string {
  const sign = milliseconds >= 0 ? '+' : '-';
  return `${sign}${formatLatency(Math.abs(milliseconds))}`;
}

function CollectionSummaryPanel({ summary }: { summary: LiveSessionEndResponse }) {
  const hasSegments = summary.segment_count > 0;
  const displayedSegments = summary.segments.slice(-COLLECTION_SUMMARY_DISPLAY_LIMIT);

  return (
    <section className="collection-summary" aria-labelledby="collection-summary-title">
      <header className="collection-summary-header">
        <Database size={18} aria-hidden="true" />
        <h2 id="collection-summary-title">데이터 수집 결과</h2>
      </header>
      {(summary.session_name || summary.started_at) && (
        <p className="collection-summary-session">
          {summary.session_name && <strong>{summary.session_name}</strong>}
          {summary.session_name && summary.started_at && ' · '}
          {summary.started_at && formatIsoTimestamp(summary.started_at)}
        </p>
      )}
      <p className="collection-summary-stats">
        후보 {summary.candidate_segment_count}개 · 선택 {summary.segment_count}개 · 총{' '}
        {summary.total_collected_duration_sec.toFixed(1)}초 저장 · 반복 제외{' '}
        {summary.rejected_repetitive_count}개 · 클래스 균형 제외{' '}
        {summary.rejected_class_balance_count}개 · 세션 상한 제외{' '}
        {summary.rejected_session_budget_count}개
      </p>
      <p className="collection-summary-stats">
        무음 제외 {summary.discarded_silent_chunk_count}개 · 음성(프라이버시) 제외{' '}
        {summary.discarded_speech_chunk_count}개 · 손상 오디오 제외 {summary.invalid_audio_count}개 · 저장 오류{' '}
        {summary.write_error_count}개
      </p>
      {hasSegments ? (
        <ul className="collection-segment-list">
          {displayedSegments.map((segment) => (
            <li key={segment.segment_index} className="collection-segment-item">
              <span className="collection-segment-index">#{segment.segment_index}</span>
              <span className="collection-segment-range">
                {formatTime(segment.start_sec)}–{formatTime(segment.end_sec)} ({segment.duration_sec.toFixed(1)}초)
              </span>
              <span className="collection-segment-labels">
                {segment.labels.length ? segment.labels.join(', ') : '라벨 없음'}
              </span>
            </li>
          ))}
        </ul>
      ) : (
        <p className="collection-summary-empty">
          수집된 세그먼트가 없습니다. 의미 있는 소리가 감지된 구간만 저장됩니다.
        </p>
      )}
      {summary.segments.length > displayedSegments.length && (
        <p className="collection-summary-note">
          장시간 세션의 화면 부하를 줄이기 위해 최근 {displayedSegments.length}개만 표시합니다.
          전체 {summary.segment_count}개 파일은 아래 수집된 데이터에서 확인할 수 있습니다.
        </p>
      )}
      {hasSegments && (
        <p className="collection-summary-note">
          저장 위치: recordings/collected/{summary.session_id}/ (세그먼트 오디오 + 메타데이터 JSON)
        </p>
      )}
    </section>
  );
}

function formatIsoTimestamp(value: string): string {
  const parsed = new Date(value);
  if (Number.isNaN(parsed.getTime())) {
    return value;
  }
  return parsed.toLocaleString('ko-KR', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function StatusBadge({
  status,
  degraded = false,
}: {
  status: RecorderStatus;
  degraded?: boolean;
}) {
  const labels: Record<RecorderStatus, string> = {
    idle: '대기',
    starting: '마이크 준비 중',
    recording: '녹음 중',
    finalizing: '수집 정리 중',
    complete: '완료',
    error: '확인 필요',
  };

  return (
    <span
      className={`status-badge status-${degraded ? 'degraded' : status}`}
      role="status"
      aria-live="polite"
      aria-atomic="true"
    >
      {degraded ? '전송 불안정' : labels[status]}
    </span>
  );
}

function countConsecutiveDeliveryIssues(records: LiveChunkRecord[]): number {
  const settled = records
    .filter((record) => record.status !== 'PENDING')
    .sort((left, right) => left.sequenceId - right.sequenceId);
  let count = 0;
  for (let index = settled.length - 1; index >= 0; index -= 1) {
    const status = settled[index].status;
    if (status !== 'FAIL' && status !== 'SKIP') {
      break;
    }
    count += 1;
  }
  return count;
}
