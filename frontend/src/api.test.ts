import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  analyzeLiveChunk,
  collectedFileUrl,
  deleteCollectedSegment,
  deleteCollectedSession,
  endLiveSession,
  fetchCollectedSessions,
} from './api';

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('analyzeLiveChunk', () => {
  it('posts live chunk form fields using the backend multipart contract', async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(
        JSON.stringify({
          sequence_id: 7,
          window_start_sec: 3,
          window_end_sec: 5,
          sound_events: [],
          processing_time_ms: 42,
        }),
        { status: 200, headers: { 'Content-Type': 'application/json' } },
      );
    });
    vi.stubGlobal('fetch', fetchMock);
    const file = new Blob(['wav-audio'], { type: 'audio/wav' });

    const result = await analyzeLiveChunk({
      file,
      sessionId: 'session-abc',
      sequenceId: 7,
      windowStartSec: 3,
      windowEndSec: 5,
    });

    expect(result.sequence_id).toBe(7);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/analyze-live-chunk',
      expect.objectContaining({ method: 'POST', body: expect.any(FormData) }),
    );
    const body = fetchMock.mock.calls[0][1]?.body as FormData;
    const fileField = body.get('file') as File;
    expect(fileField).toBeInstanceOf(File);
    expect(fileField.name).toBe('chunk-000007-3.000-5.000.wav');
    expect(fileField.type).toBe('audio/wav');
    expect(fileField.size).toBe(file.size);
    expect(body.get('session_id')).toBe('session-abc');
    expect(body.get('sequence_id')).toBe('7');
    expect(body.get('window_start_sec')).toBe('3');
    expect(body.get('window_end_sec')).toBe('5');
  });
});

describe('endLiveSession', () => {
  it('posts the session id and returns the collection summary', async () => {
    const summary = {
      session_id: 'session-abc',
      segment_count: 1,
      total_collected_duration_sec: 4,
      kept_chunk_count: 3,
      discarded_silent_chunk_count: 2,
      discarded_speech_chunk_count: 1,
      segments: [],
    };
    const fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify(summary), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await endLiveSession('session-abc');

    expect(result).toEqual(summary);
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/live-session/end',
      expect.objectContaining({ method: 'POST', body: expect.any(FormData) }),
    );
    const body = fetchMock.mock.calls[0][1]?.body as FormData;
    expect(body.get('session_id')).toBe('session-abc');
  });

  it('includes the session name when provided', async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify({ session_id: 'session-abc' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    await endLiveSession('session-abc', '사무실 소음');

    const body = fetchMock.mock.calls[0][1]?.body as FormData;
    expect(body.get('session_name')).toBe('사무실 소음');
  });

  it('throws the backend detail message on failure', async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify({ detail: '세션 종료 실패' }), {
        status: 500,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    await expect(endLiveSession('session-abc')).rejects.toThrow('세션 종료 실패');
  });
});

describe('collected session management', () => {
  it('fetches the collected sessions listing', async () => {
    const fetchMock = vi.fn(async () => {
      return new Response(JSON.stringify({ sessions: [] }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      });
    });
    vi.stubGlobal('fetch', fetchMock);

    const result = await fetchCollectedSessions();

    expect(result).toEqual({ sessions: [] });
    expect(fetchMock).toHaveBeenCalledWith('/api/collected-sessions');
  });

  it('deletes sessions and segments with encoded identifiers', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ status: 'deleted' })));
    vi.stubGlobal('fetch', fetchMock);

    await deleteCollectedSession('session a');
    await deleteCollectedSegment('session a', 'segment 1.mp3');

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/collected-sessions/session%20a',
      expect.objectContaining({ method: 'DELETE' }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/collected-sessions/session%20a/segments/segment%201.mp3',
      expect.objectContaining({ method: 'DELETE' }),
    );
  });

  it('builds encoded collected file URLs', () => {
    expect(collectedFileUrl('session a', 'segment 1.mp3')).toBe(
      '/api/collected-sessions/session%20a/files/segment%201.mp3',
    );
  });
});
