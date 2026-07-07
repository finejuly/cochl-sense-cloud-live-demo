import type { LiveChunkAnalysisResponse } from './types';

export interface LiveTimelineEvent {
  id: string;
  sequenceId: number;
  startTimeSec: number;
  endTimeSec: number;
  label: string;
  confidence: number | null;
}

export interface LiveDiagnostics {
  emptyCount: number;
  skippedCount: number;
  failureCount: number;
  latestMessage: string | null;
}

export type LiveDiagnosticKind = 'empty' | 'skipped' | 'failure';

export interface LiveTimelineViewport {
  startSec: number;
  endSec: number;
}

export interface LiveTimelineRenderEvent extends LiveTimelineEvent {
  leftPercent: number;
  widthPercent: number;
  lane: number;
}

export interface LiveViewportState {
  viewport: LiveTimelineViewport;
  maxStartSec: number;
}

export function emptyLiveDiagnostics(): LiveDiagnostics {
  return {
    emptyCount: 0,
    skippedCount: 0,
    failureCount: 0,
    latestMessage: null,
  };
}

export function recordLiveDiagnostic(
  current: LiveDiagnostics,
  kind: LiveDiagnosticKind,
  sequenceId: number,
  reason?: string,
): LiveDiagnostics {
  if (kind === 'empty') {
    return {
      ...current,
      emptyCount: current.emptyCount + 1,
      latestMessage: `EMPTY ${sequenceId}`,
    };
  }
  if (kind === 'skipped') {
    return {
      ...current,
      skippedCount: current.skippedCount + 1,
      latestMessage: `SKIP ${sequenceId}`,
    };
  }

  const suffix = reason?.trim() ? `: ${reason.trim()}` : '';
  return {
    ...current,
    failureCount: current.failureCount + 1,
    latestMessage: `FAIL ${sequenceId}${suffix}`,
  };
}

export function liveTimelineEventsFromResponse(
  response: LiveChunkAnalysisResponse,
  threshold: number,
): LiveTimelineEvent[] {
  return response.sound_events
    .map((event, index) => ({ event, index }))
    .filter(({ event }) => typeof event.confidence === 'number' && event.confidence >= threshold)
    .map(({ event, index }) => ({
      id: `live-${response.sequence_id}-${index}-${event.label}-${event.start_time_sec}-${event.end_time_sec}`,
      sequenceId: response.sequence_id,
      startTimeSec: event.start_time_sec,
      endTimeSec: event.end_time_sec,
      label: event.label,
      confidence: event.confidence,
    }));
}

export function mergeLiveTimelineEvents(
  current: LiveTimelineEvent[],
  incoming: LiveTimelineEvent[],
): LiveTimelineEvent[] {
  let merged = sortEvents([...current]);

  incoming.forEach((event) => {
    const duplicateIndex = merged.findIndex((existing) => areDuplicateEvents(existing, event));
    if (duplicateIndex === -1) {
      merged.push(event);
      merged = sortEvents(merged);
      return;
    }

    merged[duplicateIndex] = mergeEvent(merged[duplicateIndex], event);
    merged = mergeTransitiveDuplicates(sortEvents(merged));
  });

  return sortEvents(merged);
}

export function renderLiveTimelineEvents(
  events: LiveTimelineEvent[],
  viewport: LiveTimelineViewport,
): LiveTimelineRenderEvent[] {
  const viewportDurationSec = viewport.endSec - viewport.startSec;
  if (viewportDurationSec <= 0) {
    return [];
  }

  const lanes: number[] = [];
  return sortEvents(events)
    .filter((event) => event.endTimeSec > viewport.startSec && event.startTimeSec < viewport.endSec)
    .map((event) => {
      const visibleStartSec = clamp(event.startTimeSec, viewport.startSec, viewport.endSec);
      const visibleEndSec = clamp(event.endTimeSec, visibleStartSec, viewport.endSec);
      const lane = firstAvailableLane(lanes, event.startTimeSec);
      lanes[lane] = event.endTimeSec;
      return {
        ...event,
        leftPercent: roundPercentage(((visibleStartSec - viewport.startSec) / viewportDurationSec) * 100),
        widthPercent: roundPercentage(((visibleEndSec - visibleStartSec) / viewportDurationSec) * 100),
        lane,
      };
    });
}

export function latestLiveTimelineEvents(events: LiveTimelineEvent[], count: number): LiveTimelineEvent[] {
  return [...events]
    .sort((left, right) => right.startTimeSec - left.startTimeSec || right.endTimeSec - left.endTimeSec)
    .slice(0, Math.max(0, count));
}

export function resolveLiveViewport(
  durationSec: number,
  manualStartSec: number,
  autoFollow: boolean,
  viewportSec: number,
): LiveViewportState {
  const safeViewportSec = Math.max(0, viewportSec);
  const maxStartSec = Math.max(0, durationSec - safeViewportSec);
  const startSec = autoFollow ? maxStartSec : clamp(manualStartSec, 0, maxStartSec);
  return {
    viewport: {
      startSec,
      endSec: startSec + safeViewportSec,
    },
    maxStartSec,
  };
}

function mergeTransitiveDuplicates(events: LiveTimelineEvent[]): LiveTimelineEvent[] {
  const merged: LiveTimelineEvent[] = [];
  events.forEach((event) => {
    const duplicateIndex = merged.findIndex((existing) => areDuplicateEvents(existing, event));
    if (duplicateIndex === -1) {
      merged.push(event);
      return;
    }
    merged[duplicateIndex] = mergeEvent(merged[duplicateIndex], event);
  });
  return merged;
}

// Cochl reports sustained sounds as consecutive ~1s result rows, so same-label
// events that touch (or nearly touch) must chain into one continuous bar.
// Real pauses surface as >= 1s of absence, which stays above this tolerance.
const MERGE_GAP_TOLERANCE_SEC = 0.75;

function areDuplicateEvents(left: LiveTimelineEvent, right: LiveTimelineEvent): boolean {
  if (left.label !== right.label) {
    return false;
  }
  return (
    left.startTimeSec <= right.endTimeSec + MERGE_GAP_TOLERANCE_SEC
    && right.startTimeSec <= left.endTimeSec + MERGE_GAP_TOLERANCE_SEC
  );
}

function mergeEvent(left: LiveTimelineEvent, right: LiveTimelineEvent): LiveTimelineEvent {
  return {
    ...left,
    sequenceId: Math.max(left.sequenceId, right.sequenceId),
    startTimeSec: Math.min(left.startTimeSec, right.startTimeSec),
    endTimeSec: Math.max(left.endTimeSec, right.endTimeSec),
    confidence: highestConfidence(left.confidence, right.confidence),
  };
}

function highestConfidence(left: number | null, right: number | null): number | null {
  if (left === null) {
    return right;
  }
  if (right === null) {
    return left;
  }
  return Math.max(left, right);
}

function sortEvents(events: LiveTimelineEvent[]): LiveTimelineEvent[] {
  return [...events].sort((left, right) => (
    left.startTimeSec - right.startTimeSec
    || left.endTimeSec - right.endTimeSec
    || left.label.localeCompare(right.label)
  ));
}

function firstAvailableLane(lanes: number[], startTimeSec: number): number {
  const lane = lanes.findIndex((endTimeSec) => endTimeSec <= startTimeSec);
  return lane === -1 ? lanes.length : lane;
}

function clamp(value: number, min: number, max: number): number {
  return Math.min(max, Math.max(min, value));
}

function roundPercentage(value: number): number {
  return Number(value.toFixed(3));
}
