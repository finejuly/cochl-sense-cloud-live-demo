import { describe, expect, it } from 'vitest';
import {
  emptyLiveDiagnostics,
  latestLiveTimelineEvents,
  liveTimelineEventsFromResponse,
  mergeLiveTimelineEvents,
  recordLiveDiagnostic,
  renderLiveTimelineEvents,
  resolveLiveViewport,
  type LiveTimelineEvent,
} from './liveTimeline';
import type { LiveChunkAnalysisResponse, SoundEvent } from './types';

function response(
  sequenceId: number,
  events: Array<Pick<SoundEvent, 'start_time_sec' | 'end_time_sec' | 'label' | 'confidence'>>,
): LiveChunkAnalysisResponse {
  return {
    sequence_id: sequenceId,
    window_start_sec: 0,
    window_end_sec: 2,
    sound_events: events,
    processing_time_ms: 12,
  };
}

function event(
  label: string,
  startTimeSec: number,
  endTimeSec: number,
  confidence: number | null = 0.8,
): LiveTimelineEvent {
  return {
    id: `${label}-${startTimeSec}-${endTimeSec}`,
    sequenceId: Math.round(startTimeSec * 10),
    startTimeSec,
    endTimeSec,
    label,
    confidence,
  };
}

describe('liveTimelineEventsFromResponse', () => {
  it('creates events only for confidence at or above the threshold', () => {
    const events = liveTimelineEventsFromResponse(
      response(7, [
        { start_time_sec: 1, end_time_sec: 1.3, label: 'Speech', confidence: 0.82 },
        { start_time_sec: 1.5, end_time_sec: 1.7, label: 'Keyboard', confidence: 0.5 },
        { start_time_sec: 1.8, end_time_sec: 2, label: 'Noise', confidence: 0.49 },
        { start_time_sec: 2.1, end_time_sec: 2.3, label: 'Unknown', confidence: null },
      ]),
      0.5,
    );

    expect(events).toEqual([
      {
        id: 'live-7-0-Speech-1-1.3',
        sequenceId: 7,
        startTimeSec: 1,
        endTimeSec: 1.3,
        label: 'Speech',
        confidence: 0.82,
      },
      {
        id: 'live-7-1-Keyboard-1.5-1.7',
        sequenceId: 7,
        startTimeSec: 1.5,
        endTimeSec: 1.7,
        label: 'Keyboard',
        confidence: 0.5,
      },
    ]);
  });
});

describe('mergeLiveTimelineEvents', () => {
  it('merges duplicate events with overlapping ranges and keeps the widest range and highest confidence', () => {
    const merged = mergeLiveTimelineEvents(
      [event('Cough', 1, 2, 0.72)],
      [{ ...event('Cough', 1.5, 2.5, 0.91), id: 'incoming', sequenceId: 9 }],
    );

    expect(merged).toEqual([
      {
        id: 'Cough-1-2',
        sequenceId: 10,
        startTimeSec: 1,
        endTimeSec: 2.5,
        label: 'Cough',
        confidence: 0.91,
      },
    ]);
  });

  it('merges duplicate events when starts are within 0.75 seconds', () => {
    const merged = mergeLiveTimelineEvents(
      [event('Door knock', 10, 10.2, 0.7)],
      [event('Door knock', 10.7, 11, 0.8)],
    );

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      startTimeSec: 10,
      endTimeSec: 11,
      confidence: 0.8,
    });
  });

  it('chains touching same-label events from consecutive windows into one long bar', () => {
    let merged = mergeLiveTimelineEvents([], [event('Keyboard', 0, 1, 0.8)]);
    merged = mergeLiveTimelineEvents(merged, [event('Keyboard', 1, 2, 0.85)]);
    merged = mergeLiveTimelineEvents(merged, [event('Keyboard', 2, 3, 0.9)]);
    merged = mergeLiveTimelineEvents(merged, [event('Keyboard', 3, 4, 0.7)]);

    expect(merged).toHaveLength(1);
    expect(merged[0]).toMatchObject({
      startTimeSec: 0,
      endTimeSec: 4,
      confidence: 0.9,
    });
  });

  it('keeps same-label events separate when the silence gap exceeds the tolerance', () => {
    const merged = mergeLiveTimelineEvents(
      [event('Keyboard', 0, 1, 0.8)],
      [event('Keyboard', 2, 3, 0.8)],
    );

    expect(merged).toHaveLength(2);
  });

  it('sorts non-duplicate events by start, end, then label', () => {
    const merged = mergeLiveTimelineEvents(
      [event('Speech', 4, 5), event('Alarm', 1, 2)],
      [event('Keyboard', 1, 1.5)],
    );

    expect(merged.map((item) => item.label)).toEqual(['Keyboard', 'Alarm', 'Speech']);
  });
});

