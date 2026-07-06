import { type ChangeEvent, useEffect, useMemo, useRef, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  Mic,
  Pause,
  Play,
  RefreshCw,
  Square,
  Trash2,
  UploadCloud,
} from 'lucide-react';
import { analyzeLiveChunk, analyzeRecording, endLiveSession } from './api';
import { getAudioContextConstructor } from './audioContext';
import { LiveSpectrogramPanel } from './LiveSpectrogramPanel';
import {
  createLiveAudioCapture,
  encodePcm16Wav,
  type LiveAudioWindow,
  type LiveSpectrogramFrame,
} from './liveAudio';
import {
  emptyLiveDiagnostics,
  liveTimelineEventsFromResponse,
  mergeLiveTimelineEvents,
  recordLiveDiagnostic,
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
  upsertLiveChunkRecord,
  type LiveChunkRecord,
} from './liveChunkRecords';
import { fileFromRecordingChunks, selectSupportedMimeType } from './recorder';
import type {
  AnalysisResponse,
  LiveChunkCollectionStatus,
  LiveSessionEndResponse,
  SoundEvent,
} from './types';
import { eventOverlayStyle, formatTime, peaksFromSamples } from './waveform';
import './App.css';

type RecorderStatus = 'idle' | 'recording' | 'ready' | 'uploading' | 'complete' | 'error';
type SegmentPlaybackMode = 'loading' | 'buffer' | 'native';

const LIVE_WINDOW_SEC = 2;
const LIVE_HOP_SEC = 1;
const LIVE_MAX_IN_FLIGHT = 10;
const LIVE_CONFIDENCE_THRESHOLD = 0.5;
const LIVE_VIEWPORT_SEC = 20;
const LIVE_SPECTROGRAM_FPS = 12;
const LIVE_SPECTROGRAM_BINS = 64;
const LIVE_HISTORY_LIMIT = 6;

interface LiveCollectionCounts {
  collected: number;
  discardedSilent: number;
  discardedSpeech: number;
}

function emptyLiveCollectionCounts(): LiveCollectionCounts {
  return { collected: 0, discardedSilent: 0, discardedSpeech: 0 };
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
  return { ...counts, discardedSilent: counts.discardedSilent + 1 };
}

interface LiveLatencySample {
  label: string;
  sequenceId: number;
  confidence: number | null;
  eventDelayMs: number;
  requestMs: number;
  backendMs: number;
  windowEndDelayMs: number;
  windowCallbackDelayMs: number;
}

