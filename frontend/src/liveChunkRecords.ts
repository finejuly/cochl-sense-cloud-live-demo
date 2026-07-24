import type { LiveTimelineViewport } from './liveTimeline';
import type { LiveChunkAnalysisResponse, SoundEvent } from './types';

export type LiveChunkStatus = 'PENDING' | 'DETECTED' | 'EMPTY' | 'FAIL' | 'SKIP';

export interface LiveChunkRecord {
  sessionId: string;
  sequenceId: number;
  status: LiveChunkStatus;
  recordingStartedAtMs: number;
  windowStartSec: number;
  windowEndSec: number;
  windowEmittedAtMs: number;
  captureClockDriftMs: number;
  inFlightAtDispatch: number;
  requestStartedAtMs: number | null;
  responseReceivedAtMs: number | null;
  encodeMs: number | null;
  requestMs: number | null;
  backendMs: number | null;
  backendUploadMs: number | null;
  backendProviderMs: number | null;
  backendNormalizationMs: number | null;
  backendCollectionMs: number | null;
  backendTotalMs: number | null;
  windowEndDelayMs: number | null;
  eventCount: number;
  detectedLabels: string[];
  error: string | null;
}

export interface LiveChunkDelayTick {
  seconds: number;
  leftPercent: number;
  label: string | null;
  labelSide: 'right' | 'left';
  ariaLabel: string;
}

export interface LiveChunkRenderRecord extends LiveChunkRecord {
  blockLeftPercent: number;
  blockWidthPercent: number;
  tailLeftPercent: number;
  tailWidthPercent: number;
  delayTicks: LiveChunkDelayTick[];
  lane: number;
  title: string;
  ariaLabel: string;
}

interface LiveChunkRecordInput {
  sessionId: string;
  sequenceId: number;
  recordingStartedAtMs: number;
  windowStartSec: number;
  windowEndSec: number;
  windowEmittedAtMs: number;
  inFlightAtDispatch: number;
}

export function createPendingLiveChunkRecord(input: LiveChunkRecordInput): LiveChunkRecord {
  return {
    ...input,
    status: 'PENDING',
    captureClockDriftMs: captureClockDriftMs(input),
    requestStartedAtMs: null,
    responseReceivedAtMs: null,
    encodeMs: null,
    requestMs: null,
    backendMs: null,
    backendUploadMs: null,
    backendProviderMs: null,
    backendNormalizationMs: null,
    backendCollectionMs: null,
    backendTotalMs: null,
    windowEndDelayMs: null,
    eventCount: 0,
    detectedLabels: [],
    error: null,
  };
}

export function markLiveChunkRequestStarted(
  record: LiveChunkRecord,
  requestStartedAtMs: number,
): LiveChunkRecord {
  return {
    ...record,
    requestStartedAtMs,
    encodeMs: positiveRoundedMs(requestStartedAtMs - record.windowEmittedAtMs),
  };
}

export function createSkippedLiveChunkRecord(input: LiveChunkRecordInput): LiveChunkRecord {
  return {
    ...input,
    status: 'SKIP',
    captureClockDriftMs: captureClockDriftMs(input),
    requestStartedAtMs: null,
    responseReceivedAtMs: null,
    encodeMs: null,
    requestMs: null,
    backendMs: null,
    backendUploadMs: null,
    backendProviderMs: null,
    backendNormalizationMs: null,
    backendCollectionMs: null,
    backendTotalMs: null,
    windowEndDelayMs: null,
    eventCount: 0,
    detectedLabels: [],
    error: null,
  };
}

export function completeLiveChunkRecord(
  record: LiveChunkRecord,
  response: LiveChunkAnalysisResponse,
  threshold: number,
  responseReceivedAtMs: number,
): LiveChunkRecord {
  const detectedEvents = response.sound_events.filter((event) => isDetectedEvent(event, threshold));
  const status: LiveChunkStatus = detectedEvents.length ? 'DETECTED' : 'EMPTY';
  const timings = response.timings;

  return {
    ...record,
    status,
    responseReceivedAtMs,
    requestMs: requestMs(record, responseReceivedAtMs),
    backendMs: positiveRoundedMs(response.processing_time_ms),
    backendUploadMs: optionalRoundedMs(timings?.upload_ms),
    backendProviderMs: optionalRoundedMs(timings?.provider_ms),
    backendNormalizationMs: optionalRoundedMs(timings?.normalization_ms),
    backendCollectionMs: optionalRoundedMs(timings?.collection_ms),
    backendTotalMs: optionalRoundedMs(timings?.total_ms),
    windowEndDelayMs: windowEndDelayMs(record, responseReceivedAtMs),
    eventCount: detectedEvents.length,
    detectedLabels: detectedEvents.map(formatDetectedLabel),
    error: null,
  };
}

