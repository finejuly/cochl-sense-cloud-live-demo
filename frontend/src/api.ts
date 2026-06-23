import type { AnalysisResponse, LiveChunkAnalysisResponse } from './types';

export interface AnalyzeLiveChunkInput {
  file: Blob;
  sessionId: string;
  sequenceId: number;
  windowStartSec: number;
  windowEndSec: number;
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