export default function App() {
  const [status, setStatus] = useState<RecorderStatus>('idle');
  const [elapsedSec, setElapsedSec] = useState(0);
  const [recordingFile, setRecordingFile] = useState<File | null>(null);
  const [analysis, setAnalysis] = useState<AnalysisResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [selectedMimeType, setSelectedMimeType] = useState('');
  const [liveSpectrogramFrames, setLiveSpectrogramFrames] = useState<LiveSpectrogramFrame[]>([]);
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
  const [collectionSummary, setCollectionSummary] = useState<LiveSessionEndResponse | null>(null);
  const recorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const streamRef = useRef<MediaStream | null>(null);
  const startedAtRef = useRef<number | null>(null);
  const recordingSessionRef = useRef(0);
  const liveCleanupRef = useRef<(() => void) | null>(null);
  const liveInFlightRef = useRef(0);
  const liveSequenceRef = useRef(0);
  const liveSessionTokenRef = useRef('');
  const hasPendingLiveChunks = useMemo(
    () => hasPendingLiveChunkRecords(liveChunkRecords),
    [liveChunkRecords],
  );

  useEffect(() => {
    if (status !== 'recording') {
      return;
    }

    const interval = window.setInterval(() => {
      if (startedAtRef.current) {
        setElapsedSec(Math.floor((Date.now() - startedAtRef.current) / 1000));
      }
    }, 250);

    return () => window.clearInterval(interval);
  }, [status]);

  useEffect(() => {
    if (status !== 'recording' && !hasPendingLiveChunks) {
      return;
    }

    const interval = window.setInterval(() => {
      setLiveChunkSnapshotMs(Date.now());
    }, 250);

    return () => window.clearInterval(interval);
  }, [hasPendingLiveChunks, status]);

  useEffect(() => {
    return () => {
      const liveSessionToken = liveSessionTokenRef.current;
      stopLiveCapture({ clearTimeline: false, invalidateSession: true });
      void finalizeLiveSession(liveSessionToken, { showSummary: false });
    };
  }, []);

  const durationLabel = useMemo(() => formatDuration(elapsedSec), [elapsedSec]);
  const liveDurationSec = useMemo(() => {
    const frameTimes = liveSpectrogramFrames.map((frame) => frame.timestampSec);
    const eventTimes = liveTimelineEvents.map((event) => event.endTimeSec);
    const chunkTailEndSec = latestLiveChunkTailEndSec(liveChunkRecords, liveChunkSnapshotMs);
    return Math.max(0, elapsedSec, chunkTailEndSec, ...frameTimes, ...eventTimes);
  }, [elapsedSec, liveChunkRecords, liveChunkSnapshotMs, liveSpectrogramFrames, liveTimelineEvents]);
  const liveViewportState = useMemo(
    () => resolveLiveViewport(liveDurationSec, liveViewportStartSec, liveAutoFollow, LIVE_VIEWPORT_SEC),
    [liveAutoFollow, liveDurationSec, liveViewportStartSec],
  );

  async function startRecording() {
    setError(null);
    setAnalysis(null);

    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === 'undefined') {
      setStatus('error');
      setError('마이크 녹음을 지원하지 않는 브라우저입니다.');
      return;
    }

    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = selectSupportedMimeType();
      const recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
      const sessionId = recordingSessionRef.current + 1;

      recordingSessionRef.current = sessionId;
      chunksRef.current = [];
      streamRef.current = stream;
      recorderRef.current = recorder;
      startedAtRef.current = Date.now();
      setElapsedSec(0);
      startLiveCapture(stream, sessionId);
      setSelectedMimeType(mimeType || recorder.mimeType || 'browser default');
      setRecordingFile(null);

      recorder.ondataavailable = (event) => {
        if (event.data.size > 0) {
          chunksRef.current.push(event.data);
        }
      };

      recorder.onstop = () => {
        if (sessionId !== recordingSessionRef.current) {
          return;
        }
        const file = fileFromRecordingChunks(
          chunksRef.current,
          mimeType || recorder.mimeType || 'audio/webm',
        );
        if (!file.size) {
          setStatus('error');
          setError('녹음된 오디오가 비어 있습니다.');
          return;
        }

        setRecordingFile(file);
        setStatus('ready');
      };

      recorder.start();
      setStatus('recording');
    } catch (err) {
      setStatus('error');
      setError(err instanceof Error ? err.message : '마이크 권한을 가져오지 못했습니다.');
      stopTracks();
    }
  }

  function completeRecording() {
    const recorder = recorderRef.current;
    if (!recorder || recorder.state === 'inactive') {
      return;
    }
    const liveSessionToken = liveSessionTokenRef.current;
    stopLiveCapture({ clearTimeline: false, invalidateSession: false });
    recorder.stop();
    stopTracks();
    void finalizeLiveSession(liveSessionToken, { showSummary: true });
  }

  async function submitRecording() {
    if (!recordingFile) {
      setError('업로드할 녹음 파일이 없습니다.');
      return;
    }

    setStatus('uploading');
    setError(null);

    try {
      const result = await analyzeRecording(recordingFile);
      setAnalysis(result);
      setStatus('complete');
    } catch (err) {
      setStatus('ready');
      setError(err instanceof Error ? err.message : '분석 요청에 실패했습니다.');
    }
  }

  function discardRecording() {
    const liveSessionToken = liveSessionTokenRef.current;
    recordingSessionRef.current += 1;
    stopLiveCapture({ clearTimeline: true, invalidateSession: true });
    void finalizeLiveSession(liveSessionToken, { showSummary: false });
    if (recorderRef.current?.state === 'recording') {
      recorderRef.current.stop();
    }
    stopTracks();
    chunksRef.current = [];
    recorderRef.current = null;
    startedAtRef.current = null;
    setElapsedSec(0);
    setRecordingFile(null);
    setAnalysis(null);
    setError(null);
    setSelectedMimeType('');
    setStatus('idle');
  }

  function startLiveCapture(stream: MediaStream, recordingSessionId: number) {
    const liveSessionToken = `session-${Date.now()}-${recordingSessionId}`;
    liveSessionTokenRef.current = liveSessionToken;
    liveInFlightRef.current = 0;
    liveSequenceRef.current = 0;
    clearLiveTimelineState();
    setLiveLatencySample(null);

    try {
      liveCleanupRef.current = createLiveAudioCapture(
        stream,
        (window) => handleLiveWindow(window, liveSessionToken),
        {
          windowSec: LIVE_WINDOW_SEC,
          hopSec: LIVE_HOP_SEC,
          onSpectrogramFrame: (frame) => handleLiveSpectrogramFrame(frame, liveSessionToken),
          spectrogramFps: LIVE_SPECTROGRAM_FPS,
          spectrogramBins: LIVE_SPECTROGRAM_BINS,
        },
      );
    } catch (err) {
      liveCleanupRef.current = null;
      setLiveDiagnostics((current) => recordLiveDiagnostic(current, 'failure', 0, errorMessage(err)));
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
      liveSessionTokenRef.current = `inactive-${Date.now()}-${recordingSessionRef.current}`;
      liveInFlightRef.current = 0;
    }
    liveCleanupRef.current?.();
    liveCleanupRef.current = null;
    if (clearTimeline) {
      liveSequenceRef.current = 0;
      clearLiveTimelineState();
      setLiveLatencySample(null);
    }
  }

  function clearLiveTimelineState() {
    setLiveSpectrogramFrames([]);
    setLiveTimelineEvents([]);
    setLiveDiagnostics(emptyLiveDiagnostics());
    setLiveViewportStartSec(0);
    setLiveAutoFollow(true);
    setLiveChunkRecords([]);
    setLiveChunkSnapshotMs(Date.now());
    setLiveCollectionCounts(emptyLiveCollectionCounts());
    setCollectionSummary(null);
  }

  async function finalizeLiveSession(
    liveSessionToken: string,
    { showSummary }: { showSummary: boolean },
  ) {
    if (!liveSessionToken.startsWith('session-')) {
      return;
    }

    try {
      const summary = await endLiveSession(liveSessionToken);
      if (showSummary && liveSessionToken === liveSessionTokenRef.current) {
        setCollectionSummary(summary);
      }
    } catch (err) {
      console.warn('[Cochl.Sense Cloud Live Demo] 수집 세션 종료 실패:', errorMessage(err));
    }
  }

  function upsertLiveChunkState(next: LiveChunkRecord) {
    setLiveChunkRecords((current) => upsertLiveChunkRecord(current, next));
  }

  function handleLiveWindow(window: LiveAudioWindow, liveSessionToken: string) {
    if (liveSessionToken !== liveSessionTokenRef.current) {
      return;
    }

    const recordingStartedAtMs = startedAtRef.current;
    if (recordingStartedAtMs === null) {
      return;
    }

    const sequenceId = liveSequenceRef.current + 1;
    liveSequenceRef.current = sequenceId;
    const recordInput = {
      sessionId: liveSessionToken,
      sequenceId,
      recordingStartedAtMs,
      windowStartSec: window.windowStartSec,
      windowEndSec: window.windowEndSec,
    };

    if (liveInFlightRef.current >= LIVE_MAX_IN_FLIGHT) {
      upsertLiveChunkState(createSkippedLiveChunkRecord(recordInput));
      setLiveDiagnostics((current) => recordLiveDiagnostic(current, 'skipped', sequenceId));
      return;
    }

    const pendingRecord = createPendingLiveChunkRecord(recordInput);
    upsertLiveChunkState(pendingRecord);
    liveInFlightRef.current += 1;
    void submitLiveWindow(window, liveSessionToken, pendingRecord, Date.now());
  }

  function handleLiveSpectrogramFrame(frame: LiveSpectrogramFrame, liveSessionToken: string) {
    if (liveSessionToken !== liveSessionTokenRef.current) {
      return;
    }
    appendLiveSpectrogramFrame(frame);
  }

  function appendLiveSpectrogramFrame(frame: LiveSpectrogramFrame) {
    setLiveSpectrogramFrames((current) => [...current, frame]);
  }

  async function submitLiveWindow(
    window: LiveAudioWindow,
    liveSessionToken: string,
    initialRecord: LiveChunkRecord,
    windowEmittedAtMs: number,
  ) {
    let currentRecord = initialRecord;
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
      });
      const responseReceivedAtMs = Date.now();
      if (liveSessionToken !== liveSessionTokenRef.current) {
        return;
      }
      currentRecord = completeLiveChunkRecord(
        currentRecord,
        response,
        LIVE_CONFIDENCE_THRESHOLD,
        responseReceivedAtMs,
      );
      upsertLiveChunkState(currentRecord);
      const collectionStatus = response.collection_status;
      if (collectionStatus) {
        setLiveCollectionCounts((current) => applyCollectionStatus(current, collectionStatus));
      }

      const timelineEvents = liveTimelineEventsFromResponse(response, LIVE_CONFIDENCE_THRESHOLD);
      if (timelineEvents.length) {
        setLiveTimelineEvents((current) => mergeLiveTimelineEvents(current, timelineEvents));
        const detectedEvents = response.sound_events.filter(
          (event) => typeof event.confidence === 'number' && event.confidence >= LIVE_CONFIDENCE_THRESHOLD,
        );
        detectedEvents.forEach((event) => {
          recordLiveLatency({
            event,
            response,
            requestStartedAtMs,
            responseReceivedAtMs,
            windowEmittedAtMs,
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
  }: {
    event: SoundEvent;
    response: Awaited<ReturnType<typeof analyzeLiveChunk>>;
    requestStartedAtMs: number;
    responseReceivedAtMs: number;
    windowEmittedAtMs: number;
  }) {
    const recordingStartedAtMs = startedAtRef.current;
    if (recordingStartedAtMs === null) {
      return;
    }

    const markerCreatedAtMs = Date.now();
    const sample: LiveLatencySample = {
      label: event.label,
      sequenceId: response.sequence_id,
      confidence: event.confidence,
      eventDelayMs: positiveRoundedMs(markerCreatedAtMs - (recordingStartedAtMs + event.start_time_sec * 1000)),
      requestMs: positiveRoundedMs(responseReceivedAtMs - requestStartedAtMs),
      backendMs: positiveRoundedMs(response.processing_time_ms),
      windowEndDelayMs: positiveRoundedMs(markerCreatedAtMs - (recordingStartedAtMs + response.window_end_sec * 1000)),
      windowCallbackDelayMs: positiveRoundedMs(markerCreatedAtMs - windowEmittedAtMs),
    };
    setLiveLatencySample(sample);
    console.info('[Cochl.Sense Cloud Live Demo latency]', sample);
  }

  function stopTracks() {
    streamRef.current?.getTracks().forEach((track) => track.stop());
    streamRef.current = null;
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
      anchor.download = liveChunkCsvFilename(liveChunkRecords[0]?.sessionId ?? 'session');
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
    const csv = liveChunkRecordsToCsv(liveChunkRecords, Date.now());
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    return URL.createObjectURL(blob);
  }

  function scheduleObjectUrlRevoke(objectUrl: string) {
    window.setTimeout(() => URL.revokeObjectURL(objectUrl), 60_000);
  }

  const canStart = status === 'idle' || status === 'error' || status === 'complete';
  const canComplete = status === 'recording';
  const canSubmit = status === 'ready';
  const canDiscard = status !== 'uploading' && status !== 'idle';
  const canDownloadCsv =
    (status === 'ready' || status === 'uploading' || status === 'complete') && liveChunkRecords.length > 0;
  const hasCollectionActivity =
    liveCollectionCounts.collected > 0 ||
    liveCollectionCounts.discardedSilent > 0 ||
    liveCollectionCounts.discardedSpeech > 0;

  return (
    <main className="app-shell">
      <section className="workspace" aria-labelledby="app-title">
        <header className="workspace-header">
          <div>
            <p className="eyebrow">Cochl.Sense Cloud API</p>
            <h1 id="app-title">실시간 스트리밍 현황판</h1>
          </div>
          <StatusBadge status={status} />
        </header>

        <div className="recording-surface">
          <div className="timer" aria-live="polite">
            {durationLabel}
          </div>
          <div className="meta-row">
            <span>완료 후 업로드</span>
            <span>{selectedMimeType || '녹음 대기'}</span>
            {(status === 'recording' || hasCollectionActivity) && (
              <span
                aria-label="실시간 데이터 수집 현황"
                title="의미 있는 소리 구간만 저장합니다. 무음과 음성(프라이버시) 구간은 제외됩니다."
              >
                수집 {liveCollectionCounts.collected} · 무음 제외 {liveCollectionCounts.discardedSilent} · 음성 제외{' '}
                {liveCollectionCounts.discardedSpeech}
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
            frames={liveSpectrogramFrames}
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
          <div className="controls" aria-label="녹음 컨트롤">
            <button className="primary" type="button" onClick={startRecording} disabled={!canStart}>
              <Mic size={18} aria-hidden="true" />
              녹음 시작
            </button>
            <button type="button" onClick={completeRecording} disabled={!canComplete}>
              <Square size={18} aria-hidden="true" />
              완료
            </button>
            <button type="button" onClick={submitRecording} disabled={!canSubmit}>
              <UploadCloud size={18} aria-hidden="true" />
              분석
            </button>
            <button type="button" onClick={discardRecording} disabled={!canDiscard}>
              <Trash2 size={18} aria-hidden="true" />
              폐기
            </button>
          </div>
        </div>

        {status === 'uploading' && (
          <div className="notice progress" role="status">
            <RefreshCw size={18} aria-hidden="true" />
            녹음 파일을 Cochl.Sense Cloud API로 분석하고 있습니다.
          </div>
        )}

        {error && (
          <div className="notice error" role="alert">
            <AlertTriangle size={18} aria-hidden="true" />
            {error}
          </div>
        )}

        {collectionSummary && <CollectionSummaryPanel summary={collectionSummary} />}

        {analysis && <AnalysisPanel analysis={analysis} recordingFile={recordingFile} />}
      </section>
    </main>
  );
}

function errorMessage(err: unknown): string | undefined {
  return err instanceof Error ? err.message : undefined;
}

function positiveRoundedMs(value: number): number {
  return Math.max(0, Math.round(value));
}

function formatLiveLatencyLabel(sample: LiveLatencySample): string {
  return `최근 지연: ${sample.label} ${formatLatency(sample.eventDelayMs)} · 요청 ${formatLatency(sample.requestMs)}`;
}

function formatLiveLatencyTitle(sample: LiveLatencySample): string {
  return [
    `이벤트 시작->마커 ${formatLatency(sample.eventDelayMs)}`,
    `윈도우 종료->마커 ${formatLatency(sample.windowEndDelayMs)}`,
    `요청 왕복 ${formatLatency(sample.requestMs)}`,
    `서버 ${formatLatency(sample.backendMs)}`,
    `윈도우 콜백->마커 ${formatLatency(sample.windowCallbackDelayMs)}`,
    `sequence ${sample.sequenceId}`,
  ].join(', ');
}

function formatLatency(milliseconds: number): string {
  return `${(milliseconds / 1000).toFixed(2)}초`;
}

function CollectionSummaryPanel({ summary }: { summary: LiveSessionEndResponse }) {
  const hasSegments = summary.segment_count > 0;

  return (
    <section className="collection-summary" aria-labelledby="collection-summary-title">
      <header className="collection-summary-header">
        <Database size={18} aria-hidden="true" />
        <h2 id="collection-summary-title">데이터 수집 결과</h2>
      </header>
      <p className="collection-summary-stats">
        세그먼트 {summary.segment_count}개 · 총 {summary.total_collected_duration_sec.toFixed(1)}초 저장 · 무음 제외{' '}
        {summary.discarded_silent_chunk_count}개 · 음성(프라이버시) 제외 {summary.discarded_speech_chunk_count}개
      </p>
      {hasSegments ? (
        <ul className="collection-segment-list">
          {summary.segments.map((segment) => (
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
      {hasSegments && (
        <p className="collection-summary-note">
          저장 위치: recordings/collected/{summary.session_id}/ (세그먼트 오디오 + 메타데이터 JSON)
        </p>
      )}
    </section>
  );
}

function StatusBadge({ status }: { status: RecorderStatus }) {
  const labels: Record<RecorderStatus, string> = {
    idle: '대기',
    recording: '녹음 중',
    ready: '업로드 준비',
    uploading: '분석 중',
    complete: '완료',
    error: '확인 필요',
  };

  return <span className={`status-badge status-${status}`}>{labels[status]}</span>;
}

interface AnalysisPanelProps {
  analysis: AnalysisResponse;
  recordingFile: File | null;
}

export function AnalysisPanel({ analysis, recordingFile }: AnalysisPanelProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  const audioBufferRef = useRef<AudioBuffer | null>(null);
  const segmentSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const segmentContextRef = useRef<AudioContext | null>(null);
  const recordingSourceRef = useRef<AudioBufferSourceNode | null>(null);
  const recordingContextRef = useRef<AudioContext | null>(null);
  const recordingStartedAtRef = useRef(0);
  const recordingStartedOffsetRef = useRef(0);
  const recordingAnimationRef = useRef<number | null>(null);
  const segmentEndRef = useRef<number | null>(null);
  const [audioUrl, setAudioUrl] = useState('');
  const [peaks, setPeaks] = useState<number[]>([]);
  const [audioDurationSec, setAudioDurationSec] = useState<number | null>(null);
  const [playbackError, setPlaybackError] = useState<string | null>(null);
  const [segmentPlaybackMode, setSegmentPlaybackMode] = useState<SegmentPlaybackMode>('loading');
  const [recordingPlaybackSec, setRecordingPlaybackSec] = useState(0);
  const [isRecordingPlaying, setIsRecordingPlaying] = useState(false);
  const durationSec = useMemo(
    () => resolveDurationSec(analysis, audioDurationSec),
    [analysis, audioDurationSec],
  );
  const canPlaySegment =
    segmentPlaybackMode === 'buffer' || (segmentPlaybackMode === 'native' && Boolean(audioUrl));
  const hasDecodedRecordingPlayer = segmentPlaybackMode === 'buffer';

  useEffect(() => {
    let cancelled = false;
    const playbackUrls: string[] = [];

    function setPlaybackUrl(blob: Blob) {
      const url = URL.createObjectURL(blob);
      playbackUrls.push(url);
      setAudioUrl(url);
    }

    async function decodeWaveform() {
      setAudioUrl('');
      setSegmentPlaybackMode('loading');
      audioBufferRef.current = null;
      const AudioContextCtor = getAudioContextConstructor();
      if (!recordingFile || !URL.createObjectURL) {
        setPeaks([]);
        return;
      }
      if (!AudioContextCtor) {
        setPeaks([]);
        setPlaybackUrl(recordingFile);
        setSegmentPlaybackMode('native');
        return;
      }

      let context: AudioContext | null = null;
      try {
        context = new AudioContextCtor();
        const buffer = await recordingFile.arrayBuffer();
        const decoded = await context.decodeAudioData(buffer.slice(0));
        if (cancelled) {
          await context.close();
          return;
        }
        audioBufferRef.current = decoded;
        setAudioDurationSec(decoded.duration);
        setRecordingPlaybackSec(0);
        setPeaks(peaksFromSamples(decoded.getChannelData(0), 160));
        setSegmentPlaybackMode('buffer');
        await context.close();
      } catch {
        if (!cancelled) {
          audioBufferRef.current = null;
          setPeaks([]);
          setPlaybackUrl(recordingFile);
          setSegmentPlaybackMode('native');
        }
        if (context) {
          await context.close().catch(() => undefined);
        }
      }
    }

    void decodeWaveform();
    return () => {
      cancelled = true;
      playbackUrls.forEach((url) => URL.revokeObjectURL(url));
    };
  }, [recordingFile]);

  useEffect(() => {
    return () => {
      stopBufferedSegment();
      stopRecordingPlayback({ resetPosition: false });
    };
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    drawWaveform(canvas, peaks);
  }, [peaks]);

  async function playSegment(event: SoundEvent) {
    setPlaybackError(null);
    const playedFromBuffer = await playBufferedSegment(event).catch(() => false);
    if (playedFromBuffer) {
      return;
    }

    const audio = audioRef.current;
    if (!audio || !audioUrl) {
      setPlaybackError('재생할 녹음 파일을 찾지 못했습니다.');
      return;
    }

    try {
      segmentEndRef.current = event.end_time_sec;
      audio.currentTime = Math.max(0, event.start_time_sec);
      await audio.play();
    } catch {
      if (!audio.paused) {
        setPlaybackError(null);
        return;
      }
      segmentEndRef.current = null;
      setPlaybackError('녹음 파일을 재생하지 못했습니다. 오디오 컨트롤에서 직접 재생해 보거나 다시 녹음해 주세요.');
    }
  }

  async function playBufferedSegment(event: SoundEvent): Promise<boolean> {
    const buffer = audioBufferRef.current;
    const AudioContextCtor = getAudioContextConstructor();
    if (!buffer || !AudioContextCtor) {
      return false;
    }

    const startTimeSec = clamp(event.start_time_sec, 0, buffer.duration);
    const endTimeSec = clamp(event.end_time_sec, startTimeSec, buffer.duration);
    const segmentDurationSec = endTimeSec - startTimeSec;
    if (segmentDurationSec <= 0) {
      return false;
    }

    stopBufferedSegment();
    stopRecordingPlayback({ resetPosition: false });
    const nativeAudio = audioRef.current;
    if (nativeAudio) {
      nativeAudio.pause();
      try {
        nativeAudio.currentTime = startTimeSec;
      } catch {
        // The decoded buffer is the source of truth for segment playback.
      }
    }

    const context = new AudioContextCtor();
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    source.onended = () => {
      if (segmentSourceRef.current === source) {
        segmentSourceRef.current = null;
      }
      if (segmentContextRef.current === context) {
        segmentContextRef.current = null;
        void context.close().catch(() => undefined);
      }
    };
    segmentSourceRef.current = source;
    segmentContextRef.current = context;

    try {
      if (context.state === 'suspended') {
        await context.resume();
      }
      source.start(0, startTimeSec, segmentDurationSec);
      return true;
    } catch (error) {
      stopBufferedSegment();
      throw error;
    }
  }

  function stopBufferedSegment() {
    const source = segmentSourceRef.current;
    const context = segmentContextRef.current;
    segmentSourceRef.current = null;
    segmentContextRef.current = null;

    if (source) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // Source nodes throw if they have not started or already ended.
      }
      try {
        source.disconnect();
      } catch {
        // Ignore cleanup errors from already-disconnected nodes.
      }
    }

    if (context) {
      void context.close().catch(() => undefined);
    }
  }

  async function toggleRecordingPlayback() {
    if (isRecordingPlaying) {
      stopRecordingPlayback({ resetPosition: false });
      return;
    }

    await playRecordingFrom(recordingPlaybackSec);
  }

  async function playRecordingFrom(offsetSec: number) {
    const buffer = audioBufferRef.current;
    const AudioContextCtor = getAudioContextConstructor();
    if (!buffer || !AudioContextCtor) {
      setPlaybackError('재생할 녹음 파일을 찾지 못했습니다.');
      return;
    }

    stopBufferedSegment();
    stopRecordingPlayback({ resetPosition: false });
    setPlaybackError(null);

    const startOffset = clamp(offsetSec >= buffer.duration ? 0 : offsetSec, 0, buffer.duration);
    const context = new AudioContextCtor();
    const source = context.createBufferSource();
    source.buffer = buffer;
    source.connect(context.destination);
    source.onended = () => {
      if (recordingSourceRef.current !== source) {
        return;
      }
      recordingSourceRef.current = null;
      recordingContextRef.current = null;
      stopRecordingAnimation();
      setIsRecordingPlaying(false);
      setRecordingPlaybackSec(buffer.duration);
      void context.close().catch(() => undefined);
    };

    try {
      if (context.state === 'suspended') {
        await context.resume();
      }
      recordingSourceRef.current = source;
      recordingContextRef.current = context;
      recordingStartedAtRef.current = context.currentTime;
      recordingStartedOffsetRef.current = startOffset;
      setRecordingPlaybackSec(startOffset);
      setIsRecordingPlaying(true);
      source.start(0, startOffset);
      startRecordingAnimation();
    } catch {
      stopRecordingPlayback({ resetPosition: false });
      setPlaybackError('녹음 파일을 재생하지 못했습니다. 다시 녹음해 주세요.');
    }
  }

  function stopRecordingPlayback({ resetPosition }: { resetPosition: boolean }) {
    const currentPosition = currentRecordingPlaybackSec();
    const source = recordingSourceRef.current;
    const context = recordingContextRef.current;
    recordingSourceRef.current = null;
    recordingContextRef.current = null;
    stopRecordingAnimation();

    if (source) {
      source.onended = null;
      try {
        source.stop();
      } catch {
        // Source nodes throw if they have not started or already ended.
      }
      try {
        source.disconnect();
      } catch {
        // Ignore cleanup errors from already-disconnected nodes.
      }
    }

    if (context) {
      void context.close().catch(() => undefined);
    }

    setIsRecordingPlaying(false);
    setRecordingPlaybackSec(resetPosition ? 0 : currentPosition);
  }

  function currentRecordingPlaybackSec(): number {
    const buffer = audioBufferRef.current;
    const context = recordingContextRef.current;
    if (!buffer || !context || !recordingSourceRef.current) {
      return recordingPlaybackSec;
    }

    return clamp(
      recordingStartedOffsetRef.current + context.currentTime - recordingStartedAtRef.current,
      0,
      buffer.duration,
    );
  }

  function startRecordingAnimation() {
    stopRecordingAnimation();
    const tick = () => {
      setRecordingPlaybackSec(currentRecordingPlaybackSec());
      recordingAnimationRef.current = window.requestAnimationFrame(tick);
    };
    recordingAnimationRef.current = window.requestAnimationFrame(tick);
  }

  function stopRecordingAnimation() {
    if (recordingAnimationRef.current !== null) {
      window.cancelAnimationFrame(recordingAnimationRef.current);
      recordingAnimationRef.current = null;
    }
  }

  function handleRecordingSeek(event: ChangeEvent<HTMLInputElement>) {
    const nextPosition = Number(event.currentTarget.value);
    setRecordingPlaybackSec(nextPosition);
    if (isRecordingPlaying) {
      void playRecordingFrom(nextPosition);
    }
  }

  function handleNativeAudioPlay() {
    stopBufferedSegment();
    stopRecordingPlayback({ resetPosition: false });
    setPlaybackError(null);
  }

  function handleTimeUpdate() {
    const audio = audioRef.current;
    const segmentEnd = segmentEndRef.current;
    if (!audio || segmentEnd === null) {
      return;
    }
    if (audio.currentTime >= segmentEnd) {
      audio.pause();
      segmentEndRef.current = null;
    }
  }

  return (
    <section className="results" aria-label="분석 결과">
      <div className="result-header">
        <div>
          <p className="eyebrow">분석 결과</p>
          <h2>소리 이벤트 타임라인</h2>
        </div>
        <span>{analysis.usage.processing_time_ms} ms</span>
      </div>

      <div className="waveform-panel">
        <div className="waveform-toolbar">
          <div>
            <h3>녹음 파형</h3>
            <p>
              구간을 누르면 해당 위치부터 재생합니다.
            </p>
          </div>
          <span>{formatTime(durationSec)}</span>
        </div>

        <div className="waveform-stage" aria-label="녹음 파형">
          <canvas ref={canvasRef} className="waveform-canvas" aria-hidden="true" />
          {analysis.sound_events.map((event, index) => (
            <button
              key={`${event.label}-${event.start_time_sec}-${event.end_time_sec}-${index}`}
              type="button"
              className="event-overlay"
              style={eventOverlayStyle(event, durationSec)}
              disabled={!canPlaySegment}
              onClick={() => playSegment(event)}
              aria-label={`${event.label} 구간 재생 ${formatTime(event.start_time_sec)}-${formatTime(
                event.end_time_sec,
              )}`}
              title={`${event.label} ${formatTime(event.start_time_sec)}-${formatTime(
                event.end_time_sec,
              )}`}
            >
              <span>{event.label}</span>
            </button>
          ))}
        </div>

        {hasDecodedRecordingPlayer ? (
          <div className="recording-player" aria-label="녹음 재생 컨트롤">
            <button
              type="button"
              className="recording-player-button"
              onClick={() => void toggleRecordingPlayback()}
              aria-label={isRecordingPlaying ? '녹음 일시정지' : '녹음 재생'}
            >
              {isRecordingPlaying ? <Pause size={18} aria-hidden="true" /> : <Play size={18} aria-hidden="true" />}
            </button>
            <span className="recording-player-time">{formatTime(recordingPlaybackSec)}</span>
            <input
              type="range"
              aria-label="녹음 재생 위치"
              min="0"
              max={audioBufferRef.current?.duration ?? durationSec}
              step="0.01"
              value={Math.min(recordingPlaybackSec, audioBufferRef.current?.duration ?? durationSec)}
              onChange={handleRecordingSeek}
            />
            <span className="recording-player-time">{formatTime(audioBufferRef.current?.duration ?? durationSec)}</span>
          </div>
        ) : audioUrl ? (
          <audio
            ref={audioRef}
            src={audioUrl}
            controls
            preload="auto"
            className="audio-player"
            aria-label="브라우저 오디오 컨트롤"
            onLoadedMetadata={(event) => setAudioDurationSec(event.currentTarget.duration)}
            onPlay={handleNativeAudioPlay}
            onPlaying={handleNativeAudioPlay}
            onTimeUpdate={handleTimeUpdate}
          >
            <track kind="captions" />
          </audio>
        ) : (
          <p className="waveform-note">
            {recordingFile ? '오디오 컨트롤을 준비하는 중입니다.' : '녹음 파일이 없어 파형 재생을 사용할 수 없습니다.'}
          </p>
        )}
        {playbackError && (
          <div className="waveform-error" role="alert">
            <AlertTriangle size={16} aria-hidden="true" />
            {playbackError}
          </div>
        )}
      </div>

      {analysis.sound_events.length ? (
        <ol className="event-list">
          {analysis.sound_events.map((event, index) => (
            <li key={`${event.label}-${event.start_time_sec}-${index}`}>
              <EventRow event={event} onPlay={() => playSegment(event)} canPlay={canPlaySegment} />
            </li>
          ))}
        </ol>
      ) : (
        <div className="empty-state">
          <CheckCircle2 size={20} aria-hidden="true" />
          감지된 소리 이벤트가 없습니다.
        </div>
      )}

      {analysis.speech_segments.length > 0 && (
        <section className="subsection">
          <h3>전사</h3>
          {analysis.speech_segments.map((segment, index) => (
            <p key={`${segment.start_time_sec}-${index}`}>
              <strong>{segment.speaker_name || segment.speaker || 'Speaker'}</strong>
              {' '}
              {segment.transcript}
            </p>
          ))}
        </section>
      )}

      {analysis.audio_insights && (
        <section className="subsection">
          <h3>Audio Insights</h3>
          <p>{analysis.audio_insights.situation_summary || '요약 정보가 없습니다.'}</p>
        </section>
      )}
    </section>
  );
}

function EventRow({
  event,
  onPlay,
  canPlay,
}: {
  event: SoundEvent;
  onPlay: () => void;
  canPlay: boolean;
}) {
  const confidence =
    typeof event.confidence === 'number' ? `${Math.round(event.confidence * 100)}%` : 'N/A';

  return (
    <div className="event-row">
      <span className="time-range">
        {event.start_time_sec.toFixed(1)}s - {event.end_time_sec.toFixed(1)}s
      </span>
      <span className="event-label">{event.label}</span>
      <span className="confidence">{confidence}</span>
      <button type="button" className="play-segment" onClick={onPlay} disabled={!canPlay}>
        구간 재생
      </button>
    </div>
  );
}

function formatDuration(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes.toString().padStart(2, '0')}:${seconds.toString().padStart(2, '0')}`;
}

function resolveDurationSec(analysis: AnalysisResponse, audioDurationSec: number | null): number {
  const candidates = [
    analysis.recording.duration_sec,
    analysis.usage.audio_duration_sec,
    audioDurationSec,
    ...analysis.sound_events.map((event) => event.end_time_sec),
  ].filter((value): value is number => typeof value === 'number' && Number.isFinite(value));

  return Math.max(1, ...candidates);
}

function drawWaveform(canvas: HTMLCanvasElement, peaks: number[]) {
  const context = canvas.getContext('2d');
  if (!context) {
    return;
  }

  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, Math.floor(rect.width || 720));
  const height = Math.max(120, Math.floor(rect.height || 150));
  const scale = window.devicePixelRatio || 1;

  canvas.width = width * scale;
  canvas.height = height * scale;
  context.setTransform(scale, 0, 0, scale, 0, 0);
  context.clearRect(0, 0, width, height);
  context.fillStyle = '#eef4f2';
  context.fillRect(0, 0, width, height);
  context.strokeStyle = '#c8d8d3';
  context.beginPath();
  context.moveTo(0, height / 2);
  context.lineTo(width, height / 2);
  context.stroke();

  const safePeaks = peaks.length ? peaks : Array.from({ length: 160 }, (_, index) => {
    return 0.08 + Math.sin(index / 8) * 0.03;
  });
  const barWidth = width / safePeaks.length;
  context.fillStyle = '#26736d';

  safePeaks.forEach((peak, index) => {
    const barHeight = Math.max(2, peak * (height - 28));
    const x = index * barWidth;
    const y = (height - barHeight) / 2;
    context.fillRect(x, y, Math.max(1, barWidth * 0.72), barHeight);
  });
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}
