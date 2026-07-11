import { describe, expect, it } from 'vitest';
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
  renderLiveChunkRecords,
  retainRecentLiveChunkRecords,
  upsertLiveChunkRecord,
  type LiveChunkRecord,
} from './liveChunkRecords';
import type { LiveChunkAnalysisResponse, SoundEvent } from './types';

const RECORDING_STARTED_AT_MS = Date.UTC(2026, 0, 1, 0, 0, 0);

function pending(overrides: Partial<LiveChunkRecord> = {}): LiveChunkRecord {
  return {
    ...createPendingLiveChunkRecord({
      sessionId: 'session:1',
      sequenceId: 1,
      recordingStartedAtMs: RECORDING_STARTED_AT_MS,
      windowStartSec: 0,
      windowEndSec: 2,
    }),
    ...overrides,
  };
}

function response(
  sequenceId: number,
  events: Array<Pick<SoundEvent, 'label' | 'confidence'> & Partial<Pick<SoundEvent, 'start_time_sec' | 'end_time_sec'>>>,
  overrides: Partial<LiveChunkAnalysisResponse> = {},
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
    processing_time_ms: 42.4,
    ...overrides,
  };
}

describe('live chunk record lifecycle', () => {
  it('creates pending records before request start and marks request start later', () => {
    const record = pending();

    expect(record).toMatchObject({
      status: 'PENDING',
      requestStartedAtMs: null,
      responseReceivedAtMs: null,
      requestMs: null,
      backendMs: null,
    });

    expect(markLiveChunkRequestStarted(record, RECORDING_STARTED_AT_MS + 100)).toMatchObject({
      status: 'PENDING',
      requestStartedAtMs: RECORDING_STARTED_AT_MS + 100,
    });
  });

  it('completes detected and empty responses at the threshold', () => {
    const requested = markLiveChunkRequestStarted(pending(), RECORDING_STARTED_AT_MS + 2100);
    const detected = completeLiveChunkRecord(
      requested,
      response(1, [
        { label: 'Cough', confidence: 0.93 },
        { label: 'Noise', confidence: 0.49 },
        { label: 'Keyboard', confidence: 0.5 },
        { label: 'Unknown', confidence: null },
      ]),
      0.5,
      RECORDING_STARTED_AT_MS + 3400,
    );
    const empty = completeLiveChunkRecord(
      requested,
      response(1, [{ label: 'Noise', confidence: 0.49 }]),
      0.5,
      RECORDING_STARTED_AT_MS + 3500,
    );

    expect(detected).toMatchObject({
      status: 'DETECTED',
      requestMs: 1300,
      backendMs: 42,
      windowEndDelayMs: 1400,
      eventCount: 2,
      detectedLabels: ['Cough 93%', 'Keyboard 50%'],
    });
    expect(empty).toMatchObject({
      status: 'EMPTY',
      eventCount: 0,
      detectedLabels: [],
    });
  });

  it('preserves frontend record identity even when response echo fields differ', () => {
    const record = markLiveChunkRequestStarted(
      pending({ sequenceId: 12, windowStartSec: 20, windowEndSec: 22 }),
      RECORDING_STARTED_AT_MS + 20_050,
    );

    const completed = completeLiveChunkRecord(
      record,
      response(99, [{ label: 'Speech', confidence: 0.9 }], {
        window_start_sec: 100,
        window_end_sec: 102,
      }),
      0.5,
      RECORDING_STARTED_AT_MS + 23_100,
    );

    expect(completed).toMatchObject({
      sessionId: 'session:1',
      sequenceId: 12,
      windowStartSec: 20,
      windowEndSec: 22,
      windowEndDelayMs: 1100,
    });
  });

  it('records failures and skipped rows without losing identity', () => {
    const record = markLiveChunkRequestStarted(
      pending({ sequenceId: 3, windowStartSec: 4, windowEndSec: 6 }),
      RECORDING_STARTED_AT_MS + 4500,
    );
    const failed = failLiveChunkRecord(
      record,
      new Error(' network, "down" '),
      RECORDING_STARTED_AT_MS + 7100,
    );
    const skipped = createSkippedLiveChunkRecord({
      sessionId: 'session:2',
      sequenceId: 4,
      recordingStartedAtMs: RECORDING_STARTED_AT_MS,
      windowStartSec: 6,
      windowEndSec: 8,
    });

    expect(failed).toMatchObject({
      sequenceId: 3,
      windowStartSec: 4,
      windowEndSec: 6,
      status: 'FAIL',
      requestMs: 2600,
      backendMs: null,
      windowEndDelayMs: 1100,
      error: 'network, "down"',
    });
    expect(skipped).toMatchObject({
      status: 'SKIP',
      requestStartedAtMs: null,
      responseReceivedAtMs: null,
      requestMs: null,
      windowEndDelayMs: null,
    });
  });

  it('upserts by sequence id and sorts rows', () => {
    const first = pending({ sequenceId: 2 });
    const second = pending({ sequenceId: 1 });
    const replacement = createSkippedLiveChunkRecord({
      sessionId: first.sessionId,
      sequenceId: 2,
      recordingStartedAtMs: first.recordingStartedAtMs,
      windowStartSec: first.windowStartSec,
      windowEndSec: first.windowEndSec,
    });

    const records = upsertLiveChunkRecord(
      upsertLiveChunkRecord(upsertLiveChunkRecord([], first), second),
      replacement,
    );

    expect(records.map((record) => [record.sequenceId, record.status])).toEqual([
      [1, 'PENDING'],
      [2, 'SKIP'],
    ]);
  });

  it('retains one hour of completed rows without ever evicting pending requests', () => {
    const skipped = (sequenceId: number, windowStartSec: number, windowEndSec: number) => (
      createSkippedLiveChunkRecord({
        sessionId: 'session:long',
        sequenceId,
        recordingStartedAtMs: RECORDING_STARTED_AT_MS,
        windowStartSec,
        windowEndSec,
      })
    );
    const records = [
      skipped(1, 0, 2),
      pending({ sequenceId: 2, windowStartSec: 1, windowEndSec: 3 }),
      skipped(3, 400, 402),
      skipped(4, 4000, 4002),
    ];

    const retained = retainRecentLiveChunkRecords(records, 3600);

    expect(retained.map((record) => record.sequenceId)).toEqual([2, 3, 4]);
  });
});

