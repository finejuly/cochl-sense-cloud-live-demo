import { useCallback, useEffect, useRef, useState, type SyntheticEvent } from 'react';
import { FolderOpen, RefreshCw, Trash2 } from 'lucide-react';
import {
  collectedFileUrl,
  deleteCollectedSegment,
  deleteCollectedSession,
  fetchCollectedSessions,
} from './api';
import type { CollectedSessionInfo } from './types';
import { formatTime } from './waveform';

interface CollectedSessionsPanelProps {
  refreshToken: number;
}

export function CollectedSessionsPanel({ refreshToken }: CollectedSessionsPanelProps) {
  const [sessions, setSessions] = useState<CollectedSessionInfo[]>([]);
  const [expandedIds, setExpandedIds] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const knownIdsRef = useRef<Set<string>>(new Set());

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const response = await fetchCollectedSessions();
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
      setError(err instanceof Error ? err.message : '수집 데이터를 불러오지 못했습니다.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load, refreshToken]);

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
    const label = session.session_name || session.session_id;
    if (!window.confirm(`"${label}" 세션의 수집 데이터를 모두 삭제할까요?`)) {
      return;
    }
    try {
      await deleteCollectedSession(session.session_id);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : '세션 삭제에 실패했습니다.');
    }
  }

  async function handleDeleteSegment(sessionId: string, filename: string) {
    if (!window.confirm('이 세그먼트를 삭제할까요?')) {
      return;
    }
    try {
      await deleteCollectedSegment(sessionId, filename);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : '세그먼트 삭제에 실패했습니다.');
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
        <button type="button" onClick={() => void load()} disabled={loading} aria-label="수집 데이터 새로고침">
          <RefreshCw size={16} aria-hidden="true" />
          새로고침
        </button>
      </header>

      {error && (
        <p className="collected-sessions-error" role="alert">
          {error}
        </p>
      )}

      {!error && sessions.length === 0 && (
        <p className="collected-sessions-empty">
          {loading ? '수집 데이터를 불러오는 중입니다.' : '아직 수집된 세션이 없습니다.'}
        </p>
      )}

      <ul className="collected-session-list">
        {sessions.map((session) => (
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
                  {session.total_collected_duration_sec.toFixed(1)}초
                </span>
              </summary>
              <div className="collected-session-body">
                <div className="collected-session-actions">
                  <button
                    type="button"
                    onClick={() => void handleDeleteSession(session)}
                    aria-label={`${session.session_name || session.session_id} 세션 삭제`}
                  >
                    <Trash2 size={16} aria-hidden="true" />
                    세션 삭제
                  </button>
                </div>
                <ul className="collected-segment-rows">
                  {session.segments.map((segment) => (
                    <li key={segment.metadata_filename} className="collected-segment-row">
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
                          void handleDeleteSegment(session.session_id, segment.audio_filename)
                        }
                        aria-label={`세그먼트 ${segment.segment_index} 삭제`}
                      >
                        <Trash2 size={14} aria-hidden="true" />
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            </details>
          </li>
        ))}
      </ul>
    </section>
  );
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
