export const MIME_TYPE_CANDIDATES = [
  'audio/ogg;codecs=opus',
  'audio/webm;codecs=opus',
  'audio/webm',
  'audio/mp4',
] as const;

export function selectSupportedMimeType(
  isTypeSupported: (mimeType: string) => boolean = MediaRecorder.isTypeSupported,
): string {
  return MIME_TYPE_CANDIDATES.find((mimeType) => isTypeSupported(mimeType)) ?? '';
}

export function extensionForMimeType(mimeType: string): string {
  const normalized = mimeType.split(';')[0].trim().toLowerCase();
  if (normalized === 'audio/ogg') {
    return 'ogg';
  }
  if (normalized === 'audio/webm') {
    return 'webm';
  }
  if (normalized === 'audio/mp4') {
    return 'm4a';
  }
  if (normalized === 'audio/wav') {
    return 'wav';
  }
  return 'webm';
}

export function fileFromRecordingChunks(
  chunks: Blob[],
  fallbackMimeType: string,
  recordedAt: number = Date.now(),
): File {
  const actualMimeType = chunks.find((chunk) => chunk.type)?.type || fallbackMimeType || 'audio/webm';
  const blob = new Blob(chunks, { type: actualMimeType });
  const extension = extensionForMimeType(actualMimeType);
  return new File([blob], `recording-${recordedAt}.${extension}`, { type: actualMimeType });
}