describe('live chunk render helpers', () => {
  it('renders viewport-relative blocks and pending tails from now', () => {
    const rendered = renderLiveChunkRecords(
      [pending({ windowStartSec: 10, windowEndSec: 12 })],
      { startSec: 8, endSec: 18 },
      RECORDING_STARTED_AT_MS + 15_000,
    );

    expect(rendered).toHaveLength(1);
    expect(rendered[0]).toMatchObject({
      blockLeftPercent: 20,
      blockWidthPercent: 20,
      tailLeftPercent: 40,
      tailWidthPercent: 30,
      lane: 0,
    });
    expect(rendered[0].ariaLabel).toContain('PENDING');
  });

  it('adds one-second delay ticks for pending tails from now', () => {
    const rendered = renderLiveChunkRecords(
      [pending({ windowStartSec: 10, windowEndSec: 12 })],
      { startSec: 8, endSec: 18 },
      RECORDING_STARTED_AT_MS + 16_300,
    );

    expect(rendered[0].delayTicks.map((tick) => ({
      seconds: tick.seconds,
      leftPercent: tick.leftPercent,
      label: tick.label,
      labelSide: tick.labelSide,
    }))).toEqual([
      { seconds: 1, leftPercent: 50, label: '1s', labelSide: 'right' },
      { seconds: 2, leftPercent: 60, label: '2s', labelSide: 'right' },
      { seconds: 3, leftPercent: 70, label: '3s', labelSide: 'right' },
      { seconds: 4, leftPercent: 80, label: '4s', labelSide: 'right' },
    ]);
    expect(rendered[0].delayTicks[2].ariaLabel).toBe('청크 #1 지연 3초');
  });

  it('uses response time instead of now for resolved delay ticks', () => {
    const resolved = completeLiveChunkRecord(
      markLiveChunkRequestStarted(
        pending({ windowStartSec: 10, windowEndSec: 12 }),
        RECORDING_STARTED_AT_MS + 12_050,
      ),
      response(1, []),
      0.5,
      RECORDING_STARTED_AT_MS + 15_200,
    );

    const rendered = renderLiveChunkRecords(
      [resolved],
      { startSec: 8, endSec: 18 },
      RECORDING_STARTED_AT_MS + 30_000,
    );

    expect(rendered[0].delayTicks.map((tick) => tick.seconds)).toEqual([1, 2, 3]);
  });

  it('clips delay ticks to the visible viewport including exact boundaries', () => {
    const rendered = renderLiveChunkRecords(
      [pending({ windowStartSec: 10, windowEndSec: 12 })],
      { startSec: 14.5, endSec: 16.5 },
      RECORDING_STARTED_AT_MS + 16_300,
    );

    expect(rendered[0].delayTicks.map((tick) => ({
      seconds: tick.seconds,
      leftPercent: tick.leftPercent,
      label: tick.label,
    }))).toEqual([
      { seconds: 3, leftPercent: 25, label: '3s' },
      { seconds: 4, leftPercent: 75, label: '4s' },
    ]);
  });

  it('keeps records visible when only a viewport-start delay tick is visible', () => {
    const rendered = renderLiveChunkRecords(
      [pending({ windowStartSec: 0, windowEndSec: 2 })],
      { startSec: 3, endSec: 6 },
      RECORDING_STARTED_AT_MS + 3000,
    );

    expect(rendered).toHaveLength(1);
    expect(rendered[0].delayTicks).toMatchObject([
      { seconds: 1, leftPercent: 0, label: '1s' },
    ]);
  });

  it('omits delay ticks for short tails and skipped chunks', () => {
    const skipped = createSkippedLiveChunkRecord({
      sessionId: 'session:1',
      sequenceId: 2,
      recordingStartedAtMs: RECORDING_STARTED_AT_MS,
      windowStartSec: 0,
      windowEndSec: 2,
    });

    const rendered = renderLiveChunkRecords(
      [pending({ sequenceId: 1, windowStartSec: 0, windowEndSec: 2 }), skipped],
      { startSec: 0, endSec: 4 },
      RECORDING_STARTED_AT_MS + 2800,
    );

    expect(rendered.map((record) => [record.sequenceId, record.delayTicks])).toEqual([
      [1, []],
      [2, []],
    ]);
  });

  it('labels long delay tails compactly', () => {
    const rendered = renderLiveChunkRecords(
      [pending({ windowStartSec: 0, windowEndSec: 2 })],
      { startSec: 0, endSec: 12 },
      RECORDING_STARTED_AT_MS + 11_000,
    );

    expect(rendered[0].delayTicks.map((tick) => tick.seconds)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9]);
    expect(rendered[0].delayTicks.map((tick) => tick.label)).toEqual([
      '1s',
      null,
      null,
      null,
      '5s',
      null,
      null,
      null,
      '9s',
    ]);
  });

  it('labels the latest visible tick in clipped long delay tails', () => {
    const rendered = renderLiveChunkRecords(
      [pending({ windowStartSec: 0, windowEndSec: 2 })],
      { startSec: 8, endSec: 9 },
      RECORDING_STARTED_AT_MS + 11_000,
    );

    expect(rendered[0].delayTicks.map((tick) => ({
      seconds: tick.seconds,
      label: tick.label,
    }))).toEqual([
      { seconds: 6, label: null },
      { seconds: 7, label: '7s' },
    ]);
  });

  it('places labels leftward near the right edge', () => {
    const rendered = renderLiveChunkRecords(
      [pending({ windowStartSec: 0, windowEndSec: 2 })],
      { startSec: 1, endSec: 11 },
      RECORDING_STARTED_AT_MS + 11_000,
    );

    const tickFive = rendered[0].delayTicks.find((tick) => tick.seconds === 5);
    const tickNine = rendered[0].delayTicks.find((tick) => tick.seconds === 9);
    expect(tickFive).toMatchObject({ leftPercent: 60, label: '5s', labelSide: 'right' });
    expect(tickNine).toMatchObject({ leftPercent: 100, label: '9s', labelSide: 'left' });
  });

  it('clamps tails at viewport edges and assigns lanes by the full visual span', () => {
    const slowFirst = completeLiveChunkRecord(
      markLiveChunkRequestStarted(pending({ sequenceId: 1, windowStartSec: 0, windowEndSec: 2 }), RECORDING_STARTED_AT_MS),
      response(1, []),
      0.5,
      RECORDING_STARTED_AT_MS + 6200,
    );
    const second = completeLiveChunkRecord(
      markLiveChunkRequestStarted(pending({ sequenceId: 2, windowStartSec: 3, windowEndSec: 5 }), RECORDING_STARTED_AT_MS + 3000),
      response(2, []),
      0.5,
      RECORDING_STARTED_AT_MS + 5200,
    );

    const rendered = renderLiveChunkRecords([slowFirst, second], { startSec: 4, endSec: 6 }, RECORDING_STARTED_AT_MS + 7000);

    expect(rendered.map((record) => record.lane)).toEqual([0, 1]);
    expect(rendered[0]).toMatchObject({
      blockWidthPercent: 0,
      tailLeftPercent: 0,
      tailWidthPercent: 100,
    });
  });

  it('extends duration for pending and resolved tails', () => {
    const resolved = completeLiveChunkRecord(
      markLiveChunkRequestStarted(pending({ sequenceId: 1, windowStartSec: 0, windowEndSec: 2 }), RECORDING_STARTED_AT_MS),
      response(1, []),
      0.5,
      RECORDING_STARTED_AT_MS + 5100,
    );
    const waiting = pending({ sequenceId: 2, windowStartSec: 4, windowEndSec: 6 });

    expect(latestLiveChunkTailEndSec([resolved, waiting], RECORDING_STARTED_AT_MS + 8000)).toBe(8);
  });

  it('reports pending state from helpers', () => {
    const requested = pending();
    const completed = completeLiveChunkRecord(requested, response(1, []), 0.5, RECORDING_STARTED_AT_MS + 3000);

    expect(hasPendingLiveChunkRecords([requested])).toBe(true);
    expect(hasPendingLiveChunkRecords([completed])).toBe(false);
  });
});

