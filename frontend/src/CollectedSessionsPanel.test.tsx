import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CollectedSessionInfo } from './types';

const panelMocks = vi.hoisted(() => ({
  fetchCollectedSessions: vi.fn(),
  uploadCollectedSessionToGcs: vi.fn(),
  deleteCollectedSession: vi.fn(),
  deleteCollectedSegment: vi.fn(),
}));

vi.mock('./api', () => ({
  fetchCollectedSessions: panelMocks.fetchCollectedSessions,
  uploadCollectedSessionToGcs: panelMocks.uploadCollectedSessionToGcs,
  deleteCollectedSession: panelMocks.deleteCollectedSession,
  deleteCollectedSegment: panelMocks.deleteCollectedSegment,
  collectedFileUrl: (sessionId: string, filename: string) =>
    `/api/collected-sessions/${sessionId}/files/${filename}`,
}));

import { CollectedSessionsPanel } from './CollectedSessionsPanel';

function sessionInfo(overrides: Partial<CollectedSessionInfo> = {}): CollectedSessionInfo {
  return {
    session_id: 'session-1',
    session_name: '사무실 소음',
    started_at: '2026-07-06T05:00:00+00:00',
    ended_at: '2026-07-06T05:01:00+00:00',
    segment_count: 1,
    total_collected_duration_sec: 4,
    candidate_segment_count: 4,
    policy_selected_segment_count: 2,
    policy_selected_duration_sec: 8,
    policy_selected_audio_bytes: 1000,
    rejected_repetitive_count: 1,
    rejected_class_balance_count: 0,
    rejected_session_budget_count: 1,
    invalid_audio_count: 0,
    write_error_count: 0,
    selected_label_segment_counts: { Knock: 1 },
    selected_quota_duration_sec: { knock: 4 },
    policy_version: 1,
    gcs_upload: null,
    segments: [
      {
        segment_index: 1,
        start_sec: 0,
        end_sec: 4,
        duration_sec: 4,
        event_count: 2,
        labels: ['Knock'],
        audio_filename: 'segment-001-0.000-4.000.mp3',
        metadata_filename: 'segment-001-0.000-4.000.json',
      },
    ],
    ...overrides,
  };
}