export function failLiveChunkRecord(
  record: LiveChunkRecord,
  error: unknown,
  responseReceivedAtMs: number,
): LiveChunkRecord {
  return {
    ...record,
    status: 'FAIL',
    responseReceivedAtMs,
    requestMs: requestMs(record, responseReceivedAtMs),
    backendMs: null,
    backendUploadMs: null,
    backendProviderMs: null,
    backendNormalizationMs: null,
    backendCollectionMs: null,
    backendTotalMs: null,
    windowEndDelayMs: windowEndDelayMs(record, responseReceivedAtMs),
    eventCount: 0,
    detectedLabels: [],
    error: normalizeError(error),
  };
}

export function upsertLiveChunkRecord(
  records: LiveChunkRecord[],
  next: LiveChunkRecord,
): LiveChunkRecord[] {
  const index = records.findIndex((record) => record.sequenceId === next.sequenceId);
  const merged = index === -1
    ? [...records, next]
    : records.map((record, recordIndex) => (recordIndex === index ? next : record));
  return sortRecords(merged);
}

export function retainRecentLiveChunkRecords(
  records: LiveChunkRecord[],
  retentionSec: number,
): LiveChunkRecord[] {
  if (!records.length) {
    return records;
  }

  const safeRetentionSec = Number.isFinite(retentionSec) ? Math.max(0, retentionSec) : 0;
  const latestWindowEndSec = records.reduce(
    (latest, record) => Math.max(latest, record.windowEndSec),
    0,
  );
  const cutoffSec = latestWindowEndSec - safeRetentionSec;
  const retained = records.filter((record) => (
    record.status === 'PENDING' || record.windowEndSec >= cutoffSec
  ));
  return retained.length === records.length ? records : retained;
}

export function renderLiveChunkRecords(
  records: LiveChunkRecord[],
  viewport: LiveTimelineViewport,
  nowMs: number,
): LiveChunkRenderRecord[] {
  const viewportDurationSec = viewport.endSec - viewport.startSec;
  if (viewportDurationSec <= 0) {
    return [];
  }

  const lanes: number[] = [];
  return sortRecords(records)
    .map((record) => {
      const tailEndSec = tailEndSecForRecord(record, nowMs);
      const visualEndSec = Math.max(record.windowEndSec, tailEndSec ?? record.windowEndSec);
      const delayTicks = delayTicksForRecord(record, tailEndSec, viewport, viewportDurationSec);
      return { record, tailEndSec, visualEndSec, delayTicks };
    })
    .filter(({ record, visualEndSec, delayTicks }) => (
      visualEndSec > viewport.startSec && record.windowStartSec < viewport.endSec
    ) || delayTicks.length > 0)
    .map(({ record, tailEndSec, visualEndSec, delayTicks }) => {
      const blockVisibleStartSec = clamp(record.windowStartSec, viewport.startSec, viewport.endSec);
      const blockVisibleEndSec = clamp(record.windowEndSec, blockVisibleStartSec, viewport.endSec);
      const tailVisibleStartSec = tailEndSec === null
        ? record.windowEndSec
        : clamp(record.windowEndSec, viewport.startSec, viewport.endSec);
      const tailVisibleEndSec = tailEndSec === null
        ? tailVisibleStartSec
        : clamp(tailEndSec, tailVisibleStartSec, viewport.endSec);
      const lane = firstAvailableLane(lanes, record.windowStartSec);
      lanes[lane] = visualEndSec;
      const title = chunkDescription(record, nowMs);

      return {
        ...record,
        blockLeftPercent: roundPercentage(((blockVisibleStartSec - viewport.startSec) / viewportDurationSec) * 100),
        blockWidthPercent: roundPercentage(((blockVisibleEndSec - blockVisibleStartSec) / viewportDurationSec) * 100),
        tailLeftPercent: roundPercentage(((tailVisibleStartSec - viewport.startSec) / viewportDurationSec) * 100),
        tailWidthPercent: roundPercentage(((tailVisibleEndSec - tailVisibleStartSec) / viewportDurationSec) * 100),
        delayTicks,
        lane,
        title,
        ariaLabel: title,
      };
    });
}

export function latestLiveChunkTailEndSec(records: LiveChunkRecord[], nowMs: number): number {
  return records.reduce((latest, record) => {
    const tailEndSec = tailEndSecForRecord(record, nowMs);
    return Math.max(latest, record.windowEndSec, tailEndSec ?? 0);
  }, 0);
}

export function hasPendingLiveChunkRecords(records: LiveChunkRecord[]): boolean {
  return records.some((record) => record.status === 'PENDING');
}