describe('live chunk CSV helpers', () => {
  it('serializes rows, pending delay snapshots, and escaped CSV fields', () => {
    const detected = completeLiveChunkRecord(
      markLiveChunkRequestStarted(pending({ sequenceId: 2 }), RECORDING_STARTED_AT_MS + 100),
      response(2, [{ label: 'Door, "bell"', confidence: 0.91 }]),
      0.5,
      RECORDING_STARTED_AT_MS + 2600,
    );
    const failed = failLiveChunkRecord(
      markLiveChunkRequestStarted(pending({ sequenceId: 3 }), RECORDING_STARTED_AT_MS + 100),
      'line one\nline two',
      RECORDING_STARTED_AT_MS + 2900,
    );
    const waiting = pending({ sequenceId: 1 });
    const skipped = createSkippedLiveChunkRecord({
      sessionId: 'session:1',
      sequenceId: 4,
      recordingStartedAtMs: RECORDING_STARTED_AT_MS,
      windowStartSec: 3,
      windowEndSec: 5,
    });

    const csv = liveChunkRecordsToCsv(
      [detected, failed, waiting, skipped],
      RECORDING_STARTED_AT_MS + 4100,
    );

    expect(csv.split('\n')[0]).toBe(
      'session_id,sequence_id,status,window_start_sec,window_end_sec,request_started_at_iso,response_received_at_iso,request_ms,backend_ms,window_end_delay_ms,event_count,detected_labels,error',
    );
    expect(csv).toContain('session:1,1,PENDING,0,2,,,,,2100,0,,');
    expect(csv).toContain('session:1,2,DETECTED,0,2,2026-01-01T00:00:00.100Z,2026-01-01T00:00:02.600Z,2500,42,600,1,"Door, ""bell"" 91%",');
    expect(csv).toContain('session:1,3,FAIL,0,2,2026-01-01T00:00:00.100Z,2026-01-01T00:00:02.900Z,2800,,900,0,,"line one\nline two"');
    expect(csv).toContain('session:1,4,SKIP,3,5,,,,,,0,,');
  });

  it('sanitizes CSV filenames', () => {
    expect(liveChunkCsvFilename('session:one/two')).toBe('live-chunks-session-one-two.csv');
    expect(liveChunkCsvFilename('')).toBe('live-chunks-session.csv');
  });
});
