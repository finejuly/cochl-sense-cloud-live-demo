import { describe, expect, it, vi } from 'vitest';
import { extensionForMimeType, fileFromRecordingChunks, selectSupportedMimeType } from './recorder';

describe('recorder utilities', () => {
  it('prefers ogg when the browser supports it', () => {
    const isTypeSupported = vi.fn((type: string) => type === 'audio/ogg;codecs=opus');

    expect(selectSupportedMimeType(isTypeSupported)).toBe('audio/ogg;codecs=opus');
  });

  it('falls back to webm when ogg is unavailable', () => {
    const isTypeSupported = vi.fn((type: string) => type === 'audio/webm;codecs=opus');

    expect(selectSupportedMimeType(isTypeSupported)).toBe('audio/webm;codecs=opus');
  });

  it('maps mime types to upload file extensions', () => {
    expect(extensionForMimeType('audio/ogg;codecs=opus')).toBe('ogg');
    expect(extensionForMimeType('audio/webm;codecs=opus')).toBe('webm');
  });

  it('preserves the actual recorded chunk type when creating the upload file', () => {
    const chunk = new Blob([new Uint8Array([1, 2, 3])], { type: 'audio/mp4' });

    const file = fileFromRecordingChunks([chunk], 'audio/webm', 123);

    expect(file.type).toBe('audio/mp4');
    expect(file.name).toBe('recording-123.m4a');
  });
});
