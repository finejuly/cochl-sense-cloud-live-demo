import { memo, useCallback, useEffect, useRef, useState, type SyntheticEvent } from 'react';
import { CheckCircle2, CloudUpload, FolderOpen, RefreshCw, Trash2 } from 'lucide-react';
import {
  collectedFileUrl,
  deleteCollectedSegment,
  deleteCollectedSession,
  fetchCollectedSessions,
  uploadCollectedSessionToGcs,
} from './api';
import type {
  CollectedSessionInfo,
  GcsSessionUploadResponse,
  GcsUploadFileProgress,
  RuntimeCapabilities,
} from './types';
import { formatTime } from './time';

interface CollectedSessionsPanelProps {
  refreshToken: number;
  autoRefreshMs?: number;
  capabilities?: RuntimeCapabilities;
}

type GcsUploadState = 'idle' | 'uploading' | 'success' | 'error';
const SEGMENT_PAGE_SIZE = 100;
const GCS_PROGRESS_LOG_LIMIT = 40;
const DEFAULT_RUNTIME_CAPABILITIES: RuntimeCapabilities = {
  gcs: true,
};
const CAPABILITY_NOTE_ID = 'collected-session-capability-note';

interface GcsUploadProgressState {
  completedFileCount: number;
  totalFileCount: number;
  files: GcsUploadFileProgress[];
  omittedFileCount: number;
}