describe('renderLiveTimelineEvents', () => {
  it('converts viewport-relative event ranges to percentages', () => {
    const rendered = renderLiveTimelineEvents([event('Speech', 25, 30)], {
      startSec: 20,
      endSec: 40,
    });

    expect(rendered).toHaveLength(1);
    expect(rendered[0]).toMatchObject({
      leftPercent: 25,
      widthPercent: 25,
      lane: 0,
    });
  });

  it('excludes events completely outside the viewport', () => {
    const rendered = renderLiveTimelineEvents([event('Old', 1, 2), event('Future', 50, 51)], {
      startSec: 20,
      endSec: 40,
    });

    expect(rendered).toEqual([]);
  });

  it('assigns as many lanes as needed without hiding overlapping events', () => {
    const rendered = renderLiveTimelineEvents(
      [event('A', 1, 5), event('B', 2, 6), event('C', 3, 7)],
      { startSec: 0, endSec: 10 },
    );

    expect(rendered.map((item) => item.lane)).toEqual([0, 1, 2]);
  });
});

describe('latestLiveTimelineEvents', () => {
  it('returns the newest events by start time without mutating the input', () => {
    const events = [event('A', 1, 2), event('B', 3, 4), event('C', 2, 3)];

    expect(latestLiveTimelineEvents(events, 2).map((item) => item.label)).toEqual(['B', 'C']);
    expect(events.map((item) => item.label)).toEqual(['A', 'B', 'C']);
  });
});

describe('resolveLiveViewport', () => {
  it('follows the latest 20 seconds in auto-follow mode', () => {
    expect(resolveLiveViewport(45, 10, true, 20)).toEqual({
      viewport: { startSec: 25, endSec: 45 },
      maxStartSec: 25,
    });
  });

  it('clamps manual viewport starts into the valid range', () => {
    expect(resolveLiveViewport(45, -3, false, 20).viewport.startSec).toBe(0);
    expect(resolveLiveViewport(45, 90, false, 20).viewport.startSec).toBe(25);
  });
});

describe('recordLiveDiagnostic', () => {
  it('increments counters and formats the latest diagnostic message', () => {
    let diagnostics = emptyLiveDiagnostics();

    diagnostics = recordLiveDiagnostic(diagnostics, 'empty', 3);
    expect(diagnostics).toEqual({
      emptyCount: 1,
      skippedCount: 0,
      failureCount: 0,
      latestMessage: 'EMPTY 3',
    });

    diagnostics = recordLiveDiagnostic(diagnostics, 'skipped', 4);
    expect(diagnostics).toMatchObject({
      emptyCount: 1,
      skippedCount: 1,
      failureCount: 0,
      latestMessage: 'SKIP 4',
    });

    diagnostics = recordLiveDiagnostic(diagnostics, 'failure', 5, ' network down ');
    expect(diagnostics).toMatchObject({
      emptyCount: 1,
      skippedCount: 1,
      failureCount: 1,
      latestMessage: 'FAIL 5: network down',
    });
  });
});
