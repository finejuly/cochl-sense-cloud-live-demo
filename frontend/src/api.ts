import type {
  CollectedSessionsResponse,
  GcsSessionUploadResponse,
  GcsUploadFileProgress,
  LiveChunkAnalysisResponse,
  LiveSessionEndResponse,
  RuntimeConfig,
} from './types';

let cachedRuntimeConfig: RuntimeConfig | null = null;
let runtimeConfigRequest: Promise<RuntimeConfig> | null = null;
let runtimeConfigRequestSequence = 0;
let cachedRuntimeConfigSequence = 0;

export async function fetchRuntimeConfig(
  {
    force = false,
    signal,
  }: { force?: boolean; signal?: AbortSignal } = {},
): Promise<RuntimeConfig> {
  if (!force && cachedRuntimeConfig) {
    return cachedRuntimeConfig;
  }
  if (!force && !signal && runtimeConfigRequest) {
    return runtimeConfigRequest;
  }

  const requestSequence = runtimeConfigRequestSequence + 1;
  runtimeConfigRequestSequence = requestSequence;
  const responsePromise = signal
    ? fetch('/api/runtime-config', { signal })
    : fetch('/api/runtime-config');
  const request = responsePromise
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(await readErrorMessage(response));
      }
      const config: unknown = await response.json();
      if (!isRuntimeConfig(config)) {
        throw new Error('로컬 API 실행 설정이 올바르지 않습니다.');
      }
      // A slower, older forced refresh must not replace a token fetched by a
      // newer request. This matters around local server restarts, when several
      // state-changing requests can all discover an expired token together.
      if (requestSequence >= cachedRuntimeConfigSequence) {
        cachedRuntimeConfig = config;
        cachedRuntimeConfigSequence = requestSequence;
      }
      return config;
    })
    .finally(() => {
      if (runtimeConfigRequest === request) {
        runtimeConfigRequest = null;
      }
    });
  runtimeConfigRequest = request;
  return request;
}

export interface AnalyzeLiveChunkInput {
  file: Blob;
  sessionId: string;
  sequenceId: number;
  windowStartSec: number;
  windowEndSec: number;
  sessionName?: string;
  signal?: AbortSignal;
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

  const response = await fetchWithLocalAuth('/api/analyze-live-chunk', {
    method: 'POST',
    body: formData,
    signal: input.signal,
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
  signal?: AbortSignal,
): Promise<LiveSessionEndResponse> {
  const formData = new FormData();
  formData.append('session_id', sessionId);
  if (sessionName) {
    formData.append('session_name', sessionName);
  }

  const response = await fetchWithLocalAuth('/api/live-session/end', {
    method: 'POST',
    body: formData,
    signal,
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

export async function uploadCollectedSessionToGcs(
  sessionId: string,
  onProgress?: (progress: GcsUploadFileProgress) => void,
): Promise<GcsSessionUploadResponse> {
  const response = await fetchWithLocalAuth(
    `/api/collected-sessions/${encodeURIComponent(sessionId)}/gcs-upload/progress`,
    {
      method: 'POST',
      headers: { Accept: 'application/x-ndjson' },
    },
  );

  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message);
  }

  if (!response.body) {
    throw new Error('GCS 업로드 진행 정보를 읽지 못했습니다.');
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffered = '';
  let result: GcsSessionUploadResponse | null = null;

  function consumeLine(line: string) {
    const trimmed = line.trim();
    if (!trimmed) {
      return;
    }

    let event: GcsUploadStreamEvent;
    try {
      event = JSON.parse(trimmed) as GcsUploadStreamEvent;
    } catch {
      throw new Error('GCS 업로드 진행 정보가 올바르지 않습니다.');
    }

    if (event.type === 'progress') {
      onProgress?.({
        object_name: event.object_name,
        source_filename: event.source_filename,
        file_status: event.file_status,
        completed_file_count: event.completed_file_count,
        total_file_count: event.total_file_count,
      });
      return;
    }
    if (event.type === 'complete') {
      result = {
        status: event.status,
        session_id: event.session_id,
        object_prefix: event.object_prefix,
        snapshot_id: event.snapshot_id,
        uploaded_file_count: event.uploaded_file_count,
        existing_file_count: event.existing_file_count,
        total_size_bytes: event.total_size_bytes,
      };
      return;
    }
    if (event.type === 'error') {
      throw new Error(event.message || 'GCS 업로드에 실패했습니다.');
    }
    throw new Error('알 수 없는 GCS 업로드 진행 정보입니다.');
  }

  try {
    while (true) {
      const { done, value } = await reader.read();
      buffered += decoder.decode(value, { stream: !done });
      const lines = buffered.split('\n');
      buffered = lines.pop() ?? '';
      lines.forEach(consumeLine);
      if (done) {
        break;
      }
    }
    consumeLine(buffered);
  } catch (error) {
    await reader.cancel().catch(() => undefined);
    throw error;
  } finally {
    reader.releaseLock();
  }

  if (!result) {
    throw new Error('GCS 업로드 완료 응답을 받지 못했습니다.');
  }
  return result;
}

type GcsUploadStreamEvent =
  | ({ type: 'progress' } & GcsUploadFileProgress)
  | ({ type: 'complete' } & GcsSessionUploadResponse)
  | { type: 'error'; message: string };

export async function deleteCollectedSession(sessionId: string): Promise<void> {
  const response = await fetchWithLocalAuth(`/api/collected-sessions/${encodeURIComponent(sessionId)}`, {
    method: 'DELETE',
  });

  if (!response.ok) {
    const message = await readErrorMessage(response);
    throw new Error(message);
  }
}

export async function deleteCollectedSegment(sessionId: string, filename: string): Promise<void> {
  const response = await fetchWithLocalAuth(
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

async function fetchWithLocalAuth(
  input: RequestInfo | URL,
  init: RequestInit,
): Promise<Response> {
  const request = async (forceConfig: boolean) => {
    const config = forceConfig
      ? await fetchRuntimeConfig({ force: true, signal: init.signal ?? undefined })
      : (cachedRuntimeConfig ?? await fetchRuntimeConfig({
        force: true,
        signal: init.signal ?? undefined,
      }));
    return fetch(input, {
      ...init,
      headers: {
        ...(init.headers as Record<string, string> | undefined),
        'X-Cochl-Local-Token': config.api_token,
      },
    });
  };

  let response = await request(false);
  if ((response.status === 401 || response.status === 403) && !init.signal?.aborted) {
    response = await request(true);
  }
  return response;
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

function isRuntimeConfig(value: unknown): value is RuntimeConfig {
  if (!value || typeof value !== 'object') {
    return false;
  }
  const config = value as Partial<RuntimeConfig>;
  const capabilities = config.capabilities;
  return typeof config.collection_confidence_threshold === 'number'
    && Number.isFinite(config.collection_confidence_threshold)
    && config.collection_confidence_threshold >= 0
    && config.collection_confidence_threshold <= 1
    && typeof config.api_token === 'string'
    && Boolean(config.api_token.trim())
    && Boolean(capabilities)
    && typeof capabilities?.gcs === 'boolean';
}