export function liveChunkRecordsToCsv(records: LiveChunkRecord[], nowMs: number): string {
  const header = [
    'session_id',
    'sequence_id',
    'status',
    'window_start_sec',
    'window_end_sec',
    'window_emitted_at_iso',
    'capture_clock_drift_ms',
    'in_flight_at_dispatch',
    'request_started_at_iso',
    'response_received_at_iso',
    'encode_ms',
    'request_ms',
    'backend_ms',
    'backend_upload_ms',
    'backend_provider_ms',
    'backend_normalization_ms',
    'backend_collection_ms',
    'backend_total_ms',
    'window_end_delay_ms',
    'event_count',
    'detected_labels',
    'error',
  ];

  const rows = sortRecords(records).map((record) => [
    record.sessionId,
    record.sequenceId,
    record.status,
    formatNumber(record.windowStartSec),
    formatNumber(record.windowEndSec),
    new Date(record.windowEmittedAtMs).toISOString(),
    record.captureClockDriftMs,
    record.inFlightAtDispatch,
    record.requestStartedAtMs === null ? '' : new Date(record.requestStartedAtMs).toISOString(),
    record.responseReceivedAtMs === null ? '' : new Date(record.responseReceivedAtMs).toISOString(),
    csvResolvedMs(record, record.encodeMs),
    csvRequestMs(record),
    csvBackendMs(record),
    csvResolvedMs(record, record.backendUploadMs),
    csvResolvedMs(record, record.backendProviderMs),
    csvResolvedMs(record, record.backendNormalizationMs),
    csvResolvedMs(record, record.backendCollectionMs),
    csvResolvedMs(record, record.backendTotalMs),
    csvWindowEndDelayMs(record, nowMs),
    record.eventCount,
    record.detectedLabels.join('; '),
    record.error ?? '',
  ]);

  return [header, ...rows]
    .map((row) => row.map((field) => escapeCsvField(String(field))).join(','))
    .join('\n');
}

export function liveChunkCsvFilename(sessionId: string): string {
  const safeSessionId = sessionId.replace(/[^A-Za-z0-9._-]/g, '-');
  return `live-chunks-${safeSessionId || 'session'}.csv`;
}

function isDetectedEvent(event: SoundEvent, threshold: number): boolean {
  return typeof event.confidence === 'number' && event.confidence >= threshold;
}

function formatDetectedLabel(event: SoundEvent): string {
  return `${event.label} ${Math.round((event.confidence ?? 0) * 100)}%`;
}

function requestMs(record: LiveChunkRecord, responseReceivedAtMs: number): number | null {
  return record.requestStartedAtMs === null
    ? null
    : positiveRoundedMs(responseReceivedAtMs - record.requestStartedAtMs);
}

function windowEndDelayMs(record: LiveChunkRecord, snapshotMs: number): number {
  // Audio sample time and the system wall clock drift independently over long
  // sessions. Anchor response delay to this window's actual callback instead
  // of projecting every window from the one recording-start wall-clock value.
  return positiveRoundedMs(snapshotMs - record.windowEmittedAtMs);
}

function windowEndMs(
  record: Pick<LiveChunkRecord, 'recordingStartedAtMs' | 'windowEndSec'>,
): number {
  return record.recordingStartedAtMs + record.windowEndSec * 1000;
}

function captureClockDriftMs(record: LiveChunkRecordInput): number {
  return Math.round(record.windowEmittedAtMs - windowEndMs(record));
}

function tailEndSecForRecord(record: LiveChunkRecord, nowMs: number): number | null {
  if (record.status === 'SKIP') {
    return null;
  }
  const snapshotMs = record.status === 'PENDING'
    ? nowMs
    : record.responseReceivedAtMs;
  if (snapshotMs === null) {
    return null;
  }
  return record.windowEndSec + Math.max(0, snapshotMs - record.windowEmittedAtMs) / 1000;
}

