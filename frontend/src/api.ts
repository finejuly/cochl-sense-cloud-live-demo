import type {
  AnalysisResponse,
  CollectedSessionsResponse,
  LiveChunkAnalysisResponse,
  LiveSessionEndResponse,
} from './types';

export interface AnalyzeLiveChunkInput {
  file: Blob;
  sessionId: string;
  sequenceId: number;
  windowStartSec: number;
  windowEndSec: number;
  sessionName?: string;
}

export async function analyzeRecording(file: File): Promise<AnalysisResponse> {
  const formData = new FormData();
  formData.append('file', file);

  const response = await fetch('/api/analyze-recording', {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message);
  }

  return response.json() as Promise<AnalysisResponse>;
}

export async function analyzeLiveChunk(input: AnalyzeLiveChunkInput): Promise<LiveChunkAnalysisResponse> {
  const formData = new FormData();
  formData.append('file', input.file, liveChunkUploadFilename(input));
  formData.append('session_id', input.sessionId);
  formData.append('sequence_id', String(input.sequenceId));
  formData.append('window_start_sec', String(input.windowStartSec));
  formData.append('window_end_sec', String(input.windowEndSec));
  if (input.sessionName) {
    formData.append('session_name', input.sessionName);
  }

  const response = await fetch('/api/analyze-live-chunk', {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message);
  }

  return response.json() as Promise<LiveChunkAnalysisResponse>;
}

export async function endLiveSession(
  sessionId: string,
  sessionName?: string,
): Promise<LiveSessionEndResponse> {
  const formData = new FormData();
  formData.append('session_id', sessionId);
  if (sessionName) {
    formData.append('session_name', sessionName);
  }

  const response = await fetch('/api/live-session/end', {
    method: 'POST',
    body: formData,
  });

  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message);
  }

  return response.json() as Promise<LiveSessionEndResponse>;
}

export async function fetchCollectedSessions(): Promise<CollectedSessionsResponse> {
  const response = await fetch('/api/collected-sessions');

  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message);
  }

  return response.json() as Promise<CollectedSessionsResponse>;
}

export async function deleteCollectedSession(sessionId: string): Promise<void> {
  const response = await fetch(`/api/collected-sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });

  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message);
  }
}

export async function deleteCollectedSegment(sessionId: string, filename: string): Promise<void> {
  const response = await fetch(
    `/api/collected-sessions/${encodeURIComponent(sessionId)}/segments/${encodeURIComponent(filename)}`,
    { method: 'DELETE' },
  );

  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message);
  }
}

export function collectedFileUrl(sessionId: string, filename: string): string {
  return `/api/collected-sessions/${encodeURIComponent(sessionId)}/files/${encodeURIComponent(filename)}`;
}

function liveChunkUploadFilename(input: AnalyzeLiveChunkInput): string {
  const sequence = String(input.sequenceId).padStart(6, '0');
  return `chunk-${sequence}-${input.windowStartSec.toFixed(3)}-${input.windowEndSec.toFixed(3)}.wav`;
}

async function readErrorMessage(response: Response): Promise<string> {
  try {
    const body = (await response.json()) as { detail?: string };
    if (body.detail) {
      return body.detail;
    }
  } catch {
    // Fall back to the status text below.
  }
  return response.statusText || '분석 요청에 실패했습니다.';
}
