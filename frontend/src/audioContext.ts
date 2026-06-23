export type AudioContextConstructor = new () => AudioContext;

export function getAudioContextConstructor(): AudioContextConstructor | null {
  const audioWindow = window as Window & typeof globalThis & {
    webkitAudioContext?: AudioContextConstructor;
  };
  return audioWindow.AudioContext ?? audioWindow.webkitAudioContext ?? null;
}