function delayTicksForRecord(
  record: LiveChunkRecord,
  tailEndSec: number | null,
  viewport: LiveTimelineViewport,
  viewportDurationSec: number,
): LiveChunkDelayTick[] {
  if (tailEndSec === null) {
    return [];
  }

  const tailElapsedSec = Math.max(0, tailEndSec - record.windowEndSec);
  const fullSeconds = Math.floor(tailElapsedSec);
  if (fullSeconds < 1) {
    return [];
  }

  const visibleTicks: Array<Pick<LiveChunkDelayTick, 'seconds' | 'leftPercent'>> = [];
  for (let seconds = 1; seconds <= fullSeconds; seconds += 1) {
    const positionSec = record.windowEndSec + seconds;
    if (positionSec < viewport.startSec || positionSec > viewport.endSec) {
      continue;
    }
    visibleTicks.push({
      seconds,
      leftPercent: roundPercentage(((positionSec - viewport.startSec) / viewportDurationSec) * 100),
    });
  }

  if (!visibleTicks.length) {
    return [];
  }

  const labelEveryTick = fullSeconds <= 5;
  const latestVisibleSeconds = visibleTicks[visibleTicks.length - 1].seconds;

  return visibleTicks.map((tick) => {
    const shouldLabel = labelEveryTick
      || tick.seconds === 1
      || tick.seconds % 5 === 0
      || tick.seconds === latestVisibleSeconds;
    const label = shouldLabel ? `${tick.seconds}s` : null;

    return {
      ...tick,
      label,
      labelSide: label !== null && tick.leftPercent >= 92 ? 'left' : 'right',
      ariaLabel: `청크 #${record.sequenceId} 지연 ${tick.seconds}초`,
    };
  });
}

function csvRequestMs(record: LiveChunkRecord): string {
  if (record.status === 'PENDING' || record.status === 'SKIP' || record.requestMs === null) {
    return '';
  }
  return String(record.requestMs);
}

function csvBackendMs(record: LiveChunkRecord): string {
  if (record.status === 'PENDING' || record.status === 'SKIP' || record.backendMs === null) {
    return '';
  }
  return String(record.backendMs);
}

function csvResolvedMs(record: LiveChunkRecord, value: number | null): string {
  if (record.status === 'PENDING' || record.status === 'SKIP' || value === null) {
    return '';
  }
  return String(value);
}

function csvWindowEndDelayMs(record: LiveChunkRecord, nowMs: number): string {
  if (record.status === 'SKIP') {
    return '';
  }
  if (record.status === 'PENDING') {
    return String(windowEndDelayMs(record, nowMs));
  }
  return record.windowEndDelayMs === null ? '' : String(record.windowEndDelayMs);
}

function chunkDescription(record: LiveChunkRecord, nowMs: number): string {
  const details = [
    `청크 #${record.sequenceId}`,
    record.status,
    `${formatSeconds(record.windowStartSec)}-${formatSeconds(record.windowEndSec)}`,
  ];

  const currentWindowEndDelayMs = record.status === 'PENDING'
    ? windowEndDelayMs(record, nowMs)
    : record.windowEndDelayMs;
  if (record.requestMs !== null) {
    details.push(`요청 ${formatLatency(record.requestMs)}`);
  }
  if (record.backendMs !== null) {
    details.push(`서버 ${formatLatency(record.backendMs)}`);
  }
  if (record.backendTotalMs !== null && record.backendTotalMs !== record.backendMs) {
    details.push(`서버 전체 ${formatLatency(record.backendTotalMs)}`);
  }
  if (currentWindowEndDelayMs !== null) {
    details.push(`윈도우 종료 후 ${formatLatency(currentWindowEndDelayMs)}`);
  }
  if (Math.abs(record.captureClockDriftMs) >= 100) {
    details.push(`캡처 시계 차이 ${formatSignedLatency(record.captureClockDriftMs)}`);
  }
  if (record.detectedLabels.length) {
    details.push(record.detectedLabels.join(', '));
  }
  if (record.error) {
    details.push(record.error);
  }

  return details.join(', ');
}

function normalizeError(error: unknown): string | null {
  const message = error instanceof Error ? error.message : String(error ?? '');
  const trimmed = message.trim();
  return trimmed || null;
}

function sortRecords(records: LiveChunkRecord[]): LiveChunkRecord[] {
  return [...records].sort((left, right) => left.sequenceId - right.sequenceId);
}

function firstAvailableLane(lanes: number[], startTimeSec: number): number {
  const lane = lanes.findIndex((endTimeSec) => endTimeSec <= startTimeSec);
  return lane === -1 ? lanes.length : lane;
}

function positiveRoundedMs(value: number): number {
  return Math.max(0, Math.round(value));
}

function optionalRoundedMs(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value)
    ? positiveRoundedMs(value)
    : null;
}

function formatSeconds(value: number): string {
  return `${formatNumber(value)}s`;
}

function formatLatency(milliseconds: number): string {
  return `${(milliseconds / 1000).toFixed(2)}초`;
}

function formatSignedLatency(milliseconds: number): string {
  const sign = milliseconds >= 0 ? '+' : '-';
  return `${sign}${formatLatency(Math.abs(milliseconds))}`;
}

function formatNumber(value: number): string {
  return Number(value.toFixed(3)).toString();
}

function escapeCsvField(field: string): string {
  return /[",\n\r]/.test(field) ? `"${field.replace(/"/g, '""')}"` : field;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function roundPercentage(value: number): number {
  return Number(value.toFixed(3));
}
