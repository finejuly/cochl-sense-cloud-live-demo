import {
  Fragment,
  type CSSProperties,
  type ChangeEvent,
  type RefObject,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import { Download, ExternalLink } from 'lucide-react';
import type { LiveSpectrogramFrame } from './liveAudio';
import {
  renderLiveChunkRecords,
  type LiveChunkRecord,
  type LiveChunkStatus,
} from './liveChunkRecords';
import {
  latestLiveTimelineEvents,
  renderLiveTimelineEvents,
  type LiveDiagnostics,
  type LiveTimelineEvent,
  type LiveTimelineViewport,
} from './liveTimeline';
import { formatTime } from './waveform';

interface LiveSpectrogramPanelProps {
  frames: LiveSpectrogramFrame[];
  framesRef?: RefObject<LiveSpectrogramFrame[]>;
  frameVersion: number;
  active?: boolean;
  events: LiveTimelineEvent[];
  diagnostics: LiveDiagnostics;
  viewport: LiveTimelineViewport;
  totalDurationSec: number;
  maxViewportStartSec: number;
  autoFollow: boolean;
  historyLimit: number;
  chunkRecords: LiveChunkRecord[];
  chunkSnapshotMs: number;
  canDownloadCsv: boolean;
  onDownloadCsv: () => void;
  onOpenCsv: () => void;
  onViewportStartChange: (startSec: number) => void;
  onJumpToLatest: () => void;
}

const MARKER_LANE_HEIGHT = 34;
const CHUNK_LANE_HEIGHT = 24;

export function LiveSpectrogramPanel({
  frames,
  framesRef,
  frameVersion,
  active = false,
  events,
  diagnostics,
  viewport,
  totalDurationSec,
  maxViewportStartSec,
  autoFollow,
  historyLimit,
  chunkRecords,
  chunkSnapshotMs,
  canDownloadCsv,
  onDownloadCsv,
  onOpenCsv,
  onViewportStartChange,
  onJumpToLatest,
}: LiveSpectrogramPanelProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null);
  const renderEvents = useMemo(() => renderLiveTimelineEvents(events, viewport), [events, viewport]);
  const renderChunks = useMemo(
    () => renderLiveChunkRecords(chunkRecords, viewport, chunkSnapshotMs),
    [chunkRecords, chunkSnapshotMs, viewport],
  );
  const history = useMemo(() => latestLiveTimelineEvents(events, historyLimit), [events, historyLimit]);
  const selectedEvent = selectedEventId
    ? events.find((event) => event.id === selectedEventId) ?? null
    : null;
  const viewportDurationSec = Math.max(0.001, viewport.endSec - viewport.startSec);
  const laneCount = renderEvents.length
    ? Math.max(...renderEvents.map((event) => event.lane)) + 1
    : 0;
  const chunkLaneCount = renderChunks.length
    ? Math.max(...renderChunks.map((chunk) => chunk.lane)) + 1
    : 0;
  const markerLayerHeight = laneCount ? laneCount * MARKER_LANE_HEIGHT + 6 : 0;
  const chunkLaneHeight = chunkLaneCount ? chunkLaneCount * CHUNK_LANE_HEIGHT + 6 : 0;
  const timeTicks = useMemo(() => {
    const ticks: number[] = [];
    const firstTick = Math.ceil(viewport.startSec / 5) * 5;
    for (let tick = firstTick; tick <= viewport.endSec; tick += 5) {
      ticks.push(tick);
    }
    return ticks;
  }, [viewport.endSec, viewport.startSec]);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) {
      return;
    }

    const drawCurrentFrames = () => {
      drawSpectrogram(canvas, framesRef?.current ?? frames, viewport);
    };
    drawCurrentFrames();
    if (!active) {
      return;
    }
    const interval = window.setInterval(drawCurrentFrames, 1000 / 12);
    return () => window.clearInterval(interval);
  }, [active, frameVersion, frames, framesRef, viewport]);

  useEffect(() => {
    if (selectedEventId && !events.some((event) => event.id === selectedEventId)) {
      setSelectedEventId(null);
    }
  }, [events, selectedEventId]);

  function handleSliderChange(event: ChangeEvent<HTMLInputElement>) {
    onViewportStartChange(Number(event.currentTarget.value));
  }

  return (
    <section className="live-spectrogram-panel" aria-label="실시간 참조 타임라인">
      <div className="live-spectrogram-header">
        <div>
          <h2>실시간 참조 타임라인</h2>
          <span>{formatTime(viewport.startSec)} - {formatTime(viewport.endSec)}</span>
        </div>
        <button type="button" onClick={onJumpToLatest} disabled={autoFollow}>
          최신으로 이동
        </button>
      </div>

      <div
        className="live-spectrogram-stage"
        style={{ '--live-marker-layer-height': `${markerLayerHeight}px` } as CSSProperties}
      >
        <canvas ref={canvasRef} className="live-spectrogram-canvas" aria-label="실시간 스펙트로그램" />
        <div className="live-marker-layer" style={{ height: markerLayerHeight }}>
          {renderEvents.map((event) => (
            <button
              type="button"
              key={event.id}
              className="live-marker"
              style={{
                left: `${event.leftPercent}%`,
                width: `${event.widthPercent}%`,
                top: event.lane * MARKER_LANE_HEIGHT,
              }}
              title={markerDescription(event)}
              aria-label={markerDescription(event)}
              aria-pressed={selectedEventId === event.id}
              aria-describedby={selectedEventId === event.id ? 'live-marker-detail' : undefined}
              onClick={() => setSelectedEventId((current) => current === event.id ? null : event.id)}
            >
              {eventLabel(event)}
            </button>
          ))}
        </div>
      </div>

      {selectedEvent && (
        <p id="live-marker-detail" className="live-marker-detail">
          {markerDescription(selectedEvent)}
        </p>
      )}

      {chunkLaneCount > 0 && (
        <div
          className="live-chunk-request-lane"
          aria-label="실시간 청크 요청 상태"
          style={{ '--live-chunk-request-lane-height': `${chunkLaneHeight}px` } as CSSProperties}
        >
          {renderChunks.map((chunk) => (
            <Fragment key={`${chunk.sessionId}-${chunk.sequenceId}`}>
              {chunk.tailWidthPercent > 0 && (
                <span
                  aria-hidden="true"
                  className={`live-chunk-tail ${chunkStatusClass(chunk.status)}`}
                  style={{
                    left: `${chunk.tailLeftPercent}%`,
                    width: `${chunk.tailWidthPercent}%`,
                    top: chunk.lane * CHUNK_LANE_HEIGHT + 7,
                  }}
                />
              )}
              {chunk.delayTicks.map((tick) => (
                <span
                  key={`${chunk.sessionId}-${chunk.sequenceId}-delay-${tick.seconds}`}
                  aria-hidden="true"
                  className={`live-chunk-delay-tick ${chunkStatusClass(chunk.status)} live-chunk-delay-label-${tick.labelSide}`}
                  style={{
                    left: `${tick.leftPercent}%`,
                    top: chunk.lane * CHUNK_LANE_HEIGHT + 3,
                  }}
                >
                  {tick.label && <span className="live-chunk-delay-label">{tick.label}</span>}
                </span>
              ))}
              <span
                role="img"
                className={`live-chunk-block ${chunkStatusClass(chunk.status)}`}
                style={{
                  left: `${chunk.blockLeftPercent}%`,
                  width: `${chunk.blockWidthPercent}%`,
                  top: chunk.lane * CHUNK_LANE_HEIGHT + 2,
                }}
                title={chunk.title}
                aria-label={chunk.ariaLabel}
              >
                {chunkLabel(chunk)}
              </span>
            </Fragment>
          ))}
        </div>
      )}

      <div className="live-time-axis" aria-hidden="true">
        {timeTicks.map((tick) => (
          <span
            key={tick}
            style={{ left: `${((tick - viewport.startSec) / viewportDurationSec) * 100}%` }}
          >
            {formatTime(tick)}
          </span>
        ))}
      </div>

      <div className="live-timeline-controls">
        <input
          type="range"
          min="0"
          max={maxViewportStartSec}
          step="0.01"
          value={viewport.startSec}
          disabled={maxViewportStartSec <= 0}
          aria-label="실시간 타임라인 위치"
          onChange={handleSliderChange}
        />
        <span>{formatTime(totalDurationSec)}</span>
      </div>

      <div className="live-reference-summary">
        <div className="live-history">
          <h3>실시간 감지 기록</h3>
          {history.length ? (
            <ol>
              {history.map((event) => (
                <li key={event.id}>
                  <span>{eventLabel(event)}</span>
                  <time>{formatTime(event.startTimeSec)}</time>
                </li>
              ))}
            </ol>
          ) : (
            <p>기록 없음</p>
          )}
        </div>

        <div className="live-diagnostics-panel">
          <div className="live-diagnostics" aria-label="실시간 진단 상태">
            <span>EMPTY: {diagnostics.emptyCount}</span>
            <span>SKIP: {diagnostics.skippedCount}</span>
            <span>FAIL: {diagnostics.failureCount}</span>
            {diagnostics.latestMessage && <strong>{diagnostics.latestMessage}</strong>}
          </div>
          {canDownloadCsv && (
            <div className="live-chunk-csv-actions">
              <button className="live-chunk-csv-button" type="button" onClick={onDownloadCsv}>
                <Download size={14} aria-hidden="true" />
                CSV 다운로드
              </button>
              <button className="live-chunk-csv-button" type="button" onClick={onOpenCsv}>
                <ExternalLink size={14} aria-hidden="true" />
                CSV 열기
              </button>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}

function drawSpectrogram(
  canvas: HTMLCanvasElement,
  frames: LiveSpectrogramFrame[],
  viewport: LiveTimelineViewport,
) {
  const context = canvas.getContext('2d');
  if (!context) {
    return;
  }

  const rect = canvas.getBoundingClientRect();
  const width = Math.max(320, Math.floor(rect.width || 720));
  const height = Math.max(140, Math.floor(rect.height || 170));
  const scale = window.devicePixelRatio || 1;
  const viewportDurationSec = Math.max(0.001, viewport.endSec - viewport.startSec);
  const backingWidth = Math.round(width * scale);
  const backingHeight = Math.round(height * scale);

  // Assigning canvas dimensions clears and reallocates the backing store. The
  // live loop runs 12 times per second, so only resize when layout or DPR did.
  if (canvas.width !== backingWidth || canvas.height !== backingHeight) {
    canvas.width = backingWidth;
    canvas.height = backingHeight;
  }
  context.setTransform(scale, 0, 0, scale, 0, 0);
  context.clearRect(0, 0, width, height);
  context.fillStyle = '#101a22';
  context.fillRect(0, 0, width, height);

  framesInViewport(frames, viewport).forEach((frame) => {
    const x = ((frame.timestampSec - viewport.startSec) / viewportDurationSec) * width;
    const binHeight = height / Math.max(1, frame.magnitudes.length);
    const columnWidth = Math.max(1, width / Math.max(1, viewportDurationSec * 12));

    frame.magnitudes.forEach((magnitude, index) => {
      context.fillStyle = colorForMagnitude(magnitude);
      context.fillRect(
        x,
        height - (index + 1) * binHeight,
        columnWidth,
        Math.max(1, binHeight),
      );
    });
  });
}

export function framesInViewport(
  frames: LiveSpectrogramFrame[],
  viewport: LiveTimelineViewport,
): LiveSpectrogramFrame[] {
  let low = 0;
  let high = frames.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (frames[middle].timestampSec < viewport.startSec) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  const startIndex = low;

  high = frames.length;
  while (low < high) {
    const middle = Math.floor((low + high) / 2);
    if (frames[middle].timestampSec <= viewport.endSec) {
      low = middle + 1;
    } else {
      high = middle;
    }
  }
  return frames.slice(startIndex, low);
}

function colorForMagnitude(value: number): string {
  if (value >= 0.72) {
    return '#f0b84a';
  }
  if (value >= 0.38) {
    return '#1aa39a';
  }
  if (value >= 0.16) {
    return '#165760';
  }
  return '#101a22';
}

function eventLabel(event: LiveTimelineEvent): string {
  return typeof event.confidence === 'number'
    ? `${event.label} ${Math.round(event.confidence * 100)}%`
    : event.label;
}

function markerDescription(event: LiveTimelineEvent): string {
  const confidence = typeof event.confidence === 'number'
    ? ` ${Math.round(event.confidence * 100)}%`
    : '';
  return `${event.label} ${formatTime(event.startTimeSec)}-${formatTime(event.endTimeSec)}${confidence}`;
}

function chunkStatusClass(status: LiveChunkStatus): string {
  return `live-chunk-status-${status.toLowerCase()}`;
}

function chunkLabel(chunk: LiveChunkRecord): string {
  return `#${chunk.sequenceId} ${chunk.status[0]}`;
}
