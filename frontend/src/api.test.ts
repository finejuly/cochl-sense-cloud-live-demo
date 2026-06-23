import { afterEach, describe, expect, it, vi } from 'vitest';
import { analyzeLiveChunk } from './api';

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