function CollectedSessionsPanelComponent({
  refreshToken,
  autoRefreshMs,
  capabilities = DEFAULT_RUNTIME_CAPABILITIES,
}: CollectedSessionsPanelProps) {
  const [sessions, setSessions] = useState<CollectedSessionInfo[]>([]);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [gcsUploads, setGcsUploads] = useState<Record<string, GcsUploadState>>({});
  const [gcsUploadProgress, setGcsUploadProgress] = useState<
    Record<string, GcsUploadProgressState>
  >({});
  const [gcsUploadReceipts, setGcsUploadReceipts] = useState<
    Record<string, GcsSessionUploadResponse>
  >({});
  const [visibleSegmentCounts, setVisibleSegmentCounts] = useState<Record<string, number>>({});
  const knownIdsRef = useRef<Set<string>>(new Set());
  const loadSequenceRef = useRef(0);

  const load = useCallback(async ({ silent = false }: { silent?: boolean } = {}) => {
    const loadSequence = loadSequenceRef.current + 1;
    loadSequenceRef.current = loadSequence;
    if (!silent) {
      setLoading(true);
      setError(null);
    }
    try {
      const response = await fetchCollectedSessions();
      if (loadSequence !== loadSequenceRef.current) {
        return;
      }
      setSessions(response.sessions);
      setExpandedIds((current) => {
        // 새로 나타난 최신 세션만 자동으로 펼치고, 사용자가 접은 상태는 유지한다.
        const next = new Set(current);
        const newestId = response.sessions[0]?.session_id;
        if (newestId && !knownIdsRef.current.has(newestId)) {
          next.add(newestId);
        }
        knownIdsRef.current = new Set(response.sessions.map((session) => session.session_id));
        return next;
      });
    } catch (err) {
      if (!silent && loadSequence === loadSequenceRef.current) {
        setError(err instanceof Error ? err.message : '수집 데이터를 불러오지 못했습니다.');
      }
    } finally {
      if (!silent) {
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

  useEffect(() => {
    if (!autoRefreshMs || autoRefreshMs <= 0) {
      return;
    }
    // 녹음 중 실시간으로 확정되는 세그먼트가 목록에 바로 나타나도록 주기 갱신.
    const interval = window.setInterval(() => {
      void load({ silent: true });
    }, autoRefreshMs);
    return () => window.clearInterval(interval);
  }, [autoRefreshMs, load]);

  function handleToggle(sessionId: string, event: SyntheticEvent<HTMLDetailsElement>) {
    const isOpen = event.currentTarget.open;
    setExpandedIds((current) => {
      if (current.has(sessionId) === isOpen) {
        return current;
      }
      const next = new Set(current);
      if (isOpen) {
        next.add(sessionId);
      } else {
        next.delete(sessionId);
      }
      return next;
    });
  }

  async function handleDeleteSession(session: CollectedSessionInfo) {
    if (!session.ended_at) {
      setError('녹음이 끝난 세션만 삭제할 수 있습니다.');
      return;
    }
    const label = session.session_name || session.session_id;
    if (!window.confirm(
      `"${label}" 세션의 로컬 수집 데이터를 모두 삭제할까요?\n\n`
      + '이미 GCS에 업로드한 사본은 삭제되지 않습니다.',
    )) {
      return;
    }
    try {
      await deleteCollectedSession(session.session_id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : '세션 삭제에 실패했습니다.');
    }
  }

  async function handleDeleteSegment(session: CollectedSessionInfo, filename: string) {
    if (!session.ended_at) {
      setError('녹음이 끝난 세션의 세그먼트만 삭제할 수 있습니다.');
      return;
    }
    if (!window.confirm(
      '이 로컬 세그먼트를 삭제할까요?\n\n'
      + '이미 GCS에 업로드한 사본은 삭제되지 않습니다.',
    )) {
      return;
    }
    try {
      await deleteCollectedSegment(session.session_id, filename);
      // GCS uses a new manifest snapshot after a local deletion.
      setGcsUploads((current) => omitRecordKey(current, session.session_id));
      setGcsUploadProgress((current) => omitRecordKey(current, session.session_id));
      setGcsUploadReceipts((current) => omitRecordKey(current, session.session_id));
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : '세그먼트 삭제에 실패했습니다.');
    }
  }

  function handleShowOlderSegments(sessionId: string, segmentCount: number) {
    setVisibleSegmentCounts((current) => ({
      ...current,
      [sessionId]: Math.min(
        segmentCount,
        (current[sessionId] ?? SEGMENT_PAGE_SIZE) + SEGMENT_PAGE_SIZE,
      ),
    }));
  }

  async function handleGcsUpload(session: CollectedSessionInfo) {
    if (!session.ended_at) {
      setError('녹음이 끝난 세션만 GCS에 업로드할 수 있습니다.');
      return;
    }

    setError(null);
    setGcsUploads((current) => ({ ...current, [session.session_id]: 'uploading' }));
    setGcsUploadProgress((current) => ({
      ...current,
      [session.session_id]: {
        completedFileCount: 0,
        totalFileCount: session.segments.length * 2 + 2,
        files: [],
        omittedFileCount: 0,
      },
    }));
    try {
      const receipt = await uploadCollectedSessionToGcs(session.session_id, (progress) => {
        setGcsUploadProgress((current) => {
          const previous = current[session.session_id];
          const nextFiles = [...(previous?.files ?? []), progress];
          const overflow = Math.max(0, nextFiles.length - GCS_PROGRESS_LOG_LIMIT);
          return {
            ...current,
            [session.session_id]: {
              completedFileCount: progress.completed_file_count,
              totalFileCount: progress.total_file_count,
              files: overflow ? nextFiles.slice(overflow) : nextFiles,
              omittedFileCount: (previous?.omittedFileCount ?? 0) + overflow,
            },
          };
        });
      });
      setGcsUploadReceipts((current) => ({ ...current, [session.session_id]: receipt }));
      setGcsUploads((current) => ({ ...current, [session.session_id]: 'success' }));
    } catch (err) {
      setGcsUploads((current) => ({ ...current, [session.session_id]: 'error' }));
      setError(err instanceof Error ? err.message : 'GCS 업로드에 실패했습니다.');
    }
  }

  return (
    <section className="collected-sessions" aria-labelledby="collected-sessions-title">
      <header className="collected-sessions-header">
        <div className="collected-sessions-title">
          <FolderOpen size={18} aria-hidden="true" />
          <h2 id="collected-sessions-title">수집된 데이터</h2>
          {sessions.length > 0 && (
            <span className="collected-sessions-count">{sessions.length}개 세션</span>
          )}
        </div>
        <button
          type="button"
          onClick={() => void load()}
          disabled={loading}
          aria-label="수집 데이터 새로고침"
        >
          <RefreshCw size={16} aria-hidden="true" />
          새로고침
        </button>
      </header>

      {error && (
        <p className="collected-sessions-error" role="alert">
          {error}
        </p>
      )}

      {!capabilities.gcs && (
        <p id={CAPABILITY_NOTE_ID} className="collected-capability-note" role="status">
          GCS 업로드 설정이 없어 업로드를 사용할 수 없습니다.
        </p>
      )}

      {!error && sessions.length === 0 && (
        <p className="collected-sessions-empty">
          {loading ? '수집 데이터를 불러오는 중입니다.' : '아직 수집된 세션이 없습니다.'}
        </p>
      )}

      <ul className="collected-session-list">
        {sessions.map((session) => {
          const gcsUploadState = gcsUploads[session.session_id]
            ?? (session.gcs_upload ? 'success' : 'idle');
          const uploadProgress = gcsUploadProgress[session.session_id];
          const uploadReceipt = gcsUploadReceipts[session.session_id] ?? session.gcs_upload;
          const visibleSegmentCount = Math.min(
            session.segments.length,
            visibleSegmentCounts[session.session_id] ?? SEGMENT_PAGE_SIZE,
          );
          const visibleSegments = session.segments.slice(-visibleSegmentCount);
          const hiddenSegmentCount = session.segments.length - visibleSegments.length;
          return (
            <li key={session.session_id}>
              <details
                className="collected-session-item"
                open={expandedIds.has(session.session_id)}
                onToggle={(event) => handleToggle(session.session_id, event)}
              >
              <summary className="collected-session-summary-row">
                <span className="collected-session-name">
                  {session.session_name || session.session_id}
                </span>
                <span className="collected-session-meta">
                  {formatSessionTimestamp(session.started_at)} · 세그먼트 {session.segment_count}개 · 총{' '}
                  {session.total_collected_duration_sec.toFixed(1)}초 · 후보 {session.candidate_segment_count}개
                </span>
              </summary>
              {expandedIds.has(session.session_id) && (
              <div className="collected-session-body">
                <div className="collected-session-actions">
                  <button
                    className="gcs-upload-button"
                    type="button"
                    onClick={() => void handleGcsUpload(session)}
                    disabled={
                      !capabilities.gcs
                      || !session.ended_at
                      || session.segment_count === 0
                      || gcsUploadState === 'uploading'
                      || gcsUploadState === 'success'
                    }
                    aria-label={`${session.session_name || session.session_id} 세션 GCS 일괄 업로드`}
                    aria-describedby={!capabilities.gcs ? CAPABILITY_NOTE_ID : undefined}
                    title={
                      !capabilities.gcs
                        ? '서버의 GCS 업로드 설정을 먼저 구성해 주세요.'
                        : session.ended_at
                        ? `오디오와 메타데이터 ${session.segment_count}개 세그먼트 일괄 업로드`
                        : '녹음 종료 후 업로드할 수 있습니다.'
                    }
                  >
                    {gcsUploadState === 'uploading' ? (
                      <RefreshCw className="gcs-upload-spinner" size={14} aria-hidden="true" />
                    ) : (
                      <CloudUpload size={14} aria-hidden="true" />
                    )}
                    {gcsUploadLabel(gcsUploadState)}
                  </button>
                  <button
                    type="button"
                    onClick={() => void handleDeleteSession(session)}
                    disabled={
                      !session.ended_at
                      || gcsUploadState === 'uploading'
                    }
                    aria-label={`${session.session_name || session.session_id} 세션 삭제`}
                    title={session.ended_at ? '세션 전체 삭제' : '녹음 종료 후 삭제할 수 있습니다.'}
                  >
                    <Trash2 size={16} aria-hidden="true" />
                    세션 삭제
                  </button>
                </div>
                <p className="collected-session-curation-stats">
                  정책 선택 {session.policy_selected_segment_count}개 · 반복 제외{' '}
                  {session.rejected_repetitive_count}개 · 클래스 균형 제외{' '}
                  {session.rejected_class_balance_count}개 · 세션 상한 제외{' '}
                  {session.rejected_session_budget_count}개 · 손상/저장 오류{' '}
                  {session.invalid_audio_count + session.write_error_count}개
                </p>
                {uploadProgress && (
                  <div
                    className={`gcs-upload-progress gcs-upload-progress-${gcsUploadState}`}
                  >
                    <div className="gcs-upload-progress-summary">
                      <span>{gcsUploadProgressLabel(gcsUploadState)}</span>
                      <strong>
                        {uploadProgress.completedFileCount}/{uploadProgress.totalFileCount} 파일 ·{' '}
                        {gcsUploadPercent(uploadProgress)}%
                      </strong>
                    </div>
                    <progress
                      aria-label="GCS 파일 업로드 진행률"
                      max={Math.max(1, uploadProgress.totalFileCount)}
                      value={uploadProgress.completedFileCount}
                    />
                    {uploadProgress.files.length ? (
                      <>
                      {uploadProgress.omittedFileCount > 0 && (
                        <p className="gcs-upload-log-note">
                          이전 {uploadProgress.omittedFileCount}개 파일 기록은 성능을 위해 접었습니다.
                        </p>
                      )}
                      <ul className="gcs-upload-file-list" aria-label="최근 GCS 파일별 업로드 현황">
                        {uploadProgress.files.map((file) => (
                          <li key={`${file.object_name}-${file.completed_file_count}`}>
                            <CheckCircle2 size={14} aria-hidden="true" />
                            <span title={file.object_name}>{file.source_filename}</span>
                            <em>{file.file_status === 'uploaded' ? '업로드 완료' : '이미 존재'}</em>
                          </li>
                        ))}
                      </ul>
                      </>
                    ) : (
                      <p className="gcs-upload-preparing">업로드할 파일을 준비하고 있습니다.</p>
                    )}
                  </div>
                )}
                {gcsUploadState === 'success' && uploadReceipt && (
                  <div className="gcs-upload-receipt" role="status">
                    <strong>GCS 업로드 영수증</strong>
                    <span>경로: {uploadReceipt.object_prefix}</span>
                    <span>스냅샷: {uploadReceipt.snapshot_id}</span>
                    {'uploaded_file_count' in uploadReceipt && (
                      <span>
                        새 파일 {uploadReceipt.uploaded_file_count}개 · 기존 파일{' '}
                        {uploadReceipt.existing_file_count}개 · {formatBytes(uploadReceipt.total_size_bytes)}
                      </span>
                    )}
                  </div>
                )}
                <ul className="collected-segment-rows">
                  {visibleSegments.map((segment) => (
                      <li
                        key={segment.metadata_filename}
                        className="collected-segment-row"
                      >
                        <span className="collection-segment-index">#{segment.segment_index}</span>
                        <span className="collection-segment-range">
                          {formatTime(segment.start_sec)}–{formatTime(segment.end_sec)} (
                          {segment.duration_sec.toFixed(1)}초)
                        </span>
                        <span className="collection-segment-labels">
                          {segment.labels.length ? segment.labels.join(', ') : '라벨 없음'}
                        </span>
                        <audio
                          controls
                          preload="none"
                          src={collectedFileUrl(session.session_id, segment.audio_filename)}
                          aria-label={`세그먼트 ${segment.segment_index} 재생`}
                        />
                        <button
                          type="button"
                          onClick={() =>
                            void handleDeleteSegment(session, segment.audio_filename)
                          }
                          disabled={
                            !session.ended_at
                            || gcsUploadState === 'uploading'
                          }
                          aria-label={`세그먼트 ${segment.segment_index} 삭제`}
                          title={session.ended_at ? '세그먼트 삭제' : '녹음 종료 후 삭제할 수 있습니다.'}
                        >
                          <Trash2 size={14} aria-hidden="true" />
                        </button>
                      </li>
                  ))}
                </ul>
                {hiddenSegmentCount > 0 && (
                  <button
                    className="collected-segments-more"
                    type="button"
                    onClick={() => handleShowOlderSegments(
                      session.session_id,
                      session.segments.length,
                    )}
                  >
                    이전 세그먼트 더 보기 ({hiddenSegmentCount}개 남음)
                  </button>
                )}
              </div>
              )}
              </details>
            </li>
          );
        })}
      </ul>
    </section>
  );
}

export const CollectedSessionsPanel = memo(CollectedSessionsPanelComponent);

function omitRecordKey<T>(record: Record<string, T>, key: string): Record<string, T> {
  if (!(key in record)) {
    return record;
  }
  const next = { ...record };
  delete next[key];
  return next;
}

function gcsUploadLabel(state: GcsUploadState): string {
  if (state === 'uploading') {
    return 'GCS 업로드 중';
  }
  if (state === 'success') {
    return 'GCS 업로드 완료';
  }
  if (state === 'error') {
    return 'GCS 업로드 재시도';
  }
  return 'GCS 일괄 업로드';
}

function gcsUploadProgressLabel(state: GcsUploadState): string {
  if (state === 'success') {
    return 'GCS 업로드 완료';
  }
  if (state === 'error') {
    return 'GCS 업로드 중단';
  }
  return 'GCS 업로드 진행';
}

function gcsUploadPercent(progress: GcsUploadProgressState): number {
  if (progress.totalFileCount <= 0) {
    return 0;
  }
  return Math.min(100, Math.round(
    (progress.completedFileCount / progress.totalFileCount) * 100,
  ));
}

function formatSessionTimestamp(startedAt: string | null): string {
  if (!startedAt) {
    return '시각 정보 없음';
  }
  const parsed = new Date(startedAt);
  if (Number.isNaN(parsed.getTime())) {
    return '시각 정보 없음';
  }
  return parsed.toLocaleString('ko-KR', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  });
}

function formatBytes(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) {
    return '크기 정보 없음';
  }
  if (bytes < 1024) {
    return `${bytes} B`;
  }
  if (bytes < 1024 * 1024) {
    return `${(bytes / 1024).toFixed(1)} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}