beforeEach(() => {
  panelMocks.fetchCollectedSessions.mockReset();
  panelMocks.uploadCollectedSessionToGcs.mockReset();
  panelMocks.deleteCollectedSession.mockReset();
  panelMocks.deleteCollectedSegment.mockReset();
  panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [] });
  panelMocks.uploadCollectedSessionToGcs.mockResolvedValue({
    status: 'uploaded',
    session_id: 'session-1',
    object_prefix: 'root/session-1',
    snapshot_id: 'abc',
    uploaded_file_count: 4,
    existing_file_count: 0,
    total_size_bytes: 123,
  });
  panelMocks.deleteCollectedSession.mockResolvedValue(undefined);
  panelMocks.deleteCollectedSegment.mockResolvedValue(undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('CollectedSessionsPanel', () => {
  it('shows an empty message when nothing is collected', async () => {
    render(<CollectedSessionsPanel refreshToken={0} />);

    expect(await screen.findByText(/아직 수집된 세션이 없습니다/)).toBeInTheDocument();
  });

  it('lists collected sessions with name, date, and segment audio', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [sessionInfo()] });

    render(<CollectedSessionsPanel refreshToken={0} />);

    expect(await screen.findByText('사무실 소음')).toBeInTheDocument();
    expect(screen.getByText(/세그먼트 1개/)).toBeInTheDocument();
    expect(screen.getByText(/후보 4개/)).toBeInTheDocument();
    expect(screen.getByText(/정책 선택 2개 · 반복 제외 1개/)).toBeInTheDocument();
    expect(screen.getByText(/Knock/)).toBeInTheDocument();
    const audio = screen.getByLabelText(/세그먼트 1 재생/);
    expect(audio).toHaveAttribute(
      'src',
      '/api/collected-sessions/session-1/files/segment-001-0.000-4.000.mp3',
    );
  });

  it('renders long sessions in 100-segment pages starting from the newest', async () => {
    const baseSegment = sessionInfo().segments[0];
    const segments = Array.from({ length: 205 }, (_value, index) => {
      const segmentIndex = index + 1;
      return {
        ...baseSegment,
        segment_index: segmentIndex,
        start_sec: index * 5,
        end_sec: index * 5 + 5,
        audio_filename: `segment-${segmentIndex}.mp3`,
        metadata_filename: `segment-${segmentIndex}.json`,
      };
    });
    panelMocks.fetchCollectedSessions.mockResolvedValue({
      sessions: [sessionInfo({ segment_count: segments.length, segments })],
    });

    render(<CollectedSessionsPanel refreshToken={0} />);

    expect(await screen.findByLabelText('세그먼트 205 재생')).toBeInTheDocument();
    expect(screen.queryByLabelText('세그먼트 105 재생', { exact: true })).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: /이전 세그먼트 더 보기 \(105개 남음\)/ }));

    expect(screen.getByLabelText('세그먼트 6 재생')).toBeInTheDocument();
    expect(screen.queryByLabelText('세그먼트 5 재생', { exact: true })).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: /이전 세그먼트 더 보기 \(5개 남음\)/ })).toBeInTheDocument();
  });

  it('expands only the most recent session by default', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({
      sessions: [
        sessionInfo({ session_id: 'session-new', session_name: '새 세션' }),
        sessionInfo({ session_id: 'session-old', session_name: '옛 세션' }),
      ],
    });

    const { container } = render(<CollectedSessionsPanel refreshToken={0} />);
    await screen.findByText('새 세션');

    const detailsElements = container.querySelectorAll('details');
    expect(detailsElements).toHaveLength(2);
    expect(detailsElements[0]).toHaveAttribute('open');
    expect(detailsElements[1]).not.toHaveAttribute('open');
  });

  it('shows the session count in the header', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({
      sessions: [
        sessionInfo({ session_id: 'session-new', session_name: '새 세션' }),
        sessionInfo({ session_id: 'session-old', session_name: '옛 세션' }),
      ],
    });

    render(<CollectedSessionsPanel refreshToken={0} />);

    expect(await screen.findByText('2개 세션')).toBeInTheDocument();
  });

  it('reloads the list when the refresh token changes', async () => {
    const { rerender } = render(<CollectedSessionsPanel refreshToken={0} />);
    await waitFor(() => expect(panelMocks.fetchCollectedSessions).toHaveBeenCalledTimes(1));

    rerender(<CollectedSessionsPanel refreshToken={1} />);

    await waitFor(() => expect(panelMocks.fetchCollectedSessions).toHaveBeenCalledTimes(2));
  });

  it('does not let an older list response overwrite a newer refresh', async () => {
    let resolveOlder: (value: { sessions: CollectedSessionInfo[] }) => void = () => undefined;
    const olderResponse = new Promise<{ sessions: CollectedSessionInfo[] }>((resolve) => {
      resolveOlder = resolve;
    });
    panelMocks.fetchCollectedSessions
      .mockReturnValueOnce(olderResponse)
      .mockResolvedValueOnce({
        sessions: [sessionInfo({ session_id: 'newer', session_name: '최신 세션' })],
      });

    const { rerender } = render(<CollectedSessionsPanel refreshToken={0} />);
    rerender(<CollectedSessionsPanel refreshToken={1} />);
    expect(await screen.findByText('최신 세션')).toBeInTheDocument();

    await act(async () => {
      resolveOlder({
        sessions: [sessionInfo({ session_id: 'older', session_name: '이전 세션' })],
      });
      await olderResponse;
    });

    expect(screen.getByText('최신 세션')).toBeInTheDocument();
    expect(screen.queryByText('이전 세션')).not.toBeInTheDocument();
  });

  it('refreshes periodically while auto-refresh is enabled', async () => {
    vi.useFakeTimers();
    try {
      render(<CollectedSessionsPanel refreshToken={0} autoRefreshMs={1000} />);
      await act(async () => {
        await vi.advanceTimersByTimeAsync(0);
      });
      expect(panelMocks.fetchCollectedSessions).toHaveBeenCalledTimes(1);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(panelMocks.fetchCollectedSessions).toHaveBeenCalledTimes(2);

      await act(async () => {
        await vi.advanceTimersByTimeAsync(1000);
      });
      expect(panelMocks.fetchCollectedSessions).toHaveBeenCalledTimes(3);
    } finally {
      vi.useRealTimers();
    }
  });

  it('deletes a session after confirmation and reloads', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [sessionInfo()] });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<CollectedSessionsPanel refreshToken={0} />);
    await userEvent.click(await screen.findByRole('button', { name: /사무실 소음 세션 삭제/ }));

    expect(panelMocks.deleteCollectedSession).toHaveBeenCalledWith('session-1');
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining(
      '이미 GCS에 업로드한 사본은 삭제되지 않습니다.',
    ));
    await waitFor(() => expect(panelMocks.fetchCollectedSessions).toHaveBeenCalledTimes(2));
  });

  it('does not delete a session when the user cancels the confirmation', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [sessionInfo()] });
    vi.spyOn(window, 'confirm').mockReturnValue(false);

    render(<CollectedSessionsPanel refreshToken={0} />);
    await userEvent.click(await screen.findByRole('button', { name: /사무실 소음 세션 삭제/ }));

    expect(panelMocks.deleteCollectedSession).not.toHaveBeenCalled();
  });

  it('deletes a single segment after confirmation', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [sessionInfo()] });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<CollectedSessionsPanel refreshToken={0} />);
    await userEvent.click(await screen.findByRole('button', { name: /세그먼트 1 삭제/ }));

    expect(panelMocks.deleteCollectedSegment).toHaveBeenCalledWith(
      'session-1',
      'segment-001-0.000-4.000.mp3',
    );
    expect(window.confirm).toHaveBeenCalledWith(expect.stringContaining(
      '이미 GCS에 업로드한 사본은 삭제되지 않습니다.',
    ));
  });

  it('allows a fresh export after deleting a segment from an uploaded session', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [sessionInfo()] });
    vi.spyOn(window, 'confirm').mockReturnValue(true);

    render(<CollectedSessionsPanel refreshToken={0} />);
    const uploadButton = await screen.findByRole('button', {
      name: /사무실 소음 세션 GCS 일괄 업로드/,
    });
    await userEvent.click(uploadButton);
    expect(uploadButton).toHaveTextContent('GCS 업로드 완료');
    expect(uploadButton).toBeDisabled();

    await userEvent.click(screen.getByRole('button', { name: /세그먼트 1 삭제/ }));

    await waitFor(() => expect(panelMocks.deleteCollectedSegment).toHaveBeenCalledTimes(1));
    expect(uploadButton).toHaveTextContent('GCS 일괄 업로드');
    expect(uploadButton).toBeEnabled();
    expect(screen.queryByText('GCS 업로드 영수증')).not.toBeInTheDocument();
  });

  it('disables session and segment deletion while recording is open', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({
      sessions: [sessionInfo({ ended_at: null })],
    });

    render(<CollectedSessionsPanel refreshToken={0} />);

    expect(
      await screen.findByRole('button', { name: /사무실 소음 세션 삭제/ }),
    ).toBeDisabled();
    expect(screen.getByRole('button', { name: /세그먼트 1 삭제/ })).toBeDisabled();
  });

  it('shows an error message when loading fails', async () => {
    panelMocks.fetchCollectedSessions.mockRejectedValue(new Error('불러오기 실패'));

    render(<CollectedSessionsPanel refreshToken={0} />);

    expect(await screen.findByRole('alert')).toHaveTextContent('불러오기 실패');
  });

  it('disables GCS export and explains missing configuration', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [sessionInfo()] });

    render(
      <CollectedSessionsPanel
        refreshToken={0}
        capabilities={{ gcs: false }}
      />,
    );

    expect(await screen.findByText(/GCS 업로드 설정이 없어/)).toBeInTheDocument();
    expect(screen.getByRole('button', {
      name: /사무실 소음 세션 GCS 일괄 업로드/,
    })).toBeDisabled();
  });

  it('shows file progress while a GCS upload is still running', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [sessionInfo()] });
    let resolveUpload = (_value: unknown) => undefined;
    panelMocks.uploadCollectedSessionToGcs.mockImplementation(
      (_sessionId: string, onProgress?: (progress: Record<string, unknown>) => void) => {
        onProgress?.({
          object_name: 'session.json',
          source_filename: 'session.json',
          file_status: 'uploaded',
          completed_file_count: 1,
          total_file_count: 4,
        });
        return new Promise((resolve) => {
          resolveUpload = resolve;
        });
      },
    );

    render(<CollectedSessionsPanel refreshToken={0} />);
    await userEvent.click(
      await screen.findByRole('button', { name: /사무실 소음 세션 GCS 일괄 업로드/ }),
    );

    expect(await screen.findByText('GCS 업로드 진행')).toBeInTheDocument();
    expect(screen.getByRole('progressbar', { name: /GCS 파일 업로드 진행률/ })).toHaveAttribute(
      'value',
      '1',
    );
    expect(screen.getByText('session.json')).toBeInTheDocument();
    expect(screen.getByRole('button', {
      name: /사무실 소음 세션 GCS 일괄 업로드/,
    })).toHaveTextContent('GCS 업로드 중');

    await act(async () => {
      resolveUpload({
        status: 'uploaded',
        session_id: 'session-1',
        object_prefix: 'root/session-1',
        snapshot_id: 'abc',
        uploaded_file_count: 4,
        existing_file_count: 0,
        total_size_bytes: 123,
      });
    });
    expect(screen.getByRole('button', {
      name: /사무실 소음 세션 GCS 일괄 업로드/,
    })).toHaveTextContent('GCS 업로드 완료');
  });

  it('uploads every file in a completed session to GCS', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [sessionInfo()] });
    panelMocks.uploadCollectedSessionToGcs.mockImplementation(
      async (_sessionId: string, onProgress?: (progress: Record<string, unknown>) => void) => {
        [
          ['session.json', 1],
          ['segment-001-0.000-4.000.mp3', 2],
          ['segment-001-0.000-4.000.json', 3],
          ['manifest.json', 4],
        ].forEach(([filename, completed]) => onProgress?.({
          object_name: String(filename),
          source_filename: String(filename),
          file_status: 'uploaded',
          completed_file_count: Number(completed),
          total_file_count: 4,
        }));
        return {
          status: 'uploaded',
          session_id: 'session-1',
          object_prefix: 'root/session-1',
          snapshot_id: 'abc',
          uploaded_file_count: 4,
          existing_file_count: 0,
          total_size_bytes: 123,
        };
      },
    );

    render(<CollectedSessionsPanel refreshToken={0} />);
    await userEvent.click(
      await screen.findByRole('button', { name: /사무실 소음 세션 GCS 일괄 업로드/ }),
    );

    expect(panelMocks.uploadCollectedSessionToGcs).toHaveBeenCalledWith(
      'session-1',
      expect.any(Function),
    );
    expect(
      await screen.findByRole('button', { name: /사무실 소음 세션 GCS 일괄 업로드/ }),
    ).toHaveTextContent('GCS 업로드 완료');
    expect(screen.getByRole('progressbar', { name: /GCS 파일 업로드 진행률/ })).toHaveAttribute(
      'value',
      '4',
    );
    expect(screen.getByText('manifest.json')).toBeInTheDocument();
    expect(screen.getByText('GCS 업로드 영수증')).toBeInTheDocument();
    expect(screen.getByText('경로: root/session-1')).toBeInTheDocument();
    expect(screen.getByText('스냅샷: abc')).toBeInTheDocument();
    expect(screen.getByText(/새 파일 4개 · 기존 파일 0개 · 123 B/)).toBeInTheDocument();
  });

  it('caps the GCS file progress log while preserving the latest entries', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [sessionInfo()] });
    panelMocks.uploadCollectedSessionToGcs.mockImplementation(
      async (_sessionId: string, onProgress?: (progress: Record<string, unknown>) => void) => {
        for (let index = 1; index <= 45; index += 1) {
          onProgress?.({
            object_name: `objects/file-${index}.json`,
            source_filename: `file-${index}.json`,
            file_status: 'uploaded',
            completed_file_count: index,
            total_file_count: 45,
          });
        }
        return {
          status: 'uploaded',
          session_id: 'session-1',
          object_prefix: 'root/session-1',
          snapshot_id: 'snapshot-45',
          uploaded_file_count: 45,
          existing_file_count: 0,
          total_size_bytes: 2048,
        };
      },
    );

    render(<CollectedSessionsPanel refreshToken={0} />);
    await userEvent.click(
      await screen.findByRole('button', { name: /사무실 소음 세션 GCS 일괄 업로드/ }),
    );

    expect(await screen.findByText(/이전 5개 파일 기록은 성능을 위해 접었습니다/)).toBeInTheDocument();
    expect(screen.queryByText('file-1.json')).not.toBeInTheDocument();
    expect(screen.getByText('file-6.json')).toBeInTheDocument();
    expect(screen.getByText('file-45.json')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: /최근 GCS 파일별 업로드 현황/ }).children).toHaveLength(40);
  });

  it('disables GCS upload until the recording has ended', async () => {
    panelMocks.fetchCollectedSessions.mockResolvedValue({
      sessions: [sessionInfo({ ended_at: null })],
    });

    render(<CollectedSessionsPanel refreshToken={0} />);

    expect(
      await screen.findByRole('button', { name: /사무실 소음 세션 GCS 일괄 업로드/ }),
    ).toBeDisabled();
  });

  it('restores completed GCS upload state after the app remounts', async () => {
    const uploadedSession = sessionInfo({
      gcs_upload: {
        status: 'uploaded',
        object_prefix: 'root/uploader/session/snapshot',
        snapshot_id: 'snapshot',
        uploaded_at: '2026-07-10T12:00:00+00:00',
      },
    });
    panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [uploadedSession] });

    const firstMount = render(<CollectedSessionsPanel refreshToken={0} />);
    const firstButton = await screen.findByRole('button', {
      name: /사무실 소음 세션 GCS 일괄 업로드/,
    });
    expect(firstButton).toBeDisabled();
    expect(firstButton).toHaveTextContent('GCS 업로드 완료');
    firstMount.unmount();

    render(<CollectedSessionsPanel refreshToken={0} />);
    const restartedButton = await screen.findByRole('button', {
      name: /사무실 소음 세션 GCS 일괄 업로드/,
    });
    expect(restartedButton).toBeDisabled();
    expect(restartedButton).toHaveTextContent('GCS 업로드 완료');
    expect(screen.getByText('경로: root/uploader/session/snapshot')).toBeInTheDocument();
    expect(screen.getByText('스냅샷: snapshot')).toBeInTheDocument();
    expect(panelMocks.uploadCollectedSessionToGcs).not.toHaveBeenCalled();
  });
});
