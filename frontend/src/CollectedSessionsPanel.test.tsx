import { act, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CollectedSessionInfo } from './types';

const panelMocks = vi.hoisted(() => ({
  fetchCollectedSessions: vi.fn(),
  deleteCollectedSession: vi.fn(),
  deleteCollectedSegment: vi.fn(),
}));

vi.mock('./api', () => ({
  fetchCollectedSessions: panelMocks.fetchCollectedSessions,
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
  panelMocks.deleteCollectedSession.mockReset();
  panelMocks.deleteCollectedSegment.mockReset();
  panelMocks.fetchCollectedSessions.mockResolvedValue({ sessions: [] });
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
    expect(screen.getByText(/Knock/)).toBeInTheDocument();
    const audio = screen.getByLabelText(/세그먼트 1 재생/);
    expect(audio).toHaveAttribute(
      'src',
      '/api/collected-sessions/session-1/files/segment-001-0.000-4.000.mp3',
    );
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
  });

  it('shows an error message when loading fails', async () => {
    panelMocks.fetchCollectedSessions.mockRejectedValue(new Error('불러오기 실패'));

    render(<CollectedSessionsPanel refreshToken={0} />);

    expect(await screen.findByRole('alert')).toHaveTextContent('불러오기 실패');
  });
});
