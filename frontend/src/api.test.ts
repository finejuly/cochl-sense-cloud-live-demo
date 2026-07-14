import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  analyzeLiveChunk,
  collectedFileUrl,
  deleteCollectedSegment,
  deleteCollectedSession,
  endLiveSession,
  fetchRuntimeConfig,
  fetchCollectedSessions,
  uploadCollectedSessionToGcs,
} from './api';

beforeEach(async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({
    collection_confidence_threshold: 0.5,
    api_token: 'test-local-token',
    capabilities: { gcs: true },
  }), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })));
  await fetchRuntimeConfig({ force: true });
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('fetchRuntimeConfig', () => {
  it('rejects malformed configuration with an actionable contract error', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('null', {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    })));

    await expect(fetchRuntimeConfig({ force: true })).rejects.toThrow(
      '로컬 API 실행 설정이 올바르지 않습니다.',
    );
  });
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
      expect.objectContaining({
        method: 'POST',
        headers: { 'X-Cochl-Local-Token': 'test-local-token' },
        body: expect.any(FormData),
      }),
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
      expect.objectContaining({
        method: 'POST',
        headers: { 'X-Cochl-Local-Token': 'test-local-token' },
        body: expect.any(FormData),
      }),
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

  it('forwards an abort signal to the finalization request', async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify({ session_id: 'session-abc' }), {
      status: 200,
      headers: { 'Content-Type': 'application/json' },
    }));
    vi.stubGlobal('fetch', fetchMock);
    const controller = new AbortController();

    await endLiveSession('session-abc', undefined, controller.signal);

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/live-session/end',
      expect.objectContaining({ signal: controller.signal }),
    );
  });

  it('refreshes runtime config and retries once after an authentication failure', async () => {
    const refreshedConfig = {
      collection_confidence_threshold: 0.65,
      api_token: 'refreshed-token',
      capabilities: { gcs: false },
    };
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response('unauthorized', { status: 401 }))
      .mockResolvedValueOnce(new Response(JSON.stringify(refreshedConfig), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify({ session_id: 'session-abc' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }));
    vi.stubGlobal('fetch', fetchMock);

    await endLiveSession('session-abc');

    expect(fetchMock).toHaveBeenNthCalledWith(
      1,
      '/api/live-session/end',
      expect.objectContaining({ headers: { 'X-Cochl-Local-Token': 'test-local-token' } }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(2, '/api/runtime-config');
    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      '/api/live-session/end',
      expect.objectContaining({ headers: { 'X-Cochl-Local-Token': 'refreshed-token' } }),
    );
  });

  it('does not let an older runtime-config response overwrite a newer token', async () => {
    let resolveOlder: (response: Response) => void = () => undefined;
    let resolveNewer: (response: Response) => void = () => undefined;
    const olderResponse = new Promise<Response>((resolve) => {
      resolveOlder = resolve;
    });
    const newerResponse = new Promise<Response>((resolve) => {
      resolveNewer = resolve;
    });
    const fetchMock = vi.fn()
      .mockReturnValueOnce(olderResponse)
      .mockReturnValueOnce(newerResponse)
      .mockResolvedValueOnce(new Response(JSON.stringify({ session_id: 'session-abc' }), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }));
    vi.stubGlobal('fetch', fetchMock);

    const olderRequest = fetchRuntimeConfig({ force: true });
    const newerRequest = fetchRuntimeConfig({ force: true });
    resolveNewer(new Response(JSON.stringify({
      collection_confidence_threshold: 0.6,
      api_token: 'newer-token',
      capabilities: { gcs: true },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await newerRequest;
    resolveOlder(new Response(JSON.stringify({
      collection_confidence_threshold: 0.5,
      api_token: 'older-token',
      capabilities: { gcs: true },
    }), { status: 200, headers: { 'Content-Type': 'application/json' } }));
    await olderRequest;

    await endLiveSession('session-abc');

    expect(fetchMock).toHaveBeenNthCalledWith(
      3,
      '/api/live-session/end',
      expect.objectContaining({ headers: { 'X-Cochl-Local-Token': 'newer-token' } }),
    );
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
      expect.objectContaining({
        method: 'DELETE',
        headers: { 'X-Cochl-Local-Token': 'test-local-token' },
      }),
    );
    expect(fetchMock).toHaveBeenNthCalledWith(
      2,
      '/api/collected-sessions/session%20a/segments/segment%201.mp3',
      expect.objectContaining({
        method: 'DELETE',
        headers: { 'X-Cochl-Local-Token': 'test-local-token' },
      }),
    );
  });

  it('builds encoded collected file URLs', () => {
    expect(collectedFileUrl('session a', 'segment 1.mp3')).toBe(
      '/api/collected-sessions/session%20a/files/segment%201.mp3',
    );
  });
});

describe('GCS upload', () => {
  it('streams file progress while uploading a collected session', async () => {
    const payload = {
      status: 'uploaded',
      session_id: 'session a',
      object_prefix: 'root/session-a',
      snapshot_id: 'abc',
      uploaded_file_count: 4,
      existing_file_count: 0,
      total_size_bytes: 123,
    };
    const progress = {
      type: 'progress',
      object_name: 'segments/segment-001.mp3',
      source_filename: 'segment-001.mp3',
      file_status: 'uploaded',
      completed_file_count: 1,
      total_file_count: 4,
    };
    const fetchMock = vi.fn(async () =>
      new Response(`${JSON.stringify(progress)}\n${JSON.stringify({ type: 'complete', ...payload })}\n`, {
        status: 200,
        headers: { 'Content-Type': 'application/x-ndjson' },
      }),
    );
    vi.stubGlobal('fetch', fetchMock);
    const onProgress = vi.fn();

    await expect(uploadCollectedSessionToGcs('session a', onProgress)).resolves.toEqual(payload);
    expect(onProgress).toHaveBeenCalledWith(expect.objectContaining({
      source_filename: 'segment-001.mp3',
      completed_file_count: 1,
      total_file_count: 4,
    }));
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/collected-sessions/session%20a/gcs-upload/progress',
      {
        method: 'POST',
        headers: {
          Accept: 'application/x-ndjson',
          'X-Cochl-Local-Token': 'test-local-token',
        },
      },
    );
  });

  it('rejects an upload error sent after streaming has started', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(
      `${JSON.stringify({ type: 'error', message: '권한이 없습니다.' })}\n`,
      { status: 200, headers: { 'Content-Type': 'application/x-ndjson' } },
    )));

    await expect(uploadCollectedSessionToGcs('session-a')).rejects.toThrow('권한이 없습니다.');
  });
});
